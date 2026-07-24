# _PsDeobLib.ps1 — Shared PowerShell deobfuscation library
# Parse-only: the target script is never executed — only Parser::ParseInput is used.
# Dot-source this file to bring all helpers and Invoke-Ps* functions into scope.

# Module-level purity oracle (constant — never mutated at runtime).
$pureCommands = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
@('Get-Random','Get-Date','New-Object','Get-Item','Measure-Object',
  'Select-Object','Sort-Object','Where-Object','ForEach-Object',
  '%','?','Out-Null') | ForEach-Object { [void]$pureCommands.Add($_) }

# Allowlist of pure, deterministic String instance methods that Resolve-ConstImpl is willing to
# evaluate at analysis time (obfuscators frequently rebuild literals via chains of these calls,
# e.g. 'seed'.Remove(1,2).Insert(0,'x')...). Only scalar-in/scalar-out methods with no I/O,
# reflection, or environment dependency are listed here -- never .Split()/.Format()/anything
# returning a collection or invoking a delegate. Static calls are rejected before this table is
# even consulted (see the InvokeMemberExpressionAst branch below). Each entry is a scriptblock
# taking ($s, $argList) so a new method can be added without touching the resolver itself.
$script:CffAllowedStringMethods = @{
    'Remove'    = { param($s,$a)
                    if ($a.Count -notin 1,2) { throw 'arity' }
                    if ($a.Count -eq 1) { $s.Remove([int]$a[0]) } else { $s.Remove([int]$a[0],[int]$a[1]) } }
    'Insert'    = { param($s,$a) if ($a.Count -ne 2) { throw 'arity' }; $s.Insert([int]$a[0],[string]$a[1]) }
    'Replace'   = { param($s,$a) if ($a.Count -ne 2) { throw 'arity' }; $s.Replace([string]$a[0],[string]$a[1]) }
    'Substring' = { param($s,$a)
                    if ($a.Count -notin 1,2) { throw 'arity' }
                    if ($a.Count -eq 1) { $s.Substring([int]$a[0]) } else { $s.Substring([int]$a[0],[int]$a[1]) } }
    'PadLeft'   = { param($s,$a)
                    if ([int]$a[0] -gt 100000) { throw 'cap' }
                    if ($a.Count -eq 1) { $s.PadLeft([int]$a[0]) } else { $s.PadLeft([int]$a[0],[char]$a[1]) } }
    'PadRight'  = { param($s,$a)
                    if ([int]$a[0] -gt 100000) { throw 'cap' }
                    if ($a.Count -eq 1) { $s.PadRight([int]$a[0]) } else { $s.PadRight([int]$a[0],[char]$a[1]) } }
    'ToUpper'   = { param($s,$a) $s.ToUpper() }
    'ToLower'   = { param($s,$a) $s.ToLower() }
    'Trim'      = { param($s,$a) if ($a.Count -eq 0) { $s.Trim() } else { $s.Trim([char[]]$a) } }
    'TrimStart' = { param($s,$a) if ($a.Count -eq 0) { $s.TrimStart() } else { $s.TrimStart([char[]]$a) } }
    'TrimEnd'   = { param($s,$a) if ($a.Count -eq 0) { $s.TrimEnd() } else { $s.TrimEnd([char[]]$a) } }
}

# Instance methods whose only mutation is to the RECEIVER's own object — the arguments are read-only
# inputs, never written back through (unlike e.g. .CopyTo(arr,i)/.TryGetValue(k,[ref]o), which mutate
# an argument and are deliberately excluded). Used by the aggressive dead-code pass to recognize a
# bare `$v.Clear()` / `$v.Add(...)` mutator statement as a removable "writer" of $v — but ONLY when $v
# is provably a fresh, unaliased local (see Test-FreshConfinedVar), so that mutating its object can
# never be observed anywhere else. Kept narrow on purpose: a name not on this list is simply left in
# place (a missed deobfuscation, never a corruption).
$script:PsReceiverOnlyMutators = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
@('Add','AddRange','Clear','Insert','InsertRange','Remove','RemoveAt','RemoveRange',
  'Push','Enqueue','Append','AppendLine') | ForEach-Object { [void]$script:PsReceiverOnlyMutators.Add($_) }

# ---------------------------------------------------------------------------
# Stateless helpers (module-level, usable by any pass)
# ---------------------------------------------------------------------------

function Get-VarName($v) { $v.VariablePath.UserPath.ToLowerInvariant() }

function Expand-SemiDeleteRange([string]$raw, [int]$start, [int]$end) {
    $j = $end
    while ($j -lt $raw.Length -and ($raw[$j] -eq ' ' -or $raw[$j] -eq "`t")) { $j++ }
    if ($j -lt $raw.Length -and $raw[$j] -eq ';') {
        $end = $j + 1
        while ($end -lt $raw.Length -and ($raw[$end] -eq ' ' -or $raw[$end] -eq "`t")) { $end++ }
    } else {
        $k = $start - 1
        while ($k -ge 0 -and ($raw[$k] -eq ' ' -or $raw[$k] -eq "`t")) { $k-- }
        if ($k -ge 0 -and $raw[$k] -eq ';') { $start = $k }
    }
    return [pscustomobject]@{ Start = $start; End = $end }
}

# A small, explicit allowlist of .NET methods known to be pure (no I/O, no mutation, deterministic)
# regardless of how their result is consumed (assigned, [void]-cast, or a bare discarded statement).
# Deliberately narrow — NOT "any [void](...) call is pure": that would wrongly exempt e.g.
# [void]$sb.Append(...), where Append has a real, intended mutating side effect and [void] only
# suppresses its fluent return value. Only matches the literal `[System.Text.Encoding]::X.GetBytes/
# GetString(...)` static-property-then-instance-method shape, never an arbitrary variable's method of
# the same name (e.g. a custom encoder held in a variable).
function Test-PureNetMethodInvoke($inv) {
    $name = if ($inv.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $inv.Member.Value } else { $null }
    if ($null -eq $name -or $name -notin @('GetBytes','GetString')) { return $false }
    $recv = $inv.Expression
    if ($recv -isnot [System.Management.Automation.Language.MemberExpressionAst] -or -not $recv.Static) { return $false }
    if ($recv.Expression -isnot [System.Management.Automation.Language.TypeExpressionAst]) { return $false }
    if ($recv.Expression.TypeName.FullName -notmatch '(?i)^(System\.Text\.)?Encoding$') { return $false }
    # The method is pure, but its ARGUMENTS still execute — a side effect nested in an argument
    # (e.g. GetString($sb.Append(...)) or GetString([IO.File]::ReadAllBytes(...))) must NOT be
    # discarded along with the call. Require every argument to be side-effect-free: no command
    # calls, and no method invocations other than further allowlisted-pure ones. The receiver is
    # safe by construction — it can only be a static property access on the Encoding type.
    if ($null -ne $inv.Arguments) {
        foreach ($arg in $inv.Arguments) {
            if (($arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)).Count -gt 0) { return $false }
            foreach ($m in $arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
                if (-not (Test-PureNetMethodInvoke $m)) { return $false }
            }
        }
    }
    return $true
}

# Is an assignment RHS a "fresh" value — a newly-constructed or immutable object that cannot already
# be referenced through another name? Fresh: literals/constants, @(...) / 1,2,3 / @{...},
# `New-Object ...`, and `[Type]::new(...)`. Deliberately conservative: anything else (a bare `$other`,
# a cast, a method result, a command result) returns $false, meaning "may alias an external object".
# Returning $false only ever KEEPS more code, so unrecognized-but-actually-fresh forms are safe misses.
# Handles both AST shapes for the RHS: PipelineAst (PS7) and a bare CommandExpressionAst/CommandAst
# (Windows PowerShell 5.1).
function Test-FreshValueRhs($rightAst) {
    $expr = $null; $cmd = $null
    if ($rightAst -is [System.Management.Automation.Language.PipelineAst]) {
        if ($rightAst.PipelineElements.Count -ne 1) { return $false }
        $el = $rightAst.PipelineElements[0]
        if ($el -is [System.Management.Automation.Language.CommandExpressionAst]) { $expr = $el.Expression }
        elseif ($el -is [System.Management.Automation.Language.CommandAst]) { $cmd = $el }
        else { return $false }
    } elseif ($rightAst -is [System.Management.Automation.Language.CommandExpressionAst]) {
        $expr = $rightAst.Expression
    } elseif ($rightAst -is [System.Management.Automation.Language.CommandAst]) {
        $cmd = $rightAst
    } else { return $false }

    if ($null -ne $cmd) { return ($cmd.GetCommandName() -eq 'New-Object') }

    if ($expr -is [System.Management.Automation.Language.ArrayExpressionAst])  { return $true }   # @(...)
    if ($expr -is [System.Management.Automation.Language.ArrayLiteralAst])     { return $true }   # 1,2,3
    if ($expr -is [System.Management.Automation.Language.HashtableAst])        { return $true }   # @{...}
    if ($expr -is [System.Management.Automation.Language.ConstantExpressionAst] -or
        $expr -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return $true }
    if ($expr -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $expr.Static) {
        $m = $expr.Member
        if ($m -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $m.Value -eq 'new') { return $true }  # [T]::new(...)
    }
    return $false
}

# A local variable is "fresh-confined" when EVERY `=` assignment to it in the whole script binds a
# fresh value (Test-FreshValueRhs). Then the object the name holds can never have been aliased IN from
# another name. Combined with the caller's escape check (any read of the name outside its own writers
# pins it, which catches aliasing OUT), this proves the object is reachable through this one name only
# — so deleting an in-place mutation of it (e.g. a bare `$v.Clear()`) is unobservable. Requires at
# least one such `=` initializer: a name only ever mutated but never `=`-initialized in-script may
# hold a parameter/outer-scope object and is treated as NOT confined.
function Test-FreshConfinedVar([string]$name, $assignNodes) {
    $sawFreshInit = $false
    foreach ($a in $assignNodes) {
        if ($a.Operator.ToString() -ne 'Equals') { continue }
        if ($a.Left -isnot [System.Management.Automation.Language.VariableExpressionAst]) { continue }
        if ((Get-VarName $a.Left) -ne $name) { continue }
        if (-not (Test-FreshValueRhs $a.Right)) { return $false }
        $sawFreshInit = $true
    }
    return $sawFreshInit
}

function Test-HasImpureCommand($astNode, $OwnedIncrementVars = $null) {
    foreach ($cmd in $astNode.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $name = $cmd.GetCommandName()
        if ($null -eq $name) { return $true }
        if ($pureCommands.Contains($name)) { continue }
        return $true
    }
    # Assignment to an index or member target (`$arr[$i] = ...`, `$obj.Prop = ...`, and their compound
    # forms) MUTATES aliased state — the array/object may be referenced elsewhere, and the write is not
    # tracked by the variable-name liveness (AssignmentStatementAst.Left is an Index/MemberExpressionAst,
    # not a VariableExpressionAst, so it never enters AssignedVars). Without treating it as impure, a
    # loop whose only body is such a write (e.g. an XOR-decode `for(;;$i++){ $out[$i] = ... }`) would
    # look purely bookkeeping once its own `$i++` stops counting, and be deleted as a "non-functional
    # loop" even though it produces $out. Flag it here so every purity gate keeps such constructs.
    foreach ($asgn in $astNode.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        $lhs = $asgn.Left
        while ($lhs -is [System.Management.Automation.Language.ConvertExpressionAst]) { $lhs = $lhs.Child }
        if ($lhs -is [System.Management.Automation.Language.IndexExpressionAst] -or
            $lhs -is [System.Management.Automation.Language.MemberExpressionAst]) { return $true }
    }
    # Statement-level .NET method invocations have side effects (e.g. $fs.Write(...), $fs.Dispose())
    foreach ($inv in $astNode.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        if (Test-PureNetMethodInvoke $inv) { continue }
        $p = $inv.Parent
        while ($p -is [System.Management.Automation.Language.ConvertExpressionAst]) { $p = $p.Parent }
        if ($p -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $pp = $p.Parent
            if ($pp -is [System.Management.Automation.Language.PipelineAst]) {
                $ppp = $pp.Parent
                if (-not ($ppp -is [System.Management.Automation.Language.AssignmentStatementAst] -and $ppp.Right -eq $pp)) {
                    return $true
                }
            }
        }
    }
    # Pre/post increment and decrement (`$x++`, `--$x`) MUTATE their operand — a real side effect,
    # not a pure discardable expression. Without this, the pure-statement pass would delete a bare
    # `$x++` even when $x is read afterward (silently changing its value), and the loop/dead-store
    # classifiers would treat an increment-only construct as removable. Flagged here so every
    # consumer (pure statements, loops, dead stores, effect-free-block checks) treats them uniformly.
    #
    # $OwnedIncrementVars (loop gate only): a set of the *local* variable names a loop assigns as its
    # own bookkeeping (iterator/accumulator). An increment of one of those (`$i++` in `for(;;$i++)`,
    # `$counter++` in a while body) is not an externally-observable side effect — whether it matters is
    # already decided by the caller's AssignedVars liveness check, which keeps the loop if the var is
    # read elsewhere. So for that caller we skip owned-local increments here instead of reflexively
    # disqualifying the whole loop. Every other caller passes $null and keeps the original strict
    # behavior. Increments of a non-owned or scoped name (e.g. `$global:hits++`) stay impure, and a
    # non-variable operand (`$arr[0]++`) is never "owned" and stays impure.
    foreach ($u in $astNode.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
        if ($u.TokenKind.ToString() -in @('PlusPlus','MinusMinus','PostfixPlusPlus','PostfixMinusMinus')) {
            if ($null -ne $OwnedIncrementVars -and
                $u.Child -is [System.Management.Automation.Language.VariableExpressionAst] -and
                $OwnedIncrementVars.Contains((Get-VarName $u.Child))) { continue }
            return $true
        }
    }
    return $false
}

# Condition-owning statement types: a PipelineAst whose immediate .Parent is one of these directly
# (never a StatementBlockAst/NamedBlockAst) is that statement's own condition/collection syntax, not
# a discarded body statement — e.g. the `$arr` in `foreach ($y in $arr)`.
$script:ConditionOwnerAstTypes = @(
    [System.Management.Automation.Language.IfStatementAst],
    [System.Management.Automation.Language.WhileStatementAst],
    [System.Management.Automation.Language.DoWhileStatementAst],
    [System.Management.Automation.Language.DoUntilStatementAst],
    [System.Management.Automation.Language.ForStatementAst],
    [System.Management.Automation.Language.ForEachStatementAst],
    [System.Management.Automation.Language.SwitchStatementAst]
)

function Test-EffectFreeBlock($blockAst) {
    if ($null -eq $blockAst) { return $true }
    if (Test-HasImpureCommand $blockAst) { return $false }
    # Control-flow jumps change loop/function execution path and are never effect-free
    foreach ($node in $blockAst.FindAll({
            param($x) $x -is [System.Management.Automation.Language.BreakStatementAst] -or
                      $x -is [System.Management.Automation.Language.ContinueStatementAst] -or
                      $x -is [System.Management.Automation.Language.ReturnStatementAst] -or
                      $x -is [System.Management.Automation.Language.ThrowStatementAst] -or
                      $x -is [System.Management.Automation.Language.ExitStatementAst] }, $true)) {
        return $false
    }
    foreach ($node in $blockAst.FindAll({
            param($x) $x -is [System.Management.Automation.Language.CommandExpressionAst] }, $true)) {
        $parent = $node.Parent
        if ($parent -is [System.Management.Automation.Language.PipelineAst]) {
            $gp = $parent.Parent
            # A bare discarded expression only "counts" against effect-freedom when it sits in a real
            # statement position. Array-literal elements (`1,2,3` in `@(1,2,3)`) and a loop/if/switch's
            # own condition/collection syntax reuse the same node types but are not executed,
            # value-discarding statements — exempt both alongside the existing assignment-RHS case.
            $isAssignmentRhs = ($gp -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                                 $gp.Right -eq $parent)
            $isArrayLiteralData = ($gp -is [System.Management.Automation.Language.StatementBlockAst] -and
                                    $gp.Parent -is [System.Management.Automation.Language.ArrayExpressionAst])
            $isConditionSlot = $false
            foreach ($ty in $script:ConditionOwnerAstTypes) {
                if ($gp -is $ty) { $isConditionSlot = $true; break }
            }
            if (-not $isAssignmentRhs -and -not $isArrayLiteralData -and -not $isConditionSlot) {
                return $false
            }
        }
    }
    foreach ($a in $blockAst.FindAll({
            param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $n = $a.Left.VariablePath.UserPath.ToLowerInvariant()
            if ($n -match '^(global|script|env|using):') { return $false }
        }
    }
    return $true
}

# Function-body variant of the effect-free test, used ONLY by the aggressive no-op-function pass.
# Test-EffectFreeBlock (above) is deliberately left untouched: its other caller ($ifNodes) needs the
# strict rule that a `return`/`break`/`continue` inside an `if` body is real control flow for the
# ENCLOSING function. A function body is a different scope boundary — `return` there just means
# "emit this value and leave the function", which is precisely the behavior the no-op pass is
# deleting (every call site is already proven result-discarded before this predicate matters). So
# `return` is allowed here, and so is a bare value-emitting statement (`$x + 0` with no `return`),
# the implicit-output spelling of the same thing — consistent with the pure-statement pass, which
# already removes a bare `$x` / `"literal"` statement in DEFAULT mode.
#
# Takes the FunctionDefinitionAst, not just the body: short-form parameters (`function f($a = …)`)
# hang off .Parameters, OUTSIDE .Body, so a body-only scan never sees their default values or
# attribute arguments. Everything below is a rejection — an unrecognized shape returns $false, which
# only ever KEEPS the function (a missed deobfuscation, never a corruption).
function Test-EffectFreeFunctionBody($fnAst) {
    if ($null -eq $fnAst) { return $false }
    $body = $fnAst.Body
    if ($null -eq $body) { return $false }

    # Pipeline-aware functions have per-object begin/process/end semantics this analysis does not
    # model, and a dynamicparam block runs arbitrary code at binding time.
    if ($null -ne $body.DynamicParamBlock -or
        $null -ne $body.BeginBlock -or
        $null -ne $body.ProcessBlock) { return $false }

    # The shared purity oracle: non-allowlisted commands, index/member assignment, statement-level
    # .NET method calls, and ++/-- are all already rejected by this one call.
    if (Test-HasImpureCommand $body) { return $false }

    # Parameter default values and attribute arguments (e.g. `$x = (Get-Date)`,
    # `[ValidateScript({ Get-Content … })]`) execute at call time. A ParameterAst is an Ast, so the
    # same oracle reaches both.
    foreach ($p in @($fnAst.Parameters)) {
        if ($null -ne $p -and (Test-HasImpureCommand $p)) { return $false }
    }

    # throw / exit / trap escape the function and change the CALLER's control flow or error state.
    if (($body.FindAll({
            param($x) $x -is [System.Management.Automation.Language.ThrowStatementAst] -or
                      $x -is [System.Management.Automation.Language.ExitStatementAst] -or
                      $x -is [System.Management.Automation.Language.TrapStatementAst] }, $true)).Count -gt 0) {
        return $false
    }

    # break / continue are only harmless when BOUND to a loop or switch inside this same body. An
    # unbound one propagates out of the function into the caller's loop (real PowerShell semantics),
    # so it is a genuine external effect. Walk up to — but never past — the body; a labeled jump can
    # target an outer construct and is always rejected; a ScriptBlockExpressionAst boundary means a
    # deferred delegate whose invocation context is unknown, so stop and reject there too.
    foreach ($j in $body.FindAll({
            param($x) $x -is [System.Management.Automation.Language.BreakStatementAst] -or
                      $x -is [System.Management.Automation.Language.ContinueStatementAst] }, $true)) {
        if ($null -ne $j.Label) { return $false }
        $p = $j.Parent; $bound = $false
        while ($null -ne $p -and $p -ne $body) {
            if ($p -is [System.Management.Automation.Language.LoopStatementAst] -or
                $p -is [System.Management.Automation.Language.SwitchStatementAst]) { $bound = $true; break }
            if ($p -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) { break }
            $p = $p.Parent
        }
        if (-not $bound) { return $false }
    }

    # A scoped write outlives the call (same rule as Test-EffectFreeBlock). A plain `$x = …` is
    # function-local in PowerShell — it shadows rather than mutates an outer name — so it is fine.
    foreach ($a in $body.FindAll({
            param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            if ((Get-VarName $a.Left) -match '^(global|script|env|using):') { return $false }
        }
    }

    return $true
}

# Is a PipelineAst's parent a "real" statement position — normal sequential script flow (root script,
# or any function body at any nesting depth) as opposed to the body of a scriptblock *literal* passed
# as a value/callback (e.g. `ForEach-Object { ... }`, `$sb = { ... }`)? Both shapes use NamedBlockAst
# for their direct statement list, distinguished only by what the enclosing ScriptBlockAst's own
# .Parent is: $null (true root) or FunctionDefinitionAst (a function body) for real flow, vs
# ScriptBlockExpressionAst for a scriptblock literal used as a deferred delegate — deleting a
# "looks pure" statement out of a delegate body that may be invoked later in an unknown context is a
# different, riskier judgment than deleting one sitting in normal script flow, so that case stays
# excluded exactly as it was before this function existed.
function Test-RealStatementPosition($pipelineParent) {
    if ($pipelineParent -is [System.Management.Automation.Language.StatementBlockAst]) {
        # Array-literal elements (`1,2,3` in `@(1,2,3)`) also sit inside a StatementBlockAst, but that
        # block's own .Parent is the ArrayExpressionAst — that's data, not an executed statement
        # (mirrors the same exclusion already applied in Test-EffectFreeBlock above).
        return $pipelineParent.Parent -isnot [System.Management.Automation.Language.ArrayExpressionAst]
    }
    if ($pipelineParent -is [System.Management.Automation.Language.NamedBlockAst]) {
        $sb = $pipelineParent.Parent
        if ($sb -is [System.Management.Automation.Language.ScriptBlockAst]) {
            return ($null -eq $sb.Parent) -or ($sb.Parent -is [System.Management.Automation.Language.FunctionDefinitionAst])
        }
    }
    return $false
}

function Get-CondExpr($condAst) {
    if ($condAst -is [System.Management.Automation.Language.PipelineAst] -and
        $condAst.PipelineElements.Count -eq 1 -and
        $condAst.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
        return $condAst.PipelineElements[0].Expression
    }
    return $null
}

# A construct whose value is captured by an enclosing value context is functional, not dead: the
# bare expressions it emits are real data flow (e.g. an XOR-decrypt `for` loop feeding a reflective
# call). Such a construct must not be treated as a pure/removable loop, and its internal variable
# reads must count as live. Value-consuming ancestors:
#   * $(...) SubExpressionAst / @(...) ArrayExpressionAst — explicit capture wrappers.
#   * AssignmentStatementAst — a loop can only appear on the RHS (`$x = for(...){...}`; the loop's
#     emitted values become $x), never as the LHS target, so any assignment ancestor means the
#     value flows into the assignment.
# Walk ancestors: any of the above reached before the enclosing NamedBlockAst (function/script/
# named-block boundary) means the value is consumed.
function Test-ValueConsumed($astNode) {
    $p = $astNode.Parent
    while ($null -ne $p) {
        if ($p -is [System.Management.Automation.Language.SubExpressionAst] -or
            $p -is [System.Management.Automation.Language.ArrayExpressionAst]) { return $true }
        if ($p -is [System.Management.Automation.Language.AssignmentStatementAst]) { return $true }
        if ($p -is [System.Management.Automation.Language.NamedBlockAst]) { return $false }
        $p = $p.Parent
    }
    return $false
}

function Get-Base64Literal($invokeNode) {
    if ($invokeNode -isnot [System.Management.Automation.Language.InvokeMemberExpressionAst]) { return $null }
    $m = $invokeNode.Member
    if ($m -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $null }
    if ($m.Value -ne 'FromBase64String') { return $null }
    $t = $invokeNode.Expression
    if ($t -isnot [System.Management.Automation.Language.TypeExpressionAst]) { return $null }
    if ($t.TypeName.FullName -notmatch '(?i)^(System\.)?Convert$') { return $null }
    if ($invokeNode.Arguments.Count -ne 1) { return $null }
    $arg = $invokeNode.Arguments[0]
    if ($arg -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $null }
    return $arg.Value
}

# ---------------------------------------------------------------------------
# Context-dependent helpers (caller passes AST-derived state explicitly)
# ---------------------------------------------------------------------------

function Test-FoldableNull([string]$name, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    if ($hasStrictMode) { return $false }
    if ($name -match ':') { return $false }
    if ($reservedVars.Contains($name)) { return $false }
    return -not $assignedVars.Contains($name)
}

# $ConstVars (optional) is an IDictionary of lowercased varname -> resolved value. When supplied,
# a variable read resolves to its mapped value. Default $null keeps the original conservative
# behavior (only $true/$false/$null + the foldable-null heuristic), so existing callers are
# unaffected. Also handles [char]/[int]/... casts (ConvertExpressionAst) and numeric arithmetic.
# $Cache (optional) is a Dictionary[Ast,object] memoizing results per AST node for the duration of
# one pass; pass it to make repeated resolutions of the same subtree O(1) (used by
# Invoke-PsFoldArithmetic, whose maximal-subtree climb re-resolves ancestors). Additive: omit it
# and behavior is identical to before.
function Resolve-Const($exprAst, [bool]$hasStrictMode, $reservedVars, $assignedVars, $ConstVars = $null, $Cache = $null) {
    if ($null -ne $Cache -and $null -ne $exprAst -and $Cache.ContainsKey($exprAst)) { return $Cache[$exprAst] }
    $res = Resolve-ConstImpl $exprAst $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
    if ($null -ne $Cache -and $null -ne $exprAst) { $Cache[$exprAst] = $res }
    return $res
}

function Resolve-ConstImpl($exprAst, [bool]$hasStrictMode, $reservedVars, $assignedVars, $ConstVars = $null, $Cache = $null) {
    $unknown = @{ Known = $false; Value = $null }
    if ($null -eq $exprAst) { return $unknown }

    if ($exprAst -is [System.Management.Automation.Language.ParenExpressionAst]) {
        $pipe = $exprAst.Pipeline
        if ($pipe -is [System.Management.Automation.Language.PipelineAst] -and
            $pipe.PipelineElements.Count -eq 1 -and
            $pipe.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
            return Resolve-Const $pipe.PipelineElements[0].Expression $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        }
        return $unknown
    }

    # $(...) subexpression -- transparently unwrap the same way (...) is unwrapped above, when it
    # contains exactly one pipeline yielding one expression. Obfuscators wrap nearly every rebuilt
    # literal in $(...), so without this, folding stops dead at the first $(...) boundary.
    if ($exprAst -is [System.Management.Automation.Language.SubExpressionAst]) {
        $stmts = $exprAst.SubExpression.Statements
        if ($stmts.Count -eq 1 -and $stmts[0] -is [System.Management.Automation.Language.PipelineAst] -and
            $stmts[0].PipelineElements.Count -eq 1 -and
            $stmts[0].PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
            return Resolve-Const $stmts[0].PipelineElements[0].Expression $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        }
        return $unknown
    }

    if ($exprAst -is [System.Management.Automation.Language.ConstantExpressionAst] -or
        $exprAst -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        return @{ Known = $true; Value = $exprAst.Value }
    }

    if ($exprAst -is [System.Management.Automation.Language.VariableExpressionAst]) {
        $n = $exprAst.VariablePath.UserPath.ToLowerInvariant()
        switch ($n) {
            'true'  { return @{ Known = $true; Value = $true  } }
            'false' { return @{ Known = $true; Value = $false } }
            'null'  { return @{ Known = $true; Value = $null  } }
        }
        if ($null -ne $ConstVars -and $ConstVars.ContainsKey($n)) {
            return @{ Known = $true; Value = $ConstVars[$n] }
        }
        if (Test-FoldableNull $n $hasStrictMode $reservedVars $assignedVars) {
            return @{ Known = $true; Value = $null }
        }
        return $unknown
    }

    # [char][int]$x style casts — resolve the child, then apply the type cast.
    if ($exprAst -is [System.Management.Automation.Language.ConvertExpressionAst]) {
        $inner = Resolve-Const $exprAst.Child $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        if (-not $inner.Known) { return $unknown }
        $tn = $exprAst.Type.TypeName.FullName.ToLowerInvariant()
        $v  = $inner.Value
        try {
            switch ($tn) {
                'char'                    { return @{ Known = $true; Value = [char][int]$v } }
                { $_ -in 'int','int32','system.int32' }   { return @{ Known = $true; Value = [int]$v } }
                { $_ -in 'long','int64','system.int64' }  { return @{ Known = $true; Value = [long]$v } }
                'byte'                    { return @{ Known = $true; Value = [byte]$v } }
                { $_ -in 'double','system.double' }       { return @{ Known = $true; Value = [double]$v } }
                { $_ -in 'string','system.string' }       { return @{ Known = $true; Value = [string]$v } }
                default { return $unknown }
            }
        } catch { return $unknown }
    }

    if ($exprAst -is [System.Management.Automation.Language.UnaryExpressionAst]) {
        $tk = $exprAst.TokenKind.ToString()
        if ($tk -eq 'Not' -or $tk -eq 'Exclamation') {
            $op = Resolve-Const $exprAst.Child $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
            if ($op.Known) { return @{ Known = $true; Value = (-not $op.Value) } }
        }
        elseif ($tk -eq 'Minus' -or $tk -eq 'Plus') {
            $op = Resolve-Const $exprAst.Child $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
            if ($op.Known -and $op.Value -isnot [string] -and $op.Value -isnot [bool]) {
                $n = if ($null -eq $op.Value) { 0 } else { $op.Value }
                try {
                    $uval = if ($tk -eq 'Minus') { -$n } else { +$n }
                    return @{ Known = $true; Value = $uval }
                } catch { return $unknown }
            }
        }
        return $unknown
    }

    if ($exprAst -is [System.Management.Automation.Language.BinaryExpressionAst]) {
        $lhs = Resolve-Const $exprAst.Left  $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        $rhs = Resolve-Const $exprAst.Right $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        if (-not ($lhs.Known -and $rhs.Known)) { return $unknown }

        $opName = $exprAst.Operator.ToString()
        $l = $lhs.Value; $r = $rhs.Value
        $ln = if ($null -eq $l) { 0 } else { $l }
        $rn = if ($null -eq $r) { 0 } else { $r }

        try {
            $result = switch ($opName) {
                'And'  { [bool]$l -and [bool]$r }
                'Or'   { [bool]$l -or  [bool]$r }
                'Ieq'  { $l  -eq  $r }
                'Ine'  { $l  -ne  $r }
                'Igt'  { $ln -gt  $rn }
                'Ige'  { $ln -ge  $rn }
                'Ilt'  { $ln -lt  $rn }
                'Ile'  { $ln -le  $rn }
                'Plus' {
                    # PowerShell concatenates text-like operands (string or char) into a string:
                    # [char]72 + [char]105 -> 'Hi'. Treat char as text so char-code string chains
                    # fold correctly. Text mixed with a number stays $unknown (string+number is
                    # parse-risky / direction-ambiguous), matching the prior conservative behavior.
                    $lTxt = ($l -is [string]) -or ($l -is [char])
                    $rTxt = ($r -is [string]) -or ($r -is [char])
                    if ($lTxt -and $rTxt) { [string]$l + [string]$r }
                    elseif ($lTxt -or $rTxt) { return $unknown }
                    else { $ln + $rn }
                }
                'Minus'    { $ln - $rn }
                'Multiply' { $ln * $rn }
                'Divide'   { if ($rn -eq 0) { return $unknown } else { $ln / $rn } }
                'Rem'      { if ($rn -eq 0) { return $unknown } else { $ln % $rn } }
                default { return $unknown }
            }
            return @{ Known = $true; Value = $result }
        } catch { return $unknown }
    }

    # Chained string-rebuild obfuscation: 'seed'.Remove(a,b).Insert(c,'d').Replace($(...),$(...))
    # Evaluate only when the method is on the pure-string allowlist above, the receiver and every
    # argument are themselves resolvable to a scalar, and the call is an instance call (never
    # static -- [Type]::Method(...) is a different, wider surface and stays out of scope here;
    # static string-builder calls like [string]::Concat/::Join get their own dedicated pass).
    if ($exprAst -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
        if ($exprAst.Static) { return $unknown }

        $memberNameNode = $exprAst.Member
        $methodName = $null
        if ($memberNameNode -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $methodName = $memberNameNode.Value
        } else {
            # Dynamic member name, e.g. $obj.$(<chain>)(...) -- resolve the name expression itself.
            $mr = Resolve-Const $memberNameNode $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
            if ($mr.Known -and $mr.Value -is [string]) { $methodName = $mr.Value } else { return $unknown }
        }
        if (-not $script:CffAllowedStringMethods.ContainsKey($methodName)) { return $unknown }

        $recv = Resolve-Const $exprAst.Expression $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        if (-not $recv.Known -or $recv.Value -isnot [string]) { return $unknown }

        $argVals = [System.Collections.Generic.List[object]]::new()
        foreach ($argAst in $exprAst.Arguments) {
            $ar = Resolve-Const $argAst $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
            if (-not $ar.Known) { return $unknown }
            if ($ar.Value -isnot [string] -and $ar.Value -isnot [int] -and $ar.Value -isnot [long] -and $ar.Value -isnot [char]) {
                return $unknown
            }
            $argVals.Add($ar.Value)
        }

        try {
            $result = & $script:CffAllowedStringMethods[$methodName] $recv.Value $argVals
        } catch { return $unknown }   # arity/bounds/out-of-range -- bail, never guess
        return @{ Known = $true; Value = $result }
    }

    # Scalar string char-index: ("abc")[1] -> 'b'. Only the SINGLE-index case belongs in
    # this scalar resolver — an array index (("abc")[0,2]) yields a collection and is
    # handled by Get-AllStringElements, never here. Constant string target indexed by one
    # resolvable integer; PowerShell negative indexing applies; out of range -> $unknown.
    if ($exprAst -is [System.Management.Automation.Language.IndexExpressionAst]) {
        if ($exprAst.Index -is [System.Management.Automation.Language.ArrayLiteralAst]) { return $unknown }
        $tgt = Resolve-Const $exprAst.Target $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        if (-not $tgt.Known -or $tgt.Value -isnot [string]) { return $unknown }
        $ir = Resolve-Const $exprAst.Index $hasStrictMode $reservedVars $assignedVars $ConstVars $Cache
        if (-not $ir.Known -or ($ir.Value -isnot [int] -and $ir.Value -isnot [long])) { return $unknown }
        $s = [string]$tgt.Value; $i = [int]$ir.Value
        if ($i -lt 0) { $i += $s.Length }
        if ($i -lt 0 -or $i -ge $s.Length) { return $unknown }
        return @{ Known = $true; Value = $s[$i] }
    }

    return $unknown
}

function Test-FalsyConst($exprAst, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    $r = Resolve-Const $exprAst $hasStrictMode $reservedVars $assignedVars
    if (-not $r.Known) { return $false }
    $v = $r.Value
    if ($null -eq $v)                                                              { return $true }
    if ($v -is [bool])                                                             { return -not $v }
    if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [float])   { return $v -eq 0 }
    if ($v -is [string])                                                           { return $v.Length -eq 0 }
    return $false
}

function Test-InRemoved([int]$off, $removed) {
    foreach ($r in $removed) { if ($off -ge $r.Start -and $off -lt $r.End) { return $true } }
    return $false
}

function Test-AlreadyRemoved([int]$s, [int]$e, $removed) {
    foreach ($r in $removed) { if ($r.Start -eq $s -and $r.End -eq $e) { return $true } }
    return $false
}

function Test-ContainedByRemoved([int]$s, [int]$e, $removed) {
    foreach ($r in $removed) { if ($r.Start -le $s -and $e -le $r.End) { return $true } }
    return $false
}

# Fast O(1)/O(log N) variants — rebuild index once per fixpoint iteration.
function Build-RemovedIndex($removed) {
    $exact  = [System.Collections.Generic.HashSet[string]]::new()
    $sorted = @($removed | Sort-Object Start)
    foreach ($r in $sorted) { [void]$exact.Add("$($r.Start):$($r.End)") }
    return [pscustomobject]@{ Exact = $exact; Sorted = $sorted }
}

function Test-AlreadyRemovedFast([int]$s, [int]$e, $idx) {
    return $idx.Exact.Contains("${s}:${e}")
}

function Test-ContainedByRemovedFast([int]$s, [int]$e, $idx) {
    foreach ($r in $idx.Sorted) {
        if ($r.Start -gt $s) { break }
        if ($r.Start -le $s -and $e -le $r.End) { return $true }
    }
    return $false
}

function Test-InRemovedFast([int]$off, $idx) {
    foreach ($r in $idx.Sorted) {
        if ($r.Start -gt $off) { break }
        if ($off -ge $r.Start -and $off -lt $r.End) { return $true }
    }
    return $false
}

function Coalesce-Ranges($rangeList) {
    if ($rangeList.Count -eq 0) { return @() }
    $sorted = @($rangeList | Sort-Object Start)
    $merged = [System.Collections.Generic.List[pscustomobject]]::new()
    $cs = $sorted[0].Start; $ce = $sorted[0].End
    for ($i = 1; $i -lt $sorted.Length; $i++) {
        $r = $sorted[$i]
        if ($r.Start -lt $ce) { if ($r.End -gt $ce) { $ce = $r.End } }
        else { $merged.Add([pscustomobject]@{ Start=$cs; End=$ce }); $cs = $r.Start; $ce = $r.End }
    }
    $merged.Add([pscustomobject]@{ Start=$cs; End=$ce })
    return $merged.ToArray()
}

# ---------------------------------------------------------------------------
# Pass 1: Backtick stripping
# ---------------------------------------------------------------------------

function Invoke-PsStripBackticks([string]$InputPath, [string]$OutputPath) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    # Protected spans: any backtick inside a string literal / here-string / expandable string
    # is either a real escape (`n `t `r `" ...) or literal string data, and a backtick inside a
    # comment is comment text — none of these are identifier-splitting obfuscation, so they must
    # never be removed. (Historically this pass was a context-blind regex that stripped `n / `t
    # escapes out of double-quoted strings, corrupting the script's runtime behavior.)
    #
    # Known residual limitation (intentionally NOT handled here): a backtick used as a genuine
    # escape in a bare argument (e.g. `Write-Host a`nb`, where `n means newline outside quotes)
    # is indistinguishable from identifier-splitting obfuscation by the letter-backtick-letter
    # rule and is still stripped, same as before. Obfuscation inside a $(...) subexpression that
    # is embedded in an expandable string is protected (skipped) rather than stripped — a missed
    # deobfuscation, never a corruption.
    # NB: a bareword command name / unquoted argument (e.g. the obfuscated I`E`X) is *also* a
    # StringConstantExpressionAst — but with StringConstantType 'BareWord'. Those are exactly the
    # tokens where identifier-splitting backticks live and MUST stay strippable, so protect only
    # genuinely quoted strings / here-strings ($_.StringConstantType -ne 'BareWord').
    $protected = @(
        $ast.FindAll({
            param($n)
            ($n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
             $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) -and
            $n.StringConstantType -ne [System.Management.Automation.Language.StringConstantType]::BareWord
        }, $true) | ForEach-Object {
            [pscustomobject]@{ Start = $_.Extent.StartOffset; End = $_.Extent.EndOffset }
        }
    )
    $protected += @(
        $tokens |
            Where-Object { $_.Kind -eq [System.Management.Automation.Language.TokenKind]::Comment } |
            ForEach-Object { [pscustomobject]@{ Start = $_.Extent.StartOffset; End = $_.Extent.EndOffset } }
    )

    # Same obfuscation target as before — a backtick sandwiched between two ASCII letters
    # (e.g. I`E`X) — but only when it lies OUTSIDE every protected span. The match consumes only
    # the single backtick; the surrounding letters are zero-width lookarounds.
    $toRemove = @([regex]::Matches($raw, '(?<=[A-Za-z])`(?=[A-Za-z])') | Where-Object {
        $idx = $_.Index
        $inside = $false
        foreach ($r in $protected) {
            if ($idx -ge $r.Start -and $idx -lt $r.End) { $inside = $true; break }
        }
        -not $inside
    })

    # Second target — SAFE escape-decoding of non-interpolated double-quoted strings.
    # A double-quoted string with no $-interpolation has a constant runtime value, so re-emitting
    # that value as a single-quoted literal is semantics-preserving: `n / `t / `" etc. become their
    # literal characters and the string reads cleanly (e.g. an embedded VBS blob spread over real
    # lines). This is the *safe* form of the historically-corrupting blind strip warned about above —
    # we decode via the parser's already-expanded value instead of deleting backticks in place.
    #
    # Selection: StringConstantExpressionAst with StringConstantType 'DoubleQuoted' whose extent text
    # actually contains a backtick (backtick-free double-quoted strings are left untouched — nothing
    # to decode). Interpolated strings ("...$x...", "...$(...)...") parse as
    # ExpandableStringExpressionAst and are never selected, so interpolation is preserved. Single-
    # quoted strings are 'SingleQuoted' and are likewise skipped (their backticks are literal data).
    # Residual limitation: an ExpandableStringExpressionAst with zero real interpolation is NOT
    # decoded (its .Value is not a reliable decoded form) — a missed deobfuscation, never a corruption.
    $strDecodes = @(
        $ast.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            $n.StringConstantType -eq [System.Management.Automation.Language.StringConstantType]::DoubleQuoted -and
            $n.Extent.Text.Contains('`')
        }, $true) | ForEach-Object {
            $lit = "'" + $_.Value.Replace("'", "''") + "'"
            [pscustomobject]@{ Start = $_.Extent.StartOffset; Length = $_.Extent.EndOffset - $_.Extent.StartOffset; Text = $lit }
        }
    )

    # Unify both edit kinds into one list of {Start; Length; Text}. Identifier backtick removals are
    # single-char deletions OUTSIDE every protected span; string decodes replace whole (protected)
    # string extents — so the two kinds are disjoint by construction and need no overlap handling.
    # Apply descending by Start so earlier edits never shift the offsets of later ones.
    $edits = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($m in $toRemove)  { $edits.Add([pscustomobject]@{ Start = $m.Index; Length = 1; Text = '' }) }
    foreach ($d in $strDecodes){ $edits.Add($d) }

    $out = $raw
    if ($edits.Count -gt 0) {
        $sb = [System.Text.StringBuilder]::new($raw)
        foreach ($e in ($edits | Sort-Object -Property Start -Descending)) {
            [void]$sb.Remove($e.Start, $e.Length)
            if ($e.Text) { [void]$sb.Insert($e.Start, $e.Text) }
        }
        $out = $sb.ToString()
    }

    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        changed         = $toRemove.Count + $strDecodes.Count
        backticks_removed = $toRemove.Count
        strings_decoded = $strDecodes.Count
        input_bytes     = $inputLen
        output_bytes    = $out.Length
        output_path     = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 2: Blank-line collapsing
# ---------------------------------------------------------------------------

function Invoke-PsCollapseBlankLines([string]$InputPath, [string]$OutputPath) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    $stringRanges = @(
        $ast.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
            $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]
        }, $true) | ForEach-Object {
            [pscustomobject]@{ Start = $_.Extent.StartOffset; End = $_.Extent.EndOffset }
        }
    )

    # Stage 1: strip lines that are only whitespace + semicolons.
    # No string-range guard needed: such a line can never be inside a string literal.
    $semiMatches  = [regex]::Matches($raw, '(?m)^[ \t]*;[ \t;]*$')
    $semiStripped = 0
    if ($semiMatches.Count -gt 0) {
        $sb = [System.Text.StringBuilder]::new($raw)
        foreach ($m in ($semiMatches | Sort-Object -Property Index -Descending)) {
            [void]$sb.Remove($m.Index, $m.Length)
            $semiStripped++
        }
        $raw = $sb.ToString()
    }

    # Stage 2: collapse runs of 3+ blank lines.
    $blankMatches = [regex]::Matches($raw, '(\r?\n[ \t]*){3,}')
    $toCollapse = @($blankMatches | Where-Object {
        $mStart = $_.Index; $mEnd = $_.Index + $_.Length
        $inside = $false
        foreach ($r in $stringRanges) {
            if ($mStart -lt $r.End -and $mEnd -gt $r.Start) { $inside = $true; break }
        }
        -not $inside
    })

    if ($semiStripped -eq 0 -and $toCollapse.Count -eq 0) {
        [System.IO.File]::WriteAllText($OutputPath, $raw)
        return @{ changed=0; input_bytes=$inputLen; output_bytes=$raw.Length; output_path=$OutputPath }
    }

    $sb = [System.Text.StringBuilder]::new($raw)
    foreach ($m in ($toCollapse | Sort-Object -Property Index -Descending)) {
        [void]$sb.Remove($m.Index, $m.Length)
        [void]$sb.Insert($m.Index, "`n`n")
    }
    $out = $sb.ToString()
    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        changed      = $semiStripped + $toCollapse.Count
        input_bytes  = $inputLen
        output_bytes = $out.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 3: Dead-code removal (AST-based liveness analysis, fixpoint loop)
# ---------------------------------------------------------------------------

function Invoke-PsRemoveDeadCode([string]$InputPath, [string]$OutputPath, [bool]$PreserveStringLiterals = $true, [bool]$Aggressive = $false) {
    $raw    = [System.IO.File]::ReadAllText($InputPath)
    $tokens = $null; $errors = $null
    $ast    = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)


    # Build per-AST context (never reused across function calls)
    $hasStrictMode = ($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.GetCommandName() -eq 'Set-StrictMode' }, $true)).Count -gt 0

    $reservedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('_','error','?','lastexitcode','matches','args','input','pscmdlet','psitem',
      'true','false','null','pid','pwd','home','host','ofs','psscriptroot',
      'pscommandpath','executioncontext','nestedpromptlevel','shellid') |
      ForEach-Object { [void]$reservedVars.Add($_) }

    $assignNodes  = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)
    $assignedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($a in $assignNodes) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            [void]$assignedVars.Add($a.Left.VariablePath.UserPath.ToLowerInvariant())
        }
    }
    # Function/scriptblock parameters are bound at call time, so they are "assigned" too. Without
    # this, Test-FoldableNull folds an unassigned-looking param (e.g. `$method`) to $null, wrongly
    # making conditions like `$method -eq "decrypt"` statically false and deleting live branches.
    foreach ($p in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true)) {
        if ($p.Name -is [System.Management.Automation.Language.VariableExpressionAst]) {
            [void]$assignedVars.Add($p.Name.VariablePath.UserPath.ToLowerInvariant())
        }
    }
    # Short-form params (`function foo($a){}`) are not always reached as standalone ParameterAst
    # nodes by FindAll — union FunctionDefinitionAst.Parameters explicitly.
    foreach ($fn in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if ($null -ne $fn.Parameters) {
            foreach ($p in $fn.Parameters) {
                if ($p.Name -is [System.Management.Automation.Language.VariableExpressionAst]) {
                    [void]$assignedVars.Add($p.Name.VariablePath.UserPath.ToLowerInvariant())
                }
            }
        }
    }

    $targetOffsets = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($a in $assignNodes) {
        if ($a.Operator -eq 'Equals' -and $a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            [void]$targetOffsets.Add($a.Left.Extent.StartOffset)
        }
    }

    $readsByName = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.List[pscustomobject]]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($v in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        if ($targetOffsets.Contains($v.Extent.StartOffset)) { continue }
        $nm = Get-VarName $v
        if (-not $readsByName.ContainsKey($nm)) { $readsByName[$nm] = [System.Collections.Generic.List[pscustomobject]]::new() }
        $readsByName[$nm].Add([pscustomobject]@{ Start = $v.Extent.StartOffset })
    }
    # A variable reached by *string* name via the *-Variable cmdlet family
    # (`Get-Variable counter`, `Set-Variable -Name counter …`) is invisible to the
    # VariableExpressionAst scan above — the name is a StringConstantExpressionAst, not a `$var` read —
    # so its writers (a store, or a `while(){ $counter++ }`) would look dead and be removed by ANY pass,
    # silently breaking the dynamic access. Register a synthetic always-live read at sentinel offset -1
    # for each such name: -1 is below every real StartOffset and never inside a candidate's [Start,End)
    # range, so it is never Covered and permanently marks the name live everywhere (work-queue liveness
    # and the aggressive cluster's self-containment alike). Conservative and safe (only ever keeps more).
    # Interpolation `"$x"`, splat `@x`, and `[ref]$x` already surface as real reads, so are not needed
    # here. Fully dynamic names (Invoke-Expression / $ExecutionContext.SessionState.PSVariable) remain
    # undecidable — inherent to static analysis, unchanged from the base tool.
    $varRefCmdlets = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('Get-Variable','Set-Variable','Clear-Variable','Remove-Variable','New-Variable',
      'gv','sv','rv','nv','spv') | ForEach-Object { [void]$varRefCmdlets.Add($_) }
    foreach ($cmd in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $cn = $cmd.GetCommandName()
        if ($null -eq $cn -or -not $varRefCmdlets.Contains($cn)) { continue }
        for ($ei = 1; $ei -lt $cmd.CommandElements.Count; $ei++) {   # skip element 0 (the command name)
            $el = $cmd.CommandElements[$ei]
            if ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                $dn = $el.Value.ToLowerInvariant()
                if (-not $readsByName.ContainsKey($dn)) { $readsByName[$dn] = [System.Collections.Generic.List[pscustomobject]]::new() }
                $readsByName[$dn].Add([pscustomobject]@{ Start = -1 })
            }
        }
    }

    $assignments = foreach ($a in $assignNodes) {
        # Equals stores are candidates in every mode. Compound assignments (`$v += …`, `-=`, …) join
        # only under -Aggressive: they REBIND the name to a fresh value (never mutate a shared object),
        # so an accumulate-into-a-dead-variable chain is safely removable, but they are also a read of
        # the target, so the default (conservative) mode leaves them exactly as before.
        $isEquals   = ($a.Operator.ToString() -eq 'Equals')
        $isCompound = ($a.Operator.ToString() -in @('PlusEquals','MinusEquals','MultiplyEquals','DivideEquals','RemainderEquals'))
        if (-not $isEquals -and -not ($Aggressive -and $isCompound)) { continue }
        if (-not ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst])) { continue }
        $name = Get-VarName $a.Left
        if ($name -match ':') { continue }
        # For-loop init/iterator assignments are structurally bound to the loop construct;
        # their reads (condition, iterator, body) fall inside the loop's pure-construct range
        # and get pre-covered, making the init look dead while the loop itself is kept.
        # Skip them here — they're removed only when the entire loop is removed.
        if ($a.Parent -is [System.Management.Automation.Language.ForStatementAst]) { continue }

        # PreserveStringLiterals payload detection is Equals-only (a compound op's value is not a bare
        # literal). AssignmentStatementAst.Right arrives as a PipelineAst (PS7) or directly as a
        # CommandExpressionAst (Windows PowerShell 5.1) — unwrap both (mirrors Invoke-PsInlineConstants).
        $isPayloadString = $false
        if ($isEquals) {
            $rhsPayloadExpr = $null
            if ($a.Right -is [System.Management.Automation.Language.PipelineAst]) {
                if ($a.Right.PipelineElements.Count -eq 1 -and
                    $a.Right.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                    $rhsPayloadExpr = $a.Right.PipelineElements[0].Expression
                }
            } elseif ($a.Right -is [System.Management.Automation.Language.CommandExpressionAst]) {
                $rhsPayloadExpr = $a.Right.Expression
            }
            $isPayloadString = (
                $PreserveStringLiterals -and
                $null -ne $rhsPayloadExpr -and
                $rhsPayloadExpr -is [System.Management.Automation.Language.ConstantExpressionAst] -and
                $rhsPayloadExpr.Value -is [string]
            )
        }

        [pscustomobject]@{
            Start            = $a.Extent.StartOffset
            End              = $a.Extent.EndOffset
            Target           = $name
            HasImpureCommand = (Test-HasImpureCommand $a)
            IsPayloadString  = $isPayloadString
        }
    }

    $loops = foreach ($l in $ast.FindAll({ param($n)
            ($n -is [System.Management.Automation.Language.WhileStatementAst]) -or
            ($n -is [System.Management.Automation.Language.ForStatementAst]) -or
            ($n -is [System.Management.Automation.Language.ForEachStatementAst]) }, $true)) {
        $vars = @()
        foreach ($ia in $l.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
            if ($ia.Left -is [System.Management.Automation.Language.VariableExpressionAst]) { $vars += (Get-VarName $ia.Left) }
        }
        foreach ($u in $l.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
            if ($u.Child -is [System.Management.Automation.Language.VariableExpressionAst]) { $vars += (Get-VarName $u.Child) }
        }
        $isForEach = $l -is [System.Management.Automation.Language.ForEachStatementAst]
        # The per-iteration loop variable is an implicit assignment on every pass. PowerShell doesn't
        # scope it away after the loop, so if it leaks and is read afterward, the loop must count as
        # live rather than dead — include it in AssignedVars for that liveness check.
        if ($isForEach -and $l.Variable -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $vars += (Get-VarName $l.Variable)
        }
        $condExpr  = Get-CondExpr $l.Condition
        $condFalsy = ($null -ne $condExpr -and (Test-FalsyConst $condExpr $hasStrictMode $reservedVars $assignedVars))
        # Owned bookkeeping names: the loop's own local writes (iterator/accumulator). An increment of
        # one of these is not an external side effect — pass them to Test-HasImpureCommand so its own
        # `$i++`/`$counter++` no longer disqualifies the whole loop, while real impure commands and
        # increments of scoped/global names still do. Scoped names (`global:`/`script:`/`env:`/`using:`)
        # carry `:` in the UserPath and are excluded so their increments remain flagged as impure.
        $ownedInc = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($vn in $vars) { if ($vn -ne 'null' -and $vn -notmatch ':') { [void]$ownedInc.Add($vn) } }
        [pscustomobject]@{
            Start            = $l.Extent.StartOffset
            End              = $l.Extent.EndOffset
            # $null is PowerShell's reserved discard variable: assignment to it is documented,
            # always-a-no-op syntax, so a read of $null elsewhere in the script can never reflect one
            # of these internal writes (there is no data flow for this one name). Tracking it as an
            # ordinary AssignedVars entry creates a false liveness dependency on any unrelated $null
            # read anywhere in the file — exclude it so this construct's removability depends only on
            # names that can actually carry data.
            AssignedVars     = @($vars | Where-Object { $_ -ne 'null' } | Select-Object -Unique)
            HasImpureCommand = (Test-HasImpureCommand $l $ownedInc)
            CondIsFalsy      = $condFalsy
            IsValueConsumed  = (Test-ValueConsumed $l)
            IsForEach        = $isForEach
        }
    }

    $tryBlocks = foreach ($t in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true)) {
        $vars = @()
        foreach ($ia in $t.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
            if ($ia.Left -is [System.Management.Automation.Language.VariableExpressionAst]) { $vars += (Get-VarName $ia.Left) }
        }
        [pscustomobject]@{
            Start            = $t.Extent.StartOffset
            End              = $t.Extent.EndOffset
            # $null is PowerShell's reserved discard variable: assignment to it is documented,
            # always-a-no-op syntax, so a read of $null elsewhere in the script can never reflect one
            # of these internal writes (there is no data flow for this one name). Tracking it as an
            # ordinary AssignedVars entry creates a false liveness dependency on any unrelated $null
            # read anywhere in the file — exclude it so this construct's removability depends only on
            # names that can actually carry data.
            AssignedVars     = @($vars | Where-Object { $_ -ne 'null' } | Select-Object -Unique)
            HasImpureCommand = (Test-HasImpureCommand $t.Body) -or
                               ($null -ne $t.Finally -and (Test-HasImpureCommand $t.Finally))
            IsValueConsumed  = (Test-ValueConsumed $t)
        }
    }

    $ifNodes = foreach ($i in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true)) {
        $removable = $true
        # Local (non-scoped) variables assigned in REACHABLE bodies (clauses whose condition is not
        # statically falsy, plus the else). A block deemed "effect-free" by Test-EffectFreeBlock can
        # still contain such stores; if the assigned var is read elsewhere the store is live, so the
        # if must be gated on liveness rather than removed unconditionally. Stores under a falsy
        # condition never execute and are excluded.
        $collectStores = {
            param($blockAst)
            if ($null -eq $blockAst) { return }
            foreach ($ia in $blockAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
                if ($ia.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
                    $vn = Get-VarName $ia.Left
                    # $null is a reserved discard variable — see the AssignedVars comment above;
                    # a store to it can never create a real dependency on an unrelated $null read.
                    if ($vn -notmatch '^(global|script|env|using):' -and $vn -ne 'null') { $vn }
                }
            }
        }
        $storeVars = @()
        foreach ($clause in $i.Clauses) {
            $condExpr  = Get-CondExpr $clause.Item1
            $condFalsy = ($null -ne $condExpr -and (Test-FalsyConst $condExpr $hasStrictMode $reservedVars $assignedVars))
            if (-not $condFalsy -and -not (Test-EffectFreeBlock $clause.Item2)) {
                $removable = $false; break
            }
            if (-not $condFalsy) { $storeVars += & $collectStores $clause.Item2 }
        }
        if ($removable -and -not (Test-EffectFreeBlock $i.ElseClause)) { $removable = $false }
        if ($removable) { $storeVars += & $collectStores $i.ElseClause }
        $storeVars = @($storeVars | Select-Object -Unique)
        [pscustomobject]@{ Start=$i.Extent.StartOffset; End=$i.Extent.EndOffset; Removable=$removable; StoreVars=$storeVars }
    }

    # Dead function definitions: never referenced by any CommandAst
    $invokedNames = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($cmd in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $n2 = $cmd.GetCommandName(); if ($null -ne $n2) { [void]$invokedNames.Add($n2) }
    }
    $deadFnNodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
        Where-Object { -not $invokedNames.Contains($_.Name) } |
        ForEach-Object { [pscustomobject]@{ Start=$_.Extent.StartOffset; End=$_.Extent.EndOffset } })

    # Aggressive-only: functions that ARE invoked but whose body is provably effect-free, where every
    # call site is itself a result-discarded statement (never assigned, piped, redirected, or captured
    # by $(...)/@(...)) — at ANY nesting depth (a try/if/loop/function body), not just the script root;
    # a no-op helper is just as often called from inside a nested block as from the top level. Safe
    # because a value-consumed call would make the "no observable effect" premise false for that call
    # site, so any single value-consuming call disqualifies the function entirely rather than only
    # that call.
    #
    # Keyed by NAME, not by definition: a name may be defined more than once (obfuscators reuse a small
    # pool of names heavily), and removing the call sites of a name is a decision about the name — the
    # call sites cannot be attributed to one particular definition. So every definition sharing the name
    # must qualify before any of them, or any call to it, is removed.
    $noopFnNodes = @()
    if ($Aggressive) {
        # One lowercased blob of every quoted string literal in the script. A function name occurring
        # inside one may be dispatched dynamically (`& "f"`, `iex "f 2"`) — invisible to the CommandAst
        # call-site scan below — so such a name is skipped entirely. BareWord constants are excluded:
        # those ARE the ordinary command-name/argument tokens (the call sites themselves), not data.
        $litBlob = (($ast.FindAll({
                param($n)
                $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
                $n.StringConstantType -ne [System.Management.Automation.Language.StringConstantType]::BareWord
            }, $true) | ForEach-Object { $_.Value }) -join "`n").ToLowerInvariant()

        $allCommandAsts = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))
        $fnDefs = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))

        foreach ($grp in ($fnDefs | Group-Object -Property Name)) {
            $fnName = $grp.Name
            if (-not $invokedNames.Contains($fnName)) { continue }
            if ($litBlob.Contains($fnName.ToLowerInvariant())) { continue }   # possible dynamic dispatch

            $allFree = $true
            foreach ($def in $grp.Group) {
                if (-not (Test-EffectFreeFunctionBody $def)) { $allFree = $false; break }
            }
            if (-not $allFree) { continue }

            $callSites = @($allCommandAsts | Where-Object { $_.GetCommandName() -eq $fnName })
            if ($callSites.Count -eq 0) { continue }

            $allDiscarded = $true
            foreach ($call in $callSites) {
                $pipe = $call.Parent
                if (-not ($pipe -is [System.Management.Automation.Language.PipelineAst] -and
                          $pipe.PipelineElements.Count -eq 1)) { $allDiscarded = $false; break }
                # `f > out.txt` writes a file — the discarded value is not actually discarded.
                if ($call.Redirections.Count -gt 0) { $allDiscarded = $false; break }
                # Real sequential statement position only — never a scriptblock literal's body
                # (a deferred delegate) nor an @(...) element slot.
                if (-not (Test-RealStatementPosition $pipe.Parent)) { $allDiscarded = $false; break }
                # A $(...)/@(...)/assignment ancestor means the value IS consumed. The statement-position
                # test alone does not catch this: a SubExpressionAst's statements also live in a
                # StatementBlockAst, so `$a = $(f)` looks like a bare statement without this check.
                if (Test-ValueConsumed $call) { $allDiscarded = $false; break }
                # The ARGUMENTS still execute — deleting the call deletes them too, so a side effect
                # nested in one (e.g. `f (Get-Content x)`) must keep the whole call. Element 0 is the
                # command name itself.
                for ($ei = 1; $ei -lt $call.CommandElements.Count; $ei++) {
                    if (Test-HasImpureCommand $call.CommandElements[$ei]) { $allDiscarded = $false; break }
                }
                if (-not $allDiscarded) { break }
            }
            if (-not $allDiscarded) { continue }

            foreach ($def in $grp.Group) {
                $noopFnNodes += [pscustomobject]@{ Start=$def.Extent.StartOffset; End=$def.Extent.EndOffset }
            }
            foreach ($call in $callSites) {
                $noopFnNodes += [pscustomobject]@{ Start=$call.Parent.Extent.StartOffset; End=$call.Parent.Extent.EndOffset }
            }
        }
    }

    # Standalone pure pipeline statements (e.g. Get-Random | Out-Null) — result discarded, no side
    # effects. Accepted at any real statement position (root script or any function body, any nesting
    # depth) via Test-RealStatementPosition — but not inside a scriptblock *literal* passed as a value
    # (e.g. ForEach-Object { [char]$_ }), which stays excluded exactly as before. Also excluded: a
    # statement whose value is actually consumed by an enclosing value-context (e.g. the last
    # expression inside a captured `$x = foreach(...){ $i * 2 }` loop) — broadening past root-level
    # made this reachable, since such a loop body's statement now structurally qualifies as a "real
    # statement position" too, but it is not discarded at all; removing it would silently change $x.
    $pureStmtNodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.PipelineAst] }, $true) |
        Where-Object {
            (Test-RealStatementPosition $_.Parent) -and
            -not (Test-ValueConsumed $_) -and
            -not (Test-HasImpureCommand $_)
        } |
        ForEach-Object { [pscustomobject]@{ Start=$_.Extent.StartOffset; End=$_.Extent.EndOffset } })

    # Aggressive-only: bare receiver-only mutator statements (`$v.Clear()`, `$v.Add(...)`) treated as
    # removable "writers" of $v, so an accumulate-then-discard junk trio
    # (`$v = @(); $v += "x"; $v.Clear()`) clusters and is deleted whole. Each is admitted only when it
    # cannot possibly be observed elsewhere — the guards below are all necessary for soundness:
    #   * receiver is a plain local variable (no chain/static/scoped/reserved name),
    #   * method is on the receiver-ONLY mutator allowlist (args are never written back through),
    #   * arguments are side-effect-free (no command, no non-pure .NET method, no ++/--, no scriptblock
    #     that a mutator like .Sort could invoke),
    #   * the statement is a bare, result-discarded statement in real script flow (not value-consumed,
    #     not a scriptblock-literal body),
    #   * the receiver is fresh-confined (every `=` to it binds a fresh object — closes aliasing-IN;
    #     the cluster/work-queue escape check closes aliasing-OUT).
    # A method call is otherwise NEVER assumed pure (Test-HasImpureCommand still flags all of them),
    # so this is strictly additive and gated behind -Aggressive.
    $mutatorNodes = @()
    if ($Aggressive) {
        foreach ($inv in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
            if ($inv.Static) { continue }
            $mnode = $inv.Member
            if ($mnode -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
            if (-not $script:PsReceiverOnlyMutators.Contains($mnode.Value)) { continue }
            $recv = $inv.Expression
            if ($recv -isnot [System.Management.Automation.Language.VariableExpressionAst]) { continue }
            $rname = Get-VarName $recv
            if ($rname -match ':' -or $reservedVars.Contains($rname)) { continue }
            # Bare, result-discarded statement in real script flow.
            $ce = $inv.Parent
            if ($ce -isnot [System.Management.Automation.Language.CommandExpressionAst]) { continue }
            $pipe = $ce.Parent
            if ($pipe -isnot [System.Management.Automation.Language.PipelineAst]) { continue }
            if (-not (Test-RealStatementPosition $pipe.Parent)) { continue }
            if (Test-ValueConsumed $inv) { continue }
            # Side-effect-free arguments.
            $argsOk = $true
            foreach ($arg in $inv.Arguments) {
                if (($arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)).Count -gt 0) { $argsOk=$false; break }
                if (($arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.ScriptBlockExpressionAst] }, $true)).Count -gt 0) { $argsOk=$false; break }
                foreach ($m in $arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
                    if (-not (Test-PureNetMethodInvoke $m)) { $argsOk=$false; break }
                }
                if (-not $argsOk) { break }
                foreach ($u in $arg.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
                    if ($u.TokenKind.ToString() -in @('PlusPlus','MinusMinus','PostfixPlusPlus','PostfixMinusMinus')) { $argsOk=$false; break }
                }
                if (-not $argsOk) { break }
            }
            if (-not $argsOk) { continue }
            # Fresh-confined receiver — the guard that makes the in-place mutation unobservable.
            if (-not (Test-FreshConfinedVar $rname $assignNodes)) { continue }
            $mutatorNodes += [pscustomobject]@{ Start=$pipe.Extent.StartOffset; End=$pipe.Extent.EndOffset; Target=$rname }
        }
    }

    # -----------------------------------------------------------------------
    # Unified work-queue: O(N + E) replacement for the O(K*N) fixpoint loop
    # -----------------------------------------------------------------------

    $removed    = [System.Collections.Generic.List[pscustomobject]]::new()
    $removedSet = [System.Collections.Generic.HashSet[string]]::new()

    # Annotate each read object with Name and Covered; build sorted allReads array
    foreach ($nm in $readsByName.Keys) {
        foreach ($rd in $readsByName[$nm]) {
            Add-Member -InputObject $rd -NotePropertyName 'Name'    -NotePropertyValue $nm    -Force
            Add-Member -InputObject $rd -NotePropertyName 'Covered' -NotePropertyValue $false -Force
        }
    }
    $allReadsL = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($nm in $readsByName.Keys) { foreach ($rd in $readsByName[$nm]) { $allReadsL.Add($rd) } }
    $allReads = @($allReadsL | Sort-Object Start)

    # NOTE: reads are only ever marked Covered once a construct is confirmed removed (see the
    # static-seeding blocks below and the work-queue's dequeue-and-remove step) — never pre-emptively.
    # An earlier version blanket-covered every read inside any "pure, not-value-consumed" loop/try
    # before liveness was decided; if the loop survived (its own assigned vars were read elsewhere),
    # reads of *other* variables inside it were still wrongly hidden, causing live stores the surviving
    # loop still needed (e.g. an accumulator's `$data`/`$i`) to be deleted as "dead". Self-referential
    # liveness (a loop reading its own assigned var inside itself) is independently handled by the
    # self-range exclusion in the liveness check below, so nothing relied on the removed pre-cover.

    # Build liveness-dependent queue candidates with stable CID = index
    $qList = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($l in $loops) {
        if ($l.CondIsFalsy -or $l.HasImpureCommand -or $l.IsValueConsumed) { continue }
        # Foreach loops only join the purity-based "non-functional loop" removal under -Aggressive;
        # by default a foreach is only ever removed via the CondIsFalsy (unreachable/never-assigned
        # collection) path above, mirroring while/for's existing conservative default.
        if ($l.IsForEach -and -not $Aggressive) { continue }
        $qList.Add([pscustomobject]@{ CID=$qList.Count; Start=$l.Start; End=$l.End; Reason='non-functional loop'; Vars=$l.AssignedVars })
    }
    foreach ($t in $tryBlocks) {
        if ($t.HasImpureCommand -or $t.IsValueConsumed) { continue }
        $qList.Add([pscustomobject]@{ CID=$qList.Count; Start=$t.Start; End=$t.End; Reason='dead try block'; Vars=$t.AssignedVars })
    }
    foreach ($a in $assignments) {
        if ($a.HasImpureCommand) { continue }
        # $null is a reserved discard variable — see the AssignedVars comment above; a $null = <pure
        # expr> store can never be "read back" by design, so it's tracked with no dependency (always
        # dead), rather than colliding with unrelated $null reads elsewhere in the script.
        $depVars = if ($a.Target -eq 'null') { @() } else { @($a.Target) }
        $qList.Add([pscustomobject]@{ CID=$qList.Count; Start=$a.Start; End=$a.End; Reason='dead store'; Vars=$depVars })
    }
    # Removable ifs whose only effect is assigning locals: gate on liveness of those locals rather
    # than removing unconditionally (an unconditionally-seeded if with a live store — e.g. a loop
    # bound `{ $j = $len }` — would corrupt the reader). Empty-StoreVars ifs stay in the static path.
    foreach ($i in $ifNodes) {
        if (-not $i.Removable -or $i.StoreVars.Count -eq 0) { continue }
        $qList.Add([pscustomobject]@{ CID=$qList.Count; Start=$i.Start; End=$i.End; Reason='dead if (stores unused)'; Vars=$i.StoreVars })
    }
    # Bare receiver-only mutator calls (aggressive) depend on liveness of the receiver, exactly like a
    # dead store — they cluster with that variable's `=`/`+=` writers and are removed together.
    foreach ($m in $mutatorNodes) {
        $qList.Add([pscustomobject]@{ CID=$qList.Count; Start=$m.Start; End=$m.End; Reason='dead mutator call'; Vars=@($m.Target) })
    }
    $qArr = $qList.ToArray()

    # Build reverse index: read offset -> list of candidate CIDs whose liveness depends on it
    $depR2C = [System.Collections.Generic.Dictionary[int,System.Collections.Generic.List[int]]]::new()
    foreach ($c in $qArr) {
        foreach ($v in $c.Vars) {
            $vReads = $null; if (-not $readsByName.TryGetValue($v, [ref]$vReads)) { continue }
            foreach ($rd in $vReads) {
                if ($rd.Start -ge $c.Start -and $rd.Start -lt $c.End) { continue }  # self-range
                if ($rd.Covered) { continue }  # pure-construct reads never trigger propagation
                $deps = $null
                if (-not $depR2C.TryGetValue($rd.Start, [ref]$deps)) {
                    $deps = [System.Collections.Generic.List[int]]::new(); $depR2C[$rd.Start] = $deps
                }
                [void]$deps.Add($c.CID)
            }
        }
    }

    $removedCIDs = [System.Collections.Generic.HashSet[int]]::new()
    $inQueue     = [System.Collections.Generic.HashSet[int]]::new()
    $workQueue   = [System.Collections.Generic.Queue[int]]::new()

    # Seed static removals (unreachable loops + dead-ifs); propagate covered reads
    foreach ($l in $loops) {
        if (-not $l.CondIsFalsy) { continue }
        if ($removedSet.Add("$($l.Start):$($l.End)")) {
            $removed.Add([pscustomobject]@{ Start=$l.Start; End=$l.End; Reason='unreachable loop' })
            $rS=$l.Start; $rE=$l.End
            $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
            while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
            for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                    foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
                }
            }
        }
    }
    foreach ($i in $ifNodes) {
        if (-not $i.Removable -or $i.StoreVars.Count -gt 0) { continue }  # store-bearing ifs go through the work-queue
        if ($removedSet.Add("$($i.Start):$($i.End)")) {
            $removed.Add([pscustomobject]@{ Start=$i.Start; End=$i.End; Reason='dead if (unreachable/empty)' })
            $rS=$i.Start; $rE=$i.End
            $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
            while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
            for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                    foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
                }
            }
        }
    }

    foreach ($fn in $deadFnNodes) {
        if ($removedSet.Add("$($fn.Start):$($fn.End)")) {
            $removed.Add([pscustomobject]@{ Start=$fn.Start; End=$fn.End; Reason='dead function' })
            $rS=$fn.Start; $rE=$fn.End
            $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
            while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
            for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                    foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
                }
            }
        }
    }
    foreach ($nf in $noopFnNodes) {
        if ($removedSet.Add("$($nf.Start):$($nf.End)")) {
            $removed.Add([pscustomobject]@{ Start=$nf.Start; End=$nf.End; Reason='no-op function' })
            $rS=$nf.Start; $rE=$nf.End
            $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
            while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
            for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                    foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
                }
            }
        }
    }
    foreach ($stmt in $pureStmtNodes) {
        if ($removedSet.Add("$($stmt.Start):$($stmt.End)")) {
            $removed.Add([pscustomobject]@{ Start=$stmt.Start; End=$stmt.End; Reason='pure statement' })
            $rS=$stmt.Start; $rE=$stmt.End
            $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
            while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
            for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                    foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
                }
            }
        }
    }

    # -----------------------------------------------------------------------
    # Aggressive-only: whole-variable ("faint variable") cluster removal.
    #
    # The incremental work-queue removes a candidate only once ALL of its external reads are already
    # Covered, and a read is Covered only when the construct enclosing it is removed. A group of pure
    # constructs that only ever read/write each other's shared variables (e.g. six
    # `while ($counter -lt N) { $counter++ }` loops, or ten `for ($j…) { $final += $j }` loops) forms a
    # reference cycle that never bootstraps — nothing can be the first removal — so the whole dead
    # cluster survives. This pass breaks that deadlock soundly by reasoning per variable instead of per
    # construct: it removes an entire connected component of candidates at once, but ONLY when every
    # variable the component touches is provably confined to the component.
    #
    # Runs AFTER the unconditional static seeds above so their coverage is already applied: a read that
    # sits inside an already-removed construct (e.g. `$counter` inside an unreachable
    # `while ($counter -lt 0){…}` loop) is skipped below rather than counted as an escape. Only
    # unconditional removals have happened at this point (the liveness work-queue runs afterward), so no
    # read is skipped speculatively.
    #
    # Soundness: a component is removed only if (a) every candidate in it is local-eligible (no scoped/
    # reserved/dynamically-named var) and (b) every variable written anywhere in the component is
    # "self-contained" — every not-yet-Covered read of it lies inside one of that variable's own writer
    # candidates (all of which are, by construction, in the same component). Deleting the whole
    # component then erases every surviving read AND write of those variables, so no remaining code can
    # observe the change. Candidates are already pure (they passed the removal gates, and
    # Test-HasImpureCommand now treats index/member assignment as impure, so no candidate hides an
    # untracked aliased write). The one candidate type that DOES mutate an object in place — a bare
    # receiver-only mutator call (`$v.Clear()`/`$v.Add(...)`, admitted above) — is safe for the same
    # reason: it is only ever emitted for a fresh-confined receiver (Test-FreshConfinedVar rules out
    # aliasing-in) whose every surviving read is inside the component (this pass rules out aliasing-out),
    # so its object is reachable through that one name only and the in-place mutation is unobservable.
    # A component pinned alive by one escaping variable (e.g. a lone `Write-Output $final`) keeps ALL
    # its members.
    if ($Aggressive -and $qArr.Length -gt 0) {
        # NB: dynamically-named variables (`Get-Variable x`, `Set-Variable -Name x`) need no dedicated
        # guard here — each already carries a synthetic always-live read (sentinel offset -1, injected
        # where readsByName is built), so its self-containment check below finds an uncovered read
        # outside every writer and keeps the component. That one mechanism protects the default work-
        # queue and this cluster pass identically.
        $nCand = $qArr.Length
        # writersOf[var] = CIDs writing it; candOk[ci] = all of candidate ci's vars are local-eligible
        # (non-scoped, non-reserved). Scoped/global writes escape name-based liveness, so never cluster them.
        $writersOf = [System.Collections.Generic.Dictionary[string,System.Collections.Generic.List[int]]]::new([System.StringComparer]::OrdinalIgnoreCase)
        $candOk    = New-Object bool[] $nCand
        for ($ci = 0; $ci -lt $nCand; $ci++) {
            $ok = $true
            foreach ($v in $qArr[$ci].Vars) {
                if ($v -match ':' -or $reservedVars.Contains($v)) { $ok = $false }
                $lst = $null
                if (-not $writersOf.TryGetValue($v, [ref]$lst)) { $lst = [System.Collections.Generic.List[int]]::new(); $writersOf[$v] = $lst }
                $lst.Add($ci)
            }
            $candOk[$ci] = $ok
        }

        # self-contained: every not-yet-Covered read of the var falls inside one of its writer
        # candidates' extents. Covered reads belong to already-removed static constructs — they will not
        # exist in the output, so they can never observe the variable and must not count as escapes.
        $selfC = [System.Collections.Generic.Dictionary[string,bool]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($v in $writersOf.Keys) {
            $contained = $true
            $vReads = $null
            if ($readsByName.TryGetValue($v, [ref]$vReads)) {
                foreach ($rd in $vReads) {
                    if ($rd.Covered) { continue }
                    $inside = $false
                    foreach ($ci in $writersOf[$v]) {
                        if ($rd.Start -ge $qArr[$ci].Start -and $rd.Start -lt $qArr[$ci].End) { $inside = $true; break }
                    }
                    if (-not $inside) { $contained = $false; break }
                }
            }
            $selfC[$v] = $contained
        }

        # Union-Find: connect candidates that share a written variable (path-halving finds inline).
        $parent = New-Object int[] $nCand
        for ($ci = 0; $ci -lt $nCand; $ci++) { $parent[$ci] = $ci }
        foreach ($v in $writersOf.Keys) {
            $lst = $writersOf[$v]
            for ($k = 1; $k -lt $lst.Count; $k++) {
                $a = $lst[0]; $b = $lst[$k]
                while ($parent[$a] -ne $a) { $parent[$a] = $parent[$parent[$a]]; $a = $parent[$a] }
                while ($parent[$b] -ne $b) { $parent[$b] = $parent[$parent[$b]]; $b = $parent[$b] }
                if ($a -ne $b) { $parent[$a] = $b }
            }
        }
        # A component is removable unless any member is not local-eligible…
        $compRemovable = [System.Collections.Generic.Dictionary[int,bool]]::new()
        for ($ci = 0; $ci -lt $nCand; $ci++) {
            $r = $ci; while ($parent[$r] -ne $r) { $parent[$r] = $parent[$parent[$r]]; $r = $parent[$r] }
            if (-not $compRemovable.ContainsKey($r)) { $compRemovable[$r] = $true }
            if (-not $candOk[$ci]) { $compRemovable[$r] = $false }
        }
        # …or any variable it touches escapes (has a surviving read outside all its writers).
        foreach ($v in $writersOf.Keys) {
            if ($selfC[$v]) { continue }
            foreach ($ci in $writersOf[$v]) {
                $r = $ci; while ($parent[$r] -ne $r) { $parent[$r] = $parent[$parent[$r]]; $r = $parent[$r] }
                $compRemovable[$r] = $false
            }
        }
        # Seed removals for members of removable components; cover their reads (no propagation needed —
        # every candidate is enqueued for a final liveness check below, now seeing these reads Covered).
        for ($ci = 0; $ci -lt $nCand; $ci++) {
            $r = $ci; while ($parent[$r] -ne $r) { $parent[$r] = $parent[$parent[$r]]; $r = $parent[$r] }
            if (-not $compRemovable[$r]) { continue }
            $c = $qArr[$ci]
            [void]$removedCIDs.Add($ci)
            if ($removedSet.Add("$($c.Start):$($c.End)")) {
                $removed.Add([pscustomobject]@{ Start=$c.Start; End=$c.End; Reason='dead variable cluster' })
                $rS=$c.Start; $rE=$c.End
                $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
                while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
                for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
                    $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
                }
            }
        }
    }

    # Seed all queue candidates for initial liveness check
    foreach ($c in $qArr) { if ($inQueue.Add($c.CID)) { [void]$workQueue.Enqueue($c.CID) } }

    # Work-queue: dequeue, liveness-check, remove if dead, propagate coverage
    while ($workQueue.Count -gt 0) {
        $cid = $workQueue.Dequeue(); [void]$inQueue.Remove($cid)
        $c = $qArr[$cid]
        if ($removedCIDs.Contains($cid)) { continue }

        $live = $false
        foreach ($v in $c.Vars) {
            $vReads=$null; if (-not $readsByName.TryGetValue($v,[ref]$vReads)) { continue }
            foreach ($rd in $vReads) {
                if ($rd.Start -ge $c.Start -and $rd.Start -lt $c.End) { continue }
                if ($rd.Covered) { continue }
                $live=$true; break
            }
            if ($live) { break }
        }
        if ($live) { continue }

        [void]$removedCIDs.Add($cid)
        if ($removedSet.Add("$($c.Start):$($c.End)")) {
            $removed.Add([pscustomobject]@{ Start=$c.Start; End=$c.End; Reason=$c.Reason })
        }
        $rS=$c.Start; $rE=$c.End
        $bsLo=0; $bsHi=$allReads.Length-1; $bsF=$allReads.Length
        while ($bsLo -le $bsHi) { $bsM=[int](($bsLo+$bsHi)/2); if ($allReads[$bsM].Start -ge $rS) { $bsF=$bsM; $bsHi=$bsM-1 } else { $bsLo=$bsM+1 } }
        for ($ri=$bsF; $ri -lt $allReads.Length -and $allReads[$ri].Start -lt $rE; $ri++) {
            $rd=$allReads[$ri]; if ($rd.Covered) { continue }; $rd.Covered=$true
            $deps2=$null; if ($depR2C.TryGetValue($rd.Start,[ref]$deps2)) {
                foreach ($di in $deps2) { if (-not $removedCIDs.Contains($di) -and $inQueue.Add($di)) { [void]$workQueue.Enqueue($di) } }
            }
        }
    }

    # Coalesce overlapping/contained ranges before deletion (fixes String.Remove crash)
    $coalesced = Coalesce-Ranges $removed

    $out = $raw
    foreach ($r in ($coalesced | Sort-Object Start -Descending)) {
        $exp = Expand-SemiDeleteRange $out $r.Start $r.End
        $out = $out.Remove($exp.Start, $exp.End - $exp.Start)
    }
    [System.IO.File]::WriteAllText($OutputPath, $out)

    $byReason = ($removed | Group-Object Reason | ForEach-Object { "$($_.Count)x $($_.Name)" }) -join ', '
    return @{
        changed      = $removed.Count
        by_reason    = $byReason
        aggressive   = $Aggressive
        input_bytes  = $raw.Length
        output_bytes = $out.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 3: Constant string folding (+, -f format, [char[]] -join '')
# ---------------------------------------------------------------------------

function Invoke-PsFoldStrings([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $folded   = 0
    $changed  = $true
    # Strict-mode=true prevents Resolve-Const from folding unknown variables to $null
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    while ($changed) {
        $changed = $false
        $tok2 = $null; $err2 = $null
        $sAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok2, [ref]$err2)
        $reps = [System.Collections.Generic.List[pscustomobject]]::new()

        foreach ($bin in $sAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] }, $true)) {
            $op = $bin.Operator.ToString()

            if ($op -eq 'Plus') {
                $lv = Resolve-Const $bin.Left  $true $emptySet $emptySet
                $rv = Resolve-Const $bin.Right $true $emptySet $emptySet
                if (-not ($lv.Known -and $rv.Known)) { continue }
                if ($lv.Value -isnot [string] -or $rv.Value -isnot [string]) { continue }
                $escaped = ($lv.Value + $rv.Value) -replace "'", "''"
                $reps.Add([pscustomobject]@{ S=$bin.Extent.StartOffset; E=$bin.Extent.EndOffset; T="'$escaped'" })
            }
            elseif ($op -eq 'Format') {
                $lv = Resolve-Const $bin.Left $true $emptySet $emptySet
                if (-not $lv.Known -or $lv.Value -isnot [string]) { continue }
                $fmtArgs  = [System.Collections.Generic.List[object]]::new()
                $allKnown = $true
                $rhsNode  = $bin.Right
                if ($rhsNode -is [System.Management.Automation.Language.ArrayLiteralAst]) {
                    foreach ($el in $rhsNode.Elements) {
                        $ev = Resolve-Const $el $true $emptySet $emptySet
                        if (-not $ev.Known) { $allKnown = $false; break }
                        $fmtArgs.Add($ev.Value)
                    }
                } else {
                    $ev = Resolve-Const $rhsNode $true $emptySet $emptySet
                    if ($ev.Known) { $fmtArgs.Add($ev.Value) } else { $allKnown = $false }
                }
                if (-not $allKnown) { continue }
                try {
                    $result  = $lv.Value -f $fmtArgs.ToArray()
                    $escaped = $result -replace "'", "''"
                    $reps.Add([pscustomobject]@{ S=$bin.Extent.StartOffset; E=$bin.Extent.EndOffset; T="'$escaped'" })
                } catch { }
            }
            elseif ($op -eq 'Join') {
                $rv = Resolve-Const $bin.Right $true $emptySet $emptySet
                if (-not $rv.Known -or $rv.Value -isnot [string] -or $rv.Value.Length -gt 0) { continue }
                $lhs = $bin.Left
                if ($lhs -isnot [System.Management.Automation.Language.ConvertExpressionAst]) { continue }
                if ($lhs.Type.TypeName.FullName -notmatch '(?i)^char\[\]$') { continue }
                $arr = $lhs.Child
                if ($arr -isnot [System.Management.Automation.Language.ArrayLiteralAst]) { continue }
                $sb = [System.Text.StringBuilder]::new()
                $allKnown = $true
                foreach ($el in $arr.Elements) {
                    $ev = Resolve-Const $el $true $emptySet $emptySet
                    if (-not $ev.Known -or $null -eq $ev.Value) { $allKnown = $false; break }
                    try { [void]$sb.Append([char][int]$ev.Value) } catch { $allKnown = $false; break }
                }
                if (-not $allKnown) { continue }
                $escaped = $sb.ToString() -replace "'", "''"
                $reps.Add([pscustomobject]@{ S=$bin.Extent.StartOffset; E=$bin.Extent.EndOffset; T="'$escaped'" })
            }
        }

        if ($reps.Count -gt 0) {
            # Nested same-operator folds (e.g. 'a'+'b' inside 'a'+'b'+'c') yield overlapping
            # replacement ranges; applying both corrupts offsets (String.Remove throws). Keep only
            # non-overlapping reps, preferring the widest span — the outer fold already contains the
            # inner result — and let the fixpoint reparse pick up anything deferred next iteration.
            $selected = [System.Collections.Generic.List[pscustomobject]]::new()
            foreach ($r in ($reps | Sort-Object { $_.E - $_.S } -Descending)) {
                $overlap = $false
                foreach ($s in $selected) {
                    if ($r.S -lt $s.E -and $r.E -gt $s.S) { $overlap = $true; break }
                }
                if (-not $overlap) { $selected.Add($r) }
            }
            foreach ($r in ($selected | Sort-Object { $_.S } -Descending)) {
                $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
            }
            $folded  += $selected.Count
            $changed  = $true
        }
    }

    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $folded
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 3b: Constant string-method-chain folding
# ('seed'.Remove(a,b).Insert(c,'d').Replace($(...),$(...)) -- generic depth/nesting, see
# $script:CffAllowedStringMethods above for the exact allowlist and its safety boundary)
# ---------------------------------------------------------------------------

# Diagnostic-only classifier: given a top-level InvokeMemberExpressionAst that Resolve-Const could
# NOT fold, walk the same checks Resolve-ConstImpl performs to report *why*, without duplicating
# its evaluation logic (folding correctness always comes from Resolve-Const/Resolve-ConstImpl --
# this only explains a bail after the fact).
function Get-MethodChainSkipReason($node, [bool]$hasStrictMode, $reservedVars, $assignedVars, $constVars) {
    if ($node.Static) { return 'static call (out of scope)' }
    $memberNameNode = $node.Member
    $methodName = $null
    if ($memberNameNode -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $methodName = $memberNameNode.Value
    } else {
        $mr = Resolve-Const $memberNameNode $hasStrictMode $reservedVars $assignedVars $constVars
        if ($mr.Known -and $mr.Value -is [string]) { $methodName = $mr.Value } else { return 'method name not resolvable' }
    }
    if (-not $script:CffAllowedStringMethods.ContainsKey($methodName)) { return "method not allowlisted: $methodName" }
    $recv = Resolve-Const $node.Expression $hasStrictMode $reservedVars $assignedVars $constVars
    if (-not $recv.Known -or $recv.Value -isnot [string]) { return 'receiver not constant' }
    foreach ($argAst in $node.Arguments) {
        $ar = Resolve-Const $argAst $hasStrictMode $reservedVars $assignedVars $constVars
        if (-not $ar.Known) { return 'argument not constant' }
    }
    return 'arity/bounds error during invocation'
}

function Invoke-PsFoldMethodChains([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $folded   = 0
    $changed  = $true
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    while ($changed) {
        $changed = $false
        $tokM = $null; $errM = $null
        $mAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokM, [ref]$errM)
        $reps = [System.Collections.Generic.List[pscustomobject]]::new()

        foreach ($node in $mAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
            $r = Resolve-Const $node $true $emptySet $emptySet
            if (-not $r.Known -or $r.Value -isnot [string]) { continue }
            $escaped = $r.Value -replace "'", "''"
            $reps.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; T="'$escaped'" })
        }

        if ($reps.Count -gt 0) {
            # A resolvable outer chain's receiver/arguments are themselves resolvable inner chains,
            # so nested reps overlap by containment (never partial overlap -- AST-guaranteed). Keep
            # only the widest span per overlap cluster, same technique as Invoke-PsFoldStrings.
            $selected = [System.Collections.Generic.List[pscustomobject]]::new()
            foreach ($r in ($reps | Sort-Object { $_.E - $_.S } -Descending)) {
                $overlap = $false
                foreach ($s in $selected) {
                    if ($r.S -lt $s.E -and $r.E -gt $s.S) { $overlap = $true; break }
                }
                if (-not $overlap) { $selected.Add($r) }
            }
            foreach ($r in ($selected | Sort-Object { $_.S } -Descending)) {
                $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
            }
            $folded += $selected.Count
            $changed = $true
        }
    }

    # Diagnostics: classify any remaining (unfoldable) top-level method-call chains.
    $tokF = $null; $errF = $null
    $finalAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokF, [ref]$errF)
    $unresolved = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($node in $finalAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        $r = Resolve-Const $node $true $emptySet $emptySet
        if (-not $r.Known) {
            $unresolved.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; Node=$node })
        }
    }
    $maximalUnresolved = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($u in ($unresolved | Sort-Object { $_.E - $_.S } -Descending)) {
        $overlap = $false
        foreach ($s in $maximalUnresolved) {
            if ($u.S -lt $s.E -and $u.E -gt $s.S) { $overlap = $true; break }
        }
        if (-not $overlap) { $maximalUnresolved.Add($u) }
    }
    $skipped = @($maximalUnresolved | ForEach-Object {
        [pscustomobject]@{ Reason = (Get-MethodChainSkipReason $_.Node $true $emptySet $emptySet $null) }
    })
    $byReason = ($skipped | Group-Object Reason | ForEach-Object { "$($_.Count)x $($_.Name)" }) -join ', '

    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        resolved     = $folded
        skipped      = $maximalUnresolved.Count
        by_reason    = $byReason
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 3c: Constant static string-builder-call folding ([string]::Concat / [string]::Join)
#
# Deliberately kept separate from Invoke-PsFoldMethodChains (instance-method chain folding)
# rather than sharing its allowlist/branch: static dispatch of a pure string-building operation
# is treated as its own distinct technique under this toolkit's one-strategy-per-pass convention.
# Resolve-ConstImpl always bails on any static InvokeMemberExpressionAst (by design -- it stays a
# strategy-agnostic primitive), so this pass owns its own small recursive resolver below instead
# of extending the shared one.
# ---------------------------------------------------------------------------

# Deliberately tiny: only [string]::Concat / [string]::Join, both pure and deterministic.
$script:CffAllowedStaticStringMethods = @{
    'Concat' = { param($a) -join $a }
    'Join'   = { param($a)
                 if ($a.Count -lt 1) { throw 'arity' }
                 $rest = if ($a.Count -eq 1) { @() } else { $a[1..($a.Count-1)] }
                 [string]::Join([string]$a[0], [object[]]$rest) }
}

# Resolves an InvokeMemberExpressionAst as a static [string]::Concat/::Join call, recursing into
# itself for nested static-call arguments (Resolve-Const can't do this -- it always bails on
# static calls) and falling back to Resolve-Const for any argument that isn't itself a static call
# (literals, $(...)-wrapped operands, already-foldable instance-method chains, etc.).
function Resolve-StaticStringCall($node, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    $unknown = @{ Known = $false; Value = $null }
    if ($node -isnot [System.Management.Automation.Language.InvokeMemberExpressionAst]) { return $unknown }
    if (-not $node.Static) { return $unknown }
    if ($node.Expression -isnot [System.Management.Automation.Language.TypeExpressionAst]) { return $unknown }
    $tn = $node.Expression.TypeName.FullName.ToLowerInvariant()
    if ($tn -ne 'string' -and $tn -ne 'system.string') { return $unknown }

    $memberNameNode = $node.Member
    $methodName = $null
    if ($memberNameNode -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $methodName = $memberNameNode.Value
    } else {
        $mr = Resolve-Const $memberNameNode $hasStrictMode $reservedVars $assignedVars
        if ($mr.Known -and $mr.Value -is [string]) { $methodName = $mr.Value } else { return $unknown }
    }
    if (-not $script:CffAllowedStaticStringMethods.ContainsKey($methodName)) { return $unknown }

    $argVals = [System.Collections.Generic.List[object]]::new()
    foreach ($argAst in $node.Arguments) {
        $nested = Resolve-StaticStringCall $argAst $hasStrictMode $reservedVars $assignedVars
        $ar = if ($nested.Known) { $nested } else { Resolve-Const $argAst $hasStrictMode $reservedVars $assignedVars }
        if (-not $ar.Known) { return $unknown }
        if ($ar.Value -isnot [string] -and $ar.Value -isnot [int] -and $ar.Value -isnot [long] -and $ar.Value -isnot [char]) {
            return $unknown
        }
        $argVals.Add($ar.Value)
    }
    try {
        $result = & $script:CffAllowedStaticStringMethods[$methodName] $argVals
    } catch { return $unknown }   # arity/bounds error -- bail, never guess
    return @{ Known = $true; Value = $result }
}

# Diagnostic-only classifier mirroring Resolve-StaticStringCall's checks, for reporting why a
# top-level static call didn't fold.
function Get-StaticStringCallSkipReason($node, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    if ($node.Expression -isnot [System.Management.Automation.Language.TypeExpressionAst]) { return 'target type not resolvable' }
    $tn = $node.Expression.TypeName.FullName.ToLowerInvariant()
    if ($tn -ne 'string' -and $tn -ne 'system.string') { return "target type not allowlisted: $tn" }
    $memberNameNode = $node.Member
    $methodName = $null
    if ($memberNameNode -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $methodName = $memberNameNode.Value
    } else {
        $mr = Resolve-Const $memberNameNode $hasStrictMode $reservedVars $assignedVars
        if ($mr.Known -and $mr.Value -is [string]) { $methodName = $mr.Value } else { return 'method name not resolvable' }
    }
    if (-not $script:CffAllowedStaticStringMethods.ContainsKey($methodName)) { return "method not allowlisted: $methodName" }
    foreach ($argAst in $node.Arguments) {
        $nested = Resolve-StaticStringCall $argAst $hasStrictMode $reservedVars $assignedVars
        $ar = if ($nested.Known) { $nested } else { Resolve-Const $argAst $hasStrictMode $reservedVars $assignedVars }
        if (-not $ar.Known) { return 'argument not constant' }
    }
    return 'arity/bounds error during invocation'
}

function Invoke-PsFoldStaticStringCalls([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $folded   = 0
    $changed  = $true
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    while ($changed) {
        $changed = $false
        $tokS = $null; $errS = $null
        $sAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokS, [ref]$errS)
        $reps = [System.Collections.Generic.List[pscustomobject]]::new()

        foreach ($node in $sAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Static }, $true)) {
            $r = Resolve-StaticStringCall $node $true $emptySet $emptySet
            if (-not $r.Known -or $r.Value -isnot [string]) { continue }
            $escaped = $r.Value -replace "'", "''"
            $reps.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; T="'$escaped'" })
        }

        if ($reps.Count -gt 0) {
            $selected = [System.Collections.Generic.List[pscustomobject]]::new()
            foreach ($r in ($reps | Sort-Object { $_.E - $_.S } -Descending)) {
                $overlap = $false
                foreach ($s in $selected) {
                    if ($r.S -lt $s.E -and $r.E -gt $s.S) { $overlap = $true; break }
                }
                if (-not $overlap) { $selected.Add($r) }
            }
            foreach ($r in ($selected | Sort-Object { $_.S } -Descending)) {
                $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
            }
            $folded += $selected.Count
            $changed = $true
        }
    }

    # Diagnostics: classify any remaining (unfoldable) top-level static string-builder calls.
    $tokF = $null; $errF = $null
    $finalAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokF, [ref]$errF)
    $unresolved = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($node in $finalAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Static }, $true)) {
        $r = Resolve-StaticStringCall $node $true $emptySet $emptySet
        if (-not $r.Known) {
            $unresolved.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; Node=$node })
        }
    }
    $maximalUnresolved = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($u in ($unresolved | Sort-Object { $_.E - $_.S } -Descending)) {
        $overlap = $false
        foreach ($s in $maximalUnresolved) {
            if ($u.S -lt $s.E -and $u.E -gt $s.S) { $overlap = $true; break }
        }
        if (-not $overlap) { $maximalUnresolved.Add($u) }
    }
    $skipped = @($maximalUnresolved | ForEach-Object {
        [pscustomobject]@{ Reason = (Get-StaticStringCallSkipReason $_.Node $true $emptySet $emptySet) }
    })
    $byReason = ($skipped | Group-Object Reason | ForEach-Object { "$($_.Count)x $($_.Name)" }) -join ', '

    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        resolved     = $folded
        skipped      = $maximalUnresolved.Count
        by_reason    = $byReason
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 4: Base64 decode inlining
# ---------------------------------------------------------------------------

function Invoke-PsInlineBase64([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $inlined  = 0
    $tok3 = $null; $err3 = $null
    $bAst    = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok3, [ref]$err3)
    $reps    = [System.Collections.Generic.List[pscustomobject]]::new()
    $covered = [System.Collections.Generic.List[pscustomobject]]::new()

    foreach ($node in $bAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        $m = $node.Member
        if ($m -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }

        # Pattern B: [Encoding]::UTF8.GetString([Convert]::FromBase64String("..."))
        if ($m.Value -eq 'GetString' -and $node.Arguments.Count -eq 1) {
            $inner = $node.Arguments[0]
            if ($inner -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
                $b64 = Get-Base64Literal $inner
                if ($null -ne $b64) {
                    try {
                        $bytes   = [Convert]::FromBase64String($b64)
                        $decoded = [System.Text.Encoding]::UTF8.GetString($bytes)
                        $escaped = $decoded -replace "'", "''"
                        $reps.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; T="'$escaped'" })
                        $covered.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset })
                        $inlined++
                    } catch { }
                    continue
                }
            }
        }

        # Pattern A: [Convert]::FromBase64String("...")
        $b64 = Get-Base64Literal $node
        if ($null -eq $b64) { continue }
        $skip = $false
        foreach ($cr in $covered) {
            if ($node.Extent.StartOffset -ge $cr.S -and $node.Extent.EndOffset -le $cr.E) { $skip = $true; break }
        }
        if ($skip) { continue }
        try {
            $bytes       = [Convert]::FromBase64String($b64)
            $decoded     = [System.Text.Encoding]::UTF8.GetString($bytes)
            $isPrintable = ($decoded -notmatch '[\x00-\x08\x0b\x0c\x0e-\x1f]')
            if ($isPrintable) {
                $escaped = $decoded -replace "'", "''"
                $reps.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; T="'$escaped'" })
            } else {
                $hexArr = ($bytes | ForEach-Object { '0x{0:x2}' -f $_ }) -join ','
                $reps.Add([pscustomobject]@{ S=$node.Extent.StartOffset; E=$node.Extent.EndOffset; T="([byte[]]($hexArr))" })
            }
            $inlined++
        } catch { }
    }

    foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
        $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
    }
    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $inlined
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Variable extraction and renaming helpers
# ---------------------------------------------------------------------------

function Get-HeuristicName([string]$UserPath, $Preview, [int]$Index) {
    if ($UserPath -match '^[A-Za-z][A-Za-z0-9_]*$') { return $UserPath }
    if ($null -ne $Preview) {
        $p = [string]$Preview
        if ($p -match 'https?://') { return 'c2Url' }
        if ($p -match '\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b') { return 'c2Ip' }
        if ($p -match '(?i)(\\|/).*\.(exe|dll|bat|ps1|vbs|js)') { return 'dropPath' }
        if ($p -match '(?i)^function\s') { return 'funcDef' }
        if ($p -match '^[A-Za-z0-9+/=]{20,}$') { return "b64Part$Index" }
    }
    return "var$Index"
}

function Resolve-RhsPreview($rhsAst, $emptySet) {
    # AssignmentStatementAst.Right is typed as StatementAst; in practice it arrives as either
    # a PipelineAst (wrapping a single CommandExpressionAst) or directly as CommandExpressionAst.
    $expr = $null
    if ($rhsAst -is [System.Management.Automation.Language.PipelineAst]) {
        if ($rhsAst.PipelineElements.Count -ne 1) { return $null }
        if ($rhsAst.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $null }
        $expr = $rhsAst.PipelineElements[0].Expression
    } elseif ($rhsAst -is [System.Management.Automation.Language.CommandExpressionAst]) {
        $expr = $rhsAst.Expression
    } else {
        return $null
    }

    if ($expr -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $val = $expr.Value
        $preview = $null
        try {
            if ($val -match '^[A-Za-z0-9+/=]{8,}$' -and $val.Length % 4 -eq 0) {
                $bytes = [Convert]::FromBase64String($val)
                $decoded = [System.Text.Encoding]::UTF8.GetString($bytes)
                if ($decoded -notmatch '[\x00-\x08\x0b\x0c\x0e-\x1f]') { $preview = $decoded }
                else {
                    $decoded16 = [System.Text.Encoding]::Unicode.GetString($bytes)
                    if ($decoded16 -notmatch '[\x00-\x08\x0b\x0c\x0e-\x1f]') { $preview = $decoded16 }
                }
            }
        } catch {}
        if ($null -ne $preview) { return $preview }
        return $val
    }

    if ($expr -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
        $b64 = Get-Base64Literal $expr
        if ($null -ne $b64) {
            try { return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) } catch {}
        }
        return $null
    }

    $r = Resolve-Const $expr $false $emptySet $emptySet
    if ($r.Known -and $null -ne $r.Value) { return [string]$r.Value }
    return $null
}

function Invoke-PsExtractVariables([string]$InputPath) {
    $raw    = [System.IO.File]::ReadAllText($InputPath)
    $tokens = $null; $errors = $null
    $ast    = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    # All assignments with any operator (not just Equals)
    $assignNodes = $ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left -is [System.Management.Automation.Language.VariableExpressionAst]
    }, $true)

    # LHS offsets for all operators — reads exclude these
    $assignTargetOffsets = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($a in $assignNodes) { [void]$assignTargetOffsets.Add($a.Left.Extent.StartOffset) }

    # Per-variable: all assignment sites + append_built detection
    $assignmentsByVar = @{}
    $appendBuiltVars  = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    foreach ($a in $assignNodes) {
        $name  = Get-VarName $a.Left
        if ($name -match ':') { continue }
        $opStr = $a.Operator.ToString()
        $rhsText = $raw.Substring($a.Right.Extent.StartOffset,
                                   $a.Right.Extent.EndOffset - $a.Right.Extent.StartOffset).Trim()
        $site = [pscustomobject]@{
            line           = $a.Left.Extent.StartLineNumber
            operator       = $opStr
            rhs_text       = $rhsText
            resolved_value = Resolve-RhsPreview $a.Right $emptySet
        }
        if (-not $assignmentsByVar.ContainsKey($name)) {
            $assignmentsByVar[$name] = [System.Collections.Generic.List[pscustomobject]]::new()
        }
        [void]$assignmentsByVar[$name].Add($site)

        # append_built: += operator OR RHS AST contains a self-reference to this variable
        if ($opStr -eq 'PlusEquals') {
            [void]$appendBuiltVars.Add($name)
        } elseif (-not $appendBuiltVars.Contains($name)) {
            foreach ($rhsVar in $a.Right.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
                if ((Get-VarName $rhsVar) -eq $name) { [void]$appendBuiltVars.Add($name); break }
            }
        }
    }

    # Execution sink regions: iex/Invoke-Expression/Invoke-Command/Add-Type, & $var, $x.Invoke()
    $sinkRanges = [System.Collections.Generic.List[pscustomobject]]::new()
    $sinkCmds   = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('iex','Invoke-Expression','Invoke-Command','Add-Type') | ForEach-Object { [void]$sinkCmds.Add($_) }

    foreach ($cmd in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $cmdName = $cmd.GetCommandName()
        if ($null -ne $cmdName -and $sinkCmds.Contains($cmdName)) {
            $sinkRanges.Add([pscustomobject]@{ Start = $cmd.Extent.StartOffset; End = $cmd.Extent.EndOffset })
            continue
        }
        if ($cmd.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand) {
            $sinkRanges.Add([pscustomobject]@{ Start = $cmd.Extent.StartOffset; End = $cmd.Extent.EndOffset })
        }
    }
    foreach ($inv in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        $m = $inv.Member
        if ($m -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $m.Value -eq 'Invoke') {
            $sinkRanges.Add([pscustomobject]@{ Start = $inv.Expression.Extent.StartOffset; End = $inv.Expression.Extent.EndOffset })
        }
    }

    # Read-site collection: line numbers and offsets per variable (excluding all assignment LHS)
    $readLines   = @{}
    $readOffsets = @{}
    foreach ($v in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        if ($assignTargetOffsets.Contains($v.Extent.StartOffset)) { continue }
        $name = Get-VarName $v
        if ($name -match ':') { continue }
        if (-not $readLines.ContainsKey($name)) {
            $readLines[$name]   = New-Object 'System.Collections.Generic.SortedSet[int]'
            $readOffsets[$name] = New-Object 'System.Collections.Generic.List[int]'
        }
        [void]$readLines[$name].Add($v.Extent.StartLineNumber)
        [void]$readOffsets[$name].Add($v.Extent.StartOffset)
    }

    $allNames = [System.Collections.Generic.SortedSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($k in $assignmentsByVar.Keys) { [void]$allNames.Add($k) }
    foreach ($k in $readLines.Keys)        { [void]$allNames.Add($k) }

    $results = [System.Collections.Generic.List[pscustomobject]]::new()
    $idx = 0
    foreach ($name in $allNames) {
        if ($assignmentsByVar.ContainsKey($name)) { $sites   = @($assignmentsByVar[$name]) } else { $sites   = @() }
        if ($readLines.ContainsKey($name))        { $rLines  = @($readLines[$name]) }        else { $rLines  = @() }
        if ($readOffsets.ContainsKey($name))      { $offsets = @($readOffsets[$name]) }      else { $offsets = @() }

        $preview = $null
        foreach ($s in $sites) { if ($null -ne $s.resolved_value) { $preview = $s.resolved_value; break } }

        $reachesSink = $false
        :outer foreach ($off in $offsets) {
            foreach ($sr in $sinkRanges) {
                if ($off -ge $sr.Start -and $off -lt $sr.End) { $reachesSink = $true; break outer }
            }
        }

        $results.Add([pscustomobject]@{
            user_path        = $name
            assignment_count = $sites.Count
            assignments      = $sites
            append_built     = $appendBuiltVars.Contains($name)
            decoded_preview  = $preview
            use_count        = $rLines.Count
            use_lines        = $rLines
            reaches_sink     = $reachesSink
            suggested_name   = Get-HeuristicName $name $preview $idx
        })
        $idx++
    }

    $obfCount  = ($results | Where-Object { $_.user_path -match '[^A-Za-z0-9_]' }).Count
    $sinkCount = ($results | Where-Object { $_.reaches_sink }).Count
    return @{
        variables           = @($results)
        total_count         = $results.Count
        obfuscated_count    = $obfCount
        sink_reaching_count = $sinkCount
    }
}

function Invoke-PsRenameVariables([string]$InputPath, [string]$OutputPath, [hashtable]$Renames) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    $reps = [System.Collections.Generic.List[pscustomobject]]::new()
    $occurrences = @{}

    foreach ($v in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        $name = Get-VarName $v
        if (-not $Renames.ContainsKey($name)) { continue }
        $newName = $Renames[$name]
        $reps.Add([pscustomobject]@{ S = $v.Extent.StartOffset; E = $v.Extent.EndOffset; T = "`$$newName" })
        if (-not $occurrences.ContainsKey($name)) { $occurrences[$name] = 0 }
        $occurrences[$name]++
    }

    foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
        $raw = $raw.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
    }
    [System.IO.File]::WriteAllText($OutputPath, $raw)

    $occArr = @($occurrences.GetEnumerator() | ForEach-Object { [pscustomobject]@{ name=$_.Key; count=$_.Value } })
    return @{
        renamed      = $occurrences.Count
        occurrences  = $occArr
        input_bytes  = $inputLen
        output_bytes = $raw.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 6: Constant propagation + dead-store elimination
# ---------------------------------------------------------------------------

function Test-IsUnsafeAncestor($node) {
    return (
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst]       -or
        $node -is [System.Management.Automation.Language.WhileStatementAst]            -or
        $node -is [System.Management.Automation.Language.ForStatementAst]              -or
        $node -is [System.Management.Automation.Language.ForEachStatementAst]          -or
        $node -is [System.Management.Automation.Language.DoWhileStatementAst]          -or
        $node -is [System.Management.Automation.Language.DoUntilStatementAst]          -or
        $node -is [System.Management.Automation.Language.IfStatementAst]               -or
        $node -is [System.Management.Automation.Language.TryStatementAst]              -or
        $node -is [System.Management.Automation.Language.SwitchStatementAst]           -or
        $node -is [System.Management.Automation.Language.ScriptBlockExpressionAst]
    )
}

function Invoke-PsInlineConstants([string]$InputPath, [string]$OutputPath, [int]$MaxUses = 1) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    # All assignments with VariableExpressionAst on LHS
    $assignNodes = $ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left -is [System.Management.Automation.Language.VariableExpressionAst]
    }, $true)

    # Count assignments per variable; track the single AST node for each
    $assignCountByVar = @{}
    $assignNodeByVar  = @{}
    foreach ($a in $assignNodes) {
        $name = Get-VarName $a.Left
        if ($name -match ':') { continue }
        if (-not $assignCountByVar.ContainsKey($name)) { $assignCountByVar[$name] = 0 }
        $assignCountByVar[$name]++
        $assignNodeByVar[$name] = $a
    }

    # Guard set: variable names referenced by Get-Variable / Set-Variable string args
    $dynVarNames = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($cmd in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $cmdName = $cmd.GetCommandName()
        if ($cmdName -ne 'Get-Variable' -and $cmdName -ne 'Set-Variable') { continue }
        foreach ($el in $cmd.CommandElements) {
            if ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                [void]$dynVarNames.Add($el.Value.ToLowerInvariant())
            }
        }
    }

    # LHS offsets: used to distinguish assignment sites from read sites
    $assignTargetOffsets = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($a in $assignNodes) { [void]$assignTargetOffsets.Add($a.Left.Extent.StartOffset) }

    # Evaluate each single-assignment variable for eligibility
    $eligibleVars = @{}  # name -> {Literal, AssignStart, AssignEnd}
    $skipped = 0

    foreach ($name in $assignCountByVar.Keys) {
        # Condition 1: exactly one assignment with = (not +=, -=, etc.)
        if ($assignCountByVar[$name] -ne 1) { $skipped++; continue }
        $a = $assignNodeByVar[$name]
        if ($a.Operator.ToString() -ne 'Equals') { $skipped++; continue }

        # Condition 4: not scope-qualified ($global:, $script:, $env:, ...)
        if ($name -match ':') { $skipped++; continue }

        # Condition 6: not dynamically accessed via Get-Variable / Set-Variable
        if ($dynVarNames.Contains($name)) { $skipped++; continue }

        # Condition 2: assignment is at unconditional top level (no unsafe ancestor)
        $isTopLevel = $true
        $ancestor   = $a.Parent
        while ($null -ne $ancestor) {
            if (Test-IsUnsafeAncestor $ancestor) { $isTopLevel = $false; break }
            $ancestor = $ancestor.Parent
        }
        if (-not $isTopLevel) { $skipped++; continue }

        # Condition 3: RHS resolves to a constant (string / number / bool)
        # AssignmentStatementAst.Right arrives as PipelineAst or directly as CommandExpressionAst
        $expr = $null
        if ($a.Right -is [System.Management.Automation.Language.PipelineAst]) {
            if ($a.Right.PipelineElements.Count -ne 1) { $skipped++; continue }
            if ($a.Right.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { $skipped++; continue }
            $expr = $a.Right.PipelineElements[0].Expression
        } elseif ($a.Right -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $expr = $a.Right.Expression
        }
        if ($null -eq $expr) { $skipped++; continue }

        # Reject expressions that call any command (impure — re-evaluated per site)
        if (($expr.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)).Count -gt 0) {
            $skipped++; continue
        }

        # Strict mode = true: unknown variables NOT folded to $null
        $r = Resolve-Const $expr $true $emptySet $emptySet
        if (-not $r.Known -or $null -eq $r.Value) { $skipped++; continue }
        $val = $r.Value
        if ($val -isnot [string] -and $val -isnot [bool] -and
            $val -isnot [int]    -and $val -isnot [long] -and
            $val -isnot [double] -and $val -isnot [float]) { $skipped++; continue }

        # Build the single-quoted literal (safe against $ and backtick interpolation)
        if ($val -is [string]) {
            $literal = "'" + $val.Replace("'", "''") + "'"
        } elseif ($val -is [bool]) {
            $literal = if ($val) { '$true' } else { '$false' }
        } else {
            $literal = [string]$val
        }

        $eligibleVars[$name] = [pscustomobject]@{
            Literal     = $literal
            AssignStart = $a.Extent.StartOffset
            AssignEnd   = $a.Extent.EndOffset
        }
    }

    # Collect read sites; flag any splatted use (@var) which disqualifies the variable
    $readsByVar   = @{}
    $splattedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($v in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        if ($assignTargetOffsets.Contains($v.Extent.StartOffset)) { continue }
        $name = Get-VarName $v
        if (-not $eligibleVars.ContainsKey($name)) { continue }
        if ($v.Splatted) { [void]$splattedVars.Add($name); continue }
        if (-not $readsByVar.ContainsKey($name)) {
            $readsByVar[$name] = [System.Collections.Generic.List[pscustomobject]]::new()
        }
        $readsByVar[$name].Add([pscustomobject]@{ Start = $v.Extent.StartOffset; End = $v.Extent.EndOffset })
    }
    foreach ($n in $splattedVars) { [void]$eligibleVars.Remove($n); $skipped++ }

    # Apply max_uses gate and build the edit list
    $edits               = [System.Collections.Generic.List[pscustomobject]]::new()
    $changed             = 0
    $occurrencesReplaced = 0
    $assignmentsRemoved  = 0

    foreach ($name in $eligibleVars.Keys) {
        $info = $eligibleVars[$name]
        if (-not $readsByVar.ContainsKey($name)) { $skipped++; continue }
        if (-not $readsByVar[$name]) { $skipped++; continue }

        if ($readsByVar[$name]) { $reads = @($readsByVar[$name]) } else { $reads = @() }

        # max_uses <= 0 means unlimited; positive = max read-sites to accept
        if ($MaxUses -gt 0 -and $reads.Count -gt $MaxUses) { $skipped++; continue }

        foreach ($rd in $reads) {
            $edits.Add([pscustomobject]@{ Start = $rd.Start; End = $rd.End; Text = $info.Literal })
        }
        $expanded = Expand-SemiDeleteRange $raw $info.AssignStart $info.AssignEnd
        $edits.Add([pscustomobject]@{ Start = $expanded.Start; End = $expanded.End; Text = '' })

        $changed++
        $occurrencesReplaced += $reads.Count
        $assignmentsRemoved++
    }

    # Apply edits in descending offset order (established rewrite pattern)
    $out = $raw
    foreach ($e in ($edits | Sort-Object { $_.Start } -Descending)) {
        $out = $out.Remove($e.Start, $e.End - $e.Start).Insert($e.Start, $e.Text)
    }

    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        changed              = $changed
        occurrences_replaced = $occurrencesReplaced
        assignments_removed  = $assignmentsRemoved
        skipped              = $skipped
        input_bytes          = $inputLen
        output_bytes         = $out.Length
        output_path          = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Sequential constant propagation (flow-sensitive; generalizes InlineConstants)
# ---------------------------------------------------------------------------
# Invoke-PsInlineConstants only inlines variables assigned exactly once. Obfuscators defeat that by
# REUSING one variable name across a straight-line sequence — e.g. building a different member name
# into $m before each reflective call: `$m = <charcode chain>; (...).($m)(...); $m = <chain>; ...`.
# Each reused name holds a different constant at each use site, so single-assignment inlining and
# ResolveReflection's "single consistent value" map both skip it. This pass walks the root statements
# in source order, tracks each variable's CURRENT constant value in an environment, folds every
# resolvable top-level `=` assignment RHS to its literal, and substitutes each downstream read with
# the value in force at that position. Parse-only; the target is never executed.
#
# Soundness: values propagate only forward in source order via the KNOWN/NOT-YET-ASSIGNED/UNKNOWN
# model described at the top of the function body. Any variable assigned inside a statement whose
# execution/iteration order is not statically certain (a loop / if / try / switch / function body, or
# a += / ++ / -- ) is moved to UNKNOWN afterwards, so a stale value is never folded or substituted
# into or past such a construct. Chain to a pipeline fixpoint with FoldCharConcat / FoldArithmetic /
# ResolveReflection (values it exposes let those passes finish, and vice versa).
function Test-PropScalar($v) {
    return ($v -is [string] -or $v -is [bool] -or $v -is [char] -or
            $v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [float])
}

function Format-PropLiteral($val) {
    if ($val -is [string]) { return "'" + $val.Replace("'", "''") + "'" }
    if ($val -is [bool])   { if ($val) { return '$true' } else { return '$false' } }
    if ($val -is [char])   { return "([char]" + ([int]$val) + ")" }
    return (Format-NumLiteral $val)
}

# Assignments hidden inside statically-dead conditional branches (opaque predicates such as
# `elseif ((-40) -ge 55) { $seed = 152 }`) never execute, yet PsRemove-DeadCode leaves them when the
# enclosing `if` has a live clause (it only removes whole dead `if`s). Sequential propagation must
# treat such assignments as if they do not exist — otherwise a variable assigned ONLY in dead
# branches is misclassified as "assigned" (UNKNOWN) instead of never-assigned ($null → 0), which is
# how the sample's runtime actually evaluates it. Returns the set of assignment-target start offsets
# that live in a provably-dead branch: a clause whose condition is constant-falsy, any clause after a
# constant-true clause, and the else clause when some clause is constant-true.
function Get-DeadBranchAssignmentOffsets($ast, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    $dead = New-Object 'System.Collections.Generic.HashSet[int]'
    $collect = {
        param($block)
        if ($null -eq $block) { return }
        foreach ($a in $block.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
            if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) { [void]$dead.Add($a.Left.Extent.StartOffset) }
        }
        foreach ($u in $block.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
            $tk = $u.TokenKind.ToString()
            if (($tk -eq 'PlusPlus' -or $tk -eq 'MinusMinus') -and $u.Child -is [System.Management.Automation.Language.VariableExpressionAst]) {
                [void]$dead.Add($u.Child.Extent.StartOffset)
            }
        }
    }
    foreach ($if in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true)) {
        $trueSeen = $false
        foreach ($clause in $if.Clauses) {
            $cond = Get-CondExpr $clause.Item1
            if ($trueSeen -or ($null -ne $cond -and (Test-FalsyConst $cond $hasStrictMode $reservedVars $assignedVars))) {
                & $collect $clause.Item2
                continue
            }
            if ($null -ne $cond) {
                $rc = Resolve-Const $cond $hasStrictMode $reservedVars $assignedVars
                if ($rc.Known) {
                    $v = $rc.Value
                    $truthy = if     ($v -is [bool])                                  { $v }
                              elseif ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [float]) { $v -ne 0 }
                              elseif ($v -is [string])                                { $v.Length -gt 0 }
                              else                                                    { $null -ne $v }
                    if ($truthy) { $trueSeen = $true }
                }
            }
        }
        if ($trueSeen) { & $collect $if.ElseClause }
    }
    # Return the HashSet without PowerShell enumerating it: `return $dead` unrolls the collection to
    # the pipeline (empty set -> $null, 1 element -> bare [int]), so the caller's $deadTargets.Contains()
    # would throw. The unary comma wraps it in a 1-element array that unrolls back to the HashSet itself.
    return ,$dead
}

function Invoke-PropBlock {
    param($stmts, $vals, $unknownVars, $deadTargets, $reservedVars, [bool]$hasStrictMode, $edits, $stats)
    foreach ($stmt in $stmts) {
        # --- identify a simple top-level  $var = <expr>  assignment (RHS unwrapped for 5.1 & 7) ---
        $isSimpleAssign = $false; $assignName = $null; $rhsExpr = $null
        if ($stmt -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $stmt.Operator.ToString() -eq 'Equals' -and
            $stmt.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $ln = Get-VarName $stmt.Left
            if ($ln -notmatch ':') {
                if ($stmt.Right -is [System.Management.Automation.Language.PipelineAst]) {
                    if ($stmt.Right.PipelineElements.Count -eq 1 -and
                        $stmt.Right.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                        $rhsExpr = $stmt.Right.PipelineElements[0].Expression
                    }
                } elseif ($stmt.Right -is [System.Management.Automation.Language.CommandExpressionAst]) {
                    $rhsExpr = $stmt.Right.Expression
                }
                if ($null -ne $rhsExpr) { $isSimpleAssign = $true; $assignName = $ln }
            }
        }

        # --- assignment-target offsets (exclude from read substitution) + names assigned anywhere
        #     inside this statement (invalidate afterwards) ---
        # Always exclude an assignment's LHS from read substitution; but a target that lives in a
        # dead branch (never executes) must NOT invalidate the variable — leaving it never-assigned
        # so its reads still fold to $null, which is the true runtime value.
        $targetOffsets  = New-Object 'System.Collections.Generic.HashSet[int]'
        $assignedInside = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($a in $stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
            if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
                [void]$targetOffsets.Add($a.Left.Extent.StartOffset)
                if (-not $deadTargets.Contains($a.Left.Extent.StartOffset)) { [void]$assignedInside.Add((Get-VarName $a.Left)) }
            }
        }
        foreach ($u in $stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
            $tk = $u.TokenKind.ToString()
            if (($tk -eq 'PlusPlus' -or $tk -eq 'MinusMinus') -and
                $u.Child -is [System.Management.Automation.Language.VariableExpressionAst]) {
                [void]$targetOffsets.Add($u.Child.Extent.StartOffset)
                if (-not $deadTargets.Contains($u.Child.Extent.StartOffset)) { [void]$assignedInside.Add((Get-VarName $u.Child)) }
            }
        }

        # --- fold a resolvable simple-assignment RHS to a literal ---
        $folded = $false
        if ($isSimpleAssign) {
            $cache = [System.Collections.Generic.Dictionary[System.Management.Automation.Language.Ast,object]]::new()
            $r = Resolve-Const $rhsExpr $hasStrictMode $reservedVars $unknownVars $vals $cache
            if ($r.Known -and (Test-PropScalar $r.Value)) {
                $edits.Add([pscustomobject]@{ Start=$rhsExpr.Extent.StartOffset; End=$rhsExpr.Extent.EndOffset; Text=(Format-PropLiteral $r.Value) })
                $vals[$assignName] = $r.Value
                [void]$unknownVars.Remove($assignName)   # now KNOWN
                $stats.foldedAssigns++
                $folded = $true
            }
        }

        # --- for IfStatementAst: substitute condition reads flat, then recurse into each clause body
        #     with a cloned environment so intra-branch sequential propagation works without leaking
        #     branch-local values back into the outer scope ---
        $handledAsIf = $false
        if (-not $folded -and $stmt -is [System.Management.Automation.Language.IfStatementAst]) {
            $handledAsIf = $true
            # Substitute outer-known reads appearing in condition expressions
            foreach ($clause in $stmt.Clauses) {
                foreach ($v in $clause.Item1.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
                    if ($targetOffsets.Contains($v.Extent.StartOffset)) { continue }
                    if ($v.Splatted) { continue }
                    $nm = Get-VarName $v
                    if (-not $vals.ContainsKey($nm)) { continue }
                    $edits.Add([pscustomobject]@{ Start=$v.Extent.StartOffset; End=$v.Extent.EndOffset; Text=(Format-PropLiteral $vals[$nm]) })
                    $stats.substitutedReads++
                }
            }
            # Recurse into each if/elseif clause body with a cloned environment
            foreach ($clause in $stmt.Clauses) {
                $clonedVals    = @{} + $vals
                $clonedUnknown = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
                foreach ($k in $unknownVars) { [void]$clonedUnknown.Add($k) }
                Invoke-PropBlock $clause.Item2.Statements $clonedVals $clonedUnknown $deadTargets $reservedVars $hasStrictMode $edits $stats
            }
            # Recurse into else clause body with a cloned environment
            if ($null -ne $stmt.ElseClause) {
                $clonedVals    = @{} + $vals
                $clonedUnknown = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
                foreach ($k in $unknownVars) { [void]$clonedUnknown.Add($k) }
                Invoke-PropBlock $stmt.ElseClause.Statements $clonedVals $clonedUnknown $deadTargets $reservedVars $hasStrictMode $edits $stats
            }
        }

        # --- else substitute known reads with the value in force here (skip targets & self-reassigned) ---
        if (-not $folded -and -not $handledAsIf) {
            foreach ($v in $stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
                if ($targetOffsets.Contains($v.Extent.StartOffset)) { continue }
                if ($v.Splatted) { continue }
                $nm = Get-VarName $v
                if ($assignedInside.Contains($nm)) { continue }
                if (-not $vals.ContainsKey($nm)) { continue }
                $edits.Add([pscustomobject]@{ Start=$v.Extent.StartOffset; End=$v.Extent.EndOffset; Text=(Format-PropLiteral $vals[$nm]) })
                $stats.substitutedReads++
            }
        }

        # --- every variable this statement assigned (except the one just folded) is now assigned but
        #     not statically known → move to UNKNOWN so its later reads never fold to $null ---
        foreach ($nm in $assignedInside) {
            if ($folded -and $nm -eq $assignName) { continue }
            [void]$vals.Remove($nm)
            [void]$unknownVars.Add($nm)
        }
    }
}

function Invoke-PsPropagateConstants([string]$InputPath, [string]$OutputPath) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    # Emulate the target's own non-strict runtime with POSITION-AWARE null tracking. At each point in
    # the walk a variable is in one of three states: KNOWN (a resolved value, held in $vals); NOT-YET-
    # ASSIGNED (no assignment seen so far in source order → $null, i.e. 0 in arithmetic — this is how
    # the sample's running-accumulator seeds like `$x = (($x-70)*$x)` start, and how forward-referenced
    # vars read before their first assignment behave); or UNKNOWN (assigned earlier but not statically
    # resolvable, held in $unknownVars → must NOT fold to null). A whole-file assignedVars set cannot
    # tell NOT-YET-ASSIGNED from UNKNOWN, so instead $unknownVars is grown as the walk proceeds and
    # passed as Resolve-Const's assignedVars: Test-FoldableNull then folds a var to $null iff it is
    # neither known nor yet marked unknown. If the script uses Set-StrictMode, null-folding is off.
    $hasStrictMode = ($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.GetCommandName() -eq 'Set-StrictMode' }, $true)).Count -gt 0

    $reservedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('_','error','?','lastexitcode','matches','args','input','pscmdlet','psitem',
      'true','false','null','pid','pwd','home','host','ofs','psscriptroot',
      'pscommandpath','executioncontext','nestedpromptlevel','shellid') |
      ForEach-Object { [void]$reservedVars.Add($_) }

    # Full-file assigned-var set — used only to evaluate opaque-predicate branch conditions the same
    # way Invoke-PsRemoveDeadCode does (never-assigned condition vars fold to $null; assigned ones
    # stay unknown so a live-var-gated branch is never wrongly pruned). Then compute the assignment
    # offsets that sit in dead branches, so the walk can ignore them (see Get-DeadBranchAssignmentOffsets).
    $assignedVarsAll = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($a in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            [void]$assignedVarsAll.Add($a.Left.VariablePath.UserPath.ToLowerInvariant())
        }
    }
    $deadTargets = Get-DeadBranchAssignmentOffsets $ast $hasStrictMode $reservedVars $assignedVarsAll

    # Grown during the walk: variables assigned so far whose value is not statically known. Passed as
    # Resolve-Const's assignedVars so they stay $unknown while not-yet-assigned vars fold to $null.
    $unknownVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    $rootBlock = $ast.EndBlock
    if ($null -eq $rootBlock -or $null -eq $rootBlock.Statements) {
        [System.IO.File]::WriteAllText($OutputPath, $raw)
        return @{ changed=0; folded_assignments=0; substituted_reads=0; input_bytes=$inputLen; output_bytes=$raw.Length; output_path=$OutputPath }
    }

    # $vals: lowercased varname -> current known constant scalar value (NOT $env — that is the
    # PowerShell environment-variable drive).
    $vals             = @{}
    $edits            = [System.Collections.Generic.List[pscustomobject]]::new()
    $stats            = @{ foldedAssigns = 0; substitutedReads = 0 }
    Invoke-PropBlock $rootBlock.Statements $vals $unknownVars $deadTargets $reservedVars $hasStrictMode $edits $stats
    $foldedAssigns    = $stats.foldedAssigns
    $substitutedReads = $stats.substitutedReads

    # Single left-to-right rebuild (O(n + edits)); this pass can emit tens of thousands of edits,
    # for which repeated String.Remove/Insert on the whole file would be O(edits * filesize). Edits
    # are non-overlapping by construction (a fold replaces a whole RHS; substituted reads are
    # disjoint); the $pos guard just defends against any accidental overlap.
    $sb  = [System.Text.StringBuilder]::new([int]($raw.Length * 1.1))
    $pos = 0
    foreach ($e in ($edits | Sort-Object Start)) {
        if ($e.Start -lt $pos) { continue }
        [void]$sb.Append($raw, $pos, $e.Start - $pos)
        [void]$sb.Append($e.Text)
        $pos = $e.End
    }
    [void]$sb.Append($raw, $pos, $raw.Length - $pos)
    $out = $sb.ToString()
    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        changed            = $edits.Count
        folded_assignments = $foldedAssigns
        substituted_reads  = $substitutedReads
        input_bytes        = $inputLen
        output_bytes       = $out.Length
        output_path        = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 7: iex / -EncodedCommand annotation
# ---------------------------------------------------------------------------

function Invoke-PsAnnotateIex([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $annotated = 0
    $tok4 = $null; $err4 = $null
    $iAst      = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok4, [ref]$err4)
    $insertions = [System.Collections.Generic.List[pscustomobject]]::new()

    foreach ($cmd in $iAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $name = $cmd.GetCommandName()
        if ($null -eq $name) { continue }

        if ($name -in @('iex', 'Invoke-Expression') -and $cmd.CommandElements.Count -ge 2) {
            $argEl = $cmd.CommandElements[1]
            if ($argEl -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                $lines = ($argEl.Value -split "`n" | ForEach-Object { "# > $_" }) -join "`r`n"
                $insertions.Add([pscustomobject]@{
                    Offset = $cmd.Extent.EndOffset
                    Text   = "`r`n# <<<IEX PAYLOAD BEGIN>>>`r`n$lines`r`n# <<<IEX PAYLOAD END>>>"
                })
                $annotated++
                continue
            }
        }

        $els = $cmd.CommandElements
        for ($i = 0; $i -lt $els.Count - 1; $i++) {
            $el = $els[$i]
            if ($el -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
            if ($el.Value -notmatch '(?i)^-Enc') { continue }
            $b64El = $els[$i + 1]
            if ($b64El -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
            try {
                $bytes   = [Convert]::FromBase64String($b64El.Value)
                $payload = [System.Text.Encoding]::Unicode.GetString($bytes)
                $lines   = ($payload -split "`n" | ForEach-Object { "# > $_" }) -join "`r`n"
                $insertions.Add([pscustomobject]@{
                    Offset = $cmd.Extent.EndOffset
                    Text   = "`r`n# <<<ENCODED COMMAND BEGIN>>>`r`n$lines`r`n# <<<ENCODED COMMAND END>>>"
                })
                $annotated++
            } catch { }
        }
    }

    foreach ($ins in ($insertions | Sort-Object { $_.Offset } -Descending)) {
        $text = $text.Insert($ins.Offset, $ins.Text)
    }
    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $annotated
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Invoke-PsStripLines — Remove lines matching a regex pattern
# ---------------------------------------------------------------------------

function Invoke-PsStripLines {
    param(
        [string]$InputPath,
        [string]$OutputPath,
        [string]$Pattern,
        [string]$Flags = "i"
    )
    $options = [System.Text.RegularExpressions.RegexOptions]::None
    if ($Flags -match 'i') { $options = $options -bor [System.Text.RegularExpressions.RegexOptions]::IgnoreCase }
    if ($Flags -match 'm') { $options = $options -bor [System.Text.RegularExpressions.RegexOptions]::Multiline }
    if ($Flags -match 's') { $options = $options -bor [System.Text.RegularExpressions.RegexOptions]::Singleline }

    $lines   = [System.IO.File]::ReadAllLines($InputPath, [System.Text.Encoding]::UTF8)
    $kept    = $lines | Where-Object { -not [regex]::IsMatch($_, $Pattern, $options) }
    $removed = $lines.Count - @($kept).Count
    [System.IO.File]::WriteAllText($OutputPath, ($kept -join "`n"), [System.Text.Encoding]::UTF8)
    return @{
        removed_lines = $removed
        kept_lines    = @($kept).Count
        total_lines   = $lines.Count
        output_path   = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Invoke-PsStripComments — Remove comment-only lines (token-driven)
# ---------------------------------------------------------------------------

# Comments whose text carries meaning beyond documentation and must survive every strip:
#   * `#requires -Version 5` — a real directive PowerShell acts on at load time. Deleting it
#     changes how the script runs, so it is never optional.
#   * `#!` shebang — interpreter hint for the *nix launcher.
#   * PsAnnotate-Iex output (`# <<<IEX PAYLOAD BEGIN>>>`, `# > <payload line>`, …, emitted by
#     Invoke-PsAnnotateIex above) — that pass exists to recover payloads INTO comments, so stripping
#     them would throw away the analysis result of a sibling tool.
$script:PsProtectedCommentPatterns = @(
    '(?i)^#requires\b'
    '^#!'
    '^#\s*(>|<<<(IEX PAYLOAD|ENCODED COMMAND) (BEGIN|END)>>>)'
)

# Removes comments the parser identified, working from the TOKEN STREAM rather than a line regex.
# That distinction is the whole point of this pass: a `#` inside a here-string or a multi-line
# string is never a Comment token, so string data can not be corrupted the way a `^\s*#` line filter
# (Invoke-PsStripLines) corrupts it. Block comments `<# … #>` are handled for free — the token
# extent already spans every line of the block.
#
# Default scope is a comment that OWNS its line (only whitespace before it): the line, its indent
# and its newline all go. A comment sitting after real code is left alone unless -IncludeTrailing,
# and even then only when it runs to end of line — an inline `Write-Host<#x#>hi` is skipped outright
# because deleting it would fuse two adjacent tokens into one. Unrecognized shape -> keep, the same
# convention every other pass in this library follows (a missed cleanup, never a corruption).
#
# Edits are offset-based over the raw text, so line endings (this toolkit routinely sees mixed
# CRLF/LF samples) and the final-newline state survive byte-for-byte outside the deleted ranges.
function Invoke-PsStripComments {
    param(
        [string]$InputPath,
        [string]$OutputPath,
        [bool]$IncludeTrailing = $false,
        [string]$KeepPattern = $null
    )
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    # Parse-only — the target script is never executed. Parse errors are not fatal: a partially
    # parsed sample still yields usable comment tokens, and every other pass tolerates them too.
    [void][System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    $comments = @($tokens | Where-Object { $_.Kind -eq [System.Management.Automation.Language.TokenKind]::Comment })

    $ranges          = [System.Collections.Generic.List[pscustomobject]]::new()
    $lineRemoved     = 0
    $trailingRemoved = 0
    $kept            = 0

    foreach ($c in $comments) {
        $text = $c.Extent.Text

        $protect = $false
        foreach ($p in $script:PsProtectedCommentPatterns) {
            if ($text -match $p) { $protect = $true; break }
        }
        if (-not $protect -and $KeepPattern -and $text -match $KeepPattern) { $protect = $true }
        if ($protect) { $kept++; continue }

        $s = $c.Extent.StartOffset
        $e = $c.Extent.EndOffset

        # Walk left over the indent. Line-leading means nothing but whitespace precedes it on
        # its line (or it is the very start of the file).
        $ls = $s
        while ($ls -gt 0 -and ($raw[$ls - 1] -eq ' ' -or $raw[$ls - 1] -eq "`t")) { $ls-- }
        $lineLeading = ($ls -eq 0) -or ($raw[$ls - 1] -eq "`n")

        # Walk right over trailing whitespace, then the line terminator if one is there.
        $re = $e
        while ($re -lt $raw.Length -and ($raw[$re] -eq ' ' -or $raw[$re] -eq "`t")) { $re++ }
        $runsToEol = $true
        if ($re -lt $raw.Length) {
            if ($raw[$re] -eq "`r") { $re++ }
            if ($re -lt $raw.Length -and $raw[$re] -eq "`n") { $re++ }
            else { $runsToEol = ($re -ge $raw.Length) }   # non-newline follows -> code after the comment
        }

        if ($lineLeading) {
            # The comment owns the line: take indent, comment and newline. At EOF (no trailing
            # newline) $re is simply the end of the file, which deletes just as cleanly.
            if (-not $runsToEol) { $kept++; continue }   # e.g. `<#x#>code` at the start of a line
            $ranges.Add([pscustomobject]@{ Start = $ls; End = $re })
            $lineRemoved++
        }
        elseif ($IncludeTrailing -and $runsToEol) {
            # Trailing comment after real code: drop the separating whitespace and the comment,
            # but KEEP the newline so the code line stays a line.
            $ranges.Add([pscustomobject]@{ Start = $ls; End = $e })
            $trailingRemoved++
        }
        else {
            $kept++
        }
    }

    $out = $raw
    if ($ranges.Count -gt 0) {
        # Ranges are disjoint by construction (each whole-line delete consumes its own newline, so
        # the next line's left-walk can not reach back into it) — coalescing is defensive and free.
        $merged = Coalesce-Ranges $ranges
        $sb = [System.Text.StringBuilder]::new($raw)
        foreach ($r in ($merged | Sort-Object -Property Start -Descending)) {
            [void]$sb.Remove($r.Start, $r.End - $r.Start)
        }
        $out = $sb.ToString()
    }

    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        changed                   = $lineRemoved + $trailingRemoved
        comment_lines_removed     = $lineRemoved
        trailing_comments_removed = $trailingRemoved
        comments_kept             = $kept
        input_bytes               = $inputLen
        output_bytes              = $out.Length
        output_path               = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 9: Array-assembly join folding  ($v = @(); $v += @(...); $v -join 'sep')
# ---------------------------------------------------------------------------

function Get-AllStringElements($exprAst) {
    # Returns List[string] of all string values if exprAst is a constant-string
    # expression (run ps_fold_strings first so + concatenations are already folded).
    # Handles: ArrayLiteralAst of resolvable strings, the @()-form ArrayExpressionAst,
    # and any single expression Resolve-Const collapses to a string (bare literal,
    # $('x')/('x') wrappers, + concatenations, allowlisted method chains). Returns
    # $null if any element is non-constant.
    if ($null -eq $exprAst) { return $null }
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    # NB: every List return uses the unary comma (`return ,$x`) so PowerShell does not enumerate the
    # collection at the function boundary — a bare `return $list` unrolls (empty -> $null, 1 elem ->
    # scalar), which would break any caller that calls a method on the result. `,$null` still
    # propagates $null, so the "non-constant -> $null" contract is preserved. The recursive return
    # needs its own comma too, or the inner List is re-enumerated here.
    if ($exprAst -is [System.Management.Automation.Language.ArrayLiteralAst]) {
        $r = [System.Collections.Generic.List[string]]::new()
        foreach ($el in $exprAst.Elements) {
            $rv = Resolve-Const $el $true $emptySet $emptySet
            if (-not $rv.Known -or $rv.Value -isnot [string]) { return $null }
            $r.Add($rv.Value)
        }
        return ,$r
    }

    if ($exprAst -is [System.Management.Automation.Language.ArrayExpressionAst]) {
        $inner = $exprAst.SubExpression
        if ($inner.Statements.Count -eq 0) {
            return ,([System.Collections.Generic.List[string]]::new())   # @() empty init
        }
        if ($inner.Statements.Count -eq 1 -and
            $inner.Statements[0] -is [System.Management.Automation.Language.PipelineAst]) {
            $pipe = $inner.Statements[0]
            if ($pipe.PipelineElements.Count -eq 1 -and
                $pipe.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                return ,(Get-AllStringElements $pipe.PipelineElements[0].Expression)
            }
        }
        return $null
    }

    # String char-selection: ("charset")[i,j,k] — a constant string indexed by one or
    # more integer positions, yielding those characters. Obfuscators use it to spell a
    # command/type name out of a scrambled alphabet, e.g.
    # ("...Get...")[9,2,17,0,15,8,22,17,2,22,17] -join "" -> 'Get-Content'. Each selected
    # char is returned as its own single-char string element, so the caller's
    # `$fragments -join $sep` reproduces PowerShell's `("str")[i,j] -join "sep"` exactly.
    # Array-index (returns a collection) lives here rather than in Resolve-Const, whose
    # contract is scalar-only. Any non-constant target/index, or an out-of-range position,
    # bails to $null — a missed fold, never a guess.
    if ($exprAst -is [System.Management.Automation.Language.IndexExpressionAst]) {
        $tgt = Resolve-Const $exprAst.Target $true $emptySet $emptySet
        if (-not $tgt.Known -or $tgt.Value -isnot [string]) { return $null }
        $s = [string]$tgt.Value

        # Index is either an ArrayLiteralAst (i,j,k) or a single scalar expression.
        $idxAsts = if ($exprAst.Index -is [System.Management.Automation.Language.ArrayLiteralAst]) {
            $exprAst.Index.Elements
        } else {
            @($exprAst.Index)
        }

        $r = [System.Collections.Generic.List[string]]::new()
        foreach ($idxAst in $idxAsts) {
            $ir = Resolve-Const $idxAst $true $emptySet $emptySet
            if (-not $ir.Known -or ($ir.Value -isnot [int] -and $ir.Value -isnot [long])) { return $null }
            $i = [int]$ir.Value
            if ($i -lt 0) { $i += $s.Length }            # PowerShell negative indexing
            if ($i -lt 0 -or $i -ge $s.Length) { return $null }  # out of range -> bail
            $r.Add([string]$s[$i])
        }
        return ,$r
    }

    # Scalar fallback: any single expression that Resolve-Const collapses to a string
    # (bare literal, $('x')/('x') subexpression, + concatenation, allowlisted chain).
    $rv = Resolve-Const $exprAst $true $emptySet $emptySet
    if ($rv.Known -and $rv.Value -is [string]) {
        $r = [System.Collections.Generic.List[string]]::new()
        $r.Add($rv.Value)
        return ,$r
    }

    return $null
}

function Invoke-PsFoldArrayJoins([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $folded   = 0
    $changed  = $true
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    while ($changed) {
        $changed = $false
        $tok = $null; $err = $null
        $sAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$err)

        # All assignments with VariableExpressionAst on LHS
        $assignNodes = @($sAst.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left -is [System.Management.Automation.Language.VariableExpressionAst]
        }, $true) | Sort-Object { $_.Extent.StartOffset })

        # Variables accessed via Get-Variable / Set-Variable are disqualified
        $dynVarNames = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($cmd in $sAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            $cmdName = $cmd.GetCommandName()
            if ($cmdName -ne 'Get-Variable' -and $cmdName -ne 'Set-Variable') { continue }
            foreach ($el in $cmd.CommandElements) {
                if ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                    [void]$dynVarNames.Add($el.Value.ToLowerInvariant())
                }
            }
        }

        # Group assignments by name, preserving document order
        $assignsByVar = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.List[System.Management.Automation.Language.Ast]]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($a in $assignNodes) {
            $name = Get-VarName $a.Left
            if ($name -match ':') { continue }
            if (-not $assignsByVar.ContainsKey($name)) {
                $assignsByVar[$name] = [System.Collections.Generic.List[System.Management.Automation.Language.Ast]]::new()
            }
            $assignsByVar[$name].Add($a)
        }

        # Identify candidates: every assignment is top-level with a constant-string RHS
        $candidates = @{}   # name -> List[string] (ordered fragments)

        foreach ($name in $assignsByVar.Keys) {
            if ($dynVarNames.Contains($name)) { continue }

            $assignments = $assignsByVar[$name]
            $fragments   = [System.Collections.Generic.List[string]]::new()
            $valid       = $true

            foreach ($a in $assignments) {
                $op = $a.Operator.ToString()
                if ($op -ne 'Equals' -and $op -ne 'PlusEquals') { $valid = $false; break }

                # All assignments must be at unconditional top level
                $ancestor = $a.Parent
                while ($null -ne $ancestor) {
                    if (Test-IsUnsafeAncestor $ancestor) { $valid = $false; break }
                    $ancestor = $ancestor.Parent
                }
                if (-not $valid) { break }

                # Unwrap PipelineAst -> CommandExpressionAst -> Expression
                $rhs  = $a.Right
                $expr = $null
                if ($rhs -is [System.Management.Automation.Language.PipelineAst] -and
                    $rhs.PipelineElements.Count -eq 1 -and
                    $rhs.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                    $expr = $rhs.PipelineElements[0].Expression
                } elseif ($rhs -is [System.Management.Automation.Language.CommandExpressionAst]) {
                    $expr = $rhs.Expression
                }
                if ($null -eq $expr) { $valid = $false; break }

                if ($op -eq 'Equals') {
                    # The only permitted = assignment is an @() empty-array initialiser
                    if ($expr -is [System.Management.Automation.Language.ArrayExpressionAst] -and
                        $expr.SubExpression.Statements.Count -eq 0) {
                        continue
                    }
                    $valid = $false; break
                }

                # PlusEquals: collect constant string elements
                $els = Get-AllStringElements $expr
                if ($null -eq $els) { $valid = $false; break }
                foreach ($el in $els) { [void]$fragments.Add($el) }
            }

            if ($valid -and $fragments.Count -gt 0) {
                $candidates[$name] = $fragments
            }
        }

        # For each candidate, locate join sites and build replacements
        $reps            = [System.Collections.Generic.List[pscustomobject]]::new()
        $assignsToRemove = [System.Collections.Generic.List[pscustomobject]]::new()

        foreach ($name in $candidates.Keys) {
            $fragments = $candidates[$name]

            # Binary join: $name -join 'sep'
            $joinNodes = @($sAst.FindAll({
                param($n)
                $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and
                $n.Operator.ToString() -eq 'Join' -and
                $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                (Get-VarName $n.Left) -eq $name
            }, $true))

            if ($joinNodes.Count -eq 0) { continue }

            # Validate separator for every join site before committing any reps
            $localReps      = [System.Collections.Generic.List[pscustomobject]]::new()
            $joinVarOffsets = [System.Collections.Generic.HashSet[int]]::new()
            $allJoinsValid  = $true

            foreach ($joinNode in $joinNodes) {
                $sepRv = Resolve-Const $joinNode.Right $true $emptySet $emptySet
                if (-not $sepRv.Known -or $sepRv.Value -isnot [string]) { $allJoinsValid = $false; break }
                $result  = $fragments -join $sepRv.Value
                $escaped = $result -replace "'", "''"
                $localReps.Add([pscustomobject]@{ S = $joinNode.Extent.StartOffset; E = $joinNode.Extent.EndOffset; T = "'$escaped'" })
                [void]$joinVarOffsets.Add($joinNode.Left.Extent.StartOffset)
            }

            if (-not $allJoinsValid) { continue }
            foreach ($r in $localReps) { $reps.Add($r) }

            # Gated removal: only remove assignments when the join is the sole reader
            $assignTargetOffsets = [System.Collections.Generic.HashSet[int]]::new()
            foreach ($a in $assignsByVar[$name]) { [void]$assignTargetOffsets.Add($a.Left.Extent.StartOffset) }

            $allVarRefs   = @($sAst.FindAll({
                param($n)
                $n -is [System.Management.Automation.Language.VariableExpressionAst] -and
                (Get-VarName $n) -eq $name
            }, $true))
            $readRefs     = @($allVarRefs | Where-Object { -not $assignTargetOffsets.Contains($_.Extent.StartOffset) })
            $nonJoinReads = @($readRefs    | Where-Object { -not $joinVarOffsets.Contains($_.Extent.StartOffset) })

            if ($nonJoinReads.Count -eq 0) {
                foreach ($a in $assignsByVar[$name]) {
                    $range = Expand-SemiDeleteRange $text $a.Extent.StartOffset $a.Extent.EndOffset
                    $assignsToRemove.Add([pscustomobject]@{ S = $range.Start; E = $range.End; T = '' })
                }
            }
        }

        # Inline literal join: @('a','b',...) -join 'sep' (no variable indirection).
        # Disjoint from the variable-accumulator path above: that path owns joins whose
        # Left is a VariableExpressionAst; this one owns joins whose Left is an inline
        # array literal, so the two never produce overlapping edit ranges. Also owns the
        # string char-selection form ("charset")[i,j,...] -join 'sep' (Left is an
        # IndexExpressionAst) — Get-AllStringElements resolves it to its selected chars;
        # still disjoint from the variable/unary branches by Left node type.
        $inlineJoinNodes = @($sAst.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and
            $n.Operator.ToString() -eq 'Join' -and
            ($n.Left -is [System.Management.Automation.Language.ArrayExpressionAst] -or
             $n.Left -is [System.Management.Automation.Language.ArrayLiteralAst] -or
             $n.Left -is [System.Management.Automation.Language.IndexExpressionAst])
        }, $true))

        foreach ($joinNode in $inlineJoinNodes) {
            $els = Get-AllStringElements $joinNode.Left
            if ($null -eq $els) { continue }

            $sepRv = Resolve-Const $joinNode.Right $true $emptySet $emptySet
            if (-not $sepRv.Known -or $sepRv.Value -isnot [string]) { continue }

            $result  = $els -join $sepRv.Value
            $escaped = $result -replace "'", "''"
            $reps.Add([pscustomobject]@{ S = $joinNode.Extent.StartOffset; E = $joinNode.Extent.EndOffset; T = "'$escaped'" })
        }

        # Inline unary join: -join @('a','b',...)  (prefix form, no LHS, empty separator).
        # Parses as UnaryExpressionAst with TokenKind Join and .Child = the array operand
        # -- a distinct node type from the binary cases above, so their edit ranges never
        # collide. When such a join is nested (an element is itself a join), Get-AllStringElements
        # fails to resolve it this pass, so only the innermost resolvable join folds; the fixpoint
        # loop then materialises each outer layer on a later iteration -- never an overlap.
        $unaryJoinNodes = @($sAst.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.UnaryExpressionAst] -and
            $n.TokenKind.ToString() -eq 'Join'
        }, $true))

        foreach ($joinNode in $unaryJoinNodes) {
            $els = Get-AllStringElements $joinNode.Child
            if ($null -eq $els) { continue }

            $result  = $els -join ''
            $escaped = $result -replace "'", "''"
            $reps.Add([pscustomobject]@{ S = $joinNode.Extent.StartOffset; E = $joinNode.Extent.EndOffset; T = "'$escaped'" })
        }

        if ($reps.Count -gt 0) {
            $allEdits = [System.Collections.Generic.List[pscustomobject]]::new()
            foreach ($r in $reps)            { $allEdits.Add($r) }
            foreach ($r in $assignsToRemove) { $allEdits.Add($r) }
            foreach ($r in ($allEdits | Sort-Object { $_.S } -Descending)) {
                $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
            }
            $folded  += $reps.Count
            $changed  = $true
        }
    }

    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $folded
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass 10: Semicolon-to-newline expansion with indent tracking
# Converts single-line semicolon-packed scripts to one-statement-per-line.
# Uses the token stream so semicolons inside strings are never touched.
# ---------------------------------------------------------------------------

function Invoke-PsExpandSemicolons {
    param(
        [string]$InputPath,
        [string]$OutputPath,
        [string]$IndentString = '    '
    )
    $raw    = [System.IO.File]::ReadAllText($InputPath)
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    # A `for(init; cond; iter)` header uses TokenKind.Semi for its two separators — the same kind
    # emitted for a real statement terminator — so newlining them blindly breaks the loop header.
    # Precompute each for-header span [statement start, body `{` start); a Semi whose offset falls
    # inside one is a header separator and must stay a literal ';'. Nested for loops are handled
    # naturally: an inner header sits inside the inner ForStatementAst's own range.
    $forHeaderRanges = [System.Collections.Generic.List[object]]::new()
    foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ForStatementAst] }, $true)) {
        $forHeaderRanges.Add([pscustomobject]@{ Start = $f.Extent.StartOffset; BodyStart = $f.Body.Extent.StartOffset })
    }

    $sb          = [System.Text.StringBuilder]::new($raw.Length * 2)
    $depth       = 0
    $indent      = ''
    $prevEnd     = 0
    $atLineStart = $true

    $TK = [System.Management.Automation.Language.TokenKind]

    foreach ($tok in $tokens) {
        $kind = $tok.Kind
        if ($kind -eq $TK::EndOfInput) { break }

        $tokStart = $tok.Extent.StartOffset
        $tokEnd   = $tok.Extent.EndOffset
        $tokText  = $raw.Substring($tokStart, $tokEnd - $tokStart)

        # Gap between previous token end and this token start.
        # Suppress if we just emitted a newline (indent will be added lazily).
        $gap = if ($tokStart -gt $prevEnd -and -not $atLineStart) {
            $raw.Substring($prevEnd, $tokStart - $prevEnd)
        } else { '' }

        if ($kind -eq $TK::Semi) {
            $inForHeader = $false
            foreach ($r in $forHeaderRanges) {
                if ($tokStart -ge $r.Start -and $tokStart -lt $r.BodyStart) { $inForHeader = $true; break }
            }
            if ($inForHeader) {
                if ($atLineStart) { [void]$sb.Append($indent); $atLineStart = $false }
                [void]$sb.Append(';')   # keep the for-header separator; drop leading gap so no " ;"
            } else {
                [void]$sb.Append("`n")
                $atLineStart = $true
            }

        } elseif ($kind -eq $TK::NewlineToken -or $kind -eq $TK::LineContinuation) {
            [void]$sb.Append("`n")
            $atLineStart = $true

        } elseif ($kind -eq $TK::LCurly -or $kind -eq $TK::AtCurly) {
            if ($atLineStart) { [void]$sb.Append($indent); $atLineStart = $false }
            [void]$sb.Append($gap + $tokText)
            $depth++
            $indent = $IndentString * $depth
            [void]$sb.Append("`n")
            $atLineStart = $true

        } elseif ($kind -eq $TK::RCurly) {
            $depth  = [Math]::Max(0, $depth - 1)
            $indent = $IndentString * $depth
            if (-not $atLineStart) { [void]$sb.Append("`n") }
            [void]$sb.Append("$indent}")
            [void]$sb.Append("`n")
            $atLineStart = $true

        } else {
            if ($atLineStart) { [void]$sb.Append($indent); $atLineStart = $false }
            [void]$sb.Append($gap + $tokText)
        }

        $prevEnd = $tokEnd
    }

    $out = $sb.ToString().TrimEnd("`r", "`n", ' ', "`t") + "`n"
    [System.IO.File]::WriteAllText($OutputPath, $out)
    return @{
        input_bytes  = $raw.Length
        output_bytes = $out.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Fold constant arithmetic + char-code strings
#   (strategies: opaque arithmetic junk; [char][int]C + [char][int]C + ...)
# ---------------------------------------------------------------------------

# Flatten a left-associated '+' tree into its ordered leaf terms.
function Get-PlusTerms($binAst, $terms) {
    foreach ($side in @($binAst.Left, $binAst.Right)) {
        if ($side -is [System.Management.Automation.Language.BinaryExpressionAst] -and
            $side.Operator.ToString() -eq 'Plus') {
            Get-PlusTerms $side $terms
        } else {
            [void]$terms.Add($side)
        }
    }
}

# Format a resolved numeric value as a PowerShell literal (invariant culture). Negative values are
# parenthesized -> (-11) so the literal can never abut a preceding operator (e.g. 4-(7-18) must
# become 4-(-11), never 4--11 which lexes as the decrement operator). '(-N)' has no inner
# arithmetic BinaryExpression, so Invoke-PsFoldArithmetic never re-folds it (loop terminates).
function Format-NumLiteral($val) {
    if ($null -eq $val) { return '0' }
    $s = $null
    if ($val -is [System.IFormattable]) {
        try { $s = ([System.IFormattable]$val).ToString($null, [System.Globalization.CultureInfo]::InvariantCulture) } catch {}
    }
    if ($null -eq $s) { $s = [string]$val }
    if ($s.StartsWith('-')) { return "($s)" }
    return $s
}

# Atomic pass: collapse constant arithmetic (sub)expressions to their numeric literal.
# Replaces the maximal enclosing arithmetic expression that Resolve-Const evaluates to a
# number, so e.g. (18+18-(13-17)) -> 40 and 5*(2-7) -> -25. Does NOT resolve variables; it
# composes after Invoke-PsInlineConstants has inlined any single-assignment constants.
function Invoke-PsFoldArithmetic([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    $arithOps = @('Plus','Minus','Multiply','Divide','Rem')
    $folded   = 0
    $changed  = $true

    while ($changed) {
        $changed = $false
        $tok = $null; $err = $null
        $ast  = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$err)
        $reps = [System.Collections.Generic.List[pscustomobject]]::new()
        $seen = [System.Collections.Generic.HashSet[string]]::new()
        # Memoize resolutions for this parse so the maximal-subtree climb re-uses subtree results.
        $cache = New-Object 'System.Collections.Generic.Dictionary[System.Management.Automation.Language.Ast,object]'

        foreach ($bin in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator.ToString() -in @('Plus','Minus','Multiply','Divide','Rem') }, $true)) {
            $rv = Resolve-Const $bin $true $emptySet $emptySet $null $cache
            if (-not $rv.Known) { continue }
            $v = $rv.Value
            if ($v -is [string] -or $v -is [bool] -or $v -is [char]) { continue }   # numeric only

            # Climb to the maximal enclosing expression that still resolves to a *number*
            # (through parens, casts, unary/binary ops), so we only ever replace the largest
            # numeric subtree once — maximal subtrees never overlap. Stops at a cast that yields
            # a non-number (e.g. [char](..)), leaving that for Invoke-PsFoldCharConcat.
            # Inside "( .. )" PowerShell wraps the child as ParenExpr -> Pipeline ->
            # CommandExpression -> <expr>, so skip those wrappers to reach the enclosing paren.
            $top = $bin
            $p   = $bin.Parent
            while ($true) {
                while ($null -ne $p -and ($p -is [System.Management.Automation.Language.PipelineAst] -or
                                          $p -is [System.Management.Automation.Language.CommandExpressionAst])) { $p = $p.Parent }
                if ($p -isnot [System.Management.Automation.Language.ExpressionAst]) { break }
                $pv = Resolve-Const $p $true $emptySet $emptySet $null $cache
                if ($pv.Known -and $pv.Value -isnot [string] -and $pv.Value -isnot [bool] -and $pv.Value -isnot [char]) { $top = $p; $p = $p.Parent }
                else { break }
            }
            $key = "$($top.Extent.StartOffset):$($top.Extent.EndOffset)"
            if (-not $seen.Add($key)) { continue }

            $tv  = Resolve-Const $top $true $emptySet $emptySet $null $cache
            $lit = Format-NumLiteral $tv.Value
            if ($lit -eq $top.Extent.Text) { continue }   # no-op guard (prevents infinite loop)
            $reps.Add([pscustomobject]@{ S=$top.Extent.StartOffset; E=$top.Extent.EndOffset; T=$lit })
        }

        if ($reps.Count -gt 0) {
            foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
                $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
            }
            $folded  += $reps.Count
            $changed  = $true
        }
    }

    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $folded
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# Atomic pass: fold char-code concatenations into a string literal.
# Collapses outermost '+' chains that contain at least one [char] cast when every term is a
# resolvable constant (e.g. [char]72 + [char]105 -> 'Hi'). Resolves no variables of its own; it
# composes after constants have been inlined by other helpers.
function Invoke-PsFoldCharConcat([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)

    $tok = $null; $err = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$err)
    $reps   = [System.Collections.Generic.List[pscustomobject]]::new()
    $folded = 0

    foreach ($bin in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator.ToString() -eq 'Plus' }, $true)) {
        $par = $bin.Parent
        if ($par -is [System.Management.Automation.Language.BinaryExpressionAst] -and $par.Operator.ToString() -eq 'Plus') { continue }  # not outermost

        $terms = [System.Collections.Generic.List[object]]::new()
        Get-PlusTerms $bin $terms

        $sb = [System.Text.StringBuilder]::new()
        $allKnown = $true; $hasCharTerm = $false
        foreach ($t in $terms) {
            $rv = Resolve-Const $t $true $emptySet $emptySet   # strict, no var map: constants only
            if (-not $rv.Known -or $null -eq $rv.Value) { $allKnown = $false; break }
            $val = $rv.Value
            # Char-term detection is value-based, not AST-shape based: a term like ([char]80) is a
            # ParenExpressionAst wrapping the [char] cast, so matching ConvertExpressionAst on the raw
            # term misses every parenthesized/$(...)-wrapped char (the shape real obfuscators emit).
            if     ($val -is [char])   { $hasCharTerm = $true; [void]$sb.Append([char]$val) }
            elseif ($val -is [string]) { [void]$sb.Append($val) }
            elseif ($val -is [int] -or $val -is [long] -or $val -is [byte] -or $val -is [double]) {
                try { [void]$sb.Append([char][int]$val) } catch { $allKnown = $false; break }
            } else { $allKnown = $false; break }
        }
        if (-not $allKnown -or -not $hasCharTerm) { continue }

        $escaped = $sb.ToString() -replace "'", "''"
        $reps.Add([pscustomobject]@{ S=$bin.Extent.StartOffset; E=$bin.Extent.EndOffset; T="'$escaped'" })
        $folded++
    }

    if ($reps.Count -gt 0) {
        foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
            $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
        }
    }
    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed      = $folded
        input_bytes  = $inputLen
        output_bytes = $text.Length
        output_path  = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Decode numeric byte/int array literal payloads (strategy: [Byte[]]$x=1,2,3)
# ---------------------------------------------------------------------------

# Extract a byte[] from an expression that is an all-integer array literal (0-255),
# unwrapping [type](...) casts and parentheses. Returns $null if the shape doesn't match.
function Get-ByteArrayLiteral($expr) {
    $e = $expr
    while ($true) {
        if ($e -is [System.Management.Automation.Language.ParenExpressionAst]) {
            $pl = $e.Pipeline
            if ($pl -is [System.Management.Automation.Language.PipelineAst] -and
                $pl.PipelineElements.Count -eq 1 -and
                $pl.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                $e = $pl.PipelineElements[0].Expression; continue
            }
            return $null
        }
        if ($e -is [System.Management.Automation.Language.ConvertExpressionAst]) { $e = $e.Child; continue }
        break
    }
    if ($e -isnot [System.Management.Automation.Language.ArrayLiteralAst]) { return $null }
    $bytes = [System.Collections.Generic.List[byte]]::new()
    foreach ($el in $e.Elements) {
        if ($el -isnot [System.Management.Automation.Language.ConstantExpressionAst]) { return $null }
        $v = $el.Value
        if ($v -isnot [int] -and $v -isnot [long] -and $v -isnot [byte] -and $v -isnot [double]) { return $null }
        $iv = [int]$v
        if ($iv -lt 0 -or $iv -gt 255) { return $null }
        $bytes.Add([byte]$iv)
    }
    return $bytes.ToArray()
}

function Invoke-PsDecodeByteArray([string]$InputPath, [string]$OutputPath, [int]$MinLength = 8) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $tok = $null; $err = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$err)

    $reps    = [System.Collections.Generic.List[pscustomobject]]::new()
    $decoded = 0

    foreach ($asn in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($asn.Operator.ToString() -ne 'Equals') { continue }
        $rhs = $asn.Right; $expr = $null
        if ($rhs -is [System.Management.Automation.Language.PipelineAst] -and
            $rhs.PipelineElements.Count -eq 1 -and
            $rhs.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $expr = $rhs.PipelineElements[0].Expression
        } elseif ($rhs -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $expr = $rhs.Expression
        }
        if ($null -eq $expr) { continue }

        $bytes = Get-ByteArrayLiteral $expr
        if ($null -eq $bytes -or $bytes.Length -lt $MinLength) { continue }

        # Inline the array as a decoded string literal if printable, else as a hex byte[].
        # This is atomic: it only unwraps the numeric-array layer. Any base64/compression layer
        # underneath is left for the next helper in the chain (e.g. Invoke-PsInlineBase64).
        $decodedStr  = [System.Text.Encoding]::UTF8.GetString($bytes)
        $isPrintable = ($decodedStr -notmatch '[\x00-\x08\x0b\x0c\x0e-\x1f]')
        if ($isPrintable) {
            $escaped = $decodedStr -replace "'", "''"
            $reps.Add([pscustomobject]@{ S=$expr.Extent.StartOffset; E=$expr.Extent.EndOffset; T="'$escaped'" })
        } else {
            $hexArr = ($bytes | ForEach-Object { '0x{0:x2}' -f $_ }) -join ','
            $reps.Add([pscustomobject]@{ S=$expr.Extent.StartOffset; E=$expr.Extent.EndOffset; T="([byte[]]($hexArr))" })
        }
        $decoded++
    }

    foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
        $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
    }
    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed        = $decoded
        arrays_decoded = $decoded
        input_bytes    = $inputLen
        output_bytes   = $text.Length
        output_path    = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Resolve reflection member/type names (strategy: ($v -as [Type]).($m))
# ---------------------------------------------------------------------------

function Invoke-PsResolveReflection([string]$InputPath, [string]$OutputPath) {
    $text     = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $text.Length
    $emptySet = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    $tok = $null; $err = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$err)

    # --- Build name -> constant-string map (vars assigned one consistent string literal) ---
    $strMap   = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([System.StringComparer]::OrdinalIgnoreCase)
    $conflict = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($asn in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($asn.Operator.ToString() -ne 'Equals') { continue }
        $lhs = $asn.Left
        if ($lhs -isnot [System.Management.Automation.Language.VariableExpressionAst]) { continue }
        $name = $lhs.VariablePath.UserPath.ToLowerInvariant()
        if ($name -match ':') { continue }
        $rhs = $asn.Right; $expr = $null
        if ($rhs -is [System.Management.Automation.Language.PipelineAst] -and
            $rhs.PipelineElements.Count -eq 1 -and
            $rhs.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $expr = $rhs.PipelineElements[0].Expression
        } elseif ($rhs -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $expr = $rhs.Expression
        }
        $rv = Resolve-Const $expr $true $emptySet $emptySet
        if (-not $rv.Known -or $rv.Value -isnot [string]) { $null = $conflict.Add($name); continue }
        if ($strMap.ContainsKey($name) -and $strMap[$name] -ne $rv.Value) { $null = $conflict.Add($name); continue }
        $strMap[$name] = $rv.Value
    }
    foreach ($c in $conflict) { [void]$strMap.Remove($c) }

    $reps = [System.Collections.Generic.List[pscustomobject]]::new()
    $membersInlined = 0
    $typesResolved  = 0

    # --- Rewrite .($m)/::($m) member access where $m is a constant identifier string ---
    foreach ($mem in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true)) {
        $member = $mem.Member
        if ($member -isnot [System.Management.Automation.Language.VariableExpressionAst] -and
            $member -isnot [System.Management.Automation.Language.ParenExpressionAst] -and
            $member -isnot [System.Management.Automation.Language.SubExpressionAst]) { continue }
        $rv = Resolve-Const $member $true $emptySet $emptySet $strMap
        if (-not $rv.Known -or $rv.Value -isnot [string]) { continue }
        if ($rv.Value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $reps.Add([pscustomobject]@{ S=$member.Extent.StartOffset; E=$member.Extent.EndOffset; T=$rv.Value })
        $membersInlined++
    }

    # --- Rewrite ($v -as [Type]) where $v is a constant type-name string ---
    # When the cast target is literally [Type]/[type]/[System.Type] and the resolved name is a
    # safe bracket-able identifier, canonicalize the whole cast to [TypeName]; otherwise fall
    # back to substituting the literal string for the left operand (old behavior).
    foreach ($bin in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator.ToString() -eq 'As' }, $true)) {
        $left = $bin.Left
        if ($left -isnot [System.Management.Automation.Language.VariableExpressionAst] -and
            $left -isnot [System.Management.Automation.Language.ParenExpressionAst] -and
            $left -isnot [System.Management.Automation.Language.SubExpressionAst] -and
            $left -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
        $rv = Resolve-Const $left $true $emptySet $emptySet $strMap
        if (-not $rv.Known -or $rv.Value -isnot [string]) { continue }

        $isTypeCast = $bin.Right -is [System.Management.Automation.Language.TypeExpressionAst] -and
                      $bin.Right.TypeName.FullName -in @('Type', 'System.Type')
        if ($isTypeCast -and $rv.Value -match '^[A-Za-z_][A-Za-z0-9_.]*$') {
            $reps.Add([pscustomobject]@{ S=$bin.Extent.StartOffset; E=$bin.Extent.EndOffset; T="[$($rv.Value)]" })
        } elseif ($left -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
            # $left is already a literal and isn't a safe [Type] cast to canonicalize -- nothing
            # to rewrite, so skip rather than queuing a no-op replacement.
            $escaped = $rv.Value -replace "'", "''"
            $reps.Add([pscustomobject]@{ S=$left.Extent.StartOffset; E=$left.Extent.EndOffset; T="'$escaped'" })
        } else {
            continue
        }
        $typesResolved++
    }

    if ($reps.Count -gt 0) {
        foreach ($r in ($reps | Sort-Object { $_.S } -Descending)) {
            $text = $text.Remove($r.S, $r.E - $r.S).Insert($r.S, $r.T)
        }
    }
    [System.IO.File]::WriteAllText($OutputPath, $text)
    return @{
        changed         = $membersInlined + $typesResolved
        members_inlined = $membersInlined
        types_resolved  = $typesResolved
        input_bytes     = $inputLen
        output_bytes    = $text.Length
        output_path     = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Control-flow-flattening dispatcher unflattening
#
# Targets the common obfuscation idiom:
#     $state = <literal>
#     while ($state <cmp> <sentinel>) { switch ($state) { <lit> { ...; $state = <lit-or-cond> } ... } }
#
# Resolves the dispatcher by abstract interpretation of the small set of scalar
# variables that gate state transitions (constant propagation over an explicit
# ConstVars map, reusing Resolve-Const), emitting each visited case's real
# statements in true execution order and dropping the dispatcher bookkeeping.
# Bails out (leaves that loop untouched) rather than guessing whenever a guard
# or transition cannot be proven constant, or the shape doesn't match — e.g. a
# case that advances state based on decrypted network content genuinely cannot
# be resolved without executing the payload, and this pass must say so rather
# than silently emit wrong output. Never executes the target script.
# ---------------------------------------------------------------------------

function Test-CffTruthy($v) {
    if ($v -is [bool]) { return $v }
    if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [float]) { return $v -ne 0 }
    if ($v -is [string]) { return $v.Length -gt 0 }
    return ($null -ne $v)
}

function Get-CffPrecedingStatement($node) {
    $parent = $node.Parent
    $stmts = $null
    if ($parent -is [System.Management.Automation.Language.NamedBlockAst]) { $stmts = $parent.Statements }
    elseif ($parent -is [System.Management.Automation.Language.StatementBlockAst]) { $stmts = $parent.Statements }
    else { return $null }
    $idx = -1
    for ($i = 0; $i -lt $stmts.Count; $i++) {
        if ([object]::ReferenceEquals($stmts[$i], $node)) { $idx = $i; break }
    }
    if ($idx -le 0) { return $null }
    return $stmts[$idx - 1]
}

# Distinct lowercased variable names read anywhere inside $exprAst (excludes $true/$false/$null).
function Get-CffReferencedVarNames($exprAst) {
    $names = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $exprAst) { return $names }
    foreach ($v in $exprAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        $n2 = Get-VarName $v
        if ($n2 -eq 'true' -or $n2 -eq 'false' -or $n2 -eq 'null') { continue }
        $names.Add($n2)
    }
    return @($names | Select-Object -Unique)
}

# A helper variable is only safe to simulate locally if the WHOLE file never assigns it
# outside the dispatcher loop's own extent — otherwise its value entering/during the loop
# could be influenced by code this pass never sees, and any "resolved" transition built on
# it would be unsound. Checked lazily per variable name and cached by the caller.
function Test-CffVarAssignedOutsideRange([string]$nameLower, $wholeAst, [int]$rangeStart, [int]$rangeEnd) {
    foreach ($a in $wholeAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and (Get-VarName $a.Left) -eq $nameLower) {
            $s = $a.Extent.StartOffset
            if ($s -lt $rangeStart -or $s -ge $rangeEnd) { return $true }
        }
    }
    foreach ($u in $wholeAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.UnaryExpressionAst] }, $true)) {
        $tk = $u.TokenKind.ToString()
        if (($tk -eq 'PlusPlus' -or $tk -eq 'MinusMinus') -and $u.Child -is [System.Management.Automation.Language.VariableExpressionAst] -and
            (Get-VarName $u.Child) -eq $nameLower) {
            $s = $u.Extent.StartOffset
            if ($s -lt $rangeStart -or $s -ge $rangeEnd) { return $true }
        }
    }
    return $false
}

# Abstractly executes one case body given the current (mutated-in-place) ConstVars/UnknownVars
# state, returning @{ Bail; Reason; Found; NextState }. Recurses into if/else to follow the
# single resolvable branch; treats every other statement type (commands, function defs,
# try/catch, ...) as opaque and keeps it verbatim, provided it never assigns the dispatcher
# variable internally (checked, to avoid silently missing a transition hidden in a construct
# this pass doesn't model).
function Resolve-CffCaseTransition(
    $Statements, [string]$DispatcherVarLower, $ConstVars, $UnknownVars,
    [bool]$HasStrictMode, $ReservedVars, $EmitExtents,
    $WholeAst, [int]$LoopStart, [int]$LoopEnd, $ConfinedCache
) {
    $nextState = $null
    $found = $false
    foreach ($stmt in $Statements) {
        if ($stmt -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $stmt.Operator.ToString() -eq 'Equals' -and
            $stmt.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {

            $name = Get-VarName $stmt.Left
            $rhsExpr = $null
            if ($stmt.Right -is [System.Management.Automation.Language.PipelineAst]) {
                if ($stmt.Right.PipelineElements.Count -eq 1 -and
                    $stmt.Right.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                    $rhsExpr = $stmt.Right.PipelineElements[0].Expression
                }
            } elseif ($stmt.Right -is [System.Management.Automation.Language.CommandExpressionAst]) {
                $rhsExpr = $stmt.Right.Expression
            }

            if ($null -eq $rhsExpr) {
                if ($name -eq $DispatcherVarLower) {
                    return [pscustomobject]@{ Bail=$true; Reason="dispatcher assignment RHS shape not supported at offset $($stmt.Extent.StartOffset)"; Found=$false; NextState=$null }
                }
                [void]$UnknownVars.Add($name); $ConstVars.Remove($name)
                $EmitExtents.Add([pscustomobject]@{ Start=$stmt.Extent.StartOffset; End=$stmt.Extent.EndOffset })
                continue
            }

            foreach ($refName in (Get-CffReferencedVarNames $rhsExpr)) {
                if ($refName -eq $DispatcherVarLower) { continue }
                if (-not $ConfinedCache.ContainsKey($refName)) {
                    $ConfinedCache[$refName] = -not (Test-CffVarAssignedOutsideRange $refName $WholeAst $LoopStart $LoopEnd)
                }
                if (-not $ConfinedCache[$refName]) {
                    return [pscustomobject]@{ Bail=$true; Reason="variable `$$refName is assigned outside the dispatcher loop -- cannot safely simulate"; Found=$false; NextState=$null }
                }
            }

            $r = Resolve-Const $rhsExpr $HasStrictMode $ReservedVars $UnknownVars $ConstVars
            if ($name -eq $DispatcherVarLower) {
                if (-not $r.Known) {
                    return [pscustomobject]@{ Bail=$true; Reason="dispatcher set to a non-constant value at offset $($stmt.Extent.StartOffset)"; Found=$false; NextState=$null }
                }
                $nextState = $r.Value
                $found = $true
                continue   # control-flow bookkeeping -- dropped from output
            }
            if ($r.Known) { $ConstVars[$name] = $r.Value; [void]$UnknownVars.Remove($name) }
            else { [void]$UnknownVars.Add($name); $ConstVars.Remove($name) }
            $EmitExtents.Add([pscustomobject]@{ Start=$stmt.Extent.StartOffset; End=$stmt.Extent.EndOffset })
            continue
        }
        elseif ($stmt -is [System.Management.Automation.Language.IfStatementAst]) {
            $takenStatements = $null
            $matched = $false
            foreach ($clause in $stmt.Clauses) {
                $cond = Get-CondExpr $clause.Item1
                if ($null -eq $cond) {
                    return [pscustomobject]@{ Bail=$true; Reason="if-condition shape not supported at offset $($stmt.Extent.StartOffset)"; Found=$false; NextState=$null }
                }
                foreach ($refName in (Get-CffReferencedVarNames $cond)) {
                    if ($refName -eq $DispatcherVarLower) { continue }
                    if (-not $ConfinedCache.ContainsKey($refName)) {
                        $ConfinedCache[$refName] = -not (Test-CffVarAssignedOutsideRange $refName $WholeAst $LoopStart $LoopEnd)
                    }
                    if (-not $ConfinedCache[$refName]) {
                        return [pscustomobject]@{ Bail=$true; Reason="variable `$$refName is assigned outside the dispatcher loop -- cannot safely simulate"; Found=$false; NextState=$null }
                    }
                }
                $cr = Resolve-Const $cond $HasStrictMode $ReservedVars $UnknownVars $ConstVars
                if (-not $cr.Known) {
                    return [pscustomobject]@{ Bail=$true; Reason="if-condition not statically resolvable at offset $($stmt.Extent.StartOffset)"; Found=$false; NextState=$null }
                }
                if (Test-CffTruthy $cr.Value) { $takenStatements = $clause.Item2.Statements; $matched = $true; break }
            }
            if (-not $matched) {
                if ($null -ne $stmt.ElseClause) { $takenStatements = $stmt.ElseClause.Statements } else { $takenStatements = @() }
            }
            $sub = Resolve-CffCaseTransition $takenStatements $DispatcherVarLower $ConstVars $UnknownVars $HasStrictMode $ReservedVars $EmitExtents $WholeAst $LoopStart $LoopEnd $ConfinedCache
            if ($sub.Bail) { return $sub }
            if ($sub.Found) { $nextState = $sub.NextState; $found = $true }
            continue
        }
        elseif ($stmt -is [System.Management.Automation.Language.BreakStatementAst] -or
                $stmt -is [System.Management.Automation.Language.ContinueStatementAst]) {
            # A bare break/continue changes meaning once the switch/while is gone -- refuse
            # to guess rather than silently drop or mis-emit it.
            return [pscustomobject]@{ Bail=$true; Reason="case body contains a bare break/continue -- ambiguous under flattening"; Found=$false; NextState=$null }
        }
        else {
            $innerDispatcherAssigns = $stmt.FindAll({ param($n)
                $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                (Get-VarName $n.Left) -eq $DispatcherVarLower
            }, $true)
            if ($innerDispatcherAssigns.Count -gt 0) {
                return [pscustomobject]@{ Bail=$true; Reason="dispatcher variable assigned inside an unsupported statement shape ($($stmt.GetType().Name)) at offset $($stmt.Extent.StartOffset)"; Found=$false; NextState=$null }
            }
            $EmitExtents.Add([pscustomobject]@{ Start=$stmt.Extent.StartOffset; End=$stmt.Extent.EndOffset })
            continue
        }
    }
    return [pscustomobject]@{ Bail=$false; Reason=$null; Found=$found; NextState=$nextState }
}

function Invoke-PsUnflattenSwitch([string]$InputPath, [string]$OutputPath, [int]$MaxSteps = 5000) {
    $raw      = [System.IO.File]::ReadAllText($InputPath)
    $inputLen = $raw.Length
    $tokens   = $null; $errors = $null
    $ast      = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    $hasStrictMode = ($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.GetCommandName() -eq 'Set-StrictMode' }, $true)).Count -gt 0

    $reservedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('_','error','?','lastexitcode','matches','args','input','pscmdlet','psitem',
      'true','false','null','pid','pwd','home','host','ofs','psscriptroot',
      'pscommandpath','executioncontext','nestedpromptlevel','shellid') |
      ForEach-Object { [void]$reservedVars.Add($_) }

    $emptySet     = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    $edits        = [System.Collections.Generic.List[pscustomobject]]::new()
    $loopReports  = [System.Collections.Generic.List[pscustomobject]]::new()
    $flattenedCount = 0

    $whileCandidates = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.WhileStatementAst] }, $true))

    foreach ($whileAst in $whileCandidates) {
        $bodyStmts = @($whileAst.Body.Statements)
        if ($bodyStmts.Count -ne 1 -or $bodyStmts[0] -isnot [System.Management.Automation.Language.SwitchStatementAst]) { continue }
        $switchAst = $bodyStmts[0]

        try {
            $flags = $switchAst.Flags
            $badFlags = (($flags -band [System.Management.Automation.Language.SwitchFlags]::Wildcard) -ne 0) -or
                        (($flags -band [System.Management.Automation.Language.SwitchFlags]::Regex) -ne 0) -or
                        (($flags -band [System.Management.Automation.Language.SwitchFlags]::File) -ne 0)
        } catch { $badFlags = $true }
        if ($badFlags) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='switch uses Wildcard/Regex/File matching, not exact-literal dispatch'; Start=$whileAst.Extent.StartOffset })
            continue
        }

        $switchCond = Get-CondExpr $switchAst.Condition
        if ($switchCond -isnot [System.Management.Automation.Language.VariableExpressionAst]) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='switch does not dispatch on a bare variable'; Start=$whileAst.Extent.StartOffset })
            continue
        }
        $dispatcherVarLower = Get-VarName $switchCond

        $whileCond = Get-CondExpr $whileAst.Condition
        if ($null -eq $whileCond -or -not ((Get-CffReferencedVarNames $whileCond) -contains $dispatcherVarLower)) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='while-condition shape not supported / does not reference the switch variable'; Start=$whileAst.Extent.StartOffset })
            continue
        }

        $initStmt = Get-CffPrecedingStatement $whileAst
        if ($null -eq $initStmt -or
            -not ($initStmt -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                  $initStmt.Operator.ToString() -eq 'Equals' -and
                  $initStmt.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                  (Get-VarName $initStmt.Left) -eq $dispatcherVarLower)) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='no adjacent literal initializer found for the dispatcher variable'; Start=$whileAst.Extent.StartOffset })
            continue
        }
        $initRhsExpr = $null
        if ($initStmt.Right -is [System.Management.Automation.Language.PipelineAst]) {
            if ($initStmt.Right.PipelineElements.Count -eq 1 -and
                $initStmt.Right.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                $initRhsExpr = $initStmt.Right.PipelineElements[0].Expression
            }
        } elseif ($initStmt.Right -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $initRhsExpr = $initStmt.Right.Expression
        }
        $initR = if ($null -ne $initRhsExpr) { Resolve-Const $initRhsExpr $hasStrictMode $reservedVars $emptySet } else { @{ Known = $false } }
        if (-not $initR.Known) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='dispatcher initializer is not a constant literal'; Start=$whileAst.Extent.StartOffset })
            continue
        }

        $caseMap = @{}
        $badCase = $false
        foreach ($clause in $switchAst.Clauses) {
            $lr = Resolve-Const $clause.Item1 $hasStrictMode $reservedVars $emptySet
            if (-not $lr.Known) { $badCase = $true; break }
            $caseMap["$($lr.Value)"] = $clause.Item2
        }
        if ($badCase) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason='a case label is not a constant literal (e.g. a scriptblock clause)'; Start=$whileAst.Extent.StartOffset })
            continue
        }
        $defaultBody = $switchAst.Default

        $constVars     = @{}
        $unknownVars   = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
        $confinedCache = @{}
        $emitExtents   = [System.Collections.Generic.List[pscustomobject]]::new()
        $visitCounts   = @{}
        $loopStart     = $initStmt.Extent.StartOffset
        $loopEnd       = $whileAst.Extent.EndOffset

        $currentState = $initR.Value
        $constVars[$dispatcherVarLower] = $currentState
        $bailReason = $null
        $steps = 0

        while ($true) {
            foreach ($refName in (Get-CffReferencedVarNames $whileCond)) {
                if ($refName -eq $dispatcherVarLower) { continue }
                if (-not $confinedCache.ContainsKey($refName)) {
                    $confinedCache[$refName] = -not (Test-CffVarAssignedOutsideRange $refName $ast $loopStart $loopEnd)
                }
                if (-not $confinedCache[$refName]) { $bailReason = "variable `$$refName referenced in the loop condition is assigned outside the dispatcher loop"; break }
            }
            if ($null -ne $bailReason) { break }

            $condR = Resolve-Const $whileCond $hasStrictMode $reservedVars $unknownVars $constVars
            if (-not $condR.Known) { $bailReason = "loop condition not statically resolvable at state '$currentState'"; break }
            if (-not (Test-CffTruthy $condR.Value)) { break }   # natural termination

            $steps++
            if ($steps -gt $MaxSteps) { $bailReason = "exceeded MaxSteps=$MaxSteps simulated transitions -- possible unbounded dispatcher"; break }

            $key = "$currentState"
            $body = $null
            if ($caseMap.ContainsKey($key)) { $body = $caseMap[$key] }
            elseif ($null -ne $defaultBody) { $body = $defaultBody }
            else { $bailReason = "no case (and no default) matches state '$currentState'"; break }

            $visitCounts[$key] = 1 + $(if ($visitCounts.ContainsKey($key)) { $visitCounts[$key] } else { 0 })

            $walk = Resolve-CffCaseTransition $body.Statements $dispatcherVarLower $constVars $unknownVars $hasStrictMode $reservedVars $emitExtents $ast $loopStart $loopEnd $confinedCache
            if ($walk.Bail) { $bailReason = $walk.Reason; break }
            if (-not $walk.Found) { $bailReason = "case '$currentState' never assigns the dispatcher variable"; break }

            $currentState = $walk.NextState
            $constVars[$dispatcherVarLower] = $currentState
        }

        if ($null -ne $bailReason) {
            $loopReports.Add([pscustomobject]@{ Flattened=$false; Reason=$bailReason; Start=$whileAst.Extent.StartOffset })
            continue
        }

        $deadCases = @($caseMap.Keys | Where-Object { -not $visitCounts.ContainsKey($_) })
        $bodyText  = ($emitExtents | ForEach-Object { $raw.Substring($_.Start, $_.End - $_.Start) }) -join "`n"
        $edits.Add([pscustomobject]@{ Start = $loopStart; End = $loopEnd; Text = $bodyText })
        $flattenedCount++
        $loopReports.Add([pscustomobject]@{
            Flattened         = $true
            DispatcherVar     = $dispatcherVarLower
            TotalCases        = $caseMap.Keys.Count
            StatesVisited     = $visitCounts.Count
            StepsSimulated    = $steps
            StatementsEmitted = $emitExtents.Count
            DeadCases         = $deadCases
            Start             = $whileAst.Extent.StartOffset
        })
    }

    $sb  = [System.Text.StringBuilder]::new([int]($raw.Length * 1.1))
    $pos = 0
    foreach ($e in ($edits | Sort-Object Start)) {
        if ($e.Start -lt $pos) { continue }
        [void]$sb.Append($raw, $pos, $e.Start - $pos)
        [void]$sb.Append($e.Text)
        $pos = $e.End
    }
    [void]$sb.Append($raw, $pos, $raw.Length - $pos)
    $out = $sb.ToString()
    [System.IO.File]::WriteAllText($OutputPath, $out)

    return @{
        changed         = $flattenedCount
        loops_found     = $whileCandidates.Count
        loops_flattened = $flattenedCount
        loops_skipped   = @($loopReports | Where-Object { -not $_.Flattened }).Count
        details         = $loopReports
        input_bytes     = $inputLen
        output_bytes    = $out.Length
        output_path     = $OutputPath
    }
}

# ---------------------------------------------------------------------------
# Pass: Unwrap opaque-predicate ifs whose condition is statically TRUE
# ---------------------------------------------------------------------------

# Mirror of Test-FalsyConst, inverted: is this expression provably truthy?
# Unknown/complex values (arrays, objects, unresolved vars) conservatively return $false
# (i.e. "don't touch it"), matching the rest of the library's don't-touch-what-we-can't-prove style.
function Test-TruthyConst($exprAst, [bool]$hasStrictMode, $reservedVars, $assignedVars) {
    $r = Resolve-Const $exprAst $hasStrictMode $reservedVars $assignedVars
    if (-not $r.Known) { return $false }
    $v = $r.Value
    if ($null -eq $v)                                                            { return $false }
    if ($v -is [bool])                                                           { return $v }
    if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [float])   { return $v -ne 0 }
    if ($v -is [string])                                                         { return $v.Length -ne 0 }
    return $false
}

function Invoke-PsUnwrapTrueIf([string]$InputPath, [string]$OutputPath) {
    $raw    = [System.IO.File]::ReadAllText($InputPath)
    $tokens = $null; $errors = $null
    $ast    = [System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)

    $reservedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    @('_','error','?','lastexitcode','matches','args','input','pscmdlet','psitem',
      'true','false','null','pid','pwd','home','host','ofs','psscriptroot',
      'pscommandpath','executioncontext','nestedpromptlevel','shellid') |
      ForEach-Object { [void]$reservedVars.Add($_) }
    $assignedVars = [System.Collections.Generic.HashSet[string]]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($a in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            [void]$assignedVars.Add($a.Left.VariablePath.UserPath.ToLowerInvariant())
        }
    }

    # Only unwrap the simple, unambiguous case: a single-clause if (no elseif, no else) whose
    # condition is statically true. elseif/else chains are left alone — reordering/dropping
    # branches there risks changing semantics, which this pass deliberately avoids.
    $reps = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($i in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true)) {
        if ($i.Clauses.Count -ne 1) { continue }
        if ($null -ne $i.ElseClause) { continue }
        $cond = Get-CondExpr $i.Clauses[0].Item1
        if ($null -eq $cond) { continue }
        if (-not (Test-TruthyConst $cond $false $reservedVars $assignedVars)) { continue }

        $body  = $i.Clauses[0].Item2
        $stmts = $body.Statements
        $text  = ''
        if ($stmts.Count -gt 0) {
            $bs   = $stmts[0].Extent.StartOffset
            $be   = $stmts[$stmts.Count - 1].Extent.EndOffset
            $text = $raw.Substring($bs, $be - $bs)
        }
        $reps.Add([pscustomobject]@{ Start = $i.Extent.StartOffset; End = $i.Extent.EndOffset; Text = $text })
    }

    $out = $raw
    foreach ($r in ($reps | Sort-Object Start -Descending)) {
        $out = $out.Remove($r.Start, $r.End - $r.Start).Insert($r.Start, $r.Text)
    }
    [System.IO.File]::WriteAllText($OutputPath, $out)

    return @{
        changed      = $reps.Count
        input_bytes  = $raw.Length
        output_bytes = $out.Length
        output_path  = $OutputPath
    }
}

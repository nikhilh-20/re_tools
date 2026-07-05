# PowerShell Deobfuscation Toolkit — `TOOLS.md`

A field reference for the 20 utilities in this folder. Each is a thin CLI wrapper over one
`Invoke-Ps*` function in the shared library `_PsDeobLib.ps1`. Every utility targets **one**
obfuscation technique (or performs one supporting cleanup), so you chain them by hand rather than
running a single do-everything tool.

> **Safety — parse-only.** No utility ever *executes* the target script. Every transform is driven
> off the parsed AST and static constant-folding. It is safe to run these against live malware
> samples. (Base64 payloads are only *decoded*, never run.)

---

## Which tool for which obfuscation?

| You see in the file… | Use |
|---|---|
| Identifiers broken by backticks: `` W`r`i`t`e-Output `` | **PsStrip-Backticks** |
| A fake "instruction pointer" driving a switch: `$s=0; while($s -ne -1){switch($s){...}}` | **PsUnflatten-Switch** |
| Strings built with `+`, `-f`, or `[char[]](…) -join ''`: `'He'+'llo'` | **PsFold-Strings** |
| Strings rebuilt via chained method calls: `'seed'.Remove(a,b).Insert(c,'d')` | **PsFold-MethodChains** |
| Strings rebuilt via `[string]::Concat(...)` / `[string]::Join(sep,...)` | **PsFold-StaticStringCalls** |
| Numbers spelled as arithmetic junk: `(18+18-(13-17))+32` | **PsFold-Arithmetic** |
| Strings built from char codes: `[char]72 + [char]105` | **PsFold-CharConcat** |
| Strings built from a joined array: `@('ab','cd') -join ''`, `-join @($('ab'),$('cd'))`, or `$x=@();$x+=…;$x -join ''` | **PsFold-ArrayJoins** |
| Payload as a numeric array: `[Byte[]]$x = 72,101,108,…` | **PsDecode-ByteArray** |
| Base64 literal: `[Convert]::FromBase64String("…")` | **PsInline-Base64** |
| A constant assigned once, then referenced: `$a='calc.exe'; … $a` | **PsInline-Constants** |
| One variable **reused** to hold a different constant before each call | **PsPropagate-Constants** |
| Dynamic API/type resolution: `($v -as [Type]).($m)` | **PsResolve-Reflection** |
| Opaque-predicate dead branches, junk loops / stores / functions | **PsRemove-DeadCode** |
| Lots of blank or `;`-only filler lines | **PsCollapse-BlankLines** |
| Whole script packed onto one `;`-separated line | **PsExpand-Semicolons** |
| Comment banners / marker lines to drop | **PsStrip-Lines** |
| Need a map of variables → decoded values → sinks | **PsExtract-Variables** |
| Want to apply readable names to garbage identifiers | **PsRename-Variables** |
| Want to see what an `iex` / `-EncodedCommand` actually runs | **PsAnnotate-Iex** |

### Recommended chain (re-run to a fixpoint)

If the sample uses control-flow flattening (a `while`/`switch` dispatcher), run
**PsUnflatten-Switch** first — everything downstream assumes real, in-order statements. If it uses
chained-method or static-call string rebuilding, alternate these to a fixpoint next:

```
FoldMethodChains → FoldStaticStringCalls → FoldStrings
```

Each can unblock the other — a `.Replace()` chain's receiver may be a `-f`/`+`/`-join` expression
only `PsFold-Strings` folds, and a `[string]::Concat(...)` argument may be an instance-method chain
only `PsFold-MethodChains` folds — so re-run the three together until none of them reports any
further folds.

Then:

```
RemoveDeadCode → PropagateConstants → FoldArithmetic → FoldCharConcat → ResolveReflection
→ CollapseBlankLines → DecodeByteArray → InlineBase64
```

Run the block, and repeat it until every pass reports `changed:0`. Each pass exposes constants the
next one needs (e.g. propagation reveals char codes, char-concat folds them into names,
reflection resolves the API those names feed). Insert **Strip-Backticks** first if the sample uses
backtick escaping, and **Expand-Semicolons** first if it is a single packed line.

### Invocation convention

Every wrapper takes `-InputFile` and `-OutputFile` and prints a compact JSON stats line
(`changed`, `input_bytes`, `output_bytes`, `output_path`), except where noted below. On failure a
wrapper writes `ERROR: <message>` into the output file instead of transforming it.

**PsFold-MethodChains** and **PsFold-StaticStringCalls** report `resolved`/`skipped`/`by_reason`
instead of `changed`. **PsUnflatten-Switch** reports `changed`/`loops_found`/`loops_flattened`/
`loops_skipped`/`details` (each `details[]` entry has `Flattened`/`Reason`/`Start`, or — on
success — `DispatcherVar`/`TotalCases`/`StatesVisited`/`StepsSimulated`/`StatementsEmitted`/
`DeadCases`/`Start`).

```powershell
.\PsFold-CharConcat.ps1 -InputFile .\sample.ps1 -OutputFile .\sample.step1.ps1
```

Exceptions to the two-argument convention:
- **PsUnflatten-Switch** — also `-MaxSteps <int>` (optional, default `5000`).
- **PsInline-Constants** — also `-MaxUses <int>` (mandatory; `0`/negative = unlimited).
- **PsDecode-ByteArray** — also `-MinLength <int>` (optional, default `8`).
- **PsStrip-Lines** — also `-Pattern <regex> -Flags <ims>` (both mandatory).
- **PsExpand-Semicolons** — also `-IndentString <string>` (optional, default 4 spaces).
- **PsRename-Variables** — also `-RenamesFile <json>` (mandatory).
- **PsExtract-Variables** — takes **only** `-InputFile`; prints a JSON *report* to stdout and writes no output file.

---

# Obfuscation-defeating passes

## PsStrip-Backticks

### Description
Two AST-aware, semantics-preserving cleanups:
1. **Un-splits identifiers** — removes an inline backtick that sits **between two letters** and
   **outside** every string / comment, the classic `` W`r`i`t`e-Output `` trick that hides command
   and API names from signature scanners.
2. **Decodes non-interpolated double-quoted strings** — a `"..."` string with no `$`-interpolation
   has a constant value, so its backtick escapes (`` `n ``, `` `t ``, `` `" `` …) are expanded and
   the string is re-emitted as a single-quoted literal. This turns e.g. an embedded VBS blob written
   with `` `n `` into a readable multi-line literal without changing what it evaluates to.

Backticks that carry meaning are left untouched: real escapes inside a string are decoded (not
deleted) by pass 2, and interpolated strings (`"...$x..."`, `"...$(...)..."`) and single-quoted
strings are skipped entirely.

### Examples
Input:
```powershell
[Convert]::Fr`omBase64Str`ing($x)
I`E`X $payload
$v = "Dim x`nSet x = CreateObject(`"WScript.Shell`")"
```
Output (`changed:5`):
```powershell
[Convert]::FromBase64String($x)
IEX $payload
$v = 'Dim x
Set x = CreateObject("WScript.Shell")'
```

### How it works
The script is parsed to an AST. Pass 1 matches `` `(?<=[A-Za-z])`(?=[A-Za-z]) `` and keeps only the
backticks that fall outside every string-literal / here-string / comment span. Pass 2 selects each
`StringConstantExpressionAst` of type `DoubleQuoted` whose extent contains a backtick and replaces it
with `'` + its parser-decoded `.Value` (single quotes doubled) + `'`. Both edit kinds are applied in
one right-to-left pass so offsets stay valid; they are disjoint by construction (pass 1 edits only
*outside* strings, pass 2 replaces whole string extents). Interpolated strings parse as
`ExpandableStringExpressionAst` and are never decoded — a missed deobfuscation, never a corruption.
The stats report `backticks_removed` and `strings_decoded` alongside the combined `changed`.

---

## PsUnflatten-Switch

### Description
Reconstructs real control flow from **control-flow-flattening (CFF)**: the
`$state = <literal>; while ($state <cmp> <sentinel>) { switch ($state) { <lit> { ...; $state =
<lit-or-cond> } ... } }` idiom that hides a straight-line (or small looping) program behind a fake
"instruction pointer" and non-sequential case numbers. Resolves the dispatcher via abstract
interpretation — constant propagation over the small set of scalar variables that gate state
transitions — and replaces the whole `while`/`switch` with the real statements in true execution
order, dropping every case that's never actually visited.

This is a structural/state-machine technique, fundamentally different from the string/constant
folds elsewhere in this toolkit, and comes with a hard safety valve: it only ever flattens a
dispatcher when every transition can be *proven* constant; any loop where that can't be shown is
left **completely untouched**, with a diagnostic reason, rather than guessed.

### Examples
Input:
```powershell
$state = 0
while ($state -ne -1) {
    switch ($state) {
        0 { Write-Host 'start'; $state = 1 }
        1 { Write-Host 'end'; $state = -1 }
    }
}
```
Output (`loops_found:1`, `loops_flattened:1`):
```powershell
Write-Host 'start'
Write-Host 'end'
```

### How it works
It finds `WhileStatementAst` nodes whose entire body is one `SwitchStatementAst` (exact-literal
dispatch only — no wildcard/regex clauses) keyed on a bare variable that also appears in the
`while` condition, with a literal initializer immediately before the loop. It then simulates
execution: starting at the initial state, it resolves the `while` condition and the current case's
body (reusing the shared `Resolve-Const`, tracking any `if`/`else` branch taken), appending every
non-bookkeeping statement to the output and advancing the dispatcher variable, until the loop
condition resolves false or `-MaxSteps` (default `5000`) is exceeded. Before trusting any helper
variable's value it checks that the variable is never assigned **outside** the dispatcher loop's
own source range — if it is, the loop is left untouched, since its value could depend on code the
pass can't see. Any non-constant guard, non-literal case label, unsupported statement shape, or
bare `break`/`continue` inside a case body (ambiguous once flattened) also bails that specific
loop, with a `Reason` string in the JSON `details[]` array. A successfully flattened loop's report
includes `StatesVisited`, `StepsSimulated`, `StatementsEmitted`, and `DeadCases` (case labels the
simulation never actually reached). Never executes the target script.

---

## PsFold-Strings

### Description
Folds compile-time-constant **string** expressions into a single literal. Handles three shapes:
string concatenation with `+`, the `-f` format operator, and `[char[]](…) -join ''`. Use it to
collapse split-string obfuscation like `'He'+'ll'+'o'`.

### Examples
Input:
```powershell
$a = 'Hello' + ', ' + 'World'
$b = 'a{0}c{1}' -f 'B', 'D'
```
Output (`changed:2`):
```powershell
$a = 'Hello, World'
$b = 'aBcD'
```

### How it works
It parses the file, finds binary expressions whose operands all resolve to constants, evaluates
them the way PowerShell would (`+` concatenation, `.NET` `String.Format` for `-f`), and replaces
the whole expression with the resulting single-quoted literal. It loops until nothing more folds,
so nested concatenations (`'a'+'b'+'c'`) collapse in one run.

---

## PsFold-MethodChains

### Description
Folds compile-time-constant chains of pure **String instance methods** into a single literal —
the `'seed'.Remove(1,2).Insert(0,'x').Replace('a','b')`-style rebuild obfuscators use instead of
(or alongside) `+`/`-f` concatenation. Only a fixed allowlist of pure, deterministic,
scalar-in/scalar-out methods is evaluated: `Remove`, `Insert`, `Replace`, `Substring`, `PadLeft`,
`PadRight`, `ToUpper`, `ToLower`, `Trim`, `TrimStart`, `TrimEnd`. Static calls (`[string]::Concat`,
`[string]::Join`) are a distinct technique — see **PsFold-StaticStringCalls**.

### Examples
Input:
```powershell
$a = 'abcdef'.Remove(0,2).Insert(0,'XY')
$b = 'abcdef'.Split(',')
```
Output (`resolved:1`, `skipped:1`, `by_reason:"1x method not allowlisted: Split"`):
```powershell
$a = 'XYcdef'
$b = 'abcdef'.Split(',')
```

### How it works
It walks every `InvokeMemberExpressionAst` in the AST and resolves it through the shared
`Resolve-Const`, which recognizes instance calls against the allowlist above: the receiver and
every argument must themselves resolve to a constant scalar (string/int/long/char), the invocation
is evaluated in-process inside a `try/catch` so any arity/out-of-range error bails rather than
guesses, and static calls are always rejected here (that's a separate pass). It loops to a
fixpoint — nested chains resolve from the innermost call outward, keeping only the widest
non-overlapping replacement per overlap cluster — then reports any unresolved top-level chain with
a specific reason: `static call (out of scope)`, `method name not resolvable`, `method not
allowlisted: X`, `receiver not constant`, `argument not constant`, or `arity/bounds error during
invocation`. Stats fields: `resolved`, `skipped`, `by_reason`, `input_bytes`, `output_bytes`,
`output_path` — note this pass reports `resolved`/`skipped`, not `changed`.

---

## PsFold-StaticStringCalls

### Description
Folds the static-dispatch counterpart of the above: `[string]::Concat(...)` and
`[string]::Join(sep, ...)` calls whose arguments are all constant. Kept as its own utility rather
than folded into **PsFold-MethodChains** — static-call resolution is treated as a distinct
technique from instance-method-chain folding under this toolkit's one-strategy-per-utility
convention, even though both compute the same class of result.

### Examples
Input:
```powershell
$a = [string]::Concat('ab', 'cd', [string]::Concat('ef','gh'))
$b = [string]::Format('{0}', 'x')
```
Output (`resolved:1`, `skipped:1`, `by_reason:"1x method not allowlisted: Format"`):
```powershell
$a = 'abcdefgh'
$b = [string]::Format('{0}', 'x')
```

### How it works
Only `[string]::Concat`/`[string]::Join` are targeted, matched case-insensitively against the
`[string]`/`[System.String]` type. Because the shared `Resolve-Const` always rejects static calls
(by design — it stays a strategy-agnostic primitive), this pass carries its own small recursive
resolver that recurses directly into nested static calls (`Concat(Concat(...), ...)`) and falls
back to `Resolve-Const` for any non-static argument (literals, `$(...)`-wrapped values,
already-foldable instance-method chains). It loops to a fixpoint the same way
**PsFold-MethodChains** does, and reports the same style of `resolved`/`skipped`/`by_reason`
stats, with reasons: `target type not resolvable`, `target type not allowlisted: X`, `method name
not resolvable`, `method not allowlisted: X`, `argument not constant`, `arity/bounds error during
invocation`.

---

## PsFold-Arithmetic

### Description
Collapses constant **arithmetic** (`+ - * / %`, plus unary sign) into its numeric literal. Defeats
"data obfuscation" where a byte value or length is spelled as junk math like `(18+18-(13-17))+32`.
It only touches numbers — a subtree that casts to `[char]` is left for **PsFold-CharConcat**.

### Examples
Input:
```powershell
$x = (18+18-(13-17))+32
$y = 5*(2-7)
```
Output (`changed:2`):
```powershell
$x = 72
$y = (-25)
```

### How it works
For each arithmetic expression it climbs to the *largest* enclosing subtree that still evaluates to
a number, then replaces it once with the computed value. Negative results are parenthesised as
`(-25)` so the literal can never fuse with a preceding operator into `--` (the decrement operator)
and so it can't be re-folded. It repeats until stable.

---

## PsFold-CharConcat

### Description
Folds character-code concatenation chains into a string literal — the `[char]72 + [char]105`
technique for hiding API and type names. A chain qualifies only if **at least one** term is a
`[char]` cast (so pure-number sums stay with Fold-Arithmetic and pure-string sums with
Fold-Strings).

### Examples
Input:
```powershell
$s = [char]72 + [char]105 + [char]33
```
Output (`changed:1`):
```powershell
$s = 'Hi!'
```

### How it works
It finds each outermost `+` chain, resolves every term to a constant (chars, strings, and numbers —
numbers become their `[char]` code point), concatenates them, and replaces the chain with the
single-quoted result. It resolves no variables itself, so run **PsInline-Constants** /
**PsPropagate-Constants** first if the codes are held in variables.

---

## PsFold-ArrayJoins

### Description
Folds string arrays that are assembled with `-join`. Handles the **inline** binary form
`@('a','b',…) -join 'sep'`, the **inline unary** form `-join @('a','b',…)` (prefix operator, empty
separator), and the **accumulator** idiom `$v=@(); $v+='a'; $v+='b'; … $v -join 'sep'`. Array
elements need only be constant-*foldable* — bare literals, `$('x')`/`('x')` subexpression wrappers,
`+`-concatenations, and pure string-method chains all resolve. Malware uses this to keep a long
base64 blob as many small quoted fragments, and wraps individual literals in `-join @($('x'))` as a
no-op specifically to defeat receiver-must-be-constant folds like **PsFold-MethodChains**.

### Examples
Input:
```powershell
$p = @('ab','cd','ef') -join ''
$q = @(); $q += 'foo'; $q += 'bar'; $r = $q -join '-'
$s = (-join @($('Sys'),$('tem'))).Replace('y','Y')
```
Output (`changed:3`):
```powershell
$p = 'abcdef'
$r = 'foo-bar'
$s = 'System'.Replace('y','Y')     # receiver now literal -> PsFold-MethodChains folds it next
```

### How it works
For inline joins (binary or unary) it collects the constant array elements via `Resolve-Const`,
joins them with the (constant) separator, and replaces the whole expression. The unary form is a
distinct AST node type from the binary form, so their edit ranges never collide; nested joins fold
innermost-first, one layer per fixpoint iteration. For the accumulator form it gathers the ordered
`+=` fragments, folds the join at the read site, and — **only when the join is the sole reader** —
deletes the now-redundant `$q=@()`/`$q+=…` assignments. Run **PsFold-Strings** first if the fragments
themselves are still `+`-concatenations, and re-run **PsFold-MethodChains** afterwards to collapse any
`.Replace(…)` chains whose receiver this pass just materialised into a literal.

---

## PsDecode-ByteArray

### Description
Decodes a numeric byte/int array literal (`[Byte[]]$x = 72,101,108,…`) into text. If the decoded
bytes are printable it inlines a string literal; if not, it rewrites them as a compact
`([byte[]](0x..,0x..,…))` so the next layer stays valid. It is deliberately one layer only — if the
text turns out to be base64, hand it to **PsInline-Base64** next.

### Examples
Input:
```powershell
[Byte[]]$data = 72,101,108,108,111,32,87,111,114,108,100
```
Output (`changed:1`, `arrays_decoded:1`):
```powershell
[Byte[]]$data = 'Hello World'
```

### How it works
It looks for `=` assignments whose right side is an all-integer array (0–255, unwrapping `[type](…)`
casts and parens) of at least `-MinLength` elements (default 8), UTF-8-decodes the bytes, and
inlines the result. The length gate avoids mangling short, legitimate arrays.

---

## PsInline-Base64

### Description
Decodes inline base64 literals. Recognises both `[Convert]::FromBase64String("…")` and the wrapped
`[Encoding]::UTF8.GetString([Convert]::FromBase64String("…"))`, replacing the call with the decoded
string (or a `[byte[]]` literal if the bytes aren't printable text).

### Examples
Input:
```powershell
$s = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('SGVsbG8gV29ybGQh'))
$raw = [Convert]::FromBase64String('YWJj')
```
Output (`changed:2`):
```powershell
$s = 'Hello World!'
$raw = 'abc'
```

### How it works
It finds the `FromBase64String` / `GetString(FromBase64String(...))` call shapes where the argument
is a **string literal**, decodes them at parse time, and inlines the result. The argument must
already be a literal, so fold split/joined strings first (**PsFold-Strings** /
**PsFold-ArrayJoins**) if the base64 is still assembled from fragments.

---

## PsInline-Constants

### Description
Inlines variables that are **assigned exactly once** with a constant, then removes the now-dead
assignment. This is the simple "single static assignment" case (e.g. `$a='calc.exe'` used later as
`$a`). For a variable that is reassigned many times, use **PsPropagate-Constants** instead.

### Examples
Command: `-MaxUses 0` (unlimited use sites)
Input:
```powershell
$path = 'calc.exe'
Start-Process $path
```
Output (`changed:1`, `assignments_removed:1`):
```powershell

Start-Process 'calc.exe'
```
(The assignment line is deleted; a blank line remains — clean it up later with
**PsCollapse-BlankLines**.)

### How it works
It keeps only variables with a single `=` assignment at unconditional top level whose right side is
a pure constant (no command calls), then substitutes every read with the literal and drops the
assignment. `-MaxUses` caps how many read-sites qualify (`0` = unlimited; e.g. `-MaxUses 1` inlines
only single-use variables). Splatted (`@var`) and `Get-Variable`/`Set-Variable`-referenced names are
left alone for safety.

---

## PsPropagate-Constants

### Description
Flow-sensitive constant propagation — the general form of Inline-Constants for a variable name that
is **reused** to hold a different constant before each use. Obfuscators exploit single-assignment
inlining's blind spot by writing `$m = <chain>; …($m)…; $m = <chain>; …($m)…`. This pass walks the
script in source order, tracking each variable's *current* value and substituting per site. It also
correctly treats variables seeded only inside **opaque-predicate dead branches** as never-assigned
(`$null` → `0`).

### Examples
Input:
```powershell
if ((-40) -ge 55) { $seed = 152; $pad = 7 }
$m = [char](72 + $seed) + [char](105 + $seed)
Invoke-Expression $m
$m = [char]66 + [char]121
Invoke-Expression $m
```
Output (`changed:4`, `folded_assignments:2`, `substituted_reads:2`):
```powershell
if ((-40) -ge 55) { $seed = 152; $pad = 7 }
$m = 'Hi'
Invoke-Expression 'Hi'
$m = 'By'
Invoke-Expression 'By'
```
`$seed` is assigned only inside the always-false `if`, so it is treated as `0`; `$m` resolves to
`'Hi'` at the first call and `'By'` at the second.

### How it works
Walking the top-level statements in order, it holds a per-variable value environment with three
states — *known*, *not-yet-assigned* (→ `$null`/`0`), and *unknown* (assigned but unresolvable). It
folds each resolvable `=` RHS to a literal and rewrites downstream reads with the value in force at
that position. Anything assigned inside a loop / `if` / `try` / `switch` / function or via
`+=`/`++`/`--` is moved to *unknown* afterward, so stale values never leak forward.

---

## PsResolve-Reflection

### Description
Resolves dynamic API/type resolution — the `($v -as [Type]).($m)` / `::($m)` pattern
(MITRE ATT&CK **T1027.007**) — back to literal type and member names, when `$v` and `$m` hold a
single consistent constant string. This exposes the reflective call that the obfuscator hid.

### Examples
Input:
```powershell
$t = 'System.Math'
$m = 'Abs'
($t -as [Type])::($m)
```
Output (`changed:2`, `types_resolved:1`, `members_inlined:1`):
```powershell
$t = 'System.Math'
$m = 'Abs'
[System.Math]::Abs
```

### How it works
It first builds a map of variables assigned one consistent constant string, then (a) rewrites
`.($m)` / `::($m)` member accesses to the bare identifier when it resolves to a valid name, and
(b) rewrites `($v -as [Type])` to the bracket type-accelerator form `[TypeName]` when the resolved
name is a safe identifier and the cast target is literally `[Type]`; otherwise it falls back to
substituting the literal type-name string for `$v`. It inlines at the call site and leaves the
original assignments in place. Feed it resolved constants first (**PsPropagate-Constants** / the
fold passes) so the type/member variables are known.

---

## PsRemove-DeadCode

### Description
AST liveness analysis that strips filler the obfuscator adds to bury the payload: dead stores
(assignments to variables never read), unreachable loops (`while($false)`) and opaque-predicate
`if`s (`if((5-5)-gt 0)`), unreferenced function definitions, and pure result-discarded statements.
It deliberately **preserves** constant/string literal assignments (they are often the real payload).

### Examples
Input:
```powershell
$junk = (100 - 100) * 42
if ((7 -lt 3)) { Invoke-WebRequest 'http://never' }
while ($false) { $z = $z + 1 }
Write-Host 'real payload'
```
Output (`changed:4` — `1x unreachable loop, 1x dead if, 2x dead store`):
```powershell


Write-Host 'real payload'
```
(Two blank lines remain where the removed statements were — follow with **PsCollapse-BlankLines**.)

### How it works
It parses the script, builds a variable read/write graph, and works a queue: a store whose target is
never read (outside dead regions) is removed, which can make *its* inputs dead too, and so on to a
fixpoint. Loops/ifs with statically-false conditions are dropped outright. Crucially, a loop whose
result is **captured** by an enclosing `$(...)`/`@(...)` (e.g. an XOR-decrypt loop feeding a payload)
is recognised as live and never removed. Constant/number/string RHS assignments are kept via the
built-in `PreserveStringLiterals` guard.

---

# Supporting utilities

## PsCollapse-BlankLines

### Description
Cosmetic cleanup: removes lines that are only whitespace or stray semicolons, and collapses runs of
3+ blank lines down to a single blank line. Run it after the fold/removal passes, which tend to
leave empty lines behind.

### Examples
Input (`$a = 1`, several blank and `;`-only lines, then `$b = 2`):
```powershell
$a = 1




;
   ;  ;



$b = 2
```
Output (`changed:3`):
```powershell
$a = 1

$b = 2
```

### How it works
Two regex stages over the raw text: first delete whitespace/semicolon-only lines, then squeeze
blank-line runs to one. Blank runs *inside* string literals are protected by a parsed string-range
guard, so here-strings and multi-line strings are never altered.

---

## PsExpand-Semicolons

### Description
Turns a semicolon-packed one-liner into readable, one-statement-per-line code with brace-depth
indentation. This is usually the **first** thing you run on a minified single-line dropper.

### Examples
Input:
```powershell
$a=1; $b=2; if($a){Write-Host $b}
```
Output:
```powershell
$a=1
$b=2
if($a){
    Write-Host $b
}
```

### How it works
It walks the **token stream** (not raw text), emitting a newline at each statement-terminating
semicolon or line break and increasing indent inside `{ … }`. Because it works on tokens,
semicolons *inside strings* are never mistaken for statement separators. Indent width is
configurable via `-IndentString`.

---

## PsStrip-Lines

### Description
Removes every line matching a regex you supply. General-purpose scalpel for deleting comment
banners, marker lines, or any recognizable filler the other passes don't target.

### Examples
Command: `-Pattern '^\s*#' -Flags 'i'`
Input:
```powershell
# junk comment banner
$a = 1
#REM another
Write-Host $a
```
Output (`removed_lines:2`, `kept_lines:2`):
```powershell
$a = 1
Write-Host $a
```

### How it works
Line-by-line regex filter. `-Flags` combines the usual switches — `i` (ignore case), `m`
(multiline), `s` (singleline/dotall). Only whole lines that match are dropped; everything else is
preserved verbatim.

---

## PsExtract-Variables

### Description
**Analysis only — writes no output file.** Emits a JSON report describing every variable: where it
is assigned, a decoded preview of its value (auto base64-decoding string literals), whether its
value flows into an execution **sink** (`iex`, `Invoke-Expression`, `Invoke-Command`, `Add-Type`,
`& $var`, `.Invoke()`), and a suggested human-readable name. Use it to understand a sample and to
plan a rename map.

### Examples
Command: `-InputFile .\sample.ps1` (report printed to stdout)
Input:
```powershell
$enc = 'SGVsbG8gV29ybGQh'
$plain = [Convert]::FromBase64String($enc)
iex $plain
```
Output (JSON, abridged):
```json
{
  "total_count": 2,
  "variables": [
    { "user_path": "enc",   "decoded_preview": "Hello World!", "reaches_sink": false, "suggested_name": "enc" },
    { "user_path": "plain", "decoded_preview": null,           "reaches_sink": true,  "suggested_name": "plain" }
  ]
}
```
Here `$enc` decodes to `Hello World!`, and `$plain` is flagged `reaches_sink:true` because it is
passed to `iex`.

### How it works
It parses the script, groups all assignment and read sites per variable, previews values (folding
constants and base64-decoding likely-base64 literals), and marks a variable `reaches_sink` when any
of its reads falls inside an execution-sink expression. The `suggested_name` heuristic proposes
names like `c2Url`, `c2Ip`, `dropPath`, or `b64PartN` from the decoded content.

---

## PsRename-Variables

### Description
Applies a `old → new` rename map (supplied as JSON) to **all** occurrences of each variable. Pair it
with **PsExtract-Variables** (whose report gives you names and suggestions) to turn garbage
identifiers into meaningful ones.

### Examples
`renames.json`:
```json
{"ab3xk":"c2Url"}
```
Command: `-RenamesFile .\renames.json`
Input:
```powershell
$aB3xk = 'http://evil.example/c2'
Invoke-WebRequest $aB3xk
```
Output (`renamed:1`, 2 occurrences):
```powershell
$c2Url = 'http://evil.example/c2'
Invoke-WebRequest $c2Url
```

### How it works
Map keys are **lowercased** variable names (no `$`); values are the new names. It rewrites every
`VariableExpressionAst` whose name is in the map, at both assignment and read sites, and reports the
occurrence count per renamed variable. Names not in the map are untouched.

---

## PsAnnotate-Iex

### Description
Non-destructive: leaves the code intact and **appends decoded payloads as comments** so you can read
what an `iex`/`Invoke-Expression` string literal or a `-EncodedCommand <base64>` would execute,
without running anything. Handy as a final documentation pass.

### Examples
Input:
```powershell
iex 'Write-Host 42'
powershell -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAaABpAC4A…
```
Output (`changed:1`):
```powershell
iex 'Write-Host 42'
# <<<IEX PAYLOAD BEGIN>>>
# > Write-Host 42
# <<<IEX PAYLOAD END>>>
powershell -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAaABpAC4A…
```

### How it works
It finds `iex`/`Invoke-Expression` calls with a literal string argument and `-EncodedCommand`
arguments with a base64 literal (decoded as UTF-16LE, PowerShell's `-EncodedCommand` format), then
inserts the decoded text as a `# > …` comment block after the command. The original line is never
modified, so annotation is always safe to apply.

---

## Notes & gotchas

- **Chain, then re-run.** Most passes expose constants the next pass needs. Loop the recommended
  chain until every stage reports `changed:0`.
- **Fold before you decode.** Base64/byte-array/join passes need their argument to already be a
  single literal — run the string/char/arithmetic folds first.
- **Blank lines are expected.** Removal/inline passes delete statements but leave the newline;
  finish with **PsCollapse-BlankLines**.
- **Most things are static — but CFF dispatchers can now be reconstructed too, within limits.**
  **PsUnflatten-Switch** resolves state-machine `while`/`switch` dispatchers when every transition
  is provably constant; a dispatcher whose next state genuinely depends on runtime data (decrypted
  network content, `Get-Random`, environment variables) is a real static-analysis ceiling and is
  left untouched (with a diagnostic) rather than guessed — the string/byte-array payload layer
  inside each case is still recovered regardless once the dispatcher is unflattened.

# VBScript Deobfuscation Toolkit

A field reference for the 19 utilities in this folder. Each is a thin CLI wrapper
over shared library code in `vbsdeoblib/`. Every utility targets **one** obfuscation
technique (or one supporting cleanup), so you chain them by hand.

> **Safety — parse-only.** No utility ever *executes* the target script. Every
> transform is driven by static analysis and constant folding. Safe to run against
> live malware samples.

---

## Shared library (`vbsdeoblib/`)

| Module | Purpose |
|---|---|
| `tokenizer.py` | Hand-rolled VBScript lexer — STRING, NUMBER, IDENT, COMMENT, LINECONT, NEWLINE, COLON, OP, WS, UNKNOWN tokens. String/comment-aware: a `'` or `&` inside a string literal is never mistaken for code. |
| `resolver.py` | `resolve_const(tokens, env, user_fns)` — shared constant evaluator. Recursive-descent over VBScript expressions: literals, parens, the full operator set (`& + - * / \ Mod ^`, unary `-`, comparisons `= <> < > <= >=`, and the bitwise/logical operators `And Or Xor Not Eqv Imp`), and an allowlist of pure builtins (Chr, Replace, Mid, Left, Right, UCase, LCase, Trim, Asc, Len, Space, String, …). Returns `None` on anything unrecognised — callers leave those expressions untouched. |
| `statements.py` | `split_statements(tokens)` — splits a token stream into logical statement spans, joining `_` line continuations and splitting on `:` outside strings/parens. Also `find_block_end(stmts, open_i)` / `opens_block` / `closes_block` — generic For/Do/While/If/Select/With/Function/Sub/Class/Property block matcher, tracking nested blocks of any kind. |
| `io.py` | `run_tool(fn, …)` — CLI harness: `--input FILE --output FILE [--aggressive]`, reads source, calls `fn(text, **opts) -> (new_text, stats)`, writes output, prints compact JSON stats to stdout. On error: writes `ERROR: …` to output file. |

---

## Which tool for which obfuscation?

| You see in the file… | Use |
|---|---|
| `Chr(72) & Chr(105) & Chr(33)` | **vbs_fold_chr_calls** |
| Arithmetic junk: `(18*2-(13-17))+32` | **vbs_fold_arithmetic** |
| String fragments joined by `&` or `+`: `"He" & "ll" & "o"` / `"He" + "ll" + "o"` | **vbs_fold_concat** |
| Calls to pure builtins with constant args: `Replace("W@S","@","")`, `Mid("abc",2,1)` | **vbs_fold_builtin_calls** |
| `Split("a,b,c", ",")` with constant args | **vbs_fold_split_calls** |
| `accum = ""` / `For i = 0 To UBound(arr)` / `accum = accum & arr(i)` / `Next` over a literal `Array(...)` | **vbs_fold_array_join_loops** |
| Any `For`/`Do While`/`Do Until`/`While...Wend` loop with a straight-line body over already-constant inputs — rolling-XOR decode loops, custom-alphabet substitution, LCG key schedules, multi-variable byte-decode pipelines, ... | **vbs_fold_constant_loops** |
| User-defined single-expression wrapper function: `Function ttaffRy(s): ttaffRy = Replace(s,"@","")` | **vbs_inline_functions** |
| Side-effecting accumulator sink: `Function Sink(c): buf = buf & c : End Function` fed by a long run of `Call Sink("chunk")` | **vbs_inline_sink_calls** |
| A variable assigned a constant once, then referenced many times | **vbs_propagate_constants** |
| `If (5 > 3) Then ... End If` always-true wrapper | **vbs_unwrap_trueif** |
| Dead assignments (incl. a var reassigned N times in a row before first use) / always-false `If`/`Do While` blocks / empty-body anti-sandbox loops | **vbs_remove_deadcode** |
| Character harvested from a string via `InStr`+`Mid` instead of a literal: `p = InStr(1,S,"s") : c = Mid(S,p,1)` | **vbs_fold_instr_mid** |
| `Execute "<constant statement>"` hiding an ordinary line from string scanners | **vbs_unwrap_execute** |
| Multiple statements packed on one line with `:` | **vbs_expand_colons** |
| Runs of blank or whitespace-only lines | **vbs_collapse_blanklines** |
| Comment-only lines | **vbs_strip_comments** |
| Need a map of variables → decoded values → sinks | **vbs_extract_variables** |
| Want to apply readable names to garbage identifiers | **vbs_rename_variables** |
| Want to see what `ExecuteGlobal`/`Execute`/`Eval` actually runs, without modifying code | **vbs_annotate_execute** |

---

## Invocation convention

Every wrapper takes `--input FILE` and `--output FILE` and prints a compact JSON
stats line to stdout. On failure it writes `ERROR: <message>` into the output file.

```
python vbs_fold_chr_calls.py --input sample.vbs --output step1.vbs
```

### Exceptions to the two-argument convention

- **vbs_remove_deadcode** — also `--aggressive` (treats self-referential accumulator
  chains as dead, e.g. repeated `x = x & f()` never read outside its own writer
  statements), `--preserve-strings` (keeps string/number literal RHS assignments
  even when dead, as a safety guard for files not yet run through propagation), and
  `--remove-empty-loops` (removes, rather than just flags, empty-body loops whose
  condition contains no parenthesized call).
- **vbs_strip_comments** — also `--include-trailing` (also strips end-of-line comments).
- **vbs_rename_variables** — also `--renames FILE` (required; JSON mapping old names to new).
- **vbs_extract_variables** — takes **only** `--input`; prints a JSON *report* to stdout and
  writes no output file (analysis-only).
- **vbs_fold_constant_loops** — also `--max-iterations N` (default 5,000,000), a safety
  cap on simulated iterations per loop.

---

## Technique descriptions

### vbs_fold_chr_calls
Folds `Chr(N)` calls where `N` is a constant integer to a single-character string
literal. Repeats to a fixpoint so chained forms collapse in one run.

Input → Output:
```vbs
x = Chr(72) & Chr(101) & Chr(108) & Chr(108) & Chr(111)
```
```vbs
x = "H" & "e" & "l" & "l" & "o"
```
(`changed:5` — follow with **vbs_fold_concat** to collapse the `&` chain.)

### vbs_fold_arithmetic
Folds constant arithmetic (`+ - * / \ Mod ^`, including `&H`/`&O` hex/octal literals)
to a numeric literal. Targets parenthesised sub-expressions.

### vbs_fold_concat
Folds `&`- and `+`-chains of constant terms (string literals, `Chr()` results, numbers
coerced to strings) to a single quoted string literal.  For `+` chains the fold is only
emitted when the resolved value is a string (so `x = 1 + 2` is left for
`vbs_fold_arithmetic`).

Input → Output:
```vbs
v = "htt" & "ps" & "://" & "evil.example"
x = "pow" + "er" + "she" + "ll"
```
```vbs
v = "https://evil.example"
x = "powershell"
```

### vbs_fold_builtin_calls
Evaluates calls to allowlisted pure VBScript builtins with all-constant arguments:
`Chr`, `Asc`, `Len`, `UCase`, `LCase`, `Trim`, `LTrim`, `RTrim`, `CStr`, `CInt`, `CDbl`,
`CBool`, `Hex`, `Oct`, `Abs`, `Int`, `Fix`, `Sqr`, `StrReverse`, `Space`, `Mid`, `Left`,
`Right`, `Replace`, `InStr`, `String`.

Two guards keep this from folding something that only *looks* like the builtin:
a call preceded by `.` (member access, e.g. `oRE.Replace(...)`) is left untouched, and
so is a call to a name the script itself redefines via `Function`/`Sub` (e.g. a
script-defined `Function InStr(a,b)` shadowing the builtin).

`InStr` specifically declines to fold (leaves the call untouched) rather than emit a
wrong value whenever VBScript semantics are ambiguous or would raise at runtime: a
`start` argument `< 1`, or an unsupported `compare` mode other than `0`
(vbBinaryCompare) or `1` (vbTextCompare). Supported cases correctly return `0` for a
zero-length first argument or a `start` past its end, and the `start` position itself
for a zero-length second argument.

### vbs_fold_split_calls
Folds `Split(expression[, delimiter[, limit[, compare]]])` calls to an `Array(...)`
literal when every supplied argument resolves to a constant via the shared resolver.
`Split()` returns an array rather than a scalar, so it doesn't fit `resolve_const`'s
scalar `Const` contract and isn't part of `PURE_BUILTINS` — this tool resolves each
argument independently and computes the real VBScript `Split` result itself. The
replacement `Array(...)` call is a genuine VBScript expression that behaves
identically to the original `Split()` result for every downstream consumer
(`UBound`, indexing, `For ... Next`), so it's a safe drop-in.

Defeats a common string-shattering technique: a payload string is broken apart with a
throwaway delimiter and `Split` back into an array, then reassembled with a loop —
purely to keep the literal string from appearing contiguous to static scanners:
```vbs
prostatotomies = Split("H)))e)))l)))l)))o", ")))")
hills = ""
For i = 0 To UBound(prostatotomies)
    hills = hills & prostatotomies(i)
Next
```
```vbs
prostatotomies = Array("H", "e", "l", "l", "o")
hills = ""
For i = 0 To UBound(prostatotomies)
    hills = hills & prostatotomies(i)
Next
```
(Reassembling the `For` loop into a single string literal is a separate, join-loop
obfuscation pattern this tool does not address.)

**Signature semantics implemented, matching real VBScript `Split` exactly:**

| Case | Result |
|---|---|
| `expression` is `""` | single-element array containing `""` (not a zero-element array) |
| `delimiter` is `""` | single-element array containing the whole `expression` |
| `delimiter` omitted | defaults to `" "` |
| `limit` omitted | defaults to `-1` (no limit) |
| `limit = 0` | zero-element array |
| `limit > 0` | at most `limit` elements; the last one holds the unsplit remainder |
| `compare` omitted | defaults to `0` (`vbBinaryCompare`) |
| `compare = 1` | case-insensitive split (`vbTextCompare`) |
| `compare` resolves to anything else | declines to fold (leaves the call untouched) rather than guess, same convention `vbs_fold_builtin_calls`'s `InStr` folding uses for unsupported compare modes |

Same guards as **vbs_fold_builtin_calls**: a call preceded by `.` (member access,
e.g. `oRE.Split(...)`) and a call to a name the script itself redefines via
`Function`/`Sub` are left untouched. If any *supplied* argument fails to resolve to a
constant (e.g. a variable delimiter that isn't itself known), the whole call is left
untouched.

### vbs_fold_array_join_loops
Folds the other half of the "shatter a string, reassemble it with a loop" idiom that
**vbs_fold_split_calls** produces the first half of. Targets exactly:
```vbs
accum = "<const>"
For idx = <constStart> To UBound(arrName)
accum = accum & arrName(idx)
Next
```
Since `arrName`'s contents and the iteration count are both statically known once
`arrName`'s nearest prior write is a literal `arrName = Array(e0, e1, ...)` with every
element constant, the entire loop's effect is computable ahead of time — the whole
block (initializer through `Next`) collapses to one `accum = "<joined literal>"`.

Input → Output:
```vbs
prostatotomies = Array("H", "e", "l", "l", "o")
hills = ""
For asepta = 0 To UBound(prostatotomies)
hills = hills & prostatotomies(asepta)
Next
```
```vbs
prostatotomies = Array("H", "e", "l", "l", "o")
hills = "Hello"
```
(The now-unused `prostatotomies` array is left in place — that's
**vbs_remove_deadcode**'s job once nothing reads it anymore.)

**Deliberately narrow match, aborts (leaves the loop untouched) on any deviation:**
- The `For` header must be exactly `For idx = startExpr To UBound(arrName)`, optionally
  with `Step stepExpr` — `stepExpr` must resolve to `1`; anything else (a different
  function than `UBound`, extra `UBound` args, arithmetic around it) is rejected.
  `startExpr` must resolve to a constant non-negative integer; the array elements are
  sliced from that index (covers the common `start = 0` case and a "skip the first N"
  variant for free).
- The loop body must be **exactly one statement**: `accum = accum (&|+) arrName(idx)`
  with matching identifiers throughout. A loop with extra statements, or a different
  shape, is left alone.
- `arrName`'s **nearest** prior write (scanning backward) must be a literal
  `Array(...)` call with every element constant — if it was reassigned to anything else
  in between, or never assigned, the fold is declined.
- The **immediately preceding** real statement before the `For` header must be
  `accum = <constExpr>` — required, not defaulted to `""` (unlike
  **vbs_propagate_constants**'s self-append seeding), since a missing initializer more
  plausibly means `accum` already carries some other live value this tool can't see.

Matching is done via `split_statements` + `FOR`/`NEXT` depth tracking (same technique
`vbs_remove_deadcode`'s local dead-store pass uses), not regex — this pattern spans
several statements and needs real block-nesting awareness so an unrelated nested `For`
inside the body is never mistaken for the loop's own closing `Next`.

### vbs_fold_constant_loops
Generalizes the idea **vbs_fold_array_join_loops** already embodies — a loop
over statically-known data is computable ahead of time — to arbitrary bounded
loops (`For`, `Do While`, `Do Until`, `Do...Loop While/Until`, `While...Wend`)
and arbitrary straight-line bodies of plain assignment statements, not just
one accumulator over one `Array()` literal. Simulates the body
statement-by-statement via the shared `resolve_const` evaluator with an
evolving scalar environment — it has no built-in knowledge of any particular
algorithm (alphabet, key schedule, XOR key, ...); those are just data it
evaluates generically, the same way every other fold pass in this toolkit
works from the shared resolver rather than a per-sample pattern.

Defeats rolling-XOR / custom-alphabet decode loops such as:
```vbs
idx = 1 : key = 245
Do Until idx > Len(enc)
    hi  = InStr(alphaA, Mid(enc, idx, 1)) - 1
    lo  = InStr(alphaB, Mid(enc, idx + 1, 1)) - 1
    b   = (hi * 16) Or lo
    b   = b Xor key
    out = out & Chr(b)
    key = (key * 123 + 161) And 255
    idx = idx + 2
Loop
```
```vbs
idx = 1 : key = 245
hi = ...
lo = ...
b = ...
out = "<decoded literal>"
key = <final key>
idx = <final idx>
```

**Deliberately narrow, decline (leave the loop untouched) on any deviation:**
- The loop's termination must be provable via `resolve_const` at every step —
  a condition (or, for `For`, the start/end/step) that fails to resolve
  aborts the fold.
- The body must be **straight-line**: no nested `For`/`Do`/`While`/`If`/
  `Select`/`With`/`Function`/`Sub`/`Class`/`Property`. A loop with a
  conditional or another loop inside its body is left completely untouched
  (v1 scope).
- Every body statement must be a plain `name = expr` (or `Let name = expr`)
  assignment — a `Call`, method/object invocation, `Set`, or anything else
  aborts the fold for that loop.
- Bounded by `--max-iterations` (default 5,000,000) as a safety cap against
  pathological/adversarial loops; exceeding it aborts only that loop's fold,
  not the whole run.
- A variable's value entering the loop is resolved from the nearest preceding
  top-level constant assignment (same convention `vbs_fold_array_join_loops`
  uses for `accum`, generalized to every loop-carried variable) — so this
  tool is designed to run **after** `vbs_fold_builtin_calls`,
  `vbs_propagate_constants`, and `vbs_fold_concat` in the chain (everything
  the loop reads from outside itself should already be a literal), and
  **before** `vbs_remove_deadcode` (which cleans up now-unused inputs, like
  the alphabet/blob strings, once nothing reads them anymore).

Its coverage is a strict superset of `vbs_fold_array_join_loops`'s narrow
`For...UBound(Array(...))` pattern; the two are safe to run in either order
in the same chain (harmless overlap, not a conflict).

### vbs_inline_functions
Inlines user-defined single-expression wrapper functions by parameter substitution at
every call site, then removes the definition. Targets the pattern:
```vbs
Function wrapper(s)
    wrapper = <expression involving s>
End Function
```
After inlining, `wrapper("input")` becomes `(<expression with "input" substituted>)`.
Follow with **vbs_fold_builtin_calls** to fold the materialised call.

### vbs_inline_sink_calls
Materializes calls to a *side-effecting accumulator* Function/Sub as direct
self-append assignments. Targets the call-indirected form of the accumulator idiom —
the payload is built not by `buf = buf & "chunk"` at each site, but by routing every
chunk through a throwaway procedure whose only effect is that same self-append against
a **different** global than its own name:

```vbs
Function Sink(chunk)
    buf = buf & chunk
End Function
Call Sink("first ")
Call Sink("second")
```
```vbs
buf = buf & ("first ")
buf = buf & ("second")
```

Neither neighbouring tool fires on this shape: **vbs_inline_functions** matches only
`FunctionName = <expr>` (a return value, not a side effect), and
**vbs_propagate_constants**' self-append seeding never sees a literal
`buf = buf & "chunk"` to seed because no call site writes `buf` directly.

The rewrite is *semantics-preserving, not a constant fold* — the argument may be any
expression, and interleaved writes to `buf` from elsewhere keep their ordering — so
this runs **first** in a chain, before **vbs_propagate_constants** (seeds `buf` as `""`
and folds the chain), **vbs_fold_concat** (collapses it to one literal), and
**vbs_remove_deadcode** (drops the superseded intermediate stores).

Detection is by shape alone — the sink name, parameter name, and accumulator name are
all discovered, never assumed — so it is not tied to any one sample's identifiers.
Handles `Call Sink(arg)`, bare `Sink(arg)`, bare `Sink arg`, `Sub` as well as
`Function`, `&` or `+`, `ByVal`/`ByRef` parameters, and colon-joined statements.

Both join directions are recognised, since the mirrored form is the same idiom:

| Body | Call site becomes | Chunk order |
|---|---|---|
| `buf = buf & c` (append) | `buf = buf & (arg)` | source order |
| `buf = c & buf` (prepend) | `buf = (arg) & buf` | reversed |

**Declines (leaves the call untouched) on any deviation**, per the toolkit convention:
- Body is anything other than exactly one `G = G (&|+) P` statement, where `P` is one
  of the procedure's own parameters.
- `G` is the procedure's own name (that's **vbs_inline_functions**' pattern) or is
  itself a parameter (not a global accumulator).
- Call-site arity doesn't match the definition's parameter count — emitting
  `buf & ("a", "b")` would be invalid VBScript, so the call is left alone.
- A *discarded* argument (one bound to a parameter the body never appends) isn't
  provably inert — VBScript still evaluates it at the call site, so anything
  call-shaped means decline. Same guard **vbs_remove_deadcode** applies to `ReDim`
  bounds.
- The accumulator name is declared local (`Dim`/`Private`/`Public`/`ReDim`) inside any
  procedure or class body — inlining would retarget a global write to that local.

For a multi-parameter sink the argument is selected **positionally**, matching the
parameter the body actually appends.

### vbs_propagate_constants
Flow-sensitive constant propagation: walks statements in order, tracks each variable's
current constant value, and substitutes downstream reads. Kills a variable's tracked
value when it is assigned a non-constant (e.g. a method call result) or reassigned
inside a block. Always substitutes known constants into an assignment's RHS even when
the LHS is already killed.

**Self-append accumulators**: VBScript's uninitialized `Variant` is `Empty`, which
coerces to `""` in a string context.  When a variable is encountered for the first
time as both LHS and an operand of its own RHS **joined by `&`** (`X = X & "chunk"`,
or the mirrored `X = "chunk" & X`), the tool automatically seeds it as `""` so the
full chain folds.

A maximal run of consecutive top-level statements building one variable this way
(`X = <const>`, then one or more `X = X & <const>` links, with nothing else
between them) is collapsed into a **single** `X = "<final literal>"` edit, rather
than substituting `X`'s ever-growing value into every link individually. The
naive per-link approach makes total edit output grow with the *square* of the
chain length — one obfuscated sample's 1840-link chain would have required
substituting a `"..."` literal spanning the entire accumulated-so-far value at
each of its 1840 reads, ~682 MB of replacement text in total — whereas collapsing
the whole run costs one edit proportional to the *final* value's length,
independent of how many links built it. This also means the idiom now resolves
directly to one literal in this one pass; `vbs_fold_concat` is no longer needed
to finish it off (though it remains useful for other concat patterns this tool
doesn't seed).

Collapsing only applies at the top level (see "Inside a block body" below for why
in-block chains are out of scope) and only extends the run while each link's RHS
is itself constant-foldable — a link referencing something unresolvable ends the
run there, and the valid prefix collapses on its own. A separate absolute size
guard (independent of the tracked-string cap below) stops a *self-squaring* chain
like `X = X & X`, which doubles the accumulator every link regardless of chain
length: exponential growth crosses any bound within a handful of doublings, so
the scan stops almost immediately rather than needing a per-link heuristic.

A collapsed run's resulting value is kept for downstream reads elsewhere in the
file only if it's under a fixed tracked-value-size cap (8192 chars) — otherwise
it's dropped, so a second read of the same variable elsewhere doesn't pay to
re-embed an arbitrarily large literal at that read site too. That same cap also
still fully governs the fallback path for anything that isn't a clean top-level
run: ordinary non-chain assignments, and in-block self-append chains.

The seeding is deliberately restricted to `&`, which is the only VBScript operator
that guarantees string context (it coerces both operands to string, so `Empty` really
does read as `""`). A `+` self-append is never seeded: `+` *adds* when either operand
is numeric, and while `Empty + 2` is `2`, `"" + 2` is a runtime type mismatch — so
seeding a numeric accumulator as `""` would change behaviour rather than reveal it.
Such an accumulator is simply left symbolic.

**Decoy accumulator seeds**: the self-append seeding above only fires on a variable's
*first* assignment, so obfuscators defeat it by seeding the accumulator from a bare name
that is never bound anywhere — `acc = someJunkName` ahead of the chain — which marks
`acc` as "assigned something unresolvable" and blocks every later fold. VBScript without
`Option Explicit` auto-declares any unbound name as an Empty Variant, which reads as `""`
in a string context, so the seed is foldable *provided the name really is never bound*.
That is proven by a single strict criterion: the name occurs **exactly once** in the
whole raw source — the read being folded, and nothing else. One test rules out every
binding route at once, because each needs a second occurrence in the text: an
assignment/`Dim`/`Const`/`ReDim`, being passed to a procedure (VBScript parameters are
**ByRef by default**, so the callee can write back through the argument), a `For`/
`For Each` loop variable, a procedure name or parameter, or an assignment living inside
an `Execute`/`Eval` string (counted, since occurrences are tallied over raw source text
including string and comment content). Reserved words, builtins, and the zero-argument
intrinsics callable with no parentheses (`Rnd`, `Timer`, `Now`, `Err`, `ScriptEngine`,
`GetLocale`, …) are excluded outright — those are calls, not unbound variables, and a
call can legally occur exactly once. The whole fallback is disabled for any file using
`Option Explicit` (an unbound reference is a compile error there, so the fold could never
describe a real execution) or containing `Execute`/`ExecuteGlobal`/`Eval`/`CallByName`/
`GetRef` anywhere — the same dynamic-dispatch guard **vbs_remove_deadcode** already uses.

**Inside a block body**, two regimes apply depending on the kind of every block
currently open. Non-looping blocks (`If`/`Select`/`With`/`Function`/`Sub`/`Class`/
`Property`) execute their body at most once per entry in a fixed order, so a constant
computed partway through is tracked in a scope-local env and folded into later
statements in the *same* straight-line run — cleared the instant any block opens or
closes, so it never leaks across a branch/call boundary. Looping blocks (`For`/`Do`/
`While`) disable local tracking entirely: a value derived from one iteration's inputs
is not generally valid for the next, so every block-depth assignment inside (or nested
inside) a loop is killed exactly as before, never folded.

### vbs_unwrap_trueif
Collapses single-clause `If <always-true> Then … End If` blocks to just the body.
Condition is evaluated via the shared constant resolver.

### vbs_remove_deadcode
Default mode:
- **Local dead-store elimination**: a store to `X` is removed when a later
  unconditional top-level store to `X` exists with no intervening read anywhere
  (any nesting depth) — e.g. a variable reassigned 20+ times in a row purely for
  volume inflation before its first genuine use. Catches what global liveness
  structurally can't, since `X` *is* read eventually, just never from any of the
  intermediate stores. Guarded file-wide against reads inside any
  `Function`/`Sub`/`Class`/`Property` body (no call-graph analysis) and against
  string-literal references (see the dynamic-dispatch guard below).
- **File-global liveness-based dead-store removal**: assignments to variables never
  read anywhere else in the file are deleted. Iterates to a fixpoint — removing one
  dead store can cascade to make its RHS variables dead in the next pass.
  Statement-based (not line-based), so line-continuations and colon-joined
  statements are handled correctly — deleting a dead store never mangles a live
  statement sharing its physical line.
- **Dynamic-dispatch guard**: a name that appears (word-boundary matched) inside a
  string literal is never removed when the file contains `Execute`/
  `ExecuteGlobal`/`Eval`/`CallByName`/`GetRef` anywhere — the tokenizer can't see a
  read that only happens inside a string later passed to one of those. Without this
  guard, a variable referenced only from inside an `Execute "..."` payload gets
  silently deleted, breaking the deobfuscated script.
- **Empty-body loop flagging**: `Do While`/`Until ... Loop`, `While ... Wend`, and
  post-test `Do ... Loop While/Until` with a body containing only blank/comment
  lines (a common anti-sandbox stall, e.g. `Do While f.AtEndOfStream <> True / Loop`
  with nothing advancing the stream) are flagged with a marker comment. Off by
  default for removal — such loops are IOC/TTP evidence worth keeping visible.
- **Declarations** (`Dim`, `ReDim`, `Private`, `Public` — scalar or array): whole
  statement deleted when all declared names are dead; trimmed to keep only live
  names when partially dead. One liveness rule covers all four keywords and both
  shapes — binding a name is a write, not a read, regardless of syntax. `ReDim`
  is the exception requiring its own purity guard: since its bounds are runtime
  expressions (unlike `Dim`'s compile-time-constant bounds), a `ReDim` whose
  bounds look call-like (e.g. `ReDim x(Setup())`) is left untouched entirely
  rather than risk deleting a side effect.
- **Unreferenced `Function`/`Sub` removal**: a definition whose name is never
  called from outside its own body is removed. Same reachability question as
  the assignment/declaration liveness above, so it isn't behind `--aggressive` —
  a function's shape doesn't change what "never read" means.
- Statically-false `If`/`Do While` blocks removed.

`--preserve-strings`: keep string/number literal RHS assignments even when dead
(safety guard for files not yet run through `vbs_propagate_constants`).

`--aggressive`: treats self-referential accumulator chains (e.g. repeated
`x = x & f()`, with `x` never read outside its own writer statements) as dead —
a self-contained cluster that can never be observed once removed. This one
genuinely redefines what counts as live (`x` *is* read, just only by itself), so
unlike the two default-mode passes above, it's opt-in.

`--remove-empty-loops`: also *removes* (rather than just flags) empty-body loops,
but only when the condition contains no parenthesized call (e.g. `f.Read(1)`) — a
loop whose condition might have a side effect is left untouched even with this flag.

### vbs_expand_colons
Splits `:` -separated statements onto individual lines. Token-aware: `:` inside
strings/comments is never mistaken for a separator.

### vbs_collapse_blanklines
Strips whitespace-only lines and squeezes runs of 3+ blank lines to one.

### vbs_strip_comments
Removes comment-only lines. Token-stream driven: `'` or `Rem` inside a string literal
is never mistaken for a comment. `--include-trailing` also strips end-of-line comments.

### vbs_extract_variables
**Analysis only — writes no output file.** Emits a JSON report:
- All assignment sites with decoded preview of the RHS value.
- `reaches_sink`: whether the variable flows into `CreateObject`, `.Run`, `.Open/.Send`,
  `Execute`/`ExecuteGlobal`, `RegWrite`, or `Eval`.
- `suggested_name`: heuristic name suggestion based on the decoded value.

### vbs_rename_variables
Applies a `{old: new}` JSON rename map to all occurrences (case-insensitive).
Pair with **vbs_extract_variables** to plan meaningful names.

### vbs_annotate_execute
Non-destructive: appends the resolved argument of `Execute`/`ExecuteGlobal`/`Eval`
as a block comment immediately after the call. Never executes the payload.

### vbs_fold_instr_mid
Folds the algebraic identity `Mid(S, InStr(n, S, "lit"), Len("lit")) == "lit"` —
*independent of what `S` actually contains*. This defeats a common character-harvesting
trick where a known character is pulled out of some large blob (a system binary, in one
observed sample) via `InStr`+`Mid` instead of ever writing the character as a literal:

```vbs
pos = InStr(1, someBinaryBlob, "s")
ch  = Mid(someBinaryBlob, pos, 1)      ' == "s", when the InStr call succeeds
```

The `InStr` result is usually consumed through an intermediate position variable (as
above) rather than nested directly, so the tool tracks, per variable, the most recent
constant-needle `InStr` call assigned to it, and folds any later `Mid(subject, posvar,
length)` call referencing the same subject with a length equal to `Len(needle)`.

**How this holds up against every documented `InStr` return case:**

| `InStr` outcome | Return value | Effect on the fold |
|---|---|---|
| `string2` found | position `P` of the match | The identity is *definitionally* true here — no assumption about `S`'s content needed. This is the case the fold is built on. |
| `string2` not found | `0` | `Mid(S, 0, …)` is a VBScript runtime error (`start` must be ≥ 1) — the **original** script crashes here. The fold has no way to statically prove this doesn't happen, so its guarantee is conditional: correct *if* the original script reaches this point without crashing. See caveat below. |
| `string1` (subject) empty | `0` | Same consequence and same caveat as "not found" above. |
| `string2` (needle) empty | the `start` position (trivial match) | Would silently break the identity (`Mid(S, start, Len("")) = ""`, not a real extraction) — explicitly rejected: an empty needle never becomes a fold candidate. |
| Either string is `Null` | `Null` | A literal `Null`/`Nothing`/`Empty` needle resolves to `""` and hits the same empty-needle rejection. A genuinely runtime-`Null` variable simply doesn't resolve to a constant at all, so the fold never fires — inert, not wrong. |

**The one real caveat**: the fold is unconditionally safe when `InStr` succeeds (the
common case, and the only case the obfuscation technique is designed around — the
obfuscator picks a needle they expect to be found against a large haystack precisely so
the "harvest" is deterministic). It cannot *prove* success without knowing the subject's
actual runtime content, so on a haystack where the needle genuinely isn't present, the
fold produces the needle text instead of reproducing the original crash. In practice this
is very hard to hit by construction — e.g. against a real `notepad.exe` (~196 KB), common
ASCII needles like `"s"`/`"l"` are found within the first kilobyte, nowhere near "not
found."

Also rejects `vbTextCompare` (case-insensitive `InStr` can make `Mid` return different
casing than the needle), the 2-arg `Mid(s, start)` form (no length — returns everything to
the end of the string, not constrained to the needle), and any case where the subject or
position variable was reassigned between the `InStr` and the `Mid`.

Input → Output:
```vbs
Stbloklandenes = instr(1, Pashka, "s")
kama = mid(Pashka, Stbloklandenes, 1)
```
```vbs
Stbloklandenes = instr(1, Pashka, "s")
kama = "s"
```

### vbs_unwrap_execute
Inlines `Execute "<constant statement>"` when the resolved argument parses as a single
ordinary VBScript statement, hiding an otherwise-plain line from string/method scanners:
```vbs
execute "oRE.Pattern = Intoner"
```
```vbs
oRE.Pattern = Intoner  ' <deobfuscator> unwrapped Execute
```
Never runs the payload — resolution goes through the same shared constant resolver every
other fold pass uses. Deliberately conservative about *when* it fires:
- **`Eval` is never unwrapped** — it's an expression, not a statement (and a peculiar
  one: `Eval("a=1")` is a *comparison* in VBScript, not an assignment), so there is no
  statement form to inline it as.
- **`ExecuteGlobal` is only unwrapped at true module top level.** Inside a procedure it
  assigns to *global* scope; inlining it there would silently turn a global write into
  a local one.
- **The payload must look like a statement**: it either contains a top-level `=`
  (assignment), starts with a statement-leading keyword (`Call`, `Set`, `Dim`, `If`, …),
  or is a bare/member-chain call (`Foo Arg`, `Foo.Bar(Arg)`). A bare expression like
  `Execute "1+1"` is a VBScript runtime error and is left untouched.
- A colon-joined statement following the `Execute` on the same line is preserved
  exactly (the trailing colon is re-emitted); no trailing comment is added in that case,
  since a comment would swallow the rest of the line.

Complements **vbs_annotate_execute** (which never modifies code, only comments) — this
one performs the actual inlining.

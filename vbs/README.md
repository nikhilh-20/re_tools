# VBScript Deobfuscation Toolkit

A field reference for the 16 utilities in this folder. Each is a thin CLI wrapper
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
| `resolver.py` | `resolve_const(tokens, env, user_fns)` — shared constant evaluator. Recursive-descent over VBScript expressions: literals, parens, unary/binary operators, and an allowlist of pure builtins (Chr, Replace, Mid, Left, Right, UCase, LCase, Trim, Asc, Len, Space, String, …). Returns `None` on anything unrecognised — callers leave those expressions untouched. |
| `statements.py` | `split_statements(tokens)` — splits a token stream into logical statement spans, joining `_` line continuations and splitting on `:` outside strings/parens. |
| `io.py` | `run_tool(fn, …)` — CLI harness: `--input FILE --output FILE [--aggressive]`, reads source, calls `fn(text, **opts) -> (new_text, stats)`, writes output, prints compact JSON stats to stdout. On error: writes `ERROR: …` to output file. |

---

## Which tool for which obfuscation?

| You see in the file… | Use |
|---|---|
| `Chr(72) & Chr(105) & Chr(33)` | **vbs_fold_chr_calls** |
| Arithmetic junk: `(18*2-(13-17))+32` | **vbs_fold_arithmetic** |
| String fragments joined by `&` or `+`: `"He" & "ll" & "o"` / `"He" + "ll" + "o"` | **vbs_fold_concat** |
| Calls to pure builtins with constant args: `Replace("W@S","@","")`, `Mid("abc",2,1)` | **vbs_fold_builtin_calls** |
| User-defined single-expression wrapper function: `Function ttaffRy(s): ttaffRy = Replace(s,"@","")` | **vbs_inline_functions** |
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

- **vbs_remove_deadcode** — also `--aggressive` (removes unreferenced Function/Sub
  definitions), `--preserve-strings` (keeps string/number literal RHS assignments
  even when dead, as a safety guard for files not yet run through propagation), and
  `--remove-empty-loops` (removes, rather than just flags, empty-body loops whose
  condition contains no parenthesized call).
- **vbs_strip_comments** — also `--include-trailing` (also strips end-of-line comments).
- **vbs_rename_variables** — also `--renames FILE` (required; JSON mapping old names to new).
- **vbs_extract_variables** — takes **only** `--input`; prints a JSON *report* to stdout and
  writes no output file (analysis-only).

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

### vbs_propagate_constants
Flow-sensitive constant propagation: walks statements in order, tracks each variable's
current constant value, and substitutes downstream reads. Kills a variable's tracked
value when it is assigned a non-constant (e.g. a method call result) or reassigned
inside a block. Always substitutes known constants into an assignment's RHS even when
the LHS is already killed.

**Self-append accumulators**: VBScript's uninitialized `Variant` is `Empty`, which
coerces to `""` in a string context.  When a variable is encountered for the first
time as both LHS and an operand of its own RHS (`X = X & "chunk"`), the tool
automatically seeds it as `""` so the full chain folds in one run.  Follow with
**vbs_fold_concat** to collapse the resulting `"" & "chunk"` literals.

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
- `Dim` declarations: whole line deleted when all declared names are dead;
  trimmed to keep only live names when partially dead.
- Statically-false `If`/`Do While` blocks removed.

`--preserve-strings`: keep string/number literal RHS assignments even when dead
(safety guard for files not yet run through `vbs_propagate_constants`).

`--aggressive`: also removes unreferenced `Function`/`Sub` definitions.

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

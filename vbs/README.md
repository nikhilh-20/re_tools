# VBScript Deobfuscation Toolkit

A field reference for the 14 utilities in this folder. Each is a thin CLI wrapper
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
| String fragments joined by `&`: `"He" & "ll" & "o"` | **vbs_fold_concat** |
| Calls to pure builtins with constant args: `Replace("W@S","@","")`, `Mid("abc",2,1)` | **vbs_fold_builtin_calls** |
| User-defined single-expression wrapper function: `Function ttaffRy(s): ttaffRy = Replace(s,"@","")` | **vbs_inline_functions** |
| A variable assigned a constant once, then referenced many times | **vbs_propagate_constants** |
| `If (5 > 3) Then ... End If` always-true wrapper | **vbs_unwrap_trueif** |
| Dead assignments / always-false `If`/`Do While` blocks | **vbs_remove_deadcode** |
| Multiple statements packed on one line with `:` | **vbs_expand_colons** |
| Runs of blank or whitespace-only lines | **vbs_collapse_blanklines** |
| Comment-only lines | **vbs_strip_comments** |
| Need a map of variables → decoded values → sinks | **vbs_extract_variables** |
| Want to apply readable names to garbage identifiers | **vbs_rename_variables** |
| Want to see what `ExecuteGlobal`/`Execute`/`Eval` actually runs | **vbs_annotate_execute** |

---

## Invocation convention

Every wrapper takes `--input FILE` and `--output FILE` and prints a compact JSON
stats line to stdout. On failure it writes `ERROR: <message>` into the output file.

```
python vbs_fold_chr_calls.py --input sample.vbs --output step1.vbs
```

### Exceptions to the two-argument convention

- **vbs_remove_deadcode** — also `--aggressive` (removes unreferenced Function/Sub
  definitions) and `--preserve-strings` (keeps string/number literal RHS assignments
  even when dead, as a safety guard for files not yet run through propagation).
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
Folds `&`-chains of constant terms (string literals, `Chr()` results, numbers coerced
to strings) to a single quoted string literal.

Input → Output:
```vbs
v = "htt" & "ps" & "://" & "evil.example"
```
```vbs
v = "https://evil.example"
```

### vbs_fold_builtin_calls
Evaluates calls to allowlisted pure VBScript builtins with all-constant arguments:
`Chr`, `Asc`, `Len`, `UCase`, `LCase`, `Trim`, `LTrim`, `RTrim`, `CStr`, `CInt`, `CDbl`,
`Hex`, `Oct`, `Abs`, `Int`, `Fix`, `Sqr`, `StrReverse`, `Space`, `Mid`, `Left`, `Right`,
`Replace`, `InStr`, `String`.

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

### vbs_unwrap_trueif
Collapses single-clause `If <always-true> Then … End If` blocks to just the body.
Condition is evaluated via the shared constant resolver.

### vbs_remove_deadcode
Default mode:
- Liveness-based dead-store removal: assignments to variables never read are
  deleted. Iterates to a fixpoint — removing one dead store can cascade to
  make its RHS variables dead in the next pass.
- `Dim` declarations: whole line deleted when all declared names are dead;
  trimmed to keep only live names when partially dead.
- Statically-false `If`/`Do While` blocks removed.

`--preserve-strings`: keep string/number literal RHS assignments even when dead
(safety guard for files not yet run through `vbs_propagate_constants`).

`--aggressive`: also removes unreferenced `Function`/`Sub` definitions.

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

# Batch Deobfuscation Toolkit

A field reference for the 24 utilities in this folder. Each is a thin CLI wrapper over shared
library code in `batdeoblib/`. Every utility targets **one** obfuscation technique (or one
supporting cleanup), so you chain them by hand — same convention as the `vbs/` and `powershell/`
toolkits.

> **Safety — parse-only.** No utility ever *executes* the target script, and none ever run
> anything a script drops (`bat_extract_stages.py` writes recovered stages to disk but never runs
> them either). Every transform is driven by static tokenization, a documented model of cmd.exe's
> own expansion semantics, and constant folding. Safe to run against live malware samples.

> **Design principle.** Every pass here is built against cmd.exe's *documented* parsing/expansion
> semantics and against *general* obfuscation techniques — never against one sample's specific
> shape. Where a pass can't prove something, it leaves the input untouched and reports why, rather
> than guessing. See "Verification" below for how the harder semantics were checked.

---

## Shared library (`batdeoblib/`)

| Module | Purpose |
|---|---|
| `tokenizer.py` | cmd.exe-aware lexer. Quote-and-caret-aware: tracks running quote state per token, so a grammar character inside a quoted string or after a caret escape is structurally impossible to mistake for code. Recognizes `%VAR%`/`%N`/`%~mods`/`%%`, `!VAR!` (as a lexical *candidate* — whether it actually expands is a runtime fact, not a lexical one), caret escapes, labels, and `rem`/`::` comments. |
| `expansion.py` | `expand_run` / `expand_statement` — the two-phase cmd.exe expansion model (percent phase, delayed-expansion phase, and `call`'s documented extra percent-expansion round). Resolves only what it can prove; returns a typed `Expanded(text=None, reason=...)` for anything else. |
| `env.py` | Three-state variable environment (Known / Unknown / Unset) with a `setlocal`/`endlocal` scope stack. `EnableDelayedExpansion` state is itself scoped like a variable, matching verified behavior. |
| `statements.py` | Splits a token stream into statements and `(...)` blocks. Block membership matters beyond grouping: it drives the block-local `%`-pre-expansion rule (see below). |
| `simulate.py` | Shared flow-sensitive, straight-line forward simulator (no goto/branch-following) that every fold/constants pass builds on — the single source of truth for "what does the environment look like at statement N". |
| `resolver.py` | `eval_arith` (full `set /a` grammar, including `set /a`'s own bare-identifier variable reads) and `eval_condition` (`if` string/numeric comparisons). |
| `cfg.py` | Label table and goto/call edge extraction for the control-flow passes. |
| `io.py` | CLI harness: `--input FILE --output FILE`, calls `fn(text, **opts) -> (new_text, stats)`, writes output, prints compact JSON stats. On error: writes `ERROR: …` to the output file. |

---

## Which tool for which obfuscation?

| You see in the file… | Use |
|---|---|
| Identifiers broken by carets: `p^o^w^e^r^s^h^e^l^l` | **bat_strip_carets** |
| Everything packed onto one line with `&`/`&&`/`\|\|`/`\|`, or unindented `(...)` blocks | **bat_expand_lines** |
| Junk `rem`/`::` comment banners | **bat_strip_comments** |
| Any other recognizable filler line to drop | **bat_strip_lines** |
| Runs of blank/whitespace-only lines | **bat_collapse_blanklines** |
| Inconsistent `set X=Y` vs `set "X=Y"` spelling | **bat_normalize_set** |
| Character harvested via `%SEED:~N,1%` / `!SEED:~N,1!` from a big seed string | **bat_fold_substrings** |
| `%VAR:find=repl%` / `%VAR:*find=repl%` string rebuilding | **bat_fold_strsub** |
| A value built from adjacent literal/resolved pieces: `%A%%B%literal` | **bat_fold_concat** |
| Byte value spelled as junk math: `set /a "x=(18+18-(13-17))+32"` | **bat_fold_arithmetic** |
| A `for /l` or `for %%V in (...)` loop accumulating a string one piece at a time | **bat_fold_for_loops** |
| `call set "X=%%!Y!%%"` — reading a variable NAMED by another variable | **bat_resolve_indirection** |
| A variable assigned once, then referenced many times | **bat_inline_constants** |
| One variable **reused** to hold a different constant before each use | **bat_propagate_constants** |
| Opaque-predicate `if` that's always **true**: `if "1"=="1" (...)` | **bat_unwrap_trueif** |
| Code order scrambled via a chain of unconditional `goto`s | **bat_unflatten_goto** |
| A `call :label` subroutine used from exactly one call site | **bat_inline_subroutines** |
| Dead stores / unreachable code after an unconditional `goto`/`exit` | **bat_remove_deadcode** |
| A base64 or single-byte-XOR-over-hex blob held in a variable | **bat_decode_blobs** |
| Want the recovered PowerShell/VBS stage as its own file | **bat_extract_stages** |
| Want to see what a `powershell`/`cmd`/`mshta`/… sink actually runs | **bat_annotate_exec** |
| The program itself (not just an argument) is hidden behind a variable | **bat_unwrap_call** |
| Need a map of variables → decoded values → sinks | **bat_extract_variables** |
| Want to apply readable names to garbage identifiers | **bat_rename_variables** |

### Invocation convention

Every wrapper takes `--input FILE` and `--output FILE` and prints a compact JSON stats line
(`changed`, `input_bytes`, `output_bytes`, `output_path`), except where noted below. On failure a
wrapper writes `ERROR: <message>` into the output file instead of transforming it.

```
python bat_fold_substrings.py --input sample.cmd --output sample.step1.cmd
```

Exceptions to the two-argument convention:
- **bat_remove_deadcode** — also `--aggressive` (optional switch, default off; additionally
  removes a `for` loop whose body other passes have hollowed out to empty).
- **bat_decode_blobs** — also `--mode {base64,xor-hex}` (default `base64`) and `--key N`
  (0–255, required for `--mode xor-hex`). Reports `changed`/`candidates` (every blob it examined,
  decoded or not).
- **bat_strip_comments** — also `--include-data` (optional switch; without it, a `::` line whose
  content looks like a plausible data carrier — not prose — is left alone by default). Reports
  `changed`/`rem_lines_removed`/`data_comment_lines_removed`/`comments_kept`.
- **bat_strip_lines** — also `--pattern <regex> --flags <ims>` (both mandatory).
- **bat_expand_lines** — also `--indent-string <string>` (optional, default 4 spaces).
- **bat_rename_variables** — also `--renames <json>` (mandatory).
- **bat_inline_constants** — also `--max-uses <int>` (optional, `0` = unlimited).
- **bat_extract_variables** — takes **only** `--input`; prints a JSON *report* to stdout and
  writes no output file.
- **bat_extract_stages** — takes `--input` and `--outdir` (not `--output`); writes N stage files
  and prints a JSON manifest to stdout. Never executes a recovered stage.

---

# Obfuscation-defeating passes

## bat_strip_carets

### Description
Removes an inline caret that sits between two word characters (`[A-Za-z0-9_]`) outside quotes —
the `p^o^w^e^r^s^h^e^l^l` trick, the direct analogue of PsStrip-Backticks' backtick-splitting
removal. Only strips a caret when doing so is *provably* a no-op for cmd.exe's grammar: both the
character before the caret and the character it escapes must be word characters, which structurally
guarantees joining them can never create or destroy a grammar token. A caret escaping a
grammar-significant character (`^&`, `^(`, `^%`, `^^`, …) is always left untouched.

### Examples
Input:
```bat
p^o^w^e^r^s^h^e^l^l -c "a^&b" & echo a^&b
```
Output (`changed:9`):
```bat
powershell -c "a^&b" & echo a^&b
```
(The carets inside the quoted string and the one escaping `&` are correctly left alone — the first
because caret has no meaning inside quotes at all, the second because removing it would change
`a^&b` from one literal argument into two separate commands.)

### How it works
Walks the token stream for `CARET_ESC` tokens (already guaranteed by the tokenizer to be outside
quotes). For each, checks that the escaped character and the immediately preceding token's last
character are both word characters, and if so replaces the caret+char span with just the char.

---

## bat_expand_lines

### Description
Splits `&`/`&&`/`||`/`|` connectors and `(...)` blocks onto their own indented lines. The `&&`/`||`
connector is **kept as a prefix on the line it introduces**, never dropped — those are conditional
execution (run only if the previous command succeeded/failed), not a bare separator, so silently
collapsing one to a plain newline would change what runs. A block's opening `(` is only ever placed
on its own line when the statement before it was itself terminated by a real newline in the
source; otherwise (the common `for %%A in (1 2) do (...)` / `if "%x%"=="1" (...)` shape) it stays
glued to the preceding line, since `for`/`if` do not reliably tolerate their own clause being
broken before the opening paren is seen.

### Examples
Input:
```bat
set a=1&set b=2&&echo ok||echo fail
for %%A in (1 2) do (echo %%A
echo hi)
```
Output:
```bat
set a=1
& set b=2
&& echo ok
|| echo fail
for %%A in (
    1 2
)
do (
    echo %%A
    echo hi
)
```

### How it works
Builds the shared statement/block tree (`batdeoblib.statements.parse_script`) and re-renders it
with indentation, rather than regex-splitting raw text — so a connector character inside a quoted
string or a `%VAR:...%` modifier is never mistaken for a statement separator.

---

## bat_strip_comments

### Description
Removes comment-only lines (`rem ...` and `:: ...`). Unlike a `rem` line (virtually always
harmless prose), a `::` line is, structurally, just a mislabeled label cmd.exe skips without
evaluating — malware routinely parks encoded data payloads there. By default this pass only strips
a `::` line when its content looks like ordinary prose; a long or unbroken-word line (a plausible
data carrier) is left alone unless `--include-data` is passed. Annotation markers left by
**bat_annotate_exec** are always preserved.

### Examples
Input:
```bat
rem junk banner
:: short note
:: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
echo hi
```
Output (`changed:2`):
```bat
:: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
echo hi
```

### How it works
Driven by the tokenizer's `COMMENT` spans (a `::`/`rem` inside a quoted string is never mistaken
for one). For a `::` line, the content after the marker is checked against a simple prose
heuristic (longest word length, total line length) before it's eligible for removal.

---

## bat_strip_lines

### Description
General-purpose scalpel: removes every line matching a regex you supply. For filler the other
passes don't specifically target. Blind line filter with no string/comment awareness — point it
carefully.

### Examples
Command: `--pattern "^\s*rem GEN" --flags i`

### How it works
Line-by-line regex filter over the raw text. `--flags` combines `i`/`m`/`s`.

---

## bat_collapse_blanklines

### Description
Deletes whitespace-only lines outright, then separately squeezes runs of 3+ consecutive
genuinely-empty lines down to one. Quote-aware: guarded by the tokenizer's per-token `in_quotes`
flag so a blank-looking line inside an open multi-line quoted string (rare in Batch, but possible
via caret line-continuation inside quotes) is never touched or absorbed into a squeeze.

### Examples
Input (`a=1`, several blank/whitespace-only lines, then `b=2`):
```bat
a=1



   

b=2
```
Output:
```bat
a=1

b=2
```

### How it works
Two stages over the raw text: delete non-empty whitespace-only lines; then collapse runs of 3+
adjacent empty lines to one, both stages skipping any line the tokenizer shows as inside an open
quote span.

---

## bat_normalize_set

### Description
Quotes a bare `set NAME=VALUE` to `set "NAME=VALUE"` **only** where doing so is provably
meaning-preserving. `set X=Y&Z` (bare) runs `set X=Y` then, as a *separate* command, `Z` — `&` is
grammar-significant there. Wrapping the whole thing in quotes would fuse those into one assignment
whose value literally contains `&Z`, changing behavior. This pass only quotes an assignment whose
right-hand side, as tokenized, contains no grammar-significant OP token — i.e. it was already being
treated as a single argument, and wrapping it changes nothing except making that explicit.
`set "X=Y"` input is already canonical and untouched; `set /a`/`set /p` are out of scope.

### Examples
Input:
```bat
set X=Y
set X=Y&echo hi
```
Output (`changed:1`):
```bat
set "X=Y"
set X=Y&echo hi
```
(The second line is left alone: quoting it would change two statements into one.)

---

# Expansion folding — the core

## bat_fold_substrings

### Description
Folds `%VAR:~start[,len]%` / `!VAR:~start[,len]!` against a statically-known `VAR` into the literal
result — the direct analogue of the "spell the payload out of a big seed string, one character at a
time" idiom PsFold-CharConcat / vbs_fold_instr_mid defeat. Correctly distinguishes `%`-refs
(resolved against the environment as of the enclosing `(...)` block's entry — see "Verification"
below) from `!`-refs (resolved against the live, current value), via the shared simulator.

### Examples
Input:
```bat
set "S=XPT1d_9qstx(38W{)LgQVKeroAU..."
setlocal EnableDelayedExpansion
set "D=!S:~52,1!!S:~37,1!"
```
Output (`changed:2`):
```bat
set "D=Sy"
```

### How it works
Walks every statement via `batdeoblib.simulate`, and for each `%VAR:~.../!VAR:~...` token whose
base name resolves to a Known value in the appropriate environment (block-entry snapshot for `%`,
live for `!`), applies the substring extraction (negative start/length handled per verified
semantics) and replaces the token. Refuses (leaves untouched) if the resolved text itself would
contain `%` or `!` — inlining raw `%`/`!` characters risks them pairing with something else on the
line to form a brand-new, unintended expansion once re-tokenized.

---

## bat_fold_strsub

### Description
Folds `%VAR:find=repl%` / `!VAR:find=repl!`, including the `:*find=repl` prefix form (match extends
back to the start of the string through the first occurrence), against a statically-known `VAR`.
The search/replace counterpart of **bat_fold_substrings**.

### Examples
Input:
```bat
set "T=foo.bar.baz"
echo %T:.=_%
echo %T:*.=_%
```
Output (`changed:2`):
```bat
echo foo_bar_baz
echo _bar.baz
```

### How it works
Same environment model as **bat_fold_substrings**. Search/replace is case-insensitive (matching
documented `set` string-substitution behavior); an empty search pattern is refused rather than
risk the trivial always-matches-everywhere interpretation.

---

## bat_fold_concat

### Description
Folds adjacent literal/resolved pieces into one literal. Batch has no explicit concatenation
operator — `set "X=%A%%B%literal"` concatenates purely by *juxtaposition* — so "folding a
concatenation" means finding a maximal run of TEXT / `%%`-literal / resolvable `%`-var /
resolvable `!`-var / caret-escape tokens (bounded by a quote, whitespace, operator, or an
unresolvable expansion) and collapsing that whole run, anywhere in a statement, not just inside
`set`.

### Examples
Input:
```bat
set "A=Hello"
set "B=World"
set "C=%A%%B%!"
```
Output (`changed:1`):
```bat
set "C=HelloWorld!"
```

### How it works
For each maximal foldable run, resolves every piece and joins them. Refuses the fold if a piece
that came from an **actual resolved variable value** (not already-plain source text) contains `%`
or `!` — a plain-text `%`/`!` already in the source was, by construction, never part of any real
pairing (anything that was would already be its own dedicated token, not TEXT), so it's always safe
to carry through; a value pulled from a variable is dropper-controlled data and could coincidentally
form a new pairing once merged.

---

## bat_fold_arithmetic

### Description
Collapses constant `set /a` arithmetic into its numeric literal — byte values or lengths spelled
as junk math. Supports the full documented grammar (`+ - * / % & | ^ ~ ! << >>`, `0x`/octal
literals, comma-separated multi-assignment, compound assignment like `+=`), **and** `set /a`'s own
bare-identifier variable-read syntax (`set /a "x=y+1"` reads `y` directly, no `%`/`!` needed —
verified empirically; an unset or non-numeric variable contributes `0`, never a hard error,
matching real `set /a` behavior).

### Examples
Input:
```bat
set /a "x=(18+18-(13-17))+32"
set "y=4"
set /a "z=y+1,w=10 %% 3"
```
Output (`changed:2`):
```bat
set /a "x=72"
set /a "z=5,w=1"
```

### How it works
Expands `%`/`!` refs in the expression text (respecting the block/live two-clock rule), then
parses and evaluates with a small recursive-descent evaluator (32-bit signed wraparound, matching
`set /a`'s native int semantics). Compound assignment (`x+=5`) reads `x`'s pre-assignment value as
the implicit left operand. Each comma-separated item folds independently — one unresolvable item
doesn't block its siblings.

---

## bat_fold_for_loops

### Description
Folds the accumulator idiom built on a statically-enumerable `for` loop — `for /l` (numeric
start,step,end) or plain `for %%V in (item item ...)` — into a single literal, removing the loop.
The direct analogue of PsFold-ArrayJoins / vbs_fold_array_join_loops.

### Examples
Input:
```bat
setlocal EnableDelayedExpansion
set "S=abcdefgh"
set "ACC="
for /l %%i in (0,1,4) do (
  set "ACC=!ACC!!S:~%%i,1!"
)
```
Output (`changed:1`, `total_iterations_folded:5`):
```bat
set "ACC=abcde"
```

### How it works
Re-simulates the loop body once per iteration (using the shared arithmetic/expansion machinery),
substituting the `for`-loop metavariable (`%%i`) directly — the tokenizer has no built-in notion of
`for` variables (`%%i` lexes as a literal `%%` + `i` at the lexical level; recognizing it as a bound
loop variable is deliberately deferred to whichever pass actually knows a `for` declared it, which
is this one), including inside a `!S:~%%i,1!`-style embedded reference. Folds only when *every*
iteration's fragment is provably resolvable; any single unresolvable iteration refuses the whole
loop rather than emit a partial result. `for /f` tokenizing is out of scope (refuses cleanly).

---

## bat_resolve_indirection

### Description
Resolves `call`-mediated indirect variable reads (MITRE ATT&CK T1027.007-style indirection) — the
Batch analogue of PsResolve-Reflection. `call set "X=%%!Y!%%"`: the first expansion round every
statement gets already collapses `%%` to `%` and delayed-expands `!Y!` to Y's current value (say,
`REALNAME`); `call`'s documented extra percent-expansion round then reads `%REALNAME%` — Y's value
used *as a variable name*, not as data. This pass computes that first round directly (it already
knows exactly what round 2 would see) and rewrites to the plain, direct form with `call` and the
`%%`/`!` wrapping removed: `set "X=%REALNAME%"`. It does **not** also inline `REALNAME`'s own
value — that's now ordinary literal source text, picked up naturally by **bat_fold_concat** /
**bat_inline_constants** on a later pass.

### Examples
Input:
```bat
setlocal EnableDelayedExpansion
set "IDX=REALNAME"
set "REALNAME=secretvalue"
call set "RESULT=%%!IDX!%%"
```
Output (`changed:1`):
```bat
set "RESULT=%REALNAME%"
```

### How it works
For a `call`-prefixed statement whose tokens contain at least one `%%`/`!...!` (the raw material
`call`'s extra round is actually needed to unwrap), computes the first expansion round and rewrites
the whole statement to that result, with `call` dropped. Never fires on `call :label ...` (a real
subroutine invocation, not indirection) — removing `call` there would change behavior, not just
spelling.

---

## bat_inline_constants

### Description
Inlines a variable assigned **exactly once**, anywhere in the script, at unconditional top level,
with a constant — then removes the now-dead assignment. The simple single-static-assignment case.
For a variable reassigned more than once, use **bat_propagate_constants** instead (which
substitutes reads but correctly never deletes any of the several assignments that give it meaning).

### Examples
Input: `--max-uses 0`
```bat
set "path=calc.exe"
start %path%
echo %path%
```
Output (`changed:2`, `assignments_removed:1`):
```bat

start calc.exe
echo calc.exe
```

### How it works
Finds variables with exactly one top-level `set` assignment resolving to a constant, substitutes
every bare (no-modifier) read up to `--max-uses`, and deletes the assignment **only** once every
read of it — including any left for **bat_fold_substrings**/**bat_fold_strsub** because it carries
a modifier, or left uncapped by `--max-uses` — was actually covered.

---

## bat_propagate_constants

### Description
Flow-sensitive substitution of bare `%VAR%`/`!VAR!` reads (no modifier — those are
**bat_fold_substrings**/**bat_fold_strsub**'s job) with their statically-known value at that point
in the script. The general form of **bat_inline_constants** for a variable *reused* to hold a
different constant before each read.

### Examples
Input:
```bat
if 1==1 (set "seed=152") else (set "seed=0")
set "m=hi-%seed%"
Invoke-Expression %m%
set "m=by"
Invoke-Expression %m%
```
Output (`changed:1`):
```bat
if 1==1 (set "seed=152") else (set "seed=0")
set "m=hi-%seed%"
Invoke-Expression %m%
set "m=by"
Invoke-Expression by
```
(`seed` is written inside a block, so it's conservatively Unknown afterward — `m`'s *first*
assignment inherits that and stays unresolved too. Run **bat_unwrap_trueif** first if the condition
is itself provably true; it turns this into a plain top-level store this pass *can* resolve.)

### How it works
A thin consumer of the shared simulator (`batdeoblib.simulate`): substitutes wherever the
straight-line, source-order simulation proves a Known value, correctly distinguishing `%`
(block-entry snapshot) from `!` (live value).

---

# Control flow

## bat_unwrap_trueif

### Description
Collapses an `if` statement whose condition is statically **true** down to just its action/body —
the opaque-predicate counterpart to a false condition (**bat_remove_deadcode**'s job). Handles
`defined`, string equality (`==`), and numeric comparison (`EQU`/`NEQ`/`LSS`/`LEQ`/`GTR`/`GEQ`),
each optionally `/i` and/or `not`-negated, in both `if`-shapes: same-line (`if COND action`) and
parenthesized (`if COND (body) [else (elsebody)]`). `exist`/`errorlevel` are never resolvable
(filesystem/exit-code dependent) and are always left alone.

### Examples
Input:
```bat
if "1"=="1" set "key=FrsnYjHYk"
if 5 GTR 3 (
    echo yes
) else (
    echo no
)
```
Output (`changed:2`):
```bat
set "key=FrsnYjHYk"
echo yes
```

### How it works
Parses the condition's token span directly (locating where it ends and the action begins without
relying on brittle text regexes), resolves it via the shared `%`/`!` expansion plus
`resolver.eval_condition`, and — when provably true — either replaces the whole same-line statement
with just its action, or lifts the parenthesized true-branch body out in place (dropping any
`else`). Safe because `(...)` groups are not scope boundaries in Batch — there's no block scoping
at all outside `setlocal`/`endlocal` — so lifting a body out changes nothing about where its
assignments land. `defined` is checked against the variable's raw Known/Unset/Unknown *state*
(never `resolve_read()`'s expanded value, which is `''` for both an unset variable and, after
`simulate.py`'s empty-assignment-deletes-the-variable handling, a never-non-empty one — exactly
right for expansion, but not the same question as "was this ever assigned").

---

## bat_unflatten_goto

### Description
Straightens a chain of unconditional `goto`s that only exists to scramble reading order — malware
routinely chops a straight-line script into label-delimited chunks connected by out-of-order
`goto`s specifically to defeat linear reading, even without an explicit dispatcher-state-variable.
Scoped to what's safely provable: splices label `L`'s body in place of an unconditional `goto L`
exactly when relocating `L` is provably safe — `L` is targeted by exactly **one** goto/call edge
anywhere in the script, `L` is not also reachable by plain fall-through from whatever precedes it,
and the `goto` is unconditional (the statement's own first word, not embedded in an `if`). The chain
continues as far as it safely can; a loop, a real branch, or a goto whose target has any other
incoming edge stops it right there, left untouched.

### Examples
Input:
```bat
@echo off
goto PART_C
:PART_A
echo step-A
exit /b 0
:PART_C
echo step-1
goto PART_B
:PART_B
echo step-2
goto PART_A
```
Output after re-running to a fixpoint (`changed:2` then `changed:1`):
```bat
@echo off
echo step-1
echo step-2
echo step-A
exit /b 0
```
(A single call may only straighten one link of a multi-link chain at a time — like every other pass
here, re-run to a fixpoint.)

### How it works
Builds the label table and goto/call graph (`batdeoblib.cfg`), computes each label's in-degree, and
for each block ending in a single-use unconditional `goto L` with no fall-through predecessor,
moves `L`'s body (as raw text) to replace the `goto`, deleting `L`'s old location and its now-
redundant label line.

---

## bat_inline_subroutines

### Description
Inlines a `call :label args...` subroutine at its call site when there is **exactly one** call
site for that label anywhere in the script. The Batch analogue of vbs_inline_functions.
Substitutes `%1`-`%9`/`%*` in the body with the literal call-site arguments.

Scoped to the unambiguous shape: the subroutine body has at most **one** exit point — a trailing
`goto :eof`/`exit /b` as its last statement, or no explicit terminator (falls off the end). A body
with a real early return — including one embedded in a same-line `if COND goto :eof` — is left
untouched: correctly inlining that needs redirecting each early exit to a fresh post-inline label, a
transformation this pass doesn't attempt rather than risk getting subtly wrong.

### Examples
Input:
```bat
call :greet hello world
echo done
goto :eof
:greet
echo arg1=%1 arg2=%2
goto :eof
```
Output (`changed:1`):
```bat
echo arg1=hello arg2=world
echo done
goto :eof
:greet
echo arg1=%1 arg2=%2
goto :eof
```
(The now-unreferenced `:greet` definition is left in place — that's **bat_remove_deadcode**'s job,
once it's confirmed unreachable.)

### How it works
Finds labels with exactly one `call` edge (and no plain `goto` also targeting them — a label reached
both ways isn't exclusively a call-site body), verifies the single-exit-point shape, then splices
the body's text into the call site with `%1`-`%9`/`%*` replaced by the parsed call-site arguments
(quote-aware whitespace splitting).

---

## bat_remove_deadcode

### Description
Removes dead stores and unreachable code. Two independent analyses:

1. **Reachability** — a worklist walk over the statement list and the goto/call graph starting
   from statement 0. An unconditional `goto`/`exit` does not fall through; a conditional one
   (`if COND goto X`) reaches both the target and the fall-through. An orphaned label and the dead
   code after an unconditional jump are the *same* analysis, not two — a never-reached label is
   simply never added to the reachable set by this same walk. A computed/non-literal goto target
   makes the whole analysis unreliable, so this pass **refuses entirely** rather than risk deleting
   code a real jump might still reach.
2. **Dead stores** — a `set`/`set /a` target never read by any other reachable statement is
   removed, as a fixpoint: removing a dead store can make whatever *it* read dead too (cascading),
   so read counts are decremented and rechecked until stable.

### Examples
Input:
```bat
@echo off
goto SKIP
echo never runs
:SKIP
echo after
```
Output (`changed:1`, `unreachable_removed:1`):
```bat
@echo off
goto SKIP

:SKIP
echo after
```

Cascading dead store:
```bat
set "a=1"
set "b=%a%"
echo done
```
Output (`changed:2`, `dead_stores_removed:2`): both removed — `b` is never read, and once it's
gone, `a`'s only read (inside `b`'s own RHS) disappears too.

### `--aggressive` (optional, default off)
Additionally removes a `for` loop whose body other passes have hollowed out to empty. Does not
widen the dead-store liveness model itself — that stays identical in both modes.

---

# Decoding / staging

## bat_decode_blobs

### Description
Decodes a statically-known string variable via one of two GENERIC decoders (never tied to a
particular sample's variable names or loop shape):
- `--mode base64` (default): any Known variable whose value is plausibly base64 is decoded;
  printable results are reported with a preview and annotated in place.
- `--mode xor-hex --key N`: decodes a Known pure-hex-string variable by XORing each byte with the
  given single-byte key. Auto-discovering *which* of possibly several `set /a ... ^ ...`
  expressions elsewhere in a script is "the" key for a given blob is exactly the kind of
  sample-shape-specific guess this toolkit's design principle rules out — the key is supplied
  explicitly, once a human (or **bat_extract_variables**' report) has identified it.

### Examples
Command: `--mode base64`
Input: `set "BLOB=SGVsbG8gV29ybGQh..."`
Output stats: `{"changed":1,"candidates":[{"variable":"BLOB","encoding":"base64","printable_ratio":1.0,"decoded_preview":"Hello World!..."}]}`
— the source gains a trailing `rem <<<DECODED BLOB (base64)>>> / rem > ... / rem <<<END>>>` block.

### How it works
Runs the shared simulator to completion and collects every variable's final Known value, filters by
charset/length plausibility for the chosen encoding, decodes, and reports every candidate examined
(decoded or not) via `candidates` in the stats — reports, never silently drops, an unresolved
candidate.

---

## bat_extract_stages

### Description
Writes each recovered embedded stage to its own file, plus a JSON manifest — the handoff utility
that lets the PowerShell/VBS toolkits pick up a dropped stage directly. Deviates from the
`--input`/`--output` convention (like **bat_extract_variables**): takes `--input` and `--outdir`,
writes N files, prints the manifest to stdout. Never executes anything, including the recovered
stages.

Two GENERIC recovery sources: a `-EncodedCommand`/`-enc` argument to `powershell`/`pwsh` anywhere in
the script (base64 + UTF-16LE decoded, PowerShell's own documented format), and the same
statically-known-base64-variable detection **bat_decode_blobs** uses.

### Examples
```
$ python bat_extract_stages.py --input sample.cmd --outdir ./stages
{
  "stages": [
    {"file": "stage1.ps1", "origin": "-EncodedCommand argument at offset 11", "decoder": "base64/utf16le"}
  ]
}
```

---

## bat_annotate_exec

### Description
Non-destructive: leaves the code intact and appends decoded/resolved payloads as `rem` comments
after an exec-sink command, so you can read what it runs without executing anything. The Batch
analogue of PsAnnotate-Iex / vbs_annotate_execute. Recognizes `powershell`/`pwsh`
`-EncodedCommand`/`-enc` (base64+UTF-16LE decoded) and `-Command`/`-c`, `cmd`/`cmd.exe /c` (leading
`/c`/`/k` switch stripped from the annotation), and `mshta`/`wscript`/`cscript`/`rundll32`/`start`
(full resolved command line), whenever the relevant argument is statically resolvable. Markers match
**bat_strip_comments**' preserved-annotation allowlist.

### Examples
Input: `powershell -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgAGgAaQA=`
Output (`changed:1`):
```bat
powershell -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgAGgAaQA=
rem <<<EXEC PAYLOAD BEGIN>>>
rem > Write-Host hi
rem <<<EXEC PAYLOAD END>>>
```

---

## bat_unwrap_call

### Description
Resolves a statement whose **program itself** — not just one of its arguments — is hidden behind a
variable: `set "X=powershell -c calc"` followed later by a bare `%X%` statement invoking it. The
Batch analogue of vbs_unwrap_execute's "hidden statement" unwrapping, adapted to Batch's actual
indirection idiom (there is no `Execute "<string>"` dynamic-eval construct in Batch the way
VBScript has one). Distinct from **bat_propagate_constants** (substitutes individual reads
wherever they appear) and **bat_fold_concat** (merges adjacent pieces): this targets the
statement-level shape where the identity of the program being run is itself the indirection,
resolving the whole statement in one shot — including a chained case (`%CMD1%` resolves to literal
text that is itself another `%CMD2%`-shaped reference) that per-token folding only reaches after a
second pass.

### Examples
Input:
```bat
set "CMD2=powershell -c calc"
set "CMD1=%%CMD2%%"
%CMD1%
```
Output (`changed:1`, last line only):
```bat
powershell -c calc
```

---

# Analysis utilities

## bat_extract_variables

### Description
**Analysis only — writes no output file.** Emits a JSON report: every variable's assignment/read
sites, its final statically-known value (auto base64-decoding a likely-base64 literal), whether it
flows into an execution sink (`call`, `powershell`, `cmd`, `mshta`, `wscript`, `cscript`,
`rundll32`, `start` — including the indirect-command idiom **bat_unwrap_call** targets, where the
variable's value is invoked as the statement's own first word), and a suggested human-readable
name.

### Examples
Command: `--input sample.cmd`
```json
{
  "total_count": 2,
  "variables": [
    {"name": "URL", "final_value": "http://evil.example/c2", "reaches_sink": false, "suggested_name": "c2Url"},
    {"name": "CMD", "final_value": "powershell -c calc", "reaches_sink": true, "suggested_name": "sinkArg_CMD"}
  ]
}
```

---

## bat_rename_variables

### Description
Applies an `old -> new` rename map (JSON, case-insensitive keys) to **every** occurrence of each
variable — assignment targets, bare reads, and modifier-carrying reads (the modifier itself is
preserved). Pair it with **bat_extract_variables**.

### Examples
`renames.json`: `{"ab3xk": "c2Url"}`
Input:
```bat
set "aB3xk=http://evil.example/c2"
Invoke-WebRequest %aB3xk%
```
Output (`renamed:1`, 2 occurrences):
```bat
set "c2Url=http://evil.example/c2"
Invoke-WebRequest %c2Url%
```

---

## Verification

The expansion model (`batdeoblib/expansion.py`, `tokenizer.py`, `simulate.py`) is the toolkit's
foundation, and several of its rules contradict commonly-repeated folklore about cmd.exe. Rather
than trust memory, the harder semantics were checked directly against cmd.exe on Windows
10.0.19045 before being implemented:

- `%%` always collapses to a literal `%`, in or out of quotes.
- **`%VAR%` (matched pair) with `VAR` unset expands to an empty string — not left literal.** A
  single unmatched `%` (no closing partner on the line) is likewise deleted, not left literal.
- `!VAR!` (delayed expansion active) with `VAR` unset also expands to empty; with delayed expansion
  **inactive**, `!VAR!` is not even recognized as an expansion — it passes through completely
  literally, exclamation marks and all.
- Outside quotes, `^` always consumes itself and makes the next character literal, whether or not
  that character was otherwise special — the caret-splitting evasion trick. Inside quotes, `^` has
  no special meaning at all. A caret cannot be used to block `%`/`!` expansion (verified: `^%` with
  nothing after behaves exactly like a bare unmatched `%`, i.e. deletes to empty).
- `call` triggers a **second**, percent-only expansion round over the first round's result — the
  mechanism behind `call set "X=%%!Y!%%"`-style indirect variable reads.
- A `%VAR%` reference lexically inside a `(...)` block resolves using the environment **as of block
  entry**, held fixed for every statement in the block (even across `for` iterations); a `!VAR!`
  reference in the same block resolves **per statement, live** — this is *why* delayed expansion
  exists, and the reason `bat_fold_substrings`/`bat_fold_strsub`/`bat_propagate_constants` all
  track two separate environments (`env`, `pct_env`) rather than one.
- `set "X="` (empty right-hand side) does not assign `X` the empty string — cmd.exe's environment
  has no such state; it deletes the variable outright (verified: `if defined X` is false
  immediately after). `simulate.py` models this exactly (`env.unset`, not `env.set_known(name,
  '')`), which is what makes `bat_unwrap_trueif`'s `defined` check correct.
- `set /a` resolves a bare identifier (`set /a "x=y+1"`) as a direct variable read, no `%`/`!`
  needed — a `set /a`-specific grammar feature, separate from ordinary expansion. An unset or
  non-numeric variable contributes `0` in that context, not an error.
- Substring extraction (`%VAR:~start,len%`) and search/replace (`%VAR:find=repl%`,
  `%VAR:*find=repl%`) semantics, including negative start/length and the `*`-prefix
  extend-to-string-start form, were each checked against worked examples before being encoded into
  `resolver.py`/`expansion.py`.

## Notes & gotchas

- **Chain, then re-run.** Most passes expose constants the next pass needs. Loop the recommended
  chain until every stage reports `changed:0` — this includes **bat_unflatten_goto**, which
  deliberately straightens one link of a goto chain per call rather than recursively chasing the
  whole chain in one shot.
- **Fold before you decode.** **bat_decode_blobs**/**bat_extract_stages** need their target
  variable already fully resolved — run the substring/strsub/concat/constant-propagation folds
  first.
- **A caret before `%` or `!` is a real static-analysis ceiling, not a bug.** cmd.exe's own
  documented limitation is that caret cannot block `%`/`!` expansion, but correctly reproducing
  what a *caret-escaped* `%`/`!` pairs with (it's still eligible to pair, per the point above)
  would require re-scanning past a single token — `expand_run` refuses this shape explicitly rather
  than risk resolving it wrong. Run **bat_strip_carets** first (which removes exactly this
  ambiguity, since stripping `^%`→`%` is always meaning-preserving) and it never comes up.
- **Blank lines are expected.** Removal/inline passes delete statements but leave the newline;
  finish with **bat_collapse_blanklines**.
- **Most things are static — but goto-chain scrambling can now be reconstructed too, within
  limits.** **bat_unflatten_goto** resolves a chain of unconditional gotos when every link is
  provably single-use; a dispatcher whose next label genuinely depends on runtime data (decrypted
  content, a computed `goto %VAR%`) is a real static-analysis ceiling and is left untouched by both
  it and **bat_remove_deadcode** (which refuses its entire reachability analysis rather than guess
  past an unresolvable jump target).

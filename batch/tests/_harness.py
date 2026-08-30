"""Shared test helpers for the Batch deobfuscation toolkit.

Mirrors the shape the sibling vbs/tests/ suite uses (stdlib unittest, inline
synthetic sources, subprocess the wrapper + parse its JSON stdout), but factors
the copy-pasted per-file runners into one place.

Run the suite from the toolkit root:

    python -m unittest discover -s tests
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))


# --------------------------------------------------------------------------
# The recommended tool chain, in one place so the pipeline test and the
# corpus runner can never drift.  Each entry is (script_name, [extra_args]).
#
# This is the loopable --input/--output part only: cleanup -> the folds ->
# constant propagation/inlining -> control flow -> dead code.  Re-run it to a
# `changed:0` fixpoint.  The staging tools that deviate from the two-argument
# convention (--outdir) are listed separately.
# --------------------------------------------------------------------------
CHAIN: list[tuple[str, list[str]]] = [
    ('bat_strip_carets.py', []),
    ('bat_expand_lines.py', []),
    ('bat_strip_comments.py', []),
    ('bat_collapse_blanklines.py', []),
    ('bat_normalize_set.py', []),
    ('bat_fold_substrings.py', []),
    ('bat_fold_strsub.py', []),
    ('bat_fold_concat.py', []),
    ('bat_fold_arithmetic.py', []),
    ('bat_fold_for_loops.py', []),
    ('bat_resolve_indirection.py', []),
    ('bat_propagate_constants.py', []),
    ('bat_inline_constants.py', []),
    ('bat_unwrap_trueif.py', []),
    ('bat_unflatten_goto.py', []),
    ('bat_inline_subroutines.py', []),
    ('bat_remove_deadcode.py', []),
    ('bat_unwrap_call.py', []),
    ('bat_decode_blobs.py', []),
    ('bat_annotate_exec.py', []),
]

# Tools that write into a directory + a JSON manifest rather than --output.
STAGING_OUTDIR: list[tuple[str, list[str]]] = [
    ('bat_split_polyglot.py', []),
    ('bat_reconstruct_files.py', []),
    ('bat_extract_stages.py', []),
]


def _as_bytes(src) -> bytes:
    return src if isinstance(src, (bytes, bytearray)) else src.encode('utf-8')


def run_cli(script_name: str, src, *extra: str, encoding: str = 'utf-8'):
    """Write *src* to a tempfile, run bat_<script>.py as a subprocess, and
    return ``(output_text, stats_dict)``.

    *src* may be ``str`` (encoded with *encoding*) or ``bytes`` (written
    verbatim, e.g. to exercise a BOM).  ``stats_dict`` is the JSON the wrapper
    prints to stdout.
    """
    script = TOOL_DIR / script_name
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / 'in.cmd'
        out = Path(d) / 'out.cmd'
        inp.write_bytes(_as_bytes(src) if isinstance(src, (bytes, bytearray))
                        else src.encode(encoding))
        r = subprocess.run(
            [sys.executable, str(script), '--input', str(inp), '--output', str(out), *extra],
            capture_output=True, text=True, check=True,
        )
        return out.read_text('utf-8'), json.loads(r.stdout)


def run_cli_bytes(script_name: str, raw: bytes, *extra: str):
    """Like :func:`run_cli` but returns the raw output *bytes* — for newline /
    encoding assertions where text-mode round-tripping would hide the bug."""
    script = TOOL_DIR / script_name
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / 'in.cmd'
        out = Path(d) / 'out.cmd'
        inp.write_bytes(raw)
        r = subprocess.run(
            [sys.executable, str(script), '--input', str(inp), '--output', str(out), *extra],
            capture_output=True, text=True,
        )
        out_bytes = out.read_bytes() if out.exists() else b''
        return out_bytes, r


def run_cli_outdir(script_name: str, src, *extra: str):
    """Run a wrapper that takes ``--outdir`` (bat_split_polyglot,
    bat_reconstruct_files, bat_extract_stages).  Returns
    ``(outdir_Path, manifest_dict, files_written)``.  The tempdir is kept
    alive for the caller via the returned object holding a reference."""
    script = TOOL_DIR / script_name
    d = tempfile.TemporaryDirectory()
    inp = Path(d.name) / 'in.cmd'
    outdir = Path(d.name) / 'stages'
    inp.write_bytes(_as_bytes(src))
    r = subprocess.run(
        [sys.executable, str(script), '--input', str(inp), '--outdir', str(outdir), *extra],
        capture_output=True, text=True, check=True,
    )
    manifest = json.loads(r.stdout) if r.stdout.strip() else {}
    files = sorted(p.name for p in outdir.iterdir()) if outdir.exists() else []
    return _OutdirResult(d, outdir, manifest, files)


class _OutdirResult:
    def __init__(self, tmp, outdir, manifest, files):
        self._tmp = tmp          # keep the TemporaryDirectory alive
        self.outdir = outdir
        self.manifest = manifest
        self.files = files

    def read(self, name: str) -> bytes:
        return (self.outdir / name).read_bytes()

    def cleanup(self):
        self._tmp.cleanup()


def call_fn(module_name: str, fn_name: str, src: str, **kwargs):
    """In-process: import ``bat_<module>`` and call its transform function,
    returning ``(new_text, stats)``.  Faster than a subprocess and lets a test
    reach helpers, but skips the run_tool harness (encoding / newline / stats
    augmentation) — use :func:`run_cli` for those."""
    mod = importlib.import_module(module_name)
    return getattr(mod, fn_name)(src, **kwargs)


def assert_idempotent(tc: unittest.TestCase, fn, src: str, **kwargs):
    """Every wrapper transform is a single pass; running it on its own output
    must be a no-op (``changed == 0`` and identical text)."""
    out1, _ = fn(src, **kwargs)
    out2, stats2 = fn(out1, **kwargs)
    tc.assertEqual(stats2.get('changed', 0), 0,
                   f'{fn.__module__}.{fn.__name__} not idempotent: changed={stats2.get("changed")}')
    tc.assertEqual(out1, out2,
                   f'{fn.__module__}.{fn.__name__} not idempotent: output changed on 2nd run')


class PipelineMixin(unittest.TestCase):
    """Base class for end-to-end tests: gives each test a fresh tempdir and a
    ``run_pipeline`` that chains wrappers output->input."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.tmp = Path(self._d.name)

    def run_pipeline(self, steps: list[tuple[str, list[str]]], src: str, *, tag: str = 'p'):
        """Run *steps* once, threading each wrapper's --output into the next
        wrapper's --input.  Returns ``(stats_list, paths)`` where ``paths[0]``
        is the original source and ``paths[i]`` is the output of step ``i-1``."""
        paths = [self.tmp / f'{tag}{i}.cmd' for i in range(len(steps) + 1)]
        paths[0].write_bytes(src.encode('utf-8'))
        stats: list[dict] = []
        for i, (name, extra) in enumerate(steps):
            r = subprocess.run(
                [sys.executable, str(TOOL_DIR / name),
                 '--input', str(paths[i]), '--output', str(paths[i + 1]), *extra],
                capture_output=True, text=True, check=True,
            )
            stats.append(json.loads(r.stdout))
        return stats, paths

    def run_to_fixpoint(self, steps: list[tuple[str, list[str]]], src: str,
                        *, max_iters: int = 8):
        """Run *steps* repeatedly until every wrapper in a full pass reports
        ``changed:0`` (or *max_iters* is hit).  Returns ``(final_text, iters)``."""
        text = src
        for it in range(1, max_iters + 1):
            stats, paths = self.run_pipeline(steps, text, tag=f'i{it}_')
            text = paths[-1].read_text('utf-8')
            if all(s.get('changed', 0) == 0 for s in stats):
                return text, it
        return text, max_iters

"""Variable environment simulation for Batch deobfuscation passes.

Three-state lattice per variable, mirroring vbs_propagate_constants' model:
  - Known(str)  the value is a statically-resolved constant string
  - Unknown     assigned, but from something the analysis can't resolve
                (command output, `set /p`, an argument, `%random%`, ...)
  - Unset       never assigned on any path that reaches here

The Unset/Unknown distinction is load-bearing and is NOT the same as "both
just resolve to empty" even though, per the empirically-verified expansion
semantics, cmd.exe itself resolves both an unset variable AND a value that
happens to be the empty string identically at runtime. The distinction
matters to the ANALYSIS: a var that's merely Unknown must never be folded to
"" (that would be silently wrong if the real runtime value turns out
non-empty), whereas a var proven Unset really does contribute "" and passes
CAN fold it -- see resolve_read() below, which returns "" for Unset but None
(refuse) for Unknown.

setlocal/endlocal push/pop a scope. A pushed scope inherits the parent's
current bindings (batch variables are not block-scoped the way PowerShell
scriptblocks are -- setlocal scopes the whole *environment*, restoring it
wholesale on endlocal). EnableDelayedExpansion/DisableDelayedExpansion is
itself scoped exactly like a variable, restored on endlocal, per empirical
verification (Q7/Q8 above).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto


class VState(Enum):
    KNOWN = auto()
    UNKNOWN = auto()
    UNSET = auto()


@dataclass(slots=True)
class VValue:
    state: VState
    value: str | None = None   # only meaningful when state is KNOWN

    @staticmethod
    def known(v: str) -> 'VValue':
        return VValue(VState.KNOWN, v)

    @staticmethod
    def unknown() -> 'VValue':
        return VValue(VState.UNKNOWN)

    @staticmethod
    def unset() -> 'VValue':
        return VValue(VState.UNSET)


@dataclass
class Scope:
    vars: dict[str, VValue] = field(default_factory=dict)
    delayed_expansion: bool = False


# Variables Windows always defines at real runtime, but which this script
# never assigns itself -- NOT the same thing as "unset". Confusing the two
# is a real correctness hazard, not just a completeness gap: folding
# %SystemRoot% to '' produces actively MISLEADING output (`cd /d ""`
# instead of the true `cd /d "C:\Windows"`-shaped reality), the opposite of
# this toolkit's "refuse rather than guess" stance. Seeded as UNKNOWN so
# every fold pass correctly leaves a reference to one of these untouched
# (preserved as literal %SystemRoot%-style text) rather than confidently
# emptying it. %0/%1-%9/%* (the script's own invocation path/arguments) are
# exactly the same kind of "real at runtime, unknowable from source alone"
# value and are seeded the same way.
AMBIENT_UNKNOWN_VARS = frozenset(n.upper() for n in (
    'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'PROGRAMW6432', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'PUBLIC', 'USERPROFILE',
    'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'HOMEDRIVE', 'HOMEPATH',
    'USERNAME', 'USERDOMAIN', 'COMPUTERNAME', 'PATH', 'PATHEXT',
    'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER',
    'OS', 'SYSTEMDRIVE', 'CD', 'DATE', 'TIME', 'RANDOM', 'ERRORLEVEL',
    'CMDCMDLINE', 'CMDEXTVERSION',
)) | {f'@ARG{i}' for i in range(10)} | {'@ARGSTAR'}


class Env:
    """Case-insensitive variable environment with a setlocal/endlocal stack."""

    def __init__(self, *, delayed_expansion: bool = False, seed_ambient: bool = True) -> None:
        self._stack: list[Scope] = [Scope(delayed_expansion=delayed_expansion)]
        if seed_ambient:
            for name in AMBIENT_UNKNOWN_VARS:
                self._stack[0].vars[name] = VValue.unknown()

    # ------------------------------------------------------------------
    # scope management
    # ------------------------------------------------------------------

    def setlocal(self, *, enable_delayed: bool | None = None) -> None:
        cur = self._stack[-1]
        nxt = Scope(vars=dict(cur.vars), delayed_expansion=cur.delayed_expansion)
        if enable_delayed is not None:
            nxt.delayed_expansion = enable_delayed
        self._stack.append(nxt)

    def endlocal(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
        # endlocal with no matching setlocal is a runtime no-op in cmd.exe;
        # never raises here so callers can simulate malformed/truncated scripts.

    @property
    def delayed_expansion(self) -> bool:
        return self._stack[-1].delayed_expansion

    @delayed_expansion.setter
    def delayed_expansion(self, v: bool) -> None:
        self._stack[-1].delayed_expansion = v

    # ------------------------------------------------------------------
    # variable access
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(name: str) -> str:
        return name.upper()

    def get(self, name: str) -> VValue:
        return self._stack[-1].vars.get(self._norm(name), VValue.unset())

    def set_known(self, name: str, value: str) -> None:
        self._stack[-1].vars[self._norm(name)] = VValue.known(value)

    def set_unknown(self, name: str) -> None:
        self._stack[-1].vars[self._norm(name)] = VValue.unknown()

    def unset(self, name: str) -> None:
        self._stack[-1].vars[self._norm(name)] = VValue.unset()

    def resolve_read(self, name: str) -> str | None:
        """Value to substitute for a read of *name*, or None if it can't be
        statically resolved (VState.UNKNOWN). VState.UNSET resolves to ''."""
        v = self.get(name)
        if v.state == VState.KNOWN:
            return v.value
        if v.state == VState.UNSET:
            return ''
        return None

    def snapshot(self) -> dict[str, VValue]:
        """Shallow copy of the current scope's bindings, for save/restore
        around control-flow simulation (e.g. speculative if-branch walks)."""
        return dict(self._stack[-1].vars)

    def restore(self, snap: dict[str, VValue]) -> None:
        self._stack[-1].vars = dict(snap)

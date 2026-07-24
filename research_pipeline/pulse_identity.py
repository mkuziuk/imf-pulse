"""Canonical, backward-compatible pulse date/index identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


MAX_PULSE_INDEX = 9999
PULSE_BASENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<index>[1-9]\d{0,3}))?\.md$"
)


@dataclass(frozen=True)
class PulseIdentity:
    date: str
    index: int
    legacy: bool

    @property
    def basename(self) -> str:
        return f"{self.date}.md" if self.legacy else f"{self.date}-{self.index}.md"

    @property
    def pulse_id(self) -> str:
        return f"pulse-{self.date}" if self.legacy else f"pulse-{self.date}-{self.index}"


def parse_pulse_path(value: str, *, directory: str = "content/pulses") -> PulseIdentity | None:
    """Parse a safe pulse path; date-only legacy files are index one."""

    path = PurePosixPath(value)
    if path.parent.as_posix() != directory:
        return None
    match = PULSE_BASENAME_RE.fullmatch(path.name)
    if match is None:
        return None
    raw_index = match.group("index")
    return PulseIdentity(
        date=match.group("date"),
        index=int(raw_index) if raw_index is not None else 1,
        legacy=raw_index is None,
    )


def indexed_pulse_path(run_date: str, pulse_index: int, *, directory: str = "content/pulses") -> str:
    if not 1 <= pulse_index <= MAX_PULSE_INDEX:
        raise ValueError("pulse index is outside the supported range")
    return f"{directory}/{run_date}-{pulse_index}.md"


def indexed_pulse_id(run_date: str, pulse_index: int) -> str:
    if not 1 <= pulse_index <= MAX_PULSE_INDEX:
        raise ValueError("pulse index is outside the supported range")
    return f"pulse-{run_date}-{pulse_index}"

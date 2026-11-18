"""Collects check results, prints them as they happen and renders the final summary."""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"
    INFO = "INFO"


_COLORS = {
    Status.PASS: "\033[32m",
    Status.WARN: "\033[33m",
    Status.FAIL: "\033[31;1m",
    Status.SKIP: "\033[90m",
    Status.INFO: "\033[36m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"

# How many sub-items (failing files etc.) to print per check before truncating.
_MAX_ITEMS = 25


@dataclass
class Check:
    section: str
    name: str
    status: Status
    detail: str = ""
    items: list[str] = field(default_factory=list)


class Report:
    def __init__(self, verbose: bool = False, color: bool | None = None):
        self.verbose = verbose
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = color
        self.checks: list[Check] = []
        self.facts: dict[str, str] = {}
        self._section = "General"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ output
    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    def section(self, title: str) -> None:
        self._section = title
        print()
        print(self._c(f"== {title} ", _BOLD) + self._c("=" * max(0, 70 - len(title)), _BOLD))

    def log(self, message: str) -> None:
        """Progress message, only shown in verbose mode."""
        if self.verbose:
            print(self._c(f"   .. {message}", "\033[90m"))

    def note(self, message: str) -> None:
        print(f"   {message}")

    def add(self, name: str, status: Status, detail: str = "", items: list[str] | None = None) -> Check:
        check = Check(self._section, name, status, detail, list(items or []))
        with self._lock:
            self.checks.append(check)
            tag = self._c(f"[{status.value}]", _COLORS[status])
            line = f"  {tag} {name}"
            if detail:
                line += f" — {detail}"
            print(line)
            limit = None if self.verbose else _MAX_ITEMS
            shown = check.items if limit is None else check.items[:limit]
            for item in shown:
                print(f"         - {item}")
            if limit is not None and len(check.items) > limit:
                print(f"         ... and {len(check.items) - limit} more (use -v to see all)")
        return check

    def ok(self, name: str, detail: str = "", items: list[str] | None = None) -> Check:
        return self.add(name, Status.PASS, detail, items)

    def warn(self, name: str, detail: str = "", items: list[str] | None = None) -> Check:
        return self.add(name, Status.WARN, detail, items)

    def fail(self, name: str, detail: str = "", items: list[str] | None = None) -> Check:
        return self.add(name, Status.FAIL, detail, items)

    def skip(self, name: str, detail: str = "") -> Check:
        return self.add(name, Status.SKIP, detail)

    def info(self, name: str, detail: str = "", items: list[str] | None = None) -> Check:
        return self.add(name, Status.INFO, detail, items)

    def fact(self, key: str, value: str) -> None:
        """Facts are key data points (signer, sha, counts) reused in the markdown summary."""
        self.facts[key] = value

    # ----------------------------------------------------------------- summary
    def count(self, status: Status) -> int:
        return sum(1 for c in self.checks if c.status == status)

    @property
    def failed(self) -> bool:
        return self.count(Status.FAIL) > 0

    def print_summary(self) -> None:
        print()
        print(self._c("== Summary " + "=" * 63, _BOLD))
        problems = [c for c in self.checks if c.status in (Status.FAIL, Status.WARN)]
        for c in problems:
            tag = self._c(f"[{c.status.value}]", _COLORS[c.status])
            print(f"  {tag} {c.section}: {c.name}" + (f" — {c.detail}" if c.detail else ""))
        counts = ", ".join(
            f"{self.count(s)} {s.value.lower()}" for s in (Status.PASS, Status.FAIL, Status.WARN, Status.SKIP)
        )
        print()
        if self.failed:
            verdict = self._c("RESULT: FAILED", _COLORS[Status.FAIL])
        elif self.count(Status.WARN):
            verdict = self._c("RESULT: PASSED WITH WARNINGS", _COLORS[Status.WARN])
        else:
            verdict = self._c("RESULT: PASSED", _COLORS[Status.PASS])
        print(f"  {verdict}  ({counts})")

    def to_markdown(self, title: str, highlights: list[str]) -> str:
        lines = [f"# {title}", ""]
        if self.failed:
            lines.append(f"**Result: FAILED** — {self.count(Status.FAIL)} failure(s), {self.count(Status.WARN)} warning(s)")
        elif self.count(Status.WARN):
            lines.append(f"**Result: PASSED WITH WARNINGS** — {self.count(Status.WARN)} warning(s)")
        else:
            lines.append("**Result: PASSED**")
        lines.append("")
        if highlights:
            lines += ["## Summary (suitable for the vote reply)", ""]
            lines += [f"- {h}" for h in highlights]
            lines.append("")
        problems = [c for c in self.checks if c.status in (Status.FAIL, Status.WARN)]
        if problems:
            lines += ["## Failures and warnings", ""]
            for c in sorted(problems, key=lambda c: c.status != Status.FAIL):
                detail = f" — {c.detail}" if c.detail else ""
                lines.append(f"- **{c.status.value}** {c.section}: {c.name}{detail}")
                for item in c.items[:10]:
                    lines.append(f"    - `{item}`")
                if len(c.items) > 10:
                    lines.append(f"    - ... and {len(c.items) - 10} more")
            lines.append("")
        lines += ["## All checks", ""]
        section = None
        for c in self.checks:
            if c.section != section:
                section = c.section
                lines += ["", f"### {section}", ""]
            detail = f" — {c.detail}" if c.detail else ""
            lines.append(f"- **{c.status.value}** {c.name}{detail}")
            for item in c.items[:200]:
                lines.append(f"    - `{item}`")
            if len(c.items) > 200:
                lines.append(f"    - ... and {len(c.items) - 200} more")
        lines.append("")
        return "\n".join(lines)

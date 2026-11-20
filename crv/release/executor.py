"""Runs (or, in a dry run, only records) the commands of a release step.

Unlike the official scripts' `execute`, a failing command stops the step with its real exit status,
secrets never appear on screen or in logs, and a dry run lists every command, uploads included.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class StepFailed(Exception):
    def __init__(self, description: str, returncode: int, tail: list[str]):
        super().__init__(f"{description} failed with exit status {returncode}")
        self.description = description
        self.returncode = returncode
        self.tail = tail


class Wrapper(Protocol):
    """Turns a command into the host command line that runs it (e.g. inside the release container)."""

    def wrap(self, cmd: list[str], cwd: Path | None, env: dict[str, str], signing: bool) -> list[str]: ...


@dataclass
class Planned:
    description: str
    where: str  # "host" or "container"
    command: str  # shell-quoted, secrets masked
    outward: bool


@dataclass
class Executor:
    dry_run: bool
    log: Path
    container: Wrapper | None = None
    secrets: list[str] = field(default_factory=list)  # values masked in everything shown or logged
    echo: bool = True
    planned: list[Planned] = field(default_factory=list)

    def mask(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "********")
        return text

    def run(
        self,
        cmd: list[str],
        description: str,
        *,
        where: str = "host",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        outward: bool = False,
        signing: bool = False,
        capture: bool = False,
        always: bool = False,
    ) -> str | None:
        """Runs cmd and returns its output when capture is set.

        In a dry run nothing is executed except read-only commands marked always=True (e.g. `git
        rev-parse`), so later commands can still be shown with real values. Returns None for skipped ones.
        """
        # the environment a command gets is part of what it does (BRANCHES=..., GRADLE_USER_HOME=...)
        shown_env = [f"{'JAVA_HOME' if k == 'CRV_JAVA_HOME' else k}={shlex.quote(v)}" for k, v in (env or {}).items()]
        shown = self.mask(" ".join(shown_env + [shlex.join(cmd)]))
        entry = Planned(description, where, shown + (f"    (in {cwd})" if cwd else ""), outward)
        self.planned.append(entry)
        if self.dry_run and not always:
            if self.echo:
                marker = "OUTWARD " if outward else ""
                print(f"   [dry run] {marker}{description}\n             {where}$ {entry.command}")
            return None
        full = self.container.wrap(cmd, cwd, env or {}, signing) if where == "container" and self.container else cmd
        run_env = {**os.environ, **(env or {})} if where == "host" else None
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as out:
            out.write(f"\n### {description}\n{where}$ {entry.command}\n")
            out.flush()
            if self.echo:
                print(f"   {'OUTWARD: ' if outward else ''}{description}")
            proc = subprocess.run(
                full,
                cwd=cwd if where == "host" else None,
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            output = self.mask(proc.stdout or "")
            out.write(output)
            out.write(f"\n[exit {proc.returncode}]\n")
        if proc.returncode != 0:
            raise StepFailed(description, proc.returncode, output.strip().splitlines()[-20:])
        return output if capture else ""

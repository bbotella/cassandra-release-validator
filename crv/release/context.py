"""What a release step gets to work with."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from ..projects import Project
from .config import Endpoints
from .executor import Executor
from .state import ReleaseState


@dataclass
class ReleaseContext:
    project: Project
    version: str
    state: ReleaseState
    endpoints: Endpoints
    executor: Executor
    args: argparse.Namespace
    notes: list[str] = field(default_factory=list)  # shown after the step: what you do next

    @property
    def dry_run(self) -> bool:
        return self.executor.dry_run

    @property
    def tag(self) -> str:
        return self.project.rc_tag(self.version)

    @property
    def workdir(self) -> Path:
        return self.endpoints.root / "work"

    def run(self, cmd: list[str], description: str, **kw) -> str | None:
        return self.executor.run(cmd, description, **kw)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def fresh_dir(self, path: Path) -> None:
        """Removes a work directory so a step can recreate it. The delete runs in the release container: on
        macOS the podman VM's file cache keeps a directory deleted on the Mac side, and the next container
        then fails to create files in it ("Can't create directory ... No such file or directory")."""
        if path.exists():
            self.run(["rm", "-rf", str(path)], f"remove {path.name}/ from an earlier run", where="container",
                     always=True)
        path.parent.mkdir(parents=True, exist_ok=True)

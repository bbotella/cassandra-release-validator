"""The per-release state file: which steps ran, what they produced, and the attempt number."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class StepRecord:
    status: str  # "done" or "failed"
    at: str
    summary: str = ""
    log: str = ""
    attempt: int = 1


@dataclass
class ReleaseState:
    project: str
    version: str
    rehearsal: bool
    attempt: int = 1  # 2 after a re-roll: "[VOTE] ... - 2nd attempt"
    steps: dict[str, StepRecord] = field(default_factory=dict)
    data: dict = field(default_factory=dict)  # sha1, staging_repos, svn revisions, vote thread, ...
    path: Path | None = None

    # ------------------------------------------------------------------- io
    @classmethod
    def load(cls, path: Path, project: str, version: str, rehearsal: bool) -> "ReleaseState":
        if path.exists():
            raw = json.loads(path.read_text())
            state = cls(raw["project"], raw["version"], raw.get("rehearsal", False), raw.get("attempt", 1),
                        {k: StepRecord(**v) for k, v in raw.get("steps", {}).items()}, raw.get("data", {}), path)
            if (state.project, state.version, state.rehearsal) != (project, version, rehearsal):
                raise ValueError(f"{path} belongs to {state.project} {state.version} (rehearsal={state.rehearsal})")
            return state
        return cls(project, version, rehearsal, path=path)

    def save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = asdict(self)
        raw.pop("path")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- steps
    def done(self, step: str) -> bool:
        record = self.steps.get(step)
        return record is not None and record.status == "done" and record.attempt == self.attempt

    def record(self, step: str, status: str, summary: str = "", log: Path | None = None) -> None:
        self.steps[step] = StepRecord(status, time.strftime("%Y-%m-%d %H:%M:%S"), summary, str(log or ""), self.attempt)
        self.save()

    def reset_for_reroll(self, keep: tuple[str, ...]) -> None:
        """Starts a new attempt; only the steps in keep (e.g. preflight) stay valid."""
        self.attempt += 1
        for name in keep:
            if name in self.steps:
                self.steps[name].attempt = self.attempt
        for key in ("staging_repos", "svn_revisions", "vote_thread", "sha1"):
            self.data.pop(key, None)
        self.save()


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"

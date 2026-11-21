"""What every smoke test module gets: the context, the engine, the shared servers and a run directory."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ...context import Context
from . import maven_consumer
from .engine import Engine
from .server import Servers


@dataclass
class SmokeRun:
    ctx: Context
    engine: Engine
    servers: Servers
    dir: Path  # this run's own directory under <workdir>/smoke/
    tests: list[str]
    passed: list[str] = field(default_factory=list)  # short descriptions for the vote reply summary

    @property
    def timeout(self) -> int:
        return self.ctx.args.smoke_timeout

    @property
    def report(self):
        return self.ctx.report

    def wants(self, test: str) -> bool:
        return test in self.tests

    def log(self, name: str) -> Path:
        return self.dir / f"{name}.log"

    def fresh(self, name: str) -> Path:
        d = self.dir / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        return d

    def copy_files(self, name: str, files: list[Path]) -> Path:
        d = self.fresh(name)
        for f in files:
            shutil.copy2(f, d / f.name)
        return d


def release_files(ctx: Context, pattern: str) -> list[Path]:
    """Downloaded files (dist or published package repositories) whose relative path matches pattern."""
    files = {**ctx.dist_files, **ctx.package_files}
    return [path for rel, path in sorted(files.items()) if re.search(pattern, rel)]


def options_file_lines(path: Path) -> list[str]:
    """The options in a jvm*.options file (one per line, # comments)."""
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def provenance_report(run: SmokeRun, m2: Path, label: str) -> bool:
    """Reports where the release's artifacts were resolved from; True when that is fine."""
    r = run.report
    elsewhere, snapshots = maven_consumer.provenance(run.ctx, m2)
    if snapshots:
        r.fail(f"{label}: no SNAPSHOT dependencies", f"{len(snapshots)} SNAPSHOT versions resolved", snapshots)
    if elsewhere:
        if run.ctx.args.maven_base:
            r.info(f"{label}: provenance", f"some artifacts of this release came straight from {run.ctx.args.maven_base} "
                                           "(the vote email didn't list them, so they weren't verified here)", elsewhere)
        else:
            r.fail(f"{label}: provenance", "artifacts of this release were resolved from outside the staged "
                                           "repositories (not part of the staged release?)", elsewhere)
    if not snapshots and not elsewhere:
        r.ok(f"{label}: provenance", "this release's artifacts all came from the verified staged copy; no SNAPSHOTs")
    return not snapshots and (not elsewhere or bool(run.ctx.args.maven_base))

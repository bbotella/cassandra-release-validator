"""Helpers shared by the release steps."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .context import ReleaseContext


def source_dir(ctx: ReleaseContext) -> Path:
    """Your checkout of the project: --source, or ~/dev/<repo>."""
    path = Path(ctx.args.source).expanduser() if ctx.args.source else Path.home() / "dev" / ctx.project.repo
    return path.resolve()


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def push_target(ctx: ReleaseContext) -> str:
    """Where tags are pushed: the remote of your checkout that points at gitbox (so your git credentials
    apply), else the gitbox URL itself. In a rehearsal, the sandbox's bare repository."""
    if ctx.endpoints.rehearsal:
        return ctx.endpoints.git_url
    src = source_dir(ctx)
    for line in git("remote", "-v", cwd=src).splitlines():
        name, url, *_ = line.split()
        if f"gitbox.apache.org/repos/asf/{ctx.project.repo}" in url:
            return name
    return ctx.endpoints.git_url


def svn_where(ctx: ReleaseContext) -> str:
    """svn runs on this machine for real releases (your cached ASF credentials) and in the container for
    rehearsals (the sandbox repository needs no credentials)."""
    return "container" if ctx.endpoints.rehearsal or not shutil.which("svn") else "host"


def dist_dev_dir(ctx: ReleaseContext) -> str:
    return f"{ctx.endpoints.dist_dev}{ctx.project.dist_subdir}{ctx.version}"


def dist_release_parent(ctx: ReleaseContext) -> str:
    return f"{ctx.endpoints.dist_release}{ctx.project.dist_subdir}".rstrip("/")


def label(ctx: ReleaseContext) -> str:
    """'Cassandra Analytics' — as the official commit messages and mails name the project."""
    return ctx.project.display.removeprefix("Apache ")

"""State shared by all checks for one validation run."""

from __future__ import annotations

import argparse
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .gpg import Keyring, SigResult
from .projects import Project
from .report import Report
from .vote_email import MavenRef

_LOCK = threading.Lock()

@dataclass
class Context:
    project: Project
    version: str
    sha1: str | None
    tag: str
    dist_url: str
    staging_repos: list[str]
    maven_refs: list[MavenRef]
    workdir: Path
    report: Report
    keyring: Keyring
    args: argparse.Namespace

    # Filled in while checks run
    dist_files: dict[str, Path] = field(default_factory=dict)  # relative path -> local file
    package_files: dict[str, Path] = field(default_factory=dict)  # deb/rpm repository files (staged or published)
    maven_repos: set[str] = field(default_factory=set)  # <workdir>/maven/<id> folders this run downloaded and checked
    git_commit: str | None = None  # commit the tag resolves to
    git_dir: Path | None = None  # bare-ish local repo holding the fetched commit
    src_root: Path | None = None  # extracted source tarball top directory
    bin_root: Path | None = None  # extracted binary tarball top directory
    signers: Counter = field(default_factory=Counter)  # signer description -> number of signatures
    signer_fingerprints: dict[str, tuple[str, str]] = field(default_factory=dict)  # signer -> (primary, signing key)
    highlights: list[str] = field(default_factory=list)  # bullet points for the vote reply summary

    def record_signature(self, res: SigResult) -> None:
        if res.ok:
            with _LOCK:
                self.signers[res.signer] += 1
                self.signer_fingerprints[res.signer] = (res.fingerprint, res.signing_fingerprint)

    def highlight(self, text: str) -> None:
        self.highlights.append(text)

    @property
    def jobs(self) -> int:
        return self.args.jobs

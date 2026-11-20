"""Where a release reads and writes: the real ASF endpoints, or a local rehearsal sandbox."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..gpg import KEYS_URL
from ..projects import DIST_DEV, DIST_RELEASE, GITBOX, NEXUS_REPOS, Project

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassandra-release-validator"
RELEASES_ROOT = CACHE / "releases"
ARTIFACTORY = "https://apache.jfrog.io/artifactory/"


@dataclass(frozen=True)
class Endpoints:
    rehearsal: bool
    root: Path  # this release's directory: state, logs, checkouts (and the sandbox when rehearsing)
    git_url: str  # where the fresh clone comes from and where tags are pushed (gitbox)
    dist_dev: str  # svn URL of dist/dev/cassandra/ (with trailing /)
    dist_release: str  # svn URL of dist/release/cassandra/
    nexus_repos: str  # base of the staging repositories (…/orgapachecassandra-N/)
    deploy_url: str | None  # Maven deploy URL override; None: the project's own config (gradle.properties, settings.xml)
    artifactory: str
    keys: str  # KEYS used to check signatures (URL, or a local file when rehearsing)
    mail_dir: Path

    @property
    def sandbox(self) -> Path:
        return self.root / "sandbox"


def endpoints(project: Project, version: str, rehearsal: bool) -> Endpoints:
    if not rehearsal:
        return Endpoints(
            rehearsal=False,
            root=RELEASES_ROOT / f"{project.key}-{version}",
            git_url=project.git_url,
            dist_dev=DIST_DEV,
            dist_release=DIST_RELEASE,
            nexus_repos=NEXUS_REPOS,
            deploy_url=None,
            artifactory=ARTIFACTORY,
            keys=KEYS_URL,
            mail_dir=Path(os.environ.get("CRV_MAIL_DIR", Path.home() / "Mail")),
        )
    root = RELEASES_ROOT / f"rehearsal-{project.key}-{version}"
    sandbox = root / "sandbox"
    www = sandbox / "www"  # served over HTTP while steps that read it run (see sandbox.py)
    return Endpoints(
        rehearsal=True,
        root=root,
        git_url=f"file://{sandbox / 'git' / (project.repo + '.git')}",
        dist_dev=f"file://{sandbox / 'svn'}/dev/cassandra/",
        dist_release=f"file://{sandbox / 'svn'}/release/cassandra/",
        nexus_repos=f"file://{www / 'repositories'}/",
        deploy_url=f"file://{www / 'repositories' / 'orgapachecassandra-1'}",
        artifactory=f"file://{www / 'artifactory'}/",
        keys=str(sandbox / "KEYS"),
        mail_dir=root / "mail",
    )

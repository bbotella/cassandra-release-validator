"""What each project's release candidate is expected to look like.

The conventions mirror the release scripts in apache/cassandra-builds:
  cassandra-release/prepare_release.sh
  cassandra-sidecar-release/prepare_release.sh
  cassandra-analytics-release/prepare_release.sh
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DIST_DEV = "https://dist.apache.org/repos/dist/dev/cassandra/"
DIST_RELEASE = "https://dist.apache.org/repos/dist/release/cassandra/"
GITBOX = "https://gitbox.apache.org/repos/asf/"
NEXUS_REPOS = "https://repository.apache.org/content/repositories/"
STAGING_PREFIX = "orgapachecassandra-"

SIGNATURE_SUFFIXES = (".asc", ".sha256", ".sha512")


@dataclass(frozen=True)
class Project:
    key: str
    display: str  # as used in the vote email subject: "Apache Cassandra Sidecar"
    repo: str  # git repository name under gitbox / github
    dist_subdir: str  # sub directory under dist/{dev,release}/cassandra/
    tarball_prefix: str
    bin_tarball_suffix: str  # Cassandra: "-bin", others: ""
    final_tag_prefix: str  # tag created by finish_release.sh
    version_file: str
    version_pattern: str  # regex with one group capturing the version in version_file
    packages: str | None  # "cassandra" (apt + yum repos), "simple" (single .deb/.rpm), or None
    required_maven_artifacts: tuple[str, ...]  # regexes; at least one staged artifact must match each
    bin_required: tuple[str, ...]  # paths (relative to the tarball's top directory) that must exist
    smoke: str | None  # "cassandra", "sidecar" or None
    build_command: tuple[str, ...] | None

    # ----------------------------------------------------------------- naming
    def dist_url(self, version: str) -> str:
        return f"{DIST_DEV}{self.dist_subdir}{version}/"

    def released_dist_url(self, version: str) -> str:
        return f"{DIST_RELEASE}{self.dist_subdir}{version}/"

    @property
    def git_url(self) -> str:
        return f"{GITBOX}{self.repo}.git"

    @property
    def github_url(self) -> str:
        return f"https://github.com/apache/{self.repo}"

    def rc_tag(self, version: str) -> str:
        return f"{version}-tentative"

    def final_tag(self, version: str) -> str:
        return f"{self.final_tag_prefix}{version}"

    def src_tarball(self, version: str) -> str:
        return f"{self.tarball_prefix}-{version}-src.tar.gz"

    def bin_tarball(self, version: str) -> str:
        return f"{self.tarball_prefix}-{version}{self.bin_tarball_suffix}.tar.gz"

    def expected_dist_files(self, version: str, noboolean: bool = True) -> list[str]:
        """Every file prepare_release.sh is expected to stage under dist/dev for this version."""
        files: list[str] = []
        for tarball in (self.src_tarball(version), self.bin_tarball(version)):
            files.append(tarball)
            files += [tarball + s for s in SIGNATURE_SUFFIXES]
        if self.packages == "simple":
            deb = f"debian/{self.tarball_prefix}_{version}_all.deb"
            rpm = f"redhat/{self.tarball_prefix}-{version}.noarch.rpm"
            for pkg in (deb, rpm):
                files.append(pkg)
                files += [pkg + s for s in SIGNATURE_SUFFIXES]
        elif self.packages == "cassandra":
            files += cassandra_package_files(version, noboolean)
        return files


def deb_version(version: str) -> str:
    """Debian/RPM versions use '~' for pre-releases so that 6.0~alpha2 sorts before 6.0."""
    return version.replace("-", "~", 1)


def debian_series(version: str) -> str:
    """The apt distribution codename, e.g. 5.0.9 -> 50x, 6.0-alpha2 -> 60x."""
    short = version.split("-", 1)[0]
    major, minor = short.split(".")[:2]
    return f"{major}{minor}x"


def cassandra_package_files(version: str, noboolean: bool) -> list[str]:
    deb = deb_version(version)
    series = debian_series(version)
    pool = "debian/pool/main/c/cassandra/"
    debs = [f"cassandra_{deb}_all.deb", f"cassandra-tools_{deb}_all.deb", f"cassandra_{deb}.dsc", f"cassandra_{deb}.tar.gz"]
    files = [f"debian/{f}" for f in debs] + [pool + f for f in debs]
    files += [f"debian/cassandra_{deb}_amd64.changes", f"debian/cassandra_{deb}_amd64.buildinfo"]
    files += [f"debian/dists/{series}/{f}" for f in ("InRelease", "Release", "Release.gpg")]
    rpms = [f"cassandra-{deb}-1.noarch.rpm", f"cassandra-tools-{deb}-1.noarch.rpm", f"cassandra-{deb}-1.src.rpm"]
    for base in ["redhat/"] + (["redhat/noboolean/"] if noboolean else []):
        files += [base + f for f in rpms]
        files += [base + "repodata/repomd.xml", base + "repodata/repomd.xml.asc"]
    return files


CASSANDRA = Project(
    key="cassandra",
    display="Apache Cassandra",
    repo="cassandra",
    dist_subdir="",
    tarball_prefix="apache-cassandra",
    bin_tarball_suffix="-bin",
    final_tag_prefix="cassandra-",
    version_file="build.xml",
    version_pattern=r'<property\s+name="base\.version"\s+value="([^"]+)"',
    packages="cassandra",
    required_maven_artifacts=(r"^cassandra-all$",),
    bin_required=("bin/cassandra", "bin/nodetool", "conf/cassandra.yaml", "lib/apache-cassandra-{version}.jar"),
    smoke="cassandra",
    build_command=("ant", "jar", "-Dno-checkstyle=true", "-Drat.skip=true", "-Dant.gen-doc.skip=true"),
)

SIDECAR = Project(
    key="sidecar",
    display="Apache Cassandra Sidecar",
    repo="cassandra-sidecar",
    dist_subdir="cassandra-sidecar/",
    tarball_prefix="apache-cassandra-sidecar",
    bin_tarball_suffix="",
    final_tag_prefix="cassandra-sidecar-",
    version_file="gradle.properties",
    version_pattern=r"(?m)^version=(\S+)",
    packages="simple",
    required_maven_artifacts=(r"^sidecar-server$", r"^sidecar-client$"),
    bin_required=("bin/cassandra-sidecar", "conf/sidecar.yaml"),
    smoke="sidecar",
    build_command=("./gradlew", "--no-daemon", "assemble"),
)

ANALYTICS = Project(
    key="analytics",
    display="Apache Cassandra Analytics",
    repo="cassandra-analytics",
    dist_subdir="cassandra-analytics/",
    tarball_prefix="apache-cassandra-analytics",
    bin_tarball_suffix="",
    final_tag_prefix="cassandra-analytics-",
    version_file="gradle.properties",
    version_pattern=r"(?m)^version=(\S+)",
    packages=None,
    required_maven_artifacts=(r"^cassandra-analytics-core_spark\d+_2\.\d+$", r"^cassandra-bridge_spark\d+_2\.\d+$"),
    bin_required=(),
    smoke=None,
    build_command=None,  # needs ./scripts/build-dependencies.sh (builds Cassandra from source): too heavy to automate
)

PROJECTS = {p.key: p for p in (CASSANDRA, SIDECAR, ANALYTICS)}

_ALIASES = {
    "": "cassandra",
    "cassandra": "cassandra",
    "sidecar": "sidecar",
    "cassandra-sidecar": "sidecar",
    "analytics": "analytics",
    "cassandra-analytics": "analytics",
}


def by_name(name: str) -> Project | None:
    key = _ALIASES.get(name.strip().lower())
    return PROJECTS.get(key) if key else None


def by_repo(repo: str) -> Project | None:
    for p in PROJECTS.values():
        if p.repo == repo:
            return p
    return None


VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.]+)?$")

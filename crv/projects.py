"""What each project's release candidate is expected to look like.

Cassandra, Sidecar and Analytics mirror the release scripts in apache/cassandra-builds:
  cassandra-release/prepare_release.sh
  cassandra-sidecar-release/prepare_release.sh
  cassandra-analytics-release/prepare_release.sh
The Python, GoCQL and Java drivers mirror what their release managers stage (see their vote emails); they are
not built by cassandra-builds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DIST_DEV = "https://dist.apache.org/repos/dist/dev/cassandra/"
DIST_RELEASE = "https://dist.apache.org/repos/dist/release/cassandra/"
DEBIAN_PUBLIC = "https://debian.cassandra.apache.org/"  # where finish_release.sh publishes Cassandra's .deb
REDHAT_PUBLIC = "https://redhat.cassandra.apache.org/"  # ... and the .rpm (per series: 50x/, 50x/noboolean/)
MAVEN_CENTRAL = "https://repo1.maven.org/maven2/"
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
    final_tag_format: str  # tag of the released version (finish_release.sh), e.g. "cassandra-{version}"
    version_file: str | None  # file at the tag that declares the version (None: versioned by tag only)
    version_pattern: str  # regex with one group capturing the version in version_file
    packages: str | None  # "cassandra" (apt + yum repos), "simple" (single .deb/.rpm), or None
    required_maven_artifacts: tuple[str, ...]  # regexes; at least one staged artifact must match each
    bin_required: tuple[str, ...]  # paths (relative to the tarball's top directory) that must exist
    smoke: str | None  # "cassandra", "sidecar" or None
    build_command: tuple[str, ...] | None
    # --- conventions that differ for the drivers
    rc_tag_format: str = "{version}-tentative"  # tag the vote is on
    src_tarball_format: str = "{prefix}-{version}-src.tar.gz"
    src_top_dir_format: str = "{prefix}-{version}-src"
    has_bin_tarball: bool = True
    maven: bool = True  # stages artifacts in Nexus
    changes_file: str = "CHANGES.txt"
    changes_heading: str = r"^{v}\s*$"  # regex for the version's heading; {v} is the escaped version
    wheels_dir: str | None = None  # Python wheels (convenience binaries) staged in this sub directory
    unsigned_ok: str | None = None  # regex of artifacts that are known to be staged without signatures
    src_generated: str | None = None  # regex of files the source packaging tool adds (not in git)
    # regex of this project's artifactIds, to find them in a large repository (Maven Central) when the vote
    # email names only a staging repository and --maven-base points elsewhere
    maven_discover: str | None = None

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
        return self.rc_tag_format.format(version=version)

    def final_tag(self, version: str) -> str:
        return self.final_tag_format.format(version=version)

    def src_tarball(self, version: str) -> str:
        return self.src_tarball_format.format(prefix=self.tarball_prefix, version=version)

    def src_top_dir(self, version: str) -> str:
        return self.src_top_dir_format.format(prefix=self.tarball_prefix, version=version)

    def bin_tarball(self, version: str) -> str | None:
        if not self.has_bin_tarball:
            return None
        return f"{self.tarball_prefix}-{version}{self.bin_tarball_suffix}.tar.gz"

    def changes_heading_re(self, version: str) -> re.Pattern:
        return re.compile(self.changes_heading.replace("{v}", re.escape(version)))

    def simple_package_release(self, version: str, files: list[str]) -> str | None:
        """The Debian/RPM release number in the staged package names, if any: Sidecar's packages are
        apache-cassandra-sidecar_0.5.0-1_all.deb since CASSSIDECAR-472, _0.4.0_all.deb before."""
        v = re.escape(version)
        prefix = re.escape(self.tarball_prefix)
        for f in files:
            m = re.fullmatch(rf"debian/{prefix}_{v}-(\d+)_all\.deb", f) or \
                re.fullmatch(rf"redhat/{prefix}-{v}-(\d+)\.noarch\.rpm", f)
            if m:
                return m.group(1)
        return None

    def expected_dist_files(self, version: str, noboolean: bool = True, released: bool = False,
                            package_release: str | None = None) -> list[str]:
        """Every file the release scripts are expected to stage under dist/dev for this version.

        For a released version (dist/release), the deb/rpm packages are not there: finish_release.sh
        moves them to debian.cassandra.apache.org / redhat.cassandra.apache.org.

        Python wheels are not listed: their names depend on the platforms built, so the dist check
        accepts whatever is in the wheels directory and checks them separately.
        """
        files: list[str] = []
        for tarball in (self.src_tarball(version), self.bin_tarball(version)):
            if tarball is None:
                continue
            files.append(tarball)
            files += [tarball + s for s in SIGNATURE_SUFFIXES]
        if released:
            return files
        if self.packages == "simple":
            rel = f"-{package_release}" if package_release else ""
            deb = f"debian/{self.tarball_prefix}_{version}{rel}_all.deb"
            rpm = f"redhat/{self.tarball_prefix}-{version}{rel}.noarch.rpm"
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
    final_tag_format="cassandra-{version}",
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
    final_tag_format="cassandra-sidecar-{version}",
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
    final_tag_format="cassandra-analytics-{version}",
    version_file="gradle.properties",
    version_pattern=r"(?m)^version=(\S+)",
    packages=None,
    required_maven_artifacts=(r"^cassandra-analytics-core_spark\d+_2\.\d+$", r"^cassandra-bridge_spark\d+_2\.\d+$"),
    bin_required=(),
    smoke=None,
    build_command=None,  # needs ./scripts/build-dependencies.sh (builds Cassandra from source): too heavy to automate
)

PYTHON_DRIVER = Project(
    key="python-driver",
    display="Apache Cassandra Python Driver",
    repo="cassandra-python-driver",
    dist_subdir="cassandra-python-driver/",
    tarball_prefix="cassandra_driver",
    bin_tarball_suffix="",
    final_tag_format="{version}",
    version_file="pyproject.toml",
    version_pattern=r'(?m)^version\s*=\s*"([^"]+)"',
    packages=None,
    required_maven_artifacts=(),
    bin_required=(),
    smoke=None,
    build_command=None,  # building runs setup.py (arbitrary code) and needs a C toolchain: left to the voter
    # The vote is on the final tag; "<version>rc1" only exists for the TestPyPI upload.
    rc_tag_format="{version}",
    src_tarball_format="{prefix}-{version}.tar.gz",  # a Python sdist
    src_top_dir_format="{prefix}-{version}",
    has_bin_tarball=False,
    maven=False,
    changes_file="CHANGELOG.rst",
    wheels_dir="wheels/",
    unsigned_ok=r"^wheels/[^/]+\.whl$",  # wheels have been staged without .asc/.sha* (3.30.1, 3.31.0)
    src_generated=r"^(PKG-INFO|[^/]+\.egg-info/.*|setup\.cfg)$",  # added or rewritten by setuptools' sdist
)

GOCQL_DRIVER = Project(
    key="gocql-driver",
    display="Apache Cassandra GoCQL Driver",
    repo="cassandra-gocql-driver",
    dist_subdir="cassandra-gocql-driver/",
    tarball_prefix="apache-cassandra-gocql-driver",
    bin_tarball_suffix="",
    final_tag_format="v{version}",
    version_file=None,  # Go modules are versioned by their tag
    version_pattern="",
    packages=None,
    required_maven_artifacts=(),
    bin_required=(),
    smoke=None,
    build_command=("go", "build", "./..."),
    rc_tag_format="v{version}-tentative",
    src_tarball_format="{prefix}-{version}.tar.gz",
    src_top_dir_format="{prefix}-{version}",
    has_bin_tarball=False,
    maven=False,
    changes_file="CHANGELOG.md",
    changes_heading=r"^#+\s*\[?{v}\]?(\s|$)",  # "## [2.2.0]"
)

JAVA_DRIVER = Project(
    key="java-driver",
    display="Apache Cassandra Java Driver",
    repo="cassandra-java-driver",
    dist_subdir="cassandra-java-driver/",
    tarball_prefix="apache-cassandra-java-driver",
    bin_tarball_suffix="",
    final_tag_format="{version}",
    version_file="pom.xml",
    version_pattern="pom",  # the root POM's own <version> (it has an org.apache:apache <parent>)
    packages=None,
    # 4.x publishes java-driver-*, the 3.x line cassandra-driver-*
    required_maven_artifacts=(r"^(java-driver-core|cassandra-driver-core)$",),
    bin_required=(),
    smoke=None,
    build_command=("mvn", "-B", "-q", "-DskipTests", "install"),
    rc_tag_format="{version}",  # the vote is on the final tag
    src_tarball_format="{prefix}-{version}-source.tar.gz",
    src_top_dir_format="{prefix}-{version}-source",
    has_bin_tarball=False,
    changes_file="changelog/README.md",
    changes_heading=r"^#+\s*{v}\s*$",  # "### 4.19.4" (4.x), "## 3.13.0" (3.x)
    maven_discover=r"^(java-driver-|cassandra-driver-)",
)

PROJECTS = {p.key: p for p in (CASSANDRA, SIDECAR, ANALYTICS, PYTHON_DRIVER, GOCQL_DRIVER, JAVA_DRIVER)}

_ALIASES = {
    "": "cassandra",
    "cassandra": "cassandra",
    "sidecar": "sidecar",
    "cassandra-sidecar": "sidecar",
    "analytics": "analytics",
    "cassandra-analytics": "analytics",
    "python driver": "python-driver",
    "python-driver": "python-driver",
    "python": "python-driver",
    "cassandra-python-driver": "python-driver",
    "gocql driver": "gocql-driver",
    "gocql-driver": "gocql-driver",
    "gocql": "gocql-driver",
    "cassandra-gocql-driver": "gocql-driver",
    "java driver": "java-driver",
    "java-driver": "java-driver",
    "java": "java-driver",
    "cassandra-java-driver": "java-driver",
}


def by_name(name: str) -> Project | None:
    key = _ALIASES.get(re.sub(r"\s+", " ", name.strip().lower()))
    return PROJECTS.get(key) if key else None


def by_repo(repo: str) -> Project | None:
    for p in PROJECTS.values():
        if p.repo == repo:
            return p
    return None


VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.]+)?$")

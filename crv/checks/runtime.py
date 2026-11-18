"""Runtime checks in containers, one round per supported JDK (opt-in with --runtime).

This is the matrix of cassandra-check-release.sh (apache/cassandra-builds PR 32), for Apache Cassandra:
  build     ant artifacts from the source tarball            (eclipse-temurin:<jdk>-jdk)
  binary    start the binary tarball                         (eclipse-temurin:<jdk>-jdk)
  deb       install the .deb files and start Cassandra       (eclipse-temurin:<jdk>-jdk)
  apt       install from the apt repository, start           (eclipse-temurin:8-jdk, else debian:bullseye-slim)
  rpm       install the .rpm files and start Cassandra       (almalinux:9; also noboolean when staged)
  yum       install from the yum repository with gpgcheck,   (almalinux:9; also noboolean when staged)
            repo_gpgcheck, and start
A start counts as successful when Cassandra logs "Starting listening for CQL clients on", a build when ant
prints "BUILD SUCCESSFUL" (the same markers as the script).

Differences with the script, on purpose:
  * the JDKs come from java.supported in build.xml at the tag (4.0/4.1, which have no such property: 8, 11)
  * docker or podman, whichever works (--container-engine to choose)
  * apt uses a signed-by keyring made from KEYS instead of the removed apt-key
  * installs from the public repositories (--released) pin this exact version instead of the newest one
  * Go, which the script installs before the build, is downloaded for the container's architecture

Everything here runs code from the release, inside throwaway containers.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

from ..context import Context
from ..projects import DEBIAN_PUBLIC, REDHAT_PUBLIC, deb_version, debian_series
from .git import show_file

TESTS = ("build", "binary", "deb", "apt", "rpm", "yum")
CQL_READY = "Starting listening for CQL clients on"
BUILD_OK = "BUILD SUCCESSFUL"
KEYS_URL = "https://downloads.apache.org/cassandra/KEYS"
GO_VERSION = "1.24.5"  # the community script installs Go before building; kept for parity
HEAP = "HEAP_NEWSIZE=500m MAX_HEAP_SIZE=1g MAX_DIRECT_MEMORY_SIZE=1g"


# ------------------------------------------------------------------ settings
def supported_jdks(build_xml: str | None, version: str) -> list[str]:
    """JDKs to test: java.supported from build.xml, or the script's defaults for branches without it."""
    if build_xml:
        m = re.search(r'name="java\.supported"\s+value="([^"]+)"', build_xml)
        if m:
            return [j.strip() for j in m.group(1).split(",") if j.strip()]
    major = int(version.split(".")[0])
    return ["8"] if major <= 3 else ["8", "11"] if major == 4 else ["11", "17"]


def rpm_java(jdk: str) -> str:
    return "java-1.8.0-openjdk" if jdk == "8" else f"java-{jdk}-openjdk-devel"


def find_engine(preferred: str | None) -> tuple[str | None, str]:
    """A container engine whose daemon/VM answers, or (None, reason)."""
    reasons = []
    for name in ([preferred] if preferred else ["docker", "podman"]):
        path = shutil.which(name)
        if not path:
            reasons.append(f"{name}: not installed")
            continue
        probe = subprocess.run([path, "info"], capture_output=True, text=True, timeout=60)
        if probe.returncode == 0:
            return path, ""
        reasons.append(f"{name}: not running ({(probe.stderr or probe.stdout).strip().splitlines()[-1:] or ['?']})")
    return None, "; ".join(reasons)


# ------------------------------------------------------------ container jobs
def build_script(jdk: str, version: str) -> str:
    opt = " -Duse.jdk11=true" if jdk == "11" and version.startswith("4.") else ""
    return f"""
( apt-get -qq update && apt-get -qq install -y wget ant build-essential git python3 procps ) > /tmp/setup.log 2>&1 || {{ cat /tmp/setup.log; exit 1; }}
ARCH=$(dpkg --print-architecture)
( wget -q https://go.dev/dl/go{GO_VERSION}.linux-$ARCH.tar.gz && tar -C /usr/local -xzf go{GO_VERSION}.linux-$ARCH.tar.gz ) > /dev/null 2>&1
export PATH=$PATH:/usr/local/go/bin
cd /src && ant artifacts{opt}
"""


def binary_script() -> str:
    return f"""
( apt-get -qq update && apt-get -qq install -y python3 procps ) > /dev/null 2>&1
{HEAP} /cassandra/bin/cassandra -R -f
"""


def deb_script() -> str:
    # --force-depends: the image's JDK is not a dpkg package, so the javaNN-runtime dependency can't be met
    return f"""
( apt-get -qq update && apt-get -qq install -y python3 procps && dpkg --force-depends -i /debs/*.deb ) 2>&1 | tail -5
{HEAP} cassandra -R -f
"""


def apt_script(repo_url: str, series: str, pin: str | None) -> str:
    package = f"cassandra={pin} cassandra-tools={pin}" if pin else "cassandra"
    return f"""
set -e
( apt-get -qq update && apt-get -qq install -y curl gnupg ca-certificates ) > /dev/null 2>&1
curl -fsSL {KEYS_URL} -o /tmp/KEYS
export GNUPGHOME=$(mktemp -d)
gpg --batch --quiet --import /tmp/KEYS 2>/dev/null || true
gpg --batch --export > /usr/share/keyrings/cassandra.gpg
echo 'deb [signed-by=/usr/share/keyrings/cassandra.gpg] {repo_url} {series} main' > /etc/apt/sources.list.d/cassandra.list
apt-get -qq update 2>&1 | tail -5
apt-get install -y {package} 2>&1 | tail -5
set +e
{HEAP} cassandra -R -f
"""


def rpm_script(jdk: str) -> str:
    return f"""
( yum install -y -q procps-ng python3-pip {rpm_java(jdk)} && rpm -i --nodeps /rpms/*.noarch.rpm ) 2>&1 | tail -5
{HEAP} cassandra -R -f
"""


def yum_script(jdk: str, baseurl: str, pin: str | None) -> str:
    package = f"cassandra-{pin} cassandra-tools-{pin}" if pin else "cassandra"
    # LEGACY crypto policy: KEYS contains a legacy (SHA-1) third-party signature that RHEL 9+ rejects
    # otherwise (the same workaround as cassandra-check-release.sh; it affects every RHEL 9 user).
    return f"""
cat > /etc/yum.repos.d/cassandra.repo <<'EOF'
[cassandra]
name=Apache Cassandra
baseurl={baseurl}
gpgcheck=1
repo_gpgcheck=1
gpgkey={KEYS_URL}
EOF
( update-crypto-policies --set LEGACY && yum install -y -q {rpm_java(jdk)} && yum install -y {package} ) 2>&1 | tail -5
{HEAP} cassandra -R -f
"""


def run_container(engine: str, image: str, script: str, mounts: dict[Path, str], marker: str,
                  log: Path, timeout: int) -> bool:
    """Runs script in a fresh container; True as soon as marker is printed. The container is removed."""
    name = f"crv-{uuid.uuid4().hex[:12]}"
    cmd = [engine, "run", "--rm", "--name", name, "-i"]
    for host, target in mounts.items():
        cmd += ["-v", f"{host}:{target}"]
    cmd += [image, "timeout", str(timeout), "/bin/bash", "-c", script]
    log.parent.mkdir(parents=True, exist_ok=True)
    ok = False
    with open(log, "w") as out:
        out.write("$ " + " ".join(cmd[:-1]) + " <script>\n" + script + "\n----\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace")
        try:
            for line in proc.stdout:
                out.write(line)
                if marker in line:
                    ok = True
                    break
        finally:
            subprocess.run([engine, "rm", "-f", name], capture_output=True)  # stops and removes the container
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
    return ok


# ---------------------------------------------------------------------- run
def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Runtime checks in containers (cassandra-check-release.sh matrix)")
    if p.key != "cassandra":
        r.skip("Runtime checks", f"only Apache Cassandra has a runtime matrix (as in cassandra-check-release.sh), not {p.display}")
        return
    engine, why = find_engine(ctx.args.container_engine)
    if not engine:
        r.skip("Runtime checks", f"no usable container engine ({why}); start Docker or `podman machine start`")
        return
    wanted = [t.strip() for t in ctx.args.runtime_tests.split(",")] if ctx.args.runtime_tests else list(TESTS)
    unknown = [t for t in wanted if t not in TESTS]
    if unknown:
        r.fail("Runtime checks", f"unknown --runtime-tests {unknown} (choose from {', '.join(TESTS)})")
        return
    jdks = [j.strip() for j in ctx.args.jdks.split(",")] if ctx.args.jdks else supported_jdks(show_file(ctx, "build.xml"), ctx.version)
    r.info("Runtime matrix", f"JDKs {', '.join(jdks)}; tests {', '.join(wanted)}; engine {Path(engine).name}; "
           f"up to {ctx.args.runtime_timeout}s each")

    released = ctx.args.released
    series = debian_series(ctx.version)
    pin = deb_version(ctx.version) if released else None
    apt_url = DEBIAN_PUBLIC.rstrip("/") if released else ctx.dist_url + "debian"
    rpm_base = f"{REDHAT_PUBLIC}{series}/" if released else ctx.dist_url + "redhat/"
    files = ctx.package_files or ctx.dist_files
    root = ctx.workdir / "runtime"
    timeout = ctx.args.runtime_timeout
    passed: list[str] = []

    def record(name: str, ok: bool, log: Path) -> None:
        if ok:
            r.ok(name, f"log: {log}")
            passed.append(name)
        else:
            r.fail(name, f"no '{BUILD_OK if 'build' in name.lower() else CQL_READY}' within {timeout}s; log: {log}",
                   _tail(log))

    def folder_of(pattern: str) -> Path | None:
        matches = [path for rel, path in files.items() if re.fullmatch(pattern, rel)]
        return matches[0].parent if matches else None

    debs = folder_of(r"debian/(pool/main/c/cassandra/)?cassandra_[^/]+_all\.deb" if released else r"debian/cassandra_[^/]+_all\.deb")
    rh_dists = ["almalinux"] + (["noboolean"] if any(f.startswith("redhat/noboolean/") and f.endswith(".noarch.rpm") for f in files) else [])

    for jdk in jdks:
        image = f"eclipse-temurin:{jdk}-jdk"
        if "build" in wanted:
            if ctx.src_root is None:
                r.skip(f"Source build (JDK {jdk})", "source tarball not extracted")
            else:
                work = _fresh_copy(ctx.src_root, root / f"src-jdk{jdk}")
                log = root / f"build-jdk{jdk}.log"
                r.note(f"Building the source tarball with JDK {jdk} (log: {log}) ...")
                record(f"Source build (JDK {jdk})", run_container(engine, image, build_script(jdk, ctx.version),
                                                                  {work: "/src"}, BUILD_OK, log, timeout), log)
        if "binary" in wanted:
            if ctx.bin_root is None:
                r.skip(f"Binary tarball starts (JDK {jdk})", "binary tarball not extracted")
            else:
                work = _fresh_copy(ctx.bin_root, root / f"bin-jdk{jdk}")
                log = root / f"binary-jdk{jdk}.log"
                r.note(f"Starting the binary tarball with JDK {jdk} ...")
                record(f"Binary tarball starts (JDK {jdk})", run_container(engine, image, binary_script(),
                                                                           {work: "/cassandra"}, CQL_READY, log, timeout), log)
        if "deb" in wanted:
            if debs is None:
                r.skip(f"Debian package starts (JDK {jdk})", "no .deb files downloaded")
            else:
                log = root / f"deb-jdk{jdk}.log"
                r.note(f"Installing the .deb packages with JDK {jdk} ...")
                record(f"Debian package starts (JDK {jdk})", run_container(engine, image, deb_script(),
                                                                           {_only_debs(debs, root / "debs"): "/debs:ro"}, CQL_READY, log, timeout), log)
        if "apt" in wanted:
            apt_image = image if jdk == "8" else "debian:bullseye-slim"  # as the script: apt pulls the JRE itself
            log = root / f"apt-jdk{jdk}.log"
            r.note(f"Installing from the apt repository {apt_url} ({apt_image}) ...")
            record(f"APT repository install starts (JDK {jdk} round, {apt_image})",
                   run_container(engine, apt_image, apt_script(apt_url, series, pin), {}, CQL_READY, log, timeout), log)
        for rh in rh_dists:
            sub = "noboolean/" if rh == "noboolean" else ""
            if "rpm" in wanted:
                rpms = folder_of(rf"redhat/{sub}[^/]+\.noarch\.rpm")
                if rpms is None:
                    r.skip(f"RPM package starts ({rh}, JDK {jdk})", "no .rpm files downloaded")
                else:
                    log = root / f"rpm-{rh}-jdk{jdk}.log"
                    r.note(f"Installing the {rh} .rpm packages with JDK {jdk} ...")
                    record(f"RPM package starts ({rh}, JDK {jdk})", run_container(
                        engine, "almalinux:9", rpm_script(jdk), {rpms: "/rpms:ro"}, CQL_READY, log, timeout), log)
            if "yum" in wanted:
                baseurl = f"{rpm_base}{sub}".rstrip("/")
                log = root / f"yum-{rh}-jdk{jdk}.log"
                r.note(f"Installing from the yum repository {baseurl} with JDK {jdk} ...")
                record(f"YUM repository install starts ({rh}, JDK {jdk})", run_container(
                    engine, "almalinux:9", yum_script(jdk, baseurl, pin), {}, CQL_READY, log, timeout), log)
    if passed:
        ctx.highlight(f"Runtime (containers): {len(passed)} checks passed across JDK {', '.join(jdks)}: "
                      "source build, binary tarball, .deb/.rpm packages and apt/yum repositories")


def _fresh_copy(src: Path, dest: Path) -> Path:
    """A writable copy (the build and Cassandra write into their directory), so the cache stays pristine."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True)
    return dest


def _only_debs(folder: Path, dest: Path) -> Path:
    """Just the binary .deb files of the folder (the staged debian/ also holds dists/, pool/, .changes ...)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for deb in folder.glob("*_all.deb"):
        shutil.copy2(deb, dest / deb.name)
    return dest


def _tail(log: Path, n: int = 12) -> list[str]:
    try:
        return log.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []

"""Cassandra Sidecar: build/publish (SP:286-297), packages (SP:315-349), JFrog upload (SF:184-240)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from ...projects import debian_series
from .. import sandbox
from ..context import ReleaseContext
from ..util import dist_dev_dir, dist_release_parent, label, svn_where
from . import step
from .common import _svn_commit_dir

# The Java 8 build publishes the client artifacts as <version>-jdk8; the Java 11 build everything (SP:292-297).
BUILDS = (("8", "-jdk8", ["publish"]), ("11", "", ["assemble", "publish", "-PskipIntegrationTest"]))


@step("build-publish", project="sidecar", summary="Fresh clone at the tag; publish the -jdk8 client artifacts (JDK 8), then everything (JDK 11)",
      outward=True, replicates="SP:286-297",
      you_do="after this step, Close both staging repositories (JDK 8 and JDK 11) at https://repository.apache.org")
def build_publish(ctx: ReleaseContext) -> str:
    v, ep = ctx.version, ctx.endpoints
    clone = ctx.workdir / "clone"
    ctx.fresh_dir(clone)
    ctx.run(["git", "clone", "-q", ep.git_url, str(clone)], f"fresh clone of {ep.git_url}", where="container", always=True)
    ctx.run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", ctx.tag], f"check out {ctx.tag}",
            where="container", cwd=clone, always=True)
    _dtest_jar(ctx, clone)
    for n, (jdk, suffix, tasks) in enumerate(BUILDS, 1):
        env = {"CRV_JAVA_HOME": f"/opt/jdk-{jdk}"}
        cmd = ["./gradlew", "--no-daemon", f"-Dorg.gradle.java.home=/opt/jdk-{jdk}", "clean", *tasks,
               "-PforceSigning", "-Prelease=true", f"-Pversion={v}{suffix}", "--stacktrace"]
        if ep.rehearsal:
            env["GRADLE_USER_HOME"] = "/root/.gradle-rehearsal"
            cmd += [f"-Pmaven.repository.url=file://{ep.sandbox / 'www' / 'repositories' / f'orgapachecassandra-{n}'}",
                    f"-Psigning.gnupg.keyName={ctx.state.data.get('rehearsal_key', '<rehearsal key>')}",
                    "-Psigning.gnupg.executable=gpg"]
        ctx.run(cmd, f"JDK {jdk}: publish {v}{suffix}", where="container", cwd=clone, env=env, outward=True, signing=True)
    if ctx.dry_run:
        return ""
    dist = clone / "build" / "distributions"
    sums = ("", ".asc", ".sha256", ".sha512")
    tarballs = [dist / f"apache-cassandra-sidecar-{v}{s}.tar.gz{e}" for s in ("-src", "") for e in sums]
    missing = [f.name for f in tarballs if not f.exists()]
    # since CASSSIDECAR-472 the packages carry a release number (_0.5.0-1_all.deb); SP:324/342 globs
    # (_0.5.0_all.deb*, -0.5.0.noarch.rpm*) no longer match them, so they are found rather than named
    rel = ctx.project.simple_package_release(v, [f"debian/{f.name}" for f in dist.glob("*.deb")] +
                                             [f"redhat/{f.name}" for f in dist.glob("*.rpm")])
    packages = []
    for pkg in package_names(v, rel):
        packages += [dist / f"{pkg}{e}" for e in sums]
    missing += [f.name for f in packages if not f.exists()]
    if missing:
        raise RuntimeError(f"the build did not produce {', '.join(missing)}")
    ctx.state.data.update(dist_files=[str(f) for f in tarballs], package_files=[str(f) for f in packages])
    return f"{v}-jdk8 and {v} published; {len(tarballs)} dist files and {len(packages)} package files built"


def package_names(version: str, release: str | None) -> list[str]:
    """The .deb and .rpm the Sidecar build makes: apache-cassandra-sidecar_0.5.0-1_all.deb and
    apache-cassandra-sidecar-0.5.0-1.noarch.rpm (release None: the pre-CASSSIDECAR-472 names)."""
    r = f"-{release}" if release else ""
    return [f"apache-cassandra-sidecar_{version}{r}_all.deb", f"apache-cassandra-sidecar-{version}{r}.noarch.rpm"]


def dtest_jar_name(clone: Path) -> str | None:
    """The Cassandra dtest jar test-common compiles against, e.g. dtest-4.1.11.jar (test-common/build.gradle)."""
    build = clone / "test-common" / "build.gradle"
    m = re.search(r'ext\.dtestJar\s*=.*?"(dtest-[^"]+\.jar)"', build.read_text()) if build.exists() else None
    return m.group(1) if m else None


def dtest_branch(name: str) -> str:
    """The build-dtest-jars.sh branch that builds a jar: dtest-4.1.11.jar -> cassandra-4.1 (a released
    x.y.z), dtest-5.1.jar -> trunk (the unreleased version)."""
    version = name.removeprefix("dtest-").removesuffix(".jar")
    parts = version.split(".")
    return f"cassandra-{parts[0]}.{parts[1]}" if len(parts) >= 3 else "trunk"


def _dtest_jar(ctx: ReleaseContext, clone: Path) -> None:
    """The JDK 11 `assemble` compiles test-common's test fixtures, which need a Cassandra dtest jar in
    dtest-jars/ (or $CASSANDRA_DEP_DIR). Nothing published contains it, but the build fails without it, and
    the official script (SP:286-297) never provides it: the release manager's environment had to. Builds it
    with the project's scripts/build-dtest-jars.sh, only for the branch needed; a rehearsal may reuse built
    jars with --dependencies."""
    name = dtest_jar_name(clone)
    if not name:
        return
    deps = getattr(ctx.args, "dependencies", None)
    if ctx.endpoints.rehearsal and deps and (Path(deps).expanduser() / name).exists():
        (clone / "dtest-jars").mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(deps).expanduser() / name, clone / "dtest-jars" / name)
        print(f"   rehearsal: reusing {name} from {deps}")
        return
    branch = dtest_branch(name)
    ctx.run(["./scripts/build-dtest-jars.sh"], f"build {name} for test-common (scripts/build-dtest-jars.sh, {branch})",
            where="container", cwd=clone, env={"BRANCHES": branch, "CRV_JAVA_HOME": "/opt/jdk-11"})


@step("stage-packages", project="sidecar", summary="Commit the .deb and .rpm (+ .asc, .sha256, .sha512) to dist/dev under debian/ and redhat/",
      outward=True, replicates="SP:315-349")
def stage_packages(ctx: ReleaseContext) -> str:
    files = [Path(f) for f in ctx.state.data.get("package_files", [])]
    debs = [f for f in files if ".deb" in f.name]
    rpms = [f for f in files if ".rpm" in f.name]
    first = _svn_commit_dir(ctx, f"{dist_dev_dir(ctx)}/debian", debs,
                            f"Staging {label(ctx)} debian packages for {ctx.version}", "debian")
    second = _svn_commit_dir(ctx, f"{dist_dev_dir(ctx)}/redhat", rpms,
                             f"staging cassandra rpm packages for {ctx.version}", "redhat")
    return f"{first}; {second}"


@step("finish-packages", project="sidecar", summary="Upload the released .deb/.rpm to JFrog (verified), then remove them from dist/release",
      outward=True, replicates="SF:184-240",
      you_do="needs ARTIFACTORY_API_KEY (and asf_username) in the environment; the key never appears in logs")
def finish_packages(ctx: ReleaseContext) -> str:
    return upload_packages(ctx, {
        "debian": ["cassandra/cassandra-sidecar/{name}", "cassandra-deb/cassandra-sidecar/{name}"],
        "redhat": [f"cassandra-rpm/cassandra-sidecar/{debian_series(ctx.version)}/{{name}}"],
    }, note="Sidecar packages have never reached JFrog before (the official SF:194 skips them): check the "
            "Artifactory apt/yum metadata afterwards")


def upload_packages(ctx: ReleaseContext, layout: dict[str, list[str]], note: str = "") -> str:
    """Uploads every file of the released debian/ and redhat/ directories to the JFrog paths in layout,
    verifies each upload, and only then svn rm's those directories from dist/release (CF:182-237, SF:184-240)."""
    ep = ctx.endpoints
    release_dir = f"{dist_release_parent(ctx)}/{ctx.version}"
    export = ctx.workdir / "released-packages"
    ctx.fresh_dir(export)
    ctx.run(["svn", "export", "-q", release_dir, str(export)], f"export {release_dir}", where=svn_where(ctx), always=True)
    files: list[tuple[Path, str]] = []
    for sub, targets in layout.items():
        root = export / sub
        for path in sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []:
            rel = path.relative_to(root).as_posix()
            files += [(path, t.format(name=rel)) for t in targets]
    if not files and not ctx.dry_run:
        raise RuntimeError(f"nothing to upload under {release_dir}/{{{','.join(layout)}}}")

    user = os.environ.get("asf_username") or os.environ.get("USER", "")
    key = os.environ.get("ARTIFACTORY_API_KEY", "")
    if not ep.rehearsal and not key and not ctx.dry_run:
        raise RuntimeError("ARTIFACTORY_API_KEY is not set")
    netrc = Path(tempfile.mkstemp(prefix="crv-netrc-")[1])
    try:
        netrc.chmod(0o600)
        netrc.write_text(f"machine apache.jfrog.io login {user} password {key}\n")
        with (sandbox.serve(ep.sandbox / "www") if ep.rehearsal else _null()) as served:
            base = f"{served}artifactory/" if ep.rehearsal else ep.artifactory
            for path, target in files:
                url = base + target + ("" if ep.rehearsal else "?override=1")
                auth = [] if ep.rehearsal else ["--netrc-file", str(netrc)]
                ctx.run(["curl", "--fail", "--silent", "--show-error", "--retry", "3", *auth, "-X", "PUT",
                         "-T", str(path), url], f"upload {target}", outward=True)
            for path, target in files:  # every upload verified before anything is removed
                auth = [] if ep.rehearsal else ["--netrc-file", str(netrc)]
                # read-only, but not run in a dry run: nothing has been uploaded to check
                headers = ctx.run(["curl", "--fail", "--silent", "--head", *auth, base + target],
                                  f"verify {target}", capture=True)
                if headers is not None:
                    problem = upload_mismatch(path, headers)
                    if problem:
                        raise RuntimeError(f"{target}: {problem}; nothing was removed from dist/release")
    finally:
        netrc.unlink(missing_ok=True)
    for sub in layout:
        ctx.run(["svn", "rm", "-m", f"{ctx.project.display} {ctx.version} {sub} artifacts", f"{release_dir}/{sub}"],
                f"svn rm {release_dir}/{sub}", where=svn_where(ctx), outward=True)
    if note:
        ctx.note(note)
    return f"{len(files)} uploads verified; {', '.join(layout)} removed from {release_dir}"


def upload_mismatch(local: Path, headers: str) -> str | None:
    """Compares an upload with the local file, from the HEAD response: its size, and Artifactory's
    X-Checksum-Sha256 when the server sends one. None when they match."""
    fields = {}
    for line in headers.splitlines():
        name, sep, value = line.partition(":")
        if sep:
            fields[name.strip().lower()] = value.strip()
    size = local.stat().st_size
    if "content-length" in fields and int(fields["content-length"]) != size:
        return f"the server has {fields['content-length']} bytes, the file {size}"
    if "x-checksum-sha256" in fields:
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        if fields["x-checksum-sha256"].lower() != digest:
            return f"SHA-256 {fields['x-checksum-sha256']} on the server, {digest} here"
    return None


class _null:
    def __enter__(self):
        return ""

    def __exit__(self, *exc):
        return False

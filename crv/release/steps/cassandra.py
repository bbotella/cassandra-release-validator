"""Cassandra: changelog (CP:235-245), ant publish (CP:254-263), Debian packages and apt repository
(CP:282-335), RedHat packages and yum repositories (CP:339-417), JFrog upload (CF:182-237).

The packages are built the way the official script builds them on 5.0 and later: with the project's own
build images (.build/docker/*.docker) and inner scripts (.build/docker/_build-debian.sh,
_build-redhat.sh). Only the in-tree wrapper .build/docker/_docker_run.sh is replicated here, so that no
container has to start containers. 4.0 and 4.1 have no in-tree scripts (the official script then uses
cassandra-builds' build-scripts/cassandra-{deb,rpm}-packaging.sh); they are not supported yet.
"""

from __future__ import annotations

import email.utils
import hashlib
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from ...projects import deb_version, debian_series
from ..context import ReleaseContext
from ..executor import StepFailed
from ..util import CHANGELOG_HEAD, dist_dev_dir, git, source_dir
from . import step
from .common import _svn_commit_dir
from .sidecar import upload_packages

PLATFORM = "linux/amd64"  # the official packages are built on amd64: cassandra_<v>_amd64.changes (CP:312)

# .build/docker/_docker_run.sh versions that run_build_image was checked against (docs/parity.md):
# identical on cassandra-5.0, cassandra-6.0 and trunk on 2026-11-20
REVIEWED_DOCKER_RUN = {"d99fc586d9849d22120de53d8e0b9d74d7cccb23ee0c91f65438e88136324075"}


# ------------------------------------------------------------------ changelog
def release_changelog(text: str, version: str, maintainer: str, when: str) -> str:
    """What `dch -r -D unstable` does to the top entry of debian/changelog (CP:239): the distribution
    becomes unstable, and the trailer names the release manager and the current date."""
    lines = text.splitlines(keepends=True)
    m = CHANGELOG_HEAD.match(lines[0].rstrip("\n")) if lines else None
    if not m:
        raise RuntimeError("debian/changelog: the top entry can't be read")
    deb = deb_version(version)
    if m["version"] != deb:
        raise RuntimeError(f"debian/changelog's top entry is {m['version']}, not {deb}: add an entry for {deb} "
                           f"first (dch -v {deb})")
    lines[0] = f"{m['package']} ({m['version']}) unstable;{m['rest']}\n"
    for i in range(1, len(lines)):
        if CHANGELOG_HEAD.match(lines[i]):
            break
        if lines[i].startswith(" -- "):
            lines[i] = f" -- {maintainer}  {when}\n"
            return "".join(lines)
    raise RuntimeError("debian/changelog: the top entry has no ' -- name <email>  date' line")


def _maintainer(ctx: ReleaseContext, src: Path) -> str:
    if getattr(ctx.args, "maintainer", None):
        return ctx.args.maintainer
    name = git("config", "user.name", cwd=src, check=False)
    mail = git("config", "user.email", cwd=src, check=False)
    if not name or not mail:
        raise RuntimeError("no git user.name/user.email in your checkout: pass --maintainer 'Name <you@apache.org>'")
    return f"{name} <{mail}>"


@step("changelog", project="cassandra", summary="Finalize debian/changelog (dch -r -D unstable) and commit it",
      outward=True, replicates="CP:235-245",
      you_do="after the vote, forward-merge and push the 'Prepare debian changelog' commit, as with every release")
def changelog(ctx: ReleaseContext) -> str:
    v = ctx.version
    src = Path(ctx.state.data.get("source") or source_dir(ctx))
    head = ctx.state.data.get("head") or git("rev-parse", "HEAD", cwd=src)
    old = git("show", f"{head}:debian/changelog", cwd=src) + "\n"
    maintainer = _maintainer(ctx, src)
    new = release_changelog(old, v, maintainer, email.utils.format_datetime(datetime.now().astimezone()))
    # the same lines, two of them changed: shown line by line (a diff would align similar entries badly)
    for n, (before, after) in enumerate(zip(old.splitlines(), new.splitlines()), 1):
        if before != after:
            print(f"     debian/changelog:{n}\n       - {before}\n       + {after}")
    if not maintainer.endswith("@apache.org>"):
        ctx.note(f"the changelog names {maintainer}; releases use an @apache.org address "
                 f"(--maintainer 'Name <you@apache.org>')")
    message = f"Prepare debian changelog for {v}"
    if ctx.endpoints.rehearsal:
        commit = _commit_object(ctx, src, head, new, message)
    else:
        current = git("rev-parse", "HEAD", cwd=src)
        if current != head:
            raise RuntimeError(f"your checkout moved from {head[:12]} (preflight) to {current[:12]}: run preflight again")
        if not ctx.dry_run:
            (src / "debian" / "changelog").write_text(new)
        # a local commit, pushed later with the tag (CP:241)
        ctx.run(["git", "commit", "-q", "-m", message, "debian/changelog"], f"commit '{message}'", cwd=src)
        commit = "" if ctx.dry_run else git("rev-parse", "HEAD", cwd=src)
    if ctx.dry_run:
        return ""
    ctx.state.data.update(head=commit, changelog_commit=commit)
    return f"{commit[:12]} {message} ({maintainer})"


def _commit_object(ctx: ReleaseContext, src: Path, parent: str, content: str, message: str) -> str:
    """A rehearsal's changelog commit: written to the object database only, with a temporary index, so
    your checkout's branch, index and files stay as they are. The tag step pushes it to the sandbox."""
    work = ctx.workdir
    work.mkdir(parents=True, exist_ok=True)
    blob_file, index = work / "changelog.new", work / "changelog.index"
    if not ctx.dry_run:
        blob_file.write_text(content)
    env = {"GIT_INDEX_FILE": str(index)}
    blob = ctx.run(["git", "hash-object", "-w", str(blob_file)], "store the new debian/changelog", cwd=src,
                   capture=True)
    ctx.run(["git", "read-tree", parent], "a temporary index at the release commit", cwd=src, env=env)
    ctx.run(["git", "update-index", "--cacheinfo", f"100644,{(blob or '<blob>').strip()},debian/changelog"],
            "put the new debian/changelog in it", cwd=src, env=env)
    tree = ctx.run(["git", "write-tree"], "write its tree", cwd=src, env=env, capture=True)
    commit = ctx.run(["git", "commit-tree", (tree or "<tree>").strip(), "-p", parent, "-m", message],
                     f"commit '{message}' (not on any branch)", cwd=src, capture=True)
    index.unlink(missing_ok=True)
    return (commit or "").strip()


# -------------------------------------------------------------- build-publish
def build_property(clone: Path, name: str) -> str | None:
    """A <property name=... value=...> of the clone's build.xml, e.g. java.default."""
    xml = clone / "build.xml"
    m = re.search(rf'<property\s+name="{re.escape(name)}"\s+value="([^"]*)"', xml.read_text()) if xml.exists() else None
    return m.group(1) if m else None


def _java(clone: Path) -> str:
    return build_property(clone, "java.default") or "11"


def m2_repository(ctx: ReleaseContext) -> Path:
    """The local Maven repository the builds use. A real release uses yours (CP:263 and _docker_run.sh
    do); a rehearsal its own, so that rehearsal builds of cassandra-all <version> never land in yours."""
    return ctx.endpoints.root / "m2" if ctx.endpoints.rehearsal else Path.home() / ".m2" / "repository"


@step("build-publish", project="cassandra",
      summary="Fresh clone at the tag; ant realclean, then ant publish -Drelease=true (Maven artifacts to Nexus, signed tarballs)",
      outward=True, replicates="CP:254-263",
      you_do="after this step, Close the staging repository at https://repository.apache.org")
def build_publish(ctx: ReleaseContext) -> str:
    v, ep = ctx.version, ctx.endpoints
    clone = ctx.workdir / "clone"
    ctx.fresh_dir(clone)
    ctx.run(["git", "clone", "-q", ep.git_url, str(clone)], f"fresh clone of {ep.git_url}", where="container", always=True)
    ctx.run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", ctx.tag], f"check out {ctx.tag}",
            where="container", cwd=clone, always=True)
    java = _java(clone)
    env = {"CRV_JAVA_HOME": f"/opt/jdk-{java}"}
    ctx.run(["ant", "realclean"], "ant realclean", where="container", cwd=clone, env=env)
    cmd = ["ant", "publish", "-Drelease=true", "-Dno-checkstyle=true", "-Drat.skip=true", "-Dant.gen-doc.skip=true"]
    if ep.rehearsal:
        repo = ep.sandbox / "www" / "repositories" / "orgapachecassandra-1"
        cmd += [f"-Dmaven-repository-url=file://{repo}", "-Dmaven-repository-id=rehearsal"]
    ctx.run(cmd, f"ant publish (JDK {java})", where="container", cwd=clone, env=env, outward=True, signing=True)
    if ctx.dry_run:
        return ""
    files = [clone / "build" / f"apache-cassandra-{v}-{kind}.tar.gz{ext}"
             for kind in ("src", "bin") for ext in ("", ".asc", ".sha256", ".sha512")]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise RuntimeError(f"the build did not produce {', '.join(missing)}")
    ctx.state.data.update(dist_files=[str(f) for f in files], java=java)
    return f"cassandra {v} published with JDK {java}; {len(files)} dist files built"


# -------------------------------------------------------- the build images
def wrapper_target(clone: Path, wrapper: str) -> tuple[str, str]:
    """(Dockerfile, inner script) that an in-tree wrapper such as .build/docker/build-debian.sh hands to
    _docker_run.sh, e.g. ('bullseye-build.docker', 'docker/_build-debian.sh')."""
    path = clone / ".build" / "docker" / wrapper
    if not path.exists():
        raise RuntimeError(f"{path.relative_to(clone)} is missing: 4.0 and 4.1 package with cassandra-builds' "
                           f"build-scripts instead (CP:305, CP:363), which cut-release doesn't support yet")
    m = re.search(r"_docker_run\.sh\s+(\S+\.docker)\s+(\S+\.sh)", path.read_text())
    if not m:
        raise RuntimeError(f"can't tell which image and script {path.name} runs")
    return m.group(1), m.group(2)


def image_name(clone: Path, dockerfile: str) -> str:
    """_docker_run.sh's image: apache/cassandra-<dockerfile>:<md5 of the Dockerfile> (pulled, else built)."""
    digest = hashlib.md5((clone / ".build" / "docker" / dockerfile).read_bytes()).hexdigest()
    return f"docker.io/apache/cassandra-{dockerfile.removesuffix('.docker')}:{digest}"


def run_build_image(ctx: ReleaseContext, clone: Path, dockerfile: str, script: str, args: list[str],
                    description: str) -> None:
    """Replicates .build/docker/_docker_run.sh: the image (pulled, or built from the in-tree Dockerfile), a
    `build` user, then the inner script with build/ mounted at /dist. Run by this machine's engine."""
    engine = ctx.executor.container.engine
    wrapper = clone / ".build" / "docker" / "_docker_run.sh"
    if wrapper.exists() and hashlib.sha256(wrapper.read_bytes()).hexdigest() not in REVIEWED_DOCKER_RUN:
        ctx.note(f"{ctx.tag}'s .build/docker/_docker_run.sh is not a version run_build_image was checked against: "
                 "compare the two before relying on these packages (docs/parity.md)")
    java = _java(clone)
    supported = (build_property(clone, "java.supported") or java).split(",")
    if java not in supported:
        raise RuntimeError(f"java.default {java} is not in java.supported {supported}")
    image = image_name(clone, dockerfile) if (clone / ".build" / "docker" / dockerfile).exists() else \
        f"docker.io/apache/cassandra-{dockerfile.removesuffix('.docker')}:<md5>"
    try:
        ctx.run([engine, "image", "inspect", image], f"is {image} here?", capture=True, always=True)
        have = True
    except StepFailed:
        have = False
    if not have:
        try:
            ctx.run([engine, "pull", "-q", "--platform", PLATFORM, image], f"pull {image}", capture=True)
        except StepFailed:
            ctx.run([engine, "build", "--platform", PLATFORM, "-t", image, "-f", f"docker/{dockerfile}", "."],
                    f"build {image} from .build/docker/{dockerfile}", cwd=clone / ".build")
    ctx.run(["bash", "-c", "mkdir -p build && chmod -R ag+rwx build"], "prepare build/", where="container", cwd=clone)
    m2 = m2_repository(ctx)
    m2.mkdir(parents=True, exist_ok=True)
    name = f"crv_cassandra_{dockerfile.removesuffix('.docker')}_{Path(script).stem.lstrip('_')}_jdk{java}_{secrets.token_hex(3)}"
    ctx.run([engine, "run", "--platform", PLATFORM, "--name", name, "-d", "--security-opt", "seccomp=unconfined", "--rm",
             "-v", f"{clone}:/home/build/cassandra", "-v", f"{m2}:/home/build/.m2/repository/",
             "-v", f"{clone / 'build'}:/dist", image, "sleep", "1h"], f"start {name}")
    try:
        # any uid but 0 will do: the files under this directory show as root's inside the machine anyway
        ctx.run([engine, "exec", "--user", "root", name, "bash", "-c",
                 "${CASSANDRA_DIR}/.build/docker/_create_user.sh build 1000 1000"], "create the build user")
        ctx.run([engine, "exec", "--user", "build", name, "bash", "-c",
                 'export ANT_OPTS="-Dbuild.dir=${DIST_DIR}" ; '
                 f"source ${{CASSANDRA_DIR}}/.build/docker/_set_java.sh {java} ; "
                 f"${{CASSANDRA_DIR}}/.build/{script} {' '.join(args)} ; exit $? "], description)
    finally:
        if not ctx.dry_run:
            try:
                ctx.run([engine, "stop", "-t", "2", name], f"stop {name}")
            except StepFailed:
                pass


def signing_key(ctx: ReleaseContext) -> str:
    key = ctx.state.data.get("rehearsal_key") if ctx.endpoints.rehearsal else ctx.args.signing_key
    if not key:
        raise RuntimeError("a signing step needs your release key: --signing-key <fingerprint> (or $gpg_key)")
    return key


# ---------------------------------------------------------------- packages
@step("stage-debian", project="cassandra",
      summary="Build the .deb with the project's build image, debsign it, make the signed apt repository (reprepro), commit it to dist/dev",
      outward=True, replicates="CP:282-335")
def stage_debian(ctx: ReleaseContext) -> str:
    v, clone = ctx.version, ctx.workdir / "clone"
    deb, series, key = deb_version(v), debian_series(v), signing_key(ctx)
    env = {"CRV_JAVA_HOME": f"/opt/jdk-{_java(clone)}"}
    dockerfile, script = wrapper_target(clone, "build-debian.sh")
    ctx.run(["ant", "realclean"], "ant realclean", where="container", cwd=clone, env=env)
    run_build_image(ctx, clone, dockerfile, script, [], f"build the Debian packages ({script})")
    work = ctx.workdir / "debian"
    ctx.fresh_dir(work)
    work.mkdir()
    built = re.compile(rf"cassandra[-_].*{re.escape(deb)}.*")  # what _build-debian.sh moves to build/
    for f in sorted((clone / "build").glob("cassandra*")) if not ctx.dry_run else []:
        if f.is_file() and built.fullmatch(f.name):
            shutil.copy2(f, work / f.name)
    changes = f"cassandra_{deb}_amd64.changes"
    if not ctx.dry_run and not (work / changes).exists():
        raise RuntimeError(f"the Debian build did not produce {changes}")
    ctx.run(["debsign", f"-k{key}", changes], f"debsign {changes}", where="container", cwd=work, signing=True)
    (work / "conf").mkdir()
    (work / "conf" / "distributions").write_text(
        "Origin: Apache Cassandra Packages\nLabel: Apache Cassandra Packages\n"
        f"Codename: {series}\nArchitectures: i386 amd64 arm64 source\nComponents: main\n"
        f"Description: Apache Cassandra APT Repository\nSignWith: {key}\n")
    ctx.run(["bash", "-c", f"reprepro --ignore=wrongdistribution include {series} cassandra_{deb}_*.changes && rm -R db conf"],
            f"the apt repository for {series}, signed (reprepro)", where="container", cwd=work, signing=True)
    revision = _svn_commit_dir(ctx, f"{dist_dev_dir(ctx)}/debian", [], f"staging cassandra debian packages for {v}",
                               "debian", tree=work)
    if ctx.dry_run:
        return ""
    count = sum(1 for f in work.rglob("*") if f.is_file())
    return f"{count} files in debian/ (apt repository {series}): {revision}"


@step("stage-rpm", project="cassandra",
      summary="Build the .rpm (and noboolean) with the project's build image, rpmsign, make the signed yum repositories, commit them to dist/dev",
      outward=True, replicates="CP:339-417")
def stage_rpm(ctx: ReleaseContext) -> str:
    v, clone = ctx.version, ctx.workdir / "clone"
    key = signing_key(ctx)
    env = {"CRV_JAVA_HOME": f"/opt/jdk-{_java(clone)}"}
    dockerfile, script = wrapper_target(clone, "build-redhat.sh")
    dists = ["rpm"] + (["noboolean"] if (clone / "redhat" / "noboolean").is_dir() else [])  # CP:378
    work = ctx.workdir / "redhat"
    ctx.fresh_dir(work)
    work.mkdir()
    for dist in dists:
        target = work if dist == "rpm" else work / "noboolean"
        target.mkdir(exist_ok=True)
        ctx.run(["ant", "realclean"], "ant realclean", where="container", cwd=clone, env=env)
        run_build_image(ctx, clone, dockerfile, script, [dist], f"build the {dist} packages ({script} {dist})")
        rpms = sorted((clone / "build").glob("*.rpm")) if not ctx.dry_run else []
        if not ctx.dry_run and not rpms:
            raise RuntimeError(f"the {dist} build did not produce any .rpm")
        for f in rpms:
            shutil.copy2(f, target / f.name)
        ctx.run(["bash", "-c", f"rpmsign --define '_gpg_name {key}' --addsign *.rpm && createrepo_c -q . && "
                 "shopt -s nullglob && for f in repodata/repomd.xml repodata/*.bz2 repodata/*.gz ; do "
                 'gpg --batch --yes --detach-sign --armor "$f" ; done'],
                f"rpmsign, createrepo_c and signed repodata ({dist})", where="container", cwd=target, signing=True)
    revision = _svn_commit_dir(ctx, f"{dist_dev_dir(ctx)}/redhat", [], f"staging cassandra rpm packages for {v}",
                               "redhat", tree=work)
    if ctx.dry_run:
        return ""
    count = sum(1 for f in work.rglob("*") if f.is_file())
    return f"{count} files in redhat/ ({' and '.join(dists)}): {revision}"


@step("finish-packages", project="cassandra",
      summary="Upload the released apt and yum repositories to JFrog (verified), then remove them from dist/release",
      outward=True, replicates="CF:182-237",
      you_do="needs ARTIFACTORY_API_KEY (and asf_username) in the environment; the key never appears in logs")
def finish_packages(ctx: ReleaseContext) -> str:
    series = debian_series(ctx.version)
    return upload_packages(ctx, {
        "debian": ["cassandra/{name}", "cassandra-deb/{name}"],  # CF:198-199
        "redhat": [f"cassandra-rpm/{series}/{{name}}"],  # CF:229
    }, min_depth={"debian": 2})  # CF:192: only dists/ and pool/, the apt repository

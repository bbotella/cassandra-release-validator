"""Apache Cassandra: the binary tarball, the .deb and .rpm packages and cassandra-all, each used as a user would.

  binary  start the tarball; nodetool version/status, a cqlsh round trip, flush, tools/bin/sstablemetadata
  deb     apt-get install the .deb files with their real dependencies on a Debian release that has the Java they
          ask for, start the service as user cassandra, the same checks
  rpm     dnf install the .rpm files (AlmaLinux 9; noboolean on AlmaLinux 8), the same checks
  maven   every staged artifact resolves with its dependencies; cassandra-all writes SSTables with
          CQLSSTableWriter, and the binary-tarball node imports them with nodetool import and reads them back

The apt/yum repository installs stay in --runtime (the cassandra-check-release.sh matrix).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ...pkgformats import read_deb_control
from ...projects import deb_version
from ..git import show_file
from ..runtime import supported_jdks
from . import maven_consumer as mc
from .common import SmokeRun, options_file_lines, provenance_report, release_files
from .engine import Container, SetupError, fill, report
from .programs import DIR as PROGRAMS

TESTS = ("binary", "deb", "rpm", "maven")
ROWS = 1000
CQL_READY = "Starting listening for CQL clients"

# The Ubuntu releases eclipse-temurin images are built on, newest first, with their python3 minor version
TEMURIN_UBUNTU = (("noble", 12), ("jammy", 10), ("focal", 8))
CQLSH_RANGE = re.compile(r'minor_version"\s+-ge\s+(\d+)\s*\]\s*&&\s*\[\s*"\$minor_version"\s+-le\s+(\d+)')

# OpenJDK runtimes in each Debian release's main archive, newest release first
DEBIAN_JAVA = (("trixie", {"21", "25"}), ("bookworm", {"17"}), ("bullseye", {"11", "17"}))

USAGE = """
v=$(@NODETOOL@ version 2>&1 | sed -n 's/^ReleaseVersion: *//p')
if [ "$v" = "@VERSION@" ]; then crv_step nodetool-version OK "ReleaseVersion $v"
else crv_step nodetool-version FAIL "ReleaseVersion '$v', expected @VERSION@"; fi
crv_run nodetool-status bash -c '@NODETOOL@ status | tee /dev/stderr | grep -q "^UN "'
if crv_run cqlsh-write @CQLSH@ -e "CREATE KEYSPACE IF NOT EXISTS crv_smoke WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}; CREATE TABLE IF NOT EXISTS crv_smoke.t (id int PRIMARY KEY, v text); INSERT INTO crv_smoke.t (id, v) VALUES (1, 'one');"; then
crv_run cqlsh-read bash -c "@CQLSH@ -e 'SELECT v FROM crv_smoke.t WHERE id = 1' | tee /dev/stderr | grep -qw one"
crv_run flush @NODETOOL@ flush crv_smoke t
crv_run sstablemetadata bash -c 'f=$(ls @DATA@/crv_smoke/t-*/*-Data.db 2>/dev/null | head -n 1); [ -n "$f" ] || { echo "no SSTable of crv_smoke.t to read"; exit 1; }; @SSTABLEMETADATA@ "$f" > /tmp/meta.txt 2>&1; rc=$?; head -n 15 /tmp/meta.txt; [ $rc -eq 0 ] && grep -qi partitioner /tmp/meta.txt'
else
crv_step after-cqlsh INFO "cqlsh read, flush and sstablemetadata not run: they need the rows cqlsh failed to write"
fi
"""

BINARY = """
crv_prepare packages bash -c 'apt-get -qq update && apt-get -qq install -y python3 procps > /dev/null'
crv_step java INFO "$(java -version 2>&1 | head -n 1)"
crv_step python INFO "$(python3 --version 2>&1)"
cd /cassandra
export MAX_HEAP_SIZE=1G HEAP_NEWSIZE=256M
bin/cassandra -R > /tmp/cassandra.out 2>&1
crv_wait_log starts @WAIT@ logs/system.log "@READY@" || { cat /tmp/cassandra.out; crv_done; exit 0; }
@USAGE@
crv_run schema bin/cqlsh -e "CREATE TABLE IF NOT EXISTS crv_smoke.imported (id int PRIMARY KEY, value text)"
crv_done
"""

IMPORT = """
cd /cassandra
f=$(ls /tmp/crv-import/crv_smoke/imported/*-Data.db 2>/dev/null | head -n 1)
# tools read cassandra.yaml; in the shipped CASSANDRA_4 mode they reject the newest format, so read with mode NONE
rm -rf /tmp/conf-none && cp -r conf /tmp/conf-none
sed -i 's/^storage_compatibility_mode:.*/storage_compatibility_mode: NONE/' /tmp/conf-none/cassandra.yaml
if [ -n "$f" ] && CASSANDRA_CONF=/tmp/conf-none tools/bin/sstabledump "$f" > /tmp/dump.json 2> /tmp/dump.err; then
  n=$(python3 -c 'import json; print(sum(len(p.get("rows", [])) for p in json.load(open("/tmp/dump.json"))))')
  if [ "$n" = "@ROWS@" ]; then crv_step sstabledump OK "tools/bin/sstabledump reads the $n rows cassandra-all wrote"
  else crv_step sstabledump FAIL "tools/bin/sstabledump found $n rows, expected @ROWS@"; fi
else
  tail -n 5 /tmp/dump.err 2>/dev/null; crv_step sstabledump FAIL "tools/bin/sstabledump could not read ${f:-any SSTable}"
fi
if [ "@IMPORT@" = 1 ]; then
  crv_run nodetool-import bin/nodetool import crv_smoke imported /tmp/crv-import/crv_smoke/imported
  n=$(bin/cqlsh -e 'SELECT count(*) FROM crv_smoke.imported' | tr -d ' ' | grep -E '^[0-9]+$' | head -n 1)
  if [ "$n" = "@ROWS@" ]; then crv_step read-imported OK "$n rows"
  else crv_step read-imported FAIL "count(*) is '$n', expected @ROWS@"; fi
fi
crv_done
"""

PACKAGE = """
@INSTALL@
crv_step java INFO "$(java -version 2>&1 | head -n 1)"
crv_step python INFO "$(python3 --version 2>&1)"
export MAX_HEAP_SIZE=1G HEAP_NEWSIZE=256M
crv_run start env HOME=/tmp setpriv --reuid=cassandra --regid=cassandra --init-groups cassandra -p /tmp/cassandra.pid || { crv_done; exit 0; }
crv_wait_log starts @WAIT@ /var/log/cassandra/system.log "@READY@" || { crv_done; exit 0; }
@USAGE@
crv_done
"""

DEB_INSTALL = """
crv_prepare apt-update apt-get -qq update
crv_run install bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y /pkgs/*.deb' || { crv_done; exit 0; }
"""

RPM_INSTALL = """
crv_prepare dnf-metadata dnf -q makecache
crv_run install dnf install -y /pkgs/*.rpm || { crv_done; exit 0; }
"""


def debian_for(depends: str) -> str | None:
    """The newest Debian release whose archive has one of the Java runtimes the package depends on."""
    javas = set(re.findall(r"openjdk-(\d+)-jre", depends)) | set(re.findall(r"java(\d+)-runtime", depends))
    return next((release for release, have in DEBIAN_JAVA if have & javas), None)


def cqlsh_pythons(cqlsh: str) -> tuple[int, int] | None:
    """The Python 3 minor versions bin/cqlsh accepts as supported (its is_supported_version), if it says."""
    m = CQLSH_RANGE.search(cqlsh)
    return (int(m.group(1)), int(m.group(2))) if m else None


def binary_image(jdk: str, supported: tuple[int, int] | None) -> str:
    """An eclipse-temurin image for the JDK whose python3 cqlsh supports (the newest such Ubuntu base)."""
    for codename, minor in TEMURIN_UBUNTU:
        if supported is None or supported[0] <= minor <= supported[1]:
            return f"eclipse-temurin:{jdk}-jdk-{codename}"
    return f"eclipse-temurin:{jdk}-jdk"


def storage_compatibility_mode(yaml: Path) -> str | None:
    if not yaml.exists():
        return None
    m = re.search(r"(?m)^storage_compatibility_mode:\s*(\S+)", yaml.read_text(errors="replace"))
    return m.group(1) if m else None


def usage(run: SmokeRun, nodetool: str, cqlsh: str, sstablemetadata: str, data: str) -> str:
    return fill(USAGE, NODETOOL=nodetool, CQLSH=cqlsh, SSTABLEMETADATA=sstablemetadata, DATA=data,
                VERSION=run.ctx.version)


def run(run: SmokeRun) -> None:
    ctx = run.ctx
    jdks = [j.strip() for j in ctx.args.jdks.split(",")] if ctx.args.jdks else \
        supported_jdks(show_file(ctx, "build.xml"), ctx.version)[-1:]
    if run.wants("maven"):
        _resolve(run)
    for jdk in jdks:
        if run.wants("binary") or run.wants("maven"):
            _binary(run, jdk)
    if run.wants("deb"):
        _deb(run)
    if run.wants("rpm"):
        _rpm(run)


def _resolve(run: SmokeRun) -> None:
    r = run.report
    arts = mc.usable(mc.artifacts(run.ctx))
    if not arts:
        r.skip("Maven artifacts resolve", "no verified Maven artifacts (was the maven group skipped?)")
        return
    out, m2 = mc.resolve(run.ctx, run.engine, run.dir, "maven-resolve", arts, "17", run.timeout * 2)
    if report(r, "Maven artifacts resolve", out):
        provenance_report(run, m2, "Maven artifacts resolve")
        run.passed.append(f"{len(arts)} Maven artifacts resolve with their dependencies")


def _binary(run: SmokeRun, jdk: str) -> None:
    ctx, r = run.ctx, run.report
    label = f"Binary tarball (JDK {jdk})"
    if ctx.bin_root is None:
        r.skip(label, "binary tarball not extracted")
        return
    home = run.dir / f"binary-jdk{jdk}"
    if home.exists():
        shutil.rmtree(home)
    shutil.copytree(ctx.bin_root, home, symlinks=True)  # Cassandra writes data/ and logs/ into its directory
    log = run.log(f"binary-jdk{jdk}")
    r.note(f"Starting the binary tarball with JDK {jdk} (log: {log}) ...")
    cqlsh = home / "bin" / "cqlsh"
    supported = cqlsh_pythons(cqlsh.read_text(errors="replace")) if cqlsh.exists() else None
    image = binary_image(jdk, supported)
    if supported:
        r.info(f"{label}: image", f"{image}, whose python3 is in the range bin/cqlsh supports "
                                  f"(3.{supported[0]} to 3.{supported[1]})")
    try:
        node = run.engine.start(image, log=log, entrypoint="/bin/bash",
                                command=["-c", "sleep infinity"], mounts={home: "/cassandra"})
    except SetupError as e:
        r.warn(f"{label}: not run", f"{e} (log: {log})")
        return
    with node:
        script = fill(BINARY, WAIT=str(run.timeout), READY=CQL_READY,
                      USAGE=usage(run, "bin/nodetool", "bin/cqlsh", "tools/bin/sstablemetadata", "data/data"))
        out = node.exec(script, log=log, timeout=run.timeout + 300)
        if report(r, label, out):
            run.passed.append(f"binary tarball on JDK {jdk} (nodetool, cqlsh, sstablemetadata)")
        if run.wants("maven") and out.ok:
            _cassandra_all(run, node, jdk, home)


def _cassandra_all(run: SmokeRun, node: Container, jdk: str, home: Path) -> None:
    ctx, r = run.ctx, run.report
    label = f"cassandra-all as a library (JDK {jdk})"
    if not any(a.artifact == "cassandra-all" for a in mc.artifacts(ctx)):
        r.skip(label, "cassandra-all is not among the verified Maven artifacts")
        return
    project = mc.project_dir(run.dir, f"cassandra-all-jdk{jdk}", {"CassandraAllSmoke.java": PROGRAMS / "CassandraAllSmoke.java"})
    (project / "pom.xml").write_text(mc.pom_xml("cassandra-all-smoke", [mc.dependency_xml(mc.GROUP, "cassandra-all", ctx.version)], jdk))
    conf = home / "conf"
    opts = options_file_lines(conf / "jvm-clients.options") + options_file_lines(conf / f"jvm{jdk}-clients.options")
    mode = storage_compatibility_mode(conf / "cassandra.yaml")
    log = run.log(f"cassandra-all-jdk{jdk}")
    r.note(f"Writing SSTables with cassandra-all (log: {log}) ...")
    script = mc.program("CassandraAllSmoke", f"/work/sstables/crv_smoke/imported {ROWS}")
    out = mc.run(ctx, run.engine, project, script, jdk=jdk, log=log, timeout=run.timeout * 2,
                 env={"CRV_JVM_OPTS": " ".join(opts)})
    if not report(r, label, out):
        return
    provenance_report(run, project / "m2", label)
    # Through the engine, not a shared mount: on macOS a folder mounted into the running node doesn't show
    # files another container adds to it (virtiofs caches the listing)
    try:
        node.copy_in(project / "sstables", "/tmp/crv-import")
    except SetupError as e:
        r.warn(f"{label}: not run", str(e))
        return
    # A node in storage_compatibility_mode CASSANDRA_4 (the shipped 5.0 cassandra.yaml) accepts only n* SSTables,
    # and CQLSSTableWriter always writes the newest format (client mode runs in NONE): by design, not a defect
    importable = mode in (None, "NONE")
    if not importable:
        r.info(f"{label}: nodetool import", f"not run: the shipped cassandra.yaml sets storage_compatibility_mode "
                                            f"{mode}, in which neither the node nor tools/bin/sstabledump accept the "
                                            "newest-format (oa) SSTables CQLSSTableWriter always writes; bulk loading "
                                            "them needs mode NONE (the readability check below uses it)")
    out = node.exec(fill(IMPORT, ROWS=str(ROWS), IMPORT="1" if importable else "0"), log=log, timeout=run.timeout)
    if report(r, label, out):
        run.passed.append("cassandra-all writes SSTables that the node imports and reads back")


def _deb(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    v = re.escape(deb_version(ctx.version))
    debs = release_files(ctx, rf"(^|/)cassandra(-tools)?_{v}_all\.deb$")
    debs = list({p.name: p for p in debs}.values())  # staged twice (debian/ and its pool/)
    if not debs:
        r.skip("Debian package", "no .deb files downloaded")
        return
    depends = read_deb_control(next(p for p in debs if p.name.startswith("cassandra_"))).get("Depends", "")
    release = debian_for(depends)
    if release is None:
        r.fail("Debian package: installable", f"no Debian release ({', '.join(n for n, _ in DEBIAN_JAVA)}) has a Java "
                                              f"runtime the package depends on: {depends}")
        return
    label = f"Debian package (debian:{release})"
    log = run.log(f"deb-{release}")
    r.note(f"Installing the .deb packages on debian:{release} (log: {log}) ...")
    script = fill(PACKAGE, INSTALL=DEB_INSTALL, WAIT=str(run.timeout), READY=CQL_READY,
                  USAGE=usage(run, "nodetool", "cqlsh", "sstablemetadata", "/var/lib/cassandra/data"))
    out = run.engine.run(f"debian:{release}-slim", script, log=log, timeout=run.timeout + 600,
                         mounts={run.copy_files("debs", debs): "/pkgs:ro"})
    if report(r, label, out):
        run.passed.append(f".deb packages install with their dependencies and run on Debian {release}")


def _rpm(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    variants = [("", "almalinux:9")] + ([("noboolean/", "almalinux:8")] if release_files(
        ctx, r"redhat/noboolean/cassandra-[^/]+\.noarch\.rpm$") else [])
    for sub, image in variants:
        rpms = release_files(ctx, rf"redhat/{sub}cassandra(-tools)?-{re.escape(deb_version(ctx.version))}-[^/]+\.noarch\.rpm$")
        name = "noboolean" if sub else "rpm"
        label = f"RPM package{' noboolean' if sub else ''} ({image})"
        if not rpms:
            r.skip(label, "no .rpm files downloaded")
            continue
        log = run.log(f"{name}-{image.replace(':', '')}")
        r.note(f"Installing the {name} packages on {image} (log: {log}) ...")
        script = fill(PACKAGE, INSTALL=RPM_INSTALL, WAIT=str(run.timeout), READY=CQL_READY,
                      USAGE=usage(run, "nodetool", "cqlsh", "sstablemetadata", "/var/lib/cassandra/data"))
        out = run.engine.run(image, script, log=log, timeout=run.timeout + 600,
                             mounts={run.copy_files(f"{name}-pkgs", rpms): "/pkgs:ro"})
        if report(r, label, out):
            run.passed.append(f"{'noboolean ' if sub else ''}.rpm packages install and run on {image}")
    if len(variants) > 1:
        r.info("RPM noboolean", "built for RPM without boolean dependencies (EL7); tested on AlmaLinux 8, since EL7 "
                                "images no longer have working repositories")

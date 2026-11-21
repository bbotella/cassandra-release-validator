"""Apache Cassandra Sidecar: the binary tarball, the packages and the Java client, used as an operator would.

  binary     start the tarball next to each Cassandra server (inside its container, as user cassandra) with the
             shipped sidecar.yaml pointed at the node; health (process, CQL, JMX), node settings (Cassandra and
             Sidecar versions), CQL-backed settings, schema and ring through the HTTP API
  client     every staged artifact resolves with its dependencies; a program on sidecar-vertx-client (JDK 11, and
             <version>-jdk8 on JDK 8 when staged) calls the running Sidecar
  analytics  the newest released Cassandra Analytics bulk-writes and reads through this Sidecar (Sidecar's main
             consumer; a break is a WARN: compatibility with another project's release)
  deb, rpm   install the package on Debian bookworm / AlmaLinux 9, start /usr/local/bin/cassandra-sidecar,
             the same API checks
"""

from __future__ import annotations

import re

from ... import net
from ...projects import MAVEN_CENTRAL, SIDECAR
from . import maven_consumer as mc
from . import sidecar_runtime
from .common import SmokeRun, provenance_report, release_files
from .engine import SetupError, fill, report
from .programs import DIR as PROGRAMS
from .server import Server, images

TESTS = ("binary", "client", "analytics", "deb", "rpm")

PACKAGE = """
@HOSTS@
@PREPARE@
mkdir -p /var/lib/cassandra/data /var/lib/cassandra/sstable-staging  # the node's directories live in its own container
crv_run install @INSTALL@ || { crv_done; exit 0; }
crv_run layout test -x /opt/apache-cassandra-sidecar/bin/cassandra-sidecar -a -f /opt/apache-cassandra-sidecar/conf/sidecar.yaml -a -x /usr/local/bin/cassandra-sidecar || { crv_done; exit 0; }
python3 /programs/sidecar_checks.py edit-config /opt/apache-cassandra-sidecar/conf/sidecar.yaml @HOST@ || { crv_done; exit 0; }
crv_step java INFO "$(java -version 2>&1 | head -n 1)"
cassandra-sidecar > /tmp/sidecar.out 2>&1 &
python3 /programs/sidecar_checks.py check @HOST@ @SIDECAR@ @CASSANDRA@ @WAIT@
echo '---- Sidecar output'; tail -n 100 /tmp/sidecar.out
"""

PACKAGES = {
    "deb": ("debian:bookworm-slim",
            "crv_prepare packages bash -c 'apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y "
            "openjdk-17-jre-headless python3 > /dev/null'",
            "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y /pkgs/*.deb'"),
    "rpm": ("almalinux:9",
            "crv_prepare packages dnf -q install -y java-17-openjdk-headless python3",
            "dnf install -y /pkgs/*.rpm"),
}


def run(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    first: Server | None = None
    if any(run.wants(t) for t in ("binary", "client", "analytics")):
        if ctx.bin_root is None:
            r.skip("Sidecar binary tarball", "binary tarball not extracted")
        else:
            if run.wants("client"):
                _resolve(run)
            for image in images(ctx):
                server = run.servers.get(image, compat_none=True)  # as Analytics runs it
                if server is None:
                    continue
                first = first or server
                _binary(run, server, analytics=run.wants("analytics") and server is first)
    for kind in ("deb", "rpm"):
        if run.wants(kind):
            server = first or next((s for s in (run.servers.get(i, compat_none=True) for i in images(ctx)) if s), None)
            if server is not None:
                _package(run, kind, server)


def _resolve(run: SmokeRun) -> None:
    arts = mc.usable(mc.artifacts(run.ctx))
    if not arts:
        run.report.skip("Maven artifacts resolve", "no verified Maven artifacts (was the maven group skipped?)")
        return
    out, m2 = mc.resolve(run.ctx, run.engine, run.dir, "maven-resolve", arts, "17", run.timeout * 2)
    if report(run.report, "Maven artifacts resolve", out) and provenance_report(run, m2, "Maven artifacts resolve"):
        run.passed.append(f"{len(arts)} Maven artifacts resolve with their dependencies")


def _binary(run: SmokeRun, server: Server, analytics: bool) -> None:
    ctx, r = run.ctx, run.report
    label = f"Binary tarball on {server.label}"
    log = run.log(f"sidecar-binary-{server.image.replace(':', '-')}")
    try:
        home = sidecar_runtime.prepare(ctx.bin_root, run.dir / f"sidecar-{server.image.replace(':', '-')}", server.hostname)
    except ValueError as e:
        r.fail(f"{label}: config", str(e))
        return
    r.note(f"Starting the Sidecar binary tarball next to {server.label} (log: {log}) ...")
    try:
        sidecar_runtime.start(server, home)
    except SetupError as e:
        r.warn(f"{label}: not run", str(e))
        return
    try:
        out = sidecar_runtime.check(ctx, run.engine, server, ctx.version, log)
        if report(r, label, out):
            run.passed.append(f"binary tarball next to {server.label} (health, CQL, JMX, settings, schema, ring)")
        if not out.ok:
            return
        if run.wants("client"):
            _client(run, server)
        if analytics:
            _analytics(run, server)
    finally:
        sidecar_runtime.stop(server, log)


def _client(run: SmokeRun, server: Server) -> None:
    ctx = run.ctx
    clients = sorted({a.version for a in mc.artifacts(ctx) if a.artifact == "sidecar-vertx-client"})
    if not clients:
        run.report.skip("Sidecar Java client", "sidecar-vertx-client is not among the verified Maven artifacts")
        return
    for version in clients:
        jdk = "8" if version.endswith("-jdk8") else "11"
        label = f"Java client {version} (JDK {jdk}) against {server.label}"
        name = f"sidecar-client-{version}-{server.image.replace(':', '-')}"
        project = mc.project_dir(run.dir, name, {"SidecarClientSmoke.java": PROGRAMS / "SidecarClientSmoke.java"})
        (project / "pom.xml").write_text(mc.pom_xml(name, [mc.dependency_xml(mc.GROUP, "sidecar-vertx-client", version)], jdk))
        log = run.log(name)
        run.report.note(f"{label} (log: {log}) ...")
        script = mc.program("SidecarClientSmoke", f"{server.hostname} {ctx.version}", before=server.hosts_line)
        out = mc.run(ctx, run.engine, project, script, jdk=jdk, log=log,
                     timeout=run.timeout * 2, network=server.network)
        if report(run.report, label, out) and provenance_report(run, project / "m2", label):
            run.passed.append(f"sidecar-vertx-client {version} on JDK {jdk} talks to the running Sidecar")


def latest_analytics() -> str | None:
    """The newest Cassandra Analytics release on Maven Central."""
    try:
        meta = net.fetch_text(f"{MAVEN_CENTRAL}org/apache/cassandra/cassandra-analytics-core_spark3_2.12/maven-metadata.xml")
    except net.HttpError:
        return None
    m = re.search(r"<release>([^<]+)</release>", meta)
    return m.group(1).strip() if m else None


def _analytics(run: SmokeRun, server: Server) -> None:
    from .analytics import FALLBACK, Profile, bulk_job
    version = latest_analytics()
    if not version:
        run.report.warn("Compatibility with released Analytics: not run", "no Cassandra Analytics release found on Maven Central")
        return
    spark, jdk = FALLBACK[("3", "2.12")]
    profile = Profile("3", "2.12", spark, jdk)
    label = f"Compatibility with released Analytics {version} ({profile.label}) on {server.label}"
    if bulk_job(run, server, profile, version, label, as_warning=True):
        run.passed.append(f"the released Cassandra Analytics {version} bulk-writes and reads through this Sidecar")


def _package(run: SmokeRun, kind: str, server: Server) -> None:
    ctx, r = run.ctx, run.report
    v = re.escape(ctx.version)
    prefix = re.escape(SIDECAR.tarball_prefix)
    pattern = rf"^debian/{prefix}_{v}(-\d+)?_all\.deb$" if kind == "deb" else rf"^redhat/{prefix}-{v}(-\d+)?\.noarch\.rpm$"
    files = release_files(ctx, pattern)
    image, prepare, install = PACKAGES[kind]
    label = f"{'Debian' if kind == 'deb' else 'RPM'} package ({image}) next to {server.label}"
    if not files:
        r.skip(label, f"no .{kind} file downloaded")
        return
    log = run.log(f"sidecar-{kind}")
    r.note(f"Installing the Sidecar .{kind} on {image} (log: {log}) ...")
    script = fill(PACKAGE, HOSTS=server.hosts_line, HOST=server.hostname, PREPARE=prepare, INSTALL=install,
                  SIDECAR=ctx.version, CASSANDRA=server.version or "unknown",
                  WAIT=str(min(run.timeout, 300)))
    out = run.engine.run(image, script, log=log, timeout=run.timeout + 600, network=server.network,
                         mounts={run.copy_files(f"sidecar-{kind}-pkgs", files): "/pkgs:ro", PROGRAMS: "/programs:ro"})
    if report(r, label, out):
        run.passed.append(f".{kind} package installs and serves the API on {image}")

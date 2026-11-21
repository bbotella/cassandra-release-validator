"""Apache Cassandra Analytics: the published jars used the way a Spark job uses them.

  resolve  every staged artifact resolves with its dependencies, none of them a SNAPSHOT or from outside the
           staged repositories
  bulk     per Spark/Scala build staged (core_spark3_2.12, core_spark3_2.13, core_spark4_2.13) and per Cassandra
           server: a Spark job in local mode with only cassandra-analytics-core_<spark>_<scala> declared (what a
           user declares) bulk-writes rows through a released Sidecar next to the node, bulk-reads them back from a
           snapshot, and the rows are checked through CQL too

The Spark, Scala and JDK versions are the build profile's (profiles/*.gradle in the verified source tarball).
Spark 4 runs on Cassandra 5.0+ only, as the project's CI does; 5.0 runs with storage_compatibility_mode NONE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import maven_consumer as mc
from . import sidecar_runtime
from .common import SmokeRun, provenance_report
from .engine import SetupError, fill, report
from .programs import DIR as PROGRAMS
from .server import Server, images, series

TESTS = ("resolve", "bulk")
ROWS = 10000
KEYSPACE = "crv_smoke_analytics"
CORE = re.compile(r"^cassandra-analytics-core_spark(\d+)_(2\.\d+)$")
# Spark, JDK of each build profile, when the source tarball doesn't say (cassandra-analytics trunk, Nov 2026)
FALLBACK = {("3", "2.12"): ("3.2.2", "11"), ("3", "2.13"): ("3.2.2", "11"), ("4", "2.13"): ("4.0.1", "17")}
PROFILE_FILE = re.compile(r"^scala-(2\.\d+)-spark-(\d+)-jdk-(\d+)\.gradle$")


@dataclass(frozen=True)
class Profile:
    spark_major: str
    scala: str
    spark_version: str
    jdk: str

    @property
    def core(self) -> str:
        return f"cassandra-analytics-core_spark{self.spark_major}_{self.scala}"

    @property
    def label(self) -> str:
        return f"Spark {self.spark_version}, Scala {self.scala}, JDK {self.jdk}"


def profile_versions(src_root: Path | None) -> dict[tuple[str, str], tuple[str, str]]:
    """(spark major, scala) -> (spark version, jdk) from profiles/*.gradle in the source tree."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    folder = src_root / "profiles" if src_root else None
    if folder is None or not folder.is_dir():
        return found
    for f in sorted(folder.iterdir()):
        m = PROFILE_FILE.match(f.name)
        if not m:
            continue
        spark = re.search(r'sparkVersion\s*=\s*["\']([^"\']+)["\']', f.read_text(errors="replace"))
        if spark:
            found[(m.group(2), m.group(1))] = (spark.group(1), m.group(3))
    return found


def profiles(core_artifacts: list[str], src_root: Path | None) -> list[Profile]:
    versions = {**FALLBACK, **profile_versions(src_root)}
    out = []
    for name in sorted(set(core_artifacts)):
        m = CORE.match(name)
        if m and (m.group(1), m.group(2)) in versions:
            spark, jdk = versions[(m.group(1), m.group(2))]
            out.append(Profile(m.group(1), m.group(2), spark, jdk))
    return out


def run(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    arts = mc.artifacts(ctx)
    if not arts:
        r.skip("Analytics smoke tests", "no verified Maven artifacts (was the maven group skipped?)")
        return
    if run.wants("resolve"):
        _resolve(run, mc.usable(arts))
    if run.wants("bulk"):
        found = profiles([a.artifact for a in arts], ctx.src_root)
        if not found:
            r.fail("Analytics bulk write and read", "no cassandra-analytics-core_spark<N>_<scala> artifact staged")
            return
        _bulk(run, found)


def _resolve(run: SmokeRun, arts: list[mc.Artifact]) -> None:
    out, m2 = mc.resolve(run.ctx, run.engine, run.dir, "maven-resolve", arts, "17", run.timeout * 3)
    if report(run.report, "Maven artifacts resolve", out):
        if provenance_report(run, m2, "Maven artifacts resolve"):
            run.passed.append(f"{len(arts)} Maven artifacts resolve with their dependencies")


def _bulk(run: SmokeRun, found: list[Profile]) -> None:
    ctx, r = run.ctx, run.report
    try:
        sidecar_version, sidecar_home = sidecar_runtime.released(ctx, ctx.args.sidecar_version)
    except Exception as e:  # noqa: BLE001 - network, signature or layout: the environment, not this release
        r.warn("Analytics bulk write and read: not run", f"no usable Sidecar release: {e}")
        return
    r.info("Sidecar for the Analytics tests", f"released Sidecar {sidecar_version} (signature verified against KEYS)")
    for image in images(ctx):
        server = run.servers.get(image, compat_none=True)
        if server is None:
            continue
        todo = [p for p in found if p.spark_major != "4" or series(server.version) >= (5, 0)]
        for p in found:
            if p not in todo:
                r.info(f"Analytics {p.label} on {server.label}", "not run: Spark 4 is tested on Cassandra 5.0+ only, as in CI")
        if not todo:
            continue
        log = run.log(f"analytics-sidecar-{image.replace(':', '-')}")
        try:
            sidecar_runtime.start(server, sidecar_runtime.prepare(sidecar_home, run.dir / f"sidecar-{sidecar_version}",
                                                                  server.hostname))
        except SetupError as e:
            r.warn(f"Analytics on {server.label}: not run", f"could not start Sidecar {sidecar_version}: {e}")
            continue
        try:
            for p in todo:
                if bulk_job(run, server, p, ctx.version, f"Analytics {p.label} on {server.label}"):
                    run.passed.append(f"bulk write and read with {p.core} ({p.label}) on {server.label}")
        finally:
            sidecar_runtime.stop(server, log)


CQL_COUNT = """
n=$(cqlsh -e 'SELECT count(*) FROM @KEYSPACE@.@TABLE@' | tr -d ' ' | grep -E '^[0-9]+$' | head -n 1)
if [ "$n" = "@ROWS@" ]; then crv_step cql-check OK "$n rows through CQL"
else crv_step cql-check FAIL "count(*) through CQL is '$n', expected @ROWS@"; fi
crv_done
"""


def bulk_job(run: SmokeRun, server: Server, p: Profile, version: str, label: str, as_warning: bool = False) -> bool:
    """One bulk write and read with cassandra-analytics-core <version> against server's Sidecar."""
    r = run.report
    table = f"t_spark{p.spark_major}_{p.scala.replace('.', '')}_{version.replace('.', '').replace('-', '')}"
    name = f"analytics-{table}-{server.image.replace(':', '-')}"
    # the bulk writer needs the keyspace replicated on the local datacenter by name
    ddl = (f"CREATE KEYSPACE IF NOT EXISTS {KEYSPACE} WITH replication = {{'class': 'NetworkTopologyStrategy', "
           f"'datacenter1': 1}}; DROP TABLE IF EXISTS {KEYSPACE}.{table}; "
           f"CREATE TABLE {KEYSPACE}.{table} (id int PRIMARY KEY, value text)")
    proc = server.container.call(["cqlsh", "-e", ddl], timeout=120)
    if proc.returncode != 0:
        r.warn(f"{label}: not run", f"could not create the table: {(proc.stderr or proc.stdout).strip()[-200:]}")
        return False
    project = mc.project_dir(run.dir, name, {"AnalyticsSmoke.java": PROGRAMS / "AnalyticsSmoke.java",
                                             "PrintOptions.java": PROGRAMS / "PrintOptions.java"})
    deps = [mc.dependency_xml(mc.GROUP, p.core, version),
            mc.dependency_xml("org.apache.spark", f"spark-sql_{p.scala}", p.spark_version)]
    (project / "pom.xml").write_text(mc.pom_xml(name, deps, p.jdk))
    log = run.log(name)
    r.note(f"{label}: bulk write and read (log: {log}) ...")
    # The job shares the node's network namespace and host name, but not its /etc/hosts: map the name, which
    # Spark resolves for itself and the ring hands out as the node's (and its Sidecar's) address
    before = server.hosts_line + f'CRV_JVM_OPTS="$(java -cp "$CP" PrintOptions {p.jdk}) -Xmx2g"'
    script = mc.program("AnalyticsSmoke", f"{KEYSPACE} {table} {ROWS} {server.version} {server.hostname}", before=before)
    out = mc.run(run.ctx, run.engine, project, script, jdk=p.jdk, log=log, timeout=run.timeout * 3,
                 network=server.network, env={"SPARK_LOCAL_IP": "127.0.0.1"})
    ok = report(r, label, out, as_warning=as_warning)
    if ok:
        check = server.container.exec(fill(CQL_COUNT, KEYSPACE=KEYSPACE, TABLE=table, ROWS=str(ROWS)), log=log,
                                      timeout=120)
        ok = report(r, f"{label}: rows through CQL", check, as_warning=as_warning)
    return ok

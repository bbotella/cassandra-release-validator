"""Apache Cassandra Java Driver: the staged Maven artifacts used by an application's build.

  resolve  every staged artifact resolves with its dependencies, none of them a SNAPSHOT or from outside the
           staged repositories
  client   4.x: java-driver-core, -query-builder and -mapper-runtime, with -mapper-processor generating the mapper
           at build time; a program uses CqlSession, prepared statements, the query builder and a DAO; it runs
           again on java-driver-core-shaded. 3.x: cassandra-driver-core and -mapping. Each on the oldest and newest
           JDK the line supports (8 and 21 for 4.x, 8 and 17 for 3.x; --jdks to choose), against every server.
"""

from __future__ import annotations

from .common import SmokeRun, provenance_report
from . import maven_consumer as mc
from .engine import report
from .programs import DIR as PROGRAMS
from .server import images

TESTS = ("resolve", "client")


def run(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    arts = mc.artifacts(ctx)
    if not arts:
        r.skip("Java driver smoke tests", "no verified Maven artifacts (was the maven group skipped?)")
        return
    names = {a.artifact for a in arts}
    if run.wants("resolve"):
        usable = mc.usable(arts)
        out, m2 = mc.resolve(ctx, run.engine, run.dir, "maven-resolve", usable, "17", run.timeout * 2)
        if report(r, "Maven artifacts resolve", out) and provenance_report(run, m2, "Maven artifacts resolve"):
            run.passed.append(f"{len(usable)} Maven artifacts resolve with their dependencies")
    if not run.wants("client"):
        return
    four = "java-driver-core" in names
    if not four and "cassandra-driver-core" not in names:
        r.fail("Java driver client", "neither java-driver-core nor cassandra-driver-core is staged")
        return
    jdks = [j.strip() for j in ctx.args.jdks.split(",")] if ctx.args.jdks else (["8", "21"] if four else ["8", "17"])
    servers = [s for s in (run.servers.get(i) for i in images(ctx)) if s]
    for server in servers:
        for jdk in jdks:
            _client(run, server, jdk, four, shaded=False)
    if four and servers and "java-driver-core-shaded" in names:
        _client(run, servers[0], jdks[-1], four, shaded=True)


def _client(run: SmokeRun, server, jdk: str, four: bool, shaded: bool) -> None:
    ctx, v = run.ctx, run.ctx.version
    flavour = "4.x shaded core" if shaded else "4.x" if four else "3.x"
    label = f"Java driver {flavour} (JDK {jdk}) against {server.label}"
    name = f"java-driver-{'shaded-' if shaded else ''}jdk{jdk}-{server.image.replace(':', '-')}"
    folder = PROGRAMS / ("javadriver4" if four else "javadriver3")
    sources = {str(p.relative_to(folder)): p for p in sorted(folder.rglob("*.java"))}
    project = mc.project_dir(run.dir, name, sources)
    if four:
        core = "java-driver-core-shaded" if shaded else "java-driver-core"
        unshaded = [(mc.GROUP, "java-driver-core")] if shaded else []
        deps = [mc.dependency_xml(mc.GROUP, core, v),
                mc.dependency_xml(mc.GROUP, "java-driver-query-builder", v, exclusions=unshaded),
                mc.dependency_xml(mc.GROUP, "java-driver-mapper-runtime", v, exclusions=unshaded)]
        processors = [(mc.GROUP, "java-driver-mapper-processor", v)]
    else:
        deps = [mc.dependency_xml(mc.GROUP, "cassandra-driver-core", v),
                mc.dependency_xml(mc.GROUP, "cassandra-driver-mapping", v)]
        processors = []
    (project / "pom.xml").write_text(mc.pom_xml(name, deps, jdk, processors))
    log = run.log(name)
    run.report.note(f"{label} (log: {log}) ...")
    out = mc.run(ctx, run.engine, project, mc.program("crvsmoke.Main", v), jdk=jdk, log=log,
                 timeout=run.timeout * 2, network=server.network)
    if report(run.report, label, out) and provenance_report(run, project / "m2", label):
        run.passed.append(f"{flavour} driver on JDK {jdk} against {server.label}")

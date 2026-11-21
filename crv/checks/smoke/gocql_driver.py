"""Apache Cassandra GoCQL Driver: the source tarball used as the Go module it is.

  module  a consumer module imports the driver by the module path in its go.mod, wired with `replace` to the
          verified source tarball (read-only); go mod tidy, go build, then the program talks to every Cassandra
          server (schema, bound inserts, batch, select, iteration)
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import SmokeRun
from .engine import fill, report
from .programs import DIR as PROGRAMS
from .server import images

TESTS = ("module",)

GO_MOD = """module crvsmoke

go @GO@

require @MODULE@ @PSEUDO@

replace @MODULE@ => /src
"""

SCRIPT = """
cd /work
crv_step go INFO "$(go version)"
crv_run resolve go mod tidy || { crv_done; exit 0; }
crv_run build go build -o /tmp/crv-smoke . || { crv_done; exit 0; }
/tmp/crv-smoke
"""


def module_info(go_mod: str) -> tuple[str | None, str | None, str | None]:
    """(module path, go directive, toolchain version) of a go.mod."""
    module = re.search(r"(?m)^module\s+(\S+)", go_mod)
    go = re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)", go_mod)
    toolchain = re.search(r"(?m)^toolchain\s+go(\d+\.\d+(?:\.\d+)?)", go_mod)
    return (module.group(1) if module else None, go.group(1) if go else None,
            toolchain.group(1) if toolchain else None)


def placeholder_version(module: str) -> str:
    """A version for the require line (replace points it at /src); a /vN module path needs a vN version."""
    m = re.search(r"/v(\d+)$", module)
    major = m.group(1) if m and int(m.group(1)) >= 2 else "0"
    return f"v{major}.0.0-00010101000000-000000000000"


def image_for(go: str, toolchain: str | None) -> str:
    """golang:<major.minor> of the toolchain (or go directive) the module asks for."""
    major_minor = ".".join((toolchain or go).split(".")[:2])
    return f"golang:{major_minor}"


def run(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    if ctx.src_root is None or not (ctx.src_root / "go.mod").exists():
        r.skip("Go module", "source tarball not extracted, or it has no go.mod")
        return
    module, go, toolchain = module_info((ctx.src_root / "go.mod").read_text(errors="replace"))
    if not module or not go:
        r.fail("Go module", "go.mod has no module path or go directive")
        return
    image = image_for(go, toolchain)
    for server in (s for s in (run.servers.get(i) for i in images(ctx)) if s):
        label = f"Go module {module} ({image}) against {server.label}"
        work = run.fresh(f"go-{server.image.replace(':', '-')}")
        (work / "go.mod").write_text(fill(GO_MOD, GO=go, MODULE=module, PSEUDO=placeholder_version(module)))
        (work / "main.go").write_text(fill((PROGRAMS / "gocql_smoke.go.tmpl").read_text(), MODULE=module))
        log = run.log(f"go-{server.image.replace(':', '-')}")
        r.note(f"{label} (log: {log}) ...")
        out = run.engine.run(image, SCRIPT, log=log, timeout=run.timeout + 600, network=server.network,
                             mounts={work: "/work", Path(ctx.src_root): "/src:ro"}, env={"GOFLAGS": "-mod=mod"})
        if report(r, label, out):
            run.passed.append(f"a Go program built against the source tarball's module works against {server.label}")

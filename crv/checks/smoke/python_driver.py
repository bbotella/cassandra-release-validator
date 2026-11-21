"""Apache Cassandra Python Driver: the sdist and the wheels, installed with pip and used against a node.

  sdist   pip install the source distribution (builds the C extensions, needs gcc and libev) on the newest
          Python the wheels cover, against every Cassandra server
  wheels  pip install each Linux wheel on its own Python version and CPU (aarch64 natively here, x86_64 under
          emulation), against the first server; a wheel that ships compiled modules must load them
          (cassandra.cython_deps.HAVE_CYTHON). macOS and Windows wheels can't run in a Linux container.

pip installs exactly the staged file given to it; only the driver's dependencies come from PyPI.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .common import SmokeRun
from .engine import fill, report
from .programs import DIR as PROGRAMS
from .server import images

TESTS = ("sdist", "wheels")
WHEEL = re.compile(r"-(?P<py>cp3(?P<minor>\d+))-[^-]+-(?P<plat>[^-]+)\.whl$")

SCRIPT = """
@PREPARE@
crv_step python INFO "$(python3 --version 2>&1) on $(uname -m)"
cd /
crv_run install pip install --no-cache-dir --root-user-action=ignore /in/@FILE@ || { crv_done; exit 0; }
python3 /programs/python_smoke.py @VERSION@ @COMPILED@
"""
SDIST_PREPARE = ("crv_prepare build-tools bash -c 'apt-get -qq update && apt-get -qq install -y gcc libev-dev "
                 "> /dev/null'")


def wheel_target(name: str) -> tuple[str, str] | None:
    """(image, platform) that can install and run a wheel, or None for a platform containers don't offer."""
    m = WHEEL.search(name)
    if not m or "manylinux" not in m.group("plat"):
        return None
    plat = m.group("plat")
    arch = "linux/arm64" if "aarch64" in plat else "linux/amd64" if "x86_64" in plat else None
    if arch is None:
        return None
    return f"python:3.{m.group('minor')}-slim", arch


def compiled(wheel: Path) -> bool:
    with zipfile.ZipFile(wheel) as z:
        return any(n.endswith((".so", ".pyd")) for n in z.namelist())


def run(run: SmokeRun) -> None:
    ctx, r = run.ctx, run.report
    p = ctx.project
    wheels = {rel.rsplit("/", 1)[-1]: path for rel, path in ctx.dist_files.items()
              if p.wheels_dir and rel.startswith(p.wheels_dir) and rel.endswith(".whl")}
    servers = [s for s in (run.servers.get(i) for i in images(ctx)) if s] if (run.wants("sdist") or wheels) else []
    if not servers:
        return
    if run.wants("sdist"):
        sdist = ctx.dist_files.get(p.src_tarball(ctx.version))
        if sdist is None:
            r.skip("sdist", "source distribution not downloaded")
        else:
            minors = [int(m.group("minor")) for n in wheels if (m := WHEEL.search(n))]
            image = f"python:3.{max(minors) if minors else 12}-slim"
            for server in servers:
                if _install(run, server, image, None, sdist, False, f"sdist on {image} against {server.label}"):
                    run.passed.append(f"the sdist builds, installs and works on {image} against {server.label}")
    if run.wants("wheels"):
        if not wheels:
            r.skip("Wheels", "no wheels downloaded")
            return
        untested, good, tried = [], 0, 0
        for name, path in sorted(wheels.items()):
            target = wheel_target(name)
            if target is None:
                untested.append(name)
                continue
            image, platform = target
            tried += 1
            good += _install(run, servers[0], image, platform, path, compiled(path),
                             f"wheel {name.split('-', 2)[2]} on {image} ({platform})")
        if tried and good == tried:
            run.passed.append(f"all {tried} Linux wheels install and work against {servers[0].label}")
        if untested:
            r.info("Wheels not run", f"{len(untested)} wheels are for platforms a Linux container can't run", untested)


def _install(run: SmokeRun, server, image: str, platform: str | None, dist: Path, has_compiled: bool, label: str) -> bool:
    r = run.report
    name = re.sub(r"[^A-Za-z0-9._-]", "-", label)[:120]
    log = run.log(name)
    r.note(f"{label} (log: {log}) ...")
    script = fill(SCRIPT, PREPARE=SDIST_PREPARE if platform is None else "", FILE=dist.name, VERSION=run.ctx.version,
                  COMPILED="1" if has_compiled else "0")
    out = run.engine.run(image, script, log=log, timeout=run.timeout + 600, network=server.network, platform=platform,
                         mounts={run.copy_files(f"in-{name}", [dist]): "/in:ro", PROGRAMS: "/programs:ro"})
    return report(r, label, out)

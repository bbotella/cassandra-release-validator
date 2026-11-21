"""Running a Cassandra Sidecar binary tarball next to a smoke-test node, and checking its API.

Sidecar runs inside the node's container as the node's user `cassandra`: it must see the data and staging
directories at the node's own paths (Cassandra Analytics streams snapshots from them and imports uploaded
SSTables into them), and files it stages must be movable by Cassandra.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ... import archive, net
from ...context import Context
from ...projects import DIST_RELEASE, SIDECAR
from .engine import Engine, Outcome, SetupError, fill
from .programs import DIR as PROGRAMS
from .programs.sidecar_checks import edit_config
from .server import Server

HOME = "/opt/crv-sidecar"
CHECK_IMAGE = "python:3.12-slim"

START = f"""
echo $$ > /tmp/crv-sidecar.pid
export JAVA_OPTS="-Dsidecar.config=file://{HOME}/conf/sidecar.yaml -Dsidecar.logdir={HOME}/logs"
exec {HOME}/bin/cassandra-sidecar > /tmp/crv-sidecar.out 2>&1
"""

CHECK = """
@HOSTS@
python3 /programs/sidecar_checks.py check @HOST@ @SIDECAR@ @CASSANDRA@ @WAIT@
"""


def prepare(home: Path, dest: Path, host: str) -> Path:
    """A copy of an extracted Sidecar with its shipped sidecar.yaml pointed at the node (by its host name)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(home, dest, symlinks=True)
    config = dest / "conf" / "sidecar.yaml"
    config.write_text(edit_config(config.read_text(), host))
    return dest


def start(server: Server, home: Path) -> None:
    """Copies the prepared Sidecar into the node's container and starts it there (in the background)."""
    c = server.container
    c.call(["rm", "-rf", HOME], user="root")
    c.copy_in(home, HOME)
    proc = c.call(["chown", "-R", "cassandra:cassandra", HOME], user="root")
    if proc.returncode != 0:
        raise SetupError(f"chown of {HOME} failed: {proc.stderr.strip()[-200:]}")
    proc = c.call(["/bin/bash", "-c", START], user="cassandra", detach=True)
    if proc.returncode != 0:
        raise SetupError(f"could not start Sidecar: {(proc.stderr or proc.stdout).strip()[-300:]}")


def stop(server: Server, log: Path) -> None:
    c = server.container
    out = c.call(["/bin/bash", "-c", "cat /tmp/crv-sidecar.out 2>/dev/null; kill $(cat /tmp/crv-sidecar.pid) 2>/dev/null; "
                  "sleep 3; true"], user="root", timeout=60)
    with open(log, "a") as f:
        f.write("---- Sidecar output\n" + out.stdout[-60000:])


def check(ctx: Context, engine: Engine, server: Server, sidecar_version: str, log: Path) -> Outcome:
    """The API checks, from a small Python container in the node's network namespace."""
    script = fill(CHECK, HOSTS=server.hosts_line, HOST=server.hostname, SIDECAR=sidecar_version,
                  CASSANDRA=server.version or "unknown", WAIT=str(min(ctx.args.smoke_timeout, 300)))
    return engine.run(CHECK_IMAGE, script, log=log, timeout=ctx.args.smoke_timeout, network=server.network,
                      mounts={PROGRAMS: "/programs:ro"})


# ------------------------------------------------------- a released Sidecar
VERSION_DIR = re.compile(r"^(\d+(?:\.\d+)+)/$")


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def released(ctx: Context, version: str | None) -> tuple[str, Path]:
    """Downloads a released Sidecar binary tarball from dist/release (the newest unless version is given),
    verifies its signature against KEYS, and returns (version, extracted top directory)."""
    base = DIST_RELEASE + SIDECAR.dist_subdir
    if not version:
        versions = [m.group(1) for n in net.list_dir(base) if (m := VERSION_DIR.match(n))]
        if not versions:
            raise SetupError(f"no Sidecar release found under {base}")
        version = max(versions, key=_key)
    name = SIDECAR.bin_tarball(version)
    cache = ctx.workdir.parent / "smoke-cache" / f"sidecar-{version}"
    tarball = net.download(f"{base}{version}/{name}", cache / name)
    signature = net.download(f"{base}{version}/{name}.asc", cache / f"{name}.asc")
    result = ctx.keyring.verify(signature, tarball)
    if not result.ok:
        raise SetupError(f"the signature of the released {name} does not verify against KEYS")
    top = archive.extract(tarball, cache / "extract")
    if not (top / "bin" / "cassandra-sidecar").exists():
        raise SetupError(f"unexpected layout in {name}")
    return version, top

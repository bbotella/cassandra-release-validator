"""The Cassandra node client smoke tests talk to: an official cassandra:<series> image, started once per image.

Clients join the node's network namespace (--network container:<node>), so they reach CQL on 127.0.0.1:9042,
JMX on 127.0.0.1:7199 (local only, no authentication) and a Sidecar next to the node on 127.0.0.1:9043.
"""

from __future__ import annotations

import time
from pathlib import Path

from ...context import Context
from .engine import Container, Engine, SetupError

DEFAULT_IMAGES = ("cassandra:5.0", "cassandra:4.1")
HEAP = {"MAX_HEAP_SIZE": "1G", "HEAP_NEWSIZE": "256M"}
CQL_PROBE = ["cqlsh", "-e", "SELECT release_version FROM system.local"]


def images(ctx: Context) -> list[str]:
    return list(ctx.args.cassandra_image or DEFAULT_IMAGES)


def series(version: str) -> tuple[int, int]:
    parts = (version.split("-", 1)[0].split(".") + ["0"])[:2]
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


class Server:
    def __init__(self, container: Container, image: str, version: str, hostname: str = "", address: str = ""):
        self.container = container
        self.image = image
        self.version = version
        self.hostname = hostname or "localhost"  # the node's name: what the ring reports and Sidecar is configured with
        self.address = address or "127.0.0.1"

    @property
    def hosts_line(self) -> str:
        """Shell that makes the node's name resolve in a container sharing its network namespace (which gets
        the node's host name but not its /etc/hosts)."""
        return f'grep -qw "{self.hostname}" /etc/hosts || echo "{self.address} {self.hostname}" >> /etc/hosts\n'

    @property
    def network(self) -> str:
        return self.container.network

    @property
    def label(self) -> str:
        return f"Cassandra {self.version}" if self.version else self.image


class Servers:
    """Starts each image the first time a test asks for it, and removes them all at the end."""

    def __init__(self, ctx: Context, engine: Engine, run_dir: Path):
        self.ctx = ctx
        self.engine = engine
        self.run_dir = run_dir
        self._started: dict[tuple[str, bool], Server | None] = {}

    def get(self, image: str, compat_none: bool = False) -> Server | None:
        """The running node, or None (with a WARN in the report) when it can't be started.

        compat_none: storage_compatibility_mode NONE on 5.0+, as Cassandra Analytics' CI runs it.
        """
        key = (image, compat_none)
        if key not in self._started:
            self._started[key] = self._start(image, compat_none)
        return self._started[key]

    def _start(self, image: str, compat_none: bool) -> Server | None:
        r = self.ctx.report
        safe = image.replace("/", "_").replace(":", "-") + ("-compat-none" if compat_none else "")
        log = self.run_dir / f"server-{safe}.log"
        r.note(f"Starting {image} for the client smoke tests (log: {log}) ...")
        script = ""
        if compat_none:
            script += ("sed -i 's/^storage_compatibility_mode:.*/storage_compatibility_mode: NONE/' "
                       "/etc/cassandra/cassandra.yaml; ")
        script += "exec docker-entrypoint.sh cassandra -f"
        try:
            container = self.engine.start(image, log=log, entrypoint="/bin/bash", command=["-c", script], env=HEAP)
        except SetupError as e:
            r.warn(f"Server {image}: not run", f"{e} (log: {log})")
            return None
        version = self.engine.image_env(image).get("CASSANDRA_VERSION", "")
        deadline = time.time() + self.ctx.args.smoke_timeout
        while time.time() < deadline:
            if container.call(CQL_PROBE, timeout=60).returncode == 0:
                r.info(f"Server {image}", f"Apache Cassandra {version or '?'} is up" +
                       (" (storage_compatibility_mode NONE)" if compat_none else ""))
                name = container.call(["/bin/bash", "-c", 'h=$(cat /etc/hostname); echo "$h $(getent hosts "$h" | '
                                                          'awk \'{print $1}\' | head -n 1)"'], timeout=60).stdout.split()
                return Server(container, image, version, *(name + ["", ""])[:2])
            time.sleep(3)
        with open(log, "a") as out:
            out.write(container.logs()[-20000:])
        container.remove()
        r.warn(f"Server {image}: not run", f"CQL did not answer within {self.ctx.args.smoke_timeout}s (log: {log})")
        return None

    def __enter__(self) -> Servers:
        return self

    def __exit__(self, *exc) -> None:
        for server in self._started.values():
            if server is not None:
                server.container.remove()

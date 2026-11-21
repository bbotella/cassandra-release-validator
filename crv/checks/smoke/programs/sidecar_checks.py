"""Cassandra Sidecar checks, run inside a container next to Sidecar (Python standard library only).

    sidecar_checks.py edit-config <sidecar.yaml> <node host name>   point the shipped config at the node
    sidecar_checks.py check <node host name> <sidecar version> <cassandra version> <seconds>

Prints the smoke protocol (CRV-STEP ... / CRV-DONE).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9043"  # check() points it at the node's host name: Sidecar routes by the Host header
# The shipped sidecar.yaml's only instance is "host: localhost1", a name that resolves nowhere else
HOST = re.compile(r"(?m)^(\s*-?\s*host:\s*)localhost1\s*$")
MIN_FREE = re.compile(r"(?m)^(\s*min_free_space_percent:\s*)\d+\s*$")
# Sidecar's own driver: the instance is matched against the endpoints this driver knows, so it must name the node
# the same way as the instance's host
CONTACT = re.compile(r'(contact_points:\s*\n\s*-\s*"?)127\.0\.0\.1(:\d+"?)')


def edit_config(text: str, host: str = "127.0.0.1", min_free_space_percent: int | None = 1) -> str:
    """The shipped config, with the instance at host: the node's name, which is what Sidecar's ring reports and
    clients such as Cassandra Analytics call it by. min_free_space_percent: the SSTable upload floor (Cassandra
    Analytics' integration tests use 1; a container's disk may be fuller than the default 10% allows)."""
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError(f"unexpected host name {host!r}")
    new, n = HOST.subn(rf"\g<1>{host}", text, count=1)
    if n == 0:
        raise ValueError("sidecar.yaml has no 'host: localhost1' instance entry to point at the local node")
    new = CONTACT.sub(rf"\g<1>{host}\g<2>", new, count=1)
    if min_free_space_percent is not None:
        new = MIN_FREE.sub(rf"\g<1>{min_free_space_percent}", new)
    return new


def step(name: str, status: str, detail: str = "") -> None:
    print(f"CRV-STEP {name} {status} {detail}".rstrip(), flush=True)


def get(path: str, timeout: int = 20) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path), timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (OSError, ValueError) as e:
        return 0, str(e)


def until(path: str, seconds: int, ok=lambda status, body: status == 200) -> tuple[int, str, float]:
    """Polls path until ok(); returns the last answer and how long it took."""
    start = time.time()
    status, body = 0, ""
    while time.time() - start < seconds:
        status, body = get(path)
        if ok(status, body):
            break
        time.sleep(2)
    return status, body, time.time() - start


def short(body: str) -> str:
    return " ".join(body.split())[:160]


def check(host: str, sidecar_version: str, cassandra_version: str, seconds: int) -> None:
    global BASE
    BASE = f"http://{host}:9043"
    status, body, took = until("/api/v1/__health", seconds)
    if status != 200:
        step("starts", "FAIL", f"/api/v1/__health not OK within {seconds}s: {status} {short(body)}")
        print("CRV-DONE")
        return
    step("starts", "OK", f"/api/v1/__health OK after {took:.0f}s")
    # native and JMX health are refreshed by a periodic check (healthcheck.execute_interval, 30s by default)
    for path, name in (("/api/v1/cassandra/native/__health", "cql-health"),
                       ("/api/v1/cassandra/jmx/__health", "jmx-health")):
        status, body, took = until(path, 120)
        step(name, "OK" if status == 200 else "FAIL", f"{path}: {status} {short(body)}")

    status, body, _ = until("/api/v1/cassandra/settings", 120)
    if status == 200:
        settings = json.loads(body)
        release = settings.get("releaseVersion")
        reported = (settings.get("sidecar") or {}).get("version")
        step("node-settings", "OK" if release == cassandra_version else "FAIL",
             f"releaseVersion {release} (node runs {cassandra_version})")
        step("sidecar-version", "OK" if reported == sidecar_version else "FAIL",
             f"/api/v1/cassandra/settings reports Sidecar {reported}")
    else:
        step("node-settings", "FAIL", f"/api/v1/cassandra/settings: {status} {short(body)}")

    status, body, _ = until("/api/v2/cassandra/settings", 60)
    filled = status == 200 and bool((json.loads(body) or {}).get("nodeSettings"))
    step("cql-settings", "OK" if filled else "FAIL", f"/api/v2/cassandra/settings: {status} {short(body)[:80]}")

    status, body = get("/api/v1/cassandra/schema")
    has_system = status == 200 and "system" in (json.loads(body) or {}).get("schema", "")
    step("schema", "OK" if has_system else "FAIL", f"/api/v1/cassandra/schema: {status}")

    status, body = get("/api/v1/cassandra/ring")
    ring = json.loads(body) if status == 200 else []
    entry = ring[0] if isinstance(ring, list) and ring else {}
    step("ring", "OK" if entry else "FAIL",
         f"{len(ring) if isinstance(ring, list) else 0} node(s), status {entry.get('status')}, state {entry.get('state')}"
         if entry else f"/api/v1/cassandra/ring: {status} {short(body)}")
    print("CRV-DONE", flush=True)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "edit-config":
        with open(argv[1]) as f:
            text = f.read()
        try:
            new = edit_config(text, argv[2])
        except ValueError as e:
            step("config", "FAIL", str(e))
            return 1
        with open(argv[1], "w") as f:
            f.write(new)
        step("config", "OK", f"instance host set to {argv[2]}")
        return 0
    if len(argv) == 5 and argv[0] == "check":
        check(argv[1], argv[2], argv[3], int(argv[4]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Optional smoke tests: start the binary release and check it comes up with the right version."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from ..context import Context


def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Smoke test (binary tarball)")
    if not p.smoke:
        r.skip("Smoke test", f"{p.display} is a library; nothing to start")
        return
    if ctx.bin_root is None:
        r.skip("Smoke test", "binary tarball not available")
        return
    run_dir = ctx.workdir / "smoke" / ctx.bin_root.name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    shutil.copytree(ctx.bin_root, run_dir, symlinks=True)
    env = _java_env(ctx)
    if p.smoke == "cassandra":
        _cassandra(ctx, run_dir, env)
    elif p.smoke == "sidecar":
        _sidecar(ctx, run_dir, env)


def _java_env(ctx: Context) -> dict[str, str]:
    env = dict(os.environ)
    java_home = ctx.args.java_home or env.get("JAVA_HOME")
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = f"{java_home}/bin:{env.get('PATH', '')}"
    java = shutil.which("java", path=env.get("PATH"))
    if java:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, env=env)
        first = (out.stderr or out.stdout).splitlines()[0] if (out.stderr or out.stdout) else "?"
        ctx.report.info("Java used for the smoke test", first.strip() + (f" ({java_home})" if java_home else ""))
    return env


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    out = open(log, "w")
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=45)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


def _tail(log: Path, n: int = 15) -> list[str]:
    try:
        return log.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _wait(proc: subprocess.Popen, ready, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if ready():
            return True
        time.sleep(2)
    return False


def _cassandra(ctx: Context, home: Path, env: dict[str, str]) -> None:
    r = ctx.report
    busy = [port for port in (9042, 7000, 7199) if _port_open(port)]
    if busy:
        r.skip("Cassandra starts", f"ports {busy} already in use; stop the local Cassandra first")
        return
    env = {**env, "MAX_HEAP_SIZE": "1G", "HEAP_NEWSIZE": "256M", "CASSANDRA_HOME": str(home)}
    log = home / "smoke-stdout.log"
    r.note(f"Starting Cassandra from {home} (log: {log}) ...")
    proc = _start([str(home / "bin" / "cassandra"), "-f"], home, env, log)
    try:
        ok = _wait(proc, lambda: "Startup complete" in log.read_text(errors="replace") and _port_open(9042), ctx.args.smoke_timeout)
        if not ok:
            text = log.read_text(errors="replace")
            if "Unsupported Java" in text or "not supported" in text.lower():
                r.skip("Cassandra starts", "this Java version is not supported by this release; use --java-home")
            else:
                r.fail("Cassandra starts", f"did not reach 'Startup complete' within {ctx.args.smoke_timeout}s", _tail(log))
            return
        r.ok("Cassandra starts", "'Startup complete' and CQL port 9042 open")
        nodetool = [str(home / "bin" / "nodetool"), "-h", "127.0.0.1"]
        out = subprocess.run(nodetool + ["version"], capture_output=True, text=True, env=env, timeout=120)
        version = next((line.split(":", 1)[1].strip() for line in out.stdout.splitlines() if line.startswith("ReleaseVersion")), None)
        (r.ok if version == ctx.version else r.fail)("nodetool version", f"ReleaseVersion: {version}")
        out = subprocess.run(nodetool + ["status"], capture_output=True, text=True, env=env, timeout=120)
        up = any(line.startswith("UN ") for line in out.stdout.splitlines())
        (r.ok if up else r.fail)("nodetool status", "node is Up/Normal" if up else out.stdout.strip()[-300:])
        if version == ctx.version and up:
            ctx.highlight(f"Smoke test: binary tarball starts, `nodetool version` reports {version}, node is UN")
    finally:
        _stop(proc)


def _sidecar(ctx: Context, home: Path, env: dict[str, str]) -> None:
    r = ctx.report
    port = 9043
    if _port_open(port):
        r.skip("Sidecar starts", f"port {port} already in use")
        return
    log = home / "smoke-stdout.log"
    r.note(f"Starting Sidecar from {home} (log: {log}) ...")
    proc = _start([str(home / "bin" / "cassandra-sidecar")], home, env, log)
    try:
        if not _wait(proc, lambda: _port_open(port), ctx.args.smoke_timeout):
            r.fail("Sidecar starts", f"port {port} not open within {ctx.args.smoke_timeout}s", _tail(log))
            return
        r.ok("Sidecar starts", f"listening on port {port}")
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/__health")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode(errors="replace")
            ok = resp.status == 200 and "OK" in body
            (r.ok if ok else r.fail)("Sidecar health endpoint", f"HTTP {resp.status} {body.strip()[:100]}")
            if ok:
                ctx.highlight("Smoke test: Sidecar binary tarball starts and /api/v1/__health returns OK")
        except Exception as e:  # noqa: BLE001
            r.fail("Sidecar health endpoint", str(e))
    finally:
        _stop(proc)

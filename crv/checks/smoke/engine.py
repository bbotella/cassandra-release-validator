"""Containers for the smoke tests, and the protocol the scripts inside them use to report.

Everything a smoke test runs from a release runs in a throwaway container named crv-smoke-<id>, which is
always removed. A script reports through lines on its output:

    CRV-STEP <name> OK|FAIL|WARN|INFO [detail]   one check of the artifact
    CRV-SETUP <name> FAIL [detail]               the environment failed (image, apt mirror, network), not the artifact
    CRV-DONE                                     the script reached its end

A script that stops without CRV-DONE (a crash, a timeout) is a failure; a SETUP failure is not, because it says
nothing about the release. PRELUDE gives the scripts small helpers that print these lines.
"""

from __future__ import annotations

import json
import platform as _platform
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ...report import Report

PRELUDE = r"""
set -o pipefail
crv_step() { echo "CRV-STEP $1 $2 ${3:-}"; }
crv_setup() { echo "CRV-SETUP $1 FAIL ${2:-}"; exit 0; }
crv_done() { echo CRV-DONE; }
crv_last() { printf '%s' "$1" | tail -n 1 | cut -c1-240; }
# crv_run <step> <command...>: the output goes to the log, the result to the report (last line as the detail)
crv_run() {
  local name=$1; shift
  local out rc
  out=$("$@" 2>&1); rc=$?
  [ -n "$out" ] && printf '%s\n' "$out"
  if [ $rc -eq 0 ]; then crv_step "$name" OK; else crv_step "$name" FAIL "exit $rc: $(crv_last "$out")"; fi
  return $rc
}
# crv_retry <tries> <step> <command...>: crv_run, retried a few times (downloads from Maven Central or PyPI
# can break off half way)
crv_retry() {
  local tries=$1 name=$2; shift 2
  local out rc i=1
  while :; do
    out=$("$@" 2>&1); rc=$?
    { [ $rc -eq 0 ] || [ $i -ge "$tries" ]; } && break
    echo "--- attempt $i of $name failed (exit $rc), retrying:"; printf '%s\n' "$out" | tail -n 5
    i=$((i + 1)); sleep 5
  done
  [ -n "$out" ] && printf '%s\n' "$out"
  if [ $rc -eq 0 ]; then crv_step "$name" OK; else crv_step "$name" FAIL "exit $rc: $(crv_last "$out")"; fi
  return $rc
}
# crv_prepare <name> <command...>: like crv_run, but a failure is the environment's and ends the script.
# It has its own time limit (a hung mirror must not look like a release problem).
crv_prepare() {
  local name=$1; shift
  local out rc limit=${CRV_SETUP_TIMEOUT:-600}
  if command -v timeout > /dev/null; then out=$(timeout "$limit" "$@" 2>&1); else out=$("$@" 2>&1); fi
  rc=$?
  [ -n "$out" ] && printf '%s\n' "$out" | tail -n 60
  [ $rc -eq 124 ] && crv_setup "$name" "timed out after ${limit}s"
  [ $rc -eq 0 ] || crv_setup "$name" "exit $rc: $(crv_last "$out")"
}
# crv_wait_log <step> <seconds> <file> <text>: waits for text in a log file the release writes
crv_wait_log() {
  local name=$1 secs=$2 file=$3 text=$4 i=0
  while [ $i -lt "$secs" ]; do
    if grep -qF "$text" "$file" 2>/dev/null; then crv_step "$name" OK "after ${i}s"; return 0; fi
    sleep 2; i=$((i + 2))
  done
  echo "--- last lines of $file"; tail -n 40 "$file" 2>/dev/null
  crv_step "$name" FAIL "no '$text' in $file within ${secs}s"
  return 1
}
"""

LINE = re.compile(r"^CRV-(STEP|SETUP) (\S+) (OK|FAIL|WARN|INFO)\b ?(.*)$")
DONE = "CRV-DONE"


class SetupError(Exception):
    """The container engine could not do what was asked (pull, start, copy): an environment problem."""


@dataclass
class Step:
    kind: str  # STEP or SETUP
    name: str
    status: str  # OK, FAIL, WARN or INFO
    detail: str = ""


@dataclass
class Outcome:
    log: Path
    steps: list[Step] = field(default_factory=list)
    done: bool = False
    exit_code: int | None = None
    timed_out: bool = False
    timeout: int = 0

    @property
    def setup_failure(self) -> Step | None:
        return next((s for s in self.steps if s.kind == "SETUP" and s.status == "FAIL"), None)

    @property
    def ok(self) -> bool:
        """The script ran to its end and every check passed."""
        return self.done and self.setup_failure is None and not any(s.status == "FAIL" for s in self.steps)


def parse(text: str) -> tuple[list[Step], bool]:
    steps, done = [], False
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line == DONE:
            done = True
            continue
        m = LINE.match(line)
        if m:
            steps.append(Step(m.group(1), m.group(2), m.group(3), m.group(4).strip()))
    return steps, done


def tail(log: Path, n: int = 15) -> list[str]:
    """The last output lines of the last script in log (not image pulls or the script itself)."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return []
    output = text.rsplit("\n----\n", 1)[-1]
    return [line for line in output.splitlines() if line.strip() and not line.startswith("CRV-")][-n:]


def report(r: Report, label: str, out: Outcome, as_warning: bool = False) -> bool:
    """Puts an outcome in the report. Returns whether it ran to its end with no failure.

    as_warning: an artifact failure is a WARN (used for compatibility with another project's release).
    """
    bad = r.warn if as_warning else r.fail
    if out.ok and not r.verbose:  # one line when everything passed; -v shows each step
        passed = [_pretty(s.name) for s in out.steps if s.kind == "STEP" and s.status == "OK"]
        infos = [s.detail for s in out.steps if s.status == "INFO" and s.detail]
        r.ok(label, f"{len(passed)} checks passed: {', '.join(passed)}" + (f" ({'; '.join(infos)})" if infos else ""))
        for s in out.steps:
            if s.status == "WARN":
                r.warn(f"{label}: {_pretty(s.name)}", s.detail)
        return True
    shown_tail = False
    for s in out.steps:
        name = f"{label}: {_pretty(s.name)}"
        if s.kind == "SETUP":
            if s.status == "FAIL":
                r.warn(f"{label}: not run", f"{s.name} failed, an environment problem rather than the release: "
                                            f"{s.detail} (log: {out.log})")
            continue
        if s.status == "OK":
            r.ok(name, s.detail)
        elif s.status == "FAIL":
            bad(name, f"{s.detail} (log: {out.log})".strip(), [] if shown_tail else tail(out.log))
            shown_tail = True
        elif s.status == "WARN":
            r.warn(name, s.detail)
        else:
            r.info(name, s.detail)
    if not out.done and out.setup_failure is None:
        why = f"timed out after {out.timeout}s" if out.timed_out else f"stopped before its end (exit code {out.exit_code})"
        bad(label, f"{why}; log: {out.log}", tail(out.log))
    return out.ok


def native_platform() -> str:
    """The container platform matching this machine (the docker/podman VM runs the host's architecture)."""
    return "linux/arm64" if _platform.machine().lower() in ("arm64", "aarch64") else "linux/amd64"


def _pretty(name: str) -> str:
    return name if ":" in name else name.replace("-", " ")


def _new_name() -> str:
    return f"crv-smoke-{uuid.uuid4().hex[:12]}"


class Engine:
    """docker or podman, driven through its CLI."""

    def __init__(self, path: str):
        self.path = path
        self.native = native_platform()

    @property
    def name(self) -> str:
        return Path(self.path).name

    def call(self, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.path, *args], capture_output=True, text=True, errors="replace",
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(e.cmd, 124, e.stdout or "", f"timed out after {timeout}s")

    def has_image(self, image: str) -> bool:
        return self.call(["image", "inspect", image], timeout=60).returncode == 0

    def pull(self, image: str, log: Path, platform: str | None = None) -> bool:
        """Makes sure the image is available for the platform (default: this machine's), pulled once, then cached.

        One tag holds one platform locally, so a test that ran an amd64 variant must not leave the next test
        emulating it: the architecture is checked, not only the tag."""
        platform = platform or self.native
        arch = self.call(["image", "inspect", "--format", "{{.Architecture}}", image], timeout=60)
        if arch.returncode == 0 and arch.stdout.strip() == platform.split("/", 1)[1]:
            return True
        args = ["pull", "--platform", platform, image]
        proc = self.call(args, timeout=3600)
        _append(log, f"$ {self.name} {shlex.join(args)}\n{proc.stdout}{proc.stderr}\n")
        return proc.returncode == 0

    def image_env(self, image: str) -> dict[str, str]:
        proc = self.call(["image", "inspect", "--format", "{{json .Config.Env}}", image], timeout=60)
        try:
            pairs = json.loads(proc.stdout.strip() or "[]") or []
        except json.JSONDecodeError:
            return {}
        return dict(p.split("=", 1) for p in pairs if "=" in p)

    def _run_args(self, mounts: dict[Path, str] | None, env: dict[str, str] | None, network: str | None,
                  platform: str | None, user: str | None) -> list[str]:
        args: list[str] = []
        for host, target in (mounts or {}).items():
            args += ["-v", f"{host}:{target}"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        if network:
            args += ["--network", network]
        args += ["--platform", platform or self.native]
        if user:
            args += ["--user", user]
        return args

    def run(self, image: str, script: str, *, log: Path, timeout: int, mounts: dict[Path, str] | None = None,
            env: dict[str, str] | None = None, network: str | None = None, platform: str | None = None,
            user: str | None = None) -> Outcome:
        """Runs a bash script in a fresh container that is removed afterwards."""
        if not self.pull(image, log, platform):
            return _setup_outcome(log, "image", f"could not pull {image}")
        name = _new_name()
        cmd = [self.path, "run", "--rm", "--name", name, *self._run_args(mounts, env, network, platform, user),
               "--entrypoint", "/bin/bash", image, "-c", PRELUDE + script]
        return _stream(cmd, log, timeout, script, on_timeout=lambda: self.call(["rm", "-f", name], timeout=120))

    def start(self, image: str, *, log: Path, command: list[str], entrypoint: str | None = None,
              mounts: dict[Path, str] | None = None, env: dict[str, str] | None = None,
              network: str | None = None, platform: str | None = None) -> Container:
        """Starts a long-running container; the caller removes it (Container is a context manager)."""
        if not self.pull(image, log, platform):
            raise SetupError(f"could not pull {image}")
        name = _new_name()
        args = ["run", "-d", "--name", name, *self._run_args(mounts, env, network, platform, None)]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args += [image, *command]
        proc = self.call(args, timeout=600)
        _append(log, f"$ {self.name} {shlex.join(args)}\n{proc.stdout}{proc.stderr}\n")
        if proc.returncode != 0:
            self.call(["rm", "-f", name], timeout=120)
            raise SetupError(f"could not start {image}: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return Container(self, name, image)


class Container:
    def __init__(self, engine: Engine, name: str, image: str):
        self.engine = engine
        self.name = name
        self.image = image

    @property
    def network(self) -> str:
        """What another container passes as --network to share this one's network namespace."""
        return f"container:{self.name}"

    def exec(self, script: str, *, log: Path, timeout: int, user: str | None = None,
             env: dict[str, str] | None = None) -> Outcome:
        args = ["exec"] + (["--user", user] if user else [])
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        cmd = [self.engine.path, *args, self.name, "/bin/bash", "-c", PRELUDE + script]
        return _stream(cmd, log, timeout, script)

    def call(self, args: list[str], timeout: int = 120, user: str | None = None,
             detach: bool = False) -> subprocess.CompletedProcess:
        opts = (["--user", user] if user else []) + (["-d"] if detach else [])
        return self.engine.call(["exec", *opts, self.name, *args], timeout=timeout)

    def copy_in(self, src: Path, dest: str) -> None:
        proc = self.engine.call(["cp", str(src), f"{self.name}:{dest}"], timeout=600)
        if proc.returncode != 0:
            raise SetupError(f"could not copy {src.name} into the container: {(proc.stderr or proc.stdout).strip()[-300:]}")

    def logs(self) -> str:
        proc = self.engine.call(["logs", self.name], timeout=120)
        return proc.stdout + proc.stderr

    def remove(self) -> None:
        self.engine.call(["rm", "-f", self.name], timeout=120)

    def __enter__(self) -> Container:
        return self

    def __exit__(self, *exc) -> None:
        self.remove()


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        out.write(text)


def _setup_outcome(log: Path, name: str, detail: str) -> Outcome:
    return Outcome(log=log, steps=[Step("SETUP", name, "FAIL", detail)])


def _stream(cmd: list[str], log: Path, timeout: int, script: str, on_timeout=None) -> Outcome:
    """Runs cmd with its output appended to log, then reads the protocol lines it printed."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        shown = cmd[:-1] if script and cmd and cmd[-1].endswith(script) else cmd
        out.write(f"$ {shlex.join(shown)} <script>\n{script.strip()}\n----\n")
        out.flush()
        start = out.tell()
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if on_timeout:
                on_timeout()
            proc.kill()
            proc.wait()
    with open(log, errors="replace") as f:
        f.seek(start)
        steps, done = parse(f.read())
    return Outcome(log=log, steps=steps, done=done, exit_code=None if timed_out else proc.returncode,
                   timed_out=timed_out, timeout=timeout)


def fill(template: str, **values: str) -> str:
    """Replaces @NAME@ placeholders (braces and $ stay free for bash)."""
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    left = re.findall(r"@[A-Z_]+@", template)
    if left:
        raise ValueError(f"unfilled placeholders {sorted(set(left))}")
    return template

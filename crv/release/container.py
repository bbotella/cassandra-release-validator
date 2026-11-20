"""The release container: runs build, packaging and signing commands on Debian (release/Containerfile)."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from ..checks.runtime import find_engine
from .signing import AgentRelay, agent_extra_socket, export_public_key, launch_agent

CONTAINERFILE = Path(__file__).resolve().parents[2] / "release" / "Containerfile"

# Runs before every command: with a relay port, bridge the container's agent socket to the host's agent
# and import the release manager's public key, then run the command.
PRELUDE = r"""
set -e
# UTF-8 like a developer machine: with Debian's default POSIX locale Java reads sources as US-ASCII
# (javadoc then fails on any non-ASCII character)
export LANG=C.UTF-8 LC_ALL=C.UTF-8
if [ -n "$CRV_RELAY_PORT" ]; then
  socat UNIX-LISTEN:/root/.gnupg/S.gpg-agent,fork TCP:host.containers.internal:$CRV_RELAY_PORT &
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -S /root/.gnupg/S.gpg-agent ] && break; sleep 0.3; done
  gpg --batch --quiet --import /crv/signing-pub.asc 2>/dev/null
  echo "trusted-key ${CRV_SIGNING_KEY: -16}" >> /root/.gnupg/gpg.conf
fi
if [ -n "$CRV_JAVA_HOME" ]; then export JAVA_HOME="$CRV_JAVA_HOME" PATH="$CRV_JAVA_HOME/bin:$PATH"; fi
exec "$@"
"""


class ReleaseContainer:
    def __init__(self, root: Path, *, mounts: dict[Path, str] | None = None,
                 key: Callable[[], tuple[str | None, str | None]] = lambda: (None, None), engine: str | None = None):
        self.root = root  # the release directory, mounted at the same path inside
        self.mounts = mounts or {}
        self.key = key  # -> (fingerprint, GnuPG home); home None: your normal keyring, else a rehearsal keyring
        self.signing_key: str | None = None
        self._engine = engine
        self._relay: AgentRelay | None = None
        self._image: str | None = None

    # -------------------------------------------------------------- setup
    @property
    def engine(self) -> str:
        if not self._engine:
            engine, why = find_engine(None)
            if not engine:
                raise RuntimeError(f"no container engine: {why} (start Docker, or `podman machine start`)")
            self._engine = engine
        return self._engine

    @property
    def image(self) -> str:
        if self._image is None:
            digest = hashlib.sha256(CONTAINERFILE.read_bytes()).hexdigest()[:12]
            tag = f"localhost/crv-release:{digest}"
            have = subprocess.run([self.engine, "image", "exists", tag], capture_output=True).returncode == 0 \
                if self.engine.endswith("podman") else \
                subprocess.run([self.engine, "image", "inspect", tag], capture_output=True).returncode == 0
            if not have:
                print(f"   building the release container image {tag} (first use; a few minutes) ...")
                subprocess.run([self.engine, "build", "-t", tag, "-f", str(CONTAINERFILE), str(CONTAINERFILE.parent)],
                               check=True)
            self._image = tag
        return self._image

    def _relay_port(self) -> int:
        if self._relay is None:
            self.signing_key, home = self.key()
            if not self.signing_key:
                raise RuntimeError("a signing step needs your release key: --signing-key <fingerprint> (or $gpg_key)")
            launch_agent(home)
            export_public_key(self.signing_key, self.root / "signing" / "signing-pub.asc", home)
            self._relay = AgentRelay(agent_extra_socket(home))
            self._relay.start()
        return self._relay.port

    def close(self) -> None:
        if self._relay:
            self._relay.stop()
            self._relay = None

    # ------------------------------------------------------------- running
    def wrap(self, cmd: list[str], cwd: Path | None, env: dict[str, str], signing: bool) -> list[str]:
        full = [self.engine, "run", "--rm", "-i", "-v", f"{self.root}:{self.root}"]
        for host, target in self.mounts.items():
            full += ["-v", f"{host}:{target}"]
        if cwd:
            full += ["-w", str(cwd)]
        for key, value in env.items():
            full += ["-e", f"{key}={value}"]
        if signing:
            full += ["-e", f"CRV_RELAY_PORT={self._relay_port()}", "-e", f"CRV_SIGNING_KEY={self.signing_key}",
                     "-v", f"{self.root / 'signing' / 'signing-pub.asc'}:/crv/signing-pub.asc:ro"]
        return full + [self.image, "bash", "-c", PRELUDE, "crv", *cmd]

    def describe(self, cmd: list[str]) -> str:
        return shlex.join(cmd)

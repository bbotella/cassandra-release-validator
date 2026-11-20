"""Lets the release container sign with the release manager's own gpg-agent (see docs/release-signing.md).

A relay on 127.0.0.1:<random port> forwards to the agent's restricted extra socket. Inside the container,
socat turns ~/.gnupg/S.gpg-agent into a connection to host.containers.internal:<port>. The secret key
never leaves this machine. The relay only runs while a signing step does.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path


def agent_extra_socket(gnupghome: str | None = None) -> str:
    env = {"GNUPGHOME": gnupghome} if gnupghome else None
    out = subprocess.run(["gpgconf", "--list-dirs", "agent-extra-socket"], capture_output=True, text=True,
                         env={**os.environ, **(env or {})}, check=True)
    return out.stdout.strip()


def launch_agent(gnupghome: str | None = None) -> None:
    env = {**os.environ, **({"GNUPGHOME": gnupghome} if gnupghome else {})}
    subprocess.run(["gpgconf", "--launch", "gpg-agent"], env=env, check=True)


class AgentRelay:
    """TCP 127.0.0.1:<port> <-> the agent's extra socket, in a background thread."""

    def __init__(self, unix_socket: str):
        self.unix_socket = unix_socket
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the signing relay did not start")
        return self.port

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()

        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        async def handle(tcp_reader, tcp_writer):
            unix_reader, unix_writer = await asyncio.open_unix_connection(self.unix_socket)
            await asyncio.gather(pipe(tcp_reader, unix_writer), pipe(unix_reader, tcp_writer), return_exceptions=True)

        async def main():
            await asyncio.start_server(handle, "127.0.0.1", self.port)
            self._ready.set()

        self._loop.run_until_complete(main())
        self._loop.run_forever()


def export_public_key(fingerprint: str, dest: Path, gnupghome: str | None = None) -> Path:
    env = {**os.environ, **({"GNUPGHOME": gnupghome} if gnupghome else {})}
    out = subprocess.run(["gpg", "--batch", "--armor", "--export", fingerprint], capture_output=True, env=env, check=True)
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in out.stdout:
        raise RuntimeError(f"no public key {fingerprint} in the keyring")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out.stdout)
    return dest


def throwaway_key(name: str) -> tuple[str, str]:
    """A passphrase-less RSA 4096 key in a fresh GnuPG home, for rehearsals only. Returns (home, fingerprint).

    The home is in the system temp directory because gpg-agent's socket paths must stay short.
    """
    home = tempfile.mkdtemp(prefix="crv-rehearsal-gpg-")
    Path(home).chmod(0o700)
    env = {**os.environ, "GNUPGHOME": home}
    subprocess.run(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", "",
                    "--quick-gen-key", f"{name} (rehearsal only, NOT for releases) <rehearsal@invalid>",
                    "rsa4096", "sign", "30d"], env=env, check=True, capture_output=True)
    out = subprocess.run(["gpg", "--with-colons", "--list-secret-keys"], env=env, capture_output=True, text=True, check=True)
    fpr = next(line.split(":")[9] for line in out.stdout.splitlines() if line.startswith("fpr:"))
    return home, fpr


def remove_home(home: str) -> None:
    env = {**os.environ, "GNUPGHOME": home}
    subprocess.run(["gpgconf", "--kill", "all"], env=env, capture_output=True)
    if "crv-rehearsal-gpg-" in home:
        shutil.rmtree(home, ignore_errors=True)

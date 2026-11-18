"""Signature verification against the project's KEYS file, using an isolated, throwaway GnuPG home.

Your personal keyring is never used or modified: only keys from the official KEYS file are trusted.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

KEYS_URL = "https://dist.apache.org/repos/dist/release/cassandra/KEYS"

# GnuPG public key algorithm ids (RFC 4880 / 9580)
_ALGOS = {"1": "RSA", "2": "RSA", "3": "RSA", "16": "ElGamal", "17": "DSA", "18": "ECDH", "19": "ECDSA", "22": "EdDSA"}


@dataclass
class SigResult:
    ok: bool
    fingerprint: str = ""  # primary key fingerprint
    signing_fingerprint: str = ""  # (sub)key that made the signature
    uid: str = ""
    error: str = ""
    expired_key: bool = False

    @property
    def signer(self) -> str:
        return f"{self.uid} [{self.fingerprint[-16:]}]" if self.fingerprint else self.uid


@dataclass
class KeyInfo:
    fingerprint: str
    algorithm: str
    bits: int
    uids: list[str]
    expires: str


class Keyring:
    def __init__(self, keys_file: Path):
        self.gpg = shutil.which("gpg") or shutil.which("gpg2")
        if not self.gpg:
            raise RuntimeError("gpg is required (install GnuPG)")
        # Keep the path short: gpg places sockets in the home directory and they have a ~100 char limit.
        self.home = Path(tempfile.mkdtemp(prefix="crv-gpg-"))
        self.home.chmod(0o700)
        # An existing (empty) common.conf stops gpg >= 2.4.1 from enabling keyboxd for a fresh home dir,
        # so no background daemons are needed just to verify signatures.
        (self.home / "common.conf").write_text("")
        (self.home / "gpg.conf").write_text("no-autostart\ntrust-model always\n")
        out = self._run(["--import", str(keys_file)])
        self.imported = sum(1 for line in out.stdout.splitlines() if line.startswith("[GNUPG:] IMPORT_OK"))

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [self.gpg, "--homedir", str(self.home), "--batch", "--no-tty", "--status-fd", "1", *args]
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=300)
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def verify(self, signature: Path, data: Path | None = None) -> SigResult:
        """Verifies a detached signature (signature + data) or an inline/clear-signed file (signature only)."""
        args = ["--verify", str(signature)] + ([str(data)] if data is not None else [])
        proc = self._run(args)
        return _parse_status(proc.stdout, proc.stderr)

    def key_info(self, fingerprint: str) -> KeyInfo | None:
        proc = self._run(["--with-colons", "--fixed-list-mode", "--list-keys", fingerprint])
        info: KeyInfo | None = None
        for line in proc.stdout.splitlines():
            f = line.split(":")
            if f[0] == "pub":
                info = KeyInfo(fingerprint, _ALGOS.get(f[3], f[3]), int(f[2] or 0), [], f[6])
            elif f[0] == "uid" and info is not None and f[1] not in ("r", "e"):
                info.uids.append(f[9])
            elif f[0] == "fpr" and info is not None and not info.fingerprint:
                info.fingerprint = f[9]
        return info

    def signing_key_bits(self, signing_fpr: str) -> tuple[str, int] | None:
        """Algorithm and size of the exact (sub)key that produced a signature."""
        proc = self._run(["--with-colons", "--fixed-list-mode", "--list-keys", signing_fpr])
        last = None
        for line in proc.stdout.splitlines():
            f = line.split(":")
            if f[0] in ("pub", "sub"):
                last = (_ALGOS.get(f[3], f[3]), int(f[2] or 0))
            elif f[0] == "fpr" and f[9] == signing_fpr and last:
                return last
        return None

    def close(self) -> None:
        gpgconf = shutil.which("gpgconf")
        if gpgconf:
            subprocess.run([gpgconf, "--homedir", str(self.home), "--kill", "all"], capture_output=True, check=False)
        shutil.rmtree(self.home, ignore_errors=True)


def _parse_status(status: str, stderr: str) -> SigResult:
    res = SigResult(ok=False)
    good = valid = False
    for line in status.splitlines():
        if not line.startswith("[GNUPG:] "):
            continue
        parts = line[9:].split(" ")
        kw = parts[0]
        if kw == "GOODSIG":
            good = True
            res.uid = " ".join(parts[2:])
        elif kw == "EXPKEYSIG":
            good = True
            res.expired_key = True
            res.uid = " ".join(parts[2:])
        elif kw == "VALIDSIG":
            valid = True
            res.signing_fingerprint = parts[1]
            res.fingerprint = parts[-1] if len(parts) >= 11 else parts[1]
        elif kw == "BADSIG":
            res.error = "BAD signature (file was modified or signature does not match)"
        elif kw == "REVKEYSIG":
            res.error = "signature made by a REVOKED key"
        elif kw == "NO_PUBKEY":
            res.error = f"signing key {parts[1]} is not in KEYS"
        elif kw == "ERRSIG" and not res.error:
            res.error = f"cannot check signature (key {parts[1]})"
        elif kw == "NODATA" and not res.error:
            res.error = "no valid OpenPGP signature found"
    res.ok = good and valid and not res.error
    if not res.ok and not res.error:
        tail = [l for l in stderr.strip().splitlines() if l][-1:] or ["unknown gpg error"]
        res.error = tail[0]
    return res

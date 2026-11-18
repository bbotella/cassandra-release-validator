"""Checksum file parsing and digest computation."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ALGOS = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
CHECKSUM_EXTS = tuple(f".{a}" for a in ALGOS)

_BSD = re.compile(r"^(?:MD5|SHA1|SHA256|SHA512)\s*\((?P<name>.+)\)\s*=\s*(?P<hash>[0-9a-fA-F]+)\s*$")


class ChecksumFormatError(ValueError):
    pass


def parse(text: str, algo: str, expected_name: str | None = None) -> str:
    """Extracts the hex digest from a checksum file.

    Supports the formats used across the projects over the years:
      * a bare hash                                  (Cassandra: ``<hash>``)
      * coreutils style                              (Sidecar/Analytics: ``<hash>  <file>``)
      * BSD style                                    (``SHA512 (<file>) = <hash>``)
      * ``gpg --print-md`` style, grouped/upper-case (``<file>: ABCD 1234 ...``)

    Raises ChecksumFormatError when the content is not a digest of the expected length, or when it
    names a different file than expected.
    """
    size = ALGOS[algo]
    text = text.strip()
    if not text:
        raise ChecksumFormatError("checksum file is empty")
    first = text.splitlines()[0].strip()

    m = _BSD.match(first)
    if m:
        digest, name = m.group("hash"), m.group("name")
    elif text.split()[0].endswith(":"):
        # gpg --print-md: "file: AB CD ..." possibly wrapped over several lines
        name, _, rest = text.partition(":")
        digest = re.sub(r"\s+", "", rest)
        name = name.strip()
    else:
        tokens = first.split()
        digest = tokens[0]
        name = tokens[1].lstrip("*") if len(tokens) > 1 else None

    digest = digest.lower()
    if len(digest) != size or not re.fullmatch(r"[0-9a-f]+", digest):
        raise ChecksumFormatError(f"not a valid {algo} digest: {digest[:20]}...")
    if name and expected_name and Path(name).name != expected_name:
        raise ChecksumFormatError(f"checksum file refers to '{name}', expected '{expected_name}'")
    return digest


def digests(path: Path, algos: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Computes several digests of a file in a single pass."""
    hashers = {a: hashlib.new(a) for a in algos}
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            for h in hashers.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hashers.items()}


def git_blob_sha1(data: bytes) -> str:
    """The object id git assigns to a blob with this content."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()

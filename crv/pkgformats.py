"""Minimal, dependency-free readers for .deb (ar + control.tar), .rpm headers and Debian control files."""

from __future__ import annotations

import gzip
import io
import lzma
import struct
import tarfile
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- deb822 files
def parse_deb822(text: str) -> list[dict[str, str]]:
    """Parses Debian control-style stanzas (Packages, Sources, Release, .changes, .dsc)."""
    text = strip_clearsign(text)
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
                current, key = {}, None
            continue
        if line[0] in " \t" and key:
            current[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def strip_clearsign(text: str) -> str:
    if "-----BEGIN PGP SIGNED MESSAGE-----" not in text:
        return text
    body = text.split("-----BEGIN PGP SIGNED MESSAGE-----", 1)[1]
    # skip armor headers (Hash: ...) up to the first blank line
    body = body.split("\n\n", 1)[1] if "\n\n" in body else body
    body = body.split("-----BEGIN PGP SIGNATURE-----", 1)[0]
    return "\n".join(line[2:] if line.startswith("- ") else line for line in body.splitlines())


def checksum_lines(value: str) -> list[tuple[str, int, str]]:
    """Parses multi-line '<hash> <size> <name>' fields (Checksums-Sha256, SHA256, Files)."""
    out = []
    for line in value.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].isdigit():
            out.append((parts[0].lower(), int(parts[1]), parts[-1]))
    return out


# ------------------------------------------------------------------------- deb
def read_deb_control(path: Path) -> dict[str, str]:
    """Returns the fields of the control file inside a .deb package."""
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ValueError("not a Debian package (bad ar magic)")
        while True:
            header = f.read(60)
            if len(header) < 60:
                break
            name = header[:16].decode("ascii", errors="replace").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
            if name.startswith("control.tar"):
                data = f.read(size)
                raw = _decompress(name, data)
                with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                    for member in tar.getmembers():
                        if member.name.lstrip("./") == "control":
                            extracted = tar.extractfile(member)
                            if extracted:
                                stanzas = parse_deb822(extracted.read().decode("utf-8", errors="replace"))
                                return stanzas[0] if stanzas else {}
                raise ValueError("control.tar has no control file")
            f.seek(size + (size % 2), io.SEEK_CUR)
    raise ValueError("no control.tar member found")


def _decompress(name: str, data: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
        except ImportError as e:  # pragma: no cover
            raise ValueError("control.tar.zst needs Python 3.14+ to read") from e
        return zstd.decompress(data)
    return data


# ------------------------------------------------------------------------- rpm
RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
RPM_HEADER_MAGIC = b"\x8e\xad\xe8\x01"

SIGTAG_DSA = 267  # header-only, DSA
SIGTAG_RSA = 268  # header-only, RSA / EdDSA (OpenPGP packet)
SIGTAG_PGP = 1002  # header + payload, RSA
SIGTAG_GPG = 1005  # header + payload, DSA

TAG_NAME, TAG_VERSION, TAG_RELEASE, TAG_ARCH = 1000, 1001, 1002, 1022
TAG_SOURCERPM = 1044


@dataclass
class RpmHeader:
    entries: dict[int, tuple[int, int, int]]  # tag -> (type, offset, count)
    store: bytes

    def string(self, tag: int) -> str | None:
        e = self.entries.get(tag)
        if not e:
            return None
        typ, off, _ = e
        if typ not in (6, 8, 9):  # STRING, STRING_ARRAY, I18NSTRING
            return None
        end = self.store.index(b"\0", off)
        return self.store[off:end].decode("utf-8", errors="replace")

    def binary(self, tag: int) -> bytes | None:
        e = self.entries.get(tag)
        if not e or e[0] != 7:  # BIN
            return None
        _, off, count = e
        return self.store[off:off + count]


@dataclass
class Rpm:
    name: str
    version: str
    release: str
    arch: str
    is_source: bool
    signatures: dict[int, bytes] = field(default_factory=dict)  # sigtag -> OpenPGP signature packet
    header_range: tuple[int, int] = (0, 0)  # byte range of the main header in the file

    @property
    def nvr(self) -> str:
        return f"{self.name}-{self.version}-{self.release}"


def _read_header(f) -> tuple[RpmHeader, int]:
    magic = f.read(8)
    if magic[:4] != RPM_HEADER_MAGIC:
        raise ValueError("bad rpm header magic")
    nindex, hsize = struct.unpack(">II", f.read(8))
    entries = {}
    for _ in range(nindex):
        tag, typ, off, count = struct.unpack(">IIII", f.read(16))
        entries[tag] = (typ, off, count)
    store = f.read(hsize)
    return RpmHeader(entries, store), 16 + 16 * nindex + hsize


def read_rpm(path: Path) -> Rpm:
    with open(path, "rb") as f:
        lead = f.read(96)
        if lead[:4] != RPM_LEAD_MAGIC:
            raise ValueError("not an rpm (bad lead magic)")
        is_source = struct.unpack(">H", lead[6:8])[0] == 1
        sig, sig_len = _read_header(f)
        f.read((8 - sig_len % 8) % 8)  # signature header is padded to 8 bytes
        start = f.tell()
        hdr, hdr_len = _read_header(f)
    rpm = Rpm(
        name=hdr.string(TAG_NAME) or "",
        version=hdr.string(TAG_VERSION) or "",
        release=hdr.string(TAG_RELEASE) or "",
        arch=hdr.string(TAG_ARCH) or "",
        is_source=is_source or hdr.string(TAG_SOURCERPM) is None,
        header_range=(start, start + hdr_len),
    )
    for tag in (SIGTAG_RSA, SIGTAG_DSA, SIGTAG_PGP, SIGTAG_GPG):
        blob = sig.binary(tag)
        if blob:
            rpm.signatures[tag] = blob
    return rpm


def write_rpm_signed_data(path: Path, rpm: Rpm, tag: int, dest: Path) -> None:
    """Writes the bytes a given rpm signature tag covers (main header, or header + payload) to dest."""
    import shutil

    with open(path, "rb") as f, open(dest, "wb") as out:
        f.seek(rpm.header_range[0])
        if tag in (SIGTAG_RSA, SIGTAG_DSA):
            out.write(f.read(rpm.header_range[1] - rpm.header_range[0]))
        else:
            shutil.copyfileobj(f, out, length=1 << 20)

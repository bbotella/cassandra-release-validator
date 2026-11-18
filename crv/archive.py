"""Safe extraction of (untrusted) release tarballs."""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path

_MARKER = ".crv-extracted"


def extract(tarball: Path, dest: Path) -> Path:
    """Extracts tarball into dest (once; reused on later runs) and returns its single top-level directory.

    Uses tarfile's 'data' filter, which rejects absolute paths, '..' traversal, device files and links
    pointing outside the destination.
    """
    marker = dest / _MARKER
    if not marker.exists():
        if dest.exists():
            shutil.rmtree(dest)
        # Extract next to dest and rename at the end, so a concurrent run never sees half an extraction.
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        with tarfile.open(tarball) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:  # pragma: no cover - Python without PEP 706 backport
                for m in tar.getmembers():
                    if m.name.startswith("/") or ".." in Path(m.name).parts:
                        raise ValueError(f"unsafe path in tarball: {m.name}")
                tar.extractall(tmp)
        (tmp / _MARKER).write_text(str(tarball))
        try:
            tmp.rename(dest)
        except OSError:  # another run finished first: use its copy
            shutil.rmtree(tmp, ignore_errors=True)
            if not marker.exists():
                raise
    entries = [p for p in dest.iterdir() if p.name != _MARKER]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def top_level_entries(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.iterdir() if p.name != _MARKER)


def iter_files(root: Path):
    """Yields (relative posix path, Path) for every regular file and symlink under root."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_file():
            if path.name == _MARKER:
                continue
            yield path.relative_to(root).as_posix(), path

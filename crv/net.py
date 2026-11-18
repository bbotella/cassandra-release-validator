"""Small HTTP helpers on top of urllib: fetch, download with caching, and directory listing crawling."""

from __future__ import annotations

import html
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from . import __version__

USER_AGENT = f"cassandra-release-validator/{__version__}"
TIMEOUT = 120
RETRIES = 3

_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status = status


def _request(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 401, 403):
                raise HttpError(url, e.code, f"HTTP {e.code}") from None
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    status = getattr(last, "code", None)
    raise HttpError(url, status, str(last))


def fetch_bytes(url: str) -> bytes:
    with _request(url) as resp:
        return resp.read()


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def head(url: str) -> dict[str, str] | None:
    """Returns the response headers, or None when the URL does not exist."""
    try:
        with _request(url, method="HEAD") as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except HttpError as e:
        if e.status in (404, 410):
            return None
        raise


def exists(url: str) -> bool:
    try:
        return head(url) is not None
    except HttpError:
        return False


def download(url: str, dest: Path, refresh: bool = False) -> Path:
    """Downloads url to dest atomically. An existing dest is reused unless refresh is set."""
    if dest.exists() and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    # unique per process and thread, so concurrent runs never write into the same temporary file
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}-{threading.get_ident()}.part")
    with _request(url) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)
    tmp.replace(dest)
    return dest


def download_many(
    pairs: Iterable[tuple[str, Path]],
    jobs: int = 8,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Downloads (url, dest) pairs in parallel. Returns {url: error} for failures."""
    errors: dict[str, str] = {}

    def one(pair: tuple[str, Path]) -> None:
        url, dest = pair
        try:
            if progress:
                progress(f"downloading {url}")
            download(url, dest, refresh)
        except Exception as e:  # noqa: BLE001 - reported to the user
            errors[url] = str(e)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(one, list(pairs)))
    return errors


def list_dir(url: str) -> list[str]:
    """Lists the direct children of an Apache-style (svn, Nexus, Maven) HTML directory index.

    Returns names; directories keep their trailing slash.
    """
    if not url.endswith("/"):
        url += "/"
    page = fetch_text(url)
    base = urllib.parse.urlsplit(url)
    names: list[str] = []
    seen: set[str] = set()
    for href in _HREF.findall(page):
        href = html.unescape(href)
        if href.startswith(("?", "#", "mailto:")):
            continue
        absolute = urllib.parse.urljoin(url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.netloc != base.netloc or parts.query:
            continue
        if not parts.path.startswith(base.path) or parts.path == base.path:
            continue
        rest = urllib.parse.unquote(parts.path[len(base.path):])
        if not rest or rest.startswith("/") or "/" in rest.rstrip("/") or rest.rstrip("/") in (".", ".."):
            continue
        if rest not in seen:
            seen.add(rest)
            names.append(rest)
    return names


def walk(url: str, max_depth: int = 8, prefix: str = "") -> list[str]:
    """Recursively lists all files under an HTML directory index, as relative paths."""
    files: list[str] = []
    for name in list_dir(url):
        if name.endswith("/"):
            if max_depth > 0:
                files += walk(url.rstrip("/") + "/" + name, max_depth - 1, prefix + name)
        else:
            files.append(prefix + name)
    return files

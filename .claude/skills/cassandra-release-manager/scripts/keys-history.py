#!/usr/bin/env python3
"""Shows when and by whom the release signing key was added to the Cassandra KEYS file.

validate-release already checks that the signature is valid and the key is in KEYS. This adds the
provenance: the svn commit that added the key, its author (KEYS in dist/release can only be changed by
PMC members), and how old the key was when the release was signed. A key created and added hours before
the RC is normal for a first-time release manager. It is still worth knowing, and worth a check that the
commit references a ticket or was announced.

usage: keys-history.py <signing key fingerprint> [--limit 25]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = "https://dist.apache.org/repos/dist"
KEYS_PATH = "/release/cassandra/KEYS"
UA = {"User-Agent": "cassandra-release-validator"}
NS = {"S": "svn:", "D": "DAV:"}


def request(url: str, method: str = "GET", body: bytes | None = None) -> bytes:
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "text/xml"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=headers), timeout=60) as r:
        return r.read()


def head_revision() -> int:
    req = urllib.request.Request(BASE + KEYS_PATH, method="HEAD", headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(re.match(r'W?/?"(\d+)//', r.headers["ETag"]).group(1))


def svn_log(rev: int, limit: int) -> list[dict]:
    body = (
        '<?xml version="1.0" encoding="utf-8"?><S:log-report xmlns:S="svn:">'
        f"<S:start-revision>{rev}</S:start-revision><S:end-revision>0</S:end-revision><S:limit>{limit}</S:limit>"
        "<S:path></S:path><S:revprop>svn:author</S:revprop><S:revprop>svn:date</S:revprop>"
        "<S:revprop>svn:log</S:revprop></S:log-report>"
    ).encode()
    root = ET.fromstring(request(f"{BASE}/!svn/bc/{rev}{KEYS_PATH}", "REPORT", body))
    items = []
    for item in root.findall("S:log-item", NS):
        items.append({
            "rev": int(item.findtext("D:version-name", namespaces=NS)),
            "date": (item.findtext("S:date", namespaces=NS) or "")[:16].replace("T", " "),
            "author": item.findtext("D:creator-displayname", namespaces=NS) or "?",
            "message": (item.findtext("D:comment", namespaces=NS) or "").strip().splitlines()[0:1],
        })
    return items


def fingerprints(keys: bytes, home: Path) -> dict[str, tuple[str, list[str]]]:
    """fingerprint -> (creation date, uids) for every primary key in a KEYS file, without importing it."""
    proc = subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--with-colons", "--import-options", "show-only", "--import"],
        input=keys, capture_output=True,
    )
    out, current, created = {}, None, ""
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        f = line.split(":")
        if f[0] == "pub":
            current, created = None, time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(f[5]))) if f[5].isdigit() else "?"
        elif f[0] == "fpr" and current is None:
            current = f[9]
            out[current] = (created, [])
        elif f[0] == "uid" and current and current in out and f[1] not in ("r", "e"):  # skip revoked/expired uids
            out[current][1].append(f[9])
        elif f[0] == "sub":
            current = current or "sub"  # stop picking up subkey fingerprints as primaries
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("fingerprint", help="signing key fingerprint (validate-release prints it in the Signer section)")
    ap.add_argument("--limit", type=int, default=25, help="how many KEYS revisions to look back (default 25)")
    args = ap.parse_args()
    fpr = re.sub(r"\s+", "", args.fingerprint).upper()
    if not shutil.which("gpg"):
        print("gpg is required")
        return 2

    home = Path(tempfile.mkdtemp(prefix="crv-keys-"))  # short path: gpg sockets have a length limit
    (home / "common.conf").write_text("")
    try:
        log = svn_log(head_revision(), args.limit)
        print("Recent changes to KEYS:")
        for e in log:
            print(f"  r{e['rev']}  {e['date']}  {e['author']:<18} {' '.join(e['message'])}")

        current = fingerprints(request(BASE + KEYS_PATH), home)
        if fpr not in current:
            print(f"\n{fpr} is NOT in the current KEYS file.")
            return 1
        created, uids = current[fpr]
        print(f"\nKey {fpr}\n  uids: {'; '.join(uids)}\n  created: {created} UTC")

        added = None
        for newer, older in zip(log, log[1:]):
            older_keys = fingerprints(request(f"{BASE}/!svn/bc/{older['rev']}{KEYS_PATH}"), home)
            if fpr not in older_keys:
                added = newer
                break
        if added:
            print(f"  added to KEYS in r{added['rev']} on {added['date']} UTC by {added['author']}: {' '.join(added['message'])}")
        else:
            print(f"  already in KEYS {len(log)} revisions ago (r{log[-1]['rev']}, {log[-1]['date']}); not newly added")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

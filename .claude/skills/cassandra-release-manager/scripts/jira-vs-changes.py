#!/usr/bin/env python3
"""Cross-checks the JIRA fix version of a release against its CHANGES.txt section.

Reports, for the release's JIRA version:
  * tickets resolved with this fix version but not listed in CHANGES.txt (on another branch? not merged?)
  * tickets still open with this fix version (should be moved before the version is released)
  * tickets listed in CHANGES.txt whose JIRA fix versions do not include this release

These are housekeeping findings for the release manager, not release blockers. The release process
moves fix versions when the version is bumped after the vote.

CHANGES.txt is read from the commit validate-release fetched (run it first), or from --changes FILE.
Uses the public JIRA REST API of issues.apache.org (no login).

usage: jira-vs-changes.py --project analytics --version 0.5.0 [--jira-version 0.5] [--changes FILE] [--cache DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassandra-release-validator"
JIRA = "https://issues.apache.org/jira/rest/api/2/"
PROJECTS = {  # validate-release project -> (JIRA key, git repo)
    "cassandra": ("CASSANDRA", "cassandra"),
    "sidecar": ("CASSSIDECAR", "cassandra-sidecar"),
    "analytics": ("CASSANALYTICS", "cassandra-analytics"),
}
UA = {"User-Agent": "cassandra-release-validator"}
_VERSION_HEADER = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?\s*$")


def get(path: str, **params) -> dict:
    url = JIRA + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as resp:
        return json.load(resp)


def search(jql: str) -> list[dict]:
    issues, start = [], 0
    while True:
        page = get("search", jql=jql, fields="status,resolution,summary,fixVersions", maxResults=100, startAt=start)
        issues += page.get("issues", [])
        start += len(page.get("issues", []))
        if start >= page.get("total", 0) or not page.get("issues"):
            return issues


def resolve_jira_version(key: str, version: str, explicit: str | None) -> str | None:
    names = [v["name"] for v in get(f"project/{key}/versions")]
    if explicit:
        return explicit if explicit in names else None
    for candidate in (version, re.sub(r"\.0$", "", version), version.split("-")[0]):
        if candidate in names:
            return candidate
    print(f"No JIRA version named {version} in {key}. Recent versions: {', '.join(names[-12:])}")
    print("Pass --jira-version with the right one.")
    return None


def changes_section(text: str, version: str) -> str:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines[:40]) if l.strip() == version), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if _VERSION_HEADER.match(lines[j]) and lines[j].strip() != version), len(lines))
    return "\n".join(lines[start:end])


def changes_at(cache: Path, project: str, version: str, ref: str) -> str | None:
    repo = cache / f"{project}-{version}" / "git" / f"{PROJECTS[project][1]}.git"
    proc = subprocess.run(["git", "show", f"{ref}:CHANGES.txt"], cwd=repo, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))


def commits_since_previous(cache: Path, project: str, version: str) -> tuple[str, str, str] | None:
    """Deepens validate-release's shallow clone back to the previous release tag.

    Returns (range description, `git log` text of the commits in between, local ref of the release), or
    None when it can't.
    """
    repo_name = PROJECTS[project][1]
    repo = cache / f"{project}-{version}" / "git" / f"{repo_name}.git"
    url = f"https://gitbox.apache.org/repos/asf/{repo_name}.git"
    prefix = {"cassandra": "cassandra-", "sidecar": "cassandra-sidecar-", "analytics": "cassandra-analytics-"}[project]
    tags = subprocess.run(["git", "ls-remote", "--tags", url, f"refs/tags/{prefix}*"], capture_output=True, text=True)
    finals = []
    for line in tags.stdout.splitlines():
        name = line.split("refs/tags/", 1)[-1]
        v = name[len(prefix):]
        if not name.endswith("^{}") and re.fullmatch(r"\d+(\.\d+)*(-[\w.]+)?", v) and _vtuple(v) < _vtuple(version):
            finals.append((v, name))
    if not finals or not repo.exists():
        return None
    same_branch = [f for f in finals if _vtuple(f[0])[:2] == _vtuple(version)[:2]]
    prev_version, prev_tag = max(same_branch or finals, key=lambda f: _vtuple(f[0]))
    # the RC tag while the vote runs; the final tag once the release is finished (the RC tag is deleted then)
    for tag in (f"{version}-tentative", f"{prefix}{version}"):
        fetch = subprocess.run(
            ["git", "fetch", "--quiet", f"--shallow-exclude={prev_tag}", url, f"refs/tags/{tag}:refs/crv/{tag}"],
            cwd=repo, capture_output=True, text=True,
        )
        if fetch.returncode == 0:
            log = subprocess.run(["git", "log", "--format=%H %s%n%b", f"refs/crv/{tag}"], cwd=repo, capture_output=True, text=True)
            return f"{prev_tag}..{tag}", log.stdout, f"refs/crv/{tag}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", required=True, choices=sorted(PROJECTS))
    ap.add_argument("--version", required=True)
    ap.add_argument("--jira-version", help="JIRA version name if it differs (e.g. Analytics uses '0.5' for 0.5.0)")
    ap.add_argument("--changes", type=Path, help="CHANGES.txt to read instead of the one at the fetched tag")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args()
    key = PROJECTS[args.project][0]

    history = None
    if args.changes:
        text = args.changes.read_text()
    else:
        history = commits_since_previous(args.cache, args.project, args.version)
        text = changes_at(args.cache, args.project, args.version, history[2]) if history else None
    if text is None:
        print("CHANGES.txt not found: run validate-release for this candidate first, or pass --changes.")
        return 2
    section = changes_section(text, args.version)
    if not section:
        print(f"CHANGES.txt has no '{args.version}' section near the top.")
        return 1
    listed = set(re.findall(rf"\b{key}-\d+\b", section))

    jira_version = resolve_jira_version(key, args.version, args.jira_version)
    if not jira_version:
        return 2
    issues = search(f'project = {key} AND fixVersion = "{jira_version}"')
    resolved = {i["key"]: i for i in issues if i["fields"].get("resolution")}
    unresolved = {i["key"]: i for i in issues if not i["fields"].get("resolution")}

    print(f"JIRA {key} fixVersion '{jira_version}': {len(resolved)} resolved, {len(unresolved)} unresolved")
    print(f"CHANGES.txt '{args.version}' section: {len(listed)} {key} tickets")
    if history:
        print(f"Commit messages checked: {history[0]}")
    else:
        print("Commit history not available: the 'in release' column is unknown (check with git log --grep in a clone).")
    print()

    def in_release(k: str) -> str:
        if not history:
            return ""
        return "commit in release" if re.search(rf"\b{re.escape(k)}\b", history[1]) else "NO commit in release"

    def show(title: str, items: list[dict], with_commits: bool = False) -> None:
        print(f"{title}: {len(items)}")
        for i in items:
            f = i["fields"]
            status = f["status"]["name"] + (f" / {f['resolution']['name']}" if f.get("resolution") else "")
            note = f" ({in_release(i['key'])})" if with_commits and history else ""
            print(f"  {i['key']:<20} [{status}]{note} {f['summary'][:90]}")
        print()

    show("Resolved with this fix version but not in CHANGES.txt", [resolved[k] for k in sorted(resolved, key=_num) if k not in listed], with_commits=True)
    show("Still open with this fix version", [unresolved[k] for k in sorted(unresolved, key=_num)])
    missing = sorted((listed - set(resolved) - set(unresolved)), key=_num)
    if missing:
        others = search(f"key in ({','.join(missing)})") if missing else []
        for i in others:
            versions = ", ".join(v["name"] for v in i["fields"].get("fixVersions", [])) or "none"
            i["fields"]["summary"] = f"fixVersions: {versions} | {i['fields']['summary']}"
        show("In CHANGES.txt but this fix version is not set in JIRA", sorted(others, key=lambda i: _num(i["key"])))
    else:
        show("In CHANGES.txt but this fix version is not set in JIRA", [])
    return 0


def _num(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


if __name__ == "__main__":
    sys.exit(main())

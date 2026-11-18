"""Parses a release vote email (or fetches it from lists.apache.org) into structured data."""

from __future__ import annotations

import email
import email.policy
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import net
from .projects import NEXUS_REPOS, STAGING_PREFIX, Project, by_name, by_repo

LISTS_API = "https://lists.apache.org/api/"
LIST_NAME = "dev"
LIST_DOMAIN = "cassandra.apache.org"

_SUBJECT = re.compile(
    r"\[VOTE\]\s*Release\s+Apache\s+Cassandra(?:\s+(?P<proj>Sidecar|Analytics))?\s+(?P<version>\d[\w.\-]*\w)",
    re.IGNORECASE,
)
_SHA1 = re.compile(r"\bsha-?1?\s*[:=]\s*([0-9a-f]{40})\b", re.IGNORECASE)
_GITHUB_TAG = re.compile(r"https?://github\.com/apache/(?P<repo>cassandra[\w-]*)/tree/(?P<tag>[^\s/<>\"')]+)")
_GITBOX_TAG = re.compile(
    r"https?://gitbox\.apache\.org/repos/asf\?p=(?P<repo>cassandra[\w-]*)\.git;a=\w+;h=refs/tags/(?P<tag>[^\s;<>\"')]+)"
)
_DIST = re.compile(
    r"https?://dist\.apache\.org/repos/dist/dev/cassandra/(?:(?P<sub>cassandra-[\w-]+)/)?(?P<version>\d[\w.\-]*\w)/?"
)
_NEXUS = re.compile(
    re.escape(NEXUS_REPOS) + re.escape(STAGING_PREFIX) + r"(?P<repo>\d+)/?(?P<path>[\w./\-]*)"
)
_THREAD_ID = re.compile(r"^[a-z0-9]{32}$")


@dataclass
class MavenRef:
    repo_id: str
    group_path: str  # e.g. "org/apache/cassandra"
    artifact: str
    version: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.group_path, self.artifact, self.version)


@dataclass
class VoteInfo:
    project: Project | None = None
    version: str | None = None
    subject: str = ""
    sender: str = ""
    date: str = ""
    source: str = ""  # where the email came from
    sha1: str | None = None
    git_repo: str | None = None
    tag: str | None = None
    dist_url: str | None = None
    staging_repos: list[str] = field(default_factory=list)
    maven: list[MavenRef] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # inconsistencies found while parsing


def parse(body: str, subject: str = "") -> VoteInfo:
    info = VoteInfo(subject=subject.strip())
    # Some mail clients wrap long URLs. Undo a line break right after a '/' inside a URL, but only when the
    # next line is nothing but a path continuation (never when it starts another URL).
    text = re.sub(r"(https?://\S+/)\n(?=[\w.\-]+(?:/[\w.\-]*)*/?[ \t]*(?:\n|$))", r"\1", body)

    m = _SUBJECT.search(subject) or _SUBJECT.search(text)
    if m:
        info.project = by_name(m.group("proj") or "")
        info.version = m.group("version")

    sha = _SHA1.search(text)
    if sha:
        info.sha1 = sha.group(1).lower()

    tag = _GITHUB_TAG.search(text) or _GITBOX_TAG.search(text)
    if tag:
        info.git_repo = tag.group("repo")
        info.tag = tag.group("tag")

    dist = _DIST.search(text)
    if dist:
        info.dist_url = dist.group(0) if dist.group(0).endswith("/") else dist.group(0) + "/"
        dist_project = by_name(dist.group("sub") or "")
        if info.project is None:
            info.project = dist_project
        elif dist_project is not None and dist_project != info.project:
            info.problems.append(f"dist URL is for '{dist_project.key}' but the subject is for '{info.project.key}'")
        if info.version is None:
            info.version = dist.group("version")
        elif dist.group("version") != info.version:
            info.problems.append(f"dist URL version {dist.group('version')} differs from the subject version {info.version}")

    if info.git_repo:
        repo_project = by_repo(info.git_repo)
        if info.project is None:
            info.project = repo_project
        elif repo_project is not None and repo_project != info.project:
            info.problems.append(f"git URL is for repo '{info.git_repo}' but the subject is for '{info.project.key}'")

    seen: set[tuple[str, str, str, str]] = set()
    for nm in _NEXUS.finditer(text):
        repo_id = nm.group("repo")
        if repo_id not in info.staging_repos:
            info.staging_repos.append(repo_id)
        parts = [p for p in nm.group("path").split("/") if p]
        if len(parts) >= 3 and parts[0] == "org":
            ref = MavenRef(repo_id, "/".join(parts[:-2]), parts[-2], parts[-1])
            k = (repo_id, *ref.key)
            if k not in seen:
                seen.add(k)
                info.maven.append(ref)
    return info


# ------------------------------------------------------------------ sources
def read_file(path: str) -> tuple[str, str, str, str]:
    """Reads an email from a file ('-' = stdin). Returns (subject, body, sender, date).

    Accepts a raw RFC 822 message (.eml, "show original"), or plain pasted text.
    """
    import sys

    raw = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    head = raw[:4096].decode("utf-8", errors="replace")
    if re.search(r"(?mi)^(subject|from|message-id|received|date):", head) and "\n\n" in raw.decode(
        "utf-8", errors="replace"
    ).replace("\r\n", "\n"):
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        if msg.get("Subject"):
            part = msg.get_body(preferencelist=("plain", "html"))
            body = part.get_content() if part is not None else ""
            if part is not None and part.get_content_type() == "text/html":
                body = re.sub(r"<[^>]+>", " ", body)
            return str(msg.get("Subject", "")), body, str(msg.get("From", "")), str(msg.get("Date", ""))
    text = raw.decode("utf-8", errors="replace")
    subject = ""
    sm = re.search(r"(?mi)^(?:subject:\s*)?(\[VOTE\].*)$", text)
    if sm:
        subject = sm.group(1)
    return subject, text, "", ""


def thread_id_from(value: str) -> str | None:
    """Accepts a lists.apache.org thread/list URL or a bare id."""
    value = value.strip()
    if _THREAD_ID.match(value):
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.netloc.endswith("lists.apache.org"):
        m = re.search(r"/thread/([a-z0-9]{32})", parsed.path)
        if m:
            return m.group(1)
        q = urllib.parse.parse_qs(parsed.query)
        for key in ("id", "mid"):
            if key in q and _THREAD_ID.match(q[key][0]):
                return q[key][0]
    return None


def fetch_from_lists(message_id: str) -> tuple[str, str, str, str]:
    data = json.loads(net.fetch_text(f"{LISTS_API}email.lua?id={urllib.parse.quote(message_id)}"))
    date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(data.get("epoch", 0))) if data.get("epoch") else ""
    return data.get("subject", ""), data.get("body", ""), data.get("from", ""), date


@dataclass
class VoteThread:
    id: str
    subject: str
    sender: str
    epoch: int
    replies: int = 0
    result: str = ""  # subject of a [RESULT] mail, if any

    @property
    def date(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.epoch))


def list_votes(days: int = 30) -> list[VoteThread]:
    """Recent '[VOTE] Release Apache Cassandra ...' threads on dev@cassandra, newest first."""
    q = urllib.parse.urlencode({"list": LIST_NAME, "domain": LIST_DOMAIN, "d": f"lte={days}d", "q": "VOTE Release"})
    data = json.loads(net.fetch_text(f"{LISTS_API}stats.lua?{q}"))
    emails = data.get("emails", [])
    threads: dict[str, VoteThread] = {}
    for e in emails:
        subj = e.get("subject", "")
        if re.match(r"^\s*\[VOTE\]\s*Release", subj, re.IGNORECASE):
            threads[_norm(subj)] = VoteThread(e["id"], subj, e.get("from", ""), int(e.get("epoch", 0)))
    for e in emails:
        subj = e.get("subject", "")
        norm = _norm(subj)
        if re.match(r"^\s*(re|aw|fwd?):", subj, re.IGNORECASE) and norm in threads:
            threads[norm].replies += 1
        m = re.match(r"^\s*(?:re:\s*)?\[RESULT\]\s*(?:\[VOTE\]\s*)?(.*)$", subj, re.IGNORECASE)
        if m:
            key = _norm("[VOTE] " + m.group(1))
            if key in threads:
                threads[key].result = subj
    return sorted(threads.values(), key=lambda t: t.epoch, reverse=True)


@dataclass
class Reply:
    id: str
    sender: str
    epoch: int
    subject: str
    text: str  # the reply's own text, with quoted parts removed
    vote: str | None  # "+1", "-1", "+0", "-0" or None
    binding: str | None  # "binding", "non-binding" or None (not stated)

    @property
    def date(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.epoch))


# "On <date>, <name> wrote:" in the languages seen on the list
_QUOTE_HEADER = re.compile(r"(?ims)^\s*(?:On|El|Le|Am|Il)\b[^\n]{0,200}(?:\n[^\n]{0,200}){0,2}?(?:wrote|escribió|a écrit|schrieb|ha scritto)\s*:?\s*$")
# a vote token: "+1", "-1", "+0", "-0", followed by punctuation or space but not more digits ("+1.5", "+10")
_VOTE = re.compile(r"(?<![\w.+-])([+-][01])(?!\w|\.\d)")


def own_text(body: str) -> str:
    """The part of a reply its author wrote: drops quoted lines and everything after the quote header."""
    m = _QUOTE_HEADER.search(body)
    if m:
        body = body[: m.start()]
    body = re.split(r"(?m)^-{2,}\s*Original Message\s*-{2,}", body)[0]
    lines = [line.rstrip() for line in body.splitlines() if not line.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_vote(text: str) -> tuple[str | None, str | None]:
    head = text[:600]
    m = _VOTE.search(head)
    vote = m.group(1) if m else None
    if vote is None:
        return None, None
    lowered = head.lower()
    binding = "non-binding" if re.search(r"non[- ]?binding", lowered) else ("binding" if "binding" in lowered else None)
    return vote, binding


def fetch_thread(message_id: str) -> tuple[str, list[Reply]]:
    """Returns the thread's subject and its replies (oldest first), with the vote each one casts."""
    from concurrent.futures import ThreadPoolExecutor

    data = json.loads(net.fetch_text(f"{LISTS_API}thread.lua?id={urllib.parse.quote(message_id)}"))
    root = data.get("thread", {})
    ids: list[str] = []

    def walk(node: dict) -> None:
        for child in node.get("children", []) or []:
            cid = child.get("tid") or child.get("id") or child.get("mid")
            if cid and cid not in ids:
                ids.append(cid)
            walk(child)

    walk(root)

    def load(cid: str) -> Reply:
        e = json.loads(net.fetch_text(f"{LISTS_API}email.lua?id={urllib.parse.quote(cid)}"))
        text = own_text(e.get("body", ""))
        vote, binding = parse_vote(text)
        return Reply(cid, e.get("from", ""), int(e.get("epoch", 0)), e.get("subject", ""), text, vote, binding)

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(load, ids))
    return root.get("subject", ""), sorted(replies, key=lambda r: r.epoch)


def _norm(subject: str) -> str:
    s = re.sub(r"^\s*((re|aw|fwd?):\s*)+", "", subject, flags=re.IGNORECASE)
    s = re.sub(r"^\s*\[RESULT\]\s*", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip().lower()

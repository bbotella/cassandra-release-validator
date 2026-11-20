"""Steps shared by Analytics, Sidecar and Cassandra. Line references are to apache/cassandra-builds
trunk 7637494 (CP/CF = cassandra-release/{prepare,finish}_release.sh, SP/SF = sidecar, AP/AF = analytics)."""

from __future__ import annotations

import calendar
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from ... import net, vote_email
from ...checks.git import resolve_tag
from ...checks.maven import Staged, crawl, repo_url
from ...checks.runtime import find_engine
from ...projects import STAGING_PREFIX
from .. import mail, sandbox
from ..config import CACHE
from ..context import ReleaseContext
from ..executor import StepFailed
from ..state import ordinal
from ..util import dist_dev_dir, dist_release_parent, git, label, push_target, source_dir, svn_where
from . import NotYet, step

REPO = Path(__file__).resolve().parents[3]  # this repository (validate-release lives at its root)


class ChecksFailed(StepFailed):
    def __init__(self, failures: list[str]):
        super().__init__(f"{len(failures)} check(s)", 1, failures)
        self.args = (f"{len(failures)} check(s) failed",)

    def __str__(self) -> str:
        return self.args[0]


# ------------------------------------------------------------------ preflight
def _version_check(project: str, found: str | None, v: str, rehearsal: bool, file: str) -> tuple[str, str, str]:
    """The official scripts refuse to run unless the version file says the release version. A rehearsal
    of Sidecar or Analytics may run before that commit: their builds pass -Pversion, so the artifacts match."""
    name = f"Version in {file}"
    if found == v:
        return "PASS", name, f"{found} (release {v})"
    if rehearsal and project in ("sidecar", "analytics"):
        return "WARN", name, (f"{found} (release {v}): fine for a rehearsal, the build passes -Pversion={v}; "
                              f"the real release needs a commit setting version={v} first")
    return "FAIL", name, f"{found} (release {v})"


# ------------------------------------------------------------------ preflight
@step("preflight", "Read-only checks before anything is pushed", outward=False, repeatable=True,
      replicates="CP:27-36,118-165 SP:43-45,178-225 AP:43-45,138-183 (tools, clean tree, version, nothing staged yet)")
def preflight(ctx: ReleaseContext) -> str:
    p, v, rehearsal = ctx.project, ctx.version, ctx.endpoints.rehearsal
    results: list[tuple[str, str, str]] = []

    def check(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))
        print(f"   [{status}] {name}" + (f" — {detail}" if detail else ""))

    src = source_dir(ctx)
    if not (src / ".git").exists():
        check("FAIL", "Source checkout", f"{src} is not a git checkout (use --source)")
        raise ChecksFailed([f"{s}: {n} {d}" for s, n, d in results if s == "FAIL"])
    sha = git("rev-parse", "HEAD", cwd=src)
    dirty = git("status", "--porcelain", "--untracked-files=no", cwd=src)
    check("FAIL" if dirty else "PASS", "Clean checkout", f"{src} at {sha[:12]}" + (" has uncommitted changes" if dirty else ""))
    ctx.state.data.update(source=str(src), head=sha)

    if p.version_file:
        content = git("show", f"HEAD:{p.version_file}", cwd=src, check=False)
        if p.version_pattern == "pom":
            from ...checks.git import _pom_version
            found = _pom_version(content)
        else:
            m = re.search(p.version_pattern, content)
            found = m.group(1) if m else None
        check(*_version_check(p.key, found, v, rehearsal, p.version_file))
    changes = git("show", f"HEAD:{p.changes_file}", cwd=src, check=False)
    heading = p.changes_heading_re(v)
    has = any(heading.match(line.strip()) for line in changes.splitlines()[:30])
    check("PASS" if has else "FAIL", f"{p.changes_file} has a {v} section")

    # tools
    for tool in ("git", "gpg", "curl"):
        check("PASS" if shutil.which(tool) else "FAIL", f"{tool} installed")
    if not rehearsal:
        check("PASS" if shutil.which("svn") else "FAIL", "svn installed (dist steps run here, with your credentials)",
              "" if shutil.which("svn") else "brew install subversion")
    engine, why = find_engine(None)
    check("PASS" if engine else "FAIL", "Container engine", Path(engine).name if engine else why)

    # signing
    if rehearsal:
        check("INFO", "Signing key", "a throwaway rehearsal key is created in the sandbox")
    else:
        _check_signing_key(ctx, check)
        _check_config(ctx, check)

    # nothing staged or tagged yet (CP:142-165, SP:202-225, AP:160-183)
    try:
        exists = resolve_tag(ctx.endpoints.git_url, ctx.tag) if not rehearsal or Path(
            ctx.endpoints.git_url.removeprefix("file://")).exists() else None
    except Exception:  # noqa: BLE001
        exists = None
    check("FAIL" if exists else "PASS", f"Tag {ctx.tag} does not exist yet", exists or "")
    if not rehearsal:
        for what, url in (("dist/dev", dist_dev_dir(ctx) + "/"),
                          ("archive.apache.org", f"https://archive.apache.org/dist/cassandra/{p.dist_subdir}{v}/")):
            found = net.exists(url)
            check("FAIL" if found else "PASS", f"{what} has no {v} yet", url if found else "")
        open_tickets = _jira_open(p.key, v)
        if open_tickets is None:
            check("WARN", "JIRA", "could not query issues.apache.org")
        else:
            check("WARN" if open_tickets else "PASS", "No open JIRA tickets with this fix version",
                  ", ".join(open_tickets[:8]))
        drift = _upstream_drift()
        if drift is not None:
            check("WARN" if drift else "PASS", "Official release scripts unchanged since the replica was reviewed",
                  ", ".join(drift) + " changed: see `cut-release check-upstream`" if drift else "")

    failures = [f"{n}: {d}" for s, n, d in results if s == "FAIL"]
    ctx.state.save()
    if failures:
        raise ChecksFailed(failures)
    warnings = sum(1 for s, _, _ in results if s == "WARN")
    return f"{len(results)} checks, {warnings} warnings; release commit candidate {sha[:12]}"


def _check_signing_key(ctx: ReleaseContext, check) -> None:
    fpr = (ctx.args.signing_key or "").replace(" ", "").upper()
    if not fpr:
        check("FAIL", "Signing key", "give your release key with --signing-key <fingerprint> (or export gpg_key)")
        return
    out = subprocess.run(["gpg", "--with-colons", "--list-secret-keys", fpr], capture_output=True, text=True)
    sec = [line.split(":") for line in out.stdout.splitlines() if line.startswith("sec:")]
    if not sec:
        check("FAIL", "Signing key", f"no secret key {fpr} in your keyring (see docs/release-signing.md)")
        return
    algo, bits = sec[0][3], int(sec[0][2] or 0)
    check("PASS" if algo == "1" and bits >= 4096 else "FAIL", "Signing key is RSA 4096", f"algo {algo}, {bits} bits")
    keys = net.fetch_bytes(ctx.endpoints.keys)
    listed = subprocess.run(["gpg", "--batch", "--with-colons", "--import-options", "show-only", "--import"],
                            input=keys, capture_output=True).stdout.decode(errors="replace")
    check("PASS" if f":{fpr}:" in listed else "FAIL", "Signing key is in KEYS", ctx.endpoints.keys)
    conf = Path.home() / ".gnupg" / "gpg-agent.conf"
    pinentry = next((line for line in conf.read_text().splitlines() if line.startswith("pinentry-program")), "") \
        if conf.exists() else ""
    check("PASS" if "mac" in pinentry else "WARN", "A pinentry that works without a terminal",
          pinentry or "install pinentry-mac and set pinentry-program (docs/release-signing.md)")


def _check_config(ctx: ReleaseContext, check) -> None:
    """Checks that the build tools' credential settings exist — key names only, values are never read out."""
    if ctx.project.key in ("sidecar", "analytics"):
        props = Path.home() / ".gradle" / "gradle.properties"
        names = {line.split("=", 1)[0].strip() for line in props.read_text().splitlines()
                 if "=" in line and not line.lstrip().startswith("#")} if props.exists() else set()
        need = ["maven.repository.url", "maven.username", "maven.password", "signing.gnupg.keyName"]
        missing = [n for n in need if n not in names]
        check("FAIL" if missing else "PASS", "~/.gradle/gradle.properties has the publishing settings",
              f"missing: {', '.join(missing)}" if missing else ", ".join(need))
    else:
        settings = Path.home() / ".m2" / "settings.xml"
        text = settings.read_text() if settings.exists() else ""
        ok = "<id>apache.releases.https</id>" in text
        check("PASS" if ok else "FAIL", "~/.m2/settings.xml has the apache.releases.https server",
              "" if ok else "see https://cassandra.apache.org/_/development/release_process.html")
    if ctx.project.packages:
        import os
        check("PASS" if os.environ.get("ARTIFACTORY_API_KEY") else "WARN", "ARTIFACTORY_API_KEY is set",
              "" if os.environ.get("ARTIFACTORY_API_KEY") else "needed later, by finish-packages")


def _jira_open(project_key: str, version: str) -> list[str] | None:
    jira = {"cassandra": "CASSANDRA", "sidecar": "CASSSIDECAR", "analytics": "CASSANALYTICS"}[project_key]
    try:
        names = [x["name"] for x in json.loads(net.fetch_text(f"https://issues.apache.org/jira/rest/api/2/project/{jira}/versions"))]
        name = next((n for n in (version, re.sub(r"\.0$", "", version)) if n in names), None)
        if not name:
            return []
        jql = urllib.parse.quote(f'project = {jira} AND fixVersion = "{name}" AND resolution = Unresolved')
        data = json.loads(net.fetch_text(f"https://issues.apache.org/jira/rest/api/2/search?jql={jql}&fields=summary&maxResults=50"))
        return [i["key"] for i in data.get("issues", [])]
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------ tag
@step("tag", "Tag the release commit <version>-tentative and push the tag", outward=True,
      replicates="CP:251-252 SP:281-282 AP:232-233")
def tag(ctx: ReleaseContext) -> str:
    src = Path(ctx.state.data.get("source") or source_dir(ctx))
    if ctx.endpoints.rehearsal:
        sandbox.ensure(ctx, src)
    sha = ctx.state.data.get("head") or git("rev-parse", "HEAD", cwd=src)
    target = push_target(ctx)
    if ctx.endpoints.rehearsal:
        # only the sandbox repository gets the tag; your checkout is left untouched
        ctx.run(["git", "push", "-q", target, f"{sha}:refs/tags/{ctx.tag}"], f"push {ctx.tag} -> {sha[:12]} to the sandbox",
                cwd=src, outward=True)
    else:
        ctx.run(["git", "tag", ctx.tag, sha], f"create the tag {ctx.tag} at {sha[:12]}", cwd=src, outward=True)
        ctx.run(["git", "push", target, f"refs/tags/{ctx.tag}"], f"push {ctx.tag} to {target}", cwd=src, outward=True)
    if ctx.dry_run:
        return ""
    pushed = resolve_tag(ctx.endpoints.git_url, ctx.tag)
    if pushed != sha:
        raise RuntimeError(f"{ctx.tag} on {ctx.endpoints.git_url} points at {pushed}, expected {sha}")
    ctx.state.data.update(sha1=sha, tag=ctx.tag)
    return f"{ctx.tag} -> {sha} on {ctx.endpoints.git_url}"


# -------------------------------------------------------------------- staging
_NEXUS_SEEN = CACHE / "nexus-last-staging-repo"


@step("staging", "Find the Nexus staging repositories you closed, and record them", outward=False, repeatable=True,
      replicates="CP:265-266 SP:299-301 AP:249-251 (the 'staging number?' prompts)",
      you_do="before this step, Close the staging repository/repositories at https://repository.apache.org "
             "(Staging Repositories); Nexus only publishes closed ones")
def staging(ctx: ReleaseContext) -> str:
    found: list[str] = []
    if ctx.endpoints.rehearsal:
        root = ctx.endpoints.sandbox / "www" / "repositories"
        candidates = sorted(d.name.removeprefix(STAGING_PREFIX) for d in root.glob(f"{STAGING_PREFIX}*") if d.is_dir())
        with sandbox.serve(ctx.endpoints.sandbox / "www") as url:
            found = [c for c in candidates if _repo_has_release(f"{url}repositories/{STAGING_PREFIX}{c}/", c, ctx)]
    else:
        start = int(ctx.args.from_repo) if getattr(ctx.args, "from_repo", None) else (
            int(_NEXUS_SEEN.read_text()) - 10 if _NEXUS_SEEN.exists() else 1480)
        existing, misses, n = [], 0, start
        while misses < 25:
            if net.exists(repo_url(str(n))):
                existing.append(str(n))
                misses = 0
            else:
                misses += 1
            n += 1
        for repo in reversed(existing[-40:]):
            if _repo_has_release(repo_url(repo), repo, ctx):
                found.append(repo)
        if existing:
            _NEXUS_SEEN.parent.mkdir(parents=True, exist_ok=True)
            _NEXUS_SEEN.write_text(existing[-1])
    if not found:
        raise RuntimeError(f"no closed staging repository has {ctx.project.display} {ctx.version} yet "
                           "(close it in the Nexus UI first; open repositories are not visible)")
    found.sort(key=int)
    ctx.state.data["staging_repos"] = found
    return ", ".join(f"{STAGING_PREFIX}{r}" for r in found)


def _repo_has_release(url: str, repo_id: str, ctx: ReleaseContext) -> bool:
    try:
        staged = crawl(url, repo_id)
    except net.HttpError:
        return False
    versions = {ctx.version, f"{ctx.version}-jdk8"}
    return any(s.version in versions for s in staged)


# ----------------------------------------------------------------- stage-dist
@step("stage-dist", "Commit the source and binary tarballs (+ .asc, .sha256, .sha512) to dist/dev", outward=True,
      replicates="CP:268-276 SP:303-311 AP:253-261")
def stage_dist(ctx: ReleaseContext) -> str:
    files = [Path(f) for f in ctx.state.data.get("dist_files", [])]
    if not files:
        raise RuntimeError("build-publish recorded no dist files")
    return _svn_commit_dir(ctx, dist_dev_dir(ctx), files, f"staging {label(ctx)} {ctx.version}", "dist")


def _svn_commit_dir(ctx: ReleaseContext, url: str, files: list[Path], message: str, key: str) -> str:
    """svn: check out the parent of url, add url's directory with files, commit. Returns the revision."""
    where = svn_where(ctx)
    parent, leaf = url.rsplit("/", 1)
    wc = ctx.workdir / f"svn-{key}"
    ctx.fresh_dir(wc)
    ctx.run(["svn", "checkout", "-q", "--depth", "empty", parent, str(wc)], f"check out {parent}", where=where, always=True)
    target = wc / leaf
    target.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, target / f.name)
    ctx.run(["svn", "add", "-q", "--parents", str(target)], f"svn add {leaf}/ ({len(files)} files)", where=where, outward=True)
    out = ctx.run(["svn", "commit", "-m", message, str(wc)], f"svn commit: {message}", where=where, outward=True, capture=True)
    if ctx.dry_run:
        return ""
    rev = re.search(r"Committed revision (\d+)", out or "")
    ctx.state.data.setdefault("svn_revisions", {})[key] = rev.group(1) if rev else "?"
    return f"{url} (r{rev.group(1) if rev else '?'})"


# ------------------------------------------------------------------- validate
@step("validate", "Run validate-release on what was staged; a FAIL blocks the vote email", outward=False,
      repeatable=True, replicates="CP:474 SP:431 AP:378 ('verify all staged artifacts')")
def validate(ctx: ReleaseContext) -> str:
    report = ctx.endpoints.root / "validation.md"
    cmd = [sys.executable, str(REPO / "validate-release"), "--project", ctx.project.key, "--version", ctx.version,
           "--no-color", "--report", str(report), "--workdir", str(ctx.endpoints.root / "validate-cache")]
    if ctx.state.data.get("sha1"):
        cmd += ["--sha1", ctx.state.data["sha1"]]
    for repo in ctx.state.data.get("staging_repos", []):
        cmd += ["--staging-repo", repo]
    if ctx.endpoints.rehearsal:
        www = ctx.endpoints.sandbox / "www"
        rel = f"dist/{ctx.project.dist_subdir}{ctx.version}"
        sandbox.export_dist(ctx, dist_dev_dir(ctx), www / rel)
        with sandbox.serve(www) as url:
            cmd += ["--dist-url", f"{url}{rel}/", "--nexus-url", f"{url}repositories/", "--git-url", ctx.endpoints.git_url,
                    "--keys", ctx.endpoints.keys]
            return _run_validate(ctx, cmd, report)
    return _run_validate(ctx, cmd, report)


def _run_validate(ctx: ReleaseContext, cmd: list[str], report: Path) -> str:
    try:
        which = "published" if "--released" in cmd or "--tag" in cmd else "staged"
        out = ctx.run(cmd, f"validate-release on the {which} release", capture=True, always=True) or ""
    except StepFailed as e:
        raise StepFailed(f"validate-release found failures (report: {report})", e.returncode,
                         [line for line in e.tail if "FAIL" in line or "RESULT" in line]) from None
    result = next((line.strip() for line in out.splitlines() if "RESULT:" in line), "")
    ctx.state.data["validation_report"] = str(report)
    return f"{result} — {report}"


# ----------------------------------------------------------------- vote-email
@step("vote-email", "Write the [VOTE] and [ANNOUNCE] drafts from what is really staged", outward=False, repeatable=True,
      replicates="CP:430-469 SP:361-426 AP:271-375", you_do="send the vote email to dev@cassandra.apache.org yourself")
def vote_mail(ctx: ReleaseContext) -> str:
    artifacts = _staged_artifacts(ctx)
    sha = ctx.state.data["sha1"]
    ctx.endpoints.mail_dir.mkdir(parents=True, exist_ok=True)
    vote = ctx.endpoints.mail_dir / f"mail_vote_{ctx.version}"
    announce = ctx.endpoints.mail_dir / f"mail_stage_announce_{ctx.version}"
    vote.write_text(mail.vote_mail(ctx.project, ctx.version, sha, artifacts, ctx.state.attempt))
    announce.write_text(mail.announce_mail(ctx.project, ctx.version, sha, artifacts))
    ctx.state.data["vote_subject"] = mail.vote_subject(ctx.project, ctx.version, ctx.state.attempt)
    ctx.note(f"review {vote}, then send it to dev@cassandra.apache.org (the [ANNOUNCE] draft is optional)")
    return f"{len({a.url() for a in artifacts})} Maven URLs; drafts: {vote}, {announce}"


def _staged_artifacts(ctx: ReleaseContext) -> list[mail.Artifact]:
    repos = ctx.state.data.get("staging_repos", [])

    def collect(base: str) -> list[mail.Artifact]:
        out = []
        for repo in repos:
            for s in crawl(f"{base.rstrip('/')}/{STAGING_PREFIX}{repo}/", repo):
                if s.version in (ctx.version, f"{ctx.version}-jdk8"):
                    out.append(mail.Artifact(repo, s.group_path, s.artifact, s.version))
        return out

    if ctx.endpoints.rehearsal:
        with sandbox.serve(ctx.endpoints.sandbox / "www") as url:
            return collect(f"{url}repositories/")
    return collect(ctx.endpoints.nexus_repos)


# ---------------------------------------------------------------------- tally
@step("tally", "Read the vote thread: +1/-1 so far and whether 72 hours have passed", outward=False, repeatable=True,
      replicates="(manual in the official process: the RM counts the votes)",
      you_do="confirm which +1s are binding (PMC); only then is the vote passed")
def tally(ctx: ReleaseContext) -> str:
    """Done only once the vote can have passed: open 72 h, at least three +1s, no -1. Otherwise NotYet,
    which keeps the finish steps blocked. Whether three of the +1s are binding is yours to confirm."""
    thread = ctx.args.thread or ctx.state.data.get("vote_thread")
    if not thread and ctx.endpoints.rehearsal:
        ctx.note("rehearsal: nothing was sent, so there is no thread to count; the vote is taken as passed "
                 "(pass --thread <url> to practise counting a real thread)")
        return "rehearsal: no vote thread, taken as passed"
    if not thread:
        subject = ctx.state.data.get("vote_subject", mail.vote_subject(ctx.project, ctx.version, ctx.state.attempt))
        match = next((t for t in vote_email.list_votes(30) if t.subject.strip() == subject), None)
        if not match:
            raise RuntimeError(f"no thread '{subject}' on dev@ yet (or pass --thread <url>)")
        thread = match.id
    tid = vote_email.thread_id_from(thread) or thread
    ctx.state.data["vote_thread"] = tid
    subject, replies = vote_email.fetch_thread(tid)
    opened = vote_email.fetch_from_lists(tid)[3]
    last: dict[str, vote_email.Reply] = {}
    for reply in replies:
        if reply.vote:
            last[reply.sender] = reply
    for sender, reply in last.items():
        print(f"   {reply.vote:>3} {'(' + reply.binding + ')' if reply.binding else '':<14} {sender}")
    plus = sum(1 for r in last.values() if r.vote == "+1")
    minus = sum(1 for r in last.values() if r.vote == "-1")
    started = calendar.timegm(time.strptime(opened, "%Y-%m-%d %H:%M UTC")) if opened else None
    hours = (time.time() - started) / 3600 if started else 0
    stated_binding = sum(1 for r in last.values() if r.vote == "+1" and r.binding == "binding")
    ctx.state.data["tally"] = {"plus": plus, "minus": minus, "stated_binding": stated_binding, "hours": round(hours)}
    summary = (f"https://lists.apache.org/thread/{tid}: {plus} × +1 ({stated_binding} stated binding), "
               f"{minus} × -1, open {hours:.0f} h")
    if minus:
        raise NotYet(f"{summary}. There is a -1: read it before going on; a re-roll may be needed "
                     f"(cut-release {ctx.project.key} {ctx.version} reroll)")
    if not started:
        raise NotYet(f"{summary}. Could not tell when the vote opened, so 72 h can't be checked")
    if hours < 72:
        raise NotYet(f"{summary}. The vote needs 72 h; about {72 - hours:.0f} h to go")
    if plus < 3:
        raise NotYet(f"{summary}. A release needs at least three binding +1s")
    return summary


# --------------------------------------------------------------------- reroll
@step("reroll", "Cancel this attempt: delete the tentative tag and the dist/dev directory, start the next attempt",
      outward=True, replicates="(not in the official scripts; the RM does it by hand)",
      you_do="Drop the staging repository/repositories in the Nexus UI, and reply on the vote thread that it is cancelled")
def reroll(ctx: ReleaseContext) -> str:
    """Removes only what this attempt got to: an attempt that failed in build-publish has nothing in dist/dev,
    one that failed in tag may have no tag."""
    src = Path(ctx.state.data.get("source") or source_dir(ctx))
    target = push_target(ctx)
    removed = []
    remote = ctx.run(["git", "ls-remote", "--tags", target, f"refs/tags/{ctx.tag}"], f"is {ctx.tag} on {target}?",
                     cwd=src, capture=True, always=True) or ""
    if remote.strip():
        ctx.run(["git", "push", target, f":refs/tags/{ctx.tag}"], f"delete {ctx.tag} from {target}", cwd=src,
                outward=True)
        removed.append(f"{ctx.tag} on {target}")
    local = "" if ctx.endpoints.rehearsal else (  # a rehearsal never tags your checkout
        ctx.run(["git", "tag", "-l", ctx.tag], f"is there a local {ctx.tag}?", cwd=src, capture=True, always=True) or "")
    if local.strip():
        ctx.run(["git", "tag", "-d", ctx.tag], f"delete the local tag {ctx.tag}", cwd=src, outward=True)
        removed.append(f"the local {ctx.tag}")
    try:
        ctx.run(["svn", "info", "-q", dist_dev_dir(ctx)], f"is {dist_dev_dir(ctx)} there?", where=svn_where(ctx),
                capture=True, always=True)
        staged = True
    except StepFailed:
        staged = False
    if staged:
        ctx.run(["svn", "rm", "-m", f"Removing {label(ctx)} {ctx.version} RC (attempt {ctx.state.attempt})",
                 dist_dev_dir(ctx)], f"svn rm {dist_dev_dir(ctx)}", where=svn_where(ctx), outward=True)
        removed.append(dist_dev_dir(ctx))
    if not removed:
        ctx.note("this attempt had no tag and nothing in dist/dev; only the attempt number changes")
    if ctx.dry_run:
        return ""
    ctx.state.reset_for_reroll(keep=("preflight",))
    return f"removed {', '.join(removed) or 'nothing'}; now at the {ordinal(ctx.state.attempt)} attempt"


# ----------------------------------------------------------------- finishing
@step("finish-tag", "Tag the release (<prefix><version>), push it, and delete the tentative tag", outward=True,
      replicates="CF:143-151 SF:147-153 AF:144-152")
def finish_tag(ctx: ReleaseContext) -> str:
    src = Path(ctx.state.data.get("source") or source_dir(ctx))
    final, sha = ctx.project.final_tag(ctx.version), ctx.state.data["sha1"]
    target = push_target(ctx)
    message = f"{ctx.project.display} {ctx.version} release"
    if ctx.endpoints.rehearsal:
        bare = Path(ctx.endpoints.git_url.removeprefix("file://"))
        ctx.run(["git", "tag", "-a", final, "-m", message, sha], f"create {final} in the sandbox", cwd=bare, outward=True)
        ctx.run(["git", "tag", "-d", ctx.tag], f"delete {ctx.tag} in the sandbox", cwd=bare, outward=True)
        return f"{final} -> {sha} (sandbox)"
    ctx.run(["git", "fetch", "-q", target, f"refs/tags/{ctx.tag}:refs/tags/{ctx.tag}"],
            f"fetch {ctx.tag}", cwd=src, always=True)
    ctx.run(["git", "tag", "-a", final, "-m", message, sha], f"create the annotated tag {final}", cwd=src, outward=True)
    ctx.run(["git", "push", target, f"refs/tags/{final}"], f"push {final}", cwd=src, outward=True)
    ctx.run(["git", "push", target, f":refs/tags/{ctx.tag}"], f"delete {ctx.tag} from {target}", cwd=src, outward=True)
    ctx.run(["git", "tag", "-d", ctx.tag], f"delete the local tag {ctx.tag}", cwd=src, outward=True)
    return f"{final} -> {sha}"


@step("finish-dist", "svn mv the release from dist/dev to dist/release", outward=True,
      replicates="CF:157-161 SF:159-163 AF:158-162",
      you_do="only PMC members can commit to dist/release; if you aren't one, send the command above to a PMC member")
def finish_dist(ctx: ReleaseContext) -> str:
    out = ctx.run(["svn", "mv", "-m", f"Release {ctx.project.display} {ctx.version}", dist_dev_dir(ctx),
                   dist_release_parent(ctx) + "/"], "svn mv dist/dev -> dist/release", where=svn_where(ctx),
                  outward=True, capture=True)
    if ctx.dry_run:
        return ""
    rev = re.search(r"Committed revision (\d+)", out or "")
    ctx.state.data.setdefault("svn_revisions", {})["release"] = rev.group(1) if rev else "?"
    return f"{dist_release_parent(ctx)}/{ctx.version} (r{rev.group(1) if rev else '?'})"


@step("nexus-release", "Check that the released staging repositories reached Maven Central", outward=False,
      repeatable=True, replicates="CF:276 SF:280 AF:202 ('Release the staging repository')",
      you_do="before this step, Release the staging repository/repositories in the Nexus UI")
def nexus_release(ctx: ReleaseContext) -> str:
    from types import SimpleNamespace

    from ...checks.maven import _discover  # the same artifact discovery as validate-release --released

    probe = SimpleNamespace(project=ctx.project, version=ctx.version, jobs=8)
    if ctx.endpoints.rehearsal:
        # what Release does: every staging repository merged into one Maven Central stand-in
        www = ctx.endpoints.sandbox / "www"
        central = www / "central"
        shutil.rmtree(central, ignore_errors=True)  # host-only: the container never reads it
        repos = ctx.state.data.get("staging_repos", [])
        for repo in repos:
            shutil.copytree(www / "repositories" / f"{STAGING_PREFIX}{repo}", central, dirs_exist_ok=True)
        with sandbox.serve(www) as url:
            found = _discover(probe, f"{url}central/")
        if not found:
            raise RuntimeError(f"the merged stand-in for Maven Central has no {ctx.project.display} {ctx.version} artifacts")
        return f"rehearsal: {', '.join(f'{STAGING_PREFIX}{r}' for r in repos)} released into the sandbox's Maven Central " \
               f"stand-in ({len(found)} artifacts found)"
    found = _discover(probe, "https://repo1.maven.org/maven2/")
    if not found:
        raise RuntimeError("not on Maven Central yet (it can take a few hours after Release); run this step again later")
    return f"{len(found)} artifacts on Maven Central"


@step("verify-published", "validate-release --released on the published release", outward=False, repeatable=True,
      replicates="CF:277 ('wait for the artifacts to sync at downloads.apache.org')")
def verify_published(ctx: ReleaseContext) -> str:
    report = ctx.endpoints.root / "published.md"
    cmd = [sys.executable, str(REPO / "validate-release"), "--project", ctx.project.key, "--version", ctx.version,
           "--no-color", "--report", str(report), "--workdir", str(ctx.endpoints.root / "validate-cache"),
           "--sha1", ctx.state.data.get("sha1", "")]
    if ctx.endpoints.rehearsal:
        www = ctx.endpoints.sandbox / "www"
        rel = f"dist-release/{ctx.project.dist_subdir}{ctx.version}"
        sandbox.export_dist(ctx, f"{dist_release_parent(ctx)}/{ctx.version}", www / rel)
        with sandbox.serve(www) as url:
            # --released against the stand-ins: dist/release (no packages: finish-packages moved them) and the
            # Maven Central that nexus-release merged; the public package repositories have no stand-in
            cmd += ["--released", "--dist-url", f"{url}{rel}/", "--tag", ctx.project.final_tag(ctx.version),
                    "--git-url", ctx.endpoints.git_url, "--keys", ctx.endpoints.keys, "--skip", "packages",
                    "--maven-base", f"{url}central/"]
            return _run_validate(ctx, cmd, report)
    return _run_validate(ctx, cmd + ["--released"], report)


@step("post-release", "Write the [RELEASE] mail draft and the remaining checklist", outward=False, repeatable=True,
      replicates="CF:246-287 SF:245-288 AF:170-209 (mail and checklist)",
      you_do="send the [RELEASE] mail after downloads.apache.org has synced, and work through the checklist")
def post_release(ctx: ReleaseContext) -> str:
    ctx.endpoints.mail_dir.mkdir(parents=True, exist_ok=True)
    release = ctx.endpoints.mail_dir / f"mail_release_{ctx.version}"
    release.write_text(mail.release_mail(ctx.project, ctx.version))
    checklist = ctx.endpoints.root / "post-release.md"
    items = POST_RELEASE[ctx.project.key]
    checklist.write_text(f"# {ctx.project.display} {ctx.version}: after the release\n\n"
                         + "\n".join(f"- [ ] {i}" for i in items) + "\n")
    for item in items:
        print(f"   [ ] {item}")
    return f"{release}; checklist: {checklist}"


POST_RELEASE = {
    "cassandra": [
        "Wait for the artifacts at https://downloads.apache.org/cassandra/",
        "Update cassandra-website download.adoc (and the CQL doc if appropriate)",
        "Send the [RELEASE] mail to user@ and dev@",
        "Update the #cassandra Slack topic",
        "Release the version in JIRA",
        "svn rm the previous release of the branch from dist/release",
        "Increment base.version in build.xml, CHANGES.txt (move unreleased entries), debian/changelog",
        "Add the release at https://reporter.apache.org/addrelease.html?cassandra",
        "Update current_ versions in cassandra-dtest/upgrade_tests/upgrade_manifest.py",
    ],
    "sidecar": [
        "Wait for the artifacts at https://downloads.apache.org/cassandra/cassandra-sidecar/",
        "Update the website download page",
        "Send the [RELEASE] mail to user@ and dev@",
        "Release the version in JIRA (CASSSIDECAR)",
        "svn rm the previous release from dist/release/cassandra/cassandra-sidecar",
        "Increment version in gradle.properties and CHANGES.txt",
        "Add the release at https://reporter.apache.org/addrelease.html?cassandra",
    ],
    "analytics": [
        "Wait for the artifacts at https://downloads.apache.org/cassandra/cassandra-analytics/",
        "Update the website download page",
        "Send the [RELEASE] mail to user@ and dev@",
        "Release the version in JIRA (CASSANALYTICS)",
        "svn rm the previous release from dist/release/cassandra/cassandra-analytics",
        "Increment version in gradle.properties and CHANGES.txt",
        "Add the release at https://reporter.apache.org/addrelease.html?cassandra",
    ],
}


# ------------------------------------------------------------- check-upstream
PARITY = REPO / "docs" / "parity.md"
UPSTREAM = "https://raw.githubusercontent.com/apache/cassandra-builds/trunk/"


def _reviewed() -> dict[str, str]:
    """path -> sha256 of the official scripts the replica was reviewed against (table in docs/parity.md)."""
    if not PARITY.exists():
        return {}
    rows = re.findall(r"^\|\s*`([^`]+\.sh)`\s*\|\s*`([0-9a-f]{64})`\s*\|", PARITY.read_text(), re.M)
    return dict(rows)


def _upstream_drift() -> list[str] | None:
    import hashlib
    reviewed = _reviewed()
    if not reviewed:
        return None
    changed = []
    try:
        for path, digest in reviewed.items():
            if hashlib.sha256(net.fetch_bytes(UPSTREAM + path)).hexdigest() != digest:
                changed.append(path)
    except net.HttpError:
        return None
    return changed


@step("check-upstream", "Compare the official release scripts with the versions the replica was reviewed against",
      outward=False, repeatable=True)
def check_upstream(ctx: ReleaseContext) -> str:
    drift = _upstream_drift()
    if drift is None:
        raise RuntimeError(f"could not compare (no table in {PARITY}, or GitHub unreachable)")
    for path in drift:
        print(f"   changed upstream: {UPSTREAM}{path}")
    return f"{len(drift)} of {len(_reviewed())} scripts changed since review" if drift else "all scripts unchanged since review"

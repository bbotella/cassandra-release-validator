"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from . import net, vote_email
from .checks import binary, dist, git, maven, packages, smoke, source
from .context import Context
from .gpg import KEYS_URL, Keyring
from .projects import PROJECTS, VERSION_RE, by_name
from .report import Report

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKDIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassandra-release-validator"
CHECK_GROUPS = ("git", "dist", "packages", "source", "binary", "maven")

EXAMPLES = """\
examples:
  # list the release votes currently on dev@cassandra.apache.org
  validate-release --list

  # validate the release proposed in a vote thread (URL or id from lists.apache.org)
  validate-release https://lists.apache.org/thread/hknnwz6bj83tbpbwj1volyb6bsgvxztv

  # validate the newest vote, and also start the binary tarball
  validate-release --latest --smoke

  # validate a vote email saved from your mail client (or pasted on stdin with '-')
  validate-release ~/Downloads/vote.eml

  # validate a candidate before the vote email exists (e.g. as the release manager)
  validate-release --project analytics --version 0.5.0 --staging-repo 1484 --staging-repo 1485
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="validate-release",
        description="Validates Apache Cassandra, Cassandra Sidecar and Cassandra Analytics release candidates.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "source",
        nargs="?",
        help="the vote email: a lists.apache.org thread URL or id, a saved .eml/text file, or '-' for stdin",
    )
    p.add_argument("--list", action="store_true", help="list recent [VOTE] Release threads on dev@cassandra and exit")
    p.add_argument("--thread-votes", metavar="THREAD", help="show the replies and votes cast in a vote thread (URL or id) and exit")
    p.add_argument("--latest", action="store_true", help="validate the most recent [VOTE] Release thread")
    p.add_argument("--days", type=int, default=30, help="how far back --list/--latest look (default: 30)")

    m = p.add_argument_group("manual mode and overrides (take precedence over the vote email)")
    m.add_argument("--project", choices=sorted(PROJECTS), help="project of the release")
    m.add_argument("--version", help="version under vote, e.g. 5.0.9 or 6.0-alpha2")
    m.add_argument("--sha1", help="git commit the release must be built from")
    m.add_argument("--tag", help="git tag to check (default: <version>-tentative)")
    m.add_argument("--staging-repo", action="append", default=[], metavar="N", help="Nexus staging repository number, e.g. 1484 (repeatable)")
    m.add_argument("--dist-url", help="dist directory (default: dist/dev for the project and version)")
    m.add_argument("--maven-base", metavar="URL", help="check the email's Maven artifacts in this repository instead of Nexus staging, e.g. https://repo1.maven.org/maven2/")

    c = p.add_argument_group("what to check")
    c.add_argument("--skip", action="append", default=[], choices=CHECK_GROUPS, help="skip a group of checks (repeatable)")
    c.add_argument("--smoke", action="store_true", help="start the binary tarball (Cassandra/Sidecar) and check it comes up")
    c.add_argument("--build", action="store_true", help="build from the source tarball (Cassandra: ant, Sidecar: gradlew)")
    c.add_argument("--java-home", help="JDK for --smoke and --build (default: $JAVA_HOME)")
    c.add_argument("--smoke-timeout", type=int, default=300, help="seconds to wait for startup (default: 300)")

    o = p.add_argument_group("output")
    o.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR, help=f"download/cache directory (default: {DEFAULT_WORKDIR})")
    o.add_argument("--report", type=Path, help="markdown report path (default: reports/<project>-<version>.md)")
    o.add_argument("--jobs", type=int, default=8, help="parallel downloads (default: 8)")
    o.add_argument("--refresh", action="store_true", help="re-download files that are already cached")
    o.add_argument("-v", "--verbose", action="store_true", help="show progress and every item of each check")
    o.add_argument("--no-color", action="store_true", help="disable colored output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        return _list_votes(args)
    if args.thread_votes:
        return _thread_votes(args.thread_votes)

    try:
        info = _load_vote(args)
    except (net.HttpError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if info is None:
        build_parser().print_help()
        return 2

    _apply_overrides(info, args)
    if info.project is None or info.version is None:
        print("error: could not determine the project and version; use --project and --version", file=sys.stderr)
        return 2
    if not VERSION_RE.match(info.version):
        print(f"error: '{info.version}' does not look like a release version", file=sys.stderr)
        return 2

    report = Report(verbose=args.verbose, color=False if args.no_color else None)
    project, version = info.project, info.version
    workdir = (args.workdir / f"{project.key}-{version}").resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    title = f"{project.display} {version} release candidate"
    print(report._c(f"Validating {title}", "\033[1m"))
    print(f"  work directory: {workdir}")

    _email_checks(report, info, args)

    report.section("Signing keys")
    keys_file = workdir / "KEYS"
    try:
        net.download(KEYS_URL, keys_file, refresh=True)
        keyring = Keyring(keys_file)
    except Exception as e:  # noqa: BLE001
        report.fail("KEYS", f"cannot set up the keyring: {e}")
        report.print_summary()
        return 1
    report.ok("KEYS imported into an isolated keyring", f"{keyring.imported} keys from {KEYS_URL}")

    ctx = Context(
        project=project,
        version=version,
        sha1=info.sha1,
        tag=args.tag or info.tag or project.rc_tag(version),
        dist_url=args.dist_url or info.dist_url or project.dist_url(version),
        staging_repos=info.staging_repos,
        maven_refs=info.maven,
        workdir=workdir,
        report=report,
        keyring=keyring,
        args=args,
    )
    if not ctx.dist_url.endswith("/"):
        ctx.dist_url += "/"

    started = time.time()
    try:
        steps = [
            ("git", git.run),
            ("dist", dist.run),
            ("packages", packages.run),
            ("source", source.run),
            ("binary", binary.run),
            ("maven", maven.run),
        ]
        for group, step in steps:
            if group in args.skip:
                report.section(group.capitalize())
                report.skip(f"{group} checks", "skipped with --skip")
                continue
            _guarded(ctx, group, step)
        if args.smoke:
            _guarded(ctx, "smoke", smoke.run)
        _signer_summary(ctx)
    finally:
        keyring.close()

    report.print_summary()
    elapsed = time.time() - started
    path = args.report or REPO_ROOT / "reports" / f"{project.key}-{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [
        f"Vote email: {info.source}" if info.source else "Vote email: none (manual mode)",
        f"Validated on {time.strftime('%Y-%m-%d %H:%M %Z')} in {elapsed / 60:.1f} min",
    ]
    md = report.to_markdown(f"{title}: validation report", ctx.highlights)
    md = md.replace("\n## Summary", "\n" + "\n".join(f"_{m}_  " for m in meta) + "\n\n## Summary", 1)
    path.write_text(md)
    print(f"\n  Markdown report: {path}")
    return 1 if report.failed else 0


def _guarded(ctx: Context, group: str, step) -> None:
    try:
        step(ctx)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        ctx.report.fail(f"{group} checks crashed", f"{type(e).__name__}: {e}",
                        traceback.format_exc().splitlines()[-8:] if ctx.args.verbose else [])


# ------------------------------------------------------------------- inputs
def _list_votes(args) -> int:
    threads = vote_email.list_votes(args.days)
    if not threads:
        print(f"No [VOTE] Release threads on dev@cassandra.apache.org in the last {args.days} days.")
        return 0
    print(f"[VOTE] Release threads on dev@cassandra.apache.org, last {args.days} days:\n")
    for t in threads:
        parsed = vote_email.parse("", t.subject)
        supported = "" if parsed.project else "  (not supported by this tool)"
        status = f"  -> {t.result}" if t.result else ""
        print(f"  {t.date}  {t.subject}{supported}")
        print(f"              https://lists.apache.org/thread/{t.id}  ({t.replies} replies){status}")
    return 0


def _thread_votes(source: str) -> int:
    tid = vote_email.thread_id_from(source)
    if not tid:
        print(f"error: '{source}' is not a lists.apache.org thread URL or id", file=sys.stderr)
        return 2
    subject, replies = vote_email.fetch_thread(tid)
    print(f"{subject}\nhttps://lists.apache.org/thread/{tid} — {len(replies)} replies\n")
    for reply in replies:
        cast = reply.vote or "no vote"
        if reply.binding:
            cast += f" ({reply.binding})"
        print(f"  {reply.date}  {reply.sender}  [{cast}]")
        for line in reply.text.splitlines()[:12]:
            if line.strip():
                print(f"      {line}")
        if len(reply.text.splitlines()) > 12:
            print("      ...")
        print()
    print("Tally (last vote per sender counts; binding = PMC, which the list cannot tell reliably):")
    last: dict[str, vote_email.Reply] = {}
    for reply in replies:
        if reply.vote:
            last[reply.sender] = reply
    for vote in ("+1", "+0", "-0", "-1"):
        voters = [f"{s}{' (' + r.binding + ')' if r.binding else ''}" for s, r in last.items() if r.vote == vote]
        if voters:
            print(f"  {vote}: {len(voters)} — " + "; ".join(voters))
    if not last:
        print("  no votes cast yet")
    return 0


def _load_vote(args) -> vote_email.VoteInfo | None:
    if args.latest:
        for t in vote_email.list_votes(args.days):
            if vote_email.parse("", t.subject).project:
                return _from_lists(t.id)
        raise ValueError(f"no supported [VOTE] Release thread in the last {args.days} days")
    if args.source:
        tid = vote_email.thread_id_from(args.source)
        if tid:
            return _from_lists(tid)
        if args.source == "-" or Path(args.source).is_file():
            subject, body, sender, date = vote_email.read_file(args.source)
            info = vote_email.parse(body, subject)
            info.sender, info.date = sender, date
            info.source = "stdin" if args.source == "-" else str(Path(args.source).resolve())
            return info
        raise ValueError(f"'{args.source}' is neither a lists.apache.org thread nor a file")
    if args.project or args.version:
        return vote_email.VoteInfo()
    return None


def _from_lists(tid: str) -> vote_email.VoteInfo:
    subject, body, sender, date = vote_email.fetch_from_lists(tid)
    info = vote_email.parse(body, subject)
    info.sender, info.date = sender, date
    info.source = f"https://lists.apache.org/thread/{tid}"
    return info


def _apply_overrides(info: vote_email.VoteInfo, args) -> None:
    if args.project:
        info.project = by_name(args.project)
    if args.version:
        info.version = args.version
    if args.sha1:
        info.sha1 = args.sha1.lower()
    for repo in args.staging_repo:
        repo = repo.removeprefix("orgapachecassandra-")
        if repo not in info.staging_repos:
            info.staging_repos.append(repo)


def _email_checks(r: Report, info: vote_email.VoteInfo, args) -> None:
    r.section("Vote email")
    p, v = info.project, info.version
    if not info.source:
        r.info("No vote email", "manual mode: only what is given on the command line is checked against")
        return
    r.info("Vote email", f"{info.subject} — {info.sender} {info.date}".strip(" —"))
    r.note(f"source: {info.source}")
    for problem in info.problems:
        r.fail("Vote email is consistent", problem)

    (r.ok if info.sha1 else r.fail)("sha1 stated", info.sha1 or "no 'sha1: <40 hex chars>' line found")
    expected_tag = p.rc_tag(v)
    if not info.tag:
        r.warn("Git tag link", f"not found, assuming {expected_tag}")
    elif info.tag != expected_tag:
        r.warn("Git tag link", f"links to '{info.tag}' (release scripts use '{expected_tag}')")
    else:
        r.ok("Git tag link", info.tag)
    if info.git_repo and info.git_repo != p.repo:
        r.fail("Git repository", f"{info.git_repo} (expected {p.repo})")

    expected_dist = p.dist_url(v)
    if not info.dist_url:
        r.warn("Dist URL", f"not found, assuming {expected_dist}")
    elif info.dist_url.rstrip("/") != expected_dist.rstrip("/"):
        r.warn("Dist URL", f"{info.dist_url} (expected {expected_dist})")
    else:
        r.ok("Dist URL", info.dist_url)

    if info.staging_repos:
        r.ok("Maven staging repositories", ", ".join(f"orgapachecassandra-{s}" for s in info.staging_repos))
    else:
        r.fail("Maven staging repositories", "no repository.apache.org staging URL found")
    if info.maven:
        accepted = {v, f"{v}-jdk8"}
        wrong = [f"{m.artifact}/{m.version}" for m in info.maven if m.version not in accepted]
        if wrong:
            r.fail("Maven artifact versions in the email", "", wrong)
        else:
            r.ok("Maven artifacts listed", f"{len(info.maven)} artifacts, all version {v}")


def _signer_summary(ctx: Context) -> None:
    r = ctx.report
    r.section("Signer")
    if not ctx.signers:
        r.warn("Release signer", "no valid signatures were checked")
        return
    total = sum(ctx.signers.values())
    if len(ctx.signers) == 1:
        signer = next(iter(ctx.signers))
        r.ok("All artifacts signed by the same key", f"{total} signatures by {signer}")
    else:
        r.warn("All artifacts signed by the same key", f"{len(ctx.signers)} different keys",
               [f"{s}: {n} signatures" for s, n in ctx.signers.most_common()])
    for signer, (primary, signing) in ctx.signer_fingerprints.items():
        key = ctx.keyring.key_info(primary)
        algo = ctx.keyring.signing_key_bits(signing) or ((key.algorithm, key.bits) if key else None)
        if algo:
            name, bits = algo
            desc = f"{name} {bits} bit, fingerprint {primary}"
            if name == "RSA" and bits < 4096:
                r.warn(f"Key strength ({signer})", desc + " (the release process asks for a 4096 bit RSA key)")
            else:
                r.ok(f"Key strength ({signer})", desc)
        ctx.highlight(f"Signed by {signer} (fingerprint {primary}), key present in KEYS")

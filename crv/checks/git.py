"""Checks against the git repository: the tag, the sha1 from the vote email, and version metadata."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from ..context import Context
from ..projects import deb_version

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


def git(*args: str, cwd: Path | None = None, check: bool = True, timeout: int = 900) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_GIT_ENV, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def ls_remote(url: str, *refs: str) -> dict[str, str]:
    out = git("ls-remote", url, *refs, timeout=120)
    result = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        result[ref] = sha
    return result


def resolve_tag(url: str, tag: str) -> str | None:
    refs = ls_remote(url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def fetch_commit(git_dir: Path, url: str, ref: str, expected: str | None = None) -> str:
    """Shallow-fetches a single ref into a local bare repository and returns its commit id.

    When the expected commit is already in the repository (an earlier run fetched it), nothing is fetched.
    """
    if not (git_dir / "HEAD").exists():
        git_dir.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "--quiet", str(git_dir))
    if expected and subprocess.run(
        ["git", "cat-file", "-e", f"{expected}^{{commit}}"], cwd=git_dir, capture_output=True, env=_GIT_ENV
    ).returncode == 0:
        return expected
    for attempt in range(3):
        try:
            git("fetch", "--quiet", "--depth", "1", "--no-tags", url, f"{ref}:refs/crv/{ref.split('/')[-1]}",
                cwd=git_dir, timeout=1800)
            break
        except RuntimeError as e:  # another run holding git's shallow/ref lock on the same cache
            if ".lock" not in str(e) or attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return git("rev-parse", f"refs/crv/{ref.split('/')[-1]}^{{commit}}", cwd=git_dir).strip()


def show_file(ctx: Context, path: str) -> str | None:
    if not ctx.git_dir or not ctx.git_commit:
        return None
    proc = subprocess.run(
        ["git", "show", f"{ctx.git_commit}:{path}"], cwd=ctx.git_dir, capture_output=True, env=_GIT_ENV
    )
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None


def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Git")

    fetch_url = p.git_url
    try:
        tag_commit = resolve_tag(p.git_url, ctx.tag)
    except Exception as e:  # noqa: BLE001
        r.fail(f"Resolve tag {ctx.tag}", str(e))
        tag_commit = None
    else:
        if tag_commit:
            r.ok(f"Tag {ctx.tag} exists", f"{p.git_url} -> {tag_commit}")
        else:
            # gitbox is the ASF's canonical repository; GitHub is a (normally two-way synced) mirror
            github = f"{p.github_url}.git"
            try:
                tag_commit = resolve_tag(github, ctx.tag)
            except Exception:  # noqa: BLE001
                tag_commit = None
            if tag_commit:
                r.warn(f"Tag {ctx.tag} exists", f"only on GitHub ({tag_commit}), not on gitbox ({p.git_url}): "
                       "pushed to GitHub only, or not synced yet")
                fetch_url = github
            else:
                r.fail(f"Tag {ctx.tag} exists", f"not found in {p.git_url} nor on GitHub")

    if ctx.sha1 and tag_commit:
        if ctx.sha1 == tag_commit:
            r.ok("sha1 from the vote email matches the tag", ctx.sha1)
            ctx.highlight(f"Git: `{ctx.tag}` points at {ctx.sha1}, as stated in the vote email")
        else:
            r.fail("sha1 from the vote email matches the tag", f"email says {ctx.sha1}, tag points at {tag_commit}")
    elif not ctx.sha1:
        r.warn("sha1 from the vote email matches the tag", "no sha1 given (use --sha1 or pass the vote email)")
        if tag_commit:
            ctx.highlight(f"Git: `{ctx.tag}` resolves to {tag_commit}")

    final_tag = p.final_tag(ctx.version)
    if ctx.tag != final_tag:
        try:
            final_commit = resolve_tag(p.git_url, final_tag)
        except Exception:  # noqa: BLE001
            final_commit = None
        if final_commit:
            r.warn(f"Final tag {final_tag} does not exist yet", f"it already exists ({final_commit}): is {ctx.version} already released?")
        else:
            r.ok(f"Final tag {final_tag} does not exist yet")

    ref = f"refs/tags/{ctx.tag}" if tag_commit else ctx.sha1
    if not ref:
        r.skip("Fetch release commit", "no tag and no sha1 to fetch")
        return
    git_dir = ctx.workdir / "git" / f"{p.repo}.git"
    try:
        ctx.git_commit = fetch_commit(git_dir, fetch_url, ref, expected=tag_commit or ctx.sha1)
        ctx.git_dir = git_dir
        r.log(f"fetched {ref} -> {ctx.git_commit} into {git_dir}")
    except Exception as e:  # noqa: BLE001
        r.fail("Fetch release commit", str(e))
        return

    _check_version_file(ctx)
    _check_changes(ctx)
    if p.key == "cassandra":
        _check_news(ctx)
        _check_debian_changelog(ctx)


def _check_version_file(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    if not p.version_file:
        r.info("Version file", f"{p.display} has none: the version comes from the tag")
        return
    content = show_file(ctx, p.version_file)
    if content is None:
        r.fail(f"Version in {p.version_file}", "file not found at the tag")
        return
    if p.version_pattern == "pom":
        found = _pom_version(content)
    else:
        m = re.search(p.version_pattern, content)
        found = m.group(1) if m else None
    if not found:
        r.fail(f"Version in {p.version_file}", "could not find the version")
        return
    if found == ctx.version:
        r.ok(f"Version in {p.version_file}", found)
    else:
        r.fail(f"Version in {p.version_file}", f"{found} (expected {ctx.version}; the release build must use the same version)")


def _pom_version(content: str) -> str | None:
    """The project's own <version> in a POM (not the one inside <parent>)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    version = root.find("m:version", ns)
    if version is None:
        version = root.find("version")
    return version.text.strip() if version is not None and version.text else None


def _check_changes(ctx: Context) -> None:
    r = ctx.report
    name = ctx.project.changes_file
    content = show_file(ctx, name)
    if content is None:
        r.warn(name, "not found at the tag")
        return
    heading = ctx.project.changes_heading_re(ctx.version)
    head = [line.strip() for line in content.splitlines()[:30]]
    check = f"{name} has a section for this version"
    if any(heading.match(line) for line in head):
        r.ok(check, f"'{ctx.version}' heading found at the top")
    elif any(ctx.version in line for line in head):
        r.warn(check, f"'{ctx.version}' mentioned near the top but not as a heading")
    else:
        first = next((line for line in head if line), "")
        r.fail(check, f"top section is '{first}'")


def _check_news(ctx: Context) -> None:
    r = ctx.report
    content = show_file(ctx, "NEWS.txt")
    if content is None:
        r.warn("NEWS.txt", "not found at the tag")
        return
    base = ctx.version.split("-")[0]
    if re.search(rf"(?m)^{re.escape(ctx.version)}\s*$", content) or re.search(rf"(?m)^{re.escape(base)}\s*$", content):
        r.ok("NEWS.txt mentions this version")
    else:
        r.warn("NEWS.txt mentions this version", f"no '{ctx.version}' heading (fine for releases with no upgrade notes)")


def _check_debian_changelog(ctx: Context) -> None:
    r = ctx.report
    content = show_file(ctx, "debian/changelog")
    if content is None:
        r.warn("debian/changelog", "not found at the tag")
        return
    first = content.splitlines()[0] if content else ""
    m = re.match(r"^cassandra \(([^)]+)\)", first)
    expected = deb_version(ctx.version)
    if m and m.group(1) == expected:
        r.ok("debian/changelog top entry", first.strip())
    else:
        r.fail("debian/changelog top entry", f"'{first.strip()}' (expected version {expected})")

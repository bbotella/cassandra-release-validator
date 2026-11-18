"""Checks of the source release: it must match the git tag and follow ASF source release rules."""

from __future__ import annotations

import datetime
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

from .. import archive, checksums
from ..context import Context
from .git import fetch_commit, git, show_file

COMPILED_EXTS = (".jar", ".class", ".so", ".dylib", ".dll", ".exe", ".pyc", ".pyo", ".o", ".a", ".war", ".ear", ".jnilib")
HEADER_EXTS = (".java", ".scala", ".kt", ".kts", ".groovy", ".gradle", ".py", ".sh", ".js", ".ts", ".g", ".proto", ".go")
# Snippets embedded in the documentation are excluded from RAT by the projects, so skip them here too.
HEADER_EXCLUDED = re.compile(r"^(doc|docs)/(.*/)?examples/")
_LICENSE_MARKERS = ("Licensed to the Apache Software Foundation", "Apache License", "SPDX-License-Identifier: Apache-2.0")


def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Source release")
    name = p.src_tarball(ctx.version)
    tarball = ctx.dist_files.get(name)
    if tarball is None:
        r.skip("Source tarball checks", f"{name} was not downloaded")
        return
    dest = ctx.workdir / "extract" / ("src-" + checksums.digests(tarball, ["sha256"])["sha256"][:16])
    try:
        root = archive.extract(tarball, dest)
    except Exception as e:  # noqa: BLE001
        r.fail("Extract source tarball", str(e))
        return
    ctx.src_root = root

    expected_top = f"{p.tarball_prefix}-{ctx.version}-src"
    if root == dest:
        r.warn("Single top-level directory", f"found {archive.top_level_entries(dest)}")
    elif root.name != expected_top:
        r.warn("Top-level directory name", f"{root.name} (expected {expected_top})")
    else:
        r.ok("Top-level directory name", root.name)

    check_license_notice(ctx, root, "source")
    _compare_with_git(ctx, root)
    _compiled_code(ctx, root)
    _license_headers(ctx, root)
    if ctx.args.build:
        _build(ctx, root)


def check_license_notice(ctx: Context, root: Path, what: str) -> None:
    r = ctx.report
    lic = next((root / n for n in ("LICENSE.txt", "LICENSE") if (root / n).is_file()), None)
    notice = next((root / n for n in ("NOTICE.txt", "NOTICE") if (root / n).is_file()), None)
    if lic and "Apache License" in lic.read_text(errors="replace"):
        r.ok(f"LICENSE in {what} release", lic.name)
    else:
        r.fail(f"LICENSE in {what} release", "missing or not the Apache License")
    if not notice:
        r.fail(f"NOTICE in {what} release", "missing")
        return
    text = notice.read_text(errors="replace")
    year = str(datetime.date.today().year)
    m = re.search(r"Copyright[^\n]*", text)
    line = m.group(0).strip() if m else ""
    if "Apache Software Foundation" not in text:
        r.warn(f"NOTICE in {what} release", "does not mention The Apache Software Foundation")
    elif year in line:
        r.ok(f"NOTICE in {what} release", f"{notice.name}: '{line}'")
    elif re.search(r"\b\d{4}\s*-(?!\s*\d)", line):
        r.ok(f"NOTICE in {what} release", f"{notice.name}: '{line}' (open-ended range)")
    else:
        r.warn(f"NOTICE in {what} release", f"copyright year is not {year}: '{line}'")


# ------------------------------------------------------------------ git diff
def _git_tree(ctx: Context, git_dir: Path, commit: str, prefix: str = "") -> tuple[dict[str, tuple[str, str, Path]], dict[str, str]]:
    """Returns ({path: (mode, blob, git_dir)}, {submodule path: commit}) for a commit."""
    out = git("ls-tree", "-r", "-z", "--full-tree", commit, cwd=git_dir)
    files, submodules = {}, {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, typ, sha = meta.split()
        if typ == "commit":
            submodules[prefix + path] = sha
        else:
            files[prefix + path] = (mode, sha, git_dir)
    return files, submodules


def _symlink_target_blob(tree: dict[str, tuple[str, str, Path]], rel: str, depth: int = 0) -> str | None:
    """For a symlink in git, the blob id of the file it points to (following chains), if inside the tree."""
    mode, sha, git_dir = tree[rel]
    if mode != "120000":
        return sha
    if depth > 10:
        return None
    target = git("cat-file", "-p", sha, cwd=git_dir)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if resolved not in tree:
        return None
    return _symlink_target_blob(tree, resolved, depth + 1)


def _submodule_urls(ctx: Context) -> dict[str, str]:
    content = show_file(ctx, ".gitmodules") or ""
    urls, current = {}, None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("path"):
            current = line.split("=", 1)[1].strip()
        elif line.startswith("url") and current:
            urls[current] = line.split("=", 1)[1].strip()
    return urls


def _compare_with_git(ctx: Context, root: Path) -> None:
    r = ctx.report
    if not ctx.git_dir or not ctx.git_commit:
        r.skip("Source tarball matches the git tag", "git commit not available")
        return
    tree, submodules = _git_tree(ctx, ctx.git_dir, ctx.git_commit)
    unverifiable: list[str] = []
    urls = _submodule_urls(ctx)
    for path, commit in submodules.items():
        url = urls.get(path)
        try:
            if not url:
                raise RuntimeError("no url in .gitmodules")
            sub_dir = ctx.workdir / "git" / ("submodule-" + path.replace("/", "_") + ".git")
            fetched = fetch_commit(sub_dir, url, commit, expected=commit)
            sub_files, _ = _git_tree(ctx, sub_dir, fetched, prefix=path + "/")
            tree.update(sub_files)
            r.log(f"submodule {path} @ {commit} fetched from {url}")
        except Exception as e:  # noqa: BLE001
            unverifiable.append(path + "/")
            r.warn(f"Submodule {path}", f"could not fetch {commit} from {url}: {e}")

    matched, differ, extra, dereferenced = 0, [], [], []
    seen = set()
    for rel, path in archive.iter_files(root):
        seen.add(rel)
        if rel not in tree:
            if not any(rel.startswith(u) for u in unverifiable):
                extra.append(rel)
            continue
        mode, blob, _ = tree[rel]
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        actual = checksums.git_blob_sha1(data)
        if actual == blob:
            matched += 1
        elif mode == "120000" and not path.is_symlink() and _symlink_target_blob(tree, rel) == actual:
            # a symlink in git that the build stored as a copy of its target (ant's tar task does this)
            matched += 1
            dereferenced.append(rel)
        else:
            differ.append(rel)
    missing = sorted(p for p in tree if p not in seen)

    if differ:
        r.fail("Source files identical to the git tag", f"{len(differ)} files differ ({matched} identical)", differ)
    else:
        r.ok("Source files identical to the git tag", f"{matched} files identical to {ctx.git_commit[:12]}")
    if dereferenced:
        r.info("Symlinks stored as files", f"{len(dereferenced)} git symlinks are regular files in the tarball, with the target's content", dereferenced)
    if extra:
        compiled = [e for e in extra if e.endswith(COMPILED_EXTS)]
        (r.fail if compiled else r.warn)("No files in the source tarball that are not in git", f"{len(extra)} extra files", extra)
    else:
        r.ok("No files in the source tarball that are not in git")
    if missing:
        top = sorted({m.split("/")[0] + ("/" if "/" in m else "") for m in missing})
        r.info("Files in git but not in the source tarball", f"{len(missing)} files (usually intentional excludes): {', '.join(top[:12])}", missing if ctx.args.verbose else [])
    if not differ:
        note = f" ({len(extra)} extra file(s) not in git: {', '.join(extra[:3])}{', ...' if len(extra) > 3 else ''})" if extra else ""
        ctx.highlight(f"Source: all {matched} files of the source tarball that are tracked in git are identical to the tag{note}")


def _compiled_code(ctx: Context, root: Path) -> None:
    r = ctx.report
    found = [rel for rel, _ in archive.iter_files(root) if rel.endswith(COMPILED_EXTS)]
    if not found:
        r.ok("No compiled code in the source release")
        return
    wrapper = [f for f in found if f.endswith("gradle/wrapper/gradle-wrapper.jar")]
    others = [f for f in found if f not in wrapper]
    if others:
        r.warn("No compiled code in the source release", f"{len(others)} compiled files (ASF source releases must not contain compiled code)", others)
    if wrapper:
        r.info("Gradle wrapper jar in the source release", ", ".join(wrapper))


def _license_headers(ctx: Context, root: Path) -> None:
    """A light-weight approximation of Apache RAT for source code files."""
    r = ctx.report
    missing, scanned = [], 0
    for rel, path in archive.iter_files(root):
        if path.is_symlink() or not rel.endswith(HEADER_EXTS) or HEADER_EXCLUDED.match(rel):
            continue
        scanned += 1
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
        if not any(m in head for m in _LICENSE_MARKERS):
            missing.append(rel)
    if missing:
        r.warn("License headers in source files", f"{len(missing)} of {scanned} files lack an Apache license header", missing)
    else:
        r.ok("License headers in source files", f"{scanned} source files have an Apache license header")


def _build(ctx: Context, root: Path) -> None:
    r = ctx.report
    cmd = ctx.project.build_command
    if not cmd:
        r.skip("Build from source", f"not automated for {ctx.project.display}; see the project's README")
        return
    if not shutil.which(cmd[0]) and not (root / cmd[0]).exists():
        r.skip("Build from source", f"'{cmd[0]}' not found")
        return
    build_dir = ctx.workdir / "build" / root.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(root, build_dir, symlinks=True)
    log = ctx.workdir / "build" / f"{root.name}.log"
    env = dict(os.environ)
    if ctx.args.java_home:
        env["JAVA_HOME"] = ctx.args.java_home
        env["PATH"] = f"{ctx.args.java_home}/bin:{env['PATH']}"
    r.note(f"Building with `{' '.join(cmd)}` in {build_dir} (log: {log}) ...")
    with open(log, "w") as out:
        proc = subprocess.run(list(cmd), cwd=build_dir, stdout=out, stderr=subprocess.STDOUT, env=env)
    if proc.returncode == 0:
        r.ok("Build from source", f"`{' '.join(cmd)}` succeeded")
        ctx.highlight(f"Built from the source tarball with `{' '.join(cmd)}`")
    else:
        tail = log.read_text(errors="replace").splitlines()[-15:]
        r.fail("Build from source", f"exit code {proc.returncode}, see {log}", tail)

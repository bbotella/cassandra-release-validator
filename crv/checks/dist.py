"""Checks of the files staged on dist.apache.org/repos/dist/dev: completeness, signatures and checksums."""

from __future__ import annotations

import re
from pathlib import Path

from .. import checksums, net
from ..context import Context

PRIMARY_EXTS = (".tar.gz", ".deb", ".rpm", ".zip", ".tgz", ".jar", ".whl")
# Files reprepro / createrepo_c generate whose exact names can not be predicted
_GENERATED = re.compile(r"^(debian/dists/|debian/pool/|(?:redhat/(?:noboolean/)?)repodata/)")


def svn_revision(url: str) -> str | None:
    headers = net.head(url)
    if headers is None:
        return None
    m = re.match(r'W/"(\d+)//', headers.get("etag", ""))
    return m.group(1) if m else "unknown"


def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Dist area (source, binary and package artifacts)")

    try:
        rev = svn_revision(ctx.dist_url)
    except net.HttpError as e:
        r.fail("Dist directory exists", str(e))
        return
    if rev is None:
        r.fail("Dist directory exists", f"{ctx.dist_url} not found")
        return
    r.ok("Dist directory exists", f"{ctx.dist_url} (svn r{rev})")

    files = net.walk(ctx.dist_url)
    r.log(f"{len(files)} files listed")

    noboolean = _expects_noboolean(ctx, files)
    package_release = p.simple_package_release(ctx.version, files) if p.packages == "simple" else None
    expected = p.expected_dist_files(ctx.version, noboolean=noboolean, released=ctx.args.released,
                                     package_release=package_release)
    missing = [f for f in expected if f not in files]
    if missing:
        r.fail("All expected artifacts are staged", f"{len(missing)} missing", missing)
    else:
        r.ok("All expected artifacts are staged", f"{len(expected)} expected files present")

    wheels = [f for f in files if p.wheels_dir and f.startswith(p.wheels_dir) and f.endswith(".whl")]
    if p.wheels_dir:
        if wheels:
            r.ok("Wheels are staged", f"{len(wheels)} wheels in {p.wheels_dir}")
        else:
            r.warn("Wheels are staged", f"no .whl files in {p.wheels_dir}")

    def known(f: str) -> bool:
        if f in expected or _GENERATED.match(f):
            return True
        # wheels, and any signature/checksum next to them
        return bool(p.wheels_dir) and f.startswith(p.wheels_dir) and re.search(r"\.whl(\.asc|\.sha256|\.sha512)?$", f) is not None

    unexpected = [f for f in files if not known(f)]
    if unexpected:
        r.warn("No unexpected files in the dist directory", f"{len(unexpected)} not produced by prepare_release.sh", unexpected)
    else:
        r.ok("No unexpected files in the dist directory")

    weak = [f for f in files if f.endswith((".md5", ".sha1"))]
    if weak:
        r.warn("No MD5/SHA-1 checksums (ASF distribution policy)", "", weak)
    else:
        r.ok("No MD5/SHA-1 checksums (ASF distribution policy)")

    local_root = ctx.workdir / "dist" / f"r{rev}"
    pairs = [(ctx.dist_url + f, local_root / f) for f in files]
    total = sum(1 for _ in pairs)
    r.note(f"Downloading {total} files to {local_root} (cached between runs) ...")
    errors = net.download_many(pairs, jobs=ctx.jobs, refresh=ctx.args.refresh, progress=r.log)
    if errors:
        r.fail("Download dist artifacts", f"{len(errors)} failed", [f"{u}: {e}" for u, e in errors.items()])
    ctx.dist_files = {f: local_root / f for f in files if (ctx.dist_url + f) not in errors}

    _verify_signatures(ctx)
    _verify_checksums(ctx)


def _expects_noboolean(ctx: Context, files: list[str]) -> bool:
    if ctx.project.key != "cassandra":
        return False
    if ctx.git_dir and ctx.git_commit:
        from .git import git

        out = git("ls-tree", "--name-only", ctx.git_commit, "redhat/", cwd=ctx.git_dir, check=False)
        return "redhat/noboolean" in out.split()
    return any(f.startswith("redhat/noboolean/") for f in files)


def _is_primary(rel: str) -> bool:
    return rel.endswith(PRIMARY_EXTS) and not _GENERATED.match(rel)


def _verify_signatures(ctx: Context) -> None:
    r = ctx.report
    sigs = sorted(f for f in ctx.dist_files if f.endswith(".asc"))
    other_ok, other_bad = 0, []
    verified_primary = []
    for sig in sigs:
        data_rel = sig[: -len(".asc")]
        data = ctx.dist_files.get(data_rel)
        if data is None:
            r.fail(f"Signature {sig}", "signed file is missing")
            continue
        res = ctx.keyring.verify(ctx.dist_files[sig], data)
        ctx.record_signature(res)
        if _is_primary(data_rel):
            if res.ok:
                r.ok(f"Signature {sig}", res.signer + (" (key EXPIRED)" if res.expired_key else ""))
                verified_primary.append(data_rel)
            else:
                r.fail(f"Signature {sig}", res.error)
        elif res.ok:
            other_ok += 1
        else:
            other_bad.append(f"{sig}: {res.error}")
    if other_bad:
        r.fail("Signatures of repository metadata", f"{len(other_bad)} invalid", other_bad)
    elif other_ok:
        r.ok("Signatures of repository metadata", f"{other_ok} valid detached signatures")

    unsigned = [f for f in ctx.dist_files if _is_primary(f) and f + ".asc" not in ctx.dist_files]
    if unsigned:
        # Cassandra's .deb/.rpm are signed inside the package/repository instead; checked in the packages section
        if ctx.project.packages == "cassandra":
            unsigned = [f for f in unsigned if not f.startswith(("debian/", "redhat/"))]
        known = [f for f in unsigned if ctx.project.unsigned_ok and re.match(ctx.project.unsigned_ok, f)]
        unsigned = [f for f in unsigned if f not in known]
        if known:
            no_sums = [f for f in known if f + ".sha512" not in ctx.dist_files and f + ".sha256" not in ctx.dist_files]
            r.warn(
                "Every artifact has a detached signature",
                f"{len(known)} convenience binaries have no .asc"
                + (f" and {len(no_sums)} no .sha256/.sha512" if no_sums else "")
                + " (ASF distribution policy requires both; previous releases of this project did the same)",
                known,
            )
        if unsigned:
            r.fail("Every artifact has a detached signature", "", unsigned)
    if verified_primary:
        ctx.highlight(f"Dist: PGP signatures valid for {', '.join(Path(f).name for f in verified_primary)}")


def _verify_checksums(ctx: Context) -> None:
    r = ctx.report
    by_target: dict[str, list[str]] = {}
    for f in ctx.dist_files:
        for ext in checksums.CHECKSUM_EXTS:
            if f.endswith(ext):
                by_target.setdefault(f[: -len(ext)], []).append(ext[1:])
    verified = []
    for target in sorted(by_target):
        algos = sorted(by_target[target], key=list(checksums.ALGOS).index)
        data = ctx.dist_files.get(target)
        name = f"Checksums {target}"
        if data is None:
            r.fail(name, "checksummed file is missing")
            continue
        actual = checksums.digests(data, algos)
        bad = []
        for algo in algos:
            try:
                expected = checksums.parse(ctx.dist_files[f"{target}.{algo}"].read_text(errors="replace"), algo, Path(target).name)
            except checksums.ChecksumFormatError as e:
                bad.append(f"{algo}: {e}")
                continue
            if expected != actual[algo]:
                bad.append(f"{algo}: expected {expected}, got {actual[algo]}")
        if bad:
            r.fail(name, "", bad)
        else:
            r.ok(name, "/".join(a.upper() for a in algos) + " match")
            verified.append(target)
    missing_sha512 = [f for f in ctx.dist_files if _is_primary(f) and f + ".sha512" not in ctx.dist_files and f + ".asc" in ctx.dist_files]
    if missing_sha512:
        r.warn("Every signed artifact has a SHA-512 checksum", "", missing_sha512)
    if verified:
        ctx.highlight(f"Dist: SHA-256/SHA-512 checksums match for {len(verified)} artifacts")

"""Checks of the Debian and RedHat packages and package repositories of the release.

For a release candidate they are the ones staged under dist/dev. For a released version (--released) they
are read from the public repositories finish_release.sh publishes to, debian.cassandra.apache.org and
redhat.cassandra.apache.org, which hold every version of a series: the indexes are then filtered to the
version being checked.
"""

from __future__ import annotations

import gzip
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import checksums, net
from ..context import Context
from ..pkgformats import (
    checksum_lines,
    parse_deb822,
    read_deb_control,
    read_rpm,
    write_rpm_signed_data,
)
from ..projects import DEBIAN_PUBLIC, REDHAT_PUBLIC, deb_version, debian_series

_RPM_NS = {"repo": "http://linux.duke.edu/metadata/repo", "common": "http://linux.duke.edu/metadata/common"}


def run(ctx: Context) -> None:
    if not ctx.project.packages:
        return
    r = ctx.report
    r.section("Debian and RedHat packages")
    released = ctx.args.released
    if released:
        if ctx.project.packages != "cassandra":
            r.skip("Package checks", f"published {ctx.project.display} packages are not checked in --released mode")
            return
        r.note(f"Reading the published packages from {DEBIAN_PUBLIC} and {REDHAT_PUBLIC} ...")
        files = _fetch_public(ctx)
    else:
        files = ctx.dist_files
    if not files:
        r.skip("Package checks", "no package files were downloaded")
        return
    ctx.package_files = files
    if ctx.project.packages == "cassandra":
        _apt_repository(ctx, files, released)
        _debian_upload(ctx, files, released)
        for base in ("redhat/", "redhat/noboolean/"):
            if any(f.startswith(base + "repodata/") for f in files):
                _yum_repository(ctx, files, base, released)
    _deb_controls(ctx, files, released)
    _rpm_headers(ctx, files)


# -------------------------------------------------------- published packages
def _fetch_public(ctx: Context) -> dict[str, Path]:
    """Downloads this version's packages and the repository metadata that covers them.

    Files get the same relative paths as in a staged dist directory ("debian/dists/50x/Release",
    "debian/pool/...", "redhat/repodata/...", "redhat/noboolean/..."), so the same checks apply.
    """
    series, version = debian_series(ctx.version), deb_version(ctx.version)
    root = ctx.workdir / "public"
    files: dict[str, Path] = {}

    def get(url: str, rel: str, index: bool) -> Path | None:
        try:
            # indexes change with every release of the series; packages never change
            files[rel] = net.download(url, root / rel, refresh=index or ctx.args.refresh)
            return files[rel]
        except net.HttpError:
            return None

    packages: list[tuple[str, str]] = []
    base = f"dists/{series}/"
    for name in ("InRelease", "Release", "Release.gpg"):
        get(f"{DEBIAN_PUBLIC}{base}{name}", f"debian/{base}{name}", index=True)
    release = files.get(f"debian/{base}Release")
    if release:
        for _digest, _size, name in checksum_lines(parse_deb822(release.read_text(errors="replace"))[0].get("SHA256", "")):
            get(f"{DEBIAN_PUBLIC}{base}{name}", f"debian/{base}{name}", index=True)
        for rel in [f for f in files if f.startswith(f"debian/{base}") and f.endswith("/Packages")]:
            for stanza in parse_deb822(files[rel].read_text(errors="replace")):
                if stanza.get("Version") == version and stanza.get("Filename"):
                    packages.append((DEBIAN_PUBLIC + stanza["Filename"], "debian/" + stanza["Filename"]))
        sources = files.get(f"debian/{base}main/source/Sources.gz")
        if sources:
            for stanza in parse_deb822(gzip.decompress(sources.read_bytes()).decode("utf-8", errors="replace")):
                if stanza.get("Version") == version:
                    for _d, _s, name in checksum_lines(stanza.get("Checksums-Sha256", "")):
                        path = f"{stanza.get('Directory', '')}/{name}"
                        packages.append((DEBIAN_PUBLIC + path, "debian/" + path))

    for sub in ("", "noboolean/"):
        rbase = f"{REDHAT_PUBLIC}{series}/{sub}"
        repomd = get(f"{rbase}repodata/repomd.xml", f"redhat/{sub}repodata/repomd.xml", index=True)
        if repomd is None:
            continue
        get(f"{rbase}repodata/repomd.xml.asc", f"redhat/{sub}repodata/repomd.xml.asc", index=True)
        primary = None
        for data in ET.parse(repomd).getroot().findall("repo:data", _RPM_NS):
            loc = data.find("repo:location", _RPM_NS).get("href")
            local = get(rbase + loc, f"redhat/{sub}{loc}", index=True)
            if data.get("type") == "primary":
                primary = local
        if primary is not None:
            for pkg in ET.fromstring(gzip.decompress(primary.read_bytes())).findall("common:package", _RPM_NS):
                if pkg.find("common:version", _RPM_NS).get("ver") == version:
                    loc = pkg.find("common:location", _RPM_NS).get("href")
                    packages.append((rbase + loc, f"redhat/{sub}{loc}"))

    unique = dict((rel, url) for url, rel in packages)
    ctx.report.note(f"Downloading {len(unique)} published package files for {ctx.version} ...")
    errors = net.download_many([(url, root / rel) for rel, url in unique.items()], jobs=ctx.jobs,
                               refresh=ctx.args.refresh, progress=ctx.report.log)
    for rel, url in unique.items():
        if url not in errors:
            files[rel] = root / rel
    if errors:
        ctx.report.fail("Download published packages", f"{len(errors)} failed", [f"{u}: {e}" for u, e in errors.items()])
    return files


# ------------------------------------------------------------------- apt repo
def _apt_repository(ctx: Context, files: dict[str, Path], released: bool) -> None:
    r = ctx.report
    series = debian_series(ctx.version)
    base = f"debian/dists/{series}/"
    where = f"{DEBIAN_PUBLIC}dists/{series}/" if released else base
    if base + "Release" not in files:
        r.fail(f"APT repository {where}", "Release file missing")
        return

    inrelease = files.get(base + "InRelease")
    if inrelease:
        res = ctx.keyring.verify(inrelease)
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"APT {where}InRelease signature", res.signer if res.ok else res.error)
    gpgsig = files.get(base + "Release.gpg")
    if gpgsig:
        res = ctx.keyring.verify(gpgsig, files[base + "Release"])
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"APT {where}Release.gpg signature", res.signer if res.ok else res.error)

    release = parse_deb822(files[base + "Release"].read_text(errors="replace"))[0]
    if release.get("Codename") != series:
        r.fail("APT Release codename", f"{release.get('Codename')} (expected {series})")
    listed = checksum_lines(release.get("SHA256", ""))
    bad, absent = [], []
    for digest, size, name in listed:
        local = files.get(base + name)
        if local is None:
            absent.append(name)
            continue
        if local.stat().st_size != size or checksums.digests(local, ["sha256"])["sha256"] != digest:
            bad.append(name)
    if bad:
        r.fail("APT index files match the signed Release file", "", bad)
    else:
        r.ok("APT index files match the signed Release file", f"{len(listed) - len(absent)} index files verified")

    expected_version = deb_version(ctx.version)
    pkgs_bad, seen_packages = [], set()
    pool_checked = other_versions = 0
    for name in [n for _, _, n in listed if n.endswith("/Packages") and base + n in files]:
        for stanza in parse_deb822(files[base + name].read_text(errors="replace")):
            if stanza.get("Version") != expected_version:
                if released:  # the public repository holds every version of the series
                    other_versions += 1
                    continue
                pkgs_bad.append(f"{name}: {stanza.get('Package')} has version {stanza.get('Version')}")
            seen_packages.add(stanza.get("Package"))
            pool = files.get("debian/" + stanza.get("Filename", ""))
            if pool is None:
                pkgs_bad.append(f"{name}: {stanza.get('Filename')} not {'downloadable' if released else 'staged'}")
                continue
            if checksums.digests(pool, ["sha256"])["sha256"] != stanza.get("SHA256", "").lower():
                pkgs_bad.append(f"{name}: SHA256 mismatch for {stanza.get('Filename')}")
            pool_checked += 1
    for required in ("cassandra", "cassandra-tools"):
        if required not in seen_packages:
            pkgs_bad.append(f"package '{required}' {expected_version} is not in any Packages index")
    if pkgs_bad:
        r.fail("APT packages match the Packages indexes", "", pkgs_bad)
    else:
        extra = f" ({other_versions} entries for other versions ignored)" if other_versions else ""
        r.ok("APT packages match the Packages indexes", f"{pool_checked} entries, version {expected_version}{extra}")

    sources = files.get(base + "main/source/Sources.gz")
    if sources:
        bad, checked = [], 0
        for stanza in parse_deb822(gzip.decompress(sources.read_bytes()).decode("utf-8", errors="replace")):
            if released and stanza.get("Version") != expected_version:
                continue
            directory = stanza.get("Directory", "")
            for digest, _size, fname in checksum_lines(stanza.get("Checksums-Sha256", "")):
                checked += 1
                local = files.get(f"debian/{directory}/{fname}")
                if local is None or checksums.digests(local, ["sha256"])["sha256"] != digest:
                    bad.append(f"{directory}/{fname}")
        if checked or bad:
            (r.fail if bad else r.ok)("APT source package matches Sources index", "" if bad else "dsc and tarball verified", bad)
        elif released:
            r.warn("APT source package matches Sources index", f"no source package {expected_version} in the Sources index")
    if not pkgs_bad:
        ctx.highlight(f"Debian: APT repository `{series}` signature valid; Packages/Sources hashes match the .deb, .dsc and source tarball")


def _debian_upload(ctx: Context, files: dict[str, Path], released: bool) -> None:
    """The debsign-signed .changes and .dsc files, and the files they list."""
    r = ctx.report
    v = deb_version(ctx.version)
    # staged: debian/ holds the upload; published: only the .dsc remains, in the pool
    folder = "debian/pool/main/c/cassandra/" if released else "debian/"
    for name in (f"{folder}cassandra_{v}_amd64.changes", f"{folder}cassandra_{v}.dsc"):
        local = files.get(name)
        if not local:
            continue
        res = ctx.keyring.verify(local)
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"Signature {name}", res.signer if res.ok else res.error)
        stanza = parse_deb822(local.read_text(errors="replace"))[0]
        bad = []
        listed = checksum_lines(stanza.get("Checksums-Sha256", ""))
        for digest, _size, fname in listed:
            f = files.get(f"{folder}{fname}")
            if f is None:
                bad.append(f"{fname} listed but not staged")
            elif checksums.digests(f, ["sha256"])["sha256"] != digest:
                bad.append(f"{fname} SHA256 mismatch")
        (r.fail if bad else r.ok)(f"Files listed in {Path(name).name}", "" if bad else f"{len(listed)} files match", bad)


# ------------------------------------------------------------------- yum repo
def _algo(createrepo_type: str | None) -> str:
    return {"sha": "sha1", None: "sha256"}.get(createrepo_type, createrepo_type)


def _yum_repository(ctx: Context, files: dict[str, Path], base: str, released: bool) -> None:
    r = ctx.report
    where = f"{REDHAT_PUBLIC}{debian_series(ctx.version)}/{base[len('redhat/'):]}" if released else base
    repomd = files.get(base + "repodata/repomd.xml")
    if repomd is None:
        r.fail(f"YUM repository {where}", "repodata/repomd.xml missing")
        return
    if released:
        # for a staged RC the dist checks verify every .asc; here the published repomd.xml.asc is checked directly
        sig = files.get(base + "repodata/repomd.xml.asc")
        if sig is None:
            r.fail(f"YUM {where}repodata/repomd.xml signature", "repomd.xml.asc missing (yum repo_gpgcheck needs it)")
        else:
            res = ctx.keyring.verify(sig, repomd)
            ctx.record_signature(res)
            (r.ok if res.ok else r.fail)(f"YUM {where}repodata/repomd.xml signature", res.signer if res.ok else res.error)
    root = ET.parse(repomd).getroot()
    bad, primary = [], None
    for data in root.findall("repo:data", _RPM_NS):
        loc = data.find("repo:location", _RPM_NS).get("href")
        cs = data.find("repo:checksum", _RPM_NS)
        local = files.get(base + loc)
        if local is None:
            bad.append(f"{loc} listed in repomd.xml but not available")
            continue
        algo = _algo(cs.get("type")) if cs is not None else "sha256"
        if cs is not None and checksums.digests(local, [algo])[algo] != cs.text.strip():
            bad.append(f"{loc} {algo} mismatch")
        if data.get("type") == "primary":
            primary = local
    (r.fail if bad else r.ok)(f"YUM {where} metadata matches signed repomd.xml", "" if bad else "all repodata files verified", bad)

    if primary is None:
        r.fail(f"YUM {where} primary metadata", "missing")
        return
    pkg_root = ET.fromstring(gzip.decompress(primary.read_bytes()))
    expected_version = deb_version(ctx.version)
    bad, count, others = [], 0, 0
    for pkg in pkg_root.findall("common:package", _RPM_NS):
        name = pkg.find("common:name", _RPM_NS).text
        ver = pkg.find("common:version", _RPM_NS).get("ver")
        loc = pkg.find("common:location", _RPM_NS).get("href")
        cs = pkg.find("common:checksum", _RPM_NS)
        if ver != expected_version:
            if released:
                others += 1
                continue
            bad.append(f"{name}: version {ver}, expected {expected_version}")
        count += 1
        local = files.get(base + loc)
        if local is None:
            bad.append(f"{loc} listed but not available")
        elif checksums.digests(local, [_algo(cs.get("type"))])[_algo(cs.get("type"))] != cs.text.strip():
            bad.append(f"{loc} checksum mismatch")
    if released and not count:
        bad.append(f"no {expected_version} package in the primary metadata")
    extra = f" ({others} packages of other versions ignored)" if others else ""
    (r.fail if bad else r.ok)(f"YUM {where} packages match primary metadata", "" if bad else f"{count} packages, version {expected_version}{extra}", bad)
    if not bad:
        ctx.highlight(f"RedHat: `{where}` repomd.xml signature valid; repodata and {count} RPM checksums match")


# ------------------------------------------------------------ package headers
def _deb_controls(ctx: Context, files: dict[str, Path], released: bool) -> None:
    r = ctx.report
    # staged: the copies next to the upload (the pool holds the same files); published: the pool
    debs = sorted(f for f in files if f.endswith(".deb") and (released or "/pool/" not in f))
    accepted = {ctx.version, deb_version(ctx.version)}
    for rel in debs:
        try:
            control = read_deb_control(files[rel])
        except Exception as e:  # noqa: BLE001
            r.fail(f"Package metadata {rel}", f"cannot read: {e}")
            continue
        desc = f"{control.get('Package')} {control.get('Version')} ({control.get('Architecture')})"
        version = control.get("Version", "")
        if version in accepted or version.split("-")[0] in accepted:
            r.ok(f"Package metadata {Path(rel).name}", desc)
        else:
            r.fail(f"Package metadata {Path(rel).name}", f"{desc}: version does not match {ctx.version}")


def _rpm_headers(ctx: Context, files: dict[str, Path]) -> None:
    r = ctx.report
    rpms = sorted(f for f in files if f.endswith(".rpm"))
    accepted = {ctx.version, deb_version(ctx.version), ctx.version.replace("-", "_")}
    unsigned, sig_bad, sig_ok = [], [], 0
    tmp = Path(tempfile.mkdtemp(prefix="rpmsig-", dir=ctx.workdir))
    for rel in rpms:
        path = files[rel]
        try:
            rpm = read_rpm(path)
        except Exception as e:  # noqa: BLE001
            r.fail(f"Package metadata {rel}", f"cannot read: {e}")
            continue
        desc = f"{rpm.nvr}.{'src' if rpm.is_source else rpm.arch}"
        if rpm.version not in accepted:
            r.warn(f"Package metadata {rel}", f"{desc}: rpm version '{rpm.version}' does not match {ctx.version}")
        elif not rpm.release:
            r.warn(f"Package metadata {rel}", f"{desc}: the RPM Release tag is empty")
        else:
            r.ok(f"Package metadata {rel}", desc)

        if not rpm.signatures:
            unsigned.append(rel)
            continue
        for tag, blob in rpm.signatures.items():
            sig_file, data_file = tmp / "sig", tmp / "data"
            sig_file.write_bytes(blob)
            write_rpm_signed_data(path, rpm, tag, data_file)
            res = ctx.keyring.verify(sig_file, data_file)
            ctx.record_signature(res)
            if res.ok:
                sig_ok += 1
            else:
                sig_bad.append(f"{rel} (sigtag {tag}): {res.error}")
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()

    if sig_bad:
        r.fail("Embedded RPM signatures", "", sig_bad)
    elif sig_ok:
        r.ok("Embedded RPM signatures", f"{sig_ok} signatures valid in {len(rpms) - len(unsigned)} RPMs")
    if unsigned:
        detached = [u for u in unsigned if u + ".asc" in files]
        if len(detached) == len(unsigned):
            r.info("Embedded RPM signatures", "RPMs are not signed internally; their detached .asc signatures were verified")
        else:
            r.fail("RPMs are signed", "neither an embedded nor a detached signature", [u for u in unsigned if u not in detached])

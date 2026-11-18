"""Checks of the Debian and RedHat packages and package repositories staged with the release."""

from __future__ import annotations

import gzip
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import checksums
from ..context import Context
from ..pkgformats import (
    checksum_lines,
    parse_deb822,
    read_deb_control,
    read_rpm,
    write_rpm_signed_data,
)
from ..projects import deb_version, debian_series

_RPM_NS = {"repo": "http://linux.duke.edu/metadata/repo", "common": "http://linux.duke.edu/metadata/common"}


def run(ctx: Context) -> None:
    if not ctx.project.packages:
        return
    r = ctx.report
    r.section("Debian and RedHat packages")
    if not ctx.dist_files:
        r.skip("Package checks", "dist artifacts were not downloaded")
        return
    if ctx.project.packages == "cassandra":
        _apt_repository(ctx)
        _debian_upload(ctx)
        for base in ("redhat/", "redhat/noboolean/"):
            if any(f.startswith(base + "repodata/") for f in ctx.dist_files):
                _yum_repository(ctx, base)
    _deb_controls(ctx)
    _rpm_headers(ctx)


# ------------------------------------------------------------------- apt repo
def _apt_repository(ctx: Context) -> None:
    r = ctx.report
    series = debian_series(ctx.version)
    base = f"debian/dists/{series}/"
    files = ctx.dist_files
    if base + "Release" not in files:
        r.fail(f"APT repository {base}", "Release file missing")
        return

    inrelease = files.get(base + "InRelease")
    if inrelease:
        res = ctx.keyring.verify(inrelease)
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"APT {base}InRelease signature", res.signer if res.ok else res.error)
    gpgsig = files.get(base + "Release.gpg")
    if gpgsig:
        res = ctx.keyring.verify(gpgsig, files[base + "Release"])
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"APT {base}Release.gpg signature", res.signer if res.ok else res.error)

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
    pool_checked = 0
    for name in [n for _, _, n in listed if n.endswith("/Packages") and base + n in files]:
        for stanza in parse_deb822(files[base + name].read_text(errors="replace")):
            seen_packages.add(stanza.get("Package"))
            if stanza.get("Version") != expected_version:
                pkgs_bad.append(f"{name}: {stanza.get('Package')} has version {stanza.get('Version')}")
            pool = files.get("debian/" + stanza.get("Filename", ""))
            if pool is None:
                pkgs_bad.append(f"{name}: {stanza.get('Filename')} not staged")
                continue
            if checksums.digests(pool, ["sha256"])["sha256"] != stanza.get("SHA256", "").lower():
                pkgs_bad.append(f"{name}: SHA256 mismatch for {stanza.get('Filename')}")
            pool_checked += 1
    for required in ("cassandra", "cassandra-tools"):
        if required not in seen_packages:
            pkgs_bad.append(f"package '{required}' is not in any Packages index")
    if pkgs_bad:
        r.fail("APT packages match the Packages indexes", "", pkgs_bad)
    else:
        r.ok("APT packages match the Packages indexes", f"{pool_checked} entries, version {expected_version}")

    sources = files.get(base + "main/source/Sources.gz")
    if sources:
        bad = []
        for stanza in parse_deb822(gzip.decompress(sources.read_bytes()).decode("utf-8", errors="replace")):
            directory = stanza.get("Directory", "")
            for digest, _size, fname in checksum_lines(stanza.get("Checksums-Sha256", "")):
                local = files.get(f"debian/{directory}/{fname}")
                if local is None or checksums.digests(local, ["sha256"])["sha256"] != digest:
                    bad.append(f"{directory}/{fname}")
        (r.fail if bad else r.ok)("APT source package matches Sources index", "" if bad else "dsc and tarball verified", bad)
    if not pkgs_bad:
        ctx.highlight(f"Debian: APT repository `{series}` signature valid; Packages/Sources hashes match the .deb, .dsc and source tarball")


def _debian_upload(ctx: Context) -> None:
    """The debsign-signed .changes and .dsc files, and the files they list."""
    r = ctx.report
    v = deb_version(ctx.version)
    for name in (f"debian/cassandra_{v}_amd64.changes", f"debian/cassandra_{v}.dsc"):
        local = ctx.dist_files.get(name)
        if not local:
            continue
        res = ctx.keyring.verify(local)
        ctx.record_signature(res)
        (r.ok if res.ok else r.fail)(f"Signature {name}", res.signer if res.ok else res.error)
        stanza = parse_deb822(local.read_text(errors="replace"))[0]
        bad = []
        listed = checksum_lines(stanza.get("Checksums-Sha256", ""))
        for digest, _size, fname in listed:
            f = ctx.dist_files.get(f"debian/{fname}")
            if f is None:
                bad.append(f"{fname} listed but not staged")
            elif checksums.digests(f, ["sha256"])["sha256"] != digest:
                bad.append(f"{fname} SHA256 mismatch")
        (r.fail if bad else r.ok)(f"Files listed in {Path(name).name}", "" if bad else f"{len(listed)} files match", bad)


# ------------------------------------------------------------------- yum repo
def _algo(createrepo_type: str | None) -> str:
    return {"sha": "sha1", None: "sha256"}.get(createrepo_type, createrepo_type)


def _yum_repository(ctx: Context, base: str) -> None:
    r = ctx.report
    files = ctx.dist_files
    repomd = files.get(base + "repodata/repomd.xml")
    if repomd is None:
        r.fail(f"YUM repository {base}", "repodata/repomd.xml missing")
        return
    root = ET.parse(repomd).getroot()
    bad, primary = [], None
    for data in root.findall("repo:data", _RPM_NS):
        loc = data.find("repo:location", _RPM_NS).get("href")
        cs = data.find("repo:checksum", _RPM_NS)
        local = files.get(base + loc)
        if local is None:
            bad.append(f"{loc} listed in repomd.xml but not staged")
            continue
        algo = _algo(cs.get("type")) if cs is not None else "sha256"
        if cs is not None and checksums.digests(local, [algo])[algo] != cs.text.strip():
            bad.append(f"{loc} {algo} mismatch")
        if data.get("type") == "primary":
            primary = local
    (r.fail if bad else r.ok)(f"YUM {base} metadata matches signed repomd.xml", "" if bad else "all repodata files verified", bad)

    if primary is None:
        r.fail(f"YUM {base} primary metadata", "missing")
        return
    pkg_root = ET.fromstring(gzip.decompress(primary.read_bytes()))
    expected_version = deb_version(ctx.version)
    bad, count = [], 0
    for pkg in pkg_root.findall("common:package", _RPM_NS):
        name = pkg.find("common:name", _RPM_NS).text
        ver = pkg.find("common:version", _RPM_NS).get("ver")
        loc = pkg.find("common:location", _RPM_NS).get("href")
        cs = pkg.find("common:checksum", _RPM_NS)
        count += 1
        if ver != expected_version:
            bad.append(f"{name}: version {ver}, expected {expected_version}")
        local = files.get(base + loc)
        if local is None:
            bad.append(f"{loc} listed but not staged")
        elif checksums.digests(local, [_algo(cs.get("type"))])[_algo(cs.get("type"))] != cs.text.strip():
            bad.append(f"{loc} checksum mismatch")
    (r.fail if bad else r.ok)(f"YUM {base} packages match primary metadata", "" if bad else f"{count} packages, version {expected_version}", bad)
    if not bad:
        ctx.highlight(f"RedHat: `{base}` repomd.xml signature valid; repodata and {count} RPM checksums match")


# ------------------------------------------------------------ package headers
def _deb_controls(ctx: Context) -> None:
    r = ctx.report
    debs = sorted(f for f in ctx.dist_files if f.endswith(".deb") and "/pool/" not in f)
    accepted = {ctx.version, deb_version(ctx.version)}
    for rel in debs:
        try:
            control = read_deb_control(ctx.dist_files[rel])
        except Exception as e:  # noqa: BLE001
            r.fail(f"Package metadata {rel}", f"cannot read: {e}")
            continue
        desc = f"{control.get('Package')} {control.get('Version')} ({control.get('Architecture')})"
        version = control.get("Version", "")
        if version in accepted or version.split("-")[0] in accepted:
            r.ok(f"Package metadata {Path(rel).name}", desc)
        else:
            r.fail(f"Package metadata {Path(rel).name}", f"{desc}: version does not match {ctx.version}")


def _rpm_headers(ctx: Context) -> None:
    r = ctx.report
    rpms = sorted(f for f in ctx.dist_files if f.endswith(".rpm"))
    accepted = {ctx.version, deb_version(ctx.version), ctx.version.replace("-", "_")}
    unsigned, sig_bad, sig_ok = [], [], 0
    tmp = Path(tempfile.mkdtemp(prefix="rpmsig-", dir=ctx.workdir))
    for rel in rpms:
        path = ctx.dist_files[rel]
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
        detached = [u for u in unsigned if u + ".asc" in ctx.dist_files]
        if len(detached) == len(unsigned):
            r.info("Embedded RPM signatures", "RPMs are not signed internally; their detached .asc signatures were verified")
        else:
            r.fail("RPMs are signed", "neither an embedded nor a detached signature", [u for u in unsigned if u not in detached])

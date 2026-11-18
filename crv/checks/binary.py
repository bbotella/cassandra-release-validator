"""Checks of the binary (convenience) tarball."""

from __future__ import annotations

import re
import zipfile

from .. import archive, checksums
from ..context import Context
from .source import check_license_notice


def run(ctx: Context) -> None:
    r = ctx.report
    p = ctx.project
    r.section("Binary release")
    if p.wheels_dir:
        _wheels(ctx)
    if not p.has_bin_tarball:
        if not p.wheels_dir:
            r.info("Binary release", f"{p.display} is released as source only")
        return
    name = p.bin_tarball(ctx.version)
    tarball = ctx.dist_files.get(name)
    if tarball is None:
        r.skip("Binary tarball checks", f"{name} was not downloaded")
        return
    dest = ctx.workdir / "extract" / ("bin-" + checksums.digests(tarball, ["sha256"])["sha256"][:16])
    r.note(f"Extracting {name} ...")
    try:
        root = archive.extract(tarball, dest)
    except Exception as e:  # noqa: BLE001
        r.fail("Extract binary tarball", str(e))
        return
    ctx.bin_root = root

    expected_top = f"{p.tarball_prefix}-{ctx.version}"
    if root == dest:
        r.warn("Single top-level directory", f"found {archive.top_level_entries(dest)}")
    elif root.name != expected_top:
        r.warn("Top-level directory name", f"{root.name} (expected {expected_top})")
    else:
        r.ok("Top-level directory name", root.name)

    check_license_notice(ctx, root, "binary")

    required = [path.format(version=ctx.version) for path in p.bin_required]
    missing = [path for path in required if not (root / path).exists()]
    if missing:
        r.fail("Expected files in the binary tarball", "", missing)
    elif required:
        r.ok("Expected files in the binary tarball", ", ".join(required))

    jars = [rel for rel, _ in archive.iter_files(root) if rel.endswith(".jar")]
    own = [j for j in jars if re.search(rf"-{re.escape(ctx.version)}(?:-[a-z]+)?\.jar$", j)]
    snapshots = [j for j in jars if "SNAPSHOT" in j]
    if own:
        r.ok("Jars built with the release version", f"{len(own)} jars named *-{ctx.version}.jar")
    elif jars:
        r.warn("Jars built with the release version", f"none of the {len(jars)} jars is named *-{ctx.version}.jar")
    if snapshots:
        r.fail("No SNAPSHOT jars in the binary release", "", snapshots)
    elif jars:
        r.ok("No SNAPSHOT jars in the binary release")

    if p.key == "cassandra":
        jar = root / f"lib/apache-cassandra-{ctx.version}.jar"
        if jar.exists():
            try:
                with zipfile.ZipFile(jar) as z:
                    manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
                m = re.search(r"(?m)^Implementation-Version:\s*(\S+)", manifest)
                version = m.group(1) if m else None
                (r.ok if version == ctx.version else r.fail)(
                    "Implementation-Version of apache-cassandra.jar", f"{version} (expected {ctx.version})"
                )
            except Exception as e:  # noqa: BLE001
                r.warn("Implementation-Version of apache-cassandra.jar", f"cannot read manifest: {e}")


def _wheels(ctx: Context) -> None:
    """Python wheels: metadata version, LICENSE/NOTICE inside, and which interpreters/platforms are covered."""
    r = ctx.report
    p = ctx.project
    wheels = sorted(f for f in ctx.dist_files if f.startswith(p.wheels_dir) and f.endswith(".whl"))
    if not wheels:
        r.skip("Wheels", "no wheels downloaded")
        return
    bad_version, no_license, no_notice, unreadable = [], [], [], []
    names, pythons, platforms = set(), set(), set()
    for rel in wheels:
        # name-version(-build)?-python-abi-platform.whl (PEP 427)
        parts = rel.rsplit("/", 1)[-1][: -len(".whl")].split("-")
        if len(parts) >= 5:
            pythons.add(parts[-3])
            platforms.add(_platform(parts[-1]))
        try:
            with zipfile.ZipFile(ctx.dist_files[rel]) as z:
                entries = z.namelist()
                meta_name = next((n for n in entries if n.endswith(".dist-info/METADATA")), None)
                meta = z.read(meta_name).decode("utf-8", "replace") if meta_name else ""
        except (zipfile.BadZipFile, OSError) as e:
            unreadable.append(f"{rel}: {e}")
            continue
        version = re.search(r"(?m)^Version:\s*(\S+)", meta)
        name = re.search(r"(?m)^Name:\s*(\S+)", meta)
        if name:
            names.add(name.group(1))
        if not version or version.group(1) != ctx.version:
            bad_version.append(f"{rel}: Version {version.group(1) if version else '?'}")
        dist_info = [n for n in entries if ".dist-info/" in n]
        if not any(re.search(r"/(licenses/)?LICEN[CS]E[^/]*$", n) for n in dist_info):
            no_license.append(rel)
        if not any(re.search(r"/(licenses/)?NOTICE[^/]*$", n) for n in dist_info):
            no_notice.append(rel)
    if unreadable:
        r.fail("Wheels are valid zip archives", "", unreadable)
    (r.fail if bad_version else r.ok)("Wheel metadata version", "" if bad_version else f"{len(wheels)} wheels of {', '.join(sorted(names))} {ctx.version}", bad_version)
    if no_license:
        r.warn("LICENSE in wheels", f"{len(no_license)} of {len(wheels)} wheels have no LICENSE in their .dist-info", no_license)
    else:
        r.ok("LICENSE in wheels")
    if no_notice:
        r.warn("NOTICE in wheels", f"{len(no_notice)} of {len(wheels)} wheels have no NOTICE in their .dist-info", no_notice)
    else:
        r.ok("NOTICE in wheels")
    r.info("Wheel coverage", f"Python {', '.join(sorted(pythons, key=_py_key))} on {', '.join(sorted(platforms))}")


def _py_key(tag: str) -> tuple:
    m = re.match(r"[a-z]+(\d)(\d+)", tag)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _platform(tag: str) -> str:
    """'manylinux2014_x86_64.manylinux_2_17_x86_64' -> 'Linux x86_64', 'macosx_11_0_arm64' -> 'macOS arm64'."""
    arch = next((a for a in ("x86_64", "aarch64", "arm64", "amd64", "universal2", "i686", "win32") if a in tag), tag)
    if tag.startswith("macosx"):
        return f"macOS {arch}"
    if "linux" in tag:
        return f"Linux {arch}"
    if tag.startswith("win"):
        return f"Windows {arch}"
    return tag

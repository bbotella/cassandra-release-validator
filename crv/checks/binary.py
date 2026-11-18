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

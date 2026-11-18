#!/usr/bin/env python3
"""Compares the project's own jars in the binary tarball with the same artifacts staged in Maven.

Both come from the release build, so users should get the same code whichever they download. This
catches a binary tarball built from a different flavour or run than the Maven artifacts. For example,
Analytics builds Spark 3 and Spark 4 separately, and the Scala-independent jars in the tarball came
from a different build than the ones in Nexus.

Works on what validate-release already downloaded and extracted (run that first). Read-only: jars are
opened as zip files and nothing from them is executed. Run it with `python3 -I`.

usage: compare-bin-maven.py --project analytics --version 0.5.0 [--cache DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassandra-release-validator"
# jars whose name in the binary tarball differs from the Maven artifactId
ALIASES = {"apache-cassandra": "cassandra-all"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", required=True, choices=["cassandra", "sidecar", "analytics"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help=f"validate-release work directory root (default {DEFAULT_CACHE})")
    args = ap.parse_args()

    work = args.cache / f"{args.project}-{args.version}"
    bin_roots = sorted((p for p in (work / "extract").glob("bin-*/*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    if not bin_roots:
        print(f"No extracted binary tarball under {work}/extract: run validate-release for this candidate first.")
        return 2
    bin_root = bin_roots[-1]
    maven_jars: dict[str, list[Path]] = {}
    for jar in (work / "maven").rglob("*.jar"):
        if not re.search(r"-(sources|javadoc)\.jar$", jar.name):
            maven_jars.setdefault(jar.name, []).append(jar)
    if not maven_jars:
        print(f"No Maven jars under {work}/maven: run validate-release without --skip maven first.")
        return 2

    matched = []
    for jar in sorted(bin_root.rglob("*.jar")):
        name = jar.name
        base = name[: -len(f"-{args.version}.jar")] if name.endswith(f"-{args.version}.jar") else None
        candidates = maven_jars.get(name, [])
        if not candidates and base in ALIASES:
            candidates = maven_jars.get(f"{ALIASES[base]}-{args.version}.jar", [])
        for maven in candidates:
            matched.append((jar, maven))

    print(f"Binary tarball: {bin_root}")
    if not matched:
        print("No jar in the binary tarball matches a staged Maven artifact by name.")
        return 0
    identical = 0
    for jar, maven in matched:
        diff = compare(jar, maven)
        rel_bin = jar.relative_to(bin_root)
        repo = maven.relative_to(work / "maven").parts[0]
        if diff is None:
            identical += 1
            print(f"  same      {rel_bin}  ==  Maven repo {repo}")
            continue
        print(f"  DIFFERENT {rel_bin}  vs  Maven repo {repo}: {diff['summary']}")
        for line in diff["details"]:
            print(f"              {line}")
    print(f"\n{identical} of {len(matched)} jars have the same content as their Maven artifact.")
    print("A difference in bundled (shaded) dependency versions means users get different third-party code")
    print("depending on where they download from; worth a JIRA ticket. Class-only differences mean the tarball and")
    print("Maven jars came from separate build runs (a different JDK or compiler setup; a different Java target is")
    print("reported above when it is the cause). Worth asking the RM about, rarely a blocker on its own.")
    return 0


def entries(path: Path) -> dict[str, int]:
    with zipfile.ZipFile(path) as z:
        return {i.filename: i.CRC for i in z.infolist() if not i.is_dir()}


def bundled_versions(path: Path) -> dict[str, str]:
    """groupId:artifactId -> version of every dependency whose pom.properties was shaded into the jar."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.startswith("META-INF/maven/") and name.endswith("/pom.properties"):
                props = dict(
                    line.split("=", 1) for line in z.read(name).decode("utf-8", "replace").splitlines()
                    if "=" in line and not line.startswith("#")
                )
                if "groupId" in props and "artifactId" in props:
                    out[f"{props['groupId']}:{props['artifactId']}"] = props.get("version", "?")
    return out


def class_java_version(jar: Path, name: str) -> int | None:
    """Java release a class file targets (class file major version 52 = Java 8, 55 = 11, 61 = 17)."""
    with zipfile.ZipFile(jar) as z:
        head = z.read(name)[:8]
    if head[:4] != b"\xca\xfe\xba\xbe":
        return None
    return int.from_bytes(head[6:8], "big") - 44


def compare(a: Path, b: Path) -> dict | None:
    ea, eb = entries(a), entries(b)
    # MANIFEST.MF and pom.properties carry build timestamps / tool versions; compare them separately
    ignore = re.compile(r"^META-INF/(MANIFEST\.MF|maven/.*/pom\.(properties|xml))$")
    names = {n for n in ea.keys() | eb.keys() if not ignore.match(n)}
    only_a = sorted(n for n in names if n in ea and n not in eb)
    only_b = sorted(n for n in names if n in eb and n not in ea)
    changed = sorted(n for n in names if n in ea and n in eb and ea[n] != eb[n])
    va, vb = bundled_versions(a), bundled_versions(b)
    dep_diffs = [f"{k}: tarball {va.get(k, '-')} / Maven {vb.get(k, '-')}" for k in sorted(va.keys() | vb.keys()) if va.get(k) != vb.get(k)]
    if not (only_a or only_b or changed or dep_diffs):
        return None
    parts = []
    if changed:
        parts.append(f"{len(changed)} files differ")
    if only_a:
        parts.append(f"{len(only_a)} only in tarball")
    if only_b:
        parts.append(f"{len(only_b)} only in Maven")
    if dep_diffs:
        parts.append(f"{len(dep_diffs)} bundled dependency versions differ")
    details = [f"bundled {d}" for d in dep_diffs[:20]]
    first_class = next((n for n in changed if n.endswith(".class")), None)
    if first_class:
        ja, jb = class_java_version(a, first_class), class_java_version(b, first_class)
        if ja != jb:
            details.insert(0, f"compiled for Java {ja} in the tarball, Java {jb} in Maven ({first_class})")
    details += [f"differs: {n}" for n in changed[:5]] + [f"only in tarball: {n}" for n in only_a[:5]] + [f"only in Maven: {n}" for n in only_b[:5]]
    return {"summary": ", ".join(parts) + f" (of {len(names)} files)", "details": details}


if __name__ == "__main__":
    sys.exit(main())

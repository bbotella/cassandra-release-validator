"""Checks of the Maven artifacts in the Nexus staging repositories."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import checksums, net
from ..context import Context
from ..projects import NEXUS_REPOS, STAGING_PREFIX

_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
_SIDE_EXTS = (".asc",) + checksums.CHECKSUM_EXTS
_REQUIRED_SUMS = ("md5", "sha1")


@dataclass
class Staged:
    base_url: str
    repo_id: str
    group_path: str
    artifact: str
    version: str
    files: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.group_path}/{self.artifact}/{self.version}/"

    @property
    def gav(self) -> str:
        return f"{self.group_path.replace('/', '.')}:{self.artifact}:{self.version}"


def repo_url(repo_id: str) -> str:
    return f"{NEXUS_REPOS}{STAGING_PREFIX}{repo_id}/"


def crawl(base_url: str, repo_id: str, root: str = "org/apache/cassandra/") -> list[Staged]:
    """Finds every artifact version directory under root in a (small) staging repository."""
    found: list[Staged] = []

    def visit(rel: str, depth: int) -> None:
        names = net.list_dir(base_url + rel)
        subdirs = [n for n in names if n.endswith("/")]
        if "maven-metadata.xml" in names or any(d[0].isdigit() for d in subdirs):
            group, artifact = rel.rstrip("/").rsplit("/", 1)
            for d in subdirs:
                found.append(Staged(base_url, repo_id, group, artifact, d.rstrip("/")))
        elif depth < 4:
            for d in subdirs:
                visit(rel + d, depth + 1)

    visit(root, 0)
    return found


def run(ctx: Context) -> None:
    r = ctx.report
    r.section("Maven artifacts (Nexus staging)")
    if not ctx.project.maven:
        r.info("Maven artifacts", f"{ctx.project.display} does not publish to Maven: nothing staged in Nexus")
        return
    base_override = ctx.args.maven_base
    if not ctx.staging_repos and not base_override:
        r.fail("Staging repositories", "none found in the vote email (use --staging-repo N)")
        return

    staged: list[Staged] = []
    if base_override:
        base = base_override.rstrip("/") + "/"
        for ref in ctx.maven_refs:
            staged.append(Staged(base, "override", ref.group_path, ref.artifact, ref.version))
        if staged:
            r.info("Maven repository", f"{base} (only the {len(staged)} artifacts listed in the vote email)")
        elif _discover_pattern(ctx):
            staged = _discover(ctx, base)
            (r.info if staged else r.fail)("Maven repository", f"{base} ({len(staged)} artifacts matching {_discover_pattern(ctx)} "
                                           f"with version {ctx.version}; no individual artifacts were listed)")
        else:
            r.fail("Maven repository", f"the vote email lists no artifacts to look for in {base}")
    else:
        for repo_id in ctx.staging_repos:
            url = repo_url(repo_id)
            try:
                items = crawl(url, repo_id)
            except net.HttpError as e:
                r.fail(f"Staging repository {STAGING_PREFIX}{repo_id}", f"not accessible ({e}); was it closed, released or dropped?")
                continue
            r.ok(f"Staging repository {STAGING_PREFIX}{repo_id}", f"{len(items)} artifact versions")
            staged += items

    if not staged:
        return

    def list_files(s: Staged) -> None:
        try:
            s.files = [n for n in net.list_dir(s.url) if not n.endswith("/")]
        except net.HttpError:
            s.files = []

    with ThreadPoolExecutor(max_workers=ctx.jobs) as pool:
        list(pool.map(list_files, staged))

    _cross_check(ctx, staged)

    pairs = []
    for s in staged:
        local_dir = _local_dir(ctx, s)
        pairs += [(s.url + f, local_dir / f) for f in s.files]
    r.note(f"Downloading {len(pairs)} Maven files ...")
    errors = net.download_many(pairs, jobs=ctx.jobs, refresh=ctx.args.refresh, progress=r.log)
    if errors:
        r.fail("Download Maven artifacts", f"{len(errors)} failed", [f"{u}: {e}" for u, e in errors.items()])

    with ThreadPoolExecutor(max_workers=ctx.jobs) as pool:
        pom_index = {(s.group_path.replace("/", "."), s.artifact, s.version): _local_dir(ctx, s) / f"{s.artifact}-{s.version}.pom"
                     for s in staged}
        results = list(pool.map(lambda s: _verify_artifact(ctx, s, pom_index), staged))
    total_files = sum(res.files for res in results)
    failed, passed = [], []
    warnings: dict[str, list[str]] = {}
    for s, res in zip(staged, results):
        name = f"{s.gav}" + (f" (repo {s.repo_id})" if s.repo_id != "override" else "")
        for w in res.warnings:
            warnings.setdefault(w, []).append(name)
        if res.errors:
            failed.append(s)
            r.fail(name, f"{len(res.errors)} problem(s)", res.errors)
        else:
            passed.append(f"{name}: {res.files} files")
    if passed:
        r.ok(
            "Signatures, checksums (of the files and of their .asc) and POM coordinates",
            f"{len(passed)} of {len(staged)} artifacts ({total_files} files) valid",
            passed if ctx.args.verbose else [],
        )
    for message, names in warnings.items():
        r.warn(message, f"{len(names)} of {len(staged)} artifacts", names)
    if not failed:
        repos = ", ".join(sorted({s.repo_id for s in staged}))
        ctx.highlight(
            f"Maven: {len(staged)} artifacts ({total_files} files) in staging repo(s) {repos}: "
            "PGP signatures and MD5/SHA-1/SHA-256/SHA-512 checksums verified, POMs have the release version"
        )
    _compare_with_dist(ctx, staged)


def _discover_pattern(ctx: Context) -> str | None:
    """Regex of artifactIds to look for: the project's own, or else its required artifacts."""
    p = ctx.project
    return p.maven_discover or ("|".join(f"(?:{r})" for r in p.required_maven_artifacts) or None)


def _discover(ctx: Context, base: str, group: str = "org/apache/cassandra") -> list[Staged]:
    """Finds this project's artifacts with the release version in a large repository such as Maven Central."""
    pattern = re.compile(_discover_pattern(ctx))
    names = [n.rstrip("/") for n in net.list_dir(f"{base}{group}/") if n.endswith("/") and pattern.search(n.rstrip("/"))]

    def has_version(name: str) -> Staged | None:
        try:
            versions = net.list_dir(f"{base}{group}/{name}/")
        except net.HttpError:
            return None
        return Staged(base, "override", group, name, ctx.version) if f"{ctx.version}/" in versions else None

    with ThreadPoolExecutor(max_workers=ctx.jobs) as pool:
        return [s for s in pool.map(has_version, names) if s]


def _local_dir(ctx: Context, s: Staged) -> Path:
    return ctx.workdir / "maven" / s.repo_id / s.group_path / s.artifact / s.version


def _accepted_versions(ctx: Context) -> set[str]:
    versions = {ctx.version}
    if ctx.project.key == "sidecar":
        versions.add(f"{ctx.version}-jdk8")  # Java 8 client artifacts, staged in a separate repository
    return versions


def _cross_check(ctx: Context, staged: list[Staged]) -> None:
    r = ctx.report
    accepted = _accepted_versions(ctx)
    wrong = [s.gav for s in staged if s.version not in accepted]
    if wrong:
        r.fail("Staged artifacts have the release version", f"expected {sorted(accepted)}", wrong)
    else:
        r.ok("Staged artifacts have the release version", ", ".join(sorted(accepted)))

    empty = [s.gav for s in staged if not s.files]
    if empty:
        r.fail("Staged artifact directories are not empty", "", empty)

    staged_keys = {(s.repo_id, s.group_path, s.artifact, s.version) for s in staged}
    if ctx.maven_refs and not ctx.args.maven_base:
        missing = [
            f"{m.group_path.replace('/', '.')}:{m.artifact}:{m.version} (repo {m.repo_id})"
            for m in ctx.maven_refs
            if (m.repo_id, *m.key) not in staged_keys
        ]
        if missing:
            r.fail("Every artifact listed in the vote email is staged", "", missing)
        else:
            r.ok("Every artifact listed in the vote email is staged", f"{len(ctx.maven_refs)} artifacts")
        listed = {(m.repo_id, *m.key) for m in ctx.maven_refs}
        unlisted = sorted(f"{s.gav} (repo {s.repo_id})" for s in staged if (s.repo_id, s.group_path, s.artifact, s.version) not in listed)
        if unlisted:
            r.info("Staged artifacts not listed in the vote email", f"{len(unlisted)}", unlisted)

    names = {s.artifact for s in staged}
    for pattern in ctx.project.required_maven_artifacts:
        matches = sorted(n for n in names if re.search(pattern, n))
        if matches:
            r.ok(f"Required artifact {pattern}", ", ".join(matches))
        else:
            r.fail(f"Required artifact {pattern}", "not staged")


@dataclass
class ArtifactResult:
    files: int = 0
    errors: list[str] = field(default_factory=list)  # integrity / correctness problems: fail the check
    warnings: list[str] = field(default_factory=list)  # generic metadata issues, grouped across artifacts


def _verify_artifact(ctx: Context, s: Staged, pom_index: dict | None = None) -> ArtifactResult:
    res = ArtifactResult()
    problems = res.errors
    local = _local_dir(ctx, s)
    files = set(s.files)
    primary = sorted(f for f in files if not f.endswith(_SIDE_EXTS) and not f.startswith("maven-metadata"))
    res.files = len(primary)
    base = f"{s.artifact}-{s.version}"

    pom = f"{base}.pom"
    if pom not in files:
        problems.append(f"{pom} missing")
        packaging = "jar"
    else:
        packaging = _check_pom(ctx, s, local / pom, res, pom_index or {})
    if packaging != "pom":
        if packaging == "jar" and f"{base}.jar" not in files:
            problems.append(f"{base}.jar missing")
        for suffix in ("-sources.jar", "-javadoc.jar"):
            if f"{base}{suffix}" not in files:
                res.warnings.append(f"No {suffix} (Maven Central expects sources and javadoc jars)")

    for f in primary:
        path = local / f
        if not path.exists():
            problems.append(f"{f} not downloaded")
            continue
        if f + ".asc" not in files:
            problems.append(f"{f}.asc missing")
        elif (local / (f + ".asc")).exists():
            sig = ctx.keyring.verify(local / (f + ".asc"), path)
            ctx.record_signature(sig)
            if not sig.ok:
                problems.append(f"{f}.asc: {sig.error}")
        for algo in _REQUIRED_SUMS:
            if f"{f}.{algo}" not in files:
                problems.append(f"{f}.{algo} missing")
        _check_sums(local, files, f, problems)
        # The signature files carry checksums too (Nexus generates them); cassandra-check-release.sh checks them
        if f + ".asc" in files and (local / (f + ".asc")).exists():
            _check_sums(local, files, f + ".asc", problems)
    return res


def _check_sums(local: Path, files: set[str], f: str, problems: list[str]) -> None:
    """Verifies every checksum file present for f (md5, sha1, sha256, sha512)."""
    present = [a for a in checksums.ALGOS if f"{f}.{a}" in files and (local / f"{f}.{a}").exists()]
    if not present:
        return
    actual = checksums.digests(local / f, present)
    for algo in present:
        try:
            expected = checksums.parse((local / f"{f}.{algo}").read_text(errors="replace"), algo, f)
        except checksums.ChecksumFormatError as e:
            problems.append(f"{f}.{algo}: {e}")
            continue
        if expected != actual[algo]:
            problems.append(f"{f}.{algo} does not match")


def _text(el, path: str) -> str | None:
    found = el.find(path, _POM_NS)
    return found.text.strip() if found is not None and found.text else None


def _declares_license(root) -> bool:
    licenses = [(_text(l, "m:name") or "") + " " + (_text(l, "m:url") or "") for l in root.findall("m:licenses/m:license", _POM_NS)]
    return any("Apache" in l or "apache.org/licenses" in l for l in licenses)


def _inherits(root, pom_index: dict, declares) -> bool | None:
    """Whether a parent POM in the chain declares something (Maven inherits <licenses> and <scm>).

    True when a staged parent declares it, or the chain reaches the ASF parent POM (org.apache:apache,
    which declares the Apache License); False when the chain ends without it; None when a parent isn't
    staged so it can't be checked.
    """
    node = root
    for _ in range(8):
        parent = node.find("m:parent", _POM_NS)
        if parent is None:
            return False
        key = (_text(parent, "m:groupId"), _text(parent, "m:artifactId"), _text(parent, "m:version"))
        if key[:2] == ("org.apache", "apache"):
            return True
        path = pom_index.get(key)
        if path is None or not path.exists():
            return None
        try:
            node = ET.parse(path).getroot()
        except ET.ParseError:
            return None
        if declares(node):
            return True
    return None


def _check_pom(ctx: Context, s: Staged, pom: Path, res: ArtifactResult, pom_index: dict) -> str:
    problems = res.errors
    if not pom.exists():
        return "jar"
    try:
        root = ET.parse(pom).getroot()
    except ET.ParseError as e:
        problems.append(f"POM is not valid XML: {e}")
        return "jar"
    group = _text(root, "m:groupId") or _text(root, "m:parent/m:groupId")
    version = _text(root, "m:version") or _text(root, "m:parent/m:version")
    if _text(root, "m:artifactId") != s.artifact:
        problems.append(f"POM artifactId is {_text(root, 'm:artifactId')}")
    if group != s.group_path.replace("/", "."):
        problems.append(f"POM groupId is {group}")
    if version != s.version:
        problems.append(f"POM version is {version}")
    if not _declares_license(root) and _inherits(root, pom_index, _declares_license) is False:
        res.warnings.append("POM does not declare the Apache License")
    has_scm = lambda node: node.find("m:scm", _POM_NS) is not None  # noqa: E731
    if not has_scm(root) and _inherits(root, pom_index, has_scm) is False:
        res.warnings.append("POM has no <scm> section (Maven Central metadata requirement)")
    for dep in root.findall("m:dependencies/m:dependency", _POM_NS):
        dv = _text(dep, "m:version") or ""
        if "SNAPSHOT" in dv:
            problems.append(f"SNAPSHOT dependency {_text(dep, 'm:groupId')}:{_text(dep, 'm:artifactId')}:{dv}")
    return _text(root, "m:packaging") or "jar"


def _compare_with_dist(ctx: Context, staged: list[Staged]) -> None:
    """Cassandra also deploys the tarballs to Maven (apache-cassandra artifact); they must equal the dist ones."""
    r = ctx.report
    for s in staged:
        for f in s.files:
            if not f.endswith(".tar.gz"):
                continue
            dist = ctx.dist_files.get(f)
            maven = _local_dir(ctx, s) / f
            if dist is None or not maven.exists():
                continue
            same = checksums.digests(dist, ["sha512"]) == checksums.digests(maven, ["sha512"])
            (r.ok if same else r.fail)(f"Maven {f} is identical to the dist one", "" if same else "SHA-512 differs")

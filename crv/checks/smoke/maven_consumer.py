"""Maven consumers: throwaway projects that depend on the release's artifacts the way a user's build does.

The release's own artifacts come from the copy validate-release already verified (<workdir>/maven/<repo>/,
signatures and checksums checked), mounted read-only as file repositories with checksumPolicy=fail. Third-party
dependencies come from Maven Central. Every run starts with an empty local repository, and afterwards
provenance() checks where each artifact was resolved from.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ...context import Context
from ..maven import _accepted_versions
from .engine import Engine, Outcome, fill

IMAGE = "maven:3.9-eclipse-temurin-{jdk}"
REPO_PREFIX = "crv-"
GROUP = "org.apache.cassandra"
TEST_ARTIFACT = re.compile(r"(^|-)(test|tests|test-infra|integration|integration-tests|integration-framework)(-|_|$)")
MVN = "mvn -B -ntp -s /work/settings.xml -Dmaven.repo.local=/work/m2"


@dataclass(frozen=True)
class Artifact:
    group: str
    artifact: str
    version: str
    packaging: str
    repo: str

    @property
    def coords(self) -> str:
        """For dependency:get: group:artifact:version[:packaging]."""
        base = f"{self.group}:{self.artifact}:{self.version}"
        return base + (":pom" if self.packaging == "pom" else "")


def verified_repos(ctx: Context) -> dict[str, Path]:
    """repo id -> directory holding the copy of that staging (or released) repository the maven check of this
    run downloaded and checked. Folders cached by earlier runs are not used."""
    root = ctx.workdir / "maven"
    return {rid: root / rid for rid in sorted(ctx.maven_repos) if (root / rid).is_dir()}


def artifacts(ctx: Context) -> list[Artifact]:
    """Every artifact of this release in the verified copies, with its packaging from the POM."""
    accepted = _accepted_versions(ctx)
    found: list[Artifact] = []
    for repo, root in verified_repos(ctx).items():
        for pom in sorted(root.rglob("*.pom")):
            version_dir = pom.parent
            artifact = version_dir.parent.name
            if version_dir.name not in accepted or pom.name != f"{artifact}-{version_dir.name}.pom":
                continue
            group = ".".join(version_dir.parent.parent.relative_to(root).parts)
            text = pom.read_text(errors="replace")
            m = re.search(r"<packaging>\s*([^<\s]+)\s*</packaging>", _strip_parent(text))
            found.append(Artifact(group, artifact, version_dir.name, m.group(1) if m else "jar", repo))
    return found


def _strip_parent(pom: str) -> str:
    return re.sub(r"<parent>.*?</parent>", "", pom, flags=re.S)


def usable(arts: list[Artifact]) -> list[Artifact]:
    """What a user depends on: no test-only artifacts."""
    return [a for a in arts if not TEST_ARTIFACT.search(a.artifact)]


def settings_xml(repo_ids: list[str]) -> str:
    repos = "".join(f"""
        <repository>
          <id>{REPO_PREFIX}{escape(rid)}</id>
          <url>file:///staged/{escape(rid)}</url>
          <releases><enabled>true</enabled><checksumPolicy>fail</checksumPolicy></releases>
          <snapshots><enabled>false</enabled></snapshots>
        </repository>""" for rid in repo_ids)
    return f"""<settings>
  <profiles>
    <profile>
      <id>crv-verified</id>
      <repositories>{repos}
      </repositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>crv-verified</activeProfile></activeProfiles>
</settings>
"""


def dependency_xml(group: str, artifact: str, version: str, *, scope: str | None = None,
                   exclusions: list[tuple[str, str]] = ()) -> str:
    ex = "".join(f"<exclusion><groupId>{g}</groupId><artifactId>{a}</artifactId></exclusion>" for g, a in exclusions)
    return (f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"
            + (f"<scope>{scope}</scope>" if scope else "")
            + (f"<exclusions>{ex}</exclusions>" if ex else "") + "</dependency>")


def pom_xml(name: str, dependencies: list[str], jdk: str, processors: list[tuple[str, str, str]] = ()) -> str:
    """A consumer project. processors: annotation processor (group, artifact, version) used by the compiler."""
    level = "<maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>" \
        if jdk == "8" else f"<maven.compiler.release>{jdk}</maven.compiler.release>"
    paths = "".join(f"<path><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version></path>"
                    for g, a, v in processors)
    plugin = (f"<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
              f"<artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version>"
              f"<configuration><annotationProcessorPaths>{paths}</annotationProcessorPaths></configuration>"
              f"</plugin></plugins></build>") if processors else ""
    return f"""<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>crv.smoke</groupId>
  <artifactId>{escape(name)}</artifactId>
  <version>1</version>
  <properties><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>{level}</properties>
  <dependencies>{''.join(dependencies)}</dependencies>
  {plugin}
</project>
"""


def project_dir(run_dir: Path, name: str, sources: dict[str, Path] | None = None) -> Path:
    """A fresh consumer project directory with the given sources copied to src/main/java/."""
    d = run_dir / name
    if d.exists():
        shutil.rmtree(d)
    (d / "m2").mkdir(parents=True)
    for rel, src in (sources or {}).items():
        dest = d / "src" / "main" / "java" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return d


def run(ctx: Context, engine: Engine, project: Path, script: str, *, jdk: str, log: Path, timeout: int,
        network: str | None = None, mounts: dict[Path, str] | None = None, env: dict[str, str] | None = None) -> Outcome:
    """Runs script in maven:3.9-eclipse-temurin-<jdk> with the project at /work and $MVN set up."""
    repos = verified_repos(ctx)
    (project / "settings.xml").write_text(settings_xml(list(repos)))
    all_mounts = {project: "/work", **{path: f"/staged/{rid}:ro" for rid, path in repos.items()}, **(mounts or {})}
    body = f'MVN="{MVN}"\ncd /work\n' + script
    return engine.run(IMAGE.format(jdk=jdk), body, log=log, timeout=timeout, mounts=all_mounts, network=network, env=env)


PROGRAM = """
crv_prepare maven-plugins $MVN -q dependency:resolve-plugins
crv_retry 3 resolve $MVN -q dependency:resolve || { crv_done; exit 0; }
crv_run compile $MVN -q compile dependency:build-classpath -Dmdep.outputFile=/work/cp.txt || { crv_done; exit 0; }
CP="/work/target/classes:$(cat /work/cp.txt)"
crv_step java INFO "$(java -version 2>&1 | head -n 1)"
@BEFORE@
java $CRV_JVM_OPTS -cp "$CP" @MAIN@ @ARGS@
"""


def program(main: str, args: str, before: str = "") -> str:
    """Compiles the project and runs main, which prints the protocol itself (and CRV-DONE)."""
    return fill(PROGRAM, MAIN=main, ARGS=args, BEFORE=before)


RESOLVE = """
crv_prepare maven-plugins $MVN -q dependency:help -Ddetail=false
for coords in @COORDS@; do
  crv_retry 3 "resolve:${coords}" $MVN -q dependency:get -Dartifact="$coords" -Dtransitive=true
done
crv_done
"""


def resolve(ctx: Context, engine: Engine, run_dir: Path, name: str, arts: list[Artifact], jdk: str,
            timeout: int) -> tuple[Outcome, Path]:
    """dependency:get for each artifact, so each one's own dependency tree is resolved."""
    project = project_dir(run_dir, name)
    script = fill(RESOLVE, COORDS=" ".join(a.coords for a in arts))
    out = run(ctx, engine, project, script, jdk=jdk, log=run_dir / f"{name}.log", timeout=timeout)
    return out, project / "m2"


def provenance(ctx: Context, m2: Path) -> tuple[list[str], list[str]]:
    """(this release's artifacts resolved from elsewhere than the verified copy, SNAPSHOT versions resolved)."""
    accepted = _accepted_versions(ctx)
    elsewhere, snapshots = [], []
    group_dir = m2.joinpath(*GROUP.split("."))
    for marker in sorted(m2.rglob("_remote.repositories")):
        version_dir = marker.parent
        rel = version_dir.relative_to(m2)
        if version_dir.name.endswith("-SNAPSHOT"):
            snapshots.append(":".join([".".join(rel.parts[:-2]), rel.parts[-2], rel.parts[-1]]))
            continue
        if group_dir not in version_dir.parents or version_dir.name not in accepted:
            continue
        for line in marker.read_text(errors="replace").splitlines():
            if ">" not in line or line.startswith("#"):
                continue
            file, repo = line.split(">", 1)
            repo = repo.rstrip("=")
            if not repo.startswith(REPO_PREFIX) and file.endswith((".jar", ".pom")):
                elsewhere.append(f"{file} from '{repo or 'the local cache'}'")
    return elsewhere, sorted(set(snapshots))

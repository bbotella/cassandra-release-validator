"""Cassandra Analytics: build and publish (replicates cassandra-analytics-release/prepare_release.sh:235-247).

Differences from the official script, on purpose (see docs/parity.md):
  * the tag itself is checked out (AP:242 creates a branch at HEAD instead);
  * common artifacts are published once with -PartifactType=common, then the Spark artifacts once per
    profile in profiles/ (AP:246-247 pass no artifactType, so trunk publishes nothing);
  * each profile builds with its own JDK (AP:246 has a -P-D typo and only knows JDK 11);
  * `clean` before every build, because the profiles compile the same modules for other Scala versions.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..context import ReleaseContext
from . import step

PROFILE = re.compile(r"scala-(?P<scala>[\d.]+)-spark-(?P<spark>\d+)-jdk-(?P<jdk>\d+)\.gradle$")


def profiles(clone: Path) -> list[dict]:
    """The release profiles of the checked-out tag, Spark 3 before Spark 4 (the last build's tarballs are staged)."""
    found = []
    for path in sorted((clone / "profiles").glob("*.gradle")):
        m = PROFILE.match(path.name)
        if m:
            found.append(m.groupdict())
    return sorted(found, key=lambda p: (int(p["spark"]), p["scala"]))


@step("build-publish", project="analytics", summary="Fresh clone at the tag; build and publish to Nexus: common artifacts, then each Spark profile",
      outward=True, replicates="AP:235-247",
      you_do="after this step, Close the new staging repository/repositories at https://repository.apache.org")
def build_publish(ctx: ReleaseContext) -> str:
    v, ep = ctx.version, ctx.endpoints
    clone = ctx.workdir / "clone"
    ctx.fresh_dir(clone)
    ctx.run(["git", "clone", "-q", ep.git_url, str(clone)], f"fresh clone of {ep.git_url}", where="container", always=True)
    ctx.run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", ctx.tag], f"check out {ctx.tag}",
            where="container", cwd=clone, always=True)
    found = profiles(clone) if clone.exists() else []
    if not found and not ctx.dry_run:
        raise RuntimeError(f"no profiles/*.gradle in {ctx.tag}")

    deps = getattr(ctx.args, "dependencies", None)
    if ep.rehearsal and deps:
        for jar in Path(deps).expanduser().glob("dtest-*.jar"):
            (clone / "dependencies").mkdir(parents=True, exist_ok=True)
            shutil.copy2(jar, clone / "dependencies" / jar.name)
        print(f"   rehearsal: reusing the dtest jars from {deps}")
    else:
        ctx.run(["./scripts/build-dependencies.sh"], "build the Cassandra dtest jars (scripts/build-dependencies.sh)",
                where="container", cwd=clone, env={"CRV_JAVA_HOME": "/opt/jdk-11"})

    common = found[0] if found else {"scala": "2.12", "spark": "3", "jdk": "11"}
    runs = [("common", common)] + [("spark", p) for p in found]
    jdks = sorted({p["jdk"] for _, p in runs}, key=int)
    for kind, prof in runs:
        env = {"CRV_JAVA_HOME": f"/opt/jdk-{prof['jdk']}", "JDK_VERSION": prof["jdk"],
               "SCALA_VERSION": prof["scala"], "SPARK_VERSION": prof["spark"]}
        cmd = ["./gradlew", "--no-daemon", "clean", "assemble", "publish", f"-PartifactType={kind}",
               "-PforceSigning", "-Prelease=true", f"-Pversion={v}"]
        if ep.rehearsal:
            # a separate Gradle home: your gradle.properties (credentials, Nexus URL) is never read
            env["GRADLE_USER_HOME"] = "/root/.gradle-rehearsal"
            repo = ep.sandbox / "www" / "repositories" / f"orgapachecassandra-{jdks.index(prof['jdk']) + 1}"
            key = ctx.state.data.get("rehearsal_key", "<rehearsal key>")
            cmd += [f"-Pmaven.repository.url=file://{repo}", f"-Psigning.gnupg.keyName={key}",
                    "-Psigning.gnupg.executable=gpg"]
        what = "common artifacts" if kind == "common" else f"Spark {prof['spark']} / Scala {prof['scala']}"
        ctx.run(cmd, f"publish the {what} (JDK {prof['jdk']})", where="container", cwd=clone, env=env,
                outward=True, signing=True)

    dist = clone / "build" / "distributions"
    files = [dist / f"apache-cassandra-analytics-{v}{suffix}.tar.gz{ext}"
             for suffix in ("-src", "") for ext in ("", ".asc", ".sha256", ".sha512")]
    if ctx.dry_run:
        return ""
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise RuntimeError(f"the build did not produce {', '.join(missing)}")
    last = runs[-1][1]
    ctx.state.data.update(dist_files=[str(f) for f in files],
                          binary_profile=f"spark{last['spark']}_{last['scala']} jdk{last['jdk']}")
    return (f"{len(runs)} publishes ({', '.join(k if k == 'common' else 'spark' + p['spark'] + '_' + p['scala'] for k, p in runs)}); "
            f"binary tarball from Spark {last['spark']} / Scala {last['scala']}")

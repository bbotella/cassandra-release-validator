"""Smoke-test machinery without containers: the protocol, generated projects, choices and a fake engine."""

import argparse
import contextlib
import io
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv import cli, projects  # noqa: E402
from crv.checks import smoke  # noqa: E402
from crv.checks.smoke import analytics, cassandra, engine, gocql_driver, maven_consumer as mc, python_driver  # noqa: E402
from crv.checks.smoke.programs.sidecar_checks import edit_config  # noqa: E402
from crv.report import Report, Status  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def quiet_report() -> Report:
    return Report(verbose=False, color=False)


def statuses(r: Report) -> dict[str, Status]:
    return {c.name: c.status for c in r.checks}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class ProtocolTest(TempDirTest):
    def outcome(self, text: str, **kw) -> engine.Outcome:
        log = self.tmp / "x.log"
        log.write_text(text)
        steps, done = engine.parse(text)
        return engine.Outcome(log=log, steps=steps, done=done, **kw)

    def test_parse(self):
        steps, done = engine.parse("noise\nCRV-STEP starts OK after 12s\nCRV-STEP cqlsh FAIL exit 1: boom\r\n"
                                   "CRV-STEP java INFO openjdk 17\nCRV-DONE\n")
        self.assertTrue(done)
        self.assertEqual([(s.name, s.status, s.detail) for s in steps],
                         [("starts", "OK", "after 12s"), ("cqlsh", "FAIL", "exit 1: boom"), ("java", "INFO", "openjdk 17")])

    def test_steps_map_to_report(self):
        r = quiet_report()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = engine.report(r, "Pkg", self.outcome("CRV-STEP install OK\nCRV-STEP cqlsh-read FAIL no row\nCRV-DONE\n"))
        self.assertFalse(ok)
        self.assertEqual(statuses(r), {"Pkg: install": Status.PASS, "Pkg: cqlsh read": Status.FAIL})

    def test_a_passing_outcome_is_one_line(self):
        r = quiet_report()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = engine.report(r, "Wheel", self.outcome("CRV-STEP python INFO Python 3.12 on aarch64\nCRV-STEP install OK\n"
                                                        "CRV-STEP select OK 10 rows\nCRV-DONE\n"))
        self.assertTrue(ok)
        self.assertEqual([(c.name, c.status, c.detail) for c in r.checks],
                         [("Wheel", Status.PASS, "2 checks passed: install, select (Python 3.12 on aarch64)")])

    def test_setup_failure_is_a_warning_not_a_failure(self):
        r = quiet_report()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = engine.report(r, "Pkg", self.outcome("CRV-SETUP apt-update FAIL exit 100: mirror down\n", exit_code=0))
        self.assertFalse(ok)
        self.assertEqual(statuses(r), {"Pkg: not run": Status.WARN})
        self.assertFalse(r.failed)

    def test_missing_done_or_timeout_fails(self):
        r = quiet_report()
        with contextlib.redirect_stdout(io.StringIO()):
            engine.report(r, "A", self.outcome("CRV-STEP starts OK\n", exit_code=137))
            engine.report(r, "B", self.outcome("", timed_out=True, timeout=900))
        self.assertEqual([(c.name, c.status) for c in r.checks],
                         [("A: starts", Status.PASS), ("A", Status.FAIL), ("B", Status.FAIL)])
        self.assertIn("exit code 137", r.checks[1].detail)
        self.assertIn("timed out after 900s", r.checks[2].detail)

    def test_compatibility_failures_are_warnings(self):
        r = quiet_report()
        with contextlib.redirect_stdout(io.StringIO()):
            engine.report(r, "Compat", self.outcome("CRV-STEP bulk-write FAIL x\nCRV-DONE\n"), as_warning=True)
        self.assertEqual(statuses(r), {"Compat: bulk write": Status.WARN})

    def test_tail_shows_the_last_script_output_only(self):
        log = self.tmp / "t.log"
        log.write_text("Copying blob sha256:1\n$ podman run ... <script>\necho hi\n----\nreal output\nCRV-STEP x OK\n\nlast\n")
        self.assertEqual(engine.tail(log), ["real output", "last"])

    def test_fill_refuses_leftover_placeholders(self):
        self.assertEqual(engine.fill("a @X@ ${b}", X="1"), "a 1 ${b}")
        with self.assertRaises(ValueError):
            engine.fill("@X@ @Y@", X="1")


class PreludeTest(TempDirTest):
    """The bash helpers, run for real (no container needed)."""

    def bash(self, script: str) -> list[engine.Step]:
        import subprocess
        out = subprocess.run(["bash", "-c", engine.PRELUDE + script], capture_output=True, text=True, cwd=self.tmp,
                             env={"PATH": "/usr/bin:/bin", "CRV_SETUP_TIMEOUT": "5"}, timeout=60)
        return engine.parse(out.stdout)[0]

    def test_run_reports_the_last_output_line(self):
        steps = self.bash("crv_run ok true; crv_run bad bash -c 'echo first; echo the reason; exit 3'")
        self.assertEqual([(s.name, s.status, s.detail) for s in steps], [("ok", "OK", ""), ("bad", "FAIL", "exit 3: the reason")])

    def test_retry_succeeds_on_a_later_attempt(self):
        steps = self.bash("crv_retry 3 flaky bash -c 'n=$(cat n 2>/dev/null || echo 0); echo $((n + 1)) > n; [ $n -ge 1 ]'")
        self.assertEqual([(s.name, s.status) for s in steps], [("flaky", "OK")])
        self.assertEqual((self.tmp / "n").read_text().strip(), "2")

    def test_prepare_failure_stops_the_script_as_setup(self):
        steps = self.bash("crv_prepare mirror false; crv_step never OK")
        self.assertEqual([(s.kind, s.name, s.status) for s in steps], [("SETUP", "mirror", "FAIL")])

    def test_wait_log(self):
        steps = self.bash("echo 'INFO Starting listening for CQL clients' > l.log; crv_wait_log up 4 l.log 'listening for CQL'; "
                          "crv_wait_log down 2 l.log 'never'")
        self.assertEqual([(s.name, s.status) for s in steps], [("up", "OK"), ("down", "FAIL")])


class MavenConsumerTest(TempDirTest):
    def ctx(self, project, version, released=False):
        return argparse.Namespace(project=project, version=version, workdir=self.tmp, maven_repos={"1484", "1485"},
                                  args=argparse.Namespace(released=released, maven_base=None))

    def stage(self, repo: str, artifact: str, version: str, packaging: str | None = None) -> None:
        d = self.tmp / "maven" / repo / "org" / "apache" / "cassandra" / artifact / version
        d.mkdir(parents=True)
        pkg = f"<packaging>{packaging}</packaging>" if packaging else ""
        (d / f"{artifact}-{version}.pom").write_text(
            f"<project><parent><packaging>pom</packaging></parent><artifactId>{artifact}</artifactId>{pkg}</project>")

    def test_artifacts_and_usable(self):
        self.stage("1484", "sidecar-client", "0.5.0")
        self.stage("1485", "sidecar-client", "0.5.0-jdk8")
        self.stage("1484", "sidecar-parent", "0.5.0", "pom")
        self.stage("1484", "sidecar-test-infra", "0.5.0")
        self.stage("1484", "sidecar-client", "0.4.0")  # another version: not this release
        self.stage("1300", "sidecar-client", "0.5.0")  # cached by an earlier run, not checked by this one
        arts = mc.artifacts(self.ctx(projects.SIDECAR, "0.5.0"))
        self.assertEqual(sorted((a.artifact, a.version, a.packaging, a.repo) for a in arts), [
            ("sidecar-client", "0.5.0", "jar", "1484"), ("sidecar-client", "0.5.0-jdk8", "jar", "1485"),
            ("sidecar-parent", "0.5.0", "pom", "1484"), ("sidecar-test-infra", "0.5.0", "jar", "1484")])
        self.assertEqual(sorted(a.artifact for a in mc.usable(arts)), ["sidecar-client", "sidecar-client", "sidecar-parent"])
        self.assertIn("org.apache.cassandra:sidecar-parent:0.5.0:pom", [a.coords for a in arts])

    def test_test_artifacts_are_not_usable(self):
        names = ["java-driver-test-infra", "cassandra-analytics-integration-tests_spark3_2.12",
                 "cassandra-analytics-integration-framework_spark3_2.13", "java-driver-core", "cassandra-all"]
        arts = [mc.Artifact(mc.GROUP, n, "1", "jar", "r") for n in names]
        self.assertEqual([a.artifact for a in mc.usable(arts)], ["java-driver-core", "cassandra-all"])

    def test_settings_put_verified_repos_first_and_fail_on_checksums(self):
        xml = mc.settings_xml(["1484", "1485"])
        self.assertLess(xml.index("crv-1484"), xml.index("crv-1485"))
        self.assertIn("<url>file:///staged/1485</url>", xml)
        self.assertEqual(xml.count("<checksumPolicy>fail</checksumPolicy>"), 2)
        self.assertIn("<activeProfile>crv-verified</activeProfile>", xml)

    def test_pom_levels_and_processors(self):
        self.assertIn("<maven.compiler.target>1.8</maven.compiler.target>", mc.pom_xml("x", [], "8"))
        pom = mc.pom_xml("x", [mc.dependency_xml(mc.GROUP, "java-driver-query-builder", "4.19.4",
                                                 exclusions=[(mc.GROUP, "java-driver-core")])], "21",
                         [(mc.GROUP, "java-driver-mapper-processor", "4.19.4")])
        self.assertIn("<maven.compiler.release>21</maven.compiler.release>", pom)
        self.assertIn("<annotationProcessorPaths><path><groupId>org.apache.cassandra</groupId>"
                      "<artifactId>java-driver-mapper-processor</artifactId>", pom)
        self.assertIn("<exclusion><groupId>org.apache.cassandra</groupId><artifactId>java-driver-core</artifactId>", pom)

    def test_provenance(self):
        m2 = self.tmp / "m2"

        def resolved(path: str, lines: str) -> None:
            d = m2 / path
            d.mkdir(parents=True)
            (d / "_remote.repositories").write_text("#NOTE: internal\n" + lines)

        resolved("org/apache/cassandra/cassandra-all/5.0.9", "cassandra-all-5.0.9.jar>crv-r86305=\ncassandra-all-5.0.9.pom>crv-r86305=\n")
        resolved("org/apache/cassandra/cassandra-bridge/5.0.9", "cassandra-bridge-5.0.9.jar>central=\n")
        resolved("org/apache/cassandra/dtest-api/0.0.16", "dtest-api-0.0.16.jar>central=\n")  # a third-party version
        resolved("org/apache/cassandra/analytics/0.5-SNAPSHOT", "analytics-0.5-SNAPSHOT.jar>apache-snapshots=\n")
        elsewhere, snapshots = mc.provenance(self.ctx(projects.CASSANDRA, "5.0.9"), m2)
        self.assertEqual(elsewhere, ["cassandra-bridge-5.0.9.jar from 'central'"])
        self.assertEqual(snapshots, ["org.apache.cassandra:analytics:0.5-SNAPSHOT"])


class ChoicesTest(unittest.TestCase):
    def test_debian_release_from_depends(self):
        self.assertEqual(cassandra.debian_for("openjdk-17-jre-headless | openjdk-11-jre-headless | java17-runtime | "
                                              "java11-runtime, adduser, python3 (>= 3.6), procps"), "bookworm")  # 5.0.9
        self.assertEqual(cassandra.debian_for("openjdk-8-jre-headless | java8-runtime | openjdk-11-jre-headless | "
                                              "java11-runtime, adduser, python3"), "bullseye")
        self.assertEqual(cassandra.debian_for("openjdk-21-jre-headless | java21-runtime | openjdk-17-jre-headless"), "trixie")
        self.assertIsNone(cassandra.debian_for("openjdk-8-jre-headless | java8-runtime"))

    def test_binary_image_follows_cqlsh_python_range(self):
        script = 'if [ "$major_version" = 3 ] && [ "$minor_version" -ge 8 ] && [ "$minor_version" -le 13 ]; then'
        self.assertEqual(cassandra.cqlsh_pythons(script), (8, 13))
        self.assertEqual(cassandra.binary_image("17", (8, 13)), "eclipse-temurin:17-jdk-noble")
        self.assertEqual(cassandra.binary_image("11", (6, 11)), "eclipse-temurin:11-jdk-jammy")
        self.assertEqual(cassandra.binary_image("17", None), "eclipse-temurin:17-jdk-noble")
        self.assertIsNone(cassandra.cqlsh_pythons("#!/bin/sh\nexec python3 cqlsh.py"))

    def test_wheel_targets(self):
        t = python_driver.wheel_target
        self.assertEqual(t("cassandra_driver-3.31.0-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
                         ("python:3.12-slim", "linux/arm64"))
        self.assertEqual(t("cassandra_driver-3.31.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
                         ("python:3.11-slim", "linux/amd64"))
        for name in ("cassandra_driver-3.31.0-cp313-cp313-macosx_11_0_arm64.whl",
                     "cassandra_driver-3.31.0-cp313-cp313-win_amd64.whl",
                     "cassandra_driver-3.31.0-cp313-cp313-musllinux_1_2_x86_64.whl"):
            self.assertIsNone(t(name))

    def test_go_module(self):
        mod = "// header\nmodule github.com/apache/cassandra-gocql-driver/v2\n\ngo 1.22\n\ntoolchain go1.23.4\n"
        self.assertEqual(gocql_driver.module_info(mod), ("github.com/apache/cassandra-gocql-driver/v2", "1.22", "1.23.4"))
        self.assertEqual(gocql_driver.image_for("1.22", "1.23.4"), "golang:1.23")
        self.assertEqual(gocql_driver.image_for("1.24.0", None), "golang:1.24")
        self.assertEqual(gocql_driver.placeholder_version("github.com/apache/cassandra-gocql-driver/v2"),
                         "v2.0.0-00010101000000-000000000000")
        self.assertEqual(gocql_driver.placeholder_version("github.com/gocql/gocql"), "v0.0.0-00010101000000-000000000000")

    def test_smoke_test_selection(self):
        self.assertEqual(smoke.selected("sidecar", None)[0], ["binary", "client", "analytics", "deb", "rpm"])
        self.assertEqual(smoke.selected("sidecar", "rpm, binary"), (["binary", "rpm"], []))
        self.assertEqual(smoke.selected("analytics", "bulk,deb"), (["bulk"], ["deb"]))

    def test_unknown_smoke_test_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["--project", "sidecar", "--version", "0.4.0", "--smoke", "--smoke-tests", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("choose from binary, client, analytics, deb, rpm", err.getvalue())


class SidecarConfigTest(unittest.TestCase):
    def test_edit_shipped_configs(self):
        for name in ("sidecar-0.4.0.yaml", "sidecar-trunk.yaml"):
            text = (FIXTURES / name).read_text()
            new = edit_config(text, "a1b2c3d4e5f6")
            self.assertIn("    host: a1b2c3d4e5f6\n", new, name)
            self.assertNotIn("\n    host: localhost1\n", new, name)
            self.assertIn("  host: 0.0.0.0", new, name)  # Sidecar's own listen address is left alone
            self.assertIn('contact_points:\n    - "a1b2c3d4e5f6:9042"', new, name)
            self.assertIn("#    localhost1: localhost4", new, name)  # comments too
            self.assertIn("min_free_space_percent: 1\n", new, name)
            self.assertEqual(len(new.splitlines()), len(text.splitlines()))

    def test_config_without_the_instance_host_is_an_error(self):
        with self.assertRaises(ValueError):
            edit_config("cassandra_instances:\n  - id: 1\n    host: node1.example\n")
        with self.assertRaises(ValueError):
            edit_config((FIXTURES / "sidecar-0.4.0.yaml").read_text(), "bad host; rm -rf /")


class AnalyticsProfilesTest(TempDirTest):
    def test_from_source_profiles(self):
        folder = self.tmp / "profiles"
        folder.mkdir()
        (folder / "scala-2.13-spark-4-jdk-17.gradle").write_text('ext {\n    sparkVersion="4.0.2"\n}\n')
        (folder / "scala-2.12-spark-3-jdk-11.gradle").write_text("ext {\n    sparkVersion = '3.2.2'\n}\n")
        found = analytics.profiles(["cassandra-analytics-core_spark3_2.12", "cassandra-analytics-core_spark4_2.13",
                                    "cassandra-bridge_spark3_2.12", "cassandra-analytics-core_spark3_2.13"], self.tmp)
        self.assertEqual([(p.core, p.spark_version, p.jdk) for p in found], [
            ("cassandra-analytics-core_spark3_2.12", "3.2.2", "11"),
            ("cassandra-analytics-core_spark3_2.13", "3.2.2", "11"),  # no profile file: the fallback table
            ("cassandra-analytics-core_spark4_2.13", "4.0.2", "17")])

    def test_without_source(self):
        self.assertEqual([p.label for p in analytics.profiles(["cassandra-analytics-core_spark3_2.12"], None)],
                         ["Spark 3.2.2, Scala 2.12, JDK 11"])


class FakeEngineTest(TempDirTest):
    """smoke.run end to end for the GoCQL driver, with an engine that prints canned output."""

    def fake_engine(self) -> str:
        path = self.tmp / "fake-engine"
        path.write_text("""#!/bin/sh
case "$1" in
  info|pull|rm|exec) exit 0 ;;
  image) [ "$3" = --format ] && echo '["CASSANDRA_VERSION=5.0.9"]'; exit 0 ;;
  run)
    for a in "$@"; do [ "$a" = -d ] && { echo started; exit 0; }; done
    printf 'CRV-STEP go INFO go1.24\\nCRV-STEP resolve OK\\nCRV-STEP build OK\\nCRV-STEP connect OK\\nCRV-STEP select FAIL id 3 -> \\nCRV-DONE\\n'
    exit 0 ;;
esac
exit 1
""")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_gocql_run(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "go.mod").write_text("module github.com/apache/cassandra-gocql-driver/v2\n\ngo 1.24\n")
        r = quiet_report()
        args = argparse.Namespace(container_engine=self.fake_engine(), smoke_tests=None, smoke_timeout=30,
                                  cassandra_image=["cassandra:5.0"], jdks=None, released=False)
        ctx = argparse.Namespace(project=projects.GOCQL_DRIVER, version="2.2.0", workdir=self.tmp / "work", report=r,
                                 args=args, src_root=src, highlights=[], highlight=lambda text: None)
        with contextlib.redirect_stdout(io.StringIO()):
            smoke.run(ctx)
        got = statuses(r)
        label = "Go module github.com/apache/cassandra-gocql-driver/v2 (golang:1.24) against Cassandra 5.0.9"
        self.assertEqual(got[f"{label}: build"], Status.PASS)
        self.assertEqual(got[f"{label}: select"], Status.FAIL)
        self.assertEqual(got["Server cassandra:5.0"], Status.INFO)
        run_dir = next((self.tmp / "work" / "smoke").glob("run-*"))
        self.assertIn("replace github.com/apache/cassandra-gocql-driver/v2 => /src",
                      (run_dir / "go-cassandra-5.0" / "go.mod").read_text())
        self.assertIn('gocql "github.com/apache/cassandra-gocql-driver/v2"',
                      (run_dir / "go-cassandra-5.0" / "main.go").read_text())


if __name__ == "__main__":
    unittest.main()

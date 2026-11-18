import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv import projects  # noqa: E402
from crv.checks import runtime  # noqa: E402


class JdkSelectionTest(unittest.TestCase):
    def test_from_build_xml(self):
        xml = '<property name="java.default" value="11" />\n<property name="java.supported" value="11,17,21" />'
        self.assertEqual(runtime.supported_jdks(xml, "6.0-alpha2"), ["11", "17", "21"])

    def test_defaults_for_branches_without_java_supported(self):
        self.assertEqual(runtime.supported_jdks("<project/>", "4.1.12"), ["8", "11"])
        self.assertEqual(runtime.supported_jdks(None, "3.11.19"), ["8"])

    def test_rpm_java_packages(self):
        self.assertEqual(runtime.rpm_java("8"), "java-1.8.0-openjdk")
        self.assertEqual(runtime.rpm_java("17"), "java-17-openjdk-devel")


class ScriptTest(unittest.TestCase):
    def test_build_uses_jdk11_flag_only_on_4x(self):
        self.assertIn("ant artifacts -Duse.jdk11=true", runtime.build_script("11", "4.1.12"))
        self.assertNotIn("use.jdk11", runtime.build_script("11", "5.0.9"))
        self.assertIn("go1.24.5.linux-$ARCH.tar.gz", runtime.build_script("17", "5.0.9"))

    def test_apt_uses_signed_by_and_pins_released_version(self):
        script = runtime.apt_script("https://debian.cassandra.apache.org", "50x", "5.0.9")
        self.assertIn("deb [signed-by=/usr/share/keyrings/cassandra.gpg] https://debian.cassandra.apache.org 50x main", script)
        self.assertIn("apt-get install -y cassandra=5.0.9 cassandra-tools=5.0.9", script)
        self.assertNotIn("apt-key", script)
        self.assertIn("apt-get install -y cassandra 2>&1", runtime.apt_script("https://x/debian", "50x", None))

    def test_yum_checks_signatures(self):
        script = runtime.yum_script("11", "https://redhat.cassandra.apache.org/50x/noboolean", "5.0.9")
        for line in ("gpgcheck=1", "repo_gpgcheck=1", "baseurl=https://redhat.cassandra.apache.org/50x/noboolean",
                     "update-crypto-policies --set LEGACY", "yum install -y cassandra-5.0.9 cassandra-tools-5.0.9"):
            self.assertIn(line, script)

    def test_deb_and_rpm_scripts_start_cassandra_as_root(self):
        self.assertIn("dpkg --force-depends -i /debs/*.deb", runtime.deb_script())
        self.assertIn("rpm -i --nodeps /rpms/*.noarch.rpm", runtime.rpm_script("17"))
        for script in (runtime.deb_script(), runtime.rpm_script("17"), runtime.binary_script()):
            self.assertIn("cassandra -R -f", script)


class RunContainerTest(unittest.TestCase):
    """run_container with a fake engine that prints canned output instead of running a container."""

    def _engine(self, output: str) -> str:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = Path(d) / "fake-engine"
        path.write_text(f"#!/bin/sh\n[ \"$1\" = rm ] && exit 0\nprintf '{output}'\nsleep 30\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log = Path(d) / "out.log"
        return str(path)

    def test_success_marker_stops_early(self):
        engine = self._engine("setting up\\nINFO  Starting listening for CQL clients on /127.0.0.1:9042\\n")
        self.assertTrue(runtime.run_container(engine, "img", "true", {}, runtime.CQL_READY, self.log, 60))
        self.assertIn("Starting listening for CQL clients", self.log.read_text())

    def test_missing_marker_fails(self):
        engine = self._engine("Error: Unable to access jarfile\\n")
        path = Path(engine)
        path.write_text(path.read_text().replace("sleep 30", "exit 1"))
        self.assertFalse(runtime.run_container(engine, "img", "true", {}, runtime.CQL_READY, self.log, 60))

    def test_unknown_engine(self):
        engine, why = runtime.find_engine("no-such-engine-crv")
        self.assertIsNone(engine)
        self.assertIn("not installed", why)


class ReleasedLayoutTest(unittest.TestCase):
    def test_released_dist_has_no_packages(self):
        files = projects.CASSANDRA.expected_dist_files("5.0.9", released=True)
        self.assertEqual(len(files), 8)
        self.assertFalse(any(f.startswith(("debian/", "redhat/")) for f in files))
        self.assertEqual(projects.CASSANDRA.released_dist_url("5.0.9"), "https://dist.apache.org/repos/dist/release/cassandra/5.0.9/")


if __name__ == "__main__":
    unittest.main()

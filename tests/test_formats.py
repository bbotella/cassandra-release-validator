import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv import checksums, net, projects  # noqa: E402
from crv.pkgformats import checksum_lines, parse_deb822  # noqa: E402

SHA512 = "67bf961706cc2381e1dd67c9a6d1582029807ed6a1899daddfceebc980ada201b18b7357f59aeffb90a225ac85c8e26364912952c2e3ecda0fcb3c6d0b1981a7"


class ChecksumParseTest(unittest.TestCase):
    def test_bare_hash(self):  # Cassandra
        self.assertEqual(checksums.parse(SHA512 + "\n", "sha512", "x.tar.gz"), SHA512)

    def test_coreutils(self):  # Sidecar / Analytics
        text = f"{SHA512}  apache-cassandra-sidecar-0.4.0.tar.gz"
        self.assertEqual(checksums.parse(text, "sha512", "apache-cassandra-sidecar-0.4.0.tar.gz"), SHA512)

    def test_coreutils_wrong_file(self):
        with self.assertRaises(checksums.ChecksumFormatError):
            checksums.parse(f"{SHA512}  other.tar.gz", "sha512", "apache-cassandra-sidecar-0.4.0.tar.gz")

    def test_bsd(self):
        self.assertEqual(checksums.parse(f"SHA512 (a.tgz) = {SHA512}", "sha512", "a.tgz"), SHA512)

    def test_gpg_print_md(self):
        grouped = " ".join(SHA512.upper()[i:i + 8] for i in range(0, 128, 8))
        text = f"a.tgz: {grouped[:60]}\n        {grouped[60:]}"
        self.assertEqual(checksums.parse(text, "sha512", "a.tgz"), SHA512)

    def test_wrong_length(self):
        with self.assertRaises(checksums.ChecksumFormatError):
            checksums.parse(SHA512[:64], "sha512")

    def test_git_blob(self):
        self.assertEqual(checksums.git_blob_sha1(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


class Deb822Test(unittest.TestCase):
    def test_release_file(self):
        text = (
            "Origin: Apache Cassandra Packages\nCodename: 50x\nSHA256:\n"
            " aaaa 12 main/binary-amd64/Packages\n bbbb 34 main/binary-amd64/Packages.gz\n"
        )
        stanza = parse_deb822(text)[0]
        self.assertEqual(stanza["Codename"], "50x")
        self.assertEqual(checksum_lines(stanza["SHA256"]), [("aaaa", 12, "main/binary-amd64/Packages"), ("bbbb", 34, "main/binary-amd64/Packages.gz")])

    def test_clearsigned(self):
        text = (
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nFormat: 1.8\nSource: cassandra\n"
            "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n"
        )
        self.assertEqual(parse_deb822(text), [{"Format": "1.8", "Source": "cassandra"}])


class ProjectNamingTest(unittest.TestCase):
    def test_debian_naming(self):
        self.assertEqual(projects.deb_version("6.0-alpha2"), "6.0~alpha2")
        self.assertEqual(projects.debian_series("6.0-alpha2"), "60x")
        self.assertEqual(projects.debian_series("5.0.9"), "50x")

    def test_cassandra_expected_files(self):
        files = projects.CASSANDRA.expected_dist_files("6.0-alpha2", noboolean=False)
        self.assertIn("apache-cassandra-6.0-alpha2-bin.tar.gz.sha512", files)
        self.assertIn("debian/cassandra_6.0~alpha2_all.deb", files)
        self.assertIn("redhat/cassandra-6.0~alpha2-1.noarch.rpm", files)
        self.assertFalse(any("noboolean" in f for f in files))

    def test_sidecar_expected_files(self):
        files = projects.SIDECAR.expected_dist_files("0.4.0")
        self.assertIn("apache-cassandra-sidecar-0.4.0.tar.gz.asc", files)
        self.assertIn("debian/apache-cassandra-sidecar_0.4.0_all.deb.sha512", files)
        self.assertIn("redhat/apache-cassandra-sidecar-0.4.0.noarch.rpm.asc", files)
        self.assertEqual(len(files), 16)

    def test_sidecar_packages_with_a_release_number(self):  # CASSSIDECAR-472, from 0.5.0
        staged = ["apache-cassandra-sidecar-0.5.0.tar.gz", "debian/apache-cassandra-sidecar_0.5.0-1_all.deb",
                  "redhat/apache-cassandra-sidecar-0.5.0-1.noarch.rpm"]
        rel = projects.SIDECAR.simple_package_release("0.5.0", staged)
        self.assertEqual(rel, "1")
        files = projects.SIDECAR.expected_dist_files("0.5.0", package_release=rel)
        self.assertIn("debian/apache-cassandra-sidecar_0.5.0-1_all.deb.asc", files)
        self.assertIn("redhat/apache-cassandra-sidecar-0.5.0-1.noarch.rpm.sha512", files)
        self.assertEqual(len(files), 16)

    def test_sidecar_release_number_is_for_this_version_only(self):
        self.assertIsNone(projects.SIDECAR.simple_package_release(
            "0.5.0", ["debian/apache-cassandra-sidecar_0.4.0_all.deb", "debian/apache-cassandra-sidecar_0.5.0.1-1_all.deb"]))

    def test_analytics_expected_files(self):
        self.assertEqual(len(projects.ANALYTICS.expected_dist_files("0.5.0")), 8)

    def test_driver_naming(self):
        py, go = projects.PYTHON_DRIVER, projects.GOCQL_DRIVER
        self.assertEqual(py.rc_tag("3.31.0"), "3.31.0")
        self.assertEqual(py.src_tarball("3.31.0"), "cassandra_driver-3.31.0.tar.gz")
        self.assertEqual(py.src_top_dir("3.31.0"), "cassandra_driver-3.31.0")
        self.assertIsNone(py.bin_tarball("3.31.0"))
        self.assertEqual(py.expected_dist_files("3.31.0"), ["cassandra_driver-3.31.0.tar.gz", "cassandra_driver-3.31.0.tar.gz.asc",
                                                            "cassandra_driver-3.31.0.tar.gz.sha256", "cassandra_driver-3.31.0.tar.gz.sha512"])
        self.assertEqual((go.rc_tag("2.2.0"), go.final_tag("2.2.0")), ("v2.2.0-tentative", "v2.2.0"))
        self.assertEqual(go.src_tarball("2.2.0"), "apache-cassandra-gocql-driver-2.2.0.tar.gz")
        self.assertTrue(go.changes_heading_re("2.2.0").match("## [2.2.0]"))
        self.assertFalse(go.changes_heading_re("2.2.0").match("## [2.2.01]"))
        self.assertTrue(projects.CASSANDRA.changes_heading_re("5.0.9").match("5.0.9"))
        self.assertEqual(projects.by_name("Python Driver"), py)
        self.assertEqual(projects.by_name("cassandra-gocql-driver"), go)
        self.assertEqual(projects.by_name("Java Driver"), projects.JAVA_DRIVER)
        self.assertIsNone(projects.by_name("Node.js Driver"))

    def test_java_driver_naming(self):
        j = projects.JAVA_DRIVER
        self.assertEqual(j.src_tarball("4.19.4"), "apache-cassandra-java-driver-4.19.4-source.tar.gz")
        self.assertEqual(j.src_top_dir("4.19.4"), "apache-cassandra-java-driver-4.19.4-source")
        self.assertEqual(j.rc_tag("4.19.4"), j.final_tag("4.19.4"))
        self.assertTrue(j.changes_heading_re("4.19.4").match("### 4.19.4"))
        self.assertTrue(j.changes_heading_re("3.13.0").match("## 3.13.0"))

    def test_pom_version_ignores_parent(self):
        from crv.checks.git import _pom_version

        pom = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>org.apache</groupId>'
            "<artifactId>apache</artifactId><version>23</version></parent>"
            "<artifactId>java-driver-parent</artifactId><version>4.19.4</version></project>"
        )
        self.assertEqual(_pom_version(pom), "4.19.4")


class ListDirTest(unittest.TestCase):
    def test_svn_and_nexus_listings(self):
        svn = '<ul><li><a href="../">..</a></li><li><a href="a.tar.gz">a</a></li><li><a href="debian/">debian/</a></li></ul>'
        base = "https://repository.apache.org/content/repositories/orgapachecassandra-1/org/apache/cassandra/"
        nexus = (
            f'<a href="{base}../">..</a><a href="{base}cassandra-all/">cassandra-all/</a>'
            '<a href="https://repository.apache.org/favicon.ico">x</a><a href="?C=N;O=D">sort</a>'
        )
        orig = net.fetch_text
        try:
            net.fetch_text = lambda url: svn
            self.assertEqual(net.list_dir("https://dist.apache.org/repos/dist/dev/cassandra/5.0.9/"), ["a.tar.gz", "debian/"])
            net.fetch_text = lambda url: nexus
            self.assertEqual(net.list_dir(base), ["cassandra-all/"])
        finally:
            net.fetch_text = orig


if __name__ == "__main__":
    unittest.main()

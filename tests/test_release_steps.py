import contextlib
import io
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv import projects, vote_email  # noqa: E402
from crv.release import config, mail  # noqa: E402
from crv.release.executor import StepFailed  # noqa: E402
from crv.release.state import ReleaseState  # noqa: E402
from crv.release.steps import NotYet, analytics, cassandra, common, sidecar  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def words(text: str) -> list[str]:
    return re.sub(r"\s+", " ", text).strip().split(" ")


class VoteMailTest(unittest.TestCase):
    def test_sidecar_vote_mail_matches_the_one_sent(self):
        sent_subject, sent_body, _, _ = vote_email.read_file(str(FIXTURES / "vote-sidecar-0.4.0.txt"))
        info = vote_email.parse(sent_body, sent_subject)
        artifacts = [mail.Artifact(m.repo_id, m.group_path, m.artifact, m.version) for m in info.maven]
        ours = mail.vote_mail(projects.SIDECAR, "0.4.0", info.sha1, artifacts, attempt=1)
        subject, _, body = ours.partition("\n\n")
        self.assertEqual(subject, sent_subject)
        url = re.compile(r"^https://repository\.apache\.org/\S+$")
        self.assertEqual({w for w in words(body) if url.match(w)}, {w for w in words(sent_body) if url.match(w)})
        # same text once the Maven URLs (order) and line wrapping are set aside
        self.assertEqual([w for w in words(body) if not url.match(w)], [w for w in words(sent_body) if not url.match(w)])

    def test_cassandra_single_artifact_on_one_line_and_attempt_suffix(self):
        artifact = mail.Artifact("1479", "org/apache/cassandra", "cassandra-all", "5.0.9")
        text = mail.vote_mail(projects.CASSANDRA, "5.0.9", "b5f2a54210d541339c2e7c17a794195cac0e67c2", [artifact], attempt=2)
        self.assertTrue(text.startswith("[VOTE] Release Apache Cassandra 5.0.9 - 2nd attempt\n"))
        self.assertIn("Maven Artifacts: https://repository.apache.org/content/repositories/orgapachecassandra-1479/"
                      "org/apache/cassandra/cassandra-all/5.0.9/", text)
        self.assertIn("Debian and RPM packages", text)

    def test_analytics_mail_lists_every_staged_repo(self):
        artifacts = [mail.Artifact("1484", "org/apache/cassandra", "cassandra-analytics-core_spark3_2.12", "0.5.0"),
                     mail.Artifact("1485", "org/apache/cassandra", "cassandra-analytics-core_spark4_2.13", "0.5.0")]
        text = mail.vote_mail(projects.ANALYTICS, "0.5.0", "f" * 40, artifacts, attempt=1)
        self.assertIn("orgapachecassandra-1485/org/apache/cassandra/cassandra-analytics-core_spark4_2.13/0.5.0/", text)
        self.assertNotIn("/spark/", text)  # the official template's wrong path
        self.assertNotIn("Debian", text)

    def test_release_mail_wording(self):
        self.assertIn("release of Apache Cassandra Sidecar version 0.5.0", mail.release_mail(projects.SIDECAR, "0.5.0"))
        self.assertIn("is the first release[1] on the 6.1 series", mail.release_mail(projects.CASSANDRA, "6.1.0"))
        self.assertIn("is a bug fix release[1] on the 5.0 series", mail.release_mail(projects.CASSANDRA, "5.0.10"))


class AnalyticsProfilesTest(unittest.TestCase):
    def test_profiles_spark3_before_spark4(self):
        d = Path(tempfile.mkdtemp()) / "profiles"
        d.mkdir()
        for name in ("scala-2.13-spark-4-jdk-17.gradle", "scala-2.12-spark-3-jdk-11.gradle",
                     "scala-2.13-spark-3-jdk-11.gradle", "README.md"):
            (d / name).write_text("")
        self.assertEqual([(p["spark"], p["scala"], p["jdk"]) for p in analytics.profiles(d.parent)],
                         [("3", "2.12", "11"), ("3", "2.13", "11"), ("4", "2.13", "17")])


def fake_ctx(rehearsal=False, thread=None, run=None):
    tmp = Path(tempfile.mkdtemp())
    state = ReleaseState.load(tmp / "state.json", "analytics", "0.6.0", rehearsal)
    notes: list[str] = []
    return SimpleNamespace(
        project=projects.ANALYTICS, version="0.6.0", state=state, notes=notes, note=notes.append,
        endpoints=config.endpoints(projects.ANALYTICS, "0.6.0", rehearsal),
        args=SimpleNamespace(thread=thread, source=str(tmp)), tag="0.6.0-tentative",
        dry_run=False, run=run or (lambda *a, **k: ""))


class TallyTest(unittest.TestCase):
    def setUp(self):
        self._fetch, self._lists = common.vote_email.fetch_thread, common.vote_email.fetch_from_lists

    def tearDown(self):
        common.vote_email.fetch_thread, common.vote_email.fetch_from_lists = self._fetch, self._lists

    def thread(self, hours_ago: float, *votes: str):
        replies = [common.vote_email.Reply(str(i), f"voter{i}@apache.org", 0, "Re: [VOTE]", "", v, "binding")
                   for i, v in enumerate(votes)]
        opened = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(time.time() - hours_ago * 3600))
        common.vote_email.fetch_thread = lambda tid: ("[VOTE] Release ...", replies)
        common.vote_email.fetch_from_lists = lambda tid: (None, None, None, opened)

    def tally(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return common.tally(fake_ctx(thread="abc123"))

    def test_passed_vote_is_done(self):
        self.thread(73, "+1", "+1", "+1")
        self.assertIn("3 × +1", self.tally())

    def test_open_vote_is_not_yet(self):
        self.thread(10, "+1", "+1", "+1")
        with self.assertRaisesRegex(NotYet, "needs 72 h"):
            self.tally()

    def test_a_minus_one_is_not_yet(self):
        self.thread(80, "+1", "+1", "+1", "-1")
        with self.assertRaisesRegex(NotYet, "There is a -1"):
            self.tally()

    def test_too_few_plus_ones_is_not_yet(self):
        self.thread(80, "+1", "+1")
        with self.assertRaisesRegex(NotYet, "three binding"):
            self.tally()

    def test_last_vote_of_each_voter_counts(self):
        replies = [common.vote_email.Reply("1", "a@apache.org", 0, "", "", "-1", None),
                   common.vote_email.Reply("2", "a@apache.org", 1, "", "", "+1", "binding")]
        opened = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(time.time() - 80 * 3600))
        common.vote_email.fetch_thread = lambda tid: ("", replies)
        common.vote_email.fetch_from_lists = lambda tid: (None, None, None, opened)
        with self.assertRaisesRegex(NotYet, "1 × \\+1 .* 0 × -1"):
            self.tally()

    def test_rehearsal_without_a_thread_is_taken_as_passed(self):
        ctx = fake_ctx(rehearsal=True)
        self.assertIn("taken as passed", common.tally(ctx))


class RerollTest(unittest.TestCase):
    def reroll(self, *, tag_on_remote: bool, local_tag: bool, staged: bool, rehearsal: bool = False):
        ran: list[str] = []

        def run(cmd, description, **kw):
            line = " ".join(cmd)
            ran.append(line)
            if cmd[:2] == ["git", "ls-remote"]:
                return "abc\trefs/tags/0.6.0-tentative" if tag_on_remote else ""
            if cmd[:3] == ["git", "tag", "-l"]:
                return "0.6.0-tentative" if local_tag else ""
            if cmd[:2] == ["svn", "info"] and not staged:
                raise StepFailed(description, 1, ["svn: E170000: URL doesn't exist"])
            return ""

        ctx = fake_ctx(rehearsal=rehearsal, run=run)
        original = common.push_target
        common.push_target = lambda c: "origin"
        try:
            summary = common.reroll(ctx)
        finally:
            common.push_target = original
        return ran, summary, ctx

    def test_removes_everything_that_exists(self):
        ran, summary, ctx = self.reroll(tag_on_remote=True, local_tag=True, staged=True)
        self.assertIn("git push origin :refs/tags/0.6.0-tentative", ran)
        self.assertIn("git tag -d 0.6.0-tentative", ran)
        self.assertTrue(any(r.startswith("svn rm ") for r in ran))
        self.assertEqual(ctx.state.attempt, 2)
        self.assertIn("2nd attempt", summary)

    def test_failed_build_has_nothing_staged(self):
        ran, summary, ctx = self.reroll(tag_on_remote=True, local_tag=True, staged=False)
        self.assertFalse(any(r.startswith("svn rm ") for r in ran))
        self.assertIn("git push origin :refs/tags/0.6.0-tentative", ran)
        self.assertEqual(ctx.state.attempt, 2)

    def test_nothing_to_remove(self):
        ran, summary, ctx = self.reroll(tag_on_remote=False, local_tag=False, staged=False)
        self.assertEqual([r for r in ran if r.startswith(("git push", "git tag -d", "svn rm"))], [])
        self.assertIn("removed nothing", summary)
        self.assertTrue(ctx.notes)

    def test_rehearsal_never_touches_local_tags(self):
        ran, _, _ = self.reroll(tag_on_remote=True, local_tag=True, staged=True, rehearsal=True)
        self.assertFalse(any(r.startswith("git tag") for r in ran))


class SidecarDtestJarTest(unittest.TestCase):
    def test_jar_name_from_test_common(self):
        clone = Path(tempfile.mkdtemp())
        (clone / "test-common").mkdir()
        (clone / "test-common" / "build.gradle").write_text(
            'ext.dtestJar = System.getenv("DTEST_JAR") ?: "dtest-4.1.11.jar" // latest supported\n')
        self.assertEqual(sidecar.dtest_jar_name(clone), "dtest-4.1.11.jar")
        self.assertIsNone(sidecar.dtest_jar_name(Path(tempfile.mkdtemp())))

    def test_package_names(self):
        self.assertEqual(sidecar.package_names("0.5.0", "1"), ["apache-cassandra-sidecar_0.5.0-1_all.deb",
                                                              "apache-cassandra-sidecar-0.5.0-1.noarch.rpm"])
        self.assertEqual(sidecar.package_names("0.4.0", None), ["apache-cassandra-sidecar_0.4.0_all.deb",
                                                               "apache-cassandra-sidecar-0.4.0.noarch.rpm"])

    def test_upload_check(self):
        f = Path(tempfile.mkdtemp()) / "x.deb"
        f.write_bytes(b"package")
        sha = "4a4fb7f3a3bd38e30bd3f2e65ebd2b41e1e01ab8f0f67d0f7fe47fa1b1a0f2b0"
        good = f"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n"
        self.assertIsNone(sidecar.upload_mismatch(f, good))
        self.assertIn("bytes", sidecar.upload_mismatch(f, "HTTP/1.1 200 OK\r\nContent-Length: 3\r\n"))
        self.assertIn("SHA-256", sidecar.upload_mismatch(f, good + f"X-Checksum-Sha256: {sha}\r\n"))
        import hashlib
        right = hashlib.sha256(b"package").hexdigest()
        self.assertIsNone(sidecar.upload_mismatch(f, good + f"X-Checksum-Sha256: {right}\r\n"))

    def test_branch_that_builds_the_jar(self):
        self.assertEqual(sidecar.dtest_branch("dtest-4.1.11.jar"), "cassandra-4.1")
        self.assertEqual(sidecar.dtest_branch("dtest-5.0.7.jar"), "cassandra-5.0")
        self.assertEqual(sidecar.dtest_branch("dtest-5.1.jar"), "trunk")


class RehearsalNexusReleaseTest(unittest.TestCase):
    def test_staging_repositories_are_merged_like_a_release(self):
        sandbox = Path(tempfile.mkdtemp())
        base = sandbox / "www" / "repositories"
        for repo, artifact, version in (("1", "sidecar-client", "0.5.0-jdk8"), ("2", "sidecar-client", "0.5.0"),
                                        ("2", "sidecar-server", "0.5.0")):
            d = base / f"orgapachecassandra-{repo}" / "org" / "apache" / "cassandra" / artifact / version
            d.mkdir(parents=True)
            (d / f"{artifact}-{version}.pom").write_text("<project/>")
        ctx = fake_ctx(rehearsal=True)
        ctx.project, ctx.version = projects.SIDECAR, "0.5.0"
        ctx.endpoints = SimpleNamespace(rehearsal=True, sandbox=sandbox)
        ctx.state.data["staging_repos"] = ["1", "2"]
        summary = common.nexus_release(ctx)
        self.assertIn("2 artifacts found", summary)  # the JDK 8 repo alone has no sidecar-server 0.5.0
        central = sandbox / "www" / "central" / "org" / "apache" / "cassandra"
        self.assertTrue((central / "sidecar-client" / "0.5.0-jdk8").is_dir())
        self.assertTrue((central / "sidecar-server" / "0.5.0").is_dir())


CHANGELOG = """cassandra (5.0.10) UNRELEASED; urgency=medium

  * New release

 -- Past Manager <past@apache.org>  Tue, 28 Jul 2026 12:57:11 +0200

cassandra (5.0.9) unstable; urgency=medium

  * New release

 -- Past Manager <past@apache.org>  Tue, 28 Jul 2026 12:57:11 +0200
"""


class CassandraChangelogTest(unittest.TestCase):
    WHEN = "Fri, 20 Nov 2026 14:00:00 +0100"

    def test_like_dch_release(self):
        new = cassandra.release_changelog(CHANGELOG, "5.0.10", "Jane Doe <jane@apache.org>", self.WHEN)
        lines = new.splitlines()
        self.assertEqual(lines[0], "cassandra (5.0.10) unstable; urgency=medium")
        self.assertEqual(lines[4], " -- Jane Doe <jane@apache.org>  Fri, 20 Nov 2026 14:00:00 +0100")
        self.assertEqual(lines[6:], CHANGELOG.splitlines()[6:])  # older entries untouched

    def test_pre_release_uses_the_debian_version(self):
        text = CHANGELOG.replace("(5.0.10)", "(6.0~alpha3)", 1)
        new = cassandra.release_changelog(text, "6.0-alpha3", "Jane Doe <jane@apache.org>", self.WHEN)
        self.assertTrue(new.startswith("cassandra (6.0~alpha3) unstable;"))

    def test_a_re_roll_only_refreshes_the_trailer(self):
        once = cassandra.release_changelog(CHANGELOG, "5.0.10", "A <a@apache.org>", self.WHEN)
        twice = cassandra.release_changelog(once, "5.0.10", "A <a@apache.org>", "Sat, 21 Nov 2026 10:00:00 +0100")
        self.assertEqual(twice.splitlines()[0], "cassandra (5.0.10) unstable; urgency=medium")
        self.assertIn("Sat, 21 Nov 2026", twice.splitlines()[4])

    def test_the_top_entry_must_be_this_version(self):
        with self.assertRaisesRegex(RuntimeError, "dch -v 5.0.11"):
            cassandra.release_changelog(CHANGELOG, "5.0.11", "A <a@apache.org>", self.WHEN)


class CassandraBuildImageTest(unittest.TestCase):
    def clone(self, wrapper: str, body: str) -> Path:
        clone = Path(tempfile.mkdtemp())
        (clone / ".build" / "docker").mkdir(parents=True)
        (clone / ".build" / "docker" / wrapper).write_text(body)
        (clone / ".build" / "docker" / "bullseye-build.docker").write_text("FROM debian:bullseye\n")
        (clone / "build.xml").write_text('<project><property name="java.default" value="11" />'
                                         '<property name="java.supported" value="11,17" /></project>')
        return clone

    def test_wrappers_name_the_image_and_script(self):
        clone = self.clone("build-debian.sh", '$(dirname -- "$0")/_docker_run.sh bullseye-build.docker docker/_build-debian.sh $1\n')
        self.assertEqual(cassandra.wrapper_target(clone, "build-debian.sh"), ("bullseye-build.docker", "docker/_build-debian.sh"))
        red = self.clone("build-redhat.sh", '$(dirname -- "$0")/_docker_run.sh almalinux-build.docker docker/_build-redhat.sh "${java_version}" ${rpm_dist}\n')
        self.assertEqual(cassandra.wrapper_target(red, "build-redhat.sh"), ("almalinux-build.docker", "docker/_build-redhat.sh"))

    def test_image_is_named_after_the_dockerfile_md5(self):
        import hashlib
        clone = self.clone("build-debian.sh", "")
        digest = hashlib.md5(b"FROM debian:bullseye\n").hexdigest()
        self.assertEqual(cassandra.image_name(clone, "bullseye-build.docker"), f"docker.io/apache/cassandra-bullseye-build:{digest}")
        self.assertEqual(cassandra.build_property(clone, "java.default"), "11")

    def test_4_x_has_no_in_tree_scripts(self):
        with self.assertRaisesRegex(RuntimeError, "4.0 and 4.1"):
            cassandra.wrapper_target(Path(tempfile.mkdtemp()), "build-debian.sh")


class CassandraFinishPackagesTest(unittest.TestCase):
    def test_apt_repository_only_and_every_yum_file(self):
        root = Path(tempfile.mkdtemp())
        released = {
            "debian/cassandra_5.0.10_all.deb": b"top-level copy",  # depth 1: not uploaded (CF:192)
            "debian/dists/50x/InRelease": b"r",
            "debian/pool/main/c/cassandra/cassandra_5.0.10_all.deb": b"deb",
            "redhat/cassandra-5.0.10-1.noarch.rpm": b"rpm",
            "redhat/repodata/repomd.xml": b"x",
            "redhat/noboolean/cassandra-5.0.10-1.noarch.rpm": b"nb",
        }
        uploads, removed = [], []

        def run(cmd, description, **kw):
            if cmd[:2] == ["svn", "export"]:
                for rel, data in released.items():
                    (Path(cmd[-1]) / rel).parent.mkdir(parents=True, exist_ok=True)
                    (Path(cmd[-1]) / rel).write_bytes(data)
            elif "PUT" in cmd:
                uploads.append(cmd[-1].split("/artifactory/", 1)[1])
            elif cmd[:2] == ["svn", "rm"]:
                removed.append(cmd[-1].rsplit("/", 1)[1])
            return ""

        ctx = SimpleNamespace(
            project=projects.CASSANDRA, version="5.0.10", dry_run=False, notes=[], note=lambda t: None, run=run,
            workdir=root / "work", fresh_dir=lambda p: p.mkdir(parents=True, exist_ok=True),
            endpoints=SimpleNamespace(rehearsal=True, sandbox=root / "sandbox", dist_release="file:///svn/release/cassandra/"))
        (root / "sandbox" / "www").mkdir(parents=True)
        cassandra.finish_packages(ctx)
        self.assertEqual(sorted(uploads), sorted([
            "cassandra/dists/50x/InRelease", "cassandra-deb/dists/50x/InRelease",
            "cassandra/pool/main/c/cassandra/cassandra_5.0.10_all.deb",
            "cassandra-deb/pool/main/c/cassandra/cassandra_5.0.10_all.deb",
            "cassandra-rpm/50x/cassandra-5.0.10-1.noarch.rpm", "cassandra-rpm/50x/repodata/repomd.xml",
            "cassandra-rpm/50x/noboolean/cassandra-5.0.10-1.noarch.rpm"]))
        self.assertEqual(removed, ["debian", "redhat"])  # only after every upload


class PreflightVersionTest(unittest.TestCase):
    def test_real_release_needs_the_version_commit(self):
        self.assertEqual(common._version_check("sidecar", "0.5-SNAPSHOT", "0.5.0", False, "gradle.properties")[0], "FAIL")

    def test_rehearsal_runs_before_the_version_commit(self):
        status, _, detail = common._version_check("sidecar", "0.5-SNAPSHOT", "0.5.0", True, "gradle.properties")
        self.assertEqual(status, "WARN")
        self.assertIn("-Pversion=0.5.0", detail)

    def test_cassandra_rehearsal_still_needs_it(self):  # its build does not pass -Dbase.version
        self.assertEqual(common._version_check("cassandra", "5.0.10-SNAPSHOT", "5.0.10", True, "build.xml")[0], "FAIL")

    def test_matching_version_passes(self):
        self.assertEqual(common._version_check("analytics", "0.6.0", "0.6.0", False, "gradle.properties")[0], "PASS")


class ParityTest(unittest.TestCase):
    def test_reviewed_hashes_are_read_from_parity_md(self):
        reviewed = common._reviewed()
        self.assertEqual(len(reviewed), 6)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in reviewed.values()))

    def test_drift_detection(self):
        import hashlib

        original = common.net.fetch_bytes
        try:
            reviewed = common._reviewed()
            changed = "cassandra-analytics-release/prepare_release.sh"

            def fake(url: str) -> bytes:
                path = url.removeprefix(common.UPSTREAM)
                return b"changed" if path == changed else b"same"

            common.net.fetch_bytes = fake
            original_reviewed = common._reviewed
            common._reviewed = lambda: {p: (hashlib.sha256(b"same").hexdigest()) for p in reviewed}
            self.assertEqual(common._upstream_drift(), [changed])
        finally:
            common.net.fetch_bytes = original
            common._reviewed = original_reviewed


if __name__ == "__main__":
    unittest.main()

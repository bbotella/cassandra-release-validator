import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv import vote_email  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> vote_email.VoteInfo:
    subject, body, _, _ = vote_email.read_file(str(FIXTURES / name))
    return vote_email.parse(body, subject)


class ParseVoteEmailTest(unittest.TestCase):
    def test_cassandra(self):
        info = load("vote-cassandra-5.0.9.txt")
        self.assertEqual(info.project.key, "cassandra")
        self.assertEqual(info.version, "5.0.9")
        self.assertEqual(info.sha1, "b5f2a54210d541339c2e7c17a794195cac0e67c2")
        self.assertEqual(info.tag, "5.0.9-tentative")
        self.assertEqual(info.git_repo, "cassandra")
        self.assertEqual(info.dist_url, "https://dist.apache.org/repos/dist/dev/cassandra/5.0.9/")
        self.assertEqual(info.staging_repos, ["1479"])
        self.assertEqual([(m.artifact, m.version) for m in info.maven], [("cassandra-all", "5.0.9")])
        self.assertEqual(info.problems, [])

    def test_cassandra_prerelease(self):
        info = load("vote-cassandra-6.0-alpha2.txt")
        self.assertEqual(info.version, "6.0-alpha2")
        self.assertEqual(info.tag, "6.0-alpha2-tentative")
        self.assertEqual(info.dist_url, "https://dist.apache.org/repos/dist/dev/cassandra/6.0-alpha2/")

    def test_sidecar_with_two_staging_repos(self):
        info = load("vote-sidecar-0.4.0.txt")
        self.assertEqual(info.project.key, "sidecar")
        self.assertEqual(info.version, "0.4.0")
        self.assertEqual(info.staging_repos, ["1472", "1471"])
        self.assertEqual(len(info.maven), 13)
        jdk8 = [m for m in info.maven if m.version == "0.4.0-jdk8"]
        self.assertEqual(len(jdk8), 4)
        self.assertTrue(all(m.repo_id == "1471" for m in jdk8))
        self.assertEqual(info.dist_url, "https://dist.apache.org/repos/dist/dev/cassandra/cassandra-sidecar/0.4.0/")

    def test_analytics(self):
        info = load("vote-analytics-0.4.0.txt")
        self.assertEqual(info.project.key, "analytics")
        self.assertEqual(info.sha1, "a32e5e6fc1f0f4356f06db00a7124e651b6d30ca")
        self.assertEqual(len(info.maven), 24)
        self.assertEqual({m.group_path for m in info.maven}, {"org/apache/cassandra"})

    def test_subject_drives_project_and_inconsistencies_are_reported(self):
        body = (FIXTURES / "vote-sidecar-0.4.0.txt").read_text().split("\n", 2)[2]
        info = vote_email.parse(body, "[VOTE] Release Apache Cassandra Analytics 0.4.0")
        self.assertEqual(info.project.key, "analytics")
        self.assertTrue(any("dist URL" in p for p in info.problems))

    def test_wrapped_url_is_rejoined_but_consecutive_urls_are_not(self):
        base = "https://repository.apache.org/content/repositories/orgapachecassandra-1479/org/apache/cassandra/"
        body = f"Maven:\n{base}\ncassandra-all/5.0.9/\n{base}cassandra-all/5.0.9/\n{base}apache-cassandra/5.0.9/\n"
        info = vote_email.parse(body, "[VOTE] Release Apache Cassandra 5.0.9")
        self.assertEqual([(m.artifact, m.version) for m in info.maven], [("cassandra-all", "5.0.9"), ("apache-cassandra", "5.0.9")])

    def test_python_driver(self):
        info = load("vote-python-driver-3.31.0.txt")
        self.assertEqual(info.project.key, "python-driver")
        self.assertEqual(info.version, "3.31.0")
        self.assertEqual(info.sha1, "25393c2e709a353aebb8219c8bfbe60accb7bcc2")  # "Git commit sha1 for this release:"
        self.assertEqual(info.tag, "3.31.0")  # releases/tag/3.31.0, not the 3.31.0rc1 TestPyPI tag linked later
        self.assertEqual(info.git_repo, "cassandra-python-driver")
        self.assertEqual(info.dist_url, "https://dist.apache.org/repos/dist/dev/cassandra/cassandra-python-driver/3.31.0/")
        self.assertEqual(info.staging_repos, [])
        self.assertEqual(info.problems, [])

    def test_gocql_driver(self):
        info = load("vote-gocql-driver-2.2.0.txt")
        self.assertEqual(info.project.key, "gocql-driver")
        self.assertEqual(info.version, "2.2.0")
        self.assertEqual(info.sha1, "856b92387679da40584c8f982d6f43d3a42ad2b7")
        self.assertEqual(info.tag, "v2.2.0-tentative")
        self.assertEqual(info.dist_url, "https://dist.apache.org/repos/dist/dev/cassandra/cassandra-gocql-driver/2.2.0/")
        self.assertEqual(info.problems, [])

    def test_java_driver_4x_and_3x(self):
        for name, version, sha, repo in (
            ("vote-java-driver-4.19.4.txt", "4.19.4", "b98e35eb055837d4396f207c4e4e9ab428da1e9d", "1482"),
            ("vote-java-driver-3.13.0.txt", "3.13.0", "0f5f0ad3a6b3f44672c4a18fca2845c885901988", "1481"),
        ):
            info = load(name)
            self.assertEqual(info.project.key, "java-driver")
            self.assertEqual(info.version, version)
            self.assertEqual(info.sha1, sha)
            self.assertEqual(info.tag, version)  # the vote is on the final tag
            self.assertEqual(info.staging_repos, [repo])
            self.assertEqual(info.maven, [])  # only the repository root is linked
            self.assertEqual(info.dist_url, f"https://dist.apache.org/repos/dist/dev/cassandra/cassandra-java-driver/{version}/")
            self.assertEqual(info.problems, [])

    def test_unsupported_driver_is_not_mistaken_for_cassandra(self):
        info = load("vote-nodejs-driver-4.10.0.txt")
        self.assertIsNone(info.project)
        self.assertEqual(info.unsupported, "Node.js Driver")
        self.assertEqual(info.version, "4.10.0")

    def test_pasted_text_without_subject_header(self):
        body = (FIXTURES / "vote-cassandra-5.0.9.txt").read_text().split("\n", 2)[2]
        info = vote_email.parse(body)
        self.assertEqual(info.project.key, "cassandra")
        self.assertEqual(info.version, "5.0.9")

    def test_eml(self):
        eml = FIXTURES / "vote-cassandra-5.0.9.txt"
        raw = (
            "From: Someone <someone@example.org>\r\n"
            "Subject: [VOTE] Release Apache Cassandra 5.0.9 - 2nd attempt\r\n"
            "Message-ID: <x@example.org>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            + eml.read_text().split("\n", 2)[2].replace("=", "=3D").replace("\n", "\r\n")
        )
        path = Path(self._tmp()) / "vote.eml"
        path.write_text(raw)
        subject, body, sender, _ = vote_email.read_file(str(path))
        self.assertEqual(subject, "[VOTE] Release Apache Cassandra 5.0.9 - 2nd attempt")
        self.assertIn("someone@example.org", sender)
        self.assertEqual(vote_email.parse(body, subject).sha1, "b5f2a54210d541339c2e7c17a794195cac0e67c2")

    def test_reply_text_and_vote(self):
        body = "+1 (binding)\n\nChecked sigs.\n\nOn Tue, Jun 9, 2026 at 7:20 AM Paulo Motta <pa...@apache.org>\nwrote:\n> -1 quoted\n"
        text = vote_email.own_text(body)
        self.assertEqual(text, "+1 (binding)\n\nChecked sigs.")
        self.assertEqual(vote_email.parse_vote(text), ("+1", "binding"))
        spanish = "-1 non-binding, see CASSANALYTICS-1\n\nEl jue, 12 nov 2026 a las 13:55, X (<\ndev@cassandra.apache.org>) escribió:\n> +1"
        self.assertEqual(vote_email.parse_vote(vote_email.own_text(spanish)), ("-1", "non-binding"))
        self.assertEqual(vote_email.parse_vote("The vote passes with 6 +1s (+4 binding)"), (None, None))
        self.assertEqual(vote_email.parse_vote("Verified sha 0.4.0-tentative, all good"), (None, None))
        self.assertEqual(vote_email.parse_vote("Non-binding +1."), ("+1", "non-binding"))
        self.assertEqual(vote_email.parse_vote("+1, checked sigs"), ("+1", None))
        self.assertEqual(vote_email.parse_vote("bumped to +1.5 seconds and +10 nodes"), (None, None))

    def test_thread_ids(self):
        tid = "hknnwz6bj83tbpbwj1volyb6bsgvxztv"
        self.assertEqual(vote_email.thread_id_from(tid), tid)
        self.assertEqual(vote_email.thread_id_from(f"https://lists.apache.org/thread/{tid}"), tid)
        self.assertEqual(vote_email.thread_id_from(f"https://lists.apache.org/thread/{tid}?dev@cassandra.apache.org"), tid)
        self.assertIsNone(vote_email.thread_id_from("vote.eml"))

    def _tmp(self) -> str:
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main()

"""The mail drafts of a release, with the wording of the official scripts.

The Maven artifact list comes from crawling the staging repositories, never from a hand-written list,
so new modules (Spark 4, a new Sidecar adapter) are always included. Differences from the official
text are listed in docs/parity.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..projects import NEXUS_REPOS, STAGING_PREFIX, Project, debian_series
from .state import ordinal

VOTE_RULES = (
    "The vote will be open for 72 hours (longer if needed). Everyone who has tested the build is invited to vote. "
    "Votes by PMC members are considered binding. A vote passes if there are at least three binding +1s and no -1's."
)


@dataclass(frozen=True)
class Artifact:
    repo_id: str
    group_path: str
    artifact: str
    version: str

    def url(self, nexus_repos: str = NEXUS_REPOS) -> str:
        return f"{nexus_repos.rstrip('/')}/{STAGING_PREFIX}{self.repo_id}/{self.group_path}/{self.artifact}/{self.version}/"


def name(project: Project) -> str:
    return project.display.removeprefix("Apache ")  # "Cassandra Analytics"


def vote_subject(project: Project, version: str, attempt: int) -> str:
    suffix = f" - {ordinal(attempt)} attempt" if attempt > 1 else ""
    return f"[VOTE] Release {project.display} {version}{suffix}"


def _body(project: Project, version: str, sha1: str, artifacts: list[Artifact], closing: str) -> str:
    tag = project.rc_tag(version)
    gh = project.github_url
    lines = [f"Proposing the test build of {name(project)} {version} for release." if closing == VOTE_RULES
             else f"The test build of {name(project)} {version} is available.", ""]
    lines += [f"sha1: {sha1}", f"Git: {gh}/tree/{tag}"]
    urls = sorted({a.url() for a in artifacts})
    if project.key == "cassandra" and len(urls) == 1:
        lines.append(f"Maven Artifacts: {urls[0]}")
    else:
        lines.append("Maven Artifacts:")
        lines += urls
    lines.append("")
    dist = f"https://dist.apache.org/repos/dist/dev/cassandra/{project.dist_subdir}{version}/"
    if project.packages:
        lines.append(f"The Source and Build Artifacts, and the Debian and RPM packages and repositories, are available here: {dist}")
    else:
        lines.append(f"The Source and Build Artifacts and repositories, are available here: {dist}")
    lines += ["", closing, "", f"[1]: CHANGES.txt: {gh}/blob/{tag}/CHANGES.txt", f"[2]: NEWS.txt: {gh}/blob/{tag}/NEWS.txt"]
    return "\n".join(lines) + "\n"


def vote_mail(project: Project, version: str, sha1: str, artifacts: list[Artifact], attempt: int) -> str:
    return f"{vote_subject(project, version, attempt)}\n\n" + _body(project, version, sha1, artifacts, VOTE_RULES)


def announce_mail(project: Project, version: str, sha1: str, artifacts: list[Artifact]) -> str:
    """The optional '[ANNOUNCE] ... test artifact available' mail sent before the vote."""
    subject = f"[ANNOUNCE] {project.display} {version} test artifact available"
    return f"{subject}\n\n" + _body(project, version, sha1, artifacts,
                                     "A vote of this test build will be initiated within the next couple of days.")


def release_mail(project: Project, version: str) -> str:
    final = project.final_tag(version)
    gh = project.github_url
    jira = {"cassandra": "CASSANDRA", "sidecar": "CASSSIDECAR", "analytics": "CASSANALYTICS"}[project.key]
    lines = [f"[RELEASE] {project.display} {version} released", "",
             f"The Cassandra team is pleased to announce the release of {project.display} version {version}.", ""]
    if project.key == "cassandra":
        series = ".".join(version.split("-")[0].split(".")[:2])
        kind = "the first release" if version.split("-")[0].endswith(".0") else "a bug fix release"
        lines += [
            "Apache Cassandra is a fully distributed database. It is the right choice when you need scalability "
            "and high availability without compromising performance.", "",
            " https://cassandra.apache.org/", "",
            "Downloads of source and binary distributions are listed in our download section:", "",
            " https://cassandra.apache.org/_/download.html", "",
            f"This version is {kind}[1] on the {series} series. As always, please pay attention to the release "
            "notes[2] and let us know[3] if you were to encounter any problem.", "",
            f"Debian packages: https://debian.cassandra.apache.org ({debian_series(version)}); "
            f"RedHat packages: https://redhat.cassandra.apache.org/{debian_series(version)}/", "",
        ]
    else:
        lines += [
            "Downloads of source and binary distributions are available here:", "",
            f" https://dlcdn.apache.org/cassandra/{project.dist_subdir}{version}/", "",
            "The Maven artifacts can be found at:", "",
            " https://repo.maven.apache.org/maven2/org/apache/cassandra/", "",
            "These will be mirrored to other repositories.", "",
            "As always, please review the changes[1] and pay attention to the release notes[2]. Let us know[3] "
            "if you were to encounter any problem.", "",
        ]
    lines += ["Enjoy!", "", f"[1]: CHANGES.txt {gh}/blob/{final}/CHANGES.txt", f"[2]: NEWS.txt {gh}/blob/{final}/NEWS.txt",
              f"[3]: https://issues.apache.org/jira/browse/{jira}"]
    return "\n".join(lines) + "\n"

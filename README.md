# Cassandra release validator

Checks a release candidate of **Apache Cassandra**, **Cassandra Sidecar**, **Cassandra Analytics**, the
**Cassandra Python driver**, the **Cassandra GoCQL driver** or the **Cassandra Java driver** (4.x and 3.x)
before you vote on it. The Node.js driver is not supported. Give it the `[VOTE]` email from dev@cassandra.apache.org (a link is enough).
It reads the sha1, git tag, dist URL and Nexus staging repositories from the email, downloads
everything that was staged, and checks it against what the
[release process](https://cassandra.apache.org/_/development/release_process.html) and the
`prepare_release.sh` scripts in [apache/cassandra-builds](https://github.com/apache/cassandra-builds)
are expected to produce. For the drivers, which cassandra-builds doesn't build, it checks against what
their release managers stage (see `crv/projects.py`).

```bash
./validate-release --list
```

```bash
./validate-release https://lists.apache.org/thread/<id-of-the-vote-thread>
```

You get a PASS/WARN/FAIL line per check, a summary, an exit code, and a Markdown report under
`reports/`. The report has a "Summary (suitable for the vote reply)" section to paste into your reply.

## Using it from Claude Code

The repository ships a Claude Code skill,
[`cassandra-release-manager`](.claude/skills/cassandra-release-manager/SKILL.md). It makes Claude act
as an experienced Cassandra release manager. It finds the vote, reads what other voters already
reported, runs this tool, decides which findings block the release (using ASF policy and the
findings known from earlier releases), offers the checks the tool can't do, and drafts your vote
reply. It never sends anything to the list.

The skill is picked up automatically when Claude Code runs in this repository. To use it from any
directory, for example while working in your cassandra checkout, link it into your personal skills:

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/.claude/skills/cassandra-release-manager" ~/.claude/skills/cassandra-release-manager
```

Then ask things like:

* "anything on the cassandra dev list I need to vote on this week?"
* "check https://lists.apache.org/thread/... and draft my +1 if it's good"
* "I'm RM for Analytics 0.5.0, staging repos 1484 and 1485. Is it ready for the vote email?"

The skill's test cases are in `.claude/skills/cassandra-release-manager/evals/`. With Claude Code
v2.1.269+, running `claude plugin eval . --allow-tools Write,Edit,Bash` from the skill's directory
re-runs them with and without the skill. These are real, billed model runs.

## Requirements

* Python 3.9+ (standard library only, nothing to `pip install`)
* `gpg` (GnuPG 2.x) and `git`
* Disk space: everything is downloaded once to `~/.cache/cassandra-release-validator`. A Cassandra
  release with its deb/rpm repositories is ~1 GB, and the Analytics binary tarball alone is ~500 MB.
* Optional: a JDK for `--smoke`; `ant` (Cassandra) or a JDK (Sidecar's `gradlew`) for `--build`.

## Usage

```text
validate-release [source] [options]

source                 lists.apache.org thread URL or id, a saved .eml / text file, or '-' (stdin)
--list                 list the recent [VOTE] Release threads and exit
--thread-votes THREAD  show every reply in a vote thread, the vote it casts and a tally, and exit
--latest               validate the most recent supported [VOTE] Release thread
--smoke                also start the binary tarball (Cassandra / Sidecar) and check it comes up
--build                also build from the source tarball
--java-home DIR        JDK for --smoke / --build
--runtime              Cassandra: the cassandra-check-release.sh matrix in containers, per supported JDK
--released             validate a published release (dist/release, Maven Central, public deb/rpm repos)
--skip GROUP           skip git | dist | packages | source | binary | maven (repeatable)
-v, --verbose          show progress and every item of each check
```

The vote email can come from:

* a lists.apache.org link: `./validate-release https://lists.apache.org/thread/hknnwz6bj83tbpbwj1volyb6bsgvxztv`
* the newest vote: `./validate-release --latest`
* a file saved from your mail client ("Show original" / "Save as .eml"), or plain pasted text:
  `./validate-release vote.eml`, or `pbpaste | ./validate-release -`

**Manual mode**: you don't need an email. This is useful as a release manager, to check the staged
artifacts before sending the vote:

```bash
./validate-release --project analytics --version 0.5.0 --staging-repo 1484 --staging-repo 1485
```

Add `--sha1 <commit>` to also check the tag against an expected commit.

### Checking a published release (`--released`)

`--released` checks a version that is already out, as `cassandra-check-release.sh released <version>`
does. It checks dist/release, the final tag and Maven Central, and for Cassandra the public
repositories at https://debian.cassandra.apache.org and https://redhat.cassandra.apache.org (filtered to
that version, since they hold the whole series):

```bash
./validate-release --project cassandra --version 5.0.9 --released
```

### Runtime matrix in containers (`--runtime`)

`--runtime` runs what [`cassandra-check-release.sh`](https://github.com/apache/cassandra-builds/pull/32)
runs, for Apache Cassandra. For every JDK in `java.supported` of the tag's `build.xml` (8 and 11 for
4.x), each in a throwaway container:

| Test | Image | Success |
|---|---|---|
| `build`: `ant artifacts` from the source tarball | `eclipse-temurin:<jdk>-jdk` | `BUILD SUCCESSFUL` |
| `binary`: start the binary tarball | `eclipse-temurin:<jdk>-jdk` | `Starting listening for CQL clients on` |
| `deb`: `dpkg -i` the .deb files, start | `eclipse-temurin:<jdk>-jdk` | same |
| `apt`: install from the staged (or public) apt repository | `eclipse-temurin:8-jdk`, else `debian:bullseye-slim` | same |
| `rpm`: `rpm -i` the .rpm files (and noboolean), start | `almalinux:9` | same |
| `yum`: install from the yum repository with `gpgcheck`/`repo_gpgcheck` (and noboolean) | `almalinux:9` | same |

It needs Docker or Podman (`podman machine start` on macOS) and takes a long time: a source build
takes several minutes per JDK. Narrow it with `--jdks 17` or `--runtime-tests binary,deb,rpm`.
Every container's output is kept in `<workdir>/runtime/*.log`. It differs from the script on purpose:
the JDKs come from `build.xml`, it uses a `signed-by` keyring instead of the removed `apt-key`, and
installs from public repositories pin the exact version. It runs code from the release, so it is
opt-in.

### Re-validating an old release

After a vote passes, the RC's staging locations disappear: the `-tentative` tag is removed, the
dist/dev directory moves to dist/release (without the deb/rpm repos), and the Nexus repository is
released to Maven Central. The SVN history still has the staged directory, so you can point the
tool at it. This is how the tool itself was tested:

```bash
./validate-release https://lists.apache.org/thread/5bkxfo0h67n6hqf8n8dx7m12728s66rz \
    --dist-url 'https://dist.apache.org/repos/dist/!svn/bc/86615/dev/cassandra/5.0.9/' \
    --tag cassandra-5.0.9 --maven-base https://repo1.maven.org/maven2/
```

(`!svn/bc/<revision>` is any svn revision at which the RC was still in dist/dev.)

## What is checked

| Area | Checks |
|---|---|
| Vote email | sha1, git tag link, dist URL, staging repos and Maven artifact list are present and agree on project and version |
| Git | the `<version>-tentative` tag exists on gitbox and points at the sha1 from the email; the final tag doesn't exist yet; version in `build.xml` / `gradle.properties`; `CHANGES.txt` (and for Cassandra `NEWS.txt`, `debian/changelog`) have the version |
| Dist area | every file `prepare_release.sh` stages is present (src and bin tarballs, `.asc`, `.sha256`, `.sha512`; Cassandra's apt and yum repos including `noboolean` when the branch has it; Sidecar's `.deb` / `.rpm`); nothing unexpected; no MD5/SHA-1 files; every signature and checksum is valid |
| Signatures | verified with an **isolated, throwaway keyring** that contains only the official [KEYS](https://dist.apache.org/repos/dist/release/cassandra/KEYS) file; all signatures come from one key; the key is RSA 4096 |
| Packages (Cassandra) | apt `InRelease` / `Release.gpg` signatures → `Release` hashes → `Packages` / `Sources` hashes → `.deb`, `.dsc` and source tarball in `pool/`; signed `.changes` / `.dsc` and the files they list; yum `repomd.xml` signature → repodata hashes → RPM checksums and versions; **embedded RPM signatures** |
| Packages (all) | `.deb` control and `.rpm` header name/version/release (read in Python, so no `dpkg`/`rpm` is needed) |
| Source tarball | top-level directory name; LICENSE and NOTICE; **every file is byte-identical to the git tag** (git blob hashes, including submodules and symlinks the build dereferenced; files that differ only by CRLF line endings are a separate warning); files not in git; compiled code (`.jar`, `.class`, …); Apache license headers in source files (a light RAT approximation) |
| Drivers | Python: sdist and wheels (wheel metadata version, LICENSE/NOTICE inside, Python versions and platforms covered); GoCQL: source tarball only; Java: source tarball and every artifact in the Nexus staging repo (POM `<licenses>`/`<scm>` inherited from a staged parent count). No binary-tarball or package checks. A tag that exists only on the GitHub mirror and not on gitbox is a warning |
| Binary tarball | top-level directory; LICENSE and NOTICE; key files (`bin/cassandra`, `lib/apache-cassandra-<v>.jar`, …); jars carry the release version; no SNAPSHOT jars; `Implementation-Version` of the Cassandra jar |
| Maven staging | each staging repo is open and crawled; every artifact listed in the email is staged; every file has a valid `.asc` and matching `.md5` / `.sha1` (plus `.sha256` / `.sha512` when present), and so does every `.asc`; POM coordinates match; no SNAPSHOT dependencies; license, `<scm>`, sources and javadoc jars present (warnings) |
| `--smoke` | Cassandra starts, `nodetool version` reports the version and `nodetool status` shows the node `UN`; Sidecar starts and `/api/v1/__health` returns OK |
| `--build` | Cassandra `ant jar`, Sidecar `./gradlew assemble` from the source tarball |
| `--runtime` | Cassandra, per supported JDK in containers: source build, binary tarball, .deb and .rpm packages, apt and yum repositories (see above) |

### What the results mean

* **FAIL**: something is wrong with the artifacts or doesn't match the vote email, e.g. a bad
  signature or checksum, a missing artifact, a source file that differs from git, or a wrong version.
  This is usually a reason to raise it on the thread, and often to vote -1.
* **WARN**: worth a look, or worth mentioning in your reply, but often not a blocker. Examples:
  generated files in the source tarball, POMs without `<scm>`, missing license headers.
* **INFO**: context, e.g. files intentionally excluded from the source tarball.

The exit code is `0` with no failures, `1` with failures, and `2` for usage errors. Downloads are
cached by svn revision / staging repo, so re-running is fast; `--refresh` re-downloads.

### What it does not do

The tool doesn't replace judgement. Things voters often still do by hand:

* look at `CHANGES.txt` / `NEWS.txt` for the release content;
* run the full Apache RAT (`ant rat-check`, or the Gradle RAT task) and the test suite;
* install the Sidecar `.deb` / `.rpm` in a container and start the service (`--runtime` covers Cassandra);
* build Cassandra Analytics, which first builds Cassandra itself (`./scripts/build-dependencies.sh`).

## Safety

Everything downloaded is treated as untrusted:

* signatures are checked with a temporary GnuPG home that holds only the KEYS file. Your own keyring
  is never read or changed.
* tarballs are extracted with Python's `tarfile` `data` filter, which rejects absolute paths, `..`
  traversal and links that escape the destination.
* nothing from the release is executed unless you pass `--smoke`, `--build` or `--runtime` (`--runtime`
  only inside throwaway containers).

## Keeping it up to date

The expected layout of each project is in [`crv/projects.py`](crv/projects.py). It is derived from
`cassandra-release/`, `cassandra-sidecar-release/` and `cassandra-analytics-release/` in
cassandra-builds. If those scripts change what they stage, update the matching `Project` entry
(dist file names, required Maven artifacts, required binary files).

Code layout:

```text
validate-release        entry point
.claude/skills/         the cassandra-release-manager Claude Code skill
crv/cli.py              argument parsing and orchestration
crv/vote_email.py       vote email parsing, lists.apache.org client
crv/projects.py         per-project conventions
crv/checks/*.py         git, dist, packages, source, binary, maven, smoke
crv/gpg.py              isolated keyring + signature verification
crv/pkgformats.py       .deb / .rpm / Debian control file readers
```

Run the unit tests with:

```bash
python3 -m unittest discover -s tests
```

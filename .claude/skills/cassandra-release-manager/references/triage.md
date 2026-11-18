# Triaging validate-release findings

Contents:
1. Is it new? Checking the previous release
2. Finding-by-finding guide
3. Known long-standing findings (late 2026 baseline)
4. Manual checks: what changed, new dependencies, tarball vs Maven jars, JIRA vs CHANGES.txt, key provenance

Severity words used below:
- **blocker**: -1 material. Raise it on the thread; the RM should re-roll.
- **mention**: put it in the reply's "Notes (not blocking)" and suggest a JIRA ticket.
- **context**: explain it to the user; usually nothing to say on the list.

## 1. Is it new? Checking the previous release

Something every release has had is rarely a reason to block this one, but a regression is. Before calling
something a blocker, or even a mention, check the previous release of the same project and branch.

Find the previous version:

```bash
curl -s https://dist.apache.org/repos/dist/release/cassandra/ | grep -o 'href="[^"]*/"'                        # current Cassandra releases
curl -s https://archive.apache.org/dist/cassandra/ | grep -o 'href="[0-9][^"]*/"' | tail -20                    # all Cassandra releases
curl -s https://archive.apache.org/dist/cassandra/cassandra-sidecar/ | grep -o 'href="[0-9][^"]*/"'             # Sidecar
curl -s https://archive.apache.org/dist/cassandra/cassandra-analytics/ | grep -o 'href="[0-9][^"]*/"'           # Analytics
```

Cheap targeted checks (stream the tarball listing instead of downloading and extracting):

```bash
# Is a file present in the previous source/binary tarball?
curl -sL https://archive.apache.org/dist/cassandra/cassandra-sidecar/0.3.0/apache-cassandra-sidecar-0.3.0.tar.gz | tar tzf - | grep -i notice
# Did the previous POMs have <scm>?
curl -s https://repo1.maven.org/maven2/org/apache/cassandra/sidecar-client/0.3.0/sidecar-client-0.3.0.pom | grep -c '<scm>'
```

For a full comparison, re-validate the previous release with the svn-history recipe in SKILL.md
("Re-validating a release whose vote has already passed").

## 2. Finding-by-finding guide

### Vote email and git

| Finding | Severity | Notes |
|---|---|---|
| Tag `<v>-tentative` not found | blocker if the vote is open | The RM didn't push it, or the vote is over (tentative tags are deleted after release; check `--list` for a `[RESULT]`). |
| sha1 in the email ≠ tag | blocker | The email describes a different commit than what was tagged and built. Often a re-roll whose email wasn't updated. Ask the RM which one is meant. |
| Final tag already exists | context | The release was already finalized; nothing to vote on. |
| Version in build.xml / gradle.properties ≠ version | blocker | prepare_release.sh refuses to run in that case, so the tag was made by hand. Check the jar names and Implementation-Version too. |
| CHANGES.txt has no section for the version | mention | Users read CHANGES.txt to know what is in the release. |
| NEWS.txt has no heading for the version | context | Normal for patch releases without upgrade notes. |
| debian/changelog top entry ≠ version | blocker for packages | The .deb would carry the wrong version. Cross-check "Package metadata". |
| Email inconsistent (dist URL / repo for another project or version) | mention, or blocker if the artifacts really differ | Usually a copy-paste error in the email. Say which link is wrong. |

### Dist area and signatures

| Finding | Severity | Notes |
|---|---|---|
| Expected artifact missing | blocker | Unless the RM said packages are still being uploaded (prepare_release.sh `-d` / `-r`). Then re-run later. |
| Unexpected files | mention | Often leftovers from an earlier attempt. Ask the RM to remove them, since anything in dist/dev becomes part of the release when it is moved. |
| MD5 / SHA-1 checksum files | mention | ASF distribution policy: supply SHA-256/512 and don't supply MD5/SHA-1. |
| Bad signature | blocker | The file changed after signing, or the signature is for another file. |
| `signing key ... is not in KEYS` | blocker | Users can't verify the release. The RM's key must be added to dist/release/cassandra/KEYS (by a PMC member) before the vote can pass. |
| Checksum mismatch / malformed checksum file | blocker | |
| More than one signing key | mention | Fine if every key is in KEYS, but unusual. Ask why. |
| Key expired, or RSA < 4096 | mention | The Cassandra release process asks for a 4096-bit RSA key. |

### Packages

| Finding | Severity | Notes |
|---|---|---|
| APT/YUM metadata doesn't match the packages | blocker for packages | `apt`/`yum` would refuse to install from the repository. |
| Embedded RPM signature invalid | blocker for RPMs | |
| RPM Release tag empty | mention | Known for Sidecar (CASSSIDECAR-472, deferred in the 0.4.0 vote). |
| .deb / .rpm version ≠ release version | blocker for packages | |

### Source release

This is what the ASF actually releases, so it gets the strictest look.

| Finding | Severity | Notes |
|---|---|---|
| Files differ from the git tag | blocker | The source release must be the tagged source. Look at the diff (`git show <sha>:<path>` from the cached bare repo `~/.cache/cassandra-release-validator/<p>-<v>/git/<repo>.git` vs the extracted file under `extract/src-*/`). Line-ending or keyword-expansion differences are still worth reporting. |
| Extra files not in git: generated text (version files, `dependency-reduced-pom.xml`, `*.properties`) | mention | Harmless but sloppy. The build should produce them rather than ship them. |
| Extra files not in git: compiled code | blocker | |
| Compiled code that is also in git (`gradle-wrapper.jar`, `maven-wrapper.jar`) | mention | ASF policy says source releases must not contain compiled code. Wrapper jars are a long-running grey area that many projects (including these) ship. Mention it only if it is new. |
| Other compiled code (`.jar`, `.class`, `.so`) | blocker | |
| Files in git but not in the tarball | context | Usually intentional excludes (`.gitignore`, CI config, submodule metadata). Check that nothing like a LICENSE for bundled code is among them. |
| LICENSE / NOTICE missing in the source release | blocker | |
| Missing Apache license headers | mention | A heuristic, not RAT. Generated files are usually the cause. Real source files without headers would fail the project's own RAT check. |
| NOTICE copyright year not current | mention | Open-ended ranges ("2009-") are accepted by the tool. |

### Binary (convenience) release

| Finding | Severity | Notes |
|---|---|---|
| NOTICE missing | mention (policy issue) | ASF guidance wants LICENSE and NOTICE in every distribution, binary included. Long-standing in Sidecar binary tarballs, so mention it and suggest a JIRA rather than -1, unless it is new. |
| SNAPSHOT jars | blocker | The release would depend on unreleased code. |
| No jar carries the release version / Implementation-Version wrong | blocker | Built with the wrong version. |
| Expected files missing (`bin/cassandra`, `lib/apache-cassandra-<v>.jar`, ...) | blocker | |

### Maven staging

| Finding | Severity | Notes |
|---|---|---|
| Staging repository not accessible | blocker while voting | The RM didn't "Close" it in Nexus (it's private until closed), or it was dropped or already released. |
| Artifact listed in the email but not staged | blocker | |
| Missing or invalid `.asc` / checksum | blocker | |
| POM coordinates or version wrong | blocker | |
| SNAPSHOT dependency in a POM | blocker | |
| Maven tarball differs from the dist tarball | blocker | They come from the same build and must be identical. |
| POM without `<scm>` | mention | Long-standing for Sidecar and Analytics; Cassandra's POMs have it. |
| No `-sources.jar` / `-javadoc.jar` | mention | Long-standing for the shaded `*-vertx-client-all` artifacts. |
| Staged artifacts not listed in the email | context | Fine, as long as the release scripts intended them. |

## 3. Known long-standing findings (late 2026 baseline)

Observed when validating these releases. Treat them as the baseline: a finding on this list is "not
new", a finding that isn't on it deserves a closer look, and one that disappeared is worth a thank-you.

- **Cassandra 5.0.9, 6.0-alpha2**: `pylib/cqlshlib/serverversion.py` is generated into the source
  tarball, without a license header. 6.0 also ships `.build/git/git-hooks/post-switch/100-update-submodules.sh`
  (not in git) and `modules/accord/gradle/wrapper/gradle-wrapper.jar` (from the accord submodule).
  Git symlinks (`redhat/noboolean/*`, `.build/run-ci.d/run_ci.py`) are stored as regular files; the tool
  reports this as INFO and treats it as identical.
- **Sidecar 0.4.0**: the binary tarball has no NOTICE. The RPM Release tag is empty (CASSSIDECAR-472).
  The source tarball contains the generated `scripts/dependency-reduced-pom.xml` and
  `server-common/src/main/resources/sidecar.version`, plus `scripts/.mvn/wrapper/maven-wrapper.jar` and
  the gradle wrapper jar. POMs have no `<scm>`. `sidecar-vertx-client-all` has no sources/javadoc jars.
- **Analytics 0.5.0 RC**: the source tarball contains the generated
  `cassandra-analytics-core/src/main/resources/cassandra-analytics-build.properties` and
  `scripts/dependency-reduced-pom.xml`. POMs have no `<scm>`. `analytics-sidecar-vertx-client-all` has
  no sources/javadoc jars. Spark 3 artifacts are staged in one Nexus repository and Spark 4 artifacts in
  a second one. The binary tarball is the Spark 4 / Scala 2.13 build. Its Spark 4 jars are identical to
  repository 1485, but its 8 Scala-independent jars (analytics-sidecar-*, cassandra-analytics-common,
  cassandra-analytics-sidecar-client) come from a different build run than the ones in repository 1484.
  `analytics-sidecar-vertx-client-all` bundles Jackson 2.18.2 in the tarball and 2.12.7 in Nexus. In JIRA,
  fix version `0.5` included tickets with no commit in the release (CASSANALYTICS-160, -192, -194, -197,
  -200), open tickets (-108, -127, -161, -201), and CHANGES.txt entries without the fix version
  (-155, -168, -196, -217, -220).

## 4. Manual checks

### What changed since the previous release

The tool's cached git repository is a depth-1 fetch, so it has no history. Use the user's own clone
(often `~/dev/<repo>`) or GitHub:

```bash
git -C ~/dev/cassandra fetch --no-tags https://gitbox.apache.org/repos/asf/cassandra.git \
    refs/tags/cassandra-5.0.8:refs/tags/cassandra-5.0.8 refs/tags/5.0.9-tentative:refs/tags/5.0.9-tentative
git -C ~/dev/cassandra log --oneline cassandra-5.0.8..5.0.9-tentative
```

Or open `https://github.com/apache/<repo>/compare/<previous-final-tag>...<version>-tentative`. Compare
the result with the new `CHANGES.txt` section. Every user-visible change should be listed, and for a
patch release nothing should look like a new feature or a risky upgrade change without a NEWS.txt note.

### New bundled dependencies

The point is to spot third-party code that wasn't in the previous release and check that its license
is acceptable for an ASF distribution. Category A (Apache, MIT, BSD, ...) is fine. Category B (weak
copyleft such as EPL, MPL, CDDL) is fine in binary form only. Category X (GPL, AGPL, SSPL, ...) is never
fine. See https://www.apache.org/legal/resolved.html.

How the projects document this today (checked on 5.0.9 / Sidecar 0.4.0 / Analytics 0.5.0): the binary
tarballs' LICENSE.txt is the Apache License, plus, for Cassandra, a "SUBCOMPONENTS" section about
*source files* copied into Cassandra. There is no per-jar license list. So don't demand one for a new jar
when previous releases had none. Do raise a new dependency whose license is category X or unclear, or
one that needs attribution (a non-Apache license whose terms require its notice to be reproduced).

Diff the jar names against the previous release, writing the lists to a scratch directory:

```bash
W=~/.cache/cassandra-release-validator/cassandra-5.0.9
SCRATCH=$(mktemp -d)   # or the session's scratchpad directory
NEW=$(find "$W"/extract -maxdepth 2 -mindepth 2 -type d -path '*/bin-*' | head -1)
find "$NEW" -name '*.jar' -path '*/lib/*' -exec basename {} \; | sort > "$SCRATCH/new-jars.txt"
curl -sL https://archive.apache.org/dist/cassandra/5.0.8/apache-cassandra-5.0.8-bin.tar.gz \
    | tar tzf - | grep '/lib/.*\.jar$' | xargs -n1 basename | sort > "$SCRATCH/old-jars.txt"
diff "$SCRATCH/old-jars.txt" "$SCRATCH/new-jars.txt"
```

A changed version of an existing dependency only matters if its license changed (some projects have
relicensed between versions). For each new name, look up its license, e.g. from the POM on Maven
Central: `curl -s https://repo1.maven.org/maven2/<group path>/<artifact>/<version>/<artifact>-<version>.pom | grep -A3 '<license>'`.

### Binary tarball vs Maven jars (`scripts/compare-bin-maven.py`)

For every jar in the binary tarball that has the same name as a staged Maven artifact (Cassandra's
`lib/apache-cassandra-<v>.jar` is matched with `cassandra-all`), it reports `same` or `DIFFERENT`. It
compares the content of every file inside the jar, ignoring the manifest and Maven metadata. For
differences it also reports which shaded dependency versions differ (from the bundled `pom.properties`)
and whether the classes target a different Java release.

How to read it:
- **All `same`**: the tarball and Maven artifacts come from the same build. This is the normal case for
  Cassandra (5.0.9: `apache-cassandra-5.0.9.jar` identical to `cassandra-all`).
- **Bundled dependency versions differ**: users get different third-party code from the tarball and
  from Maven. Mention it and suggest a JIRA ticket. It becomes a blocker only if one side bundles a
  version with a known security issue or a different license.
- **Only class files differ, same Java target**: two separate build runs produced the jars, so the
  code is very likely the same but nobody can prove it from the bytes. Worth a short note and a
  question to the RM.
- Jars in the tarball that match no Maven artifact are third-party dependencies, and aren't listed.

### JIRA fix version vs CHANGES.txt (`scripts/jira-vs-changes.py`)

JIRA project keys: CASSANDRA, CASSSIDECAR, CASSANALYTICS. Version names don't always match the release
version (Analytics uses `0.5` for 0.5.0); the script tries the obvious variants and otherwise lists the
recent versions so you can pass `--jira-version`.

How to read the three lists:
- **Resolved with this fix version but not in CHANGES.txt**: often fine. Test-only, CI and build
  tickets usually don't get a CHANGES.txt entry. The script deepens the cached clone back to the
  previous release tag and marks each ticket "commit in release" or "NO commit in release" by searching
  the commit messages in between. "commit in release" plus a test or CI summary is noise. "NO commit
  in release" has two explanations:
  - the fix lives in another repository (cassandra-builds, cassandra-dtest, the website, CI config):
    normal (e.g. CASSANDRA-20672, a release-script change, for 5.0.9);
  - the change landed somewhere else, such as a feature branch or trunk only, so the fix version is
    wrong and the RM should move it (e.g. CASSANALYTICS-160/-192/-194/-197/-200 for 0.5.0, from the
    mutation-tracking work).
- **Still open with this fix version**: these need moving to the next version before the JIRA version
  is released (part of the post-vote steps). The release ticket itself (e.g. "Cassandra Analytics
  0.5.0 release") is expected to be open.
- **In CHANGES.txt but this fix version is not set**: the ticket shipped, but JIRA will not show it
  in the release notes. Easy to fix: the RM or the ticket's assignee sets the fix version. Placeholder
  versions such as `5.0.x` or a bare `5.0` are what the release process asks the RM to replace with the
  actual release version.

None of these block a release. Report them to the RM as a short list. If the user isn't the RM, a
direct message to the RM is friendlier than a vote reply.

### Signing key provenance (`scripts/keys-history.py <fingerprint>`)

It prints the recent KEYS commits, the key's creation date, and the commit that added it. Only PMC
members can commit to dist/release, so the author of that commit is the PMC member who vouched for
adding the key. Normal patterns:
- An established key ("already in KEYS ... not newly added"): nothing to say.
- A key created and added shortly before the RC, in a commit that references a ticket (e.g. "Add
  Saranya Krishnakumar's gpg key for CASSANDRA-21898"), by a PMC member: normal for a first-time release
  manager. Optionally thank them in the reply.
- A key added without a ticket or an obvious reason, or a uid that doesn't match the RM: ask on the
  thread. Signatures by a key nobody can vouch for are worth nothing.

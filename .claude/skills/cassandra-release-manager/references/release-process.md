# Cassandra release lifecycle (release manager view)

Sources: https://cassandra.apache.org/_/development/release_process.html and the scripts in
https://github.com/apache/cassandra-builds (`cassandra-release/`, `cassandra-sidecar-release/`,
`cassandra-analytics-release/`). When the user has a local clone (often `~/dev/cassandra-builds`), read
the script there before giving step-by-step advice, because the scripts change and they are the truth.

Contents:
1. Roles and prerequisites
2. Per-project facts
3. Prepare and stage
4. Validate, then call the vote
5. The vote
6. Failed vote / re-roll
7. Finish and announce

## 1. Roles and prerequisites

- Any **committer** can prepare a release and call the vote. Only a **PMC member** can finish it
  (finish_release.sh, promoting the Nexus repository, the announcement). Only PMC votes are binding.
- The RM's GPG key must be RSA 4096, published to a keyserver, and present in
  https://dist.apache.org/repos/dist/release/cassandra/KEYS. A PMC member adds it; ask through a JIRA
  ticket. Without that, voters can't verify the signatures, and the vote can't pass.
- Cassandra packaging needs a Debian-based OS with `svn git ant devscripts reprepro rpmsign docker
  createrepo_c mvn`. Sidecar and Analytics need `svn git shasum` and Gradle signing/Nexus credentials in
  `~/.gradle/gradle.properties`. Cassandra needs Nexus credentials in `~/.m2/settings.xml`.

## 2. Per-project facts

| | Cassandra | Sidecar | Analytics |
|---|---|---|---|
| Script | `cassandra-release/prepare_release.sh [-v] <version>` | `cassandra-sidecar-release/prepare_release.sh <version> <java8_home> <java11_home>` | `cassandra-analytics-release/prepare_release.sh <version> <java11_home>` |
| Run from | the cassandra checkout, on the release branch | the cassandra-sidecar checkout | the cassandra-analytics checkout |
| Version must match | `base.version` in build.xml | `version=` in gradle.properties | `version=` in gradle.properties |
| RC tag | `<version>-tentative` | `<version>-tentative` | `<version>-tentative` |
| Final tag | `cassandra-<version>` | `cassandra-sidecar-<version>` | `cassandra-analytics-<version>` |
| dist/dev | `dev/cassandra/<version>/` | `dev/cassandra/cassandra-sidecar/<version>/` | `dev/cassandra/cassandra-analytics/<version>/` |
| Staged in dist | `-bin`/`-src` tarballs (+ .asc/.sha256/.sha512); `debian/` signed apt repo (`dists/<MAJMIN>x`, `pool/`, .changes/.dsc); `redhat/` yum repo with signed repodata; `redhat/noboolean/` on branches that have it (not 6.0) | src + bin tarballs; `debian/*_all.deb`, `redhat/*.noarch.rpm`, each with .asc/.sha256/.sha512 | src + bin tarballs only |
| Nexus | one staging repo (`cassandra-all`) | two: Java 11 artifacts, and `-jdk8` client artifacts | Spark 3 artifacts (Scala 2.12 + 2.13), and in 0.5.0 a second repo with Spark 4 (Scala 2.13) |

The analytics script on cassandra-builds trunk still lists Maven URLs under `org/apache/cassandra/spark/`
and two Scala-specific staging repos. The real 0.5.0 staging uses `org/apache/cassandra/` and a separate
Spark 4 repo. So for Analytics, the vote email should list what was actually staged, not what the
template generates. validate-release checks the email against the staging repos for exactly this reason.

## 3. Prepare and stage

1. Agree on timing on dev@ and check with the PMC that no security fix is being worked on in private.
   Make sure no urgent JIRA is open for the version.
2. Run prepare_release.sh. It tags `<version>-tentative`, pushes the tag, clones a fresh copy from
   gitbox, builds and signs, and uploads to Nexus.
3. When it pauses: in https://repository.apache.org "Staging Repositories", find the
   `orgapachecassandra-NNNN` repository and **Close** it. Closing runs Nexus' rules and makes it
   public. Give the number to the script; for Sidecar/Analytics there are two repositories, so give both.
4. The script commits the artifacts to dist/dev with svn and writes two email drafts to `~/Mail`:
   `mail_stage_announce_<version>` (optional "[ANNOUNCE] ... test artifact available") and
   `mail_vote_<version>`.
5. Cassandra only: forward-merge and push the `debian/changelog` commit the script made.
6. If deb or rpm packaging failed, re-run only that part with `-d` (deb) or `-r` (rpm).

## 4. Validate, then call the vote

Run validate-release in manual mode on the staged candidate before sending anything:

```bash
<skill>/scripts/validate-release --project <p> --version <v> --staging-repo <N> [--staging-repo <M>] --sha1 <tag commit>
```

Fix every FAIL first, which usually means a re-roll (section 6). Then compare the vote email draft
with the report: the sha1, the tag link, the dist URL, and the full list of staged Maven artifacts.
After that, send the vote email to dev@cassandra.apache.org. The user sends it, not you.

## 5. The vote

- It runs for at least 72 hours. Cassandra's template: "A vote passes if there are at least three
  binding +1s and no -1's."
- Anyone may vote; only PMC votes are binding. Voters write "+1 (binding)" or "+1 (non-binding)" and
  ideally list what they checked.
- The RM closes it with a reply or a `[RESULT][VOTE] Release ...` email giving the tally (binding and
  non-binding counted separately).
- When an issue comes up, the RM and voters decide on the thread whether it is a re-roll or a follow-up
  ticket. `--thread-votes` shows that discussion.

## 6. Failed vote / re-roll

1. Reply on the thread that the vote is cancelled, with the reason.
2. Delete the tentative tag on the ASF remote (`git push <asf-remote> :refs/tags/<version>-tentative`)
   and locally.
3. **Drop** the staging repository in Nexus.
4. Remove the RC from dist/dev:
   `svn rm -m "..." https://dist.apache.org/repos/dist/dev/cassandra/<subdir><version>`.
5. Fix the problem, then run prepare_release.sh again. Steps 2 and 4 are required: the script refuses
   to run while the tentative tag or the dist/dev directory exists. Send the new vote with a subject
   ending "- 2nd attempt" (then "3rd attempt", ...).

## 7. Finish and announce (PMC)

1. Check out the tentative tag and run `finish_release.sh -v <version>` from the project's checkout. It
   creates and pushes the final tag, **deletes the `-tentative` tag**, and `svn mv`s dist/dev →
   dist/release. For Cassandra (and Sidecar packages) it uploads the deb/rpm files to the ASF JFrog
   Artifactory (`ARTIFACTORY_API_KEY` must be set), which serves https://debian.cassandra.apache.org and
   https://redhat.cassandra.apache.org/<MAJMIN>x/. It then removes the `debian/` and `redhat/`
   directories from dist/release. It writes the announcement email draft and prints a checklist.
2. Work through that checklist. For Cassandra it is: **Release** the Nexus staging repository (so it
   syncs to Maven Central); wait for https://downloads.apache.org/cassandra/; update the website download
   page (cassandra-website `download.adoc`) and the CQL doc if needed; send the announcement (the user
   sends it, to user@ and dev@); update the Slack topic; release the version in JIRA; `svn rm` the
   previous release of the branch from dist/release (archive.apache.org keeps everything); bump
   `base.version` / CHANGES.txt (moving unreleased entries up) / debian/changelog; add the release at
   https://reporter.apache.org/addrelease.html?cassandra; update `upgrade_manifest.py` in cassandra-dtest.
   Sidecar and Analytics print their own, shorter lists.

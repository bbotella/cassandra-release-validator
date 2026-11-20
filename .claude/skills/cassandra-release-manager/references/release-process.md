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
8. The Python, GoCQL and Java drivers
9. cut-release steps, per project (generated)

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

With `cut-release`, this is `preflight` up to `vote-email`; section 9 lists each project's steps.
Below is what the official scripts do, which the steps replicate (`docs/parity.md`).

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

With `cut-release`, this is `tally` up to `post-release`; section 9 lists each project's steps.

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

## 8. The Python, GoCQL and Java drivers

The drivers are Cassandra PMC releases too: same dev@ list, same KEYS file, same 72-hour vote with three
binding +1s. But cassandra-builds doesn't build them. Their release managers stage by hand, so the
conventions below come from their vote emails (Python 3.30.1 / 3.31.0, GoCQL 2.1.x / 2.2.0, Java 4.19.4 / 3.13.0).

| | Python driver | GoCQL driver | Java driver |
|---|---|---|---|
| Repository | `cassandra-python-driver` | `cassandra-gocql-driver` | `cassandra-java-driver` (4.x on `4.x`, 3.x on `3.x`) |
| JIRA | CASSPYTHON | CASSGO | CASSJAVA |
| Vote is on | the final tag `<v>`; `<v>rc1` is a separate tag (one version-bump commit later) for a TestPyPI upload | `v<v>-tentative`, renamed to `v<v>` after the vote | the final tag `<v>` |
| dist/dev | `dev/cassandra/cassandra-python-driver/<v>/`: `cassandra_driver-<v>.tar.gz` (sdist) + `.asc/.sha256/.sha512`, and `wheels/` with one wheel per Python version and platform | `dev/cassandra/cassandra-gocql-driver/<v>/`: `apache-cassandra-gocql-driver-<v>.tar.gz` + `.asc/.sha256/.sha512` | `dev/cassandra/cassandra-java-driver/<v>/`: `apache-cassandra-java-driver-<v>-source.tar.gz` + `.asc/.sha256/.sha512`; the Maven artifacts are in a Nexus staging repo |
| Version declared in | `pyproject.toml` | nowhere: the Go module version is the tag | root `pom.xml` (its own `<version>`, not the `org.apache:apache` parent's) |
| Changelog | `CHANGELOG.rst` (`3.31.0` heading) | `CHANGELOG.md` (`## [2.2.0]` heading) | `changelog/README.md` (`### 4.19.4`, `## 3.13.0`) |
| Published after the vote | PyPI (sdist and wheels) | the `v<v>` tag (Go modules are fetched from git) | Maven Central (the Nexus repo is released) |

When the user is the RM for a driver release, the same advice applies: validate in manual mode before
sending the vote (`--project python-driver|gocql-driver|java-driver --version <v> --sha1 <commit>`, plus
`--staging-repo N` for Java). Check the
known gaps listed in `triage.md` (Python: NOTICE in MANIFEST.in, signatures for the wheels; GoCQL: LF
line endings in the tarball; Java: build the source assembly from a clean checkout), because they are cheaper to fix before the vote than to discuss during it.

## 9. cut-release steps, per project (generated)

<!-- BEGIN generated by `./cut-release runbook --write` from crv/release/steps: do not edit by hand -->
Each outward step is a dry run unless `--confirm` is given. `cut-release <project> <version> plan` shows
where a release stands. Steps run in this order; a step refuses to run before the ones above it.
In *Replicates*, `CP`/`CF` are cassandra-release/prepare_release.sh and finish_release.sh, `SP`/`SF`
the Sidecar ones and `AP`/`AF` the Analytics ones, with line numbers (`docs/parity.md` maps them).

### Apache Cassandra Analytics

| # | Step | Kind | What it does | Your part | Replicates |
|---|---|---|---|---|---|
| 1 | `preflight` | local | Read-only checks before anything is pushed |  | CP:27-36,118-165 SP:43-45,178-225 AP:43-45,138-183 (tools, clean tree, version, nothing staged yet) |
| 2 | `tag` | outward | Tag the release commit \<version>-tentative and push the tag |  | CP:251-252 SP:281-282 AP:232-233 |
| 3 | `build-publish` | outward | Fresh clone at the tag; build and publish to Nexus: common artifacts, then each Spark profile | after this step, Close the new staging repository/repositories at https://repository.apache.org | AP:235-247 |
| 4 | `staging` | local | Find the Nexus staging repositories you closed, and record them | before this step, Close the staging repository/repositories at https://repository.apache.org (Staging Repositories); Nexus only publishes closed ones | CP:265-266 SP:299-301 AP:249-251 (the 'staging number?' prompts) |
| 5 | `stage-dist` | outward | Commit the source and binary tarballs (+ .asc, .sha256, .sha512) to dist/dev |  | CP:268-276 SP:303-311 AP:253-261 |
| 6 | `validate` | local | Run validate-release on what was staged; a FAIL blocks the vote email |  | CP:474 SP:431 AP:378 ('verify all staged artifacts') |
| 7 | `vote-email` | local | Write the [VOTE] and [ANNOUNCE] drafts from what is really staged | send the vote email to dev@cassandra.apache.org yourself | CP:430-469 SP:361-426 AP:271-375 |
| 8 | `tally` | local | Read the vote thread: +1/-1 so far and whether 72 hours have passed | confirm which +1s are binding (PMC); only then is the vote passed | (manual in the official process: the RM counts the votes) |
| 9 | `finish-tag` | outward | Tag the release (\<prefix>\<version>), push it, and delete the tentative tag |  | CF:143-151 SF:147-153 AF:144-152 |
| 10 | `finish-dist` | outward | svn mv the release from dist/dev to dist/release | only PMC members can commit to dist/release; if you aren't one, send the command above to a PMC member | CF:157-161 SF:159-163 AF:158-162 |
| 11 | `nexus-release` | local | Check that the released staging repositories reached Maven Central | before this step, Release the staging repository/repositories in the Nexus UI | CF:276 SF:280 AF:202 ('Release the staging repository') |
| 12 | `verify-published` | local | validate-release --released on the published release |  | CF:277 ('wait for the artifacts to sync at downloads.apache.org') |
| 13 | `post-release` | local | Write the [RELEASE] mail draft and the remaining checklist | send the [RELEASE] mail after downloads.apache.org has synced, and work through the checklist | CF:246-287 SF:245-288 AF:170-209 (mail and checklist) |

### Apache Cassandra Sidecar

| # | Step | Kind | What it does | Your part | Replicates |
|---|---|---|---|---|---|
| 1 | `preflight` | local | Read-only checks before anything is pushed |  | CP:27-36,118-165 SP:43-45,178-225 AP:43-45,138-183 (tools, clean tree, version, nothing staged yet) |
| 2 | `tag` | outward | Tag the release commit \<version>-tentative and push the tag |  | CP:251-252 SP:281-282 AP:232-233 |
| 3 | `build-publish` | outward | Fresh clone at the tag; publish the -jdk8 client artifacts (JDK 8), then everything (JDK 11) | after this step, Close both staging repositories (JDK 8 and JDK 11) at https://repository.apache.org | SP:286-297 |
| 4 | `staging` | local | Find the Nexus staging repositories you closed, and record them | before this step, Close the staging repository/repositories at https://repository.apache.org (Staging Repositories); Nexus only publishes closed ones | CP:265-266 SP:299-301 AP:249-251 (the 'staging number?' prompts) |
| 5 | `stage-dist` | outward | Commit the source and binary tarballs (+ .asc, .sha256, .sha512) to dist/dev |  | CP:268-276 SP:303-311 AP:253-261 |
| 6 | `stage-packages` | outward | Commit the .deb and .rpm (+ .asc, .sha256, .sha512) to dist/dev under debian/ and redhat/ |  | SP:315-349 |
| 7 | `validate` | local | Run validate-release on what was staged; a FAIL blocks the vote email |  | CP:474 SP:431 AP:378 ('verify all staged artifacts') |
| 8 | `vote-email` | local | Write the [VOTE] and [ANNOUNCE] drafts from what is really staged | send the vote email to dev@cassandra.apache.org yourself | CP:430-469 SP:361-426 AP:271-375 |
| 9 | `tally` | local | Read the vote thread: +1/-1 so far and whether 72 hours have passed | confirm which +1s are binding (PMC); only then is the vote passed | (manual in the official process: the RM counts the votes) |
| 10 | `finish-tag` | outward | Tag the release (\<prefix>\<version>), push it, and delete the tentative tag |  | CF:143-151 SF:147-153 AF:144-152 |
| 11 | `finish-dist` | outward | svn mv the release from dist/dev to dist/release | only PMC members can commit to dist/release; if you aren't one, send the command above to a PMC member | CF:157-161 SF:159-163 AF:158-162 |
| 12 | `finish-packages` | outward | Upload the released .deb/.rpm to JFrog (verified), then remove them from dist/release | needs ARTIFACTORY_API_KEY (and asf_username) in the environment; the key never appears in logs | SF:184-240 |
| 13 | `nexus-release` | local | Check that the released staging repositories reached Maven Central | before this step, Release the staging repository/repositories in the Nexus UI | CF:276 SF:280 AF:202 ('Release the staging repository') |
| 14 | `verify-published` | local | validate-release --released on the published release |  | CF:277 ('wait for the artifacts to sync at downloads.apache.org') |
| 15 | `post-release` | local | Write the [RELEASE] mail draft and the remaining checklist | send the [RELEASE] mail after downloads.apache.org has synced, and work through the checklist | CF:246-287 SF:245-288 AF:170-209 (mail and checklist) |

### Apache Cassandra

| # | Step | Kind | What it does | Your part | Replicates |
|---|---|---|---|---|---|
| 1 | `preflight` | local | Read-only checks before anything is pushed |  | CP:27-36,118-165 SP:43-45,178-225 AP:43-45,138-183 (tools, clean tree, version, nothing staged yet) |
| 2 | `changelog` | | *not implemented yet* | | |
| 3 | `tag` | outward | Tag the release commit \<version>-tentative and push the tag |  | CP:251-252 SP:281-282 AP:232-233 |
| 4 | `build-publish` | | *not implemented yet* | | |
| 5 | `staging` | local | Find the Nexus staging repositories you closed, and record them | before this step, Close the staging repository/repositories at https://repository.apache.org (Staging Repositories); Nexus only publishes closed ones | CP:265-266 SP:299-301 AP:249-251 (the 'staging number?' prompts) |
| 6 | `stage-dist` | outward | Commit the source and binary tarballs (+ .asc, .sha256, .sha512) to dist/dev |  | CP:268-276 SP:303-311 AP:253-261 |
| 7 | `stage-debian` | | *not implemented yet* | | |
| 8 | `stage-rpm` | | *not implemented yet* | | |
| 9 | `validate` | local | Run validate-release on what was staged; a FAIL blocks the vote email |  | CP:474 SP:431 AP:378 ('verify all staged artifacts') |
| 10 | `vote-email` | local | Write the [VOTE] and [ANNOUNCE] drafts from what is really staged | send the vote email to dev@cassandra.apache.org yourself | CP:430-469 SP:361-426 AP:271-375 |
| 11 | `tally` | local | Read the vote thread: +1/-1 so far and whether 72 hours have passed | confirm which +1s are binding (PMC); only then is the vote passed | (manual in the official process: the RM counts the votes) |
| 12 | `finish-tag` | outward | Tag the release (\<prefix>\<version>), push it, and delete the tentative tag |  | CF:143-151 SF:147-153 AF:144-152 |
| 13 | `finish-dist` | outward | svn mv the release from dist/dev to dist/release | only PMC members can commit to dist/release; if you aren't one, send the command above to a PMC member | CF:157-161 SF:159-163 AF:158-162 |
| 14 | `finish-packages` | | *not implemented yet* | | |
| 15 | `nexus-release` | local | Check that the released staging repositories reached Maven Central | before this step, Release the staging repository/repositories in the Nexus UI | CF:276 SF:280 AF:202 ('Release the staging repository') |
| 16 | `verify-published` | local | validate-release --released on the published release |  | CF:277 ('wait for the artifacts to sync at downloads.apache.org') |
| 17 | `post-release` | local | Write the [RELEASE] mail draft and the remaining checklist | send the [RELEASE] mail after downloads.apache.org has synced, and work through the checklist | CF:246-287 SF:245-288 AF:170-209 (mail and checklist) |

Out of band, for every project:

- `reroll`: cancel the attempt: removes the tentative tag and the dist/dev directory, then the next attempt starts
- `check-upstream`: compare the official scripts upstream with the versions reviewed in docs/parity.md
- `cleanup`: rehearsals only (`--rehearsal`): delete the sandbox and its throwaway signing key
<!-- END generated runbook -->

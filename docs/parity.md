# Parity with the official release scripts

`cut-release` replicates the release scripts of
[apache/cassandra-builds](https://github.com/apache/cassandra-builds). This file maps every step of the
official scripts to a replica step, or says why the replica differs. Line numbers are from the
versions below (trunk `e850f73`, 2026-11-20).

## Reviewed versions

`cut-release <project> <version> check-upstream` (and `preflight`) compares upstream trunk with these
hashes. If one changed, re-read that script, update the mapping below, then update its hash.

| Script | SHA-256 |
|---|---|
| `cassandra-release/prepare_release.sh` | `9b1a2d7934288c32ef34b0919297a04859ee4ebab6ce2ab3e8b1a860491d006d` |
| `cassandra-release/finish_release.sh` | `8a03f6a8ba22d806b49a4a73be191404a3904f6f470fb4e93556a84903db5ccf` |
| `cassandra-sidecar-release/prepare_release.sh` | `044a85a6b1d28d49e7be1c50cec686b1f02e0a89f15fab9505c0b3a6014e6402` |
| `cassandra-sidecar-release/finish_release.sh` | `c7117b3b7562f7ec74ccbba814ca71ef275a0bb28f9770fa1aa0c8e1426ffce2` |
| `cassandra-analytics-release/prepare_release.sh` | `6e0c138002a71e812470e5aa0f7f15bda4cc031d3c3a63d66f2fceb56964faf6` |
| `cassandra-analytics-release/finish_release.sh` | `1fa9092563e32aa951e5c46549790178ff5db4dad40df8e037f717b038e51a88` |

## Differences that apply to every project

| Official behaviour | Replica | Why |
|---|---|---|
| `execute` ends with `exit $?` after an `echo`, so a failed step exits 0 | every command's exit status is checked; the step fails and the state records it | a failed release must not look successful |
| `-f` fake mode still writes files and prompts, and shows none of the uploads | every outward step is a dry run unless `--confirm`, and lists every command | you see exactly what will happen |
| interactive `select yn` / `read -p "staging number?"` | the `staging` step finds the closed repositories itself | Claude can drive it, nothing to type |
| `~/Mail`, dist URLs and (Cassandra) `git_asf_remote` are hardcoded | configurable (`crv/release/config.py`), used by `--rehearsal` | rehearsals |
| the tag is pushed to a remote named `origin` (or `$git_asf_remote`) | the remote of your checkout that points at gitbox, else the gitbox URL | no editing of the script |
| GNU-only shell (`expr index`, `find -printf`) | Python on the Mac; builds in the release container | runs on macOS |
| vote mail artifact URLs are typed into the script | crawled from the staging repositories | new modules can't be forgotten (Spark 4, `sidecar-adapters-cassandra50`) |
| `[RELEASE]` mail: "Apache Sidecar Cassandra" / "Apache Analytics Cassandra"; Cassandra always says "bug fix release"; a 2023 "repositories have moved" warning | names fixed; "the first release" for x.y.0; package repository URLs instead of the warning | wording slips |
| `validate` is a reminder in the checklist | a step that runs `validate-release`; a FAIL blocks `vote-email` | |
| no re-roll support | `reroll`: removes only what the attempt got to (tag, dist/dev directory) | a re-roll after a failed build has nothing staged |
| the RM counts the votes by hand | `tally` reads the thread; it succeeds only after 72 h with at least three +1s and no -1, so the finish steps stay blocked before that | finishing early is the costly mistake |

## Cassandra Analytics (`AP` = prepare, `AF` = finish)

| Official | Replica step | Notes |
|---|---|---|
| AP:43-45 tools, AP:138-158 clean git checkout + version, AP:160-183 nothing staged or tagged yet | `preflight` | also: signing key in KEYS and RSA 4096, credentials present (names only), open JIRA tickets, upstream drift |
| AP:185-199 show the commit and confirm | `preflight` summary + the `tag` dry run | |
| AP:231-233 tag `<v>-tentative`, push | `tag` | |
| AP:235-238 fresh clone from gitbox | `build-publish` | |
| AP:242 `git checkout -b <v>-tentative` (a branch at HEAD, not the tag) | `build-publish`: checks out the tag | bug in the official script |
| AP:244 `scripts/build-dependencies.sh` | `build-publish` (a rehearsal may reuse built dtest jars with `--dependencies`) | |
| AP:245-247 `clean`, then `publish` for Scala 2.12 and 2.13, without `-PartifactType`, `-P-D` typo, JDK 11 only | `build-publish`: `-PartifactType=common` once, then `spark` for each `profiles/*.gradle` (Spark 3 / 2.12, Spark 3 / 2.13 with JDK 11; Spark 4 / 2.13 with JDK 17), `clean` each time | the official script publishes nothing on trunk |
| AP:249-251 type the staging numbers | `staging` | |
| AP:253-262 svn commit the tarballs to dist/dev | `stage-dist` | |
| AP:271-375 mail drafts | `vote-email` | artifact list crawled; the official one has wrong `org/apache/cassandra/spark/` paths |
| AF:139-152 final tag, push, delete the tentative tag | `finish-tag` | |
| AF:156-162 `svn mv` dev → release | `finish-dist` | PMC hand-off |
| AF:166-200 release mail | `post-release` | |
| AF:201-209 checklist | `post-release` | |

## Cassandra Sidecar (`SP` = prepare, `SF` = finish)

| Official | Replica step | Notes |
|---|---|---|
| SP:43-45 tools, SP:178-196 git repository, clean tree, `version=` in gradle.properties, SP:200-225 nothing staged, released or tagged yet | `preflight` | in a rehearsal a `-SNAPSHOT` version only warns: the build passes `-Pversion`, and a rehearsal is most useful before the "Prepare <v> release" commit |
| SP:227-245 show the commit and confirm | `preflight` summary + the `tag` dry run | |
| SP:280-282 tag `<v>-tentative`, push | `tag` | |
| SP:284-291 fresh clone from gitbox, check out the tag | `build-publish` | |
| SP:292-297 JDK 8 `clean` then `publish` as `<v>-jdk8`; JDK 11 `clean` then `assemble publish -PskipIntegrationTest` | `build-publish` | the same tasks and properties; `clean` runs in the same Gradle invocation |
| (missing) the JDK 11 `assemble` compiles `test-common`'s test fixtures against a Cassandra dtest jar (`dtest-4.1.11.jar` in `dtest-jars/` or `$CASSANDRA_DEP_DIR`), which a fresh clone doesn't have | `build-publish`: `BRANCHES=<that branch> scripts/build-dtest-jars.sh` first (a rehearsal may reuse built jars with `--dependencies`) | without it the build fails in `:test-common:compileTestFixturesJava`; nothing published contains the jar |
| SP:299-301 type the two staging numbers | `staging` | finds both (JDK 8 first, as Nexus numbers them) |
| SP:303-312 svn commit the tarballs to dist/dev | `stage-dist` | |
| SP:315-349 the `.deb` under `debian/`, the `.rpm` under `redhat/`, one commit each | `stage-packages` | same layout and commit messages. Since CASSSIDECAR-472 (June 2026) the packages are `_<v>-1_all.deb` and `-<v>-1.noarch.rpm`; SP:324's `_<v>_all.deb*` and SP:342's `-<v>.noarch.rpm*` no longer match, so the official script stops at SP:324 with `Error running cp …` (exit status 0), after the tarballs are already in dist/dev and before the mail drafts. The replica finds the files |
| SP:361-426 mail drafts | `vote-email` | artifact list crawled from both staging repositories; the official list misses `sidecar-adapters-cassandra50` |
| SF:142-153 final tag `cassandra-sidecar-<v>`, push, delete the tentative tag | `finish-tag` | |
| SF:159-163 `svn mv` dev → release | `finish-dist` | PMC hand-off |
| SF:165-178 package repository series (`0.5.0` → `05x`) | `finish-packages` | `debian_series()` |
| SF:184-210 upload `debian/` to JFrog `cassandra/cassandra-sidecar/` and `cassandra-deb/cassandra-sidecar/`, then `svn rm` it | `finish-packages` | SF:194's `find -mindepth 2` skips the `.deb`, which sits directly in `debian/`, so the official script uploads nothing; the replica uploads it. No Sidecar package of any release is on JFrog (checked 2026-11-20: `cassandra-deb/cassandra-sidecar/` and `cassandra-rpm/cassandra-sidecar/` are 404), and dist/release no longer has them: the voted 0.4.0 packages are published nowhere |
| SF:212-240 upload `redhat/` to `cassandra-rpm/cassandra-sidecar/<series>/`, then `svn rm` it | `finish-packages` | |
| SF:280 "Release" the staging repository | `nexus-release` | you Release both repositories in the UI; the step checks Maven Central (a rehearsal merges the staging repositories into a Central stand-in) |
| SF:245-275 release mail | `post-release` | "Apache Sidecar Cassandra" fixed |
| SF:279-290 checklist | `post-release` | the reporter link uses the PMC name (`?cassandra`, not `?cassandra-sidecar`); no "tweet" item |

JFrog uploads, here and for Cassandra: `curl --fail --retry 3` (SF:201-232 ignore failures), the API key in a
temporary netrc file instead of argv and the log, a HEAD check of every upload, no `-mtime -10` filter, and
the `svn rm` from dist/release only once every upload succeeded.

## Cassandra (`CP` / `CF`)

Mapped as its steps are implemented (`cut-release cassandra <version> plan` lists them): `changelog`
(CP:235-245), `ant publish` (CP:263), `stage-debian` (CP:280-335), `stage-rpm` (CP:337-417),
`finish-packages` (CF:182-237).

"""Cutting releases: a replica of the cassandra-builds release scripts, as small resumable steps.

Every step that changes anything outside your machine (git push, Nexus deploy, svn commit, uploads) is
a dry run unless --confirm is given, and records what it did in the release's state file.
"""

"""cut-release: run the steps of a release one at a time."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..projects import VERSION_RE, by_name
from . import runbook, signing
from . import steps as steps_mod
from .config import endpoints
from .container import ReleaseContainer
from .context import ReleaseContext
from .executor import Executor, StepFailed
from .state import ReleaseState, ordinal

SUPPORTED = ("analytics", "sidecar", "cassandra")

EXAMPLES = """\
examples:
  cut-release analytics 0.6.0 plan                 # the steps and where you are
  cut-release analytics 0.6.0 preflight            # read-only checks before anything is pushed
  cut-release analytics 0.6.0 tag                  # dry run: shows the commands
  cut-release analytics 0.6.0 tag --confirm        # does it
  cut-release analytics 0.6.0 tag --rehearsal --confirm   # the same against a local sandbox
  cut-release analytics 0.6.0 cleanup --rehearsal  # delete that sandbox and its throwaway key
  cut-release runbook [--write]                    # every project's steps (--write: into the skill's runbook)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cut-release",
        description="Cut an Apache Cassandra, Sidecar or Analytics release, one step at a time. Steps that change "
                    "anything outside this machine are dry runs unless --confirm is given.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("project", help=", ".join(SUPPORTED))
    p.add_argument("version", help="the version to release, e.g. 0.6.0 or 5.0.10")
    p.add_argument("step", nargs="?", default="plan",
                   help="a step name, or plan (default) / status / cleanup (rehearsals: delete the sandbox and its key)")
    p.add_argument("--confirm", action="store_true", help="really do an outward step (otherwise: dry run)")
    p.add_argument("--rehearsal", action="store_true", help="use a local sandbox instead of gitbox/Nexus/dist/JFrog")
    p.add_argument("--force", action="store_true", help="run a step out of order, or again")
    p.add_argument("--source", help="your checkout of the project (default: ~/dev/<repo>)")
    p.add_argument("--signing-key", default=os.environ.get("gpg_key"), help="fingerprint of your release key (default: $gpg_key)")
    p.add_argument("--jdk", action="append", default=[], metavar="N=PATH", help="JDK home inside the container, e.g. 11=/opt/jdk-11")
    p.add_argument("--thread", help="the [VOTE] thread on lists.apache.org (for tally)")
    p.add_argument("--from-repo", metavar="N", help="staging: start looking at orgapachecassandra-N")
    p.add_argument("--dependencies", metavar="DIR", help="rehearsal: reuse built Cassandra dtest jars from DIR (e.g. ~/dev/cassandra-analytics/dependencies, "
                        "~/dev/cassandra-sidecar/dtest-jars) instead of building them")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["runbook"]:  # no project or version: every project's steps
        if argv[1:] == ["--write"]:
            print(f"{'updated' if runbook.write() else 'already up to date:'} {runbook.RUNBOOK}")
        else:
            print(runbook.render(), end="")
        return 0
    args = build_parser().parse_args(argv)
    project = by_name(args.project)
    if project is None or project.key not in SUPPORTED:
        print(f"error: cut-release supports {', '.join(SUPPORTED)} (not '{args.project}')", file=sys.stderr)
        return 2
    if not VERSION_RE.match(args.version):
        print(f"error: '{args.version}' does not look like a release version", file=sys.stderr)
        return 2
    steps_mod.load_all()
    ep = endpoints(project, args.version, args.rehearsal)
    state = ReleaseState.load(ep.root / "state.json", project.key, args.version, args.rehearsal)
    order = steps_mod.PLANS[project.key]

    if args.step in ("plan", "status"):
        _print_plan(project, args.version, state, order, ep, details=args.step == "status")
        return 0
    if args.step == "cleanup":
        return _cleanup(ep, state, args)
    if args.step not in order and args.step not in steps_mod.OUT_OF_BAND:
        print(f"error: unknown step '{args.step}' for {project.key}; steps: {', '.join(order + steps_mod.OUT_OF_BAND)}",
              file=sys.stderr)
        return 2
    step = steps_mod.lookup(args.step, project.key)
    if step is None:
        print(f"error: step '{args.step}' is not implemented yet for {project.key}", file=sys.stderr)
        return 2

    problem = _gate(step.name, order, state, args.force, step.repeatable)
    if problem:
        print(f"error: {problem} (use --force to override)", file=sys.stderr)
        return 2

    dry_run = step.outward and not args.confirm
    n = (order.index(step.name) + 1) if step.name in order else 0
    log = ep.root / "logs" / f"{state.attempt:02d}-{n:02d}-{step.name}{'-dry-run' if dry_run else ''}.log"
    executor = Executor(dry_run=dry_run, log=log, secrets=[os.environ.get("ARTIFACTORY_API_KEY", "")])
    executor.container = _container(ep, state, args)
    ctx = ReleaseContext(project, args.version, state, ep, executor, args)

    where = "REHEARSAL (local sandbox)" if ep.rehearsal else "REAL"
    print(f"{project.display} {args.version}, {ordinal(state.attempt)} attempt, step '{step.name}' [{where}]"
          f"{' — DRY RUN' if dry_run else ''}")
    print(f"   {step.summary}")
    if step.replicates:
        print(f"   replicates: {step.replicates}")
    # a reminder shows once: up front when it is about before the step, or about an outward step you are
    # deciding on (the PMC hand-off); otherwise once the step is done
    you_first = bool(step.you_do) and (step.you_do.startswith("before this step")
                                       or (step.outward and not step.you_do.startswith("after this step")))
    if you_first:
        print(f"   you: {step.you_do}")
    started = time.time()
    try:
        summary = step.run(ctx) or ""
    except steps_mod.NotYet as e:
        executor.container.close()
        state.record(step.name, "waiting", str(e), log)
        print(f"\nNOT YET: {e}")
        print("   the steps after this one stay blocked; run it again later")
        return 3
    except StepFailed as e:
        executor.container.close()
        state.record(step.name, "failed", str(e), log)
        print(f"\nFAILED: {e}")
        for line in e.tail:
            print(f"   | {line}")
        print(f"   full log: {log}")
        return 1
    except Exception as e:  # noqa: BLE001 - recorded and shown, never swallowed
        executor.container.close()
        state.record(step.name, "failed", f"{type(e).__name__}: {e}", log)
        print(f"\nFAILED: {type(e).__name__}: {e}")
        return 1

    executor.container.close()  # stops the signing relay as soon as the step is over
    if dry_run:
        outward = [p for p in executor.planned if p.outward]
        print(f"\nDry run: nothing was changed ({len(executor.planned)} commands, {len(outward)} outward). "
              f"Run again with --confirm to do it.")
        return 0
    state.record(step.name, "done", summary, log)
    print(f"\ndone in {time.time() - started:.0f}s: {summary}" if summary else f"\ndone in {time.time() - started:.0f}s")
    for text in ctx.notes:
        print(f"   next: {text}")
    if step.you_do and not you_first:
        print(f"   you: {step.you_do}")
    if step.name in order and order.index(step.name) + 1 < len(order):
        print(f"   following step: {order[order.index(step.name) + 1]}")
    return 0


def _container(ep, state: ReleaseState, args) -> ReleaseContainer:
    """Real releases use your ~/.gradle and ~/.m2 (credentials, caches); rehearsals share only the caches,
    so they never see your credentials or publish settings."""
    home = Path.home()
    mounts: dict[Path, str] = {}
    gitconfig = home / ".gitconfig"
    if gitconfig.exists():
        mounts[gitconfig] = "/root/.gitconfig:ro"
    if ep.rehearsal:
        for host, target in ((home / ".gradle" / "caches", "/root/.gradle-rehearsal/caches"),
                             (home / ".gradle" / "wrapper", "/root/.gradle-rehearsal/wrapper"),
                             (home / ".m2" / "repository", "/root/.m2/repository")):
            host.mkdir(parents=True, exist_ok=True)
            mounts[host] = target
        key = lambda: (state.data.get("rehearsal_key"), state.data.get("rehearsal_gnupghome"))  # noqa: E731
    else:
        for host, target in ((home / ".gradle", "/root/.gradle"), (home / ".m2", "/root/.m2")):
            host.mkdir(parents=True, exist_ok=True)
            mounts[host] = target
        key = lambda: (args.signing_key, None)  # noqa: E731
    return ReleaseContainer(ep.root, mounts=mounts, key=key)


def _cleanup(ep, state: ReleaseState, args) -> int:
    """Deletes a rehearsal: its throwaway key and agent, and its directory. A real release's state is its
    record (svn revisions, staging numbers, logs), so this refuses to touch one."""
    if not ep.rehearsal:
        print("error: cleanup only deletes rehearsals (add --rehearsal); a real release's state is kept",
              file=sys.stderr)
        return 2
    home = state.data.get("rehearsal_gnupghome")
    if home:
        signing.remove_home(home)
        print(f"   removed the rehearsal key {state.data.get('rehearsal_key', '')} and {home}")
    if ep.root.exists():
        # through the container, like ReleaseContext.fresh_dir: the podman VM must see the delete, or a later
        # rehearsal of the same version finds stale directories
        container = _container(ep, state, args)
        subprocess.run(container.wrap(["find", str(ep.root), "-mindepth", "1", "-delete"], None, {}, False),
                       check=True)
        shutil.rmtree(ep.root, ignore_errors=True)
        print(f"   removed {ep.root}")
    return 0


def _gate(name: str, order: tuple[str, ...], state: ReleaseState, force: bool, repeatable: bool) -> str | None:
    if force or name not in order:
        return None
    if state.done(name) and not repeatable:
        return f"step '{name}' is already done in this attempt"
    missing = [s for s in order[: order.index(name)] if not state.done(s)]
    if missing:
        return f"step '{name}' needs {', '.join(missing)} first"
    return None


def _print_plan(project, version, state: ReleaseState, order, ep, details: bool) -> None:
    print(f"{project.display} {version} — {ordinal(state.attempt)} attempt"
          f"{' — REHEARSAL' if ep.rehearsal else ''}   state: {state.path}")
    for i, name in enumerate(order, 1):
        step = steps_mod.lookup(name, project.key)
        record = state.steps.get(name)
        if record and record.attempt == state.attempt:
            mark = {"done": "✔", "failed": "✘", "waiting": "…"}.get(record.status, "?")
        else:
            mark = " "
        kind = "outward" if step and step.outward else "local  "
        summary = step.summary if step else "(not implemented yet)"
        print(f"  {mark} {i:2d}. {name:<17} {kind}  {summary}")
        if details and record and record.attempt == state.attempt and record.summary:
            print(f"        {record.status} {record.at}: {record.summary}")
        if details and step and step.you_do:
            print(f"        you: {step.you_do}")
    if details and state.data:
        print("  data:")
        for key, value in sorted(state.data.items()):
            print(f"    {key}: {value}")

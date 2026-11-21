"""Smoke tests (opt-in with --smoke): every artifact of the release used the way a user uses it.

Each project's module says what that means for it (crv/checks/smoke/<project>.py). Everything runs in
throwaway containers (docker or podman, never on the host), against official cassandra:<series> images for
the client tests, with the files validate-release has already verified. The scripts in the containers report
through the protocol in engine.py; an environment problem (an image that can't be pulled, a mirror that is
down) is a WARN "not run", never a FAIL.
"""

from __future__ import annotations

import shutil
import time

from ...context import Context
from ..runtime import find_engine
from . import analytics, cassandra, gocql_driver, java_driver, python_driver, sidecar
from .common import SmokeRun
from .engine import Engine
from .server import Servers, images

MODULES = {
    "cassandra": cassandra,
    "sidecar": sidecar,
    "analytics": analytics,
    "python-driver": python_driver,
    "gocql-driver": gocql_driver,
    "java-driver": java_driver,
}
KEEP_RUNS = 3  # older run directories under <workdir>/smoke/ are removed


def tests_for(project_key: str) -> tuple[str, ...]:
    module = MODULES.get(project_key)
    return module.TESTS if module else ()


def selected(project_key: str, value: str | None) -> tuple[list[str], list[str]]:
    """(tests to run, unknown names) for a --smoke-tests value."""
    known = tests_for(project_key)
    if not value:
        return list(known), []
    wanted = [t.strip() for t in value.split(",") if t.strip()]
    return [t for t in known if t in wanted], [t for t in wanted if t not in known]


def run(ctx: Context) -> None:
    r = ctx.report
    r.section("Smoke tests (the artifacts used as they are, in throwaway containers)")
    module = MODULES.get(ctx.project.key)
    if module is None:
        r.skip("Smoke tests", f"none defined for {ctx.project.display}")
        return
    path, why = find_engine(ctx.args.container_engine)
    if not path:
        r.skip("Smoke tests", f"no usable container engine ({why}); start Docker or `podman machine start`")
        return
    tests, _ = selected(ctx.project.key, ctx.args.smoke_tests)
    root = ctx.workdir / "smoke"
    root.mkdir(parents=True, exist_ok=True)
    for old in sorted(root.glob("run-*"))[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)
    run_dir = root / time.strftime("run-%Y%m%d-%H%M%S")  # a new name each run: never reuse a mounted path
    run_dir.mkdir()
    engine = Engine(path)
    servers_used = ", ".join(images(ctx)) if module is not cassandra else "the release itself"
    r.info("Smoke tests", f"{', '.join(tests)} with {engine.name}; Cassandra servers: {servers_used}; "
                          f"up to {ctx.args.smoke_timeout}s per step; logs in {run_dir}")
    with Servers(ctx, engine, run_dir) as servers:
        smoke = SmokeRun(ctx, engine, servers, run_dir, tests)
        module.run(smoke)
    if smoke.passed:
        ctx.highlight("Smoke tests in containers: " + "; ".join(smoke.passed))

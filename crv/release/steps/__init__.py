"""Release steps and the order each project runs them in.

Each step names the official cassandra-builds script lines it replicates (see docs/parity.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Step:
    name: str
    summary: str
    outward: bool  # changes something outside this machine: dry run unless --confirm
    run: Callable  # (ReleaseContext) -> str, a one-line result
    replicates: str = ""  # e.g. "cassandra-analytics-release/prepare_release.sh:231-233"
    you_do: str = ""  # what the release manager does by hand around this step (Nexus UI, sending mail)
    repeatable: bool = False  # may run again once done (read-only steps)


class NotYet(Exception):
    """The step ran but its condition isn't met yet (the vote is still open, say). Recorded as waiting,
    so the steps after it stay blocked; run it again later."""


# step name -> {project key, or "*" for every project: Step}
REGISTRY: dict[str, dict[str, Step]] = {}


def step(name: str, summary: str, *, outward: bool, project: str = "*", replicates: str = "", you_do: str = "",
         repeatable: bool = False):
    def register(fn: Callable) -> Callable:
        REGISTRY.setdefault(name, {})[project] = Step(name, summary, outward, fn, replicates, you_do, repeatable)
        return fn

    return register


def lookup(name: str, project: str) -> Step | None:
    """The project's own implementation of a step, else the common one."""
    impls = REGISTRY.get(name, {})
    return impls.get(project) or impls.get("*")


# The order of the steps for each project. Out-of-band steps (reroll) are not listed.
PLANS: dict[str, tuple[str, ...]] = {
    "analytics": (
        "preflight", "tag", "build-publish", "staging", "stage-dist", "validate", "vote-email",
        "tally", "finish-tag", "finish-dist", "nexus-release", "verify-published", "post-release",
    ),
    "sidecar": (
        "preflight", "tag", "build-publish", "staging", "stage-dist", "stage-packages", "validate", "vote-email",
        "tally", "finish-tag", "finish-dist", "finish-packages", "nexus-release", "verify-published", "post-release",
    ),
    "cassandra": (
        "preflight", "changelog", "tag", "build-publish", "staging", "stage-dist", "stage-debian", "stage-rpm",
        "validate", "vote-email", "tally", "finish-tag", "finish-dist", "finish-packages", "nexus-release",
        "verify-published", "post-release",
    ),
}
OUT_OF_BAND = ("reroll", "check-upstream")


def load_all() -> None:
    """Imports the step modules so they register themselves."""
    from . import analytics, cassandra, common, sidecar  # noqa: F401

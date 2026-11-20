import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crv.release import cli, config, steps  # noqa: E402
from crv.release.executor import Executor, StepFailed  # noqa: E402
from crv.release.state import ReleaseState, ordinal  # noqa: E402


class FrameworkTest(unittest.TestCase):
    def setUp(self):
        steps.load_all()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._root, config.RELEASES_ROOT = config.RELEASES_ROOT, self.tmp
        self._plans, self._registry = dict(steps.PLANS), dict(steps.REGISTRY)
        self.ran: list[str] = []

        def local(ctx):
            self.ran.append("local")
            return "checked"

        def outward(ctx):
            ctx.run(["echo", "pushed", "secret-token-123"], "push the tag", outward=True)
            self.ran.append("outward")
            return "pushed"

        def broken(ctx):
            ctx.run(["sh", "-c", "echo boom; exit 7"], "a failing command")

        def waiting(ctx):
            raise steps.NotYet("the vote is still open")

        steps.REGISTRY.update({
            "t-waiting": {"*": steps.Step("t-waiting", "a step that waits", False, waiting)},
            "t-before": {"*": steps.Step("t-before", "needs you first", False, local,
                                         you_do="before this step, Close the repository")},
            "t-after": {"*": steps.Step("t-after", "needs you after", False, local,
                                        you_do="after this step, Close the repository")},
            "t-local": {"*": steps.Step("t-local", "a local step", False, local)},
            "t-outward": {"*": steps.Step("t-outward", "an outward step", True, outward)},
            "t-broken": {"*": steps.Step("t-broken", "a broken step", False, broken)},
        })
        steps.PLANS["analytics"] = ("t-local", "t-outward", "t-broken")

    def tearDown(self):
        config.RELEASES_ROOT = self._root
        steps.PLANS.clear()
        steps.PLANS.update(self._plans)
        steps.REGISTRY.clear()
        steps.REGISTRY.update(self._registry)

    def cut(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["analytics", "9.9.9", *argv])
        return code, out.getvalue()

    def state(self) -> ReleaseState:
        return ReleaseState.load(self.tmp / "analytics-9.9.9" / "state.json", "analytics", "9.9.9", False)

    def test_order_is_enforced(self):
        code, out = self.cut("t-outward", "--confirm")
        self.assertEqual(code, 2)
        self.assertIn("needs t-local first", out)
        self.assertEqual(self.ran, [])

    def test_outward_step_is_a_dry_run_without_confirm(self):
        self.assertEqual(self.cut("t-local")[0], 0)
        code, out = self.cut("t-outward")
        self.assertEqual(code, 0)
        self.assertIn("[dry run] OUTWARD push the tag", out)
        self.assertIn("Dry run: nothing was changed", out)
        self.assertFalse(self.state().done("t-outward"))  # a dry run records nothing

    def test_confirm_runs_and_records(self):
        self.cut("t-local")
        code, out = self.cut("t-outward", "--confirm")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.state().done("t-outward"))
        self.assertEqual(self.ran, ["local", "outward"])

    def test_done_step_is_not_run_again(self):
        self.cut("t-local")
        code, out = self.cut("t-local")
        self.assertEqual(code, 2)
        self.assertIn("already done", out)

    def test_failure_is_recorded_with_its_exit_status(self):
        self.cut("t-local")
        self.cut("t-outward", "--confirm")
        code, out = self.cut("t-broken")
        self.assertEqual(code, 1)
        self.assertIn("exit status 7", out)
        self.assertIn("| boom", out)
        self.assertEqual(self.state().steps["t-broken"].status, "failed")

    def test_not_yet_is_recorded_as_waiting_and_blocks_later_steps(self):
        steps.PLANS["analytics"] = ("t-waiting", "t-local")
        code, out = self.cut("t-waiting")
        self.assertEqual(code, 3)
        self.assertIn("NOT YET: the vote is still open", out)
        self.assertEqual(self.state().steps["t-waiting"].status, "waiting")
        code, out = self.cut("t-local")
        self.assertEqual(code, 2)
        self.assertIn("needs t-waiting first", out)
        self.assertIn("…  1. t-waiting", self.cut("plan")[1])

    def test_reminders_show_once_where_they_apply(self):
        steps.PLANS["analytics"] = ("t-before", "t-after")
        _, before = self.cut("t-before")
        _, after = self.cut("t-after")
        self.assertEqual(before.count("you: before this step"), 1)
        self.assertLess(before.index("you: before this step"), before.index("done in"))
        self.assertEqual(after.count("you: after this step"), 1)
        self.assertGreater(after.index("you: after this step"), after.index("done in"))

    def test_cleanup_refuses_a_real_release(self):
        code, out = self.cut("cleanup")
        self.assertEqual(code, 2)
        self.assertIn("only deletes rehearsals", out)

    def test_plan_shows_progress(self):
        self.cut("t-local")
        code, out = self.cut("plan")
        self.assertEqual(code, 0)
        self.assertIn("✔  1. t-local", out)
        self.assertRegex(out, r"2\. t-outward\s+outward")


class ExecutorTest(unittest.TestCase):
    def test_secrets_are_masked_in_output_and_log(self):
        log = Path(tempfile.mkdtemp()) / "x.log"
        ex = Executor(dry_run=False, log=log, secrets=["s3cr3t"], echo=False)
        out = ex.run(["echo", "token=s3cr3t"], "print", capture=True)
        self.assertEqual(out.strip(), "token=********")
        self.assertNotIn("s3cr3t", log.read_text())
        self.assertIn("********", ex.planned[0].command)

    def test_dry_run_shows_the_environment(self):
        ex = Executor(dry_run=True, log=Path(tempfile.mkdtemp()) / "log", echo=False)
        ex.run(["./build.sh"], "build", env={"BRANCHES": "cassandra-4.1", "CRV_JAVA_HOME": "/opt/jdk-11"})
        self.assertEqual(ex.planned[0].command, "BRANCHES=cassandra-4.1 JAVA_HOME=/opt/jdk-11 ./build.sh")

    def test_dry_run_runs_only_read_only_commands(self):
        log = Path(tempfile.mkdtemp()) / "x.log"
        ex = Executor(dry_run=True, log=log, echo=False)
        self.assertIsNone(ex.run(["sh", "-c", "exit 3"], "would fail if run"))
        self.assertEqual(ex.run(["echo", "hi"], "read-only", capture=True, always=True).strip(), "hi")

    def test_failure_raises(self):
        ex = Executor(dry_run=False, log=Path(tempfile.mkdtemp()) / "x.log", echo=False)
        with self.assertRaises(StepFailed) as cm:
            ex.run(["sh", "-c", "echo nope; exit 4"], "fails")
        self.assertEqual(cm.exception.returncode, 4)
        self.assertEqual(cm.exception.tail, ["nope"])


class RegistryTest(unittest.TestCase):
    def test_project_specific_steps_do_not_clash(self):
        steps.load_all()
        a = steps.lookup("build-publish", "analytics")
        s = steps.lookup("build-publish", "sidecar")
        self.assertIsNotNone(a)
        self.assertIsNotNone(s)
        self.assertIsNot(a.run, s.run)
        self.assertIn("AP:", a.replicates)
        self.assertIn("SP:", s.replicates)
        self.assertIs(steps.lookup("tag", "analytics"), steps.lookup("tag", "cassandra"))  # common step


class RunbookTest(unittest.TestCase):
    def test_runbook_in_the_skill_matches_the_steps(self):
        from crv.release import runbook
        self.assertEqual(runbook.current(), runbook.render(),
                         "release-process.md is out of date: run ./cut-release runbook --write")

    def test_every_planned_step_is_listed(self):
        from crv.release import runbook
        text = runbook.render()
        for order in steps.PLANS.values():
            for name in order:
                self.assertIn(f"`{name}`", text)


class StateTest(unittest.TestCase):
    def test_reroll_starts_a_new_attempt(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        s = ReleaseState.load(path, "analytics", "1.0.0", False)
        s.record("preflight", "done")
        s.record("tag", "done")
        s.data["staging_repos"] = ["1484"]
        s.reset_for_reroll(keep=("preflight",))
        s = ReleaseState.load(path, "analytics", "1.0.0", False)
        self.assertEqual(s.attempt, 2)
        self.assertTrue(s.done("preflight"))
        self.assertFalse(s.done("tag"))
        self.assertNotIn("staging_repos", s.data)

    def test_state_file_must_match(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        ReleaseState.load(path, "analytics", "1.0.0", False).save()
        with self.assertRaises(ValueError):
            ReleaseState.load(path, "sidecar", "1.0.0", False)

    def test_ordinals(self):
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"])


if __name__ == "__main__":
    unittest.main()

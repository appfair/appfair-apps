"""The shape of the publishing flow: what each workflow may do, and when.

A submission goes out from its own pull request, behind the `store` environment's reviewers, and
the pull request merges once the stores have taken it. These are the properties that flow rests
on, read from the workflows themselves.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import queue as catalog  # noqa: E402

WORKFLOWS = {
    path.stem: yaml.safe_load(path.read_text())
    for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
}


def triggers(workflow: dict) -> dict:
    """`on:` parses as the boolean True, which is YAML 1.1 for the bare word "on"."""
    return workflow.get("on", workflow.get(True)) or {}


class FlowTests(unittest.TestCase):
    def test_a_merge_publishes_nothing(self):
        """The whole point: publishing is not a side effect of merging."""
        self.assertEqual(sorted(triggers(WORKFLOWS["publish"])), ["workflow_dispatch"])

    def test_the_pull_request_carries_the_submission(self):
        submit = WORKFLOWS["pr"]["jobs"]["submit"]
        self.assertEqual(submit["uses"], "./.github/workflows/submit.yml")
        self.assertEqual(submit["secrets"], "inherit")
        self.assertIn("validate", submit["needs"])

    def test_the_submission_waits_for_a_reviewer(self):
        """The `store` environment is where a maintainer approves; without its protection rules
        the run would go straight through."""
        self.assertEqual(WORKFLOWS["submit"]["jobs"]["publish"]["environment"], "store")

    def test_both_callers_run_the_same_stage(self):
        for name in ("pr", "publish"):
            self.assertEqual(
                WORKFLOWS[name]["jobs"]["submit"]["uses"], "./.github/workflows/submit.yml", name
            )

    def test_one_approval_covers_every_channel(self):
        """A matrix leg that starts waiting later raises its own approval request, which is what
        `max-parallel: 1` used to cause: one prompt per store."""
        publish = WORKFLOWS["submit"]["jobs"]["publish"]["strategy"]
        self.assertNotIn("max-parallel", publish)

    def test_the_merge_waits_for_every_channel(self):
        merge = WORKFLOWS["pr"]["jobs"]["merge"]
        self.assertIn("needs.submit.result == 'success'", merge["if"])
        self.assertEqual(merge["permissions"]["pull-requests"], "write")
        self.assertEqual(merge["permissions"]["contents"], "write")
        run = " ".join(step.get("run", "") for step in merge["steps"])
        self.assertIn("gh pr merge", run)
        self.assertIn("queue.py record", run)

    def test_the_record_is_written_on_main(self):
        """The merge lands the submission; the record is a commit on top of it."""
        steps = WORKFLOWS["pr"]["jobs"]["merge"]["steps"]
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("Merge this pull request"), names.index("Record the publication"))

    def test_only_the_job_that_needs_keys_is_in_the_environment(self):
        """Stage A runs the app's code, so the environment must not reach it. One job holds the
        store keys and the app's private key, and it is the only one: a second job in the same
        environment would ask the reviewer to approve again, for work they already released."""
        gated = {(name, job) for name, workflow in WORKFLOWS.items()
                 for job, spec in workflow["jobs"].items() if "environment" in spec}
        self.assertEqual(gated, {("submit", "publish")})

    def test_the_app_key_is_read_only_inside_the_environment(self):
        """A repository secret is readable by any workflow here, including one on a branch."""
        for name, workflow in WORKFLOWS.items():
            for job, spec in workflow["jobs"].items():
                uses_key = "APPFAIR_APP_PRIVATE_KEY" in yaml.safe_dump(spec)
                if uses_key:
                    self.assertEqual(spec.get("environment"), "store", f"{name}:{job}")


class RecordTests(unittest.TestCase):
    """`record --matrix` writes one entry per app the plan named."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name) / "published.json"
        original = catalog.STATE
        catalog.STATE = state
        self.addCleanup(lambda: setattr(catalog, "STATE", original))
        self.state = state

    def record(self, **kwargs):
        fields = {"app": "", "matrix": "", "tag": "", "channels": "", "run_url": "https://example/run"}
        return catalog.cmd_record(catalog.argparse.Namespace(**{**fields, **kwargs}))

    def test_every_row_of_the_matrix_is_recorded(self):
        token = next(app.token for app in catalog.catalog())
        matrix = json.dumps(
            {"include": [{"token": token, "tag": "v9.9.9", "channels": "apple-app-store"}]}
        )
        self.assertEqual(self.record(matrix=matrix), 0)
        written = json.loads(self.state.read_text())["apps"][token]
        self.assertEqual(written["tag"], "v9.9.9")
        self.assertEqual(written["channels"], ["apple-app-store"])
        self.assertEqual(written["run"], "https://example/run")

    def test_an_empty_matrix_is_a_failure(self):
        self.assertEqual(self.record(matrix=json.dumps({"include": []})), 1)

    def test_one_app_still_records_on_its_own(self):
        token = next(app.token for app in catalog.catalog())
        self.assertEqual(self.record(app=token, tag="v1.0.0", channels="google-play-store"), 0)
        self.assertEqual(json.loads(self.state.read_text())["apps"][token]["tag"], "v1.0.0")


if __name__ == "__main__":
    unittest.main()

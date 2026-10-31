"""Synthetic PR diffs: reject unrelated app changes before creating a build matrix."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import check_pr_scope as scope


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "review"
        self.outputs = self.root / "outputs"
        self.summary = self.root / "summary"

    def run_guard(self, paths="", error=None):
        result = subprocess.CompletedProcess([], 0, stdout=paths, stderr="")
        output = io.StringIO()
        with patch.object(scope.subprocess, "run", return_value=result, side_effect=error) as run, \
             patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.outputs),
                                     "GITHUB_STEP_SUMMARY": str(self.summary)}), \
             contextlib.redirect_stdout(output):
            code = scope.main(["--base", "a" * 40, "--pr", "40", "--out", str(self.out)])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "git", "diff", "--name-only", "--no-renames", "-z", "a" * 40 + "...HEAD", "--", "apps/",
        ])
        self.assertTrue(run.call_args.kwargs["check"])
        return code, output.getvalue()

    def test_no_submission_changes_pass_without_a_comment(self):
        code, _ = self.run_guard("README.md\0apps/README.md\0")
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.outputs.exists())

    def test_one_submission_with_other_changes_passes(self):
        for suffix in ("yaml", "yml"):
            with self.subTest(suffix=suffix):
                code, _ = self.run_guard(f"apps/Stanza-Redux.{suffix}\0policy.yaml\0scripts/queue.py\0")
                self.assertEqual(code, 0)
                self.assertFalse(self.out.exists())

    def test_two_apps_fail_and_prepare_the_pr_comment(self):
        code, log = self.run_guard("apps/Stanza-Redux.yaml\0apps/Games-Fair.yaml\0")
        self.assertEqual(code, 1)
        self.assertIn("::error::", log)
        comment = (self.out / "review.md").read_text()
        self.assertTrue(comment.startswith("<!-- appfair-review -->"))
        for text in ("apps/Games-Fair.yaml", "apps/Stanza-Redux.yaml", "separate PRs",
                     "base-branch contents", "entire PR diff", "No app builds"):
            self.assertIn(text, comment)
        self.assertEqual((self.out / "review-pr.txt").read_text(), "40\n")
        self.assertEqual(self.outputs.read_text(), "blocked=true\n")
        self.assertEqual(self.summary.read_text(), comment)

    def test_deleted_or_renamed_files_count_without_existing_on_disk(self):
        # --no-renames reports a rename as old and new paths. Never filter by file existence.
        code, _ = self.run_guard("apps/Old-App.yaml\0apps/New-App.yml\0")
        self.assertEqual(code, 1)
        self.assertIn("apps/Old-App.yaml", (self.out / "review.md").read_text())

    def test_duplicate_paths_do_not_count_as_two_submissions(self):
        code, _ = self.run_guard("apps/Demo.yaml\0apps/Demo.yaml\0")
        self.assertEqual(code, 0)

    def test_paths_are_escaped_in_the_comment(self):
        code, _ = self.run_guard("apps/<img>.yaml\0apps/Other.yaml\0")
        self.assertEqual(code, 1)
        comment = (self.out / "review.md").read_text()
        self.assertIn("&lt;img&gt;", comment)
        self.assertNotIn("<img>", comment)

    def test_unavailable_base_fails_without_falling_back_to_the_last_commit(self):
        error = subprocess.CalledProcessError(128, "git", stderr="bad revision")
        code, log = self.run_guard(error=error)
        self.assertEqual(code, 1)
        self.assertIn("Cannot determine", log)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.outputs.exists())


if __name__ == "__main__":
    unittest.main()

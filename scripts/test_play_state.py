"""Cases for judging what Google Play says after a lane has run."""
import unittest

from play_state import judge, unused_code


def track(*releases):
    return {"track": "production", "releases": list(releases)}


def release(status, *codes, name=None):
    record = {"status": status, "versionCodes": [str(c) for c in codes]}
    if name:
        record["name"] = name
    return record


class JudgeTests(unittest.TestCase):
    def test_a_completed_production_release_passes(self):
        ok, lines, problems = judge(track(release("completed", 36)), "36", "production", True)
        self.assertTrue(ok, problems)
        self.assertTrue(any("36 is completed" in line for line in lines), lines)

    def test_a_staged_rollout_counts_as_submitted(self):
        ok, _, problems = judge(track(release("inProgress", 36)), "36", "production", True)
        self.assertTrue(ok, problems)

    def test_a_draft_is_not_a_submission(self):
        ok, _, problems = judge(track(release("draft", 36)), "36", "production", True)
        self.assertFalse(ok)
        self.assertTrue(any("draft" in p for p in problems), problems)

    def test_a_draft_is_fine_when_the_lane_only_uploaded(self):
        ok, _, problems = judge(track(release("draft", 36)), "36", "internal", False)
        self.assertTrue(ok, problems)

    def test_a_missing_version_code_fails_and_says_what_is_there(self):
        ok, _, problems = judge(track(release("completed", 35)), "36", "production", True)
        self.assertFalse(ok)
        self.assertTrue(any("holds 35" in p for p in problems), problems)

    def test_an_empty_track_says_so(self):
        ok, _, problems = judge(track(), "36", "production", True)
        self.assertFalse(ok)
        self.assertTrue(any("is empty" in p for p in problems), problems)

    def test_an_unreadable_track_is_a_failure_not_a_pass(self):
        ok, _, problems = judge({"error": 403, "detail": "no access"}, "36", "production", True)
        self.assertFalse(ok)
        ok, _, problems = judge(None, "36", "production", False)
        self.assertFalse(ok)

    def test_a_halted_release_is_not_live(self):
        ok, _, problems = judge(track(release("halted", 36)), "36", "production", True)
        self.assertFalse(ok)



class UnusedCodeTests(unittest.TestCase):
    def test_a_fresh_code_passes(self):
        ok, lines, problems = unused_code([{"versionCode": 35}], "37", {})
        self.assertTrue(ok, problems)
        self.assertTrue(any("35" in line for line in lines), lines)

    def test_the_state_that_refused_this_submission(self):
        ok, _, problems = unused_code(
            [{"versionCode": c} for c in (30, 35, 36)],
            "36",
            {"internal": track(release("draft", 36))},
        )
        self.assertFalse(ok)
        self.assertTrue(any("already been uploaded" in p for p in problems), problems)
        self.assertTrue(any("internal track as a draft" in p for p in problems), problems)

    def test_a_used_code_with_no_track_still_blocks(self):
        ok, _, problems = unused_code([{"versionCode": 36}], "36", {})
        self.assertFalse(ok)
        self.assertTrue(any("raise `build`" in p for p in problems), problems)

if __name__ == "__main__":
    unittest.main()

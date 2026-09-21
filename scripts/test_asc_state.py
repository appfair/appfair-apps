"""Cases for judging what App Store Connect says after a lane has run."""
import unittest

from asc_state import judge


def version(state, build_id=None, string="2.0.2"):
    record = {"attributes": {"appStoreState": state, "versionString": string}, "relationships": {}}
    if build_id:
        record["relationships"]["build"] = {"data": {"type": "builds", "id": build_id}}
    return record


def build(number, state="VALID", identifier="b1"):
    return {"id": identifier, "attributes": {"version": str(number), "processingState": state}}


class JudgeTests(unittest.TestCase):
    def test_a_submitted_version_with_its_build_attached_passes(self):
        for state in ["WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE"]:
            ok, _, problems = judge(version(state, "b1"), [build(36)], "36", True)
            self.assertTrue(ok, f"{state}: {problems}")

    def test_the_run_that_prompted_this_would_have_failed(self):
        # What the first publish really produced: the binary uploaded and valid, the version
        # still in Prepare for Submission with nothing attached.
        ok, _, problems = judge(version("PREPARE_FOR_SUBMISSION"), [build(36)], "36", True)
        self.assertFalse(ok)
        self.assertTrue(any("PREPARE_FOR_SUBMISSION" in p for p in problems), problems)
        self.assertTrue(any("not the one attached" in p for p in problems), problems)

    def test_an_upload_only_lane_passes_but_says_the_version_is_empty(self):
        ok, lines, problems = judge(version("PREPARE_FOR_SUBMISSION"), [build(36)], "36", False)
        self.assertTrue(ok, problems)
        self.assertTrue(any("no build yet" in line for line in lines), lines)

    def test_a_missing_binary_fails_whatever_the_lane_did(self):
        for submitted in (True, False):
            ok, _, problems = judge(version("PREPARE_FOR_SUBMISSION"), [build(35)], "36", submitted)
            self.assertFalse(ok)
            self.assertTrue(any("no build 36" in p for p in problems), problems)

    def test_another_build_attached_is_not_this_submission(self):
        ok, _, problems = judge(
            version("WAITING_FOR_REVIEW", "older"), [build(36), build(35, identifier="older")],
            "36", True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("not the one attached" in p for p in problems), problems)

    def test_a_rejected_version_is_not_a_submission_that_worked(self):
        ok, _, problems = judge(version("DEVELOPER_REJECTED", "b1"), [build(36)], "36", True)
        self.assertFalse(ok)

    def test_a_build_still_failing_processing_fails(self):
        ok, _, problems = judge(version("WAITING_FOR_REVIEW", "b1"), [build(36, "INVALID")], "36", True)
        self.assertFalse(ok)
        self.assertTrue(any("INVALID" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()

"""The shape of the publishing flow: what each workflow may do, and when.

A submission goes out from its own pull request, behind the `store` environment's reviewers, and
the pull request merges once the stores have taken it. These are the properties that flow rests
on, read from the workflows themselves.
"""

import contextlib
import io
import json
import os
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

    def test_the_review_comment_is_posted_before_the_run_waits_for_approval(self):
        """The run pauses at the submission stage for a reviewer, and a comment posted only when
        the run completes would arrive after the approval it exists to inform. It waits for
        verify alone, which is what cuts the listing it shows out of the release."""
        review = WORKFLOWS["pr"]["jobs"]["review"]
        self.assertEqual(sorted(review["needs"]), ["plan", "verify"])
        self.assertNotIn("submit", review["needs"])
        self.assertNotIn("environment", review)
        self.assertEqual(review["permissions"]["pull-requests"], "write")
        names = [step.get("name") for step in review["steps"]]
        self.assertIn("Post the summary", names)
        post = review["steps"][names.index("Post the summary")]
        self.assertEqual(post["uses"], "./.github/actions/post-review")
        self.assertIn("head.repo.full_name == github.repository", " ".join(post["if"].split()))
        summarize = review["steps"][names.index("Summarize the source changes")]
        self.assertIn("--listing listing", summarize["run"])
        # A fork's pull request still gets its comment after the run, from the base branch.
        fork = WORKFLOWS["comment"]["jobs"]["comment"]
        self.assertIn("head_repository.full_name != github.repository", " ".join(fork["if"].split()))

    def test_the_listing_is_cut_out_once_and_reused(self):
        """Verify fetches the release's screenshots.tar.xz and keeps the listing as an artifact;
        the review shows it and the submission stages from it, so nothing is fetched twice and
        the store receives the files the reviewer saw."""
        for name in ("pr", "publish"):
            verify = WORKFLOWS[name]["jobs"]["verify"]
            names = [step.get("name") for step in verify["steps"]]
            fetch = verify["steps"][names.index("Fetch the release's screenshots")]
            self.assertIn("screenshots.tar.xz", fetch["run"])
            self.assertIn("--gallery release-shots/gallery.json",
                          verify["steps"][names.index("Verify the submission")]["run"])
            cut = verify["steps"][names.index("Cut the listing out of the release")]
            self.assertIn('"$DAY_BIN" screenshot unpack release-shots/screenshots.tar.xz --out release-shots/unpacked', cut["run"])
            self.assertIn("--gallery release-shots/unpacked/gallery.json", cut["run"])
            self.assertIn("--captures release-shots/unpacked --out listing", cut["run"])
            self.assertLess(cut["run"].index("screenshot unpack"), cut["run"].index("queue.py listing"))
            self.assertIn("store screenshots", cut["run"])
            keep = verify["steps"][names.index("Keep the listing for the review and the submission")]
            self.assertEqual(keep["with"]["name"], "listing-${{ matrix.token }}")
        publish = WORKFLOWS["submit"]["jobs"]["publish"]
        downloads = [s for s in publish["steps"] if s.get("uses", "").startswith("actions/download-artifact")]
        listing = next(s for s in downloads if s["with"]["name"] == "listing-${{ matrix.token }}")
        self.assertIn("matrix.screenshots", str(listing["if"]))
        sign = next(s for s in publish["steps"] if s.get("uses") == "./.github/actions/sign-submit")
        self.assertIn("matrix.screenshots && 'listing' || ''", sign["with"]["screenshots"])

    def test_screenshots_can_be_left_alone(self):
        """`screenshots: false` in the submission, or the publish workflow's input, keeps every
        screenshot step out of the run and stages the listing without them."""
        for name in ("pr", "publish"):
            verify = WORKFLOWS[name]["jobs"]["verify"]
            by_name = {s.get("name"): s for s in verify["steps"]}
            for step in ("Fetch the release's screenshots", "Cut the listing out of the release",
                         "Keep the listing for the review and the submission"):
                self.assertEqual(" ".join(str(by_name[step]["if"]).split()), "${{ matrix.screenshots }}", (name, step))
            self.assertIn('if [ "${{ matrix.screenshots }}" = true ]', by_name["Verify the submission"]["run"])
        inputs = WORKFLOWS["publish"]["on"]["workflow_dispatch"]["inputs"] if "on" in WORKFLOWS["publish"] else WORKFLOWS["publish"][True]["workflow_dispatch"]["inputs"]
        self.assertIs(inputs["screenshots"]["default"], True)
        plan = WORKFLOWS["publish"]["jobs"]["plan"]
        self.assertIn("--no-screenshots", " ".join(s.get("run", "") for s in plan["steps"]))

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
        # A case that exercises a failure path prints the annotation that failure would print,
        # and a test run inside Actions would hang it on the job as a real error.
        was = os.environ.pop("GITHUB_ACTIONS", None)
        if was is not None:
            self.addCleanup(lambda: os.environ.__setitem__("GITHUB_ACTIONS", was))
        state = Path(self.temp.name) / "published.json"
        original = catalog.STATE
        catalog.STATE = state
        self.addCleanup(lambda: setattr(catalog, "STATE", original))
        self.state = state

    def record(self, **kwargs):
        """The command's exit code; what it prints belongs to the command, not to this log."""
        fields = {"app": "", "matrix": "", "tag": "", "channels": "", "run_url": "https://example/run"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
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


class ScreenshotPreviewTests(unittest.TestCase):
    def shot(self, prefix="", digest="release", **extra):
        # Synthetic index entries, not bundled app resources.
        path = prefix + "gallery/ios-uikit/iphone/light/home.png"
        return {"path": path, "sha256": digest, "url": "https://example.test/" + path,
                "platform": "ios-uikit", "device": "iphone", "locale": "en", "theme": "light",
                "shot": "home", "title": 'Home " & <play>', **extra}

    def index(self, shots):
        return {"screenshots": shots, "listings": {"ios-uikit": {"stores": {
            "apple-app-store": {"iphone": {"en": [{"shot": "home", "theme": "light"}]}}
        }}}}

    def test_channel_paths_prefer_the_release_channel_without_checking_hashes(self):
        release = self.index([self.shot()])
        site = self.index([self.shot(digest="new-build"), self.shot("main/")])
        shown = catalog.viewable(release, site)["screenshots"][0]
        self.assertEqual(shown["url"], "https://example.test/gallery/ios-uikit/iphone/light/home.png")
        self.assertTrue(shown["website_preview"])
        self.assertNotIn("website_preview", release["screenshots"][0])

    def test_changed_bytes_render_a_labeled_linked_preview(self):
        shown = catalog.viewable(self.index([self.shot()]), self.index([self.shot("main/", "optimized")]))
        section = catalog.screenshot_section(shown, "https://example.test/gallery.json",
                                            artifact=("listing-App", "https://example.test/artifact"))
        self.assertIn('<a href="https://example.test/main/gallery/', section)
        self.assertIn('<img src="https://example.test/main/gallery/', section)
        self.assertIn('title="Website preview; may differ from the submitted release"', section)
        self.assertIn("1 image(s) are website previews", section)
        self.assertIn("Review the listing artifact below", section)
        self.assertIn("Home &quot; &amp; &lt;play&gt;", section)

    def test_missing_and_other_device_captures_are_not_substituted(self):
        other = self.shot("main/")
        other["path"] = other["path"].replace("iphone", "ipad")
        for site in (None, self.index([other])):
            shown = catalog.viewable(self.index([self.shot()]), site)
            self.assertNotIn("url", shown["screenshots"][0])
            self.assertIn("<code>Home &quot; &amp; &lt;play&gt;</code>",
                          catalog.screenshot_section(shown, "https://example.test/gallery.json"))

    def test_no_checksums_cannot_claim_an_exact_capture(self):
        shown = catalog.viewable(self.index([self.shot(digest=None)]),
                                 self.index([self.shot("prerelease/", None)]))
        self.assertTrue(shown["screenshots"][0]["website_preview"])

    def test_site_gallery_reads_all_channels_even_when_release_is_missing(self):
        from unittest.mock import patch
        app = catalog.catalog()[0]
        with patch.object(catalog, "site_host", return_value="https://example.test"), \
                patch.object(catalog, "fetch_json", side_effect=[(404, None), (200, self.index([self.shot("prerelease/")])),
                                                               (200, self.index([self.shot("main/")]))]) as fetch:
            site = catalog.site_gallery(app, "fixture-commit")
        self.assertEqual(len(site["screenshots"]), 2)
        self.assertEqual([c.args[0] for c in fetch.call_args_list],
                         ["https://example.test/" + p for p in catalog.GALLERY_PATHS])

    def test_folded_links_follow_the_published_gallery_channel(self):
        for channel in ("", "main/", "prerelease/"):
            with self.subTest(channel=channel):
                index = self.index([self.shot(channel)])
                index["site"] = "https://example.test"
                section = catalog.screenshot_section(index, "https://example.test/gallery.json", budget=1)
                self.assertIn(f"[open the gallery](https://example.test/en/{channel}gallery/)", section)
                self.assertIn("App Store listing (iPhone, English)", section)
                self.assertNotIn("<img ", section)

    def test_invalid_image_urls_are_not_embedded(self):
        for url in ('javascript:alert(1)', 'https://[invalid'):
            site = self.index([self.shot(url=url)])
            self.assertNotIn("url", catalog.viewable(self.index([self.shot()]), site)["screenshots"][0])


if __name__ == "__main__":
    unittest.main()

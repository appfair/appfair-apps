"""The lint's rules, each against a project that passes and one that does not.

The fixtures are built here rather than checked in, so a rule added later describes its own
failure in its own test instead of in a shared sample tree.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "actions" / "appfair-lint"))

import appfair_lint as lint  # noqa: E402

# Synthetic source fixture; license terms are assessed separately during review.
SPDX = "// SPDX-License-Identifier: MIT\n"


class LintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def good(self) -> Path:
        """An app that passes every rule."""
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "io.github.Demo"\n')
        (self.root / "Day-appfair.toml").write_text('[app]\nid = "org.appfair.app.Demo"\n')
        (self.root / "LICENSE.txt").write_text("Synthetic license fixture for presence checks\n")
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text(SPDX + "pub fn demo() {}\n")
        return self.root

    def findings(self, **kwargs) -> list[lint.Finding]:
        return self.report(**kwargs).findings

    def report(self, project: lint.Project | None = None, **kwargs) -> lint.Report:
        return lint.run(project or lint.Project(root=self.root), **kwargs)

    def codes(self, **kwargs) -> list[str]:
        return [f.rule for f in self.findings(**kwargs)]

    def silently(self, call, *args):
        """The command line prints its report; a test wants the exit code."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return call(*args)

    def test_a_project_that_follows_the_rules_reports_nothing(self):
        self.good()
        self.assertEqual(self.findings(), [])

    def test_every_rule_is_asked_so_one_push_sees_every_problem(self):
        # An empty directory: no manifest, no flavor, no licences, and nothing to read them from.
        codes = self.codes()
        self.assertEqual(
            sorted(codes), ["day-project", "flavor-manifest", "license"]
        )

    def test_a_missing_flavor_manifest_says_what_to_write(self):
        self.good()
        (self.root / "Day-appfair.toml").unlink()
        found = self.findings(only=["flavor-manifest"])
        self.assertEqual(len(found), 1)
        self.assertIn("Day-appfair.toml is missing", found[0].message)
        self.assertIn("org.appfair.app.", found[0].fix)
        # The flavor is an input, so a catalog that names its own finds its own file.
        other = lint.run(
            lint.Project(root=self.root, flavor="gamesfair"),
            only=["flavor-manifest"],
        )
        self.assertIn("Day-gamesfair.toml", other.findings[0].message)

    def test_license_choices_pass_without_network_access(self):
        self.good()
        (self.root / "LICENSE.txt").unlink()
        # Synthetic license texts: the lint checks presence, leaving terms to human review.
        for expression in ("MIT", "Apache-2.0", "GPL-3.0-or-later", "MIT OR Apache-2.0"):
            with self.subTest(expression=expression):
                (self.root / "COPYING").write_text(f"Synthetic {expression} license fixture\n")
                (self.root / "src/lib.rs").write_text(
                    f"// SPDX-License-Identifier: {expression}\npub fn demo() {{}}\n"
                )
                from unittest.mock import patch
                with patch("socket.create_connection", side_effect=AssertionError("network")):
                    report = self.report()
                self.assertEqual(report.findings, [])
                self.assertIn("transitive dependencies", " ".join(report.notes))
                self.assertIn("binary dependency", " ".join(report.notes))

    def test_missing_empty_and_unreadable_license_texts_fail(self):
        self.good()
        p = self.root / "LICENSE.txt"
        for content in (b"", b"   \n", b"\xff"):
            with self.subTest(content=content):
                p.write_bytes(content)
                found = self.findings(only=["license"])
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].path, "LICENSE.txt")
        p.unlink()
        self.assertEqual(self.codes(only=["license"]), ["license"])

    def test_license_file_conventions_and_reuse_directory(self):
        self.good()
        (self.root / "LICENSE.txt").unlink()
        for name in ("LICENSE", "LICENSE.md", "LICENSE-MIT", "COPYING", "COPYING.txt", "LICENSES/MIT.txt"):
            with self.subTest(name=name):
                p = self.root / name
                p.parent.mkdir(exist_ok=True)
                p.write_text("Synthetic license fixture for presence check\n")
                self.assertEqual(self.findings(only=["license"]), [])
                p.unlink()

    def test_license_presence_does_not_claim_compliance(self):
        self.good()
        (self.root / "LICENSE.txt").write_text("Synthetic nonfree terms for manual review\n")
        report = self.report(only=["license"])
        self.assertEqual(report.findings, [])
        self.assertIn("do not establish compliance", report.notes[0])
        import json
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            lint.main(["--path", str(self.root), "--only", "license", "--format", "json"])
        self.assertEqual(json.loads(output.getvalue())["notes"], report.notes)

    def test_an_id_android_cannot_build_is_named_with_the_platform_and_the_fix(self):
        self.good()
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "io.github.fair-starter"\n')
        found = self.findings(only=["app-ids"])
        self.assertEqual({f.rule for f in found}, {"app-ids"})
        # One per platform that cannot build it, and Apple is not among them.
        self.assertEqual(len(found), 2)
        self.assertTrue(any("android" in f.message for f in found))
        self.assertTrue(any("harmony" in f.message for f in found))
        self.assertFalse(any("apple" in f.message for f in found))
        self.assertIn("io.github.fair_starter", found[0].fix)

    def test_an_override_covers_the_platform_it_names_and_no_other(self):
        """The failure this rule was written for: an android override, no harmony one."""
        self.good()
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "io.github.Demo"\n')
        (self.root / "Day-appfair.toml").write_text(
            '[app]\nid = "org.appfair.app.Faire-Games"\n'
            '[app.android]\nid = "org.appfair.app.Faire_Games"\n'
        )
        found = self.findings(only=["app-ids"])
        self.assertEqual([f.path for f in found], ["Day-appfair.toml"])
        self.assertIn("harmony", found[0].message)

        # With the harmony override too, the hyphen is Apple's alone and nothing is reported.
        (self.root / "Day-appfair.toml").write_text(
            '[app]\nid = "org.appfair.app.Faire-Games"\n'
            '[app.android]\nid = "org.appfair.app.Faire_Games"\n'
            '[app.harmony]\nid = "org.appfair.app.Faire_Games"\n'
        )
        self.assertEqual(self.findings(only=["app-ids"]), [])

    def test_an_id_that_is_not_reverse_dns_or_not_toml_is_reported(self):
        self.good()
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "demo"\n')
        self.assertIn("one segment", self.findings(only=["app-ids"])[0].message)
        (self.root / "Day.toml").write_text("schema = 1\n[app\n")
        self.assertIn("does not parse", self.findings(only=["app-ids"])[0].message)

    def test_a_template_s_placeholders_are_left_to_the_app_scaffolded_from_it(self):
        self.good()
        (self.root / "Day.toml").write_text(
            'schema = 1\n[app]\nid = "{{id}}"\ntargets = [{{targets_toml}}]\n'
        )
        (self.root / "Day-appfair.toml").write_text('[app]\nid = "org.appfair.app.{{repo}}"\n')
        self.assertEqual(self.findings(only=["app-ids"]), [])

    def test_a_rust_file_without_the_notice_is_named_with_the_line_to_add(self):
        self.good()
        (self.root / "src" / "main.rs").write_text("fn main() {}\n")
        found = self.findings(only=["spdx-headers"])
        self.assertEqual([f.path for f in found], ["src/main.rs"])
        self.assertIn("SPDX-License-Identifier", found[0].fix)

    def test_empty_or_unlabelled_source_notices_fail(self):
        self.good()
        for notice in ("// MIT", "// SPDX-License-Identifier:   ", "// MIT OR Apache-2.0"):
            with self.subTest(notice=notice):
                (self.root / "src/lib.rs").write_text(notice + "\n")
                self.assertEqual(self.codes(only=["spdx-headers"]), ["spdx-headers"])

    def test_the_notice_may_follow_a_shebang_or_an_attribute(self):
        self.good()
        (self.root / "src" / "top.rs").write_text(f"#![allow(dead_code)]\n\n{SPDX}fn top() {{}}\n")
        self.assertEqual(self.findings(only=["spdx-headers"]), [])

    def test_build_output_is_not_the_app_s_source(self):
        self.good()
        for directory in ("target", "build", "node_modules"):
            (self.root / directory).mkdir()
            (self.root / directory / "generated.rs").write_text("fn generated() {}\n")
        self.assertEqual(self.findings(only=["spdx-headers"]), [])

    def test_rules_can_be_selected_and_a_broken_rule_reports_itself(self):
        self.good()
        self.assertEqual(self.report(only=["license"]).checked, ["license"])
        checked = self.report(skip=["license"]).checked
        self.assertNotIn("license", checked)
        self.assertIn("spdx-headers", checked)

        def explode(project):
            raise RuntimeError("a rule with a bug in it")

        original = lint.RULES["license"]
        lint.RULES["license"] = lint.Rule("license", original.summary, explode)
        self.addCleanup(lambda: lint.RULES.__setitem__("license", original))
        found = self.report(only=["license"]).findings
        self.assertEqual(len(found), 1)
        self.assertIn("a rule with a bug in it", found[0].message)

    def test_the_command_line_answers_with_an_exit_code(self):
        self.good()
        run = lambda *argv: self.silently(lint.main, ["--path", str(self.root), *argv])  # noqa: E731
        self.assertEqual(run("--format", "json"), 0)
        (self.root / "src" / "main.rs").write_text("fn main() {}\n")
        self.assertEqual(run("--format", "json"), 1)
        self.assertEqual(run("--list"), 0)
        with self.assertRaises(SystemExit):
            run("--only", "no-such-rule")

    def test_the_action_calls_the_rules_it_documents(self):
        """The action's inputs and the README's table against the rules that exist."""
        import yaml

        action = yaml.safe_load((ROOT / ".github/actions/appfair-lint/action.yml").read_text())
        self.assertEqual(
            sorted(action["inputs"]), ["flavor", "only", "path", "skip"]
        )
        # Every input reaches the script, or it is an input in name only.
        run = action["runs"]["steps"][0]["run"]
        for name in action["inputs"]:
            self.assertIn(f"--{name} ", run, f"the action never passes {name} on")
        readme = (ROOT / ".github/actions/appfair-lint/README.md").read_text()
        for code in lint.RULES:
            self.assertIn(f"`{code}`", readme, f"{code} is not in the action's README")


if __name__ == "__main__":
    unittest.main()

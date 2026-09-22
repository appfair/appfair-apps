"""The lint's rules, each against a project that passes and one that does not.

The fixtures are built here rather than checked in, so a rule added later describes its own
failure in its own test instead of in a shared sample tree.
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "actions" / "appfair-lint"))

import appfair_lint as lint  # noqa: E402

SPDX = f"{lint.SPDX_LINE}\n"


class LintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def good(self) -> Path:
        """An app that passes every rule."""
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "io.github.Demo"\n')
        (self.root / "Day-appfair.toml").write_text('[app]\nid = "org.appfair.app.Demo"\n')
        for name in ("LICENSE.txt", "LICENSE-EXCEPTIONS.txt"):
            shutil.copy(lint.REFERENCE / name, self.root / name)
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text(SPDX + "pub fn demo() {}\n")
        return self.root

    def findings(self, **kwargs) -> list[lint.Finding]:
        return lint.run(lint.Project(root=self.root), **kwargs).findings

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
            sorted(codes), ["day-project", "flavor-manifest", "license", "license-exception"]
        )

    def test_a_missing_flavor_manifest_says_what_to_write(self):
        self.good()
        (self.root / "Day-appfair.toml").unlink()
        found = self.findings(only=["flavor-manifest"])
        self.assertEqual(len(found), 1)
        self.assertIn("Day-appfair.toml is missing", found[0].message)
        self.assertIn("org.appfair.app.", found[0].fix)
        # The flavor is an input, so a catalog that names its own finds its own file.
        other = lint.run(lint.Project(root=self.root, flavor="gamesfair"), only=["flavor-manifest"])
        self.assertIn("Day-gamesfair.toml", other.findings[0].message)

    def test_an_edited_licence_is_reported_at_the_line_that_differs(self):
        self.good()
        text = (self.root / "LICENSE.txt").read_text().splitlines()
        text[12] = "  Copyright (C) 2026 Someone Else"
        (self.root / "LICENSE.txt").write_text("\n".join(text) + "\n")
        found = self.findings(only=["license"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 13)
        self.assertIn("Someone Else", found[0].message)

    def test_a_truncated_licence_is_reported_by_its_length(self):
        self.good()
        keep = (self.root / "LICENSE-EXCEPTIONS.txt").read_text().splitlines()[:10]
        (self.root / "LICENSE-EXCEPTIONS.txt").write_text("\n".join(keep) + "\n")
        found = self.findings(only=["license-exception"])
        self.assertEqual(len(found), 1)
        self.assertIn("shorter", found[0].message)

    def test_a_rust_file_without_the_notice_is_named_with_the_line_to_add(self):
        self.good()
        (self.root / "src" / "main.rs").write_text("fn main() {}\n")
        found = self.findings(only=["spdx-headers"])
        self.assertEqual([f.path for f in found], ["src/main.rs"])
        self.assertIn(lint.SPDX_LINE, found[0].fix)

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
        self.assertEqual(lint.run(lint.Project(root=self.root), only=["license"]).checked, ["license"])
        checked = lint.run(lint.Project(root=self.root), skip=["license"]).checked
        self.assertNotIn("license", checked)
        self.assertIn("spdx-headers", checked)

        def explode(project):
            raise RuntimeError("a rule with a bug in it")

        original = lint.RULES["license"]
        lint.RULES["license"] = lint.Rule("license", original.summary, explode)
        self.addCleanup(lambda: lint.RULES.__setitem__("license", original))
        found = lint.run(lint.Project(root=self.root), only=["license"]).findings
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
        self.assertEqual(sorted(action["inputs"]), ["flavor", "only", "path", "skip"])
        readme = (ROOT / ".github/actions/appfair-lint/README.md").read_text()
        for code in lint.RULES:
            self.assertIn(f"`{code}`", readme, f"{code} is not in the action's README")


if __name__ == "__main__":
    unittest.main()

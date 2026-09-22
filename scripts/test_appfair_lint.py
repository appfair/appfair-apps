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
        # No test reaches the network: the template answers out of `reference/`, which holds the
        # same text. A test that wants a different answer passes its own `template()`.
        original = lint.http_get
        lint.http_get = lambda url: (lint.REFERENCE / url.rsplit("/", 1)[-1]).read_text()
        self.addCleanup(lambda: setattr(lint, "http_get", original))

    def template(self, files: dict[str, str] | None = None, fail: Exception | None = None):
        """A template served from memory, so the tests never reach the network.

        `files` overrides what a name returns; anything else comes from `reference/`, which is
        what the template holds. `fail` is raised instead, for the fallback and error paths.
        """
        served = dict(files or {})

        def get(url: str) -> str:
            if fail is not None:
                raise fail
            name = url.rsplit("/", 1)[-1]
            if name in served:
                return served[name]
            return (lint.REFERENCE / name).read_text()

        return lint.Template(spec="appfair/day-appfair@main", get=get)

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
        return self.report(**kwargs).findings

    def report(self, project: lint.Project | None = None, **kwargs) -> lint.Report:
        return lint.run(project or lint.Project(root=self.root, template=self.template()), **kwargs)

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
        other = lint.run(
            lint.Project(root=self.root, flavor="gamesfair", template=self.template()),
            only=["flavor-manifest"],
        )
        self.assertIn("Day-gamesfair.toml", other.findings[0].message)

    def test_the_licence_is_compared_with_the_app_template_s_own(self):
        """The template is the source: a licence that matches `reference/` but not the template
        is still wrong, and the finding says which text it was measured against."""
        self.good()
        moved_on = (lint.REFERENCE / "LICENSE.txt").read_text().replace(
            "GNU AFFERO GENERAL PUBLIC LICENSE", "GNU AFFERO GENERAL PUBLIC LICENCE", 1
        )
        project = lint.Project(
            root=self.root, template=self.template({"LICENSE.txt": moved_on})
        )
        found = self.report(project, only=["license"]).findings
        self.assertEqual(len(found), 1)
        self.assertIn("LICENCE", found[0].message)
        self.assertIn(
            "raw.githubusercontent.com/appfair/day-appfair/main/LICENSE.txt", found[0].message
        )
        self.assertIn("github.com/appfair/day-appfair/blob/main/LICENSE.txt", found[0].fix)

    def test_a_template_that_cannot_be_reached_falls_back_and_says_so(self):
        """A network failure is not the app's fault, so the run uses this action's copy and
        notes it. An answer of 4xx is a wrong `template` input, and fails."""
        self.good()
        import urllib.error

        offline = lint.Project(
            root=self.root,
            template=self.template(fail=urllib.error.URLError("no route to host")),
        )
        report = self.report(offline, only=["license"])
        self.assertEqual(report.findings, [])
        self.assertEqual(len(report.notes), 1)
        self.assertIn("this action's copy", report.notes[0])

        gone = urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        self.addCleanup(gone.close)
        missing = lint.Project(root=self.root, template=self.template(fail=gone))
        found = self.report(missing, only=["license"]).findings
        self.assertEqual(len(found), 1)
        self.assertIn("404", found[0].message)
        self.assertIn("`template` input", found[0].fix)

    def test_the_action_s_copies_are_checked_against_the_template(self):
        """`--check-template` is what keeps the offline fallback honest."""
        self.assertEqual(self.silently(lint.check_template, self.template()), 0)
        drifted = self.template({"LICENSE.txt": "A licence of my own\n"})
        self.assertEqual(self.silently(lint.check_template, drifted), 1)

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
            sorted(action["inputs"]), ["flavor", "only", "path", "skip", "template"]
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

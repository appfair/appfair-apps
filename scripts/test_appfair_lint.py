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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "actions" / "appfair-lint"))

import appfair_lint as lint  # noqa: E402

# Source notice for the synthetic app fixture.
SPDX = "// SPDX-License-Identifier: GPL-2.0-or-later\n"

# What the fixture carries for the `appfair-module` rule: the dependency, and the settings builder
# that is handed the App Fair group.
MODULE = '[dependencies]\nday-piece-appfair = "0.*"\n'
SETTINGS = SPDX + """\
use day_piece_settings::Preferences;

pub fn install() {
    Preferences::new()
        .provider(&day_piece_appfair::APP_FAIR)
        .install();
}
"""
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def lock(*versions: str, source: str | None = REGISTRY, checksum: bool = True) -> str:
    """A Cargo.lock holding the app and one day-piece-appfair package per version. A registry
    entry carries its checksum, as cargo writes it, unless `checksum` says otherwise."""
    text = 'version = 4\n\n[[package]]\nname = "demo"\nversion = "1.0.0"\n'
    for version in versions:
        text += f'\n[[package]]\nname = "day-piece-appfair"\nversion = "{version}"\n'
        if source:
            text += f'source = "{source}"\n'
        if source and source.startswith("registry+") and checksum:
            text += f'checksum = "{"a" * 64}"\n'
    return text


class LintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def good(self) -> Path:
        """An app that passes every rule."""
        (self.root / "Day.toml").write_text('schema = 1\n[app]\nid = "io.github.Demo"\n')
        (self.root / "Day-appfair.toml").write_text('[app]\nid = "org.appfair.app.Demo"\n')
        shutil.copyfile(lint.REFERENCE, self.root / "LICENSE.txt")
        (self.root / "Cargo.toml").write_text(MODULE)
        (self.root / "Cargo.lock").write_text(lock(lint.APPFAIR_MODULE_MINIMUM))
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text(SPDX + "pub fn demo() {}\n")
        (self.root / "src" / "settings.rs").write_text(SETTINGS)
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
        # An empty directory: no manifest, no flavor, no licences, no App Fair module, and nothing
        # to read them from.
        self.assertEqual(
            set(self.codes()), {"day-project", "flavor-manifest", "license", "appfair-module"}
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

    def test_other_app_licenses_fail_even_with_the_reference_text(self):
        self.good()
        for expression in ("MIT", "Apache-2.0", "GPL-2.0-only", "GPL-3.0-only", "GPL-3.0-or-later",
                           "AGPL-3.0-only", "GPL-2.0-or-later WITH Classpath-exception-2.0",
                           "MIT OR GPL-2.0-or-later"):
            with self.subTest(expression=expression):
                (self.root / "Cargo.toml").write_text(f'[package]\nlicense = "{expression}"\n{MODULE}')
                self.assertEqual([f.path for f in self.findings()], ["Cargo.toml"])

    def test_app_package_license_can_be_inherited_and_third_party_files_keep_their_notices(self):
        self.good()
        (self.root / "src/lib.rs").write_text("// SPDX-License-Identifier: MIT\n")
        self.assertEqual(self.codes(), ["license"])
        expression = "GPL-2.0-or-later"
        for package in (f'license = "{expression}"', f'license.workspace = true\n[workspace.package]\nlicense = "{expression}"'):
            with self.subTest(package=package):
                (self.root / "Cargo.toml").write_text(f'[package]\n{package}\n{MODULE}')
                self.assertEqual(self.findings(), [])

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

    def test_license_text_passes_with_formatting_differences(self):
        self.good()
        path = self.root / "LICENSE.txt"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        report = self.report(only=["license"])
        self.assertEqual(report.findings, [])
        self.assertIn("transitive dependencies", report.notes[0])
        self.assertIn("binary dependency", report.notes[0])
        import json
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            lint.main(["--path", str(self.root), "--only", "license", "--format", "json"])
        self.assertEqual(json.loads(output.getvalue())["notes"], report.notes)

    def test_wrong_primary_license_text_fails_even_with_an_extra_gpl_copy(self):
        self.good()
        (self.root / "LICENSE.txt").rename(self.root / "COPYING")
        (self.root / "LICENSE.txt").write_text("Synthetic permissive license\n")
        self.assertEqual([f.path for f in self.findings()], ["LICENSE.txt"])

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

    # --- appfair-module -------------------------------------------------------------------

    def module(self) -> list[lint.Finding]:
        return self.findings(only=["appfair-module"])

    def settings(self, body: str, name: str = "src/settings.rs") -> None:
        """Replace the settings builder with `body`, the chain after `Preferences::new()`."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{SPDX}pub fn install() {{\n    Preferences::new()\n{body}\n}}\n")

    def test_an_app_that_adopts_the_module_passes(self):
        self.good()
        self.assertEqual(self.module(), [])
        # The lock names a crates.io release, at the minimum or past it.
        for version in (lint.APPFAIR_MODULE_MINIMUM, "0.1.1", "0.2.0", "1.0.0"):
            with self.subTest(version=version):
                (self.root / "Cargo.lock").write_text(lock(version))
                self.assertEqual(self.module(), [])

    def test_an_app_without_the_module_is_told_each_step(self):
        self.good()
        (self.root / "Cargo.toml").write_text('[package]\nname = "demo"\n\n[dependencies]\nday = "0.*"\n')
        (self.root / "Cargo.lock").write_text(lock())
        self.settings("        .install();")
        manifest, locked, source = self.module()
        self.assertEqual((manifest.path, manifest.line), ("Cargo.toml", 4))
        self.assertIn("has no day-piece-appfair under [dependencies]", manifest.message)
        self.assertIn('day-piece-appfair = "0.*"', manifest.fix)
        self.assertEqual(locked.path, "Cargo.lock")
        self.assertIn("locks no day-piece-appfair", locked.message)
        self.assertIn("cargo update --workspace", locked.fix)
        # The file and line of the builder the call belongs on.
        self.assertEqual((source.path, source.line), ("src/settings.rs", 3))
        self.assertIn("no source under src/ passes the App Fair provider", source.message)
        self.assertIn(".provider(&day_piece_appfair::APP_FAIR)", source.fix)

    def test_the_dependency_is_a_crates_io_version_requirement(self):
        self.good()
        accepted = (
            'day-piece-appfair = "0.*"',
            'day-piece-appfair = "0.1"',
            'day-piece-appfair = { version = "0.*" }',
            'day-piece-appfair = { version = "0.*", features = ["x"], default-features = false }',
            'day-piece-appfair.version = "0.*"',
            'day = "0.*"\n[dependencies.day-piece-appfair]\nversion = "0.*"',
            # Inherited: `[workspace.dependencies]` is where the version is stated.
            'day-piece-appfair = { workspace = true }\n[workspace.dependencies]\nday-piece-appfair = "0.*"',
            'day-piece-appfair.workspace = true\n[workspace.dependencies]\nday-piece-appfair = { version = "0.*" }',
        )
        for entry in accepted:
            with self.subTest(entry=entry):
                (self.root / "Cargo.toml").write_text(f"[dependencies]\n{entry}\n")
                self.assertEqual(self.module(), [])

    def test_a_path_or_git_dependency_is_not_a_release_input(self):
        self.good()
        refused = {
            'day-piece-appfair = { path = "../day-piece-appfair" }': "from a local path",
            'day-piece-appfair = { version = "0.*", path = "../day-piece-appfair" }': "from a local path",
            'day-piece-appfair = { git = "https://example.test/day-piece-appfair" }': "from a git repository",
            'day-piece-appfair = { version = "0.*", git = "https://example.test/x", tag = "v0.1.0" }': "from a git repository",
            'day-piece-appfair = { workspace = true }\n[workspace.dependencies]\n'
            'day-piece-appfair = { path = "../day-piece-appfair" }': "from a local path",
            'day-piece-appfair = { features = ["x"] }': "no version",
            # A workspace entry this manifest does not define states no version here.
            'day-piece-appfair = { workspace = true }': "no version",
        }
        for entry, reason in refused.items():
            with self.subTest(entry=entry):
                (self.root / "Cargo.toml").write_text(f'[package]\nname = "demo"\n[dependencies]\n{entry}\n')
                found = self.module()
                self.assertEqual([(f.path, f.line) for f in found], [("Cargo.toml", 4)])
                self.assertIn(reason, found[0].message)
                self.assertIn('day-piece-appfair = "0.*"', found[0].fix)

    def test_a_dependency_some_builds_leave_out_is_not_enough(self):
        self.good()
        tables = {
            "[target.'cfg(not(target_arch = \"wasm32\"))'.dependencies]": "target.'cfg(not(target_arch = \"wasm32\"))'.dependencies",
            "[dev-dependencies]": "dev-dependencies",
            "[build-dependencies]": "build-dependencies",
        }
        for header, name in tables.items():
            with self.subTest(table=name):
                (self.root / "Cargo.toml").write_text(f'[dependencies]\nday = "0.*"\n\n{header}\nday-piece-appfair = "0.*"\n')
                found = self.module()
                self.assertEqual([(f.path, f.line) for f in found], [("Cargo.toml", 5)])
                self.assertIn(f"under [{name}] and not under [dependencies]", found[0].message)
                self.assertIn("move it under [dependencies]", found[0].fix)

    def test_a_renamed_dependency_counts_and_the_call_follows_the_name(self):
        self.good()
        (self.root / "Cargo.toml").write_text(
            '[dependencies]\nfair = { package = "day-piece-appfair", version = "0.*" }\n'
        )
        self.settings("        .provider(&fair::APP_FAIR)\n        .install();")
        self.assertEqual(self.module(), [])
        # The crate's own name is not one this app's code can use.
        (self.root / "src" / "settings.rs").write_text(SETTINGS)
        found = self.module()
        self.assertEqual(len(found), 1)
        self.assertIn(".provider(&fair::APP_FAIR)", found[0].fix)
        # A path source is refused under any name, and the fix keeps the name.
        (self.root / "Cargo.toml").write_text(
            '[dependencies]\nfair = { package = "day-piece-appfair", path = "../fair" }\n'
        )
        self.assertIn('fair = { package = "day-piece-appfair", version = "0.*" }', self.module()[0].fix)
        # Another crate under the module's name is not the module.
        (self.root / "Cargo.toml").write_text(
            '[dependencies]\nday-piece-appfair = { package = "something-else", version = "1" }\n'
        )
        self.assertIn("has no day-piece-appfair under [dependencies]", self.module()[0].message)

    def test_a_manifest_that_does_not_parse_is_reported(self):
        self.good()
        (self.root / "Cargo.toml").write_text("[dependencies\n")
        found = self.module()
        self.assertEqual([f.path for f in found], ["Cargo.toml"])
        self.assertIn("Cannot read Cargo.toml", found[0].message)
        (self.root / "Cargo.toml").write_text(MODULE)
        (self.root / "Cargo.lock").write_text("[[package]\n")
        found = self.module()
        self.assertEqual([f.path for f in found], ["Cargo.lock"])
        self.assertIn("Cannot read Cargo.lock", found[0].message)

    def test_a_lock_older_than_the_minimum_names_the_version_and_the_command(self):
        self.good()
        self.assertEqual(lint.APPFAIR_MODULE_MINIMUM, "0.1.0")
        # A pre-release of the minimum comes before it.
        for version in ("0.0.9", "0.1.0-rc.1", "0.0.0"):
            with self.subTest(version=version):
                (self.root / "Cargo.lock").write_text(lock(version))
                found = self.module()
                # The entry's `version` line: the app, a blank line, the header, the name.
                self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", 9)])
                self.assertIn(f"locks day-piece-appfair {version}", found[0].message)
                self.assertIn("takes 0.1.0 or newer", found[0].message)
                self.assertIn("cargo update -p day-piece-appfair", found[0].fix)
        # Two locked releases: the older one is still part of the build.
        (self.root / "Cargo.lock").write_text(lock("0.0.9", "0.2.0"))
        self.assertEqual(len(self.module()), 1)

    def test_raising_the_minimum_is_one_constant(self):
        self.good()
        (self.root / "Cargo.lock").write_text(lock("0.1.5"))
        self.assertEqual(self.module(), [])
        with patch.object(lint, "APPFAIR_MODULE_MINIMUM", "0.2.0"):
            found = self.module()
            self.assertIn("locks day-piece-appfair 0.1.5", found[0].message)
            self.assertIn("takes 0.2.0 or newer", found[0].message)

    def test_a_lock_without_the_package_says_how_to_lock_it(self):
        self.good()
        (self.root / "Cargo.lock").write_text(lock())
        found = self.module()
        self.assertEqual([f.path for f in found], ["Cargo.lock"])
        self.assertIn("locks no day-piece-appfair", found[0].message)
        self.assertIn("cargo update --workspace", found[0].fix)

    def test_an_app_that_commits_no_lock_is_told_to_commit_one(self):
        self.good()
        (self.root / "Cargo.lock").unlink()
        found = self.module()
        self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", None)])
        self.assertIn("commits no Cargo.lock", found[0].message)
        for step in (".cargo/config.toml", "cargo generate-lockfile", "cargo metadata --locked",
                     "commit Cargo.lock"):
            self.assertIn(step, found[0].fix)
        # A directory under that name is no lock either.
        (self.root / "Cargo.lock").mkdir()
        self.assertIn("commits no Cargo.lock", self.module()[0].message)

    def test_a_lock_entry_from_a_local_checkout_is_not_a_release_input(self):
        """A patch table points the crates.io requirement at a checkout, and cargo then writes
        the entry without a `source`."""
        self.good()
        (self.root / "Cargo.lock").write_text(lock("0.1.0", source=None))
        found = self.module()
        # The entry's `name` line, since it has no `source` line to point at.
        self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", 8)])
        self.assertIn("locks day-piece-appfair 0.1.0 from a local checkout", found[0].message)
        self.assertIn("has no `source`", found[0].message)
        for step in (".cargo/config.toml", "day patch --local", "cargo update --workspace",
                     "cargo metadata --locked", "commit Cargo.lock"):
            self.assertIn(step, found[0].fix)
        # The version it records is held to the minimum as well.
        (self.root / "Cargo.lock").write_text(lock("0.0.1", source=None))
        source, version = self.module()
        self.assertIn("from a local checkout", source.message)
        self.assertIn("locks day-piece-appfair 0.0.1, and the App Fair takes", version.message)
        # A path dependency is reported in the manifest that asks for it and in the lock it
        # leaves, so one push names both edits.
        (self.root / "Cargo.toml").write_text('[dependencies]\nday-piece-appfair = { path = "../x" }\n')
        (self.root / "Cargo.lock").write_text(lock("0.1.0", source=None))
        self.assertEqual([f.path for f in self.module()], ["Cargo.toml", "Cargo.lock"])

    def test_a_lock_entry_from_anywhere_but_crates_io_is_named_with_its_source(self):
        self.good()
        sources = {
            "git+https://example.test/day-piece-appfair?branch=main#0123abc": "from a git repository",
            "registry+https://registry.example.test/index": "from another registry",
            "sparse+https://registry.example.test/index/": "from another registry",
        }
        for source, origin in sources.items():
            with self.subTest(source=source):
                (self.root / "Cargo.lock").write_text(lock("0.1.0", source=source))
                found = self.module()
                # The entry's `source` line.
                self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", 10)])
                self.assertIn(f"locks day-piece-appfair 0.1.0 {origin} ({source})", found[0].message)
                self.assertIn("cargo update --workspace", found[0].fix)
        # One release from crates.io does not excuse a second copy from elsewhere.
        text = lock("0.1.0") + lock("0.2.0", source=None).split("\n\n", 2)[2]
        (self.root / "Cargo.lock").write_text(text)
        found = self.module()
        self.assertEqual(len(found), 1)
        self.assertIn("locks day-piece-appfair 0.2.0 from a local checkout", found[0].message)

    def test_a_registry_entry_carries_its_checksum(self):
        self.good()
        (self.root / "Cargo.lock").write_text(lock("0.1.0", checksum=False))
        found = self.module()
        # No `checksum` line to point at, so the entry's `name` line.
        self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", 8)])
        self.assertIn("from crates.io with no checksum", found[0].message)
        self.assertIn("cargo update --workspace", found[0].fix)
        self.assertNotIn(".cargo/config.toml", found[0].fix)
        # An empty one is no checksum.
        (self.root / "Cargo.lock").write_text(
            lock("0.1.0", checksum=False) + 'checksum = ""\n'
        )
        found = self.module()
        self.assertEqual([(f.path, f.line) for f in found], [("Cargo.lock", 11)])
        self.assertIn("with no checksum", found[0].message)

    def test_versions_order_as_semver_orders_them(self):
        ordered = ["0.0.9", "0.1.0-alpha", "0.1.0-alpha.1", "0.1.0-rc.2", "0.1.0-rc.10", "0.1.0",
                   "0.1.1", "0.2.0", "0.10.0", "1.0.0"]
        keys = [lint.version_key(v) for v in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(lint.version_key("0.1.0+build.5"), lint.version_key("0.1.0"))
        for text in ("", "0.1", "v0.1.0", "0.1.0.0", "latest"):
            self.assertIsNone(lint.version_key(text))
            self.assertFalse(lint.version_at_least(text, "0.1.0"))
        self.assertIsNotNone(lint.version_key(lint.APPFAIR_MODULE_MINIMUM))

    def test_the_call_is_found_however_it_is_formatted(self):
        self.good()
        chains = (
            "        .provider(&day_piece_appfair::APP_FAIR)\n        .install();",
            "        .provider( & day_piece_appfair :: APP_FAIR )\n        .install();",
            "        .provider(\n            &day_piece_appfair::APP_FAIR,\n        )\n        .install();",
            "        .provider(&::day_piece_appfair::APP_FAIR).install();",
            "        .\n        provider\n        (&day_piece_appfair::APP_FAIR)\n        .install();",
            # A quoted URL's slashes start no comment.
            '        .link("https://appfair.org").provider(&day_piece_appfair::APP_FAIR).install();',
            "        .separator('\"').provider(&day_piece_appfair::APP_FAIR) // the App Fair group\n        .install();",
            # Nor do the slashes inside a raw string, whose quotes do not end it.
            '        .pattern(r#"" // "#).provider(&day_piece_appfair::APP_FAIR).install();',
        )
        for chain in chains:
            with self.subTest(chain=chain):
                self.settings(chain)
                self.assertEqual(self.module(), [])

    def test_the_constant_may_be_imported(self):
        self.good()
        call = "pub fn install() {\n    Preferences::new().provider(&APP_FAIR).install();\n}\n"
        accepted = (
            "use day_piece_appfair::APP_FAIR;",
            "use ::day_piece_appfair::APP_FAIR;",
            "pub(crate) use day_piece_appfair::APP_FAIR;",
            "use day_piece_appfair::{APP_FAIR};",
            "use day_piece_appfair::{self, APP_FAIR, Channel};",
            "use day_piece_appfair::{\n    Channel,\n    APP_FAIR,\n};",
        )
        for line in accepted:
            with self.subTest(use=line):
                (self.root / "src" / "settings.rs").write_text(f"{SPDX}{line}\n\n{call}")
                self.assertEqual(self.module(), [])
        refused = (
            "",
            "use day_piece_appfair::Channel;",
            "use day_piece_appfair::{Channel};\nuse other_crate::APP_FAIR;",
            "use day_piece_appfair::APP_FAIR as FAIR;",
            "// use day_piece_appfair::APP_FAIR;",
        )
        for line in refused:
            with self.subTest(use=line):
                (self.root / "src" / "settings.rs").write_text(f"{SPDX}{line}\n\n{call}")
                self.assertEqual([f.path for f in self.module()], ["src/settings.rs"])
        # The `use` and the call have to share a file.
        (self.root / "src" / "lib.rs").write_text(SPDX + "use day_piece_appfair::APP_FAIR;\n")
        (self.root / "src" / "settings.rs").write_text(SPDX + call)
        self.assertEqual(len(self.module()), 1)

    def test_a_commented_out_call_is_not_a_call(self):
        self.good()
        chains = (
            "        // .provider(&day_piece_appfair::APP_FAIR)\n        .install();",
            "        .install(); // .provider(&day_piece_appfair::APP_FAIR)",
            "        /// .provider(&day_piece_appfair::APP_FAIR)\n        .install();",
            "        /* .provider(&day_piece_appfair::APP_FAIR) */\n        .install();",
            "        /*\n        .provider(&day_piece_appfair::APP_FAIR)\n        */\n        .install();",
            # A quote character is not the start of a string that would swallow the comment.
            "        .separator('\"') // .provider(&day_piece_appfair::APP_FAIR) \"\n        .install();",
        )
        for chain in chains:
            with self.subTest(chain=chain):
                self.settings(chain)
                found = self.module()
                self.assertEqual([(f.path, f.line) for f in found], [("src/settings.rs", 3)])
                self.assertIn("no source under src/", found[0].message)

    def test_the_call_has_to_be_in_the_app_s_own_sources(self):
        self.good()
        (self.root / "src" / "settings.rs").unlink()
        for elsewhere in ("examples/demo.rs", "tests/settings.rs", "crates/ui/src/lib.rs"):
            self.settings("        .provider(&day_piece_appfair::APP_FAIR)\n        .install();", elsewhere)
        found = self.module()
        self.assertEqual(len(found), 1)
        # No builder under src/ to point at.
        self.assertIsNone(found[0].path)
        # Any depth under src/ counts.
        self.settings("        .provider(&day_piece_appfair::APP_FAIR)\n        .install();", "src/ui/settings.rs")
        self.assertEqual(self.module(), [])

    def test_taking_the_group_out_again_is_reported_where_it_happens(self):
        self.good()
        provider = "        .provider(&day_piece_appfair::APP_FAIR)\n"
        removals = (
            '        .without_provider("appfair")',
            '        .without_provider( "appfair" , )',
            '        .without_provider(\n            "appfair",\n        )',
            # The crate's constant for the same id.
            "        .without_provider(day_piece_appfair::GROUP_ID)",
            "        .without_provider(GROUP_ID)",
        )
        for call in removals:
            with self.subTest(call=call):
                self.settings(f"{provider}{call}\n        .install();")
                found = self.module()
                self.assertEqual([(f.path, f.line) for f in found], [("src/settings.rs", 5)])
                self.assertIn('.without_provider("appfair")', found[0].message)
                self.assertIn("delete the", found[0].fix)
        # Other groups are the app's to leave out, and a comment removes nothing. Under
        # `Providers::None` the builder still collects the providers an app names, and this app
        # names the App Fair's.
        kept = (
            '        .without_provider("selfupdate")',
            '        // .without_provider("appfair")',
            '        /* .without_provider("appfair") */',
            "        .providers(Providers::None)",
            "        .providers(day_piece_settings::Providers::None)",
            "        .providers(Providers::Registered)",
        )
        for call in kept:
            with self.subTest(call=call):
                self.settings(f"{provider}{call}\n        .install();")
                self.assertEqual(self.module(), [])
        # Any Rust source can hold the builder, so every one is read for a removal.
        self.settings(f'{provider}        .without_provider("appfair")\n        .install();', "crates/ui/src/lib.rs")
        self.assertEqual([f.path for f in self.module()], ["crates/ui/src/lib.rs"])

    def test_the_module_s_group_id_is_not_the_app_s_to_declare(self):
        self.good()
        provider = "        .provider(&day_piece_appfair::APP_FAIR)\n"
        declared = (
            '        .group(Group::new("appfair", "About the Fair"))',
            '        .group(Group :: new ( "appfair" , title()))',
            '        .group(\n            Group::new(\n                "appfair",\n                title(),\n            ),\n        )',
            '        .group(day_piece_settings::Group::new("appfair", title()))',
            '        .group(settings::Group::new("appfair", title()))',
            # The crate's constant for the same id.
            "        .group(Group::new(day_piece_appfair::GROUP_ID, title()))",
            "        .group(Group::new(::day_piece_appfair::GROUP_ID, title()))",
        )
        for call in declared:
            with self.subTest(call=call):
                self.settings(f"{provider}{call}\n        .install();")
                found = self.module()
                self.assertEqual([f.path for f in found], ["src/settings.rs"])
                self.assertIn('declares a settings group with the id "appfair"', found[0].message)
                self.assertIn("settings/appfair route belong to the App Fair group", found[0].message)
                self.assertIn("give that group another id", found[0].fix)
        self.settings(f'{provider}        .group(Group::new("appfair", title()))\n        .install();')
        self.assertEqual(self.module()[0].line, 5)
        # Other ids are the app's, a comment declares nothing, and a constant of the app's own
        # under that name is not the crate's.
        kept = (
            '        .group(Group::new("general", "General"))',
            '        .group(Group::new("appfair-extras", "Extras"))',
            '        // .group(Group::new("appfair", "About the Fair"))',
            '        /* .group(Group::new("appfair", title())) */',
            "        .group(Group::new(GROUP_ID, title()))",
            "        .group(Group::new(crate::ids::GROUP_ID, title()))",
            '        .group(Group::new("general", "General").row(rows::note("appfair")))',
        )
        for call in kept:
            with self.subTest(call=call):
                self.settings(f"{provider}{call}\n        .install();")
                self.assertEqual(self.module(), [])
        # `GROUP_ID` alone is the crate's in a file that imports it from the crate.
        body = (
            "pub fn install() {\n    Preferences::new()\n"
            f"{provider}        .group(Group::new(GROUP_ID, title()))\n        .install();\n}}\n"
        )
        for line in ("use day_piece_appfair::GROUP_ID;", "use day_piece_appfair::{APP_FAIR, GROUP_ID};"):
            with self.subTest(use=line):
                (self.root / "src" / "settings.rs").write_text(f"{SPDX}{line}\n\n{body}")
                found = self.module()
                self.assertEqual([(f.path, f.line) for f in found], [("src/settings.rs", 7)])
        (self.root / "src" / "settings.rs").write_text(f"{SPDX}use crate::ids::GROUP_ID;\n\n{body}")
        self.assertEqual(self.module(), [])
        # A renamed dependency is known by the name the app gave it.
        (self.root / "Cargo.toml").write_text(
            '[dependencies]\nfair = { package = "day-piece-appfair", version = "0.*" }\n'
        )
        self.settings("        .provider(&fair::APP_FAIR)\n        .group(Group::new(fair::GROUP_ID, title()))\n        .install();")
        found = self.module()
        self.assertEqual(len(found), 1)
        self.assertIn(".provider(&fair::APP_FAIR)", found[0].fix)
        # Any Rust source can declare a group, so every one is read.
        self.settings("        .provider(&fair::APP_FAIR)\n        .install();")
        self.settings('        .group(Group::new("appfair", title()))\n        .install();', "crates/ui/src/lib.rs")
        self.assertEqual([f.path for f in self.module()], ["crates/ui/src/lib.rs"])

    def test_the_module_rule_runs_alone_or_not_at_all(self):
        self.good()
        run = lambda *argv: self.silently(lint.main, ["--path", str(self.root), *argv])  # noqa: E731
        self.assertEqual(self.report(only=["appfair-module"]).checked, ["appfair-module"])
        self.assertEqual(run("--only", "appfair-module"), 0)
        (self.root / "Cargo.toml").write_text('[dependencies]\nday = "0.*"\n')
        self.assertEqual(run("--only", "appfair-module"), 1)
        self.assertEqual(run("--skip", "appfair-module"), 0)

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

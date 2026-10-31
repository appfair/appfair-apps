#!/usr/bin/env python3
"""The checks an App Fair app's repository has to pass, run by the action beside this file.

A failure names the file, the line, what is wrong and the text that fixes it. Every rule runs,
so one push reports every problem. `--list` prints the rules, `--only` and `--skip` select them,
and `--format json` returns the findings.

Adding a rule is one function:

    @rule("store-listing", "The store listing carries every field the App Store takes")
    def store_listing(project: Project) -> Iterable[Finding]:
        ...yield Finding(...)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# Build output and dependency trees, which no walk descends into. A rule that needs one of these
# asks for it by path.
SKIP_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    "build",
    "DerivedData",
    "node_modules",
    "oh_modules",
    "target",
    "vendor",
}

# What each platform accepts as an app id. Apple takes letters, digits, hyphens and periods;
# Android and HarmonyOS read the id as a Java package name, so every segment starts with a letter
# and carries no hyphen. The Day targets are grouped by which rule they answer to.
JAVA_PLATFORMS = {"android", "harmony", "ohos"}
APPLE_SEGMENT = re.compile(r"^[A-Za-z0-9][\w-]*$")
JAVA_SEGMENT = re.compile(r"^[A-Za-z]\w*$")

# The manifest keys an id can be overridden under, per platform: `[app.android]`, `[app.harmony]`
# (with `[app.ohos]` as its older spelling), and the targets themselves. Day still reads
# `[app.android-mdc]` as the older spelling of `[app.android-material]`.
PLATFORM_KEYS = {
    "android": ["android", "android-material", "android-mdc"],
    "harmony": ["harmony", "ohos", "harmony-arkui"],
    "apple": ["ios", "macos", "ios-uikit", "macos-appkit", "macos-gtk", "macos-qt"],
}

# Source notices name the applicable license, not a catalog-wide license. This is a presence
# check, not an SPDX expression parser or a determination of DFSG compliance.
SPDX_NOTICE = re.compile(r"^\s*//\s*SPDX-License-Identifier:\s*(\S.*?)\s*$")
LICENSE_PATTERNS = ("LICENSE", "LICENSE.*", "LICENSE-*", "COPYING", "COPYING.*", "COPYING-*", "LICENSES/*.txt")
LICENSE_REVIEW_NOTE = (
    "License review required: verify publicly available source under DFSG-compliant licenses "
    "for the app and all direct and transitive dependencies, compatible distribution terms, "
    "and evidence that every binary dependency (including Maven JARs) can be reproduced from "
    "its available source. License files and SPDX notices alone do not establish compliance. "
    "See https://appfair.org/docs/inclusion-criteria/#license-and-dependencies"
)
# How far in the notice may sit, so a shebang or an attribute can precede it.
SPDX_WITHIN = 5


@dataclass(frozen=True)
class Finding:
    """One problem: what is wrong (`message`) and what to do about it (`fix`, literal enough to
    paste). `path` is relative to the project, `line` is 1-based where a rule knows one.
    """

    rule: str
    message: str
    fix: str
    path: str | None = None
    line: int | None = None

    def where(self) -> str:
        if not self.path:
            return ""
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class Project:
    """The app being checked."""

    root: Path
    #: The build flavor whose manifest carries the identity the App Fair publishes under.
    flavor: str = "appfair"
    #: Anything a rule wants said that is not a finding, such as a required manual review.
    notes: list[str] = field(default_factory=list)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def sources(self, suffix: str) -> list[Path]:
        """Every file with `suffix`, in a stable order. A walk rather than `git ls-files`, so a
        tarball or a sparse checkout is checked like a clone.
        """
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if name.endswith(suffix):
                    found.append(Path(dirpath) / name)
        return found

    def read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None


Check = Callable[[Project], Iterable[Finding]]


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str
    check: Check


RULES: dict[str, Rule] = {}


def rule(code: str, summary: str) -> Callable[[Check], Check]:
    """Register a rule under `code`. The summary is one line, shown by `--list`."""

    def register(check: Check) -> Check:
        if code in RULES:
            raise ValueError(f"two rules named {code!r}")
        RULES[code] = Rule(code, summary, check)
        return check

    return register


# --------------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------------


@rule("day-project", "The repository holds a Day project")
def day_project(project: Project) -> Iterable[Finding]:
    """Every other rule reads this manifest."""
    if not project.path("Day.toml").is_file():
        yield Finding(
            "day-project",
            f"{project.root} holds no Day.toml, so there is no Day project here to check",
            "run the lint against the directory holding the app's Day.toml (the action's `path` "
            "input), or scaffold one with `day new app <Name>`",
        )


@rule("flavor-manifest", "The App Fair identity lives in its own flavor manifest")
def flavor_manifest(project: Project) -> Iterable[Finding]:
    """The catalog builds, signs and uploads through this flavor, so the ids it states are the
    ids the stores receive.
    """
    name = f"Day-{project.flavor}.toml"
    if project.path(name).is_file():
        return
    yield Finding(
        "flavor-manifest",
        f"{name} is missing, so this app has no App Fair identity to publish under",
        f"create {name} beside Day.toml with the ids the App Fair publishes this app under, "
        "each inside `org.appfair.app.`:\n"
        '    [app]\n    id = "org.appfair.app.<Your-App>"\n\n'
        "    # Play and HarmonyOS take no hyphen\n"
        '    [app.android]\n    id = "org.appfair.app.<Your_App>"\n\n'
        '    [app.harmony]\n    id = "org.appfair.app.<Your_App>"',
        path=name,
    )


def license_files(project: Project) -> list[Path]:
    return sorted({
        path for pattern in LICENSE_PATTERNS for path in project.root.glob(pattern)
        if path.is_file() and path.name != "LICENSE-EXCEPTIONS.txt"
    })


@rule("license", "The app includes license texts for review under the DFSG policy")
def license_text(project: Project) -> Iterable[Finding]:
    """Check documentation exists; reviewers assess the actual terms and dependency sources."""
    if LICENSE_REVIEW_NOTE not in project.notes:
        project.notes.append(LICENSE_REVIEW_NOTE)
    paths = license_files(project)
    if not paths:
        yield Finding(
            "license",
            "No license text was found for the app",
            "include the applicable DFSG-compliant license text in LICENSE, COPYING, "
            "a LICENSE/COPYING file with an extension or suffix, or LICENSES/*.txt; "
            "retain required notices and provide dependency source and binary rebuild evidence",
        )
    for path in paths:
        text = project.read(path)
        if text is None or not text.strip():
            yield Finding(
                "license",
                f"{project.rel(path)} is empty or not readable as UTF-8 text",
                "include the complete applicable license text for review",
                path=project.rel(path),
            )


def _ids(manifest: dict) -> dict[str, str]:
    """The id each platform family builds under, most specific key winning."""
    app = manifest.get("app", {})
    base = str(app.get("id", "") or "")
    out = {}
    for family, keys in PLATFORM_KEYS.items():
        found = base
        for key in keys:
            override = app.get(key, {})
            if isinstance(override, dict) and override.get("id"):
                found = str(override["id"])
        if found:
            out[family] = found
    return out


@rule("app-ids", "Every platform's app id is one that platform accepts")
def app_ids(project: Project) -> Iterable[Finding]:
    """An id with a hyphen builds on Apple and fails on Android and HarmonyOS.

    AGP refuses it as a namespace ("not a valid Java package name") and HarmonyOS refuses it in
    `bundleName`, both at configuration time, so the catalog's build would stop there.

    A manifest holding `{{placeholders}}` belongs to a project template rather than to an app, and
    is left to the app scaffolded from it.
    """
    for name in ("Day.toml", f"Day-{project.flavor}.toml"):
        path = project.path(name)
        if not path.is_file():
            continue
        text = project.read(path) or ""
        if "{{" in text:
            continue
        try:
            manifest = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            yield Finding("app-ids", f"{name} does not parse: {error}", f"fix the TOML in {name}",
                          path=name)
            continue
        for family, app_id in _ids(manifest).items():
            java = family in ("android", "harmony")
            segments = app_id.split(".")
            pattern = JAVA_SEGMENT if java else APPLE_SEGMENT
            bad = [s for s in segments if not pattern.match(s)]
            if len(segments) < 2:
                yield Finding(
                    "app-ids",
                    f"{name} builds {family} under {app_id!r}, which is one segment",
                    "write it in reverse-DNS form, such as `io.github.<app>`",
                    path=name,
                )
            elif bad and java:
                yield Finding(
                    "app-ids",
                    f"{name} builds {family} under {app_id!r}, and {bad[0]!r} is not a Java "
                    f"package segment: Android and HarmonyOS take no hyphen, and every segment "
                    f"starts with a letter",
                    f"write the id with underscores ({app_id.replace('-', '_')!r}), or override "
                    f"it for that platform:\n"
                    f"    [app.{'android' if family == 'android' else 'harmony'}]\n"
                    f'    id = "{app_id.replace("-", "_")}"',
                    path=name,
                )
            elif bad:
                yield Finding(
                    "app-ids",
                    f"{name} builds {family} under {app_id!r}, and {bad[0]!r} carries characters "
                    f"the App Store does not accept",
                    "use letters, digits, hyphens and periods",
                    path=name,
                )


@rule("spdx-headers", "Every Rust source names the licence it is under")
def spdx_headers(project: Project) -> Iterable[Finding]:
    """A file without the notice leaves its licence to be guessed from the repository."""
    for path in project.sources(".rs"):
        text = project.read(path)
        if text is None:
            continue
        head = text.splitlines()[:SPDX_WITHIN]
        if any(SPDX_NOTICE.fullmatch(line) for line in head):
            continue
        yield Finding(
            "spdx-headers",
            f"{project.rel(path)} carries no licence notice in its first {SPDX_WITHIN} lines",
            "add an SPDX-License-Identifier comment naming the actual license expression "
            "for this file; retain existing third-party notices and use DFSG-compliant terms",
            path=project.rel(path),
            line=1,
        )


# --------------------------------------------------------------------------------------------
# Running them
# --------------------------------------------------------------------------------------------


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    #: What a rule wants said without failing the run, such as a required manual review.
    notes: list[str] = field(default_factory=list)


def run(project: Project, only: list[str] | None = None, skip: list[str] | None = None) -> Report:
    """Every selected rule, in registration order. A rule that raises becomes a finding, so a
    broken rule cannot hide the others."""
    report = Report()
    for code, entry in RULES.items():
        if only and code not in only:
            continue
        if skip and code in skip:
            continue
        report.checked.append(code)
        try:
            report.findings.extend(entry.check(project))
        except Exception as error:  # noqa: BLE001 - a rule's own bug is reported, not raised
            report.findings.append(
                Finding(code, f"the {code} rule failed: {error!r}", "report this to appfair-apps")
            )
    report.notes = list(project.notes)
    return report


def emit(report: Report, annotate: bool) -> None:
    """The findings as text, plus annotations when this runs in Actions."""
    for note in report.notes:
        print(f"note: {note}")
        if annotate:
            print(f"::warning::appfair-lint: {note}")
    for finding in report.findings:
        where = finding.where()
        head = f"{finding.rule}: {finding.message}"
        print(f"\n✗ {head}" + (f"\n  in {where}" if where else ""))
        for index, line in enumerate(finding.fix.splitlines()):
            print(f"  fix: {line}" if index == 0 else f"       {line}")
        if annotate:
            location = ""
            if finding.path:
                location = f" file={finding.path}" + (f",line={finding.line}" if finding.line else "")
            flat = re.sub(r"\s+", " ", f"{head}. Fix: {finding.fix}")
            print(f"::error{location}::{flat}")
    rules = ", ".join(report.checked)
    if report.findings:
        print(f"\n{len(report.findings)} problem(s) from {len(report.checked)} rule(s): {rules}")
    else:
        print(f"appfair-lint: {len(report.checked)} rule(s) passed: {rules}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="appfair-lint",
        description="The checks an App Fair app's repository has to pass.",
    )
    parser.add_argument("--path", default=".", help="the project directory (holding Day.toml)")
    parser.add_argument(
        "--flavor",
        default=os.environ.get("APPFAIR_FLAVOR", "appfair"),
        help="the build flavor carrying the App Fair identity (default: appfair)",
    )
    parser.add_argument("--only", default="", help="run only these rules, comma-separated")
    parser.add_argument("--skip", default="", help="run every rule but these, comma-separated")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--list", action="store_true", help="print the rules and stop")
    args = parser.parse_args(argv)

    if args.list:
        for code, entry in RULES.items():
            print(f"{code:18} {entry.summary}")
        return 0

    split = lambda value: [p.strip() for p in value.replace(",", " ").split() if p.strip()]  # noqa: E731
    only, skip = split(args.only), split(args.skip)
    for code in only + skip:
        if code not in RULES:
            parser.error(f"no rule named {code!r}; `--list` prints them")

    project = Project(
        root=Path(args.path).resolve(),
        flavor=args.flavor,
    )
    report = run(project, only=only, skip=skip)

    if args.format == "json":
        print(json.dumps({"checked": report.checked, "findings": [f.__dict__ for f in report.findings], "notes": report.notes}, indent=2))
    else:
        emit(report, annotate=bool(os.environ.get("GITHUB_ACTIONS")))
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())

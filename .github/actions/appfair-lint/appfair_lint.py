#!/usr/bin/env python3
"""The checks an App Fair app's own repository has to pass, before the catalog builds it.

What this is for: an app is easiest to fix while its maintainer is looking at it, so the same
rules the submission checks apply run in the app's own CI — `.github/actions/appfair-lint`, which
this script is the whole of. A failure names the file, the line, what is wrong with it and the
exact text that fixes it.

Adding a rule is one function:

    @rule("store-listing", "The store listing carries every field the App Store takes")
    def store_listing(project: Project) -> Iterable[Finding]:
        ...yield Finding(...)

Rules see a `Project` (its root, the flavor name, and the reference material shipped beside this
file), return findings, and never stop the run themselves: every rule is asked, so one pull
request shows every problem rather than the first. `--list` prints them, `--only` / `--skip`
select them, and `--format json` is for a caller that wants the findings rather than the text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference"

# Directories a walk never descends into: build output, dependency trees, and the host projects'
# own generated state. A rule that needs one of these asks for it by path.
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

# The licence every app in the catalog ships under, and the line that says so in a source file.
SPDX = "AGPL-3.0-only WITH App-Fair-Distribution-Exception"
SPDX_LINE = f"// SPDX-License-Identifier: {SPDX}"
# How far into a file the notice may sit: a shebang, an attribute, or a blank line may precede it.
SPDX_WITHIN = 5


@dataclass(frozen=True)
class Finding:
    """One problem, in the words the person who has to fix it needs.

    `message` says what is wrong with this file; `fix` says what to do about it, literally enough
    to paste. `path` is relative to the project, and `line` is 1-based where a rule knows one.
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
    reference: Path = REFERENCE

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def sources(self, suffix: str) -> list[Path]:
        """Every file with `suffix` the app owns, in a stable order.

        A walk rather than `git ls-files`, so the rules hold for a source tree that arrived as a
        tarball or a sparse checkout as well as for a clone.
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
    """Everything else reads this manifest, so an absent one is reported first and plainly."""
    if not project.path("Day.toml").is_file():
        yield Finding(
            "day-project",
            f"{project.root} holds no Day.toml, so there is no Day project here to check",
            "run the lint against the directory holding the app's Day.toml (the action's `path` "
            "input), or scaffold one with `day new app <Name>`",
        )


@rule("flavor-manifest", "The App Fair identity lives in its own flavor manifest")
def flavor_manifest(project: Project) -> Iterable[Finding]:
    """`Day-appfair.toml` is how an app keeps its own id separate from the published one.

    The catalog builds, lints, signs and uploads every submission through this flavor, so the ids
    it states are the ids the stores receive.
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


def _compare_to_reference(project: Project, name: str, code: str, what: str) -> Iterable[Finding]:
    """One licence file against the copy this action ships, reported by the first line that differs."""
    reference = project.reference / name
    expected = reference.read_text(encoding="utf-8")
    path = project.path(name)
    if not path.is_file():
        yield Finding(
            code,
            f"{name} is missing, and every app in the catalog ships {what}",
            f"copy it from the App Fair app template: "
            f"https://github.com/appfair/day-appfair/blob/main/{name}",
            path=name,
        )
        return
    actual = project.read(path)
    if actual is None:
        yield Finding(code, f"{name} is not readable as UTF-8 text", f"restore it from {what}", path=name)
        return
    if actual == expected:
        return
    mine, theirs = actual.splitlines(), expected.splitlines()
    for number, (got, want) in enumerate(zip(mine, theirs), start=1):
        if got != want:
            yield Finding(
                code,
                f"{name} differs from {what} at line {number}:\n"
                f"      expected: {want.strip()!r}\n"
                f"      found:    {got.strip()!r}",
                f"restore the line, or replace the file with "
                f"https://github.com/appfair/day-appfair/blob/main/{name}",
                path=name,
                line=number,
            )
            return
    longer, shorter = ("shorter", len(mine)) if len(mine) < len(theirs) else ("longer", len(mine))
    yield Finding(
        code,
        f"{name} is {longer} than {what}: {len(mine)} lines against {len(theirs)}",
        f"replace it with https://github.com/appfair/day-appfair/blob/main/{name}",
        path=name,
        line=min(len(mine), len(theirs)) + 1,
    )


@rule("license", "LICENSE.txt is the GNU AGPL 3.0 text")
def license_text(project: Project) -> Iterable[Finding]:
    """The catalog publishes free software, and the licence is the unmodified AGPL."""
    yield from _compare_to_reference(project, "LICENSE.txt", "license", "the AGPL-3.0 text")


@rule("license-exception", "LICENSE-EXCEPTIONS.txt is the App Fair distribution exception")
def license_exception(project: Project) -> Iterable[Finding]:
    """The additional permission that lets the App Fair sign and submit the app to the stores."""
    yield from _compare_to_reference(
        project,
        "LICENSE-EXCEPTIONS.txt",
        "license-exception",
        "the App Fair Distribution Exception",
    )


@rule("spdx-headers", "Every Rust source names the licence it is under")
def spdx_headers(project: Project) -> Iterable[Finding]:
    """A file without the notice is a file whose licence has to be guessed from the repository."""
    for path in project.sources(".rs"):
        text = project.read(path)
        if text is None:
            continue
        head = text.splitlines()[:SPDX_WITHIN]
        if any(SPDX in line for line in head):
            continue
        yield Finding(
            "spdx-headers",
            f"{project.rel(path)} carries no licence notice in its first {SPDX_WITHIN} lines",
            f"add this line at the top of the file:\n    {SPDX_LINE}",
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


def run(project: Project, only: list[str] | None = None, skip: list[str] | None = None) -> Report:
    """Every selected rule, in registration order. A rule that raises is a finding of its own,
    so one broken rule cannot hide the others."""
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
    return report


def emit(report: Report, annotate: bool) -> None:
    """The findings, for a person and (in Actions) for the file view."""
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
            flat = re.sub(r"\s+", " ", f"{head} — fix: {finding.fix}")
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

    project = Project(root=Path(args.path).resolve(), flavor=args.flavor)
    report = run(project, only=only, skip=skip)

    if args.format == "json":
        print(json.dumps({"checked": report.checked, "findings": [f.__dict__ for f in report.findings]}, indent=2))
    else:
        emit(report, annotate=bool(os.environ.get("GITHUB_ACTIONS")))
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())

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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference"

# Where the licence texts come from: the App Fair app template, which is what an app is
# scaffolded from and therefore the text an app is measured against. `reference/` holds a copy
# for a run that cannot reach it; `--check-template` fails this repository's own checks when the
# two drift apart.
DEFAULT_TEMPLATE = "appfair/day-appfair@main"
TEMPLATE = os.environ.get("APPFAIR_TEMPLATE", DEFAULT_TEMPLATE)
TEMPLATE_TIMEOUT = 20

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
# (with `[app.ohos]` as its older spelling), and the targets themselves.
PLATFORM_KEYS = {
    "android": ["android", "android-mdc"],
    "harmony": ["harmony", "ohos", "harmony-arkui"],
    "apple": ["ios", "macos", "ios-uikit", "macos-appkit", "macos-gtk", "macos-qt"],
}

# The licence every app here ships under, and the line that says so in a source file.
SPDX = "AGPL-3.0-only WITH App-Fair-Distribution-Exception"
SPDX_LINE = f"// SPDX-License-Identifier: {SPDX}"
# How far in the notice may sit, so a shebang or an attribute can precede it.
SPDX_WITHIN = 5


class TemplateUnavailable(Exception):
    """The template answered 4xx: its repository, ref or file name is wrong."""


def http_get(url: str) -> str:
    """One GET, as text. Raises on anything but a 200."""
    request = urllib.request.Request(url, headers={"User-Agent": "appfair-lint"})
    with urllib.request.urlopen(request, timeout=TEMPLATE_TIMEOUT) as response:  # noqa: S310
        return response.read().decode("utf-8")


@dataclass(frozen=True)
class Source:
    """One canonical text and where it was read from."""

    text: str
    origin: str
    #: Why the template was not used, when it could not be reached.
    fell_back: str | None = None


@dataclass
class Template:
    """The App Fair app template, as `owner/repo@ref`.

    A file is read from it over HTTPS; the copy under `reference/` answers when it cannot be
    reached, so a network failure does not turn into an app's lint failure.
    """

    spec: str = TEMPLATE
    reference: Path = REFERENCE
    #: How a URL is read; the tests pass one that serves from memory.
    get: Callable[[str], str] | None = None
    _cache: dict[str, Source] = field(default_factory=dict, repr=False)

    def read(self, url: str) -> str:
        return (self.get or http_get)(url)

    @property
    def repo(self) -> str:
        return self.spec.split("@", 1)[0]

    @property
    def ref(self) -> str:
        return self.spec.split("@", 1)[1] if "@" in self.spec else "main"

    def url(self, name: str) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.ref}/{name}"

    def blob(self, name: str) -> str:
        """The file's page on GitHub, for a message someone reads."""
        return f"https://github.com/{self.repo}/blob/{self.ref}/{name}"

    def source(self, name: str) -> Source:
        """`name` from the template.

        A connection that fails falls back to `reference/`, so a network failure does not fail an
        app's lint. A 4xx answer raises instead, since it means the spec is wrong.
        """
        if name not in self._cache:
            url = self.url(name)
            try:
                self._cache[name] = Source(self.read(url), url)
            except urllib.error.HTTPError as error:
                if 400 <= error.code < 500:
                    raise TemplateUnavailable(
                        f"{url} answered {error.code} {error.reason}"
                    ) from error
                self._cache[name] = self._fallback(name, url, error)
            except (urllib.error.URLError, OSError, ValueError) as error:
                self._cache[name] = self._fallback(name, url, error)
        return self._cache[name]

    def _fallback(self, name: str, url: str, error: Exception) -> Source:
        local = self.reference / name
        return Source(
            local.read_text(encoding="utf-8"),
            f"{local.name} (this action's copy)",
            f"{url} could not be read ({error}), so the comparison used this action's copy",
        )


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
    #: Where the licence texts are read from.
    template: Template = field(default_factory=Template)
    #: Anything a rule wants said that is not a finding, such as a template it could not reach.
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


def _compare_to_template(project: Project, name: str, code: str, what: str) -> Iterable[Finding]:
    """One licence file against the template's, character for character, reported at the first
    line that differs."""
    try:
        source = project.template.source(name)
    except TemplateUnavailable as error:
        yield Finding(
            code,
            f"{name} could not be read from the App Fair app template: {error}",
            "check the action's `template` input (owner/repo@ref); it names where the licence "
            f"texts come from, and the default is {DEFAULT_TEMPLATE}",
        )
        return
    if source.fell_back and source.fell_back not in project.notes:
        project.notes.append(source.fell_back)
    expected = source.text
    blob = project.template.blob(name)
    path = project.path(name)
    if not path.is_file():
        yield Finding(
            code,
            f"{name} is missing, and every app in the catalog ships {what}",
            f"copy it from the App Fair app template: {blob}",
            path=name,
        )
        return
    actual = project.read(path)
    if actual is None:
        yield Finding(code, f"{name} is not readable as UTF-8 text", f"restore it from {blob}", path=name)
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
                f"      found:    {got.strip()!r}\n"
                f"      compared against: {source.origin}",
                f"restore the line, or replace the file with {blob}",
                path=name,
                line=number,
            )
            return
    longer = "shorter" if len(mine) < len(theirs) else "longer"
    yield Finding(
        code,
        f"{name} is {longer} than {what}: {len(mine)} lines against {len(theirs)}\n"
        f"      compared against: {source.origin}",
        f"replace it with {blob}",
        path=name,
        line=min(len(mine), len(theirs)) + 1,
    )


@rule("license", "LICENSE.txt is the GNU AGPL 3.0 text the app template carries")
def license_text(project: Project) -> Iterable[Finding]:
    """The catalog publishes free software under the unmodified AGPL."""
    yield from _compare_to_template(project, "LICENSE.txt", "license", "the AGPL-3.0 text")


@rule("license-exception", "LICENSE-EXCEPTIONS.txt is the App Fair distribution exception")
def license_exception(project: Project) -> Iterable[Finding]:
    """The additional permission that lets the App Fair sign and submit the app to the stores."""
    yield from _compare_to_template(
        project,
        "LICENSE-EXCEPTIONS.txt",
        "license-exception",
        "the App Fair Distribution Exception",
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
    #: What a rule wants said without failing the run, such as a template it could not reach.
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


def check_template(template: Template) -> int:
    """This action's `reference/` copies against the template, for this repository's own checks.

    The copies are the fallback for a run that cannot reach the template, so drift here means a
    lint without network would measure an app against the wrong text.
    """
    drifted = 0
    for path in sorted(template.reference.iterdir()):
        try:
            canonical = template.read(template.url(path.name))
        except (urllib.error.URLError, OSError, ValueError) as error:
            print(f"::error::{template.url(path.name)} could not be read ({error})")
            return 1
        mine = path.read_text(encoding="utf-8")
        if mine == canonical:
            print(f"ok   reference/{path.name} matches {template.blob(path.name)}")
            continue
        drifted += 1
        theirs, ours = canonical.splitlines(), mine.splitlines()
        line = next(
            (n for n, (a, b) in enumerate(zip(ours, theirs), start=1) if a != b),
            min(len(ours), len(theirs)) + 1,
        )
        print(
            f"::error file=.github/actions/appfair-lint/reference/{path.name},line={line}::"
            f"reference/{path.name} differs from {template.blob(path.name)} at line {line}. "
            f"Fix: copy the template's file over it"
        )
    return 1 if drifted else 0


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
    parser.add_argument(
        "--template",
        default=TEMPLATE,
        help=f"the app template the licence texts come from, owner/repo@ref (default: {TEMPLATE})",
    )
    parser.add_argument(
        "--check-template",
        action="store_true",
        help="compare this action's reference/ copies with the template and stop",
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

    if args.check_template:
        return check_template(Template(spec=args.template))

    split = lambda value: [p.strip() for p in value.replace(",", " ").split() if p.strip()]  # noqa: E731
    only, skip = split(args.only), split(args.skip)
    for code in only + skip:
        if code not in RULES:
            parser.error(f"no rule named {code!r}; `--list` prints them")

    project = Project(
        root=Path(args.path).resolve(),
        flavor=args.flavor,
        template=Template(spec=args.template),
    )
    report = run(project, only=only, skip=skip)

    if args.format == "json":
        print(json.dumps({"checked": report.checked, "findings": [f.__dict__ for f in report.findings]}, indent=2))
    else:
        emit(report, annotate=bool(os.environ.get("GITHUB_ACTIONS")))
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())

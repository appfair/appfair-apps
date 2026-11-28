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

# Third-party files keep their own SPDX expressions.
SPDX_NOTICE = re.compile(r"^\s*//\s*SPDX-License-Identifier:\s*(\S.*?)\s*$")
APP_LICENSE = "GPL-2.0-or-later"
REFERENCE = Path(__file__).resolve().parent / "reference" / "LICENSE.txt"
LICENSE_PATTERNS = ("LICENSE", "LICENSE.*", "LICENSE-*", "COPYING", "COPYING.*", "COPYING-*", "LICENSES/*.txt")
LICENSE_REVIEW_NOTE = (
    "Verify the rights holders grant the app license. Check public "
    "source, DFSG compliance and compatible terms for all direct and transitive dependencies. "
    "Verify that each binary dependency can be reproduced from its corresponding source. "
    "See https://appfair.org/docs/inclusion-criteria/#license-and-dependencies"
)
# How far in the notice may sit, so a shebang or an attribute can precede it.
SPDX_WITHIN = 5

# The crate every App Fair app builds in. It adds the App Fair group to the app's settings and
# leaves the bytes `day-piece-appfair/<version>` and a NUL in the binary. APPFAIR_MODULE_MINIMUM
# is the oldest release the catalog accepts, and the one place that states it: the
# `appfair-module` rule holds an app's Cargo.lock to it, and scripts/queue.py holds the marker in
# a built package to it.
APPFAIR_MODULE = "day-piece-appfair"
APPFAIR_MODULE_MINIMUM = "0.1.0"
# Where a Cargo.lock entry comes from when crates.io published it, whichever protocol fetched it.
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"

SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")

# Rust's comments, and the literals a comment marker can sit inside, so a commented-out call is
# not read as a call and the `//` of a quoted URL does not start a comment.
RUST_COMMENT_OR_LITERAL = re.compile(
    r'r(#*)"[\s\S]*?"\1'
    r'|"(?:\\[\s\S]|[^"\\])*"'
    r"|'(?:\\(?:x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f_]+\}|[^\n])|[^'\\\n])'"
    r"|//[^\n]*"
    r"|/\*[\s\S]*?\*/"
)
# The call that hands the group to the `Preferences` builder of day-piece-settings, however
# rustfmt breaks it: with the constant's path spelled out, or bare beside a `use` of it. `{crate}`
# is the name the app's code knows the crate by.
PROVIDER_CALL = r"\.\s*provider\s*\(\s*&\s*(?:::\s*)?{crate}\s*::\s*APP_FAIR\s*,?\s*\)"
PROVIDER_CALL_IMPORTED = re.compile(r"\.\s*provider\s*\(\s*&\s*APP_FAIR\s*,?\s*\)")
PROVIDER_IMPORT = (
    r"\buse\s+(?:::\s*)?{crate}\s*::\s*(?:APP_FAIR\b|\{{[^;]*?\bAPP_FAIR\b)(?!\s+as\b)"
)
# The builder call that takes the group out again, by the id the crate gives it: the literal, or
# the crate's `GROUP_ID` constant under any path. A builder set to `Providers::None` still
# collects the providers an app names, so that setting passes.
WITHOUT_PROVIDER = re.compile(
    r"\.\s*without_provider\s*\(\s*(?:\"appfair\"|(?:[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*GROUP_ID)\s*,?\s*\)"
)
PREFERENCES_BUILDER = re.compile(r"\bPreferences\s*::\s*new\s*\(")
# A settings group the app declares under the id the module's group has. The id and its
# `settings/appfair` route are the module's: day-piece-settings keeps the provider's group when two
# share an id, so the app's group would not show. `Group` may carry its path. The id is the
# literal, the crate's `GROUP_ID` under its path, or `GROUP_ID` alone in a file that imports it
# from the crate (an app is free to name a constant of its own `GROUP_ID`).
GROUP_NEW = r"\bGroup\s*::\s*new\s*\(\s*"
RESERVED_GROUP_LITERAL = re.compile(GROUP_NEW + r'"appfair"')
RESERVED_GROUP_PATH = GROUP_NEW + r"(?:::\s*)?{crate}\s*::\s*GROUP_ID\b"
RESERVED_GROUP_IMPORTED = re.compile(GROUP_NEW + r"GROUP_ID\b")
GROUP_ID_IMPORT = (
    r"\buse\s+(?:::\s*)?{crate}\s*::\s*(?:GROUP_ID\b|\{{[^;]*?\bGROUP_ID\b)(?!\s+as\b)"
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


def additional_license_files(project: Project) -> list[Path]:
    return sorted({
        path for pattern in LICENSE_PATTERNS for path in project.root.glob(pattern)
        if path.is_file() and path != project.path("LICENSE.txt")
    })


@rule("license", "The app declares GPL version 2 or later")
def license_text(project: Project) -> Iterable[Finding]:
    if LICENSE_REVIEW_NOTE not in project.notes:
        project.notes.append(LICENSE_REVIEW_NOTE)
    expected = REFERENCE.read_text(encoding="utf-8")
    actual = project.read(project.path("LICENSE.txt"))
    if actual is None or actual.split() != expected.split():
        yield Finding(
            "license", "LICENSE.txt must contain the complete GPL version 2 text",
            "copy LICENSE.txt from the appfair-lint action’s reference directory",
            path="LICENSE.txt",
        )
    for path in additional_license_files(project):
        text = project.read(path)
        if text is None or not text.strip():
            yield Finding(
                "license",
                f"{project.rel(path)} is empty or not readable as UTF-8 text",
                "include the complete applicable license text for review",
                path=project.rel(path),
            )

    # The GPL text alone cannot distinguish version 2 only from version 2 or later.
    manifest = project.path("Cargo.toml")
    if manifest.exists():
        try:
            with manifest.open("rb") as file:
                cargo = tomllib.load(file)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            yield Finding("license", f"Cannot read Cargo.toml: {error}", "provide valid UTF-8 TOML", path="Cargo.toml")
            return
        declared = cargo.get("package", {}).get("license")
        if declared == {"workspace": True}:
            declared = cargo.get("workspace", {}).get("package", {}).get("license", "")
        if declared is not None:
            if declared != APP_LICENSE:
                yield Finding(
                    "license", f"The app package declares {declared!r}",
                    f'declare license = "{APP_LICENSE}" for the app package',
                    path="Cargo.toml",
                )
            return

    # Existing Day apps can declare the app's terms in a crate root instead of the manifest.
    for name in ("src/lib.rs", "src/main.rs"):
        source = project.read(project.path(name)) or ""
        for line in source.splitlines()[:SPDX_WITHIN]:
            notice = SPDX_NOTICE.fullmatch(line)
            if notice and notice.group(1) == APP_LICENSE:
                return
    yield Finding(
        "license", "The app has no declaration of the required license",
        f'declare license = "{APP_LICENSE}" in the app’s [package] section or use that '
        "identifier in the SPDX notice in src/lib.rs or src/main.rs",
        path="Cargo.toml",
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


def version_key(version: str) -> tuple | None:
    """A sortable form of a semver version, or None for text that is not one. A pre-release sorts
    before its release, and its identifiers compare as semver orders them.
    """
    match = SEMVER.fullmatch(version.strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")
    ) if pre else ()
    return (int(major), int(minor), int(patch), 0 if pre else 1, identifiers)


def version_at_least(version: str, minimum: str) -> bool:
    """Whether `version` is `minimum` or newer. Text that is not a version is older than any."""
    key, floor = version_key(version), version_key(minimum)
    if floor is None:
        raise ValueError(f"{minimum!r} is not a semver version")
    return key is not None and key >= floor


def rust_code(text: str) -> str:
    """Rust source with its comments blanked. Every other character stays where it was, so a match
    in the result is on the line it has in the file.
    """

    def blank(match: re.Match) -> str:
        found = match.group(0)
        return re.sub(r"[^\n]", " ", found) if found.startswith(("//", "/*")) else found

    return RUST_COMMENT_OR_LITERAL.sub(blank, text)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _module_entry(cargo: dict, table: object) -> tuple[str, object] | None:
    """The entry of a dependency table that names the module, with the key it sits under. A
    dependency renamed with `package = "day-piece-appfair"` counts, and an entry that says
    `workspace = true` is read from `[workspace.dependencies]`, which states its version.
    """
    if not isinstance(table, dict):
        return None
    inherited = cargo.get("workspace", {}).get("dependencies", {})
    for key, entry in table.items():
        if isinstance(entry, dict) and entry.get("workspace") is True:
            entry = inherited.get(key, entry)
        package = entry.get("package", key) if isinstance(entry, dict) else key
        if package == APPFAIR_MODULE:
            return key, entry
    return None


def _module_manifest(project: Project) -> tuple[list[Finding], str]:
    """Cargo.toml asks crates.io for the module, under `[dependencies]`. Returns the findings and
    the name the app's code knows the crate by, which a renamed dependency changes.
    """
    crate = APPFAIR_MODULE.replace("-", "_")
    manifest = project.path("Cargo.toml")
    text = project.read(manifest)
    try:
        if text is None and manifest.exists():
            raise ValueError("it is not UTF-8 text")
        text = text or ""
        cargo = tomllib.loads(text)
    except ValueError as error:  # a TOMLDecodeError is one
        return [Finding(
            "appfair-module", f"Cannot read Cargo.toml: {error}", "provide valid UTF-8 TOML",
            path="Cargo.toml",
        )], crate

    def line(pattern: str) -> int | None:
        found = re.search(pattern, text, re.MULTILINE)
        return _line(text, found.start()) if found else None

    module = re.escape(APPFAIR_MODULE)
    requirement = f'{APPFAIR_MODULE} = "0.*"'
    declared = _module_entry(cargo, cargo.get("dependencies"))
    if declared is None:
        # The tables a build can leave out: one for some targets, for tests or for build.rs.
        others = {
            f"target.'{cfg}'.dependencies": target.get("dependencies")
            for cfg, target in cargo.get("target", {}).items() if isinstance(target, dict)
        }
        others |= {name: cargo.get(name) for name in ("dev-dependencies", "build-dependencies")}
        elsewhere = next((n for n, table in others.items() if _module_entry(cargo, table)), None)
        if elsewhere:
            return [Finding(
                "appfair-module",
                f"Cargo.toml lists {APPFAIR_MODULE} under [{elsewhere}] and not under "
                f"[dependencies], so a build can leave the App Fair module out",
                f"move it under [dependencies], where every target builds it:\n    {requirement}",
                path="Cargo.toml",
                line=line(rf'^[ \t]*(?:{module}(?![\w-])|.*\bpackage\s*=\s*"{module}")'),
            )], crate
        return [Finding(
            "appfair-module",
            f"Cargo.toml has no {APPFAIR_MODULE} under [dependencies], and every App Fair app "
            f"builds that crate in for the App Fair group of its settings",
            f"add this line under [dependencies] in Cargo.toml:\n    {requirement}",
            path="Cargo.toml",
            line=line(r"^[ \t]*\[dependencies\]"),
        )], crate

    key, entry = declared
    crate = key.replace("-", "_")
    if isinstance(entry, str):
        return [], crate
    if key != APPFAIR_MODULE:
        requirement = f'{key} = {{ package = "{APPFAIR_MODULE}", version = "0.*" }}'
    where = line(rf"^[ \t]*(?:\[dependencies\.)?{re.escape(key)}(?![\w-])")
    fields = entry if isinstance(entry, dict) else {}
    source = next((name for name in ("path", "git") if name in fields), None)
    if source:
        origin = "a local path" if source == "path" else "a git repository"
        return [Finding(
            "appfair-module",
            f"Cargo.toml takes {APPFAIR_MODULE} from {origin}, and a tagged release has to build "
            f"it from crates.io",
            f"replace the entry with a crates.io version requirement:\n    {requirement}",
            path="Cargo.toml",
            line=where,
        )], crate
    if not isinstance(fields.get("version"), str):
        return [Finding(
            "appfair-module",
            f"Cargo.toml gives {APPFAIR_MODULE} no version, so the entry names no crates.io "
            f"release",
            f"state a crates.io version requirement:\n    {requirement}",
            path="Cargo.toml",
            line=where,
        )], crate
    return [], crate


def _lock_line(text: str, version: str, key: str) -> int | None:
    """The 1-based line of `key` in the module's lock entry for `version`, or the entry's `name`
    line when the entry has no such key."""
    entry = re.search(
        rf'^name\s*=\s*"{re.escape(APPFAIR_MODULE)}"\s*\nversion\s*=\s*"{re.escape(version)}"[^\n]*'
        rf"(?:\n(?!\[)[^\n]+)*",
        text, re.MULTILINE,
    )
    if not entry:
        return None
    found = re.search(rf"^{re.escape(key)}\s*=", entry.group(0), re.MULTILINE)
    return _line(text, entry.start() + (found.start() if found else 0))


def _module_lock(project: Project) -> Iterable[Finding]:
    """The app commits a Cargo.lock that locks the module as crates.io published it: an entry
    whose `source` is the crates.io registry, with its checksum, at a release the catalog accepts.
    The catalog builds what the lock names. An entry with no `source` is a local checkout, which a
    patch table or a path dependency leaves there, and it is reported like any other source.
    """
    path = project.path("Cargo.lock")
    relock = (
        "write the lock with no local source in effect: move .cargo/config.toml (the patch table "
        "`day patch --local` writes) out of the way, "
    )
    if not path.is_file():
        yield Finding(
            "appfair-module",
            "the app commits no Cargo.lock, and the catalog builds the releases a lock names",
            f"{relock}run `cargo generate-lockfile`, check the result with "
            "`cargo metadata --locked`, then commit Cargo.lock",
            path="Cargo.lock",
        )
        return
    text = project.read(path) or ""
    try:
        packages = tomllib.loads(text).get("package", [])
    except tomllib.TOMLDecodeError as error:
        yield Finding(
            "appfair-module", f"Cannot read Cargo.lock: {error}",
            "restore the Cargo.lock that cargo wrote", path="Cargo.lock",
        )
        return
    locked = [p for p in packages if isinstance(p, dict) and p.get("name") == APPFAIR_MODULE]
    if not locked:
        yield Finding(
            "appfair-module",
            f"Cargo.lock locks no {APPFAIR_MODULE}, and the catalog builds what the lock names",
            f"with the dependency in Cargo.toml, run `cargo update --workspace`, which locks "
            f"{APPFAIR_MODULE} and leaves every other package where it is, then commit Cargo.lock",
            path="Cargo.lock",
        )
    # `cargo update -p` cannot name an entry that has no source once the patch table is gone,
    # and `--workspace` locks it again from crates.io while every other package stays put.
    update = (
        "run `cargo update --workspace`, check the result with `cargo metadata --locked`, then "
        "commit Cargo.lock"
    )
    for package in locked:
        version = str(package.get("version", ""))
        source = package.get("source")
        if source != CRATES_IO:
            if source is None:
                origin = "from a local checkout (the entry has no `source`)"
            elif str(source).startswith("git+"):
                origin = f"from a git repository ({source})"
            else:
                origin = f"from another registry ({source})"
            yield Finding(
                "appfair-module",
                f"Cargo.lock locks {APPFAIR_MODULE} {version} {origin}, and the catalog builds "
                f"the release crates.io published",
                f"{relock}{update}",
                path="Cargo.lock",
                line=_lock_line(text, version, "source"),
            )
        elif not package.get("checksum"):
            yield Finding(
                "appfair-module",
                f"Cargo.lock locks {APPFAIR_MODULE} {version} from crates.io with no checksum, "
                f"and the catalog takes a registry entry only with the checksum cargo wrote",
                update,
                path="Cargo.lock",
                line=_lock_line(text, version, "checksum"),
            )
        if not version_at_least(version, APPFAIR_MODULE_MINIMUM):
            yield Finding(
                "appfair-module",
                f"Cargo.lock locks {APPFAIR_MODULE} {version}, and the App Fair takes "
                f"{APPFAIR_MODULE_MINIMUM} or newer",
                f"run `cargo update -p {APPFAIR_MODULE}` and commit Cargo.lock",
                path="Cargo.lock",
                line=_lock_line(text, version, "version"),
            )


def _module_sources(project: Project, crate: str) -> Iterable[Finding]:
    """The settings builder is handed the group, nothing takes it out again, and no group of the
    app's is declared under its id."""
    call = re.compile(PROVIDER_CALL.format(crate=re.escape(crate)))
    imported = re.compile(PROVIDER_IMPORT.format(crate=re.escape(crate)))
    reserved_path = re.compile(RESERVED_GROUP_PATH.format(crate=re.escape(crate)))
    group_id_imported = re.compile(GROUP_ID_IMPORT.format(crate=re.escape(crate)))
    src = project.path("src")
    passed = False
    builder: tuple[str, int] | None = None
    for path in project.sources(".rs"):
        text = project.read(path)
        if text is None:
            continue
        code = rust_code(text)
        name = project.rel(path)
        if path.is_relative_to(src):
            if call.search(code) or (PROVIDER_CALL_IMPORTED.search(code) and imported.search(code)):
                passed = True
            found = PREFERENCES_BUILDER.search(code)
            if found and builder is None:
                builder = (name, _line(code, found.start()))
        for found in WITHOUT_PROVIDER.finditer(code):
            yield Finding(
                "appfair-module",
                f'{name} calls .without_provider("appfair"), which takes the App Fair group out '
                f"of the app's settings",
                'delete the .without_provider("appfair") call',
                path=name,
                line=_line(code, found.start()),
            )
        reserved = [*RESERVED_GROUP_LITERAL.finditer(code), *reserved_path.finditer(code)]
        if group_id_imported.search(code):
            reserved += RESERVED_GROUP_IMPORTED.finditer(code)
        for found in sorted(reserved, key=lambda match: match.start()):
            yield Finding(
                "appfair-module",
                f'{name} declares a settings group with the id "appfair", and that id and its '
                f"settings/appfair route belong to the App Fair group",
                "give that group another id. The App Fair group comes from "
                f".provider(&{crate}::APP_FAIR)",
                path=name,
                line=_line(code, found.start()),
            )
    if not passed:
        yield Finding(
            "appfair-module",
            "no source under src/ passes the App Fair provider to the settings builder, so the "
            "app's settings have no App Fair group",
            "add this call where the app declares its settings, on the `Preferences` builder of "
            f"day-piece-settings and before `.install()`:\n    .provider(&{crate}::APP_FAIR)",
            path=builder[0] if builder else None,
            line=builder[1] if builder else None,
        )


@rule("appfair-module", "The app builds in day-piece-appfair and shows its settings group")
def appfair_module(project: Project) -> Iterable[Finding]:
    """Every App Fair app carries the catalog's group in its settings.

    The group comes from one crate, which the app takes from crates.io, locks in a committed
    Cargo.lock as crates.io published it, at a release the catalog accepts, and hands to its
    settings builder. The validation stage looks for the marker that crate leaves in the built
    package, so a submission that fails here fails there too.
    """
    findings, crate = _module_manifest(project)
    yield from findings
    yield from _module_lock(project)
    yield from _module_sources(project, crate)


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

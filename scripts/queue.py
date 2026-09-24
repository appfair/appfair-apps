#!/usr/bin/env python3
"""The catalog's command-line tool: write and validate submissions, plan builds, check what was
built, and record what was published.

The workflows call subcommands here, which keeps the rules in one file that can be read, run
locally and tested:

    scripts/queue.py validate apps/Faire-Games.yaml   # or --all
    scripts/queue.py plan --changed-from origin/main  # the build matrix for a pull request
    scripts/queue.py verify --app Faire-Games --metadata day-metadata.json
    scripts/queue.py authorize Faire-Games --actor someone --base origin/main
    scripts/queue.py record --app Faire-Games --stores apple,play --run-url https://…
    scripts/queue.py selftest                         # the cases below, offline

PyYAML is the one dependency, and GitHub runners have it. Errors print as GitHub annotations
under GITHUB_ACTIONS and as plain lines otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("scripts/queue.py needs PyYAML: pip install pyyaml (GitHub runners have it already)")

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"
STATE = ROOT / "state" / "published.json"

# The keys a submission may carry; anything else is rejected as a typo. A channel's settings live
# under its name, and policy.yaml lists the channels and the settings each one takes.
TOP_LEVEL = {"token", "title", "tag", "commit", "distribution", "summary", "id", "android-id", "screenshots"}

# What each store accepts as an id. Apple takes letters, digits, hyphens and periods; Play takes a
# Java package name, which rules the hyphen out and wants every segment to start with a letter.
BUNDLE_ID = re.compile(r"^[A-Za-z][\w-]*(\.[\w-]+)+$")
PACKAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def load_yaml(path: Path) -> dict:
    """One YAML document as a mapping. Anything else raises, and the caller reports it."""
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("expected a mapping of keys at the top level")
    return data


# --------------------------------------------------------------------------------------------
# Policy and submissions
# --------------------------------------------------------------------------------------------


def default_flavor() -> str:
    """The Day build flavor every submission is built with, taken from this repository's name.

    `appfair-apps` builds each app's `appfair` flavor; a fork called `gamesfair-apps` builds
    `gamesfair`. Submissions do not choose it, since the flavor carries the identity the catalog
    publishes under.
    """
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    name = repository.split("/")[-1] if repository else ROOT.name
    return name.removesuffix("-apps") or name


@dataclass
class Channel:
    """One channel an app can be published to, as declared in policy.yaml."""

    name: str
    target: str
    lane: str
    hold_lane: str
    options: set[str]
    status: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass
class Policy:
    """policy.yaml, read once and passed to the checks."""

    id_namespace: str
    token_pattern: str
    tag_pattern: str
    commit_pattern: str
    channels: dict[str, Channel]
    flavor: str
    day_version: str
    review_highlights: list[str]
    review_max: int
    runners: dict[str, str]

    @staticmethod
    def load(path: Path | None = None) -> "Policy":
        raw = load_yaml(path or ROOT / "policy.yaml")
        channels = {
            name: Channel(
                name=name,
                target=str(spec["target"]),
                lane=str(spec.get("lane", "")),
                hold_lane=str(spec.get("hold-lane", spec.get("lane", ""))),
                options=set(spec.get("options", [])),
                status=str(spec.get("status", "planned")),
            )
            for name, spec in (raw.get("channels") or {}).items()
        }
        return Policy(
            id_namespace=raw["id-namespace"],
            token_pattern=raw["token-pattern"],
            tag_pattern=raw["tag-pattern"],
            commit_pattern=raw["commit-pattern"],
            channels=channels,
            flavor=str(raw.get("flavor") or default_flavor()),
            day_version=str(raw.get("day-version", "main")),
            review_highlights=[
                str(pattern) for pattern in (raw.get("review") or {}).get("highlight-paths", [])
            ],
            review_max=int((raw.get("review") or {}).get("max-highlights", 20)),
            runners={str(k): str(v) for k, v in (raw.get("runners") or {}).items()},
        )

    @property
    def ready_channels(self) -> dict[str, Channel]:
        return {name: c for name, c in self.channels.items() if c.ready}

    @property
    def buildable_targets(self) -> list[str]:
        """The targets at least one ready channel takes a package from."""
        return sorted({c.target for c in self.ready_channels.values()})


@dataclass
class Problem:
    """One problem, in the form an annotation takes.

    `file` is a path when the problem is in one, and a label such as `plan` or `record` when it
    is not. Only a path becomes `file=`: GitHub renders that field as a location, so a label
    there hangs the annotation off a file nobody can open.
    """

    file: str
    message: str

    def annotation(self) -> str:
        where = self.file
        if "/" in where or Path(where).suffix:
            return f"::error file={where}::{self.message}"
        return f"::error::{where}: {self.message}"

    def emit(self) -> None:
        if os.environ.get("GITHUB_ACTIONS"):
            print(self.annotation())
        else:
            print(f"{self.file}: {self.message}", file=sys.stderr)


@dataclass
class App:
    """One parsed `apps/<token>.yaml`. [`validate_app`] decides whether it is usable."""

    path: Path
    data: dict
    problems: list[Problem] = field(default_factory=list)

    @property
    def token(self) -> str:
        return str(self.data.get("token", self.path.stem))

    @property
    def owner_repo(self) -> str:
        """`owner/name`, the form GitHub actions take.

        App Fair apps live at `<token>/<token>`
        (https://appfair.org/docs/inclusion-criteria/#naming), so the token gives both halves.
        """
        return f"{self.token}/{self.token}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner_repo}"


def github_api(url: str, timeout: int = 20) -> tuple[int, object]:
    """Ask GitHub a read-only question.

    Returns the status and the decoded body, or `(0, None)` when the question could not be asked
    at all. The token is whatever the job holds; every endpoint used here is readable on a public
    repository without one.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            try:
                return response.status, json.loads(body)
            except ValueError:
                return response.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 0, None


def relative(path: Path) -> str:
    """A path for an annotation: relative to the repository, or unchanged for a path outside it,
    such as a selftest fixture under /tmp."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_app(path: Path) -> App:
    try:
        return App(path=path, data=load_yaml(path))
    except (OSError, ValueError, yaml.YAMLError) as e:
        app = App(path=path, data={})
        app.problems.append(Problem(relative(path), f"cannot be read: {e}"))
        return app


def catalog() -> list[App]:
    """Every submission in the repository."""
    paths = sorted(list(APPS.glob("*.yaml")) + list(APPS.glob("*.yml")))
    return [load_app(p) for p in paths]


# --------------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------------


def validate_app(app: App, policy: Policy, others: list[App] | None = None) -> list[Problem]:
    """Everything checkable without leaving this repository.

    Whether the commit builds the app the file claims is `verify`'s job, which reads the app's
    manifest after checking it out.
    """
    rel = relative(app.path)
    problems = list(app.problems)
    data = app.data
    if not data:
        return problems

    def bad(message: str) -> None:
        problems.append(Problem(rel, message))

    known_channels = policy.channels
    unknown = set(data) - TOP_LEVEL - set(known_channels)
    if unknown:
        bad(f"unknown key(s): {', '.join(sorted(unknown))}")

    if "screenshots" in data and not isinstance(data.get("screenshots"), bool):
        bad("screenshots takes true or false: whether the submission replaces the stores' screenshots")

    token = data.get("token")
    if not isinstance(token, str) or not token:
        bad("token is required: the app's GitHub organization and repository name")
        token = ""
    elif not re.match(policy.token_pattern, token):
        bad(f"token {token!r} does not match {policy.token_pattern}")
    if token and app.path.stem != token:
        bad(f"the file is named {app.path.name} while its token is {token!r}; they have to agree")

    # What the app publishes under. A submission states these only when they are not the
    # catalog's default spelling (`<namespace><token>`, and that with hyphens as underscores for
    # Play); the value itself is the app's own, checked for being an id the stores accept rather
    # than for following the token.
    for field, pattern, shape in (
        ("id", BUNDLE_ID, "reverse-DNS, two or more segments of letters, digits, `_` or `-`"),
        ("android-id", PACKAGE_NAME, "a Java package name: two or more segments, each starting "
                                     "with a letter, no hyphen"),
    ):
        declared = data.get(field)
        if declared is None:
            continue
        if not isinstance(declared, str) or not pattern.match(declared):
            bad(f"{field} {declared!r} is not one the stores accept ({shape})")
        elif not declared.startswith(policy.id_namespace):
            bad(
                f"{field} {declared!r} is outside {policy.id_namespace!r}, which is where the "
                f"App Fair publishes"
            )

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        bad("title is required: the name people see on their device and in the store")
    elif len(title) > 30:
        bad(f"title is {len(title)} characters; the App Store takes 30")

    tag = data.get("tag")
    if not isinstance(tag, str) or not tag:
        bad("tag is required: the released tag of the app to build")
    elif not re.match(policy.tag_pattern, tag):
        bad(f"tag {tag!r} does not match {policy.tag_pattern}; submit a released version")

    # The commit that tag points at. Every stage checks this out, so what is reviewed and what is
    # published are one thing even if the tag is moved afterwards.
    commit = data.get("commit")
    if not isinstance(commit, str) or not commit:
        bad(
            "commit is required: the full 40-character commit the tag points at "
            "(`git rev-parse <tag>^{commit}`, or scripts/queue.py resolve)"
        )
    elif not re.match(policy.commit_pattern, commit):
        bad(
            f"commit {commit!r} does not match {policy.commit_pattern}; it takes the full "
            f"40-character sha in lower case"
        )

    # Where the app goes, under the target that builds for it. A channel listed under the wrong
    # target is caught by the shape of the file, and a target can feed several channels.
    distribution = data.get("distribution")
    used_channels: list[str] = []
    if not isinstance(distribution, dict) or not distribution:
        bad(
            "distribution is required: the channels this app is published to, under the target "
            f"that builds for them ({', '.join(policy.buildable_targets)})"
        )
        distribution = {}
    for target, channels in distribution.items():
        if target not in policy.buildable_targets:
            bad(
                f"distribution names the target {target!r}, which no channel takes a package "
                f"from ({', '.join(policy.buildable_targets)})"
            )
            continue
        if not isinstance(channels, list) or not channels:
            bad(f"distribution.{target} must list at least one channel")
            continue
        for channel in channels:
            if not isinstance(channel, str) or channel not in known_channels:
                bad(
                    f"distribution.{target} names {channel!r}, which is not a channel this "
                    f"catalog knows ({', '.join(sorted(known_channels))})"
                )
                continue
            spec = known_channels[channel]
            if not spec.ready:
                bad(
                    f"{channel} is on the way and cannot be published to yet; the catalog's "
                    f"channels are {', '.join(sorted(policy.ready_channels))}"
                )
                continue
            if spec.target != target:
                bad(f"{channel} takes a {spec.target} package, and it is listed under {target}")
                continue
            if channel in used_channels:
                bad(f"{channel} is listed twice")
                continue
            used_channels.append(channel)

    # A channel's settings live under its name, and only for channels this app publishes to.
    for name, spec in known_channels.items():
        if name not in data:
            continue
        settings = data[name]
        if not isinstance(settings, dict):
            bad(f"{name} must be a mapping of settings")
            continue
        if name not in used_channels:
            bad(f"{name} has settings, and distribution does not send this app there")
        for key in sorted(set(settings) - spec.options):
            bad(
                f"{name}: unknown setting {key!r}"
                + (f" (it takes {', '.join(sorted(spec.options))})" if spec.options else "")
            )
        if "submit" in settings and not isinstance(settings["submit"], bool):
            bad(f"{name}.submit must be true or false")
        secret = settings.get("profile-secret")
        if secret is not None and (
            not isinstance(secret, str) or not re.match(r"^[A-Z][A-Z0-9_]*$", secret)
        ):
            bad(f"{name}.profile-secret takes the NAME of a repository secret; this looks like a value")

    # Titles are unique in the catalog, and the stores reject a duplicate anyway
    # (https://appfair.org/docs/inclusion-criteria/#naming).
    for other in others or []:
        if other.path == app.path:
            continue
        if isinstance(title, str) and str(other.data.get("title", "")).lower() == title.lower():
            bad(f"title {title!r} is already taken by {other.path.name}")
        # Two apps publishing as one id would submit to each other's store records.
        if not token:
            continue
        for what, resolve in (("id", published_id), ("android-id", published_android_id)):
            mine = resolve(app, policy)
            if mine == resolve(other, policy):
                bad(f"{what} {mine!r} is already taken by {other.path.name}")

    return problems


def cmd_validate(args: argparse.Namespace) -> int:
    policy = Policy.load()
    everything = catalog()
    if args.all or not args.paths:
        apps = everything
    else:
        apps = [load_app(Path(p) if Path(p).is_absolute() else ROOT / p) for p in args.paths]
    problems: list[Problem] = []
    for app in apps:
        found = validate_app(app, policy, everything)
        problems += found
        if not found:
            print(f"ok       {app.path.name}: {app.token} {app.data.get('tag', '')}")
    for problem in problems:
        problem.emit()
    if problems:
        print(f"\n{len(problems)} problem(s) in {len(apps)} submission(s)", file=sys.stderr)
        return 1
    print(f"\n{len(apps)} submission(s) valid")
    return 0


# --------------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------------


def changed_files(base: str) -> list[str]:
    """The files this branch changes against `base`, from git."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        # A shallow clone without the base commit. A merge to the default branch changes what its
        # last commit changed, so the fallback covers that case.
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def app_channels(app: App, policy: Policy) -> list[tuple[str, Channel]]:
    """Every (target, channel) this submission publishes to, in the order it lists them."""
    out: list[tuple[str, Channel]] = []
    for target, channels in (app.data.get("distribution") or {}).items():
        for name in channels or []:
            spec = policy.channels.get(str(name))
            if spec and spec.ready and spec.target == target:
                out.append((str(target), spec))
    return out


def published_id(app: App, policy: Policy) -> str:
    """The bundle id this app publishes under.

    Whatever the submission states, and `<namespace><token>` when it states nothing. That is the
    convention a new app follows, not a rule: an app keeps the id its store records were created
    under by writing it here.
    """
    declared = str(app.data.get("id") or "").strip()
    return declared or f"{policy.id_namespace}{app.token}"


def published_android_id(app: App, policy: Policy) -> str:
    """The id Play publishes this app under.

    `android-id` when the submission states one; otherwise the bundle id with hyphens as
    underscores, since a Java package name takes no hyphen and that is the spelling day derives.
    """
    declared = str(app.data.get("android-id") or "").strip()
    return declared or published_id(app, policy).replace("-", "_")


def runner_for(target: str, policy: Policy) -> str:
    """The runner image a target builds on.

    The comparison holds this build against the one the app's own CI published, so the image has
    to be the one that CI used. A mismatch shows up as a differing Info.plist and a binary from
    another SDK, which is a real difference rather than an artifact of the flavor.
    """
    if target in policy.runners:
        return policy.runners[target]
    return "macos-15" if target.startswith("ios") else "ubuntu-latest"


# `plan --no-screenshots` sets this: a manual publish that leaves the stores' screenshots alone.
NO_SCREENSHOTS: dict[str, bool] = {}


def submits_screenshots(app: App) -> bool:
    return bool(app.data.get("screenshots", True))


def matrix_entry(app: App, policy: Policy) -> dict:
    """One row per app, holding what every stage needs from the submission."""
    pairs = app_channels(app, policy)
    return {
        "token": app.token,
        "title": app.data.get("title", app.token),
        "app_id": published_id(app, policy),
        "repo": app.owner_repo,
        "tag": app.data.get("tag", ""),
        "commit": app.data.get("commit", ""),
        # Only the catalog's flavor is built here; the app's own build belongs to its repository.
        "flavor": policy.flavor,
        "flavors_only": True,
        "targets": ",".join(sorted({target for target, _ in pairs})),
        "channels": ",".join(channel.name for _, channel in pairs),
        "day_version": policy.day_version,
        # Whether the stores' screenshots are replaced: the file's `screenshots`, true unless it
        # says otherwise, and `plan --no-screenshots` for one run.
        "screenshots": bool(app.data.get("screenshots", True)) and not NO_SCREENSHOTS.get("set", False),
    }


def build_rows(entry: dict, policy: Policy) -> list[dict]:
    """One row per target: the build and the validation of what it produced."""
    return [
        dict(entry, target=target, runner=runner_for(target, policy))
        for target in entry["targets"].split(",")
        if target
    ]


def publish_rows(app: App, entry: dict, policy: Policy) -> list[dict]:
    """One row per channel: the signing and upload of its target's package."""
    rows = []
    for target, channel in app_channels(app, policy):
        settings = app.data.get(channel.name) or {}
        rows.append(
            dict(
                entry,
                target=target,
                runner=runner_for(target, policy),
                channel=channel.name,
                # Publishing means submitting: the channel's own lane asks for review and
                # distribution. `submit: false` under the channel is the exception, and runs the
                # lane that uploads the build and leaves it alone.
                lane=channel.lane if settings.get("submit", True) else channel.hold_lane,
                # Empty unless the submission named a secret. The Apple channel requests a
                # profile during the run from the catalog's App Store Connect key.
                profile_secret=(
                    str(settings.get("profile-secret", ""))
                    if "profile-secret" in channel.options
                    else ""
                ),
            )
        )
    return rows


def cmd_plan(args: argparse.Namespace) -> int:
    policy = Policy.load()
    NO_SCREENSHOTS["set"] = bool(getattr(args, "no_screenshots", False))
    if args.app:
        paths = [p for p in (APPS / f"{args.app}.yaml", APPS / f"{args.app}.yml") if p.exists()]
        if not paths:
            Problem("apps", f"no submission named {args.app!r} in apps/").emit()
            return 1
    else:
        files = args.changed or changed_files(args.changed_from or "origin/main")
        paths = [
            ROOT / f
            for f in files
            if f.startswith("apps/") and f.endswith((".yaml", ".yml")) and (ROOT / f).exists()
        ]
    apps = [load_app(p) for p in sorted(set(paths))]
    problems = [p for app in apps for p in validate_app(app, policy, catalog())]
    for problem in problems:
        problem.emit()
    if problems:
        return 1

    entries = [matrix_entry(app, policy) for app in apps]
    matrix = json.dumps({"include": entries}, separators=(",", ":"))
    # Two more matrices, because the stages fan out differently. A build happens once per target;
    # a submission happens once per channel, and a target can feed several.
    builds = [row for entry in entries for row in build_rows(entry, policy)]
    publishes = [
        row for app, entry in zip(apps, entries) for row in publish_rows(app, entry, policy)
    ]
    # One channel, for publishing again after a store-side failure without repeating the upload
    # the other channel already accepted.
    if args.channel:
        publishes = [row for row in publishes if row["channel"] == args.channel]
        if not publishes:
            Problem("plan", f"no submission here publishes to {args.channel!r}").emit()
            return 1
        wanted = {row["target"] for row in publishes}
        builds = [row for row in builds if row["target"] in wanted]
    # A named lane, for a release that needs finishing rather than repeating: `ios submit`
    # attaches a binary App Store Connect already has, where `ios release` would upload it again
    # and Apple refuses a build number twice.
    if args.lane:
        if len(publishes) != 1:
            Problem("plan", "--lane needs one channel, so name --channel with it").emit()
            return 1
        publishes = [dict(row, lane=args.lane) for row in publishes]

    build_matrix = json.dumps({"include": builds}, separators=(",", ":"))
    publish_matrix = json.dumps({"include": publishes}, separators=(",", ":"))
    summary = [
        "| app | token | tag | targets | channels |",
        "|---|---|---|---|---|",
    ] + [
        f"| {e['title']} | `{e['token']}` | `{e['tag']}` | {e['targets']} | {e['channels']} |"
        for e in entries
    ]
    if not entries:
        summary = ["No submission changed in this pull request."]

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"matrix={matrix}\n")
            fh.write(f"build-matrix={build_matrix}\n")
            fh.write(f"publish-matrix={publish_matrix}\n")
            fh.write(f"count={len(entries)}\n")
            fh.write(f"builds={len(builds)}\n")
            fh.write(f"publishes={len(publishes)}\n")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write("\n".join(summary) + "\n")
    print(matrix)
    print(build_matrix)
    print(publish_matrix)
    print("\n".join(summary), file=sys.stderr)
    return 0


# --------------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Check a submission against the app it points at, once that app is checked out.

    Answers what the metadata alone cannot: whether the commit builds the app the file claims,
    under the bundle id the App Fair publishes it as.
    """
    policy = Policy.load()
    everything = catalog()
    app = next((a for a in everything if a.token == args.app), None)
    if app is None:
        Problem("apps", f"no submission named {args.app!r} in apps/").emit()
        return 1
    rel = relative(app.path)
    problems = validate_app(app, policy, everything)

    metadata = json.loads(Path(args.metadata).read_text())
    project = metadata.get("project", {})
    resolved_id = project.get("id", "")
    version = str(project.get("version", ""))
    tag = str(app.data.get("tag", ""))
    def bad(message: str) -> None:
        problems.append(Problem(rel, message))

    # The id is the app's: whatever its manifest states, the stores accept, and no other
    # submission has claimed. Stating `id` / `android-id` pins it, so a build that changed the
    # record it publishes to fails here rather than at the store.
    android = project.get("resolved", {}).get("android-mdc", {})
    built_ids = {"id": resolved_id, "android-id": android.get("id", "")}

    # The App Fair is the publisher of record for what it uploads, so every id a submission
    # resolves to is inside its namespace; what follows the namespace is the app's own business.
    # An app whose own builds go out under another id puts these in its flavor manifest
    # (`Day-<flavor>.toml`), which the queue reads.
    for field, value in built_ids.items():
        if not value or (field == "android-id" and "android-mdc" not in app.data.get("distribution", {})):
            continue
        if not value.startswith(policy.id_namespace):
            bad(
                f"this tag builds {field} {value!r}, which is outside {policy.id_namespace!r}. "
                f"An App Fair build publishes inside that namespace; state the id it publishes "
                f"under in Day-{policy.flavor}.toml, which the queue builds through"
            )
    for field, resolve in (("id", published_id), ("android-id", published_android_id)):
        if field == "android-id" and "android-mdc" not in app.data.get("distribution", {}):
            continue
        if app.data.get(field) is None:
            continue
        pinned = resolve(app, policy)
        if built_ids[field] != pinned:
            bad(
                f"this file pins {field} {pinned!r}, and this tag builds {built_ids[field]!r}. "
                f"Update the pin for an intentional change of store record, or check with the "
                f"maintainer"
            )
    for other in everything:
        if other.path == app.path or other.data.get("id") is None:
            continue
        if published_id(other, policy) == resolved_id:
            bad(f"{other.path.name} already publishes {resolved_id!r}")

    # Does the tag still point at the pinned commit? The stages check out the commit, so a moved
    # tag changes nothing that is built, but it usually means a different release was intended.
    submitted = str(app.data.get("commit", ""))
    if args.tag_commit is not None and submitted and args.tag_commit != submitted:
        bad(
            f"the tag now points at {args.tag_commit[:12]}, and this submission names "
            f"{submitted[:12]}. The build follows the commit. Update `commit` for an intentional "
            f"move, or check with the maintainer"
        )

    # The tag names the version being published. `v2.0.2` publishes 2.0.2, so a release in a
    # store can be traced back to a tag, and two releases cannot carry the same version. A flavor
    # that states its own `version` breaks that, and inheriting the source version keeps it.
    tagged = re.match(r"^v(\d+\.\d+\.\d+)", tag)
    if tagged:
        built = {version} | {
            str(project.get("resolved", {}).get(target, {}).get("version", version))
            for target in app.data.get("distribution", {})
        }
        for other in sorted(v for v in built if v and v != tagged.group(1)):
            bad(
                f"{tag} builds version {other!r}. The tag names the version the App Fair "
                f"publishes, so either tag the release v{other}, or let the flavor take the "
                f"source version and drop its own `version` from Day-{policy.flavor}.toml"
            )

    declared = set(project.get("targets", []))
    for target in app.data.get("distribution", {}):
        if declared and target not in declared:
            bad(
                f"targets include {target}, which the app does not declare "
                f"({', '.join(sorted(declared))})"
            )

    # Which flavor this was read with. An app that declares the catalog's flavor has to be read
    # through it, since that manifest is what states the identity it publishes under; an app
    # that declares none is read as it stands, and then a flavored read is the workflow's bug.
    flavor = metadata.get("flavor") or ""
    declares = set(metadata.get("flavors") or [])
    if policy.flavor in declares and flavor != policy.flavor:
        bad(
            f"{app.token} carries Day-{policy.flavor}.toml, and the metadata was read with "
            f"flavor {flavor!r}; the workflow and policy.yaml disagree"
        )
    elif policy.flavor not in declares and flavor:
        bad(
            f"the metadata was read with flavor {flavor!r}, which {app.token} does not declare "
            f"({', '.join(sorted(declares)) or 'it declares none'})"
        )

    # The review shows the reviewer the app's screenshots from the site the app publishes, and
    # the stores show the same listing. Without the release's index there is nothing to review.
    # A submission with `screenshots: false` leaves the stores' screenshots alone, so none of
    # this applies to it.
    if not getattr(args, "offline", False) and submits_screenshots(app):
        if getattr(args, "gallery", None):
            try:
                index, why = json.loads(Path(args.gallery).read_text()), args.gallery
            except (OSError, ValueError) as error:
                index, why = None, f"{args.gallery} could not be read: {error}"
        else:
            index, why = gallery_index(app, tag)
        if index is None:
            bad(why)
        else:
            problems.extend(screenshot_problems(app, index, store_rules(app, tag), rel))

    for problem in problems:
        problem.emit()
    if problems:
        return 1
    ids = resolved_id + (f" / {built_ids['android-id']}" if built_ids["android-id"] else "")
    print(f"verified {app.token}: {ids} {version} ({project.get('build')}) from {tag}")
    return 0


# --------------------------------------------------------------------------------------------
# add and update
# --------------------------------------------------------------------------------------------


def remote_tags(repo_url: str) -> list[str]:
    """Every tag in the app's repository, read without cloning it."""
    # Without this, a missing repository prompts for a username instead of reporting the name.
    out = subprocess.run(
        ["git", "ls-remote", "--tags", repo_url],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if out.returncode != 0:
        detail = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "unreadable"
        if "could not read Username" in detail or "Authentication failed" in detail:
            detail = "no public repository there; the token is the repository name"
        raise RuntimeError(f"{repo_url}: {detail}")
    names = set()
    for line in out.stdout.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/"):
            names.add(ref[len("refs/tags/") :].removesuffix("^{}"))
    return sorted(names)


def version_key(tag: str) -> tuple:
    """A sortable form of a tag: the numbers, then releases ahead of their own pre-releases."""
    match = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:[.-](.*))?$", tag)
    if not match:
        return (0, 0, 0, 0, tag)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), 0 if suffix else 1, suffix or "")


def newest_tag(tags: list[str], pattern: str) -> str | None:
    """The highest tag the catalog accepts."""
    usable = [t for t in tags if re.match(pattern, t)]
    return max(usable, key=version_key) if usable else None


def tag_commit(repo_url: str, tag: str) -> str | None:
    """The commit a tag points at, peeling annotated tags."""
    for ref in (f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"):
        out = subprocess.run(
            ["git", "ls-remote", repo_url, ref],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if out.stdout.strip():
            return out.stdout.split("\t")[0].strip()
    return None


def latest_release(owner_repo: str) -> str | None:
    """The tag of the repository's newest release, pre-release or not.

    The comparison needs a release, so a release is a better answer than the highest tag. A
    pre-release counts: an app that stages its releases (daybrite/actions `release-mode:
    pre-release`) publishes one for every version and marks it Latest once it is approved, and the
    one waiting for approval is exactly the one the catalog is asked to build. Drafts do not, since
    their assets are not downloadable.

    A failure here falls back to the tags.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner_repo}/releases?per_page=20", method="GET"
    )
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            releases = json.loads(response.read())
    except (urllib.error.HTTPError, OSError, ValueError):
        return None
    if not isinstance(releases, list):
        return None
    # The list arrives newest first, by creation.
    for release in releases:
        if isinstance(release, dict) and not release.get("draft"):
            return str(release.get("tag_name") or "") or None
    return None


def fetch_text(owner_repo: str, ref: str, path: str) -> str | None:
    """One file from a public repository, as text, or None."""
    import urllib.error
    import urllib.request

    url = f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
            return response.read().decode().strip()
    except (urllib.error.HTTPError, OSError, UnicodeDecodeError):
        return None


# The site's development channel: what the last deploy built. After a tag build that is the
# tagged version's captures, the same files the release carries, which is what lets the review
# link an image rather than point into a zip.
GALLERY_PATH = "main/gallery/gallery.json"
# The comment has to stay under GitHub's limit for one comment (65,536 characters), with room
# for the rest of the review. Past this, later locales fold to a link.
COMMENT_BUDGET = 56_000
OS_NAMES = {"ios": "iOS", "android": "Android"}
DEVICE_NAMES = {"iphone": "iPhone", "ipad": "iPad", "phone": "Phone", "tablet": "Tablet"}
LANGUAGE_NAMES = {
    "ar": "Arabic", "de": "German", "en": "English", "es": "Spanish", "fr": "French",
    "hi": "Hindi", "id": "Indonesian", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "pt-BR": "Portuguese (Brazil)",
    "ru": "Russian", "sv": "Swedish", "th": "Thai", "tr": "Turkish", "uk": "Ukrainian",
    "vi": "Vietnamese", "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
}


def fetch_json(url: str) -> tuple[int, object]:
    """A public URL, decoded. `(0, None)` when it could not be asked at all."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


def site_host(owner_repo: str, commit: str) -> str:
    """The `host` the app's website/site.toml names at that commit, or an empty string."""
    text = fetch_text(owner_repo, commit, "website/site.toml")
    if not text:
        return ""
    try:
        host = tomllib.loads(text).get("host")
    except tomllib.TOMLDecodeError:
        return ""
    return str(host).rstrip("/") if host else ""


def release_asset_url(app: "App", tag: str, name: str) -> str:
    return f"https://github.com/{app.owner_repo}/releases/download/{tag}/{name}"


def store_rules(app: "App", tag: str) -> dict:
    """The stores' screenshot rules to hold the release's captures to, in policy.yaml's shape.

    The release's own `storefront.json` (what `day store export` wrote, with the rules the
    app's CLI held the listing to) first, so the queue and the app agree; policy.yaml's copy
    for a release that predates the document.
    """
    import urllib.error
    import urllib.request

    url = release_asset_url(app, tag, "storefront.json")
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
            doc = json.loads(response.read().decode())
    except (urllib.error.HTTPError, OSError, ValueError):
        return load_yaml(ROOT / "policy.yaml")
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, dict):
        return load_yaml(ROOT / "policy.yaml")
    screenshots = {
        store: rule.get("screenshots") or {}
        for store, rule in rules.items()
        if isinstance(rule, dict) and rule.get("layout")
    }
    return {"screenshots": screenshots}


def gallery_index(app: "App", tag: str) -> tuple[dict | None, str]:
    """The screenshot index the app's release carries, or why there is none.

    The captures that go to the stores are the tagged version's own: the app's CI ran its
    walkthrough on the tag and attached `gallery.json` and `screenshots.zip` to the release, and
    the signing stage takes them from there. Reading the release rather than the app's website
    means a submission needs no site, and the set cannot drift from the version.
    """
    url = release_asset_url(app, tag, "gallery.json")
    status, body = fetch_json(url)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("screenshots"), list):
        return None, (
            f"{url} could not be read ({status or 'no answer'}). An App Fair submission takes the "
            f"listing's screenshots from the release: the shared Day workflow attaches "
            f"gallery.json and screenshots.zip to every release whose dayscripts captured "
            f"screenshots (https://daybrite.dev/docs/ci)"
        )
    return body, url


def site_gallery(app: "App", commit: str) -> dict | None:
    """The app's site's own index of its latest build, when the site is published.

    Only the review comment reads it, to link an image the reviewer can open: a release's
    captures live in its screenshots.zip, and the site's development channel serves the same
    files after the tag build deploys it.
    """
    host = site_host(app.owner_repo, commit)
    if not host:
        return None
    status, body = fetch_json(f"{host}/{GALLERY_PATH}")
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("screenshots"), list):
        return None
    return body


def viewable(index: dict, site: dict | None) -> dict:
    """The index with each capture's `url` pointing at the site's copy of that very file, by
    sha-256, and no `url` at all where the site has none or a different one."""
    served = {
        (str(e.get("path")), str(e.get("sha256"))): str(e.get("url"))
        for e in (site or {}).get("screenshots") or []
        if isinstance(e, dict) and e.get("url")
    }
    shots = []
    for e in index.get("screenshots") or []:
        if not isinstance(e, dict):
            continue
        url = served.get((str(e.get("path")), str(e.get("sha256"))))
        shots.append({**e, "url": url} if url else {k: v for k, v in e.items() if k != "url"})
    return {**index, "screenshots": shots}


STORE_LABELS = {"apple-app-store": "App Store", "google-play-store": "Google Play"}
CHANNEL_TARGETS = {"apple-app-store": "ios-uikit", "google-play-store": "android-mdc"}


def capture_kind(target: str, device: object) -> str:
    """The device kind a capture belongs to: its slug, or what a profile-less capture on the
    platform is (`iphone`, `phone`), the way the day CLI reads it."""
    if device:
        return str(device)
    return {"ios-uikit": "iphone", "android-mdc": "phone"}.get(target, "default")


def listing_captures(index: dict, target: str, store: str) -> list[dict]:
    """The captures `store`'s listing shows for `target`, from the index's `listings` (what
    `day screenshot index` resolved from the app's store/storefront.toml [storefront]): per device
    kind and locale, the declared shots in order, in the declared theme (a capture with no
    theme is taken). Each carries `position`, `kind` and `store`."""
    kinds = (((index.get("listings") or {}).get(target) or {}).get("stores") or {}).get(store) or {}
    out = []
    for kind, by_locale in kinds.items():
        for locale, wanted in (by_locale or {}).items():
            for i, want in enumerate(wanted or []):
                if not isinstance(want, dict):
                    continue
                for e in index.get("screenshots") or []:
                    if e.get("platform") != target or e.get("shot") != want.get("shot"):
                        continue
                    if capture_kind(target, e.get("device")) != kind or e.get("locale") != locale:
                        continue
                    if e.get("theme") not in (None, want.get("theme") or "light"):
                        continue
                    out.append({**e, "position": i + 1, "kind": kind, "store": store})
    return sorted(out, key=lambda s: (s["store"], s["kind"], str(s.get("locale")), s["position"]))


def listing_pairs(index: dict, channels: list[str]) -> list[tuple[str, str]]:
    """Every (target, store) the index declares among the catalog's channels, App Store first."""
    listings = index.get("listings") or {}
    pairs = []
    for channel in channels:
        target = CHANNEL_TARGETS.get(channel)
        if target and channel in ((listings.get(target) or {}).get("stores") or {}):
            pairs.append((target, channel))
    return pairs


def screenshot_problems(app: "App", index: dict, policy_raw: dict, path: str) -> list["Problem"]:
    """What the stores would refuse about the listing's screenshots, read from the index alone.

    Sizes come from the index (`width`/`height`), so nothing is downloaded. Apple takes exact
    sizes per device kind; Google a range, with the limits its API enforces. A kind the
    policy marks `required` needs at least one screenshot in every locale the listing carries,
    which is the refusal App Store Connect gives at submission time otherwise.
    """
    rules_by_channel = policy_raw.get("screenshots") or {}
    problems: list[Problem] = []
    index_locales = [str(l) for l in index.get("locales") or []]
    for target, channels in (app.data.get("distribution") or {}).items():
        if target not in CHANNEL_TARGETS.values():
            continue
        for channel in channels or []:
            rules = rules_by_channel.get(channel)
            if not isinstance(rules, dict):
                continue
            chosen = listing_captures(index, target, channel)
            for shot in chosen:
                if not any(isinstance(rules.get(k), dict) and (shot["kind"] == k or (k == "tablet" and shot["kind"].startswith("tablet"))) for k in rules):
                    problems.append(Problem(path, (
                        f"{channel}: the screenshot {shot.get('shot')!r} ({shot.get('locale')}) was captured on device "
                        f"{shot['kind']!r}, which is not a kind the store lists ({', '.join(rules)}); the CI device "
                        f"profile's `slug=` names it"
                    )))
            for kind, rule in rules.items():
                if not isinstance(rule, dict):
                    continue
                mine = [s for s in chosen if s["kind"] == kind or (kind == "tablet" and s["kind"].startswith("tablet"))]
                # Each screenshot's size, against what the store takes for that kind.
                for shot in mine:
                    w, h = shot.get("width") or 0, shot.get("height") or 0
                    sizes = rule.get("sizes")
                    if sizes and [w, h] not in [list(x) for x in sizes]:
                        accepted = ", ".join(f"{a}×{b}" for a, b in sizes)
                        problems.append(Problem(path, (
                            f"{channel}: the {kind} screenshot {shot.get('shot')!r} ({shot.get('locale')}) "
                            f"is {w}×{h}, which the store does not take for a {kind}; it takes {accepted}. "
                            f"Capture on a device profile that produces one of those"
                        )))
                        continue
                    lo, hi = min(w, h), max(w, h)
                    if rule.get("min-side") and lo < int(rule["min-side"]):
                        problems.append(Problem(path, (
                            f"{channel}: the {kind} screenshot {shot.get('shot')!r} ({shot.get('locale')}) is {w}×{h}; "
                            f"the short side has to be at least {rule['min-side']} px. A CI tablet past three million "
                            f"pixels is captured halved (`medium_tablet` at 1280×800); `Nexus 7 2013` with `density=240` "
                            f"captures 1920×1200"
                        )))
                    if rule.get("max-side") and hi > int(rule["max-side"]):
                        problems.append(Problem(path, f"{channel}: the {kind} screenshot {shot.get('shot')!r} ({shot.get('locale')}) is {w}×{h}; the long side has to be at most {rule['max-side']} px"))
                    ratio = float(rule.get("max-ratio") or 0)
                    if ratio and lo and hi / lo > ratio + 1e-9:
                        problems.append(Problem(path, (
                            f"{channel}: the {kind} screenshot {shot.get('shot')!r} ({shot.get('locale')}) is {w}×{h}, "
                            f"{hi / lo:.2f}:1; Google Play takes at most {ratio:g}:1 (the long side no more than "
                            f"{ratio:g}× the short). Capture on a {kind} profile with a shorter screen"
                        )))
                # Coverage: every locale, and not more than the store's ceiling.
                per_locale: dict[str, int] = {}
                for shot in mine:
                    per_locale[str(shot.get("locale"))] = per_locale.get(str(shot.get("locale")), 0) + 1
                limit = int(rule.get("max") or 0)
                for locale, n in sorted(per_locale.items()):
                    if limit and n > limit:
                        problems.append(Problem(path, f"{channel}: {n} {kind} screenshots for {locale}, and the store takes at most {limit}; lower the `store:` marks in the walkthrough to {limit}"))
                if rule.get("required"):
                    missing = [l for l in index_locales if l not in per_locale]
                    if not mine:
                        problems.append(Problem(path, (
                            f"{channel} needs {kind} screenshots and the listing declares none for it, or the "
                            f"walkthrough captured none on a {kind} profile. Declare them in store/storefront.toml, "
                            f"`[storefront.{target}.{channel}.screenshots]` with a `{kind}` or `default` list, "
                            f"and capture on a {kind} profile"
                        )))
                    elif missing:
                        problems.append(Problem(path, f"{channel}: no {kind} screenshots for {', '.join(missing)}; the walkthrough captured other locales, so run it for these too"))
    return problems


def cmd_listing(args: argparse.Namespace) -> int:
    """Extract the listing's captures from a release's screenshots.zip into a capture tree.

    The tree is what the later stages use: `day store stage --screenshots <out>/gallery.json`
    places from it, and the review's artifact holds it, so the stores receive exactly what the
    reviewer was shown. Each file is checked against the index's sha-256 as it is written.
    """
    import hashlib
    import zipfile

    index = json.loads(Path(args.gallery).read_text())
    channels = list(Policy.load().channels)
    chosen: list[dict] = []
    seen: set[str] = set()
    for target, store in listing_pairs(index, channels):
        for shot in listing_captures(index, target, store):
            if str(shot.get("path")) not in seen:
                seen.add(str(shot.get("path")))
                chosen.append(shot)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    written = 0
    with zipfile.ZipFile(args.zip) as archive:
        members = set(archive.namelist())
        for shot in chosen:
            member = str(shot.get("path") or "").removeprefix("gallery/")
            if not member or member not in members:
                problems.append(f"{shot.get('path')} is in gallery.json and not in {args.zip}")
                continue
            data = archive.read(member)
            digest = hashlib.sha256(data).hexdigest()
            if shot.get("sha256") and digest != shot.get("sha256"):
                problems.append(f"{member} in {args.zip} is not the file gallery.json describes (sha-256 differs)")
                continue
            target = out / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
    (out / "gallery.json").write_text(json.dumps(index))
    for problem in problems:
        Problem("listing", problem).emit()
    if problems:
        return 1
    print(f"{written} listing capture(s) from {args.zip} → {out}")
    return 0


def screenshot_groups(captures: list[dict], index: dict) -> list[tuple[tuple[str, str, str], list[dict]]]:
    """Listing captures grouped by store, device kind and language, in review order: the App
    Store before Google Play, phones before tablets, the site's default language first; within
    a group, listing order."""
    order_store = list(STORE_LABELS)
    order_kind = ["iphone", "ipad", "phone", "tablet"]
    order_locale = list(index.get("locales") or [])
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for shot in captures:
        key = (str(shot["store"]), str(shot["kind"]), str(shot.get("locale", "")))
        groups.setdefault(key, []).append(shot)

    def rank(key):
        store, kind, locale = key
        return (
            order_store.index(store) if store in order_store else len(order_store),
            store,
            order_kind.index(kind) if kind in order_kind else len(order_kind),
            kind,
            order_locale.index(locale) if locale in order_locale else len(order_locale),
            locale,
        )

    return [
        (key, sorted(groups[key], key=lambda s: (s["position"], str(s.get("shot", "")))))
        for key in sorted(groups, key=rank)
    ]


def screenshot_section(index: dict, url: str, budget: int = COMMENT_BUDGET, artifact: tuple[str, str] | None = None) -> str:
    """The screenshots as folded blocks, one per platform, device, theme and language.

    Every block lists its images at thumbnail size, linked to the full capture. When the whole
    set would not fit in one comment, later languages keep their block but link to the gallery
    page instead of inlining, and the block says so.
    """
    site = str(index.get("site") or url.rsplit("/", 2)[0]).rstrip("/")
    channels = list(Policy.load().channels)
    listing = [s for target, store in listing_pairs(index, channels) for s in listing_captures(index, target, store)]
    if not listing:
        return (
            f"No screenshots are declared for the store listings in the [gallery index]({url}). "
            "The app declares them in store/storefront.toml under `[storefront.<target>.<store>.screenshots]`, "
            "naming its walkthrough's `screenshot:` steps (https://daybrite.dev/docs/store#screenshots)."
        )
    groups = screenshot_groups(listing, index)

    def summary(key, count):
        store, kind, locale = key
        return (
            f"{STORE_LABELS.get(store, store)} listing ({DEVICE_NAMES.get(kind, kind or 'device')}, "
            f"{LANGUAGE_NAMES.get(locale, locale or 'default')}) · {count}"
        )

    def inline(key, shots):
        # A capture the site does not serve is named rather than shown: it is in the release's
        # screenshots.zip, which is what the store receives.
        images = " ".join(
            f'<img src="{s["url"]}" width="120" alt="{s.get("title") or s.get("shot") or ""}">'
            if s.get("url") else f"<code>{s.get('shot') or s.get('file') or ''}</code>"
            for s in shots
        )
        return f"<details><summary>{summary(key, len(shots))}</summary>\n\n<p>{images}</p>\n</details>"

    def folded(key, shots):
        locale = key[2]
        page = f"{site}/{locale}/main/gallery/" if locale else f"{site}/main/gallery/"
        return (
            f"<details><summary>{summary(key, len(shots))}</summary>\n\n"
            f"Not inlined, to keep this comment under GitHub's size limit: [open the gallery]({page}).\n"
            f"</details>"
        )

    generated = str(index.get("generated") or "")[:10]
    unseen = sum(1 for s in listing if not s.get("url"))
    head = (
        f"The listing's screenshots, from the release's [gallery.json]({url})"
        + (f", generated {generated}" if generated else "")
        + ", in listing order"
        + (
            f" ({unseen} of them named rather than shown: the site's latest build does not serve "
            f"that file, so it is in the release's screenshots.zip)"
            if unseen else ""
        )
        + ":"
    )
    if artifact:
        name, link = artifact
        head += (
            f"\n\nThe complete set the stores receive, as this run's artifact: [`{name}`]({link}) "
            f"(a zip of the captures with their `gallery.json`)."
        )
    blocks = [inline(key, shots) for key, shots in groups]
    # Fold until the section fits, least useful first: other languages before the site's
    # default, dark before light, tablets before phones. Every group keeps its heading, so the
    # reviewer still sees what exists and where.
    locales = list(index.get("locales") or [])
    def fold_rank(i):
        store, kind, locale = groups[i][0]
        theme = "light"
        device = kind
        return (
            -(locales.index(locale) if locale in locales else len(locales)),
            0 if theme == "dark" else 1,
            0 if device in ("ipad", "tablet") else 1,
            -i,
        )
    for i in sorted(range(len(groups)), key=fold_rank):
        if len("\n\n".join([head] + blocks)) <= budget:
            break
        blocks[i] = folded(*groups[i])
    return "\n\n".join([head] + blocks)


def listing_name(text: str, yaml_form: bool) -> str | None:
    """The default locale's `name` from a storefront file's `[storefront.metadata]`, or None."""
    try:
        doc = yaml.safe_load(text) if yaml_form else tomllib.loads(text)
    except (yaml.YAMLError, tomllib.TOMLDecodeError, ValueError):
        return None
    metadata = ((doc or {}).get("storefront") or {}).get("metadata") or {}
    name = metadata.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def app_title(owner_repo: str, commit: str, token: str, flavor: str) -> str:
    """The app's name, from its store listing, then its manifest, then the token.

    The flavor's listing and manifest first, for an app that carries one, then the app's own:
    a flavor is optional, and without one those are the files that name it. The listing's
    name is `[storefront.metadata] name` in store/storefront.toml (or storefront.yaml, the same
    tree; the names from before the rename, app.toml and app.yaml, still read).
    """
    for directory in (f"store-{flavor}", "store"):
        for file in ("storefront.toml", "storefront.yaml", "storefront.yml", "app.toml", "app.yaml", "app.yml"):
            text = fetch_text(owner_repo, commit, f"{directory}/{file}")
            if text:
                name = listing_name(text, not file.endswith(".toml"))
                if name:
                    return name
    for path in (f"Day-{flavor}.toml", "Day.toml"):
        manifest = fetch_text(owner_repo, commit, path)
        if manifest:
            match = re.search(r'(?m)^\s*title\s*=\s*"([^"]+)"', manifest)
            if match:
                return match.group(1)
    return token.replace("-", " ")


def released(token: str, policy: Policy, wanted: str | None) -> tuple[str, str, str]:
    """The tag to submit and the commit it points at.

    Returns `(tag, commit, how)`; `how` records whether the tag came from the latest release or
    from the tag list.
    """
    owner_repo = f"{token}/{token}"
    repo_url = f"https://github.com/{owner_repo}"
    tag, how = wanted, "named on the command line"
    if not tag:
        tag = latest_release(owner_repo)
        how = "the newest release"
    if not tag:
        tag = newest_tag(remote_tags(repo_url), policy.tag_pattern)
        how = "the highest tag (no release was readable)"
    if not tag:
        raise RuntimeError(f"{repo_url} publishes no release or tag this catalog would accept")
    if not re.match(policy.tag_pattern, tag):
        raise RuntimeError(f"{tag} does not match {policy.tag_pattern}")
    commit = tag_commit(repo_url, tag)
    if not commit:
        raise RuntimeError(f"{repo_url} has no tag {tag}")
    return tag, commit, how


def submission_text(token: str, title: str, tag: str, commit: str, policy: Policy) -> str:
    """The text of a new submission file."""
    channels = {}
    for name, channel in policy.ready_channels.items():
        channels.setdefault(channel.target, []).append(name)
    distribution = "\n".join(
        f"  {target}:\n" + "\n".join(f"    - {name}" for name in sorted(names))
        for target, names in sorted(channels.items())
    )
    return f"""# yaml-language-server: $schema=../schema/app.schema.json
#
# {title} in the App Fair catalog. The file is named for the app token, which is where the app
# lives: https://github.com/{token}/{token}
#
# To publish a new version, change the tag and the commit below in a pull request. The rest is
# read from the app's repository at that commit.

token: {token}
title: {title}
tag: {tag}
# The commit that tag points at. Since a tag can be moved afterwards, this is what every stage
# checks out. `scripts/queue.py update {token}` rewrites both lines.
commit: {commit}

# Where this app goes, keyed by the target that builds for it. policy.yaml lists the channels the
# queue can publish to. Drop a line to keep the app off a channel.
distribution:
{distribution}
"""


def report(app: App, policy: Policy) -> int:
    """Validate the file that was just written and report any problems."""
    problems = validate_app(app, policy, catalog())
    for problem in problems:
        problem.emit()
    return 1 if problems else 0


def cmd_add(args: argparse.Namespace) -> int:
    """Write a new submission from the app's latest release."""
    policy = Policy.load()
    token = args.token
    path = APPS / f"{token}.yaml"
    if path.exists():
        message = f"already exists; `queue.py update {token}` moves it to a newer release"
        Problem(relative(path), message).emit()
        return 1
    try:
        tag, commit, how = released(token, policy, args.tag)
    except RuntimeError as e:
        Problem(f"{token}/{token}", str(e)).emit()
        return 1
    title = args.title or app_title(f"{token}/{token}", commit, token, policy.flavor)
    # git carries no empty directory, so a catalog with no submissions yet has no apps/.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(submission_text(token, title, tag, commit, policy))
    print(f"wrote    {relative(path)}")
    print(f"         {title}: {tag} ({commit[:12]}), from {how}")
    print("         review the title and the channel list before opening a pull request")
    return report(load_app(path), policy)


def cmd_update(args: argparse.Namespace) -> int:
    """Move an existing submission to the app's latest release, leaving the rest of the file,
    including its comments and channel settings, unchanged."""
    policy = Policy.load()
    token = args.token
    path = next((p for p in (APPS / f"{token}.yaml", APPS / f"{token}.yml") if p.exists()), None)
    if path is None:
        Problem("apps", f"no submission for {token!r}; `queue.py add {token}` writes one").emit()
        return 1
    try:
        tag, commit, how = released(token, policy, args.tag)
    except RuntimeError as e:
        Problem(relative(path), str(e)).emit()
        return 1

    text = path.read_text()
    current = load_app(path).data
    if str(current.get("tag", "")) == tag and str(current.get("commit", "")) == commit:
        print(f"ok       {relative(path)} is already {tag} ({commit[:12]}), from {how}")
        return report(load_app(path), policy)

    # Edited line by line: a YAML round trip would drop the file's comments.
    updated, tags = re.subn(r"(?m)^tag:[ \t]*\S.*$", f"tag: {tag}", text, count=1)
    updated, commits = re.subn(
        r"(?m)^commit:[ \t]*\S.*$", f"commit: {commit}", updated, count=1
    )
    if not tags or not commits:
        Problem(relative(path), "has no `tag:` or `commit:` line to rewrite").emit()
        return 1
    path.write_text(updated)
    print(f"updated  {relative(path)}")
    print(f"         {current.get('tag', '?')} → {tag} ({commit[:12]}), from {how}")
    return report(load_app(path), policy)


# --------------------------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------------------------


def cmd_resolve(args: argparse.Namespace) -> int:
    """Print the `tag` and `commit` lines for an app and tag, peeling an annotated tag."""
    repo = (args.repo or f"https://github.com/{args.token}/{args.token}").removesuffix(".git").rstrip("/")
    for ref in (f"refs/tags/{args.tag}^{{}}", f"refs/tags/{args.tag}"):
        out = subprocess.run(
            ["git", "ls-remote", repo, ref], capture_output=True, text=True
        )
        sha = out.stdout.split("\t")[0].strip() if out.stdout.strip() else ""
        if sha:
            print(f"tag: {args.tag}")
            print(f"commit: {sha}")
            return 0
        if out.returncode != 0 and out.stderr.strip():
            Problem(repo, out.stderr.strip().splitlines()[-1]).emit()
            return 1
    Problem(repo, f"no tag {args.tag!r}; tag the release before submitting it").emit()
    return 1


# --------------------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------------------

REVIEW_MARKER = "<!-- appfair-review -->"


def previous_submission(base: str, path: Path) -> dict | None:
    """The submission as it stands on `base`, or None when this pull request adds the file."""
    out = subprocess.run(
        ["git", "show", f"{base}:{relative(path)}"], cwd=ROOT, capture_output=True, text=True
    )
    if out.returncode != 0:
        return None
    try:
        data = yaml.safe_load(out.stdout)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def compare_stats(owner_repo: str, base: str, head: str) -> dict | None:
    """What GitHub says about the range between two commits of an app.

    The counts and the file list come from the compare endpoint, which reads the repository
    without cloning it. None when GitHub cannot be asked, and the comment then carries the link
    on its own.
    """
    status, payload = github_api(
        f"https://api.github.com/repos/{owner_repo}/compare/{base}...{head}?per_page=100"
    )
    if status != 200 or not isinstance(payload, dict):
        return None
    files = [str(f.get("filename", "")) for f in payload.get("files") or []]
    return {
        "commits": len(payload.get("commits") or []),
        "total_commits": int(payload.get("total_commits") or 0),
        "files": files,
        "file_count": len(files),
        "additions": sum(int(f.get("additions") or 0) for f in payload.get("files") or []),
        "deletions": sum(int(f.get("deletions") or 0) for f in payload.get("files") or []),
        "status": str(payload.get("status") or ""),
        "behind_by": int(payload.get("behind_by") or 0),
    }


def highlights(files: list[str], policy: Policy) -> tuple[list[str], int]:
    """The changed paths policy.yaml asks a reviewer to open first, and how many were left out."""
    import fnmatch

    # Shallow paths first, so an app's own manifest and build script come before the twelfth
    # crate's Cargo.toml.
    picked = sorted(
        (
            name
            for name in files
            if any(fnmatch.fnmatch(name, pattern) for pattern in policy.review_highlights)
        ),
        key=lambda name: (name.count("/"), name),
    )
    return picked[: policy.review_max], max(0, len(picked) - policy.review_max)


def other_changes(previous: dict, current: dict) -> list[str]:
    """The keys this pull request changes besides the tag and the commit."""
    keys = (set(previous) | set(current)) - {"tag", "commit"}
    return sorted(key for key in keys if previous.get(key) != current.get(key))


def plural(count: int, noun: str) -> str:
    """`1 commit`, `4 commits`. The comment is read by people, not parsed."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def review_section(
    app: App, previous: dict | None, policy: Policy, stats: dict | None, asked: bool = True
) -> str:
    """One app's part of the reviewer's comment."""
    data = app.data
    url = app.repo_url
    # The title comes out of the submission, so it is one line and carries no backtick that could
    # close the code span it lands in.
    title = " ".join(str(data.get("title", app.token)).split()).replace("`", "'")
    tag = str(data.get("tag", ""))
    commit = str(data.get("commit", ""))
    lines = [f"### {title} (`{app.token}`)", ""]

    if previous is None:
        lines += [
            f"First submission, at `{tag}`.",
            "",
            f"- [Source at `{commit[:7]}`]({url}/tree/{commit})",
            f"- [Release notes]({url}/releases/tag/{tag})",
            "",
            "Nothing from this app has been published before, so there is no range to read."
            " Review the repository as it stands.",
        ]
        return "\n".join(lines)

    was_tag = str(previous.get("tag", ""))
    was_commit = str(previous.get("commit", ""))
    changed = other_changes(previous, data)

    if was_commit == commit:
        lines.append(f"Same tag and commit as the published `{tag}`, so the source is unchanged.")
        lines.append("")
        lines.append(
            "This file changes " + ", ".join(f"`{key}`" for key in changed) + "."
            if changed
            else "Nothing else in the file changed."
        )
        return "\n".join(lines)

    lines += [
        f"`{was_tag}` → `{tag}`",
        "",
        f"- [Source diff `{was_commit[:7]}…{commit[:7]}`]"
        f"({url}/compare/{was_commit}...{commit})",
        f"- [Release notes]({url}/releases/tag/{tag})",
    ]

    if stats:
        count = stats["total_commits"] or stats["commits"]
        lines[-2] += (
            f": {plural(count, 'commit')}, {plural(stats['file_count'], 'file')}, "
            f"+{stats['additions']} −{stats['deletions']}"
        )
        if stats["file_count"] >= 100:
            lines[-2] += " (GitHub lists the first 100 files)"
        picked, rest = highlights(stats["files"], policy)
        if picked:
            lines += ["", "Build and packaging files touched:", ""]
            lines += [f"- `{name}`" for name in picked]
            if rest:
                lines.append(f"- …and {rest} more")
        # GitHub answers `ahead` when the proposed commit continues the published one.
        # Anything else is worth a reviewer's attention before the range is read.
        warning = {
            "diverged": "**These two commits have diverged.** "
            f"{plural(stats['behind_by'], 'commit')} from the published release are missing here,"
            " so the history was rewritten or the tag moved to another branch.",
            "behind": f"**This commit is {plural(stats['behind_by'], 'commit')} behind the"
            " published one**, so merging would publish older source than what is already out.",
            "identical": "**This is the commit already published.**",
        }.get(stats["status"])
        if warning:
            lines += ["", warning]
    elif asked:
        lines += ["", "GitHub did not return the range, so the link above is all there is."]

    if changed:
        named = ", ".join(f"`{key}`" for key in changed)
        lines += ["", f"The submission file also changes {named}."]
    return "\n".join(lines)


def listing_review(app: "App", commit: str, args: argparse.Namespace) -> str:
    """The screenshot part of one app's review: from the listing this run extracted from the
    release (`--listing DIR`, one `listing-<token>/` per app, with the artifact links in
    `--artifacts JSON`), or, without one, from the release's index over the network."""
    if not submits_screenshots(app):
        return (
            "The stores' screenshots are not part of this submission (`screenshots: false`); "
            "each store keeps what it shows today."
        )
    tag = str(app.data.get("tag", ""))
    url = release_asset_url(app, tag, "gallery.json")
    index = None
    artifact = None
    listing_dir = Path(args.listing) if getattr(args, "listing", None) else None
    if listing_dir:
        for candidate in (listing_dir / f"listing-{app.token}", listing_dir / app.token):
            if (candidate / "gallery.json").is_file():
                index = json.loads((candidate / "gallery.json").read_text())
                break
        links = {}
        if getattr(args, "artifacts", None):
            links = json.loads(Path(args.artifacts).read_text())
        if index is not None and links.get(app.token):
            artifact = (f"listing-{app.token}", str(links[app.token]))
    if index is None:
        index, why = gallery_index(app, tag)
        if index is None:
            return why
    return screenshot_section(viewable(index, site_gallery(app, commit)), url, artifact=artifact)


def review_body(sections: list[str]) -> str:
    """The comment itself, with the marker the workflow finds it by."""
    return "\n\n".join(
        [REVIEW_MARKER, "## Submissions"]
        + sections
        + ["*Updated on every push.*"]
    ) + "\n"


def cmd_review(args: argparse.Namespace) -> int:
    """Write the reviewer's summary of the submissions a pull request changes.

    For an update this is the range between the commit that is published and the one being
    proposed, so the source changes can be read before the submission is approved.
    """
    policy = Policy.load()
    base = args.changed_from or "origin/main"
    if args.app:
        paths = [p for p in (APPS / f"{args.app}.yaml", APPS / f"{args.app}.yml") if p.exists()]
    else:
        paths = [
            ROOT / f
            for f in (args.changed or changed_files(base))
            if f.startswith("apps/") and f.endswith((".yaml", ".yml")) and (ROOT / f).exists()
        ]
    apps = [load_app(p) for p in sorted(set(paths))]
    apps = [app for app in apps if app.data]
    if not apps:
        print("no submission changed, so there is nothing to summarize")
        return 0

    sections = []
    for app in apps:
        previous = previous_submission(base, app.path)
        stats = None
        was_commit = str((previous or {}).get("commit", ""))
        commit = str(app.data.get("commit", ""))
        if previous and was_commit and commit and was_commit != commit and not args.offline:
            stats = compare_stats(app.owner_repo, was_commit, commit)
        section = review_section(app, previous, policy, stats, asked=not args.offline)
        if not args.offline:
            section += "\n\n" + listing_review(app, commit, args)
        sections.append(section)

    body = review_body(sections)
    if args.out:
        Path(args.out).write_text(body)
        print(f"wrote    {args.out}")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(body + "\n")
    if not args.out:
        print(body)
    return 0


# --------------------------------------------------------------------------------------------
# authorize
# --------------------------------------------------------------------------------------------


def cmd_authorize(args: argparse.Namespace) -> int:
    """Report whether the author of a change maintains the app it points at.

    Maintainership is a property of the app's own repository, so this queries GitHub instead of
    keeping a list here that would go out of date.

    Prints a warning and exits 0. The reviewer who merges decides; someone other than the
    maintainer may legitimately bump a tag, and the warning puts that in the thread.
    """
    actor = (args.actor or "").lstrip("@")

    def ask(url: str) -> int:
        """The status GitHub answers with, or 0 when the question could not be asked."""
        return github_api(url)[0]

    for token_name in args.apps:
        app = next((a for a in catalog() if a.token == token_name), None)
        if app is None:
            Problem("apps", f"no submission named {token_name!r} in apps/").emit()
            continue
        owner, _, name = app.owner_repo.partition("/")
        if not actor:
            print(f"ok   {token_name}: no author to check")
            continue

        # An app under a personal account is maintained by its owner.
        if owner.lower() == actor.lower():
            print(f"ok   {token_name}: {actor} owns {app.owner_repo}")
            continue

        # Write access is the accurate answer but needs a token with that access. Public
        # membership of the app's organization is readable with any token.
        status = ask(f"https://api.github.com/repos/{owner}/{name}/collaborators/{actor}")
        if status == 204:
            print(f"ok   {token_name}: {actor} has write access to {app.owner_repo}")
            continue
        status = ask(f"https://api.github.com/orgs/{owner}/public_members/{actor}")
        if status == 204:
            print(f"ok   {token_name}: {actor} is a public member of {owner}")
            continue
        if status == 0:
            print(f"note {token_name}: GitHub could not be asked who maintains {app.owner_repo}")
            continue
        message = (
            f"{actor} is neither a public member of {owner} nor a collaborator this queue can "
            f"see on {app.owner_repo}; a reviewer should confirm this change with whoever "
            f"maintains it"
        )
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning file=apps/{token_name}.yaml::{message}")
        else:
            print(message, file=sys.stderr)
    return 0


# --------------------------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    """Record one publication, or every app in a plan's matrix, in state/published.json."""
    if args.matrix:
        rows = json.loads(args.matrix).get("include", [])
        if not rows:
            Problem("record", "--matrix holds no rows to record").emit()
            return 1
        for row in rows:
            one = argparse.Namespace(
                app=row.get("token", ""),
                tag=row.get("tag", ""),
                channels=row.get("channels", ""),
                run_url=args.run_url,
                matrix="",
            )
            if cmd_record(one):
                return 1
        return 0
    app = next((a for a in catalog() if a.token == args.app), None)
    if app is None:
        Problem("apps", f"no submission named {args.app!r} in apps/").emit()
        return 1
    state = {"apps": {}}
    if STATE.exists():
        state = json.loads(STATE.read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy = Policy.load()
    state.setdefault("apps", {})[app.token] = {
        "title": app.data.get("title", app.token),
        "id": published_id(app, policy),
        "repo": app.repo_url,
        "tag": args.tag or app.data.get("tag", ""),
        "channels": sorted(c for c in (args.channels or "").split(",") if c),
        "published": now,
        "run": args.run_url or "",
    }
    state["updated"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(f"recorded {app.token} {state['apps'][app.token]['tag']} → {relative(STATE)}")
    return 0


# --------------------------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------------------------

SIGNATURE_PATHS = ("META-INF/", "_CodeSignature/", "embedded.mobileprovision", "CodeResources")


def package_entries(path: Path) -> list[dict]:
    """Every file inside a package, with its size and digest.

    .aab, .apk and .ipa are all zips, so this reads the package as data.
    """
    import hashlib
    import zipfile

    out: list[dict] = []
    with zipfile.ZipFile(path) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            digest = hashlib.sha256()
            with z.open(info) as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            out.append(
                {"path": info.filename, "size": info.file_size, "sha256": digest.hexdigest()}
            )
    return out


def file_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_aapt2() -> str | None:
    """`aapt2` from the Android SDK, which reads an APK's binary manifest."""
    import glob
    import shutil

    found = shutil.which("aapt2")
    if found:
        return found
    roots = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "Library/Android/sdk"),
        "/usr/local/lib/android/sdk",
    ]
    for root in filter(None, roots):
        candidates = sorted(glob.glob(f"{root}/build-tools/*/aapt2"))
        if candidates:
            return candidates[-1]
    return None


def android_facts(apk: Path) -> dict:
    """Identity and permissions, read out of an APK with aapt2."""
    tool = find_aapt2()
    if tool is None:
        return {"source": None, "note": "aapt2 was unavailable, so no manifest facts were read"}
    out = subprocess.run([tool, "dump", "badging", str(apk)], capture_output=True, text=True)
    if out.returncode != 0:
        return {"source": apk.name, "note": f"aapt2 failed: {out.stderr.strip()[:200]}"}
    facts: dict = {"source": apk.name, "permissions": []}
    for line in out.stdout.splitlines():
        if line.startswith("package:"):
            for key, field in (("name", "id"), ("versionCode", "build"), ("versionName", "version")):
                m = re.search(rf"{key}='([^']*)'", line)
                if m:
                    facts[field] = m.group(1)
        elif line.startswith("uses-permission:"):
            m = re.search(r"name='([^']*)'", line)
            if m:
                facts["permissions"].append(m.group(1))
        elif line.startswith("application-label:"):
            facts["title"] = line.split(":", 1)[1].strip().strip("'")
        elif line.startswith("targetSdkVersion:"):
            facts["target-sdk"] = line.split(":", 1)[1].strip().strip("'")
        elif line.startswith("sdkVersion:"):
            facts["min-sdk"] = line.split(":", 1)[1].strip().strip("'")
    facts["permissions"] = sorted(set(facts["permissions"]))
    return facts


def apple_facts(ipa: Path) -> dict:
    """Identity, usage descriptions and entitlements, read out of an .ipa with plistlib."""
    import plistlib
    import zipfile

    facts: dict = {"source": ipa.name, "permissions": [], "entitlements": {}}
    with zipfile.ZipFile(ipa) as z:
        names = z.namelist()
        plists = [n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if not plists:
            facts["note"] = "no Payload/<app>/Info.plist in the package"
            return facts
        info = plistlib.loads(z.read(plists[0]))
        facts["id"] = info.get("CFBundleIdentifier", "")
        facts["version"] = info.get("CFBundleShortVersionString", "")
        facts["build"] = str(info.get("CFBundleVersion", ""))
        facts["title"] = info.get("CFBundleDisplayName", info.get("CFBundleName", ""))
        facts["min-os"] = info.get("MinimumOSVersion", "")
        facts["executable"] = info.get("CFBundleExecutable", "")
        facts["permissions"] = sorted(k for k in info if k.endswith("UsageDescription"))
        facts["url-schemes"] = sorted(
            scheme
            for entry in info.get("CFBundleURLTypes", [])
            for scheme in entry.get("CFBundleURLSchemes", [])
        )
        xcent = [n for n in names if n.endswith(".xcent")]
        if xcent:
            try:
                facts["entitlements"] = plistlib.loads(z.read(xcent[0]))
            except Exception as e:  # noqa: BLE001 - a malformed plist is a fact about the package
                facts["entitlements-note"] = f"unreadable: {e}"
    return facts


def cmd_inspect(args: argparse.Namespace) -> int:
    """Record what a package contains: every file with its digest, plus the identity and
    permissions from its manifest.

    The package is opened as a zip and its manifest read with aapt2 or plistlib. Nothing in it is
    executed.
    """
    package = Path(args.package)
    if not package.is_file():
        Problem(package.name, "no such package").emit()
        return 1
    fmt = package.suffix.lstrip(".").lower()
    entries = package_entries(package)
    executables = [
        e["path"]
        for e in entries
        if e["path"].endswith((".so", ".dylib"))
        or re.fullmatch(r"Payload/[^/]+\.app/[^/.]+", e["path"])
    ]
    if fmt == "apk":
        facts = android_facts(package)
    elif fmt == "aab":
        # An .aab stores its manifest as protobuf, which needs bundletool. The .apk from the same
        # pack run carries the same manifest in a form aapt2 reads, so the facts come from there.
        facts = android_facts(Path(args.sibling)) if args.sibling else {
            "source": None,
            "note": "no sibling .apk was given, so no manifest facts were read",
        }
    elif fmt == "ipa":
        facts = apple_facts(package)
    else:
        Problem(package.name, f"unknown package format {fmt!r}").emit()
        return 1

    report = {
        "package": package.name,
        "format": fmt,
        "size": package.stat().st_size,
        "sha256": file_digest(package),
        "entries": entries,
        "executables": executables,
        "facts": facts,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"inspected {package.name}: {len(entries)} file(s), {len(executables)} executable(s), "
        f"{len(facts.get('permissions', []))} permission(s)"
    )
    summary = [
        f"### {package.name}",
        "",
        "| | |",
        "|---|---|",
        f"| format | {fmt} |",
        f"| size | {report['size']:,} bytes |",
        f"| sha256 | `{report['sha256']}` |",
        f"| files | {len(entries)} |",
        f"| identity | `{facts.get('id', '?')}` {facts.get('version', '?')} ({facts.get('build', '?')}) |",
        f"| permissions | {', '.join(facts.get('permissions', [])) or 'none'} |",
    ]
    write_summary(summary)
    return 0


def write_summary(lines: list[str]) -> None:
    """Append to the run summary, or print to the terminal when there is none."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")
    else:
        print(text)


# --------------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------------


def build_tools(package: Path) -> dict[str, str]:
    """What built a package, from the `<package>.buildinfo.json` day writes beside it."""
    sidecar = package.with_name(package.name + ".buildinfo.json")
    if not sidecar.exists():
        return {}
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return {}
    tools = {
        str(tool.get("key", "")): str(tool.get("version", "")).strip()
        for tool in data.get("tools", [])
    }
    host = data.get("host") or {}
    if host:
        tools["host"] = f"{host.get('os', '?')} {host.get('arch', '?')}"
    return {key: value for key, value in tools.items() if key and value}


def toolchain_table(ours: dict[str, str], theirs: dict[str, str]) -> tuple[list[str], list[str]]:
    """The two builds' tools side by side, and the names of the ones that disagree.

    A different Xcode, NDK or rustc is the usual reason two builds of one commit differ, and it
    is the first thing a reviewer needs to see when they do.
    """
    if not ours and not theirs:
        return [], []
    differing = [k for k in sorted(set(ours) | set(theirs)) if ours.get(k) != theirs.get(k)]
    rows = ["| tool | this build | the app's release |", "|---|---|---|"]
    for key in sorted(set(ours) | set(theirs)):
        mark = " ⚠️" if key in differing else ""
        rows.append(f"| `{key}`{mark} | {ours.get(key, '—')} | {theirs.get(key, '—')} |")
    return rows, differing


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare the queue's flavored payload with the base release, normalizing declared metadata."""
    import fnmatch
    from package_compare import describe_difference, identity, payload

    policy_raw = load_yaml(ROOT / "policy.yaml")
    ignore = list(policy_raw.get("ignore-in-comparison", []))
    expected_patterns = list(policy_raw.get("expected-differences", []))
    options = [args.metadata, args.reference_metadata, args.target]
    if any(options) and not all(options):
        Problem("compare", "--metadata, --reference-metadata and --target must be supplied together").emit()
        return 1
    try:
        ours_meta = json.loads(Path(args.metadata).read_text()) if args.metadata else None
        theirs_meta = json.loads(Path(args.reference_metadata).read_text()) if args.reference_metadata else None
        ours_origins: dict[str, str] = {}
        theirs_origins: dict[str, str] = {}
        ours, normalized_ours = payload(
            Path(args.ours), ours_meta, args.target, find_aapt2(), ours_origins
        )
        theirs, normalized_theirs = payload(
            Path(args.theirs), theirs_meta, args.target, find_aapt2(), theirs_origins
        )
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        Problem("compare", str(error)).emit()
        return 1
    ours = {k: v for k, v in ours.items() if not any(fnmatch.fnmatch(k, p) for p in ignore)}
    theirs = {k: v for k, v in theirs.items() if not any(fnmatch.fnmatch(k, p) for p in ignore)}

    only_ours = sorted(set(ours) - set(theirs))
    only_theirs = sorted(set(theirs) - set(ours))
    changed = sorted(k for k in set(ours) & set(theirs) if ours[k] != theirs[k])
    same = len(set(ours) & set(theirs)) - len(changed)

    # The flavor's display name is compiled in, so the app's own binary cannot match the base
    # release. policy.yaml names those paths; everything else still has to agree.
    def expected(path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in expected_patterns)

    allowed = sorted(p for p in changed + only_ours + only_theirs if expected(p))
    changed = [p for p in changed if not expected(p)]
    only_ours = [p for p in only_ours if not expected(p)]
    only_theirs = [p for p in only_theirs if not expected(p)]

    ours_tools = build_tools(Path(args.ours))
    theirs_tools = build_tools(Path(args.theirs))
    tool_rows, differing_tools = toolchain_table(ours_tools, theirs_tools)

    # What each difference is, read out of the file it is in. Without this a run says only that
    # a count of files disagree, which is not something anyone can act on.
    detail: dict[str, list[str]] = {}
    for path in changed[:20]:
        try:
            detail[path] = describe_difference(
                Path(args.ours), Path(args.theirs),
                ours_origins[path], theirs_origins[path],
                ours_meta and identity(ours_meta, args.target),
                theirs_meta and identity(theirs_meta, args.target),
            )
        except (KeyError, OSError, ValueError) as error:
            detail[path] = [f"could not be read: {error}"]

    result = {
        "ours": Path(args.ours).name,
        "theirs": Path(args.theirs).name,
        "normalized-or-excluded": {"ours": normalized_ours, "theirs": normalized_theirs},
        "identical": same,
        "changed": changed,
        "only-in-ours": only_ours,
        "only-in-theirs": only_theirs,
        "expected-differences": allowed,
        "why": detail,
        "tools": {"ours": ours_tools, "theirs": theirs_tools, "differing": differing_tools},
    }
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")

    differences = len(changed) + len(only_ours) + len(only_theirs)
    lines = [
        f"### Comparison: `{result['ours']}` against the release's `{result['theirs']}`",
        "",
        f"{same} file(s) identical, {len(changed)} differing, {len(only_ours)} only in this "
        f"build, {len(only_theirs)} only in the release.",
    ]
    if allowed:
        lines += [
            "",
            f"{len(allowed)} path(s) carry the flavor's own identity and are expected to differ: "
            + ", ".join(f"`{path}`" for path in allowed),
        ]
    if tool_rows:
        lines += ["", "#### How the two were built", ""] + tool_rows
        if differing_tools:
            lines += [
                "",
                "The two builds did not use the same "
                + ", ".join(f"`{tool}`" for tool in differing_tools)
                + ". Until that matches what the app's CI used, differences below follow from"
                " the toolchain rather than from the source.",
            ]
    if differences:
        lines += ["", "#### What differs", ""]
        for path in (changed + only_ours + only_theirs)[:20]:
            where = (
                " (only in this build)" if path in only_ours
                else " (only in the release)" if path in only_theirs else ""
            )
            lines.append(f"- `{path}`{where}")
            lines += [f"  - {line}" for line in detail.get(path, [])[:12]]
        if differences > 20:
            lines.append(f"- …and {differences - 20} more")
    write_summary(lines)

    print(
        f"compared {result['ours']}: {same} identical, {len(changed)} differing, "
        f"{len(only_ours)} extra here, {len(only_theirs)} extra there, "
        f"{len(allowed)} expected to differ"
    )
    for path in allowed:
        print(f"         expected difference: {path}")
    for tool in differing_tools:
        print(
            f"         tool mismatch: {tool} is "
            f"{ours_tools.get(tool, 'absent')!r} here and "
            f"{theirs_tools.get(tool, 'absent')!r} in the app's release"
        )
    for path in (changed + only_ours + only_theirs)[:20]:
        print(f"         differs: {path}")
        for line in detail.get(path, [])[:12]:
            print(f"                  {line}")
    if not differences:
        return 0
    if args.allow_mismatch:
        message = f"{differences} difference(s) from the maintainer's release, allowed by label"
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{message}")
        else:
            print(message, file=sys.stderr)
        return 0
    cause = (
        f"; the two builds used different {', '.join(differing_tools)}, which is the likely cause"
        if differing_tools
        else ""
    )
    Problem(
        result["ours"],
        f"{differences} file(s) differ from the same tag's release asset{cause}. "
        f"The run's summary and compare.json name each one and what differs inside it; a "
        f"maintainer can re-run with the override label once the cause is understood",
    ).emit()
    return 1


def select_release(directory: Path, metadata: dict, target: str) -> Path:
    from package_compare import identity
    app = identity(metadata, target)
    stem = app.get("artifact") or metadata["project"]["artifact"]
    ext = {"android-mdc": "aab", "ios-uikit": "ipa"}[target]
    suffixes = ["", "-unsigned"] if target == "ios-uikit" else [""]
    names = {f"{prefix}-{target}{suffix}.{ext}"
             for prefix in [stem, f"{stem}-{app['version']}"] for suffix in suffixes}
    matches = [p for p in directory.iterdir() if p.name in names and p.is_file()]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one base release package ({', '.join(sorted(names))}); found {len(matches)}")
    return matches[0]


def cmd_select_release(args: argparse.Namespace) -> int:
    try:
        print(select_release(Path(args.directory), json.loads(Path(args.metadata).read_text()), args.target))
        return 0
    except (ValueError, OSError) as error:
        Problem("release", str(error)).emit()
        return 1


# --------------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------------


def expected_permissions(metadata: dict, platform: str, app_id: str, policy_raw: dict) -> set[str]:
    """The permissions a package may ask for: the app's declared permissions mapped through day's
    catalog, its raw entries for this platform, and the framework baseline in policy.yaml."""
    catalog = {entry["name"]: entry for entry in metadata.get("permissionCatalog", [])}
    project = metadata.get("project", {})
    wanted: set[str] = set()
    for declared in project.get("permissions", []):
        name = declared if isinstance(declared, str) else declared.get("name", "")
        entry = catalog.get(name, {})
        for item in entry.get(platform, []):
            wanted.add(item["name"] if isinstance(item, dict) else str(item))
    for raw in project.get("rawPermissions", {}).get(platform, []) or []:
        wanted.add(raw["name"] if isinstance(raw, dict) else str(raw))
    for item in policy_raw.get("baseline-permissions", {}).get(platform, []) or []:
        wanted.add(str(item).replace("{id}", app_id))
    return wanted


def cmd_audit(args: argparse.Namespace) -> int:
    """Check a package against the submission, the app's manifest and its provenance.

    Reads the inspection report, the `day metadata --json` output for the app, and the sidecars
    packed beside the artifact.
    """
    policy = Policy.load()
    policy_raw = load_yaml(ROOT / "policy.yaml")
    report = json.loads(Path(args.report).read_text())
    metadata = json.loads(Path(args.metadata).read_text())
    app = next((a for a in catalog() if a.token == args.app), None)
    if app is None:
        Problem("apps", f"no submission named {args.app!r} in apps/").emit()
        return 1
    rel = relative(app.path)
    problems: list[Problem] = []

    def bad(message: str) -> None:
        problems.append(Problem(rel, message))

    facts = report.get("facts", {})
    project = metadata.get("project", {})
    resolved = project.get("resolved", {}).get(args.target, {}) if args.target else {}
    expected_id = resolved.get("id") or project.get("id", "")
    expected_version = str(resolved.get("version") or project.get("version", ""))
    expected_build = str(resolved.get("build") or project.get("build", ""))

    # 1. The package is the app the submission names, in the version the tag names.
    if facts.get("id") and facts["id"] != expected_id:
        bad(f"the package is {facts['id']!r} while the manifest builds {expected_id!r}")
    if facts.get("version") and facts["version"] != expected_version:
        bad(f"the package is version {facts['version']} while the manifest says {expected_version}")
    if facts.get("build") and facts["build"] != expected_build:
        bad(f"the package is build {facts['build']} while the manifest says {expected_build}")
    if not facts.get("id"):
        bad(f"no manifest facts were read from the package ({facts.get('note', 'no reason given')})")

    # 2. It asks for nothing the manifest leaves undeclared.
    platform = {"android-mdc": "android", "ios-uikit": "ios"}.get(args.target, "")
    if platform:
        allowed = expected_permissions(metadata, platform, expected_id, policy_raw)
        for permission in facts.get("permissions", []):
            if permission not in allowed:
                bad(
                    f"the package asks for {permission}, which the app's manifest does not declare "
                    f"(declared: {', '.join(sorted(allowed)) or 'none'})"
                )

    # 3. The provenance beside the package. These sidecars come from the same untrusted stage as
    #    the package, so each claim is checked against something known here: the digest of the
    #    file, and the commit the submission pins.
    sidecars = Path(args.sidecars) if args.sidecars else None
    if sidecars and sidecars.is_dir():
        name = report["package"]
        buildinfo = sidecars / f"{name}.buildinfo.json"
        if not buildinfo.is_file():
            bad(f"no {name}.buildinfo.json beside the package")
        else:
            info = json.loads(buildinfo.read_text())
            listed = {a.get("name"): a.get("sha256") for a in info.get("artifacts", [])}
            if name not in listed:
                bad(f"{buildinfo.name} lists {', '.join(listed) or 'nothing'}, leaving out {name}")
            elif listed[name] != report["sha256"]:
                bad(f"{buildinfo.name} records a different digest for {name} than the file has")
            if info.get("target") and args.target and info["target"] != args.target:
                bad(f"{buildinfo.name} was written for {info['target']}, not {args.target}")
            if info.get("profile") and info["profile"] != "release":
                bad(f"{buildinfo.name} records a {info['profile']} build; a submission ships release")
            day_tool = next((t for t in info.get("tools", []) if t.get("key") == "day"), None)
            if day_tool:
                print(f"         built with day {day_tool.get('version', '?')}")

        cdx = sidecars / f"{name}.sbom-cdx.json"
        if not cdx.is_file():
            bad(f"no {name}.sbom-cdx.json beside the package")
        else:
            doc = json.loads(cdx.read_text())
            props = {
                str(p.get("name")): str(p.get("value"))
                for p in doc.get("metadata", {}).get("properties", [])
            }
            components = len(doc.get("components", []))
            if components == 0:
                bad(f"{cdx.name} lists no components")
            else:
                print(f"         {cdx.name}: {components} component(s)")
            commit = props.get("day:commit", "")
            if args.expect_commit and commit and commit[:12] != args.expect_commit[:12]:
                bad(
                    f"the build records commit {commit[:12]} while the submitted tag points at "
                    f"{args.expect_commit[:12]}"
                )
            if args.expect_commit and not commit:
                bad(f"{cdx.name} records no commit, so the build cannot be tied to the tag")
            if props.get("day:dirty") == "true":
                bad("the build came from a checkout with uncommitted changes")
            repo = props.get("day:repository", "")
            if repo and repo.rstrip("/") != app.repo_url:
                bad(f"the build records repository {repo}, and this app lives at {app.repo_url}")
            if props.get("day:app-id") and props["day:app-id"] != project.get("id", ""):
                bad(
                    f"the build records app id {props['day:app-id']}, and the manifest builds "
                    f"{project.get('id', '')}"
                )

        spdx = sidecars / f"{name}.sbom-spdx.json"
        if not spdx.is_file():
            bad(f"no {name}.sbom-spdx.json beside the package")

    lines = [
        f"### Audit: {report['package']}",
        "",
        "| | |",
        "|---|---|",
        f"| identity | `{facts.get('id', '?')}` {facts.get('version', '?')} ({facts.get('build', '?')}) |",
        f"| expected | `{expected_id}` {expected_version} ({expected_build}) |",
        f"| permissions | {', '.join(facts.get('permissions', [])) or 'none'} |",
        f"| findings | {len(problems)} |",
    ]
    write_summary(lines)

    for problem in problems:
        problem.emit()
    if problems:
        return 1
    print(f"audited {report['package']}: {expected_id} {expected_version} ({expected_build})")
    return 0


# --------------------------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------------------------


def action_definition(uses: str, local_actions: Path | None) -> tuple[str, dict] | None:
    """The action.yml behind a `uses:`, from this repository or the one it names.

    The stages depend on those interfaces, so an input renamed in daybrite/actions should fail
    this check instead of a submission.
    """
    if uses.startswith("./"):
        # A reusable workflow rather than an action: `uses: ./.github/workflows/submit.yml`. Its
        # `workflow_call` inputs are the interface, and they are checked the same way.
        if uses.endswith((".yml", ".yaml")):
            path = ROOT / uses[2:]
            if not path.is_file():
                return None
            document = yaml.safe_load(path.read_text()) or {}
            # `on:` parses as the boolean True, which is YAML 1.1 for the bare word "on".
            triggers = document.get("on", document.get(True)) or {}
            return uses, {"inputs": (triggers.get("workflow_call") or {}).get("inputs") or {}}
        for name in ("action.yml", "action.yaml"):
            path = ROOT / uses[2:] / name
            if path.is_file():
                return uses, yaml.safe_load(path.read_text())
        return None
    if uses.startswith("daybrite/actions/"):
        rest = uses.split("@", 1)[0][len("daybrite/actions/"):]
        if local_actions:
            path = local_actions / rest / "action.yml"
            if path.is_file():
                return uses, yaml.safe_load(path.read_text())
            return None
        import urllib.request

        url = f"https://raw.githubusercontent.com/daybrite/actions/main/{rest}/action.yml"
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - a fixed URL
            return uses, yaml.safe_load(response.read().decode())
    return None


def cmd_wiring(args: argparse.Namespace) -> int:
    """Check every `uses:` in the workflows and actions against the action it names."""
    local_actions = Path(args.actions_dir) if args.actions_dir else None
    problems = 0
    checked = 0
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    # The composite actions call actions of their own, and those calls are the same dependency.
    files += sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
    for path in files:
        document = yaml.safe_load(path.read_text())
        calls: list[tuple[str, str, dict]] = []
        for name, job in (document.get("jobs") or {}).items():
            if "uses" in job:
                calls.append((name, str(job["uses"]), job.get("with") or {}))
            for step in job.get("steps") or []:
                if "uses" in step:
                    calls.append((name, str(step["uses"]), step.get("with") or {}))
        for step in (document.get("runs") or {}).get("steps") or []:
            if "uses" in step:
                calls.append((path.parent.name, str(step["uses"]), step.get("with") or {}))
        for job, uses, passed in calls:
            found = action_definition(uses, local_actions)
            if found is None:
                if uses.startswith("./"):
                    problems += 1
                    print(f"::error file={path}::{job} calls {uses}, which is not in this repository")
                continue
            checked += 1
            _, definition = found
            declared = definition.get("inputs") or {}
            for key in sorted(set(passed) - set(declared)):
                problems += 1
                print(f"::error file={path}::{job} passes {key!r} to {uses}, which does not declare it")
            for key, spec in declared.items():
                if spec.get("required") and "default" not in spec and key not in passed:
                    problems += 1
                    print(f"::error file={path}::{job} calls {uses} without its required {key!r}")
            print(f"ok   {path.name}:{job} → {uses} ({len(passed)} input(s))")
    print(f"\n{checked} call(s) checked, {problems} problem(s)")
    return 1 if problems else 0


# --------------------------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------------------------

GOOD = """
token: Faire-Games
title: Fair Games
tag: v1.9.0
commit: 026ae1d62a8c49b1b0793aed8b5a2a0064ba95b6
distribution:
  ios-uikit:
    - apple-app-store
  android-mdc:
    - google-play-store
"""

CASES: list[tuple[str, str, str]] = [
    # (what is wrong, the edit to GOOD, the text the message has to contain)
    ("a token that is not the file name", "token: Faire-Games|token: Fair-Games", "have to agree"),
    ("a branch where a tag belongs", "tag: v1.9.0|tag: main", "submit a released version"),
    (
        "a tag with no commit pinned to it",
        "commit: 026ae1d62a8c49b1b0793aed8b5a2a0064ba95b6\n|",
        "commit is required",
    ),
    (
        "a short commit",
        "commit: 026ae1d62a8c49b1b0793aed8b5a2a0064ba95b6|commit: 026ae1d",
        "full 40-character sha",
    ),
    (
        "a commit in capitals",
        "commit: 026ae1d62a8c49b1b0793aed8b5a2a0064ba95b6|commit: 026AE1D62A8C49B1B0793AED8B5A2A0064BA95B6",
        "lower case",
    ),
    (
        "a target nothing takes a package from",
        "  ios-uikit:\n    - apple-app-store|  macos-appkit:\n    - apple-app-store",
        "which no channel takes a package from",
    ),
    (
        "a channel the catalog does not know",
        "    - google-play-store|    - amazon-appstore",
        "not a channel this catalog knows",
    ),
    (
        "a channel that is still on the way",
        "    - google-play-store|    - f-droid",
        "on the way and cannot be published to yet",
    ),
    (
        "a channel under the wrong target",
        "  android-mdc:\n    - google-play-store|  android-mdc:\n    - apple-app-store",
        "takes a ios-uikit package",
    ),
    (
        "a target with no channel at all",
        "  android-mdc:\n    - google-play-store|  android-mdc: []",
        "must list at least one channel",
    ),
    ("nowhere to publish", "distribution:|distributions:", "distribution is required"),
    ("a misspelled key", "title: Fair Games|titel: Fair Games", "unknown key"),
    (
        "a submission choosing its own flavor",
        "title: Fair Games|title: Fair Games\nflavor: something-else",
        "unknown key",
    ),
    (
        "an id that is not an id",
        "title: Fair Games|title: Fair Games\nid: fair games",
        "not one the stores accept",
    ),
    (
        "a Play package name with a hyphen in it",
        "title: Fair Games|title: Fair Games\nandroid-id: org.appfair.app.Faire-Games",
        "not one the stores accept",
    ),
    (
        "an id outside the namespace the App Fair publishes in",
        "title: Fair Games|title: Fair Games\nid: com.example.games",
        "is outside",
    ),
    (
        "a title longer than the store takes",
        "title: Fair Games|title: Fair Games And Other Diversions Vol 2",
        "30",
    ),
    (
        "a setting a channel does not take",
        "title: Fair Games|title: Fair Games\ngoogle-play-store:\n  profile-secret: SOME_SECRET",
        "unknown setting",
    ),
    (
        "a secret's value where its name belongs",
        "title: Fair Games|title: Fair Games\napple-app-store:\n  profile-secret: MIIKmAIBAz",
        "NAME of a repository secret",
    ),
    (
        "settings for a channel this app does not use",
        "title: Fair Games|title: Fair Games\naltstore:\n  submit: true",
        "distribution does not send this app there",
    ),
]


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Run the validator over one good submission and one broken one per rule.

    Each case is a submission this repository could receive; the assertion is the message a
    maintainer would read.
    """
    import tempfile

    policy = Policy.load()
    failures = 0

    def check(name: str, text: str, want: str | None) -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Faire-Games.yaml"
            path.write_text(text)
            problems = validate_app(load_app(path), policy)
            messages = " ".join(p.message for p in problems)
            ok = not problems if want is None else any(want in p.message for p in problems)
            print(f"{'ok  ' if ok else 'FAIL'} {name}")
            if not ok:
                failures += 1
                print(f"     wanted {want!r}, got: {messages or '(no problems)'}")

    check("a valid submission", GOOD, None)
    for name, edit, want in CASES:
        old, new = edit.split("|", 1)
        old, new = old.replace("\\n", "\n"), new.replace("\\n", "\n")
        assert old in GOOD, f"selftest case {name!r} does not apply to the good submission"
        check(name, GOOD.replace(old, new), want)

    # The schema an editor reads and the rules this script applies describe one file, so they have
    # to name the same keys. They drift the first time one of them gains a key alone.
    schema = json.loads((ROOT / "schema" / "app.schema.json").read_text())
    documented = set(schema["properties"])
    known = TOP_LEVEL | set(policy.channels)
    for missing, where in (
        (known - documented, "schema/app.schema.json"),
        (documented - known, "policy.yaml or queue.py"),
    ):
        if missing:
            failures += 1
            print(f"FAIL {where} is missing: {', '.join(sorted(missing))}")
    if known == documented:
        print("ok   the schema, the rules and the channels describe the same keys")
    # Every channel the policy declares is a target this queue can build for and a set of
    # settings the schema documents.
    for name, channel in policy.channels.items():
        documented = set(schema["properties"][name]["properties"])
        if documented != channel.options:
            failures += 1
            print(f"FAIL {name}: the schema has {sorted(documented)}, the policy {sorted(channel.options)}")
        else:
            print(f"ok   {name} settings agree")
    targets = set(schema["properties"]["distribution"]["properties"])
    declared = {c.target for c in policy.channels.values()}
    if targets != declared:
        failures += 1
        print(f"FAIL distribution: the schema takes {sorted(targets)}, the channels {sorted(declared)}")
    else:
        print("ok   the schema takes the targets the channels name")

    # policy.yaml is read by the workflows and the actions as well as by this script, and a key
    # dropped from it fails in the middle of a submission. Name them here instead.
    raw = load_yaml(ROOT / "policy.yaml")
    required = [
        "id-namespace", "token-pattern", "tag-pattern", "commit-pattern", "channels",
        "day-version", "runners", "review", "ignore-in-comparison", "expected-differences", "mismatch",
        "override-label", "virus-scan", "baseline-permissions", "app-data-paths",
    ]
    missing = [key for key in required if key not in raw]
    if missing:
        failures += 1
        print(f"FAIL policy.yaml is missing: {', '.join(missing)}")
    else:
        print("ok   policy.yaml carries every key the workflows read")

    # Picking a release: the numbers decide, and a released version comes after a pre-release of
    # the same numbers. `add` and `update` write whatever this chooses into a submission.
    tags = ["v1.9.0", "v1.10.0", "v2.0.0-rc.1", "v2.0.0", "v0.1.0", "not-a-tag", "v10.0.0"]
    picked = newest_tag(tags, policy.tag_pattern)
    if picked == "v10.0.0":
        print("ok   the newest tag is picked by version order")
    else:
        failures += 1
        print(f"FAIL the newest of {tags} came out as {picked!r}")
    if newest_tag(["v2.0.0-rc.1", "v2.0.0"], policy.tag_pattern) == "v2.0.0":
        print("ok   a release comes after its own pre-release")
    else:
        failures += 1
        print("FAIL a pre-release was chosen over the release")
    if newest_tag(["main", "latest"], policy.tag_pattern) is None:
        print("ok   nothing to pick when no tag is a version")
    else:
        failures += 1
        print("FAIL a branch name was chosen as a release")

    # `update` rewrites two lines and leaves everything else, comments included, where it was.
    before = GOOD.replace("tag: v1.9.0", "tag: v1.9.0\n# a comment under the tag")
    after = re.sub(r"(?m)^tag:[ \t]*\S.*$", "tag: v2.0.0", before, count=1)
    after = re.sub(r"(?m)^commit:[ \t]*\S.*$", "commit: " + "a" * 40, after, count=1)
    if "# a comment under the tag" in after and "tag: v2.0.0" in after and "a" * 40 in after:
        print("ok   an update keeps the comments around what it changes")
    else:
        failures += 1
        print("FAIL an update lost the file around the two lines it changes")

    # Every target a channel takes a package from is built somewhere, and on the image the app's
    # own CI used, or the comparison reports a toolchain difference instead of a clean match.
    missing = [t for t in policy.buildable_targets if t not in policy.runners]
    if missing:
        failures += 1
        print(f"FAIL policy.yaml names no runner for: {', '.join(missing)}")
    else:
        print("ok   every buildable target names the runner it is built on")

    # Every ready channel needs both lanes: the one that submits, and the one a `submit: false`
    # submission falls back to.
    laneless = [
        name
        for name, channel in policy.ready_channels.items()
        if not channel.lane or not channel.hold_lane
    ]
    if laneless:
        failures += 1
        print(f"FAIL these channels are missing a lane: {', '.join(laneless)}")
    else:
        print("ok   every ready channel names the lane it submits with and the one it holds with")

    # The paths a flavor build cannot match: the app's own binary carries the display name day
    # compiles into it. They are reported and allowed; anything else still counts.
    import fnmatch as _fnmatch

    patterns = load_yaml(ROOT / "policy.yaml")["expected-differences"]
    allowed = ["base/lib/arm64-v8a/libdayapp.so", "Payload/App.app/<executable>"]
    refused = [
        "base/lib/arm64-v8a/libother.so", "base/dex/classes.dex", "base/assets/sounds/a.wav",
        "Payload/App.app/Info.plist", "Payload/App.app/assets/sounds/a.wav",
    ]
    wrong = [p for p in allowed if not any(_fnmatch.fnmatch(p, q) for q in patterns)]
    wrong += [p for p in refused if any(_fnmatch.fnmatch(p, q) for q in patterns)]
    if wrong:
        failures += 1
        print(f"FAIL expected-differences classifies these wrongly: {wrong}")
    else:
        print("ok   expected-differences covers the app's binary and nothing else")

    # The reviewer's comment: an update names both commits, a first submission says there is
    # nothing to compare against, and a highlighted path is one a reviewer should open.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Faire-Games.yaml"
        previous = yaml.safe_load(GOOD)
        proposed = GOOD.replace("tag: v1.9.0", "tag: v2.0.1")
        proposed = proposed.replace(str(previous["commit"]), "b" * 40)
        path.write_text(proposed)
        app = load_app(path)
        stats = {
            "commits": 4, "total_commits": 4, "files": ["build.rs", "src/main.rs"],
            "file_count": 2, "additions": 62, "deletions": 78, "status": "ahead", "behind_by": 0,
        }
        section = review_section(app, previous, policy, stats)
        wanted = [
            f"{previous['commit']}..." + "b" * 40,
            "`v1.9.0` → `v2.0.1`",
            "4 commits, 2 files, +62 −78",
            "`build.rs`",
        ]
        missing = [w for w in wanted if w not in section]
        if missing:
            failures += 1
            print(f"FAIL the reviewer's comment is missing: {missing}")
        elif "src/main.rs" in section:
            failures += 1
            print("FAIL the reviewer's comment lists a path policy.yaml does not highlight")
        else:
            print("ok   the reviewer's comment names both commits and the files to open first")
        first = review_section(app, None, policy, None)
        if "First submission" in first and "compare" not in first:
            print("ok   a first submission has nothing to compare against")
        else:
            failures += 1
            print("FAIL a first submission was given a comparison")
        same = review_section(app, app.data, policy, None)
        if "unchanged" in same:
            print("ok   a submission that keeps its commit says the source is unchanged")
        else:
            failures += 1
            print("FAIL a re-pinned submission claimed a source change")

    # The screenshots in the comment come from the release's index. A synthetic index stands in,
    # so this runs offline: its `listings` are derived from a test-only `store` position on each
    # entry, per platform and device kind, the way the day CLI resolves store/storefront.toml.
    PLATFORMS = {"ios": "ios-uikit", "android": "android-mdc", "linux": "linux-gtk"}

    def with_listings(index):
        per: dict = {}
        for e in index["screenshots"]:
            pos = e.get("store")
            if not isinstance(pos, int) or pos < 1:
                continue
            store = CHANNEL_TARGETS and {"ios-uikit": "apple-app-store", "android-mdc": "google-play-store"}.get(e["platform"])
            if not store:
                continue
            kinds = per.setdefault(e["platform"], {"website": {}, "stores": {}})["stores"].setdefault(store, {})
            item = {"shot": e["shot"], "theme": e.get("theme") or "light"}
            lst = kinds.setdefault(e.get("device") or "default", {}).setdefault(e.get("locale") or "default", [])
            while len(lst) < pos:
                lst.append(None)
            lst[pos - 1] = item
        for target in per.values():
            for kinds in target["stores"].values():
                for kind, by_locale in kinds.items():
                    for locale, lst in by_locale.items():
                        by_locale[locale] = [x for x in lst if x]
        return {**index, "listings": per}

    def shot(os_, device, theme, locale, name, store=1):
        return {"os": os_, "platform": PLATFORMS[os_], "device": device, "theme": theme, "locale": locale, "shot": name, "store": store,
                "title": name.title(), "url": f"https://example.test/App/gallery/{os_}/{device}/{theme}/{locale}/{name}.png"}
    index = with_listings({
        "site": "https://example.test/App", "generated": "2026-09-23T05:01:54Z",
        "locales": ["en", "fr"],
        "screenshots": [
            shot("android", "phone", "light", "en", "home"), shot("ios", "iphone", "light", "fr", "home"),
            shot("ios", "iphone", "light", "en", "home"), shot("ios", "iphone", "light", "en", "about", 2),
            shot("ios", "iphone", "dark", "en", "home", None),     # the other theme: not the listing's
            shot("linux", "desktop", "light", "en", "home"),  # not a store platform
        ],
    })
    section = screenshot_section(index, "https://example.test/App/gallery/gallery.json")
    headings = [line[len("<details><summary>"):line.index("</summary>")]
                for line in section.splitlines() if line.startswith("<details><summary>")]
    wanted = [
        "App Store listing (iPhone, English) · 2",
        "App Store listing (iPhone, French) · 1",
        "Google Play listing (Phone, English) · 1",
    ]
    if headings == wanted and "linux" not in section and '<img src="https://example.test/App/gallery/ios/iphone/light/en/about.png"' in section:
        print("ok   the comment groups the listing screenshots by store, device and language")
    else:
        failures += 1
        print(f"FAIL the screenshot groups came out as {headings}")
    tight = screenshot_section(index, "https://example.test/App/gallery/gallery.json", budget=400)
    if tight.count("Not inlined") >= 1 and "[open the gallery](https://example.test/App/fr/main/gallery/)" in tight and headings[0] in tight:
        print("ok   past the comment's size budget, other languages fold to a link and keep their heading")
    else:
        failures += 1
        print("FAIL folding for size did not keep the headings or link the gallery page")
    # The listing's screenshots: marked with `store: N`, validated against each store's sizes.
    def shot(os_, device, theme, locale, name, w, h, store=None):
        return {"os": os_, "platform": PLATFORMS[os_], "device": device, "theme": theme, "locale": locale, "shot": name, "store": store,
                "width": w, "height": h, "url": f"https://example.test/App/main/gallery/{os_}/{device}/{theme}/{locale}/{name}.png"}
    listing_index = with_listings({"locales": ["en", "fr"], "screenshots": [
        shot("ios", "iphone", "light", "en", "home", 1320, 2868, 1),
        shot("ios", "iphone", "dark", "en", "home", 1320, 2868),         # the other theme: not taken
        shot("ios", "iphone", "light", "en", "smoke", 1320, 2868),        # not declared: not taken
        shot("ios", "ipad", "light", "en", "home", 2752, 2064, 1),
        shot("android", "phone", "light", "en", "home", 1080, 1920, 1),
        shot("android", "tablet", "light", "en", "home", 1920, 1200, 1),
        shot("ios", "iphone", "light", "fr", "home", 1320, 2868, 1),
        shot("ios", "ipad", "light", "fr", "home", 2752, 2064, 1),
        shot("android", "phone", "light", "fr", "home", 1080, 1920, 1),
    ]})
    taken = listing_captures(listing_index, "ios-uikit", "apple-app-store") + listing_captures(listing_index, "android-mdc", "google-play-store")
    if len(taken) == 7 and all(s["theme"] == "light" and s["position"] == 1 for s in taken):
        print("ok   the listing takes the declared captures in the declared theme and no others")
    else:
        failures += 1
        print(f"FAIL the listing took {[(s['platform'], s['kind'], s['theme'], s['shot']) for s in taken]}")
    policy_raw = load_yaml(ROOT / "policy.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Faire-Games.yaml"
        path.write_text(GOOD)
        app = load_app(path)
        clean = screenshot_problems(app, listing_index, policy_raw, "apps/Faire-Games.yaml")
        if not clean:
            print("ok   a listing with every locale on every required kind, at the stores' sizes, passes")
        else:
            failures += 1
            print(f"FAIL a valid listing was refused: {[p.message for p in clean]}")
        # Play's API takes a 20:9 phone (2.22:1 is within its 2.3), refuses a taller one, and
        # refuses the halved CI tablet (800 px on the short side, under its 1080 floor).
        tall = {**listing_index, "screenshots": [
            dict(s, width=1080, height=2400) if s["device"] == "phone" else s for s in listing_index["screenshots"]]}
        taller = {**listing_index, "screenshots": [
            dict(s, width=1080, height=2500) if s["device"] == "phone" else s for s in listing_index["screenshots"]]}
        halved = {**listing_index, "screenshots": [
            dict(s, width=1280, height=800) if s["device"] == "tablet" else s for s in listing_index["screenshots"]]}
        ok_tall = not screenshot_problems(app, tall, policy_raw, "x")
        found = [p.message for p in screenshot_problems(app, taller, policy_raw, "x")]
        found_halved = [p.message for p in screenshot_problems(app, halved, policy_raw, "x")]
        if ok_tall and any("2.31:1" in m for m in found) and any("1280×800" in m and "Nexus 7 2013" in m for m in found_halved):
            print("ok   Play's enforced limits: a 20:9 phone passes, a taller one and a halved tablet are named with the fix")
        else:
            failures += 1
            print(f"FAIL the Play limits came out wrong: tall ok={ok_tall}, taller={found}, halved={found_halved}")
        # An iPhone size Apple does not list.
        odd = {**listing_index, "screenshots": [
            dict(s, width=1290, height=2778) if s["device"] == "iphone" else s for s in listing_index["screenshots"]]}
        found = [p.message for p in screenshot_problems(app, odd, policy_raw, "x")]
        if any("1290×2778" in m and "does not take" in m for m in found):
            print("ok   an iPhone capture at a size the App Store does not list is refused")
        else:
            failures += 1
            print(f"FAIL the odd iPhone size passed: {found}")
        # French has no iPad screenshot: App Store Connect refuses the version for that language.
        partial = {**listing_index, "screenshots": [
            s for s in listing_index["screenshots"] if not (s["device"] == "ipad" and s["locale"] == "fr")]}
        found = [p.message for p in screenshot_problems(app, partial, policy_raw, "x")]
        if any("no ipad screenshots for fr" in m for m in found):
            print("ok   a locale missing a required kind is named")
        else:
            failures += 1
            print(f"FAIL the missing French iPad set passed: {found}")
        # Nothing declared at all: the message says what to add, and where.
        found = [p.message for p in screenshot_problems(app, {**listing_index, "listings": {}}, policy_raw, "x")]
        if any("declares none" in m and "[storefront.ios-uikit.apple-app-store.screenshots]" in m for m in found):
            print("ok   an index with nothing declared is told what to declare")
        else:
            failures += 1
            print(f"FAIL an undeclared gallery passed: {found}")
        # The review shows the listing alone, in listing order, and names what it cannot show.
        unseen_index = {**listing_index, "screenshots": [
            ({k: v for k, v in s.items() if k != "url"} if s.get("locale") == "fr" else s)
            for s in listing_index["screenshots"]]}
        section = screenshot_section(unseen_index, "https://example.test/App/releases/download/v1/gallery.json")
        heads = [l[len("<details><summary>"):l.index("</summary>")] for l in section.splitlines() if l.startswith("<details><summary>")]
        if (heads[:2] == ["App Store listing (iPhone, English) · 1", "App Store listing (iPhone, French) · 1"]
                and "smoke.png" not in section and "/dark/" not in section
                and "release's [gallery.json](https://example.test/App/releases/download/v1/gallery.json)" in section
                and "3 of them named rather than shown" in section and "<code>home</code>" in section):
            print("ok   the review shows the listing's screenshots and nothing else")
        else:
            failures += 1
            print(f"FAIL the review's screenshot blocks were {heads}: {section[:300]}")

    # A release without the index: the message says what attaches it.
    nowhere = App(path=Path("apps/Nowhere.yaml"), data={"token": "Nowhere", "repo": "example/nowhere"})
    missing, why = gallery_index(nowhere, "v0.0.0")
    if missing is None and "releases/download/v0.0.0/gallery.json" in why and "screenshots.zip" in why:
        print("ok   a release without gallery.json is told what the review needs")
    else:
        failures += 1
        print(f"FAIL the missing-index message was: {why}")

    # The comment links a capture only where the site serves that very file.
    release = {"screenshots": [
        {"path": "gallery/ios-uikit/iphone/light/home.png", "sha256": "aa", "url": "https://example.test/App/gallery/x/home.png", "shot": "home"},
        {"path": "gallery/ios-uikit/iphone/light/play.png", "sha256": "bb", "url": "https://example.test/App/gallery/x/play.png", "shot": "play"},
        {"path": "gallery/ios-uikit/iphone/light/old.png", "sha256": "cc", "shot": "old"},
    ]}
    site = {"screenshots": [
        {"path": "gallery/ios-uikit/iphone/light/home.png", "sha256": "aa", "url": "https://example.test/App/main/gallery/x/home.png"},
        {"path": "gallery/ios-uikit/iphone/light/play.png", "sha256": "b2", "url": "https://example.test/App/main/gallery/x/play.png"},
    ]}
    seen = [s.get("url") for s in viewable(release, site)["screenshots"]]
    if seen == ["https://example.test/App/main/gallery/x/home.png", None, None] and [s.get("url") for s in viewable(release, None)["screenshots"]] == [None, None, None]:
        print("ok   the review links the site's copy of a capture only when it is the release's file")
    else:
        failures += 1
        print(f"FAIL the viewable urls came out as {seen}")

    # The listing is cut out of the release's zip, file by file against the index's sha-256.
    import hashlib
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        good = b"home-png"
        entries = [
            {"os": "ios", "platform": "ios-uikit", "device": "iphone", "theme": "light", "locale": "en", "store": 1, "shot": "home",
             "path": "gallery/ios-uikit/iphone/light/home.png", "sha256": hashlib.sha256(good).hexdigest()},
            {"os": "ios", "platform": "ios-uikit", "device": "iphone", "theme": "dark", "locale": "en", "shot": "home",
             "path": "gallery/ios-uikit/iphone/dark/home.png", "sha256": "ignored"},
            {"os": "ios", "platform": "ios-uikit", "device": "iphone", "theme": "light", "locale": "en", "shot": "smoke",
             "path": "gallery/ios-uikit/iphone/light/smoke.png", "sha256": "ignored"},
        ]
        (temp_path / "gallery.json").write_text(json.dumps(with_listings({"screenshots": entries})))
        with zipfile.ZipFile(temp_path / "screenshots.zip", "w") as archive:
            archive.writestr("ios-uikit/iphone/light/home.png", good)
            archive.writestr("ios-uikit/iphone/dark/home.png", b"dark")
            archive.writestr("ios-uikit/iphone/light/smoke.png", b"smoke")
        out = temp_path / "listing"
        args = argparse.Namespace(gallery=str(temp_path / "gallery.json"), zip=str(temp_path / "screenshots.zip"), out=str(out))
        code = cmd_listing(args)
        files = sorted(str(f.relative_to(out)) for f in out.rglob("*") if f.is_file())
        if code == 0 and files == ["gallery.json", "ios-uikit/iphone/light/home.png"] and (out / "ios-uikit/iphone/light/home.png").read_bytes() == good:
            print("ok   the listing is cut out of the release's zip: the marked light captures and the index")
        else:
            failures += 1
            print(f"FAIL the listing extraction wrote {files} (exit {code})")
        with zipfile.ZipFile(temp_path / "screenshots.zip", "w") as archive:
            archive.writestr("ios-uikit/iphone/light/home.png", b"not the same bytes")
        was = os.environ.pop("GITHUB_ACTIONS", None)
        code = cmd_listing(args)
        if was is not None:
            os.environ["GITHUB_ACTIONS"] = was
        if code == 1:
            print("ok   a capture that is not the file the index describes is refused")
        else:
            failures += 1
            print("FAIL a zip that does not match its index was accepted")
    # The comment names the run's artifact, which holds every capture the stores receive.
    with_artifact = screenshot_section(listing_index, "https://example.test/x/gallery.json", artifact=("listing-App", "https://github.com/x/actions/runs/1/artifacts/2"))
    if "[`listing-App`](https://github.com/x/actions/runs/1/artifacts/2)" in with_artifact:
        print("ok   the review links the artifact holding the listing's captures")
    else:
        failures += 1
        print("FAIL the review did not link the listing artifact")

    # `screenshots: false` is a boolean, rides into every matrix row, and switches the review off.
    off = App(path=Path("apps/Off.yaml"), data={"token": "Off", "repo": "example/off", "tag": "v1.0.0", "commit": "0" * 40, "screenshots": False, "distribution": {"ios-uikit": ["apple-app-store"]}})
    typo = App(path=Path("apps/Typo.yaml"), data={**off.data, "screenshots": "no"})
    if any("screenshots takes true or false" in p.message for p in validate_app(typo, policy)) \
            and not any("screenshots" in p.message for p in validate_app(off, policy)):
        print("ok   screenshots takes a boolean")
    else:
        failures += 1
        print("FAIL the screenshots key was not validated as a boolean")
    NO_SCREENSHOTS["set"] = False
    on_row = matrix_entry(App(path=Path("apps/On.yaml"), data={**off.data, "token": "On"}), policy)
    off_row = matrix_entry(off, policy)
    NO_SCREENSHOTS["set"] = True
    forced_row = matrix_entry(App(path=Path("apps/On.yaml"), data={**off.data, "token": "On"}), policy)
    NO_SCREENSHOTS["set"] = False
    if on_row["screenshots"] is False and off_row["screenshots"] is False and forced_row["screenshots"] is False and matrix_entry(App(path=Path("apps/Yes.yaml"), data={**off.data, "token": "Yes", "screenshots": True}), policy)["screenshots"] is True:
        print("ok   the matrix rows say whether the stores' screenshots are replaced")
    else:
        failures += 1
        print(f"FAIL the screenshots flag on the rows came out as {on_row['screenshots']}, {off_row['screenshots']}, {forced_row['screenshots']}")
    if "not part of this submission" in listing_review(off, "0" * 40, argparse.Namespace(listing=None, artifacts=None)):
        print("ok   the review says when the screenshots are left alone")
    else:
        failures += 1
        print("FAIL the review did not say the screenshots are left alone")

    # The flavor follows this catalog's name, so a fork builds its own without editing anything.
    expected = ROOT.name.removesuffix("-apps")
    if policy.flavor == expected:
        print(f"ok   the flavor follows this catalog's name ({policy.flavor})")
    else:
        failures += 1
        print(f"FAIL the flavor is {policy.flavor!r} and this catalog is {ROOT.name!r}")

    # The real catalog passes its own rules, every time this runs.
    everything = catalog()
    for app in everything:
        problems = validate_app(app, policy, everything)
        print(f"{'ok  ' if not problems else 'FAIL'} apps/{app.path.name}")
        if problems:
            failures += 1
            for problem in problems:
                print(f"     {problem.message}")

    # Two submissions cannot share a title.
    with tempfile.TemporaryDirectory() as tmp:
        one = Path(tmp) / "Faire-Games.yaml"
        one.write_text(GOOD)
        two = Path(tmp) / "Other-Games.yaml"
        two.write_text(GOOD.replace("Faire-Games", "Other-Games"))
        problems = validate_app(load_app(one), policy, [load_app(one), load_app(two)])
        ok = any("already taken" in p.message for p in problems)
        print(f"{'ok  ' if ok else 'FAIL'} two submissions with one title")
        failures += 0 if ok else 1

    # The app's name is the default locale's `name` in the storefront file, TOML or YAML.
    toml_text = '[storefront.metadata]\nname = "Games Fair"\n[storefront.metadata.fr]\nname = "Jeux"\n'
    yaml_text = "storefront:\n  metadata:\n    name: Games Fair\n    fr: { name: Jeux }\n"
    ok = (
        listing_name(toml_text, False) == "Games Fair"
        and listing_name(yaml_text, True) == "Games Fair"
        and listing_name("[storefront.submission-info]\n", False) is None
        and listing_name("storefront: [", True) is None
    )
    print(f"{'ok  ' if ok else 'FAIL'} the listing name comes from [storefront.metadata]")
    failures += 0 if ok else 1

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check submissions against policy.yaml")
    p.add_argument("paths", nargs="*", help="apps/<token>.yaml files (default: all)")
    p.add_argument("--all", action="store_true", help="every submission in apps/")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="the build matrix for the submissions a change touches")
    p.add_argument("--changed-from", help="git ref to diff against (default: origin/main)")
    p.add_argument("--changed", nargs="*", help="explicit changed paths, in place of a git diff")
    p.add_argument("--app", help="one token, in place of a git diff")
    p.add_argument("--channel", help="publish to this channel alone, for a re-run")
    p.add_argument("--lane", help="run this fastlane lane instead of the channel's, with --channel")
    p.add_argument("--no-screenshots", action="store_true", help="leave the stores' screenshots alone this run")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("review", help="summarize the source changes a submission proposes")
    p.add_argument("--changed-from", help="git ref to diff against (default: origin/main)")
    p.add_argument("--changed", nargs="*", help="explicit changed paths, in place of a git diff")
    p.add_argument("--app", help="one token, in place of a git diff")
    p.add_argument("--out", help="where to write the comment (default: stdout)")
    p.add_argument("--offline", action="store_true", help="skip the counts GitHub would supply")
    p.add_argument("--listing", help="a directory of `listing-<token>/` trees `queue.py listing` wrote")
    p.add_argument("--artifacts", help="a JSON file mapping each token to its listing artifact's URL")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("listing", help="extract the listing's captures from a release's screenshots.zip")
    p.add_argument("--gallery", required=True, help="the release's gallery.json")
    p.add_argument("--zip", required=True, help="the release's screenshots.zip")
    p.add_argument("--out", required=True, help="the capture tree to write, with gallery.json at its root")
    p.set_defaults(func=cmd_listing)

    p = sub.add_parser("verify", help="check a submission against the app it points at")
    p.add_argument("--app", required=True, help="the app token")
    p.add_argument("--metadata", required=True, help="`day metadata --json` output")
    p.add_argument("--tag-commit", help="what the tag points at now, for the pin check")
    p.add_argument("--offline", action="store_true", help="skip the release's screenshot check")
    p.add_argument("--gallery", help="a local gallery.json, in place of the release's")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("add", help="write a new submission from what an app has released")
    p.add_argument("token", help="the app token, whose repository is <token>/<token>")
    p.add_argument("--tag", help="a particular release, instead of the latest")
    p.add_argument("--title", help="the app's name, instead of the one its listing carries")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="move a submission to what the app has released since")
    p.add_argument("token", help="the app token")
    p.add_argument("--tag", help="a particular release, instead of the latest")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("resolve", help="print the tag and commit lines for an app and tag")
    p.add_argument("--token", help="the app token, whose repository is <token>/<token>")
    p.add_argument("--repo", help="a repository URL, for an app that is not in the catalog yet")
    p.add_argument("--tag", required=True, help="the released tag")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("authorize", help="warn when a change comes from outside an app's maintainers")
    p.add_argument("apps", nargs="+", help="app tokens the change touches")
    p.add_argument("--actor", help="the GitHub account proposing the change")
    p.set_defaults(func=cmd_authorize)

    p = sub.add_parser("record", help="write a publication into state/published.json")
    p.add_argument("--app", default="")
    p.add_argument(
        "--matrix",
        default="",
        help="a plan's per-app matrix as JSON, recording every row in it instead of one --app",
    )
    p.add_argument("--tag")
    p.add_argument("--channels", help="comma-separated channel names this run published to")
    p.add_argument("--run-url")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("inspect", help="write down what a package contains")
    p.add_argument("--package", required=True, help="the .aab, .apk or .ipa to read")
    p.add_argument("--sibling", help="the .apk beside an .aab, whose manifest aapt2 can read")
    p.add_argument("--out", required=True, help="where to write the report")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("compare", help="compare this build against the maintainer's release")
    p.add_argument("--ours", required=True, help="the package this queue built")
    p.add_argument("--theirs", required=True, help="the package attached to the app's release")
    p.add_argument("--out", help="where to write the comparison")
    p.add_argument("--metadata", help="the queue flavor's day metadata JSON")
    p.add_argument("--reference-metadata", help="the base release's day metadata JSON")
    p.add_argument("--target", choices=["android-mdc", "ios-uikit"])

    p.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="report differences and pass, for a re-run a maintainer has labelled",
    )
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("select-release", help="select the base app's release package unambiguously")
    p.add_argument("--directory", required=True)
    p.add_argument("--metadata", required=True)
    p.add_argument("--target", required=True, choices=["android-mdc", "ios-uikit"])
    p.set_defaults(func=cmd_select_release)

    p = sub.add_parser("audit", help="hold a package against the submission and the manifest")
    p.add_argument("--app", required=True, help="the app token")
    p.add_argument("--report", required=True, help="the inspection report")
    p.add_argument("--metadata", required=True, help="`day metadata --json` for the app at the tag")
    p.add_argument("--target", help="the target the package was built for")
    p.add_argument("--sidecars", help="the directory holding the provenance and SBOM files")
    p.add_argument("--expect-commit", help="the commit the submitted tag points at")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("wiring", help="check every workflow against the actions it calls")
    p.add_argument(
        "--actions-dir",
        help="a local checkout of daybrite/actions' .github/actions, instead of fetching it",
    )
    p.set_defaults(func=cmd_wiring)

    p = sub.add_parser("selftest", help="run the validator's own cases")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

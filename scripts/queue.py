#!/usr/bin/env python3
"""The submission queue's one program: validate submissions, plan builds, verify what was built,
and record what was published.

Every workflow in this repository calls a subcommand here, so the rules live in one file that a
maintainer can read, run locally, and test:

    scripts/queue.py validate apps/Faire-Games.yaml   # or --all
    scripts/queue.py plan --changed-from origin/main  # the build matrix for a pull request
    scripts/queue.py verify --app Faire-Games --metadata day-metadata.json
    scripts/queue.py authorize Faire-Games --actor someone --base origin/main
    scripts/queue.py record --app Faire-Games --stores apple,play --run-url https://…
    scripts/queue.py selftest                         # the cases below, no network, no checkout

The only dependency is PyYAML, which every GitHub runner already carries. Errors print as GitHub
annotations when GITHUB_ACTIONS is set, and as plain lines on a terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - the message is the point
    sys.exit("scripts/queue.py needs PyYAML: pip install pyyaml (GitHub runners have it already)")

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"
STATE = ROOT / "state" / "published.json"

# The keys a submission may carry. Anything else is a typo, and a typo that parsed would be a rule
# nobody applied. A channel's own settings live under its name, and policy.yaml says which
# channels exist and which settings each of them takes.
TOP_LEVEL = {"token", "title", "tag", "commit", "distribution", "summary"}


def load_yaml(path: Path) -> dict:
    """One YAML document as a mapping. A file holding anything else raises, and the caller turns
    that into a problem against the file."""
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
    """The Day build flavor every submission is built with: this catalog's own name.

    `appfair-apps` builds each app's `appfair` flavor, and a fork called `gamesfair-apps` builds
    its `gamesfair` one, with nothing to edit. The identity an app is published under belongs to
    whoever publishes it, so the name of the queue is the right place for it to come from — and a
    submission cannot choose it.
    """
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    name = repository.split("/")[-1] if repository else ROOT.name
    return name.removesuffix("-apps") or name


@dataclass
class Channel:
    """One place an app can be published, as policy.yaml declares it."""

    name: str
    target: str
    lane: str
    submit_lane: str
    options: set[str]
    status: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass
class Policy:
    """policy.yaml: what the catalog accepts. Read once, passed everywhere."""

    id_namespace: str
    token_pattern: str
    tag_pattern: str
    commit_pattern: str
    channels: dict[str, Channel]
    flavor: str
    day_version: str
    default_ios_profile_secret: str

    @staticmethod
    def load(path: Path | None = None) -> "Policy":
        raw = load_yaml(path or ROOT / "policy.yaml")
        channels = {
            name: Channel(
                name=name,
                target=str(spec["target"]),
                lane=str(spec.get("lane", "")),
                submit_lane=str(spec.get("submit-lane", spec.get("lane", ""))),
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
            default_ios_profile_secret=raw.get(
                "default-ios-profile-secret", "DAY_IOS_PROFILE_B64"
            ),
        )

    @property
    def ready_channels(self) -> dict[str, Channel]:
        return {name: c for name, c in self.channels.items() if c.ready}

    @property
    def buildable_targets(self) -> list[str]:
        """The targets at least one channel takes a package from."""
        return sorted({c.target for c in self.ready_channels.values()})


@dataclass
class Problem:
    """One thing wrong with one submission, in the shape an annotation wants."""

    file: str
    message: str

    def emit(self) -> None:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error file={self.file}::{self.message}")
        else:
            print(f"{self.file}: {self.message}", file=sys.stderr)


@dataclass
class App:
    """One `apps/<token>.yaml`, parsed. [`validate_app`] decides whether it is usable."""

    path: Path
    data: dict
    problems: list[Problem] = field(default_factory=list)

    @property
    def token(self) -> str:
        return str(self.data.get("token", self.path.stem))

    @property
    def owner_repo(self) -> str:
        """`owner/name`, the shape every GitHub action wants.

        An App Fair app lives at `<token>/<token>`: the token is the name of its organization and
        of the repository inside it (https://appfair.org/docs/inclusion-criteria/#naming), so the
        submission says it once and nothing can disagree with it.
        """
        return f"{self.token}/{self.token}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner_repo}"


def relative(path: Path) -> str:
    """A path as an annotation wants it: relative to the repository when it is inside one, and
    left alone when it is somewhere else, such as a fixture under /tmp in the selftest."""
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
    """Every submission in the repository, for the checks that compare apps against each other."""
    paths = sorted(list(APPS.glob("*.yaml")) + list(APPS.glob("*.yml")))
    return [load_app(p) for p in paths]


# --------------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------------


def validate_app(app: App, policy: Policy, others: list[App] | None = None) -> list[Problem]:
    """Everything that can be checked from inside this repository.

    Whether the app at that tag is really that app is a question for `verify`, which reads the
    answer out of the app's own manifest once the tag is checked out.
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

    token = data.get("token")
    if not isinstance(token, str) or not token:
        bad("token is required: the app's GitHub organization and repository name")
        token = ""
    elif not re.match(policy.token_pattern, token):
        bad(f"token {token!r} does not match {policy.token_pattern}")
    if token and app.path.stem != token:
        bad(f"the file is named {app.path.name} while its token is {token!r}; they have to agree")

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

    # Where the app goes, and what builds for it. The channels sit under the target whose package
    # they take, so the pairing is in the file: a channel under the wrong target is an error the
    # shape catches, and a target can feed more than one channel as the catalog grows.
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

    # A channel's own settings live under its name, and only the channels this app publishes to
    # may carry them: settings for a channel nobody uses are a rule that does nothing.
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

    # Two apps sharing a title would be two apps nobody can tell apart in the catalog, and the
    # stores refuse the second one anyway (https://appfair.org/docs/inclusion-criteria/#naming).
    for other in others or []:
        if other.path == app.path:
            continue
        if isinstance(title, str) and str(other.data.get("title", "")).lower() == title.lower():
            bad(f"title {title!r} is already taken by {other.path.name}")

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
        # A shallow clone without the base commit. A merge to the default branch changes exactly
        # what its last commit changed, which covers the case this fallback exists for.
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


def matrix_entry(app: App, policy: Policy) -> dict:
    """One row per app: what every stage needs to know about the submission itself."""
    pairs = app_channels(app, policy)
    return {
        "token": app.token,
        "title": app.data.get("title", app.token),
        "app_id": f"{policy.id_namespace}{app.token}",
        "repo": app.owner_repo,
        "tag": app.data.get("tag", ""),
        "commit": app.data.get("commit", ""),
        # Every app is built as this catalog's flavor, whose manifest carries the identity it is
        # published under. The app's own build belongs to its repository, and building it here
        # would double every submission.
        "flavor": policy.flavor,
        "flavors_only": True,
        "targets": ",".join(sorted({target for target, _ in pairs})),
        "channels": ",".join(channel.name for _, channel in pairs),
        "day_version": policy.day_version,
    }


def build_rows(entry: dict) -> list[dict]:
    """One row per target: a build, and the validation of what it produced."""
    return [
        dict(
            entry,
            target=target,
            runner="macos-15" if target == "ios-uikit" else "ubuntu-latest",
        )
        for target in entry["targets"].split(",")
        if target
    ]


def publish_rows(app: App, entry: dict, policy: Policy) -> list[dict]:
    """One row per channel: a signature and an upload, reading the package its target built."""
    rows = []
    for target, channel in app_channels(app, policy):
        settings = app.data.get(channel.name) or {}
        rows.append(
            dict(
                entry,
                target=target,
                runner="macos-15" if target == "ios-uikit" else "ubuntu-latest",
                channel=channel.name,
                # `upload` puts the build on the channel and stops there, which suits a queue
                # where publishing a binary and asking a store to review it are two decisions.
                # `submit: true` under the channel runs the lane that asks for review.
                lane=channel.submit_lane if settings.get("submit") else channel.lane,
                # Only a channel that signs with a provisioning profile carries one; the rest
                # leave the field empty rather than name a secret nothing reads.
                profile_secret=(
                    str(settings.get("profile-secret", policy.default_ios_profile_secret))
                    if "profile-secret" in channel.options
                    else ""
                ),
            )
        )
    return rows


def cmd_plan(args: argparse.Namespace) -> int:
    policy = Policy.load()
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
    builds = [row for entry in entries for row in build_rows(entry)]
    publishes = [
        row for app, entry in zip(apps, entries) for row in publish_rows(app, entry, policy)
    ]
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
    """Cross-check a submission against the app it points at, once that app is checked out.

    This is the half that metadata alone cannot answer: whether the tag builds the app this file
    claims, under the bundle id the App Fair publishes it as.
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
    expected_id = f"{policy.id_namespace}{app.token}"

    def bad(message: str) -> None:
        problems.append(Problem(rel, message))

    if resolved_id != expected_id:
        bad(
            f"{app.token} has to build under {expected_id!r}, and this tag builds {resolved_id!r}. "
            f"An App Fair build takes its identity from a Day flavor; see "
            f"https://appfair.org/docs/building/#bundle-id"
        )

    # The tag still points at the commit this submission was written against. A tag can be moved,
    # and a submission reviewed at one commit and published from another would be a review of
    # nothing. The stages check the commit out, so a moved tag changes no bytes; this says so out
    # loud, because a moved tag usually means the maintainer meant to submit something else.
    submitted = str(app.data.get("commit", ""))
    if args.tag_commit and submitted and args.tag_commit != submitted:
        bad(
            f"the tag now points at {args.tag_commit[:12]}, and this submission names "
            f"{submitted[:12]}. The build follows the commit; update `commit` when the tag was "
            f"moved on purpose, and ask the maintainer when it was not"
        )

    # The tag names the version: `v1.9.0` builds 1.9.0. Stores order releases by that number, so a
    # tag disagreeing with the manifest is a submission of something other than what it says.
    tag = str(app.data.get("tag", ""))
    if tag[1:] and version and not tag[1:].startswith(version):
        bad(f"tag {tag} disagrees with the version the app builds ({version})")

    declared = set(project.get("targets", []))
    for target in app.data.get("targets", []):
        if declared and target not in declared:
            bad(
                f"targets include {target}, which the app does not declare "
                f"({', '.join(sorted(declared))})"
            )

    flavor = metadata.get("flavor") or ""
    if flavor != policy.flavor:
        bad(
            f"the metadata was read with flavor {flavor!r}, and this catalog builds "
            f"{policy.flavor!r}; the workflow and policy.yaml disagree"
        )

    for problem in problems:
        problem.emit()
    if problems:
        return 1
    print(f"verified {app.token}: {resolved_id} {version} ({project.get('build')}) from {tag}")
    return 0


# --------------------------------------------------------------------------------------------
# resolve — the two lines a submission pins itself with
# --------------------------------------------------------------------------------------------


def cmd_resolve(args: argparse.Namespace) -> int:
    """Print the `tag` and `commit` lines for an app and tag.

    Every submission pins a commit, and this is where the value comes from, so nobody has to know
    that an annotated tag has to be peeled before its commit appears.
    """
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
# authorize
# --------------------------------------------------------------------------------------------


def cmd_authorize(args: argparse.Namespace) -> int:
    """Say whether the person proposing a change maintains the app it points at.

    Who maintains an app is a fact about that app's repository, so this asks GitHub rather than
    reading a list somebody typed here: a list would go stale the day a maintainer changed, and
    nothing in this repository could tell.

    It prints a warning and exits 0. The reviewer who merges decides, and there are good reasons
    for someone else to bump a tag: a maintainer stepping in, a security fix, an app changing
    hands. What this adds is that the change says so in the thread.
    """
    import json as _json
    import urllib.error
    import urllib.request

    actor = (args.actor or "").lstrip("@")

    def ask(url: str) -> int:
        """The status GitHub answers with, or 0 when the question could not be asked."""
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                return response.status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError:
            return 0

    for token_name in args.apps:
        app = next((a for a in catalog() if a.token == token_name), None)
        if app is None:
            Problem("apps", f"no submission named {token_name!r} in apps/").emit()
            continue
        owner, _, name = app.owner_repo.partition("/")
        if not actor:
            print(f"ok   {token_name}: no author to check")
            continue

        # An app kept under someone's own account has one maintainer, and the repository says so.
        if owner.lower() == actor.lower():
            print(f"ok   {token_name}: {actor} owns {app.owner_repo}")
            continue

        # Write access to the app's repository is the real answer, and only a token with that
        # access can read it. Public membership of the app's organization is the one a queue can
        # always ask about, so it is what this reports.
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
    """Write down what was published, so the catalog's state is a file in the repository that
    anybody can read."""
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
        "id": f"{policy.id_namespace}{app.token}",
        "repo": app.repo_url,
        "tag": args.tag or app.data.get("tag", ""),
        "channels": sorted(c for c in (args.channels or "").split(",") if c),
        "published": now,
        "run": args.run_url or "",
    }
    state["updated"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(f"recorded {app.token} {state['apps'][app.token]['tag']} → {STATE.relative_to(ROOT)}")
    return 0


# --------------------------------------------------------------------------------------------
# inspect — what is inside a package, without running any of it
# --------------------------------------------------------------------------------------------

SIGNATURE_PATHS = ("META-INF/", "_CodeSignature/", "embedded.mobileprovision", "CodeResources")


def package_entries(path: Path) -> list[dict]:
    """Every file inside a package, with its size and digest.

    A package is a zip — an .aab, an .apk and an .ipa all are — so this reads it as data and never
    asks the operating system to run anything it holds.
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
    """`aapt2` from the Android SDK, which reads an APK's binary manifest. It is a tool of the
    platform, and it reads the package as data."""
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
    """Write down what a package contains: every file with its digest, and the identity and
    permissions its manifest declares.

    Nothing here executes the package. It is opened as a zip, and its manifest is read by the
    platform's own tool (aapt2) or by Python's plist reader.
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
        # An .aab keeps its manifest in protobuf, which needs bundletool to read. The .apk from
        # the same pack run holds the same manifest in a form aapt2 reads, so the facts come from
        # there and say so.
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
    """Add to the run's summary page when there is one, and to the terminal when there is not."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")
    else:
        print(text)


# --------------------------------------------------------------------------------------------
# compare — the queue's build against the maintainer's release
# --------------------------------------------------------------------------------------------


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare two packages file by file, ignoring what signing writes.

    The App Fair builds the app itself and publishes that build. This asks the other question: a
    release the maintainer built from the same tag should contain the same files. Where the two
    differ, something about the build is unreproducible, and the run says which files.
    """
    import fnmatch

    policy_raw = load_yaml(ROOT / "policy.yaml")
    ignore = list(policy_raw.get("ignore-in-comparison", []))

    def ignored(path: str) -> bool:
        return path.startswith(SIGNATURE_PATHS) or any(fnmatch.fnmatch(path, p) for p in ignore)

    ours = {e["path"]: e["sha256"] for e in package_entries(Path(args.ours)) }
    theirs = {e["path"]: e["sha256"] for e in package_entries(Path(args.theirs))}
    ours = {k: v for k, v in ours.items() if not ignored(k)}
    theirs = {k: v for k, v in theirs.items() if not ignored(k)}

    only_ours = sorted(set(ours) - set(theirs))
    only_theirs = sorted(set(theirs) - set(ours))
    changed = sorted(k for k in set(ours) & set(theirs) if ours[k] != theirs[k])
    same = len(set(ours) & set(theirs)) - len(changed)

    result = {
        "ours": Path(args.ours).name,
        "theirs": Path(args.theirs).name,
        "identical": same,
        "changed": changed,
        "only-in-ours": only_ours,
        "only-in-theirs": only_theirs,
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
    if differences:
        listed = (changed + only_ours + only_theirs)[:20]
        lines += ["", "```text"] + listed + (["…"] if differences > 20 else []) + ["```"]
    write_summary(lines)

    print(
        f"compared {result['ours']}: {same} identical, {len(changed)} differing, "
        f"{len(only_ours)} extra here, {len(only_theirs)} extra there"
    )
    if not differences:
        return 0
    if args.allow_mismatch:
        message = f"{differences} difference(s) from the maintainer's release, allowed by label"
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{message}")
        else:
            print(message, file=sys.stderr)
        return 0
    Problem(
        result["ours"],
        f"{differences} file(s) differ from the same tag's release asset; a maintainer can "
        f"re-run with the override label once the cause is understood",
    ).emit()
    return 1


# --------------------------------------------------------------------------------------------
# audit — the package against what the submission and the app's manifest say
# --------------------------------------------------------------------------------------------


def expected_permissions(metadata: dict, platform: str, app_id: str, policy_raw: dict) -> set[str]:
    """What a package may ask for: the app's declared permissions mapped through day's own
    catalog, the raw entries it declares for this platform, and the baseline the framework itself
    adds (policy.yaml holds that list)."""
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
    """Hold a package against the submission, the app's manifest and its provenance.

    Everything here reads files: the inspection report from the previous step, the app's manifest
    as `day metadata --json` printed it, and the sidecars packed beside the artifact.
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
    expected_version = str(project.get("version", ""))
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

    # 3. The provenance beside it describes this submission. These sidecars come from the same
    #    untrusted stage as the package, so they are claims; what makes them worth reading is that
    #    they are checked against facts from elsewhere — the digest of the file in hand, and the
    #    commit the submitted tag resolves to, which this stage read from git itself.
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
# wiring — the workflows against the actions they call
# --------------------------------------------------------------------------------------------


def action_definition(uses: str, local_actions: Path | None) -> tuple[str, dict] | None:
    """The action.yml behind a `uses:`, from this repository or from the one it names.

    A queue whose stages are actions has their interfaces as its dependency: an input renamed in
    daybrite/actions would otherwise first appear as a failed submission.
    """
    if uses.startswith("./"):
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
    """Check every `uses:` in every workflow against the action it names."""
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
    ("a key nobody reads", "title: Fair Games|titel: Fair Games", "unknown key"),
    (
        "a submission choosing its own flavor",
        "title: Fair Games|title: Fair Games\nflavor: something-else",
        "unknown key",
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

    The queue cannot be tried out before a pull request exists, so its rules are exercised here
    instead. Every case is a submission this repository could receive, and the assertion is the
    message a maintainer would read.
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
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("verify", help="check a submission against the app it points at")
    p.add_argument("--app", required=True, help="the app token")
    p.add_argument("--metadata", required=True, help="`day metadata --json` output")
    p.add_argument("--tag-commit", help="what the tag points at now, for the pin check")
    p.set_defaults(func=cmd_verify)

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
    p.add_argument("--app", required=True)
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
    p.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="report differences and pass, for a re-run a maintainer has labelled",
    )
    p.set_defaults(func=cmd_compare)

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

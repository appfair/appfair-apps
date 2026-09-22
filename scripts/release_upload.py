#!/usr/bin/env python3
"""The catalog's signed packages, attached to the maintainer's release.

The App Fair builds an app from its released tag, signs it with the catalog's keys and publishes
it to the stores. These commands put the same signed packages on the app's own release, beside
the ones the maintainer published, named with the build flavor:

    games-fair-android-mdc.aab           the maintainer's, untouched
    games-fair-appfair-android-mdc.aab   the App Fair's, attached by `upload`

    release_upload.py check  --repo Games-Fair/Games-Fair [--tag v2.1.1]
    release_upload.py stage  --dir packages --flavor appfair --out release-assets
    release_upload.py upload --repo Games-Fair/Games-Fair --tag v2.1.1 --dir release-assets

`check` asks whether the account in policy.yaml can write to the repository and says what to
grant when it cannot. `stage` gathers the packages under the names they are published with, for
the workflow artifact every run leaves behind. `upload` attaches them and, once they are all
there, marks a pre-release as the latest release, so
`releases/latest/download/<name>` keeps answering between a tag and its publication.

An app that has granted nothing is a warning, not a failure: the run carries on and the staged
artifact is there to attach by hand. The token comes from APPFAIR_RELEASE_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TOKEN_ENV = "APPFAIR_RELEASE_TOKEN"

# What gets attached. The `.buildinfo.json` and `.sbom-*.json` sidecars beside these describe the
# unsigned package stage A built, so they stay where they are.
PACKAGES = (".aab", ".apk", ".ipa", ".dmg", ".pkg", ".hap", ".exe", ".msix", ".flatpak", ".appimage")


def emit(kind: str, message: str) -> None:
    """One line. A problem is also an Actions annotation; a progress line is not."""
    if kind in ("error", "warning") and os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}::{message}")
    if kind == "notice":
        print(message)
    else:
        print(f"{kind}: {message}", file=sys.stderr if kind == "error" else sys.stdout)


def policy() -> dict:
    raw = yaml.safe_load((ROOT / "policy.yaml").read_text()) or {}
    return raw.get("release-upload") or {}


def api(method: str, url: str, token: str, data: bytes | None = None, content_type: str = "") -> tuple[int, object]:
    """One GitHub call. Returns the status and the decoded body, or `(0, …)` when it could not be
    made at all."""
    request = urllib.request.Request(url, method=method, data=data)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            body = response.read()
            try:
                return response.status, json.loads(body)
            except ValueError:
                return response.status, None
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except (ValueError, OSError):
            return error.code, None
    except OSError as error:
        return 0, {"message": str(error)}


def grant_text(account: str) -> str:
    """What the maintainer does to let the catalog attach its packages."""
    return (
        f"Settings -> Collaborators and teams -> Add people -> {account} -> Write, or add a team "
        "from the appfair organization with that role. It lets the App Fair attach its signed "
        "packages beside yours; nothing of yours is replaced or removed."
    )


def access(repo: str, token: str, account: str) -> tuple[bool, str]:
    """Whether the token can write to `repo`, and what to say when it cannot."""
    status, body = api("GET", f"{API}/repos/{repo}", token)
    if status == 0:
        return False, f"GitHub could not be reached: {(body or {}).get('message', 'no answer')}."
    if status == 404:
        return False, (
            f"{repo} is not visible to the App Fair's account ({account}), so its release cannot "
            f"be written to. If the repository is private, grant the access: {grant_text(account)}"
        )
    if status != 200 or not isinstance(body, dict):
        return False, f"GitHub answered {status} for {repo}."
    if not (body.get("permissions") or {}).get("push"):
        return False, (
            f"the App Fair's account ({account}) cannot write to {repo}, so the signed packages "
            f"have nowhere to go. Grant it: {grant_text(account)}"
        )
    return True, f"{account} can write to {repo}."


def release(repo: str, tag: str, token: str) -> tuple[int, dict]:
    status, body = api("GET", f"{API}/repos/{repo}/releases/tags/{tag}", token)
    return status, body if isinstance(body, dict) else {}


def published_name(name: str) -> str:
    """The name the package is published under.

    `day pack --no-sign` marks an .ipa `-unsigned`, and stage C signs it; the asset carries one
    name whichever way the run went, as the app's own release CI also does.
    """
    return name.replace("-unsigned.ipa", ".ipa")


def resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    """The account, the token and what a missing grant does, from policy.yaml and the flags."""
    conf = policy()
    account = args.account or str(conf.get("account") or "")
    severity = getattr(args, "missing_access", "") or str(conf.get("on-missing-access") or "warn")
    return account, os.environ.get(TOKEN_ENV, ""), "error" if severity == "error" else "warning"


def writable(repo: str, account: str, token: str) -> tuple[bool, str]:
    """Whether the catalog can write to `repo`'s releases, and why not when it cannot."""
    if not token:
        return False, (
            f"no {TOKEN_ENV} in this job, so the App Fair's write access to {repo} could not be "
            "checked."
        )
    status, who = api("GET", f"{API}/user", token)
    login = (who or {}).get("login", "") if isinstance(who, dict) else ""
    if status != 200:
        return False, f"{TOKEN_ENV} was refused by GitHub ({status}); it is expired or not a token."
    if account and login.lower() != account.lower():
        return False, (
            f"{TOKEN_ENV} authenticates as {login!r}, and policy.yaml names {account!r} as the "
            "account apps grant access to; either the token or policy.yaml is wrong"
        )
    return access(repo, token, account or login)


def cmd_check(args: argparse.Namespace) -> int:
    account, token, severity = resolve(args)
    allowed, message = writable(args.repo, account, token)
    if not allowed:
        emit(
            severity,
            f"{message} The catalog publishes either way; its signed packages are left as a "
            "workflow artifact instead of going on the release.",
        )
        return 1 if severity == "error" else 0
    emit("notice", f"ok   {message}")

    if args.tag:
        status, body = release(args.repo, args.tag, token)
        if status != 200:
            emit(severity, f"{args.repo} has no release for {args.tag} ({status}), so there is nothing to attach to")
            return 1 if severity == "error" else 0
        assets = len(body.get("assets") or [])
        state = "pre-release" if body.get("prerelease") else "released"
        emit("notice", f"ok   {args.repo} {args.tag} is {state} with {assets} asset(s)")
    return 0


def attachable(directory: Path, flavor: str) -> tuple[list[Path], list[str]]:
    """The packages to attach, and what was left out of `directory` and why."""
    skipped: list[str] = []
    found: list[Path] = []
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        if path.suffix.lower() not in PACKAGES:
            continue
        # The rail: an asset the catalog attaches carries the flavor, so it can never take the
        # name of a package the maintainer published.
        if f"-{flavor}-" not in path.name:
            skipped.append(f"{path.name} (no -{flavor}- in its name)")
            continue
        found.append(path)
    return found, skipped


def cmd_stage(args: argparse.Namespace) -> int:
    """Copy the packages to attach into one directory, under their published names.

    Every run leaves this as a workflow artifact, so a release the catalog could not write to can
    still be filled in by hand from what it built.
    """
    source = Path(args.dir)
    out = Path(args.out)
    if not args.flavor:
        emit("notice", "this app builds no flavor, so the catalog has nothing of its own to stage")
        return 0
    if not source.is_dir():
        emit("error", f"{source} is not a directory, so there is nothing to stage")
        return 1
    found, skipped = attachable(source, args.flavor)
    for note in skipped:
        emit("notice", f"skipped {note}")
    out.mkdir(parents=True, exist_ok=True)
    for path in found:
        shutil.copy2(path, out / published_name(path.name))
        emit("notice", f"staged {published_name(path.name)}")
    if not found:
        emit(
            "notice",
            f"no package here carries -{args.flavor}-, so this app was built as it stands and the "
            "catalog has nothing of its own to publish",
        )
    return 0


def promote(repo: str, tag: str, token: str, body: dict, dry_run: bool) -> None:
    """Mark a pre-release as the latest release, now that its packages are all on it.

    `releases/latest/download/<name>` answers only for the latest release, so an app that tags a
    pre-release and waits for the catalog would have a dead link until this runs.
    """
    if not body.get("prerelease"):
        return
    if dry_run:
        emit("notice", f"would mark {tag} as the latest release")
        return
    status, _ = api(
        "PATCH",
        f"{API}/repos/{repo}/releases/{body.get('id')}",
        token,
        data=json.dumps({"prerelease": False, "make_latest": "true"}).encode(),
        content_type="application/json",
    )
    if status == 200:
        emit("notice", f"ok   {tag} is now the latest release")
    else:
        emit("warning", f"{tag} could not be marked as the latest release ({status})")


def cmd_upload(args: argparse.Namespace) -> int:
    account, token, severity = resolve(args)
    directory = Path(args.dir)

    if not args.flavor:
        emit("notice", f"{args.repo} builds no flavor, so the catalog has nothing of its own to attach")
        return 0
    if not directory.is_dir():
        emit("error", f"{directory} is not a directory, so there are no packages to attach")
        return 1
    found, skipped = attachable(directory, args.flavor)
    for note in skipped:
        emit("notice", f"skipped {note}")
    if not found:
        emit("notice", f"nothing in {directory} for the catalog to attach to {args.repo} {args.tag}")
        return 0

    allowed, why = writable(args.repo, account, token)
    if not allowed:
        emit(
            severity,
            f"{why} The {len(found)} signed package(s) are in this run's artifacts instead, to "
            f"attach to {args.repo} {args.tag} by hand.",
        )
        return 1 if severity == "error" else 0

    status, body = release(args.repo, args.tag, token)
    if status != 200:
        emit(severity, f"{args.repo} has no release for {args.tag} ({status})")
        return 1 if severity == "error" else 0
    existing = {str(asset.get("name")): asset for asset in body.get("assets") or []}
    release_id = body.get("id")

    attached: list[tuple[str, int]] = []
    for path in found:
        name = published_name(path.name)
        old = existing.get(name)
        if old is not None:
            uploader = ((old.get("uploader") or {}).get("login") or "").lower()
            if account and uploader != account.lower():
                emit(
                    severity,
                    f"{name} is already on {args.tag} and was uploaded by {uploader or 'someone else'}; "
                    "the App Fair replaces only its own assets",
                )
                return 1 if severity == "error" else 0
            if args.dry_run:
                emit("notice", f"would replace {name}")
            else:
                code, _ = api("DELETE", f"{API}/repos/{args.repo}/releases/assets/{old.get('id')}", token)
                if code not in (204, 200):
                    emit(severity, f"{name} could not be removed before it was replaced ({code})")
                    return 1 if severity == "error" else 0
        if args.dry_run:
            emit("notice", f"would attach {name} ({path.stat().st_size} bytes)")
            attached.append((name, path.stat().st_size))
            continue
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        code, answer = api(
            "POST",
            f"{UPLOADS}/repos/{args.repo}/releases/{release_id}/assets?name={name}",
            token,
            data=path.read_bytes(),
            content_type=mime,
        )
        if code not in (200, 201):
            message = (answer or {}).get("message", "") if isinstance(answer, dict) else ""
            emit(severity, f"{name} was not attached to {args.tag} ({code}) {message}".strip())
            return 1 if severity == "error" else 0
        attached.append((name, path.stat().st_size))
        emit("notice", f"ok   attached {name}")

    # Every package is on the release, so a pre-release can become the one `latest` points at.
    if policy().get("promote-prerelease", True):
        promote(args.repo, args.tag, token, body, args.dry_run)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"### Attached to {args.repo} {args.tag}", "", "| asset | size |", "|---|---|"]
        lines += [f"| `{name}` | {size} |" for name, size in attached]
        Path(summary).open("a").write("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_upload.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    remote = argparse.ArgumentParser(add_help=False)
    remote.add_argument("--repo", required=True, help="the app's repository, as owner/name")
    remote.add_argument("--account", default="", help="the account that uploads (default: policy.yaml)")
    remote.add_argument(
        "--missing-access",
        choices=["warn", "error"],
        default="",
        help="what a repository that granted nothing does (default: policy.yaml)",
    )

    check = sub.add_parser("check", parents=[remote], help="can the catalog write to this release?")
    check.add_argument("--tag", default="", help="also check that this tag is released")
    check.set_defaults(func=cmd_check)

    stage = sub.add_parser("stage", help="gather the packages under their published names")
    stage.add_argument("--dir", default="packages", help="the directory the build left them in")
    stage.add_argument("--out", default="release-assets", help="where to gather them")
    stage.add_argument("--flavor", default="", help="the build flavor their names carry")
    stage.set_defaults(func=cmd_stage)

    upload = sub.add_parser("upload", parents=[remote], help="attach the signed packages")
    upload.add_argument("--tag", required=True, help="the released tag they belong to")
    upload.add_argument("--dir", default="release-assets", help="the directory holding them")
    upload.add_argument("--flavor", default="", help="the build flavor their names carry")
    upload.add_argument("--dry-run", action="store_true", help="say what would be attached")
    upload.set_defaults(func=cmd_upload)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

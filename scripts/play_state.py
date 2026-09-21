"""Ask Google Play what became of a submission, and judge it.

`supply` reports what it sent, not what the console now holds. This reads the track a publish was
meant to reach and fails when the version code is not in it, or is sitting there as a draft after
a lane that was supposed to submit it.

The service-account key comes from the environment (APPFAIR_PLAY_KEY_PATH) and is never printed.
An edit is opened to read the track and deleted again; an edit that is never committed changes
nothing, which is how the API is read.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# A release Google has been asked to review and distribute. `draft` is a release parked in the
# console; `halted` is one that was stopped.
LIVE = {"completed", "inProgress"}


def access_token(service_account: dict) -> str:
    """An OAuth2 token for the Play API, from the service account's RS256 key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    def segment(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": service_account["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 600,
    }
    signing_input = segment(json.dumps(header).encode()) + b"." + segment(json.dumps(claims).encode())
    key = load_pem_private_key(service_account["private_key"].encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = (signing_input + b"." + segment(signature)).decode()

    body = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read())["access_token"]


def call(token: str, path: str, method: str = "GET") -> dict:
    """One call to the Play API. A failure is reported rather than raised past the caller."""
    request = urllib.request.Request(API + path, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Length", "0")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        return {"error": error.code, "detail": error.read()[:400].decode("utf8", "replace")}
    except OSError as error:
        return {"error": 0, "detail": str(error)}


def report(heading: list[str], lines: list[str], problems: list[str]) -> None:
    """The same story in the run's log and in its summary."""
    body = heading + [f"- {line}" for line in lines]
    if problems:
        body += ["", "**This is not ready to publish:**", ""] + [f"- {p}" for p in problems]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write("\n".join(body) + "\n")
    for line in lines:
        print(f"         {line}")
    for problem in problems:
        print(f"::error::{problem}")


def judge(track: dict | None, version_code: str, track_name: str, submitted: bool):
    """Whether the track holds this build, and in the state the lane was supposed to leave it.

    Pure, so the cases below can be checked without a network.
    """
    lines, problems = [], []
    if track is None or "error" in (track or {}):
        problems.append(f"Google Play would not say what is in the {track_name} track: {track}")
        return False, lines, problems

    mine = None
    for release in track.get("releases", []):
        if str(version_code) in [str(code) for code in release.get("versionCodes", [])]:
            mine = release
            break

    if mine is None:
        codes = sorted(
            {str(c) for r in track.get("releases", []) for c in r.get("versionCodes", [])}
        )
        problems.append(
            f"version code {version_code} is not in the {track_name} track"
            + (f", which holds {', '.join(codes)}" if codes else ", which is empty")
        )
        return False, lines, problems

    status = mine.get("status", "?")
    lines.append(f"{track_name} track: version code {version_code} is {status}")
    if mine.get("name"):
        lines.append(f"release name: {mine['name']}")
    if submitted and status not in LIVE:
        problems.append(
            f"the release is {status} rather than {' or '.join(sorted(LIVE))}: the lane was "
            f"supposed to submit it for review and distribution"
        )
    return not problems, lines, problems


def unused_code(bundles: list[dict], version_code: str, tracks: dict[str, dict]):
    """Whether Play will take this version code, and where it already sits if not.

    Play version codes are used once for the whole package, so a release that repeats one is
    refused at upload with "Version code N has already been used". Pure, so the cases below can
    be checked without a network.
    """
    lines, problems = [], []
    used = sorted({str(b.get("versionCode")) for b in bundles})
    lines.append("version codes Play already holds: " + (", ".join(used) if used else "none"))
    if str(version_code) not in used:
        return True, lines, problems

    where = [
        f"the {name} track as a {release.get('status')} release"
        for name, track in tracks.items()
        for release in (track.get("releases") or [])
        if str(version_code) in [str(c) for c in release.get("versionCodes", [])]
    ]
    problems.append(
        f"version code {version_code} has already been uploaded"
        + (", and sits in " + " and ".join(where) if where else "")
        + ". Play takes each code once, so raise `build` in the app's flavor manifest and tag "
        "again"
    )
    return False, lines, problems


def preflight(token: str, package: str, version_code: str, tracks: tuple = ("production", "internal")):
    """Read what a submission would run into, before anything is signed or uploaded."""
    edit = call(token, f"/applications/{package}/edits", method="POST")
    if "error" in edit:
        return False, [], [f"Google Play refused an edit for {package}: {edit}"]
    try:
        bundles = call(token, f"/applications/{package}/edits/{edit['id']}/bundles")
        if "error" in bundles:
            return False, [], [f"Google Play would not list the bundles: {bundles}"]
        found = {}
        for name in tracks:
            track = call(token, f"/applications/{package}/edits/{edit['id']}/tracks/{name}")
            if "error" not in track:
                found[name] = track
    finally:
        call(token, f"/applications/{package}/edits/{edit['id']}", method="DELETE")
    return unused_code(bundles.get("bundles", []), version_code, found)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", help="the Play package name, or read it from --metadata")
    parser.add_argument("--version-code", help="the version code, such as 36")
    parser.add_argument("--metadata", help="`day metadata --json`, to take the code from")
    parser.add_argument("--target", default="android-mdc", help="the target `--metadata` resolves")
    parser.add_argument("--track", default="production", help="the track the lane published to")
    parser.add_argument("--expect", choices=["submitted", "uploaded"])
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="ask what a submission would run into, before anything is uploaded",
    )
    parser.add_argument("--attempts", type=int, default=6, help="Play takes a moment to settle")
    parser.add_argument("--delay", type=int, default=20, help="seconds between attempts")
    args = parser.parse_args(argv)

    if args.metadata:
        project = json.loads(Path(args.metadata).read_text())["project"]
        resolved = project.get("resolved", {}).get(args.target, {})
        args.version_code = args.version_code or str(resolved.get("build", project["build"]))
        # The Play package name is the target's own: an `[app.android] id` is how a bundle id
        # with a hyphen becomes a legal one.
        args.package = args.package or str(resolved.get("id", project["id"]))
    if not args.package:
        print("::error::play_state.py needs --package, or --metadata to read it from")
        return 1
    if not args.version_code:
        print("::error::play_state.py needs --version-code, or --metadata to read it from")
        return 1

    key_path = os.environ.get("APPFAIR_PLAY_KEY_PATH", "")
    if not (key_path and Path(key_path).is_file()):
        print("::error::no Play service-account key, so the submission cannot be confirmed")
        return 1
    if not (args.preflight or args.expect):
        print("::error::play_state.py needs --expect, or --preflight")
        return 1
    token = access_token(json.loads(Path(key_path).read_text()))

    if args.preflight:
        ok, lines, problems = preflight(token, args.package, args.version_code)
        report(["#### Google Play, before anything is uploaded", ""], lines, problems)
        return 0 if ok else 1

    ok, lines, problems = False, [], ["the track was never read"]
    for attempt in range(1, max(1, args.attempts) + 1):
        edit = call(token, f"/applications/{args.package}/edits", method="POST")
        if "error" in edit:
            print(f"::error::Google Play refused an edit for {args.package}: {edit}")
            return 1
        try:
            track = call(
                token, f"/applications/{args.package}/edits/{edit['id']}/tracks/{args.track}"
            )
        finally:
            # An edit that is never committed changes nothing, and this leaves none open.
            call(token, f"/applications/{args.package}/edits/{edit['id']}", method="DELETE")
        ok, lines, problems = judge(
            track, args.version_code, args.track, args.expect == "submitted"
        )
        if ok or attempt == args.attempts:
            break
        print(f"         not there yet, asking again in {args.delay}s ({attempt}/{args.attempts})")
        time.sleep(args.delay)

    report(["#### Google Play, after the lane ran", ""], lines, problems)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

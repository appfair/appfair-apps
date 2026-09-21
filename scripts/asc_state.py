"""Ask App Store Connect what became of a submission, and judge it.

An upload and a submission are different things. `deliver` can finish happily having sent a
binary while the version it belongs to sits in Prepare for Submission with no build attached,
which is what the App Store Connect web page shows: an empty version. This reads the two records
that decide whether a release is really on its way — the build and the version — and fails when
they do not say what the run claimed.

The key comes from the environment (APPFAIR_ASC_KEY_ID, APPFAIR_ASC_ISSUER, APPFAIR_ASC_KEY_PATH)
and is never printed. Nothing here writes to App Store Connect.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Where a version can stand and be considered submitted. Apple's own states, as the API spells
# them; a rejection or a developer-removed version is not a submission that worked.
SUBMITTED = {
    "WAITING_FOR_REVIEW",
    "IN_REVIEW",
    "PENDING_APPLE_RELEASE",
    "PENDING_DEVELOPER_RELEASE",
    "PROCESSING_FOR_APP_STORE",
    "READY_FOR_SALE",
    "ACCEPTED",
}


def jwt(key_id: str, issuer: str, private_key: str) -> str:
    """A ten-minute App Store Connect token, signed with the key's ES256."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    def segment(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {"iss": issuer, "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"}
    signing_input = segment(json.dumps(header).encode()) + b"." + segment(json.dumps(claims).encode())
    key = load_pem_private_key(private_key.encode(), password=None)
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return (signing_input + b"." + segment(signature)).decode()


def ask(token: str, path: str) -> dict:
    """One read from the API. A failure is reported rather than raised past the caller."""
    request = urllib.request.Request("https://api.appstoreconnect.apple.com" + path)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"error": error.code, "detail": error.read()[:400].decode("utf8", "replace")}
    except OSError as error:
        return {"error": 0, "detail": str(error)}


def judge(version: dict | None, builds: list[dict], build_number: str, submitted: bool):
    """Whether the records say what the run claimed, and the lines that explain it.

    `version` is the app store version record (with its attached build), `builds` the app's
    recent builds. Pure, so the cases below can be checked without a network.
    """
    lines, problems = [], []
    mine = next((b for b in builds if str(b["attributes"].get("version")) == str(build_number)), None)
    if mine is None:
        problems.append(
            f"App Store Connect has no build {build_number} for this app, so nothing was uploaded"
        )
    else:
        state = mine["attributes"].get("processingState")
        lines.append(f"build {build_number}: {state}")
        if state not in {"VALID", "PROCESSING"}:
            problems.append(f"build {build_number} is {state} rather than processing or valid")

    if version is None:
        problems.append("App Store Connect has no version record for this release")
        return not problems, lines, problems

    state = version["attributes"].get("appStoreState", "?")
    attached = (version.get("relationships", {}).get("build", {}) or {}).get("data")
    attached_number = None
    if attached and mine and attached.get("id") == mine.get("id"):
        attached_number = build_number
    lines.append(f"version {version['attributes'].get('versionString')}: {state}")
    lines.append(
        f"build attached to the version: {attached_number or ('another build' if attached else 'none')}"
    )

    if submitted:
        if state not in SUBMITTED:
            problems.append(
                f"the version is {state}: the lane submitted it, so it should be one of "
                + ", ".join(sorted(SUBMITTED))
            )
        if attached_number != build_number:
            problems.append(
                f"build {build_number} is not the one attached to the version, so the submission "
                f"is not this build"
            )
    elif attached_number != build_number:
        lines.append(
            "the lane uploaded without submitting, so the version carries no build yet — set "
            "`submit: true` under the channel to have a run finish the job"
        )
    return not problems, lines, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--version", help="the version string, such as 2.0.2")
    parser.add_argument("--build", help="the build number, such as 36")
    parser.add_argument("--metadata", help="`day metadata --json`, to take the numbers from")
    parser.add_argument("--target", default="ios-uikit", help="the target `--metadata` resolves")
    parser.add_argument(
        "--expect",
        choices=["submitted", "uploaded"],
        required=True,
        help="what the lane that just ran was supposed to achieve",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=6,
        help="how many times to ask before deciding; App Store Connect takes a moment to show a "
        "submission that has just been made",
    )
    parser.add_argument("--delay", type=int, default=20, help="seconds between attempts")
    args = parser.parse_args(argv)

    if args.metadata:
        project = json.loads(Path(args.metadata).read_text())["project"]
        resolved = project.get("resolved", {}).get(args.target, {})
        args.version = args.version or str(resolved.get("version", project["version"]))
        args.build = args.build or str(resolved.get("build", project["build"]))
    if not (args.version and args.build):
        print("::error::asc_state.py needs --version and --build, or --metadata to read them from")
        return 1

    key_id = os.environ.get("APPFAIR_ASC_KEY_ID", "")
    issuer = os.environ.get("APPFAIR_ASC_ISSUER", "")
    key_path = os.environ.get("APPFAIR_ASC_KEY_PATH", "")
    if not (key_id and issuer and key_path and Path(key_path).is_file()):
        print("::error::no App Store Connect key, so the submission cannot be confirmed")
        return 1

    token = jwt(key_id, issuer, Path(key_path).read_text())
    apps = ask(token, f"/v1/apps?filter[bundleId]={args.bundle_id}")
    if "error" in apps or not apps.get("data"):
        print(f"::error::App Store Connect does not know {args.bundle_id}: {apps}")
        return 1
    app = apps["data"][0]["id"]

    for attempt in range(1, max(1, args.attempts) + 1):
        versions = ask(
            token,
            f"/v1/apps/{app}/appStoreVersions?filter[versionString]={args.version}"
            f"&include=build&limit=1",
        )
        builds = ask(token, f"/v1/builds?filter[app]={app}&limit=10&sort=-uploadedDate")
        version = (versions.get("data") or [None])[0]
        ok, lines, problems = judge(
            version, builds.get("data", []), args.build, args.expect == "submitted"
        )
        if ok or attempt == args.attempts:
            break
        print(f"         not there yet, asking again in {args.delay}s ({attempt}/{args.attempts})")
        time.sleep(args.delay)

    report = [f"#### App Store Connect, after the lane ran", ""]
    report += [f"- {line}" for line in lines]
    if problems:
        report += ["", "**The submission did not finish:**", ""] + [f"- {p}" for p in problems]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write("\n".join(report) + "\n")
    for line in lines:
        print(f"         {line}")
    for problem in problems:
        print(f"::error::{problem}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

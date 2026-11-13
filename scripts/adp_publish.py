#!/usr/bin/env python3
"""The iOS marketplace's packages, attached to the app's own release.

When a notarized version is ready, the appfair.cloud marketplace sends `adp-available` with the
version's alternative distribution package: the zip holding manifest.json and signature, and a
short-lived download URL for each variant and delta. The marketplace keeps each file's key blob
itself; nothing here is secret. adp.yml then:

    adp_publish.py plan   --event "$GITHUB_EVENT_PATH"              which repository and release
    adp_publish.py fetch  --event "$GITHUB_EVENT_PATH" --out adp    download and check the files
    release_upload.py upload --repo … --tag … --dir adp --all-files --no-promote
    adp_publish.py notify --event "$GITHUB_EVENT_PATH" --repo … --tag …   tell the marketplace

and the marketplace starts redirecting iOS's downloads to the release assets. `ingest` asks the
marketplace to take a version in again, which sends a fresh `adp-available` here.

Every file is published as `x-adp-<appleVersionId>-<path with / as ->`, the name the marketplace
expects (appfair.cloud/api/src/ingest.ts); the `x-` sorts them after the app's own release
assets. manifest.json and signature are published byte for byte: iOS verifies the manifest against
Apple's signature.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
MARKETPLACE_API = "https://api.appfair.cloud"
AUDIENCE = "https://api.appfair.cloud"


def emit(kind: str, message: str) -> None:
    if kind in ("error", "warning") and os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}::{message}")
    else:
        print(message)


def payload(event_path: str) -> dict:
    """The `adp` object of a repository_dispatch event."""
    event = json.loads(Path(event_path).read_text())
    adp = (event.get("client_payload") or {}).get("adp")
    if not isinstance(adp, dict):
        raise SystemExit("the event carries no client_payload.adp")
    for key in ("appleItemId", "appleVersionId", "bundleId", "shortVersion", "zip", "files"):
        if not adp.get(key):
            raise SystemExit(f"client_payload.adp has no {key}")
    return adp


def listed(item: str) -> dict:
    """The marketplace.yaml entry for `item`, or exit."""
    apps = (yaml.safe_load((ROOT / "marketplace.yaml").read_text()) or {}).get("apps") or {}
    entry = apps.get(str(item))
    if not entry:
        raise SystemExit(f"Apple item {item} is not in marketplace.yaml, so it is not published")
    return entry


def tag_candidates(version: str) -> list[str]:
    v = version.strip()
    return [f"v{v}", v] if not v.startswith("v") else [v, v[1:]]


def http(url: str, token: str = "", data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("User-Agent", "appfair-apps-adp")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def find_release(repo: str, version: str, token: str, get=http) -> str | None:
    """The tag of `repo`'s release for `version`: `v1.2.3` or `1.2.3`."""
    for tag in tag_candidates(version):
        status, _ = get(f"{API}/repos/{repo}/releases/tags/{tag}", token)
        if status == 200:
            return tag
    return None


def cmd_plan(args: argparse.Namespace, get=http) -> int:
    adp = payload(args.event)
    entry = listed(adp["appleItemId"])
    if entry.get("bundle-id") != adp["bundleId"]:
        emit("error", f"{adp['bundleId']} is not {entry.get('title')}'s bundle id ({entry.get('bundle-id')})")
        return 1
    repo = entry["repository"]
    tag = find_release(repo, adp["shortVersion"], os.environ.get("GITHUB_TOKEN", ""), get)
    if not tag:
        emit(
            "error",
            f"{repo} has no release for {adp['shortVersion']} (tried {', '.join(tag_candidates(adp['shortVersion']))}). "
            "Publish the release, then run adp.yml again with this version.",
        )
        return 1
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [f"repo={repo}", f"tag={tag}", f"item={adp['appleItemId']}", f"version={adp['appleVersionId']}"]
    if out:
        with open(out, "a") as f:
            f.write("\n".join(lines) + "\n")
    emit("notice", f"{entry.get('title')} {adp['shortVersion']} ({adp.get('bundleVersion', '')}) → {repo} {tag}")
    return 0


#: What every package file's release asset name starts with, before the package version id.
PREFIX = "x-adp"


def asset_path(version: str, name: str) -> str:
    """`x-adp-<version>-variant-<id>.ipa` → `variant/<id>.ipa`, the path the manifest uses."""
    rest = name[len(f"{PREFIX}-{version}-"):]
    for folder in ("variant-", "delta-"):
        if rest.startswith(folder):
            return f"{folder[:-1]}/{rest[len(folder):]}"
    return rest


def expected(manifest: dict) -> dict[str, dict]:
    """Each variant's and delta's size and digests, by asset path."""
    out = {}
    for kind, details in (("variants", "variantDetails"), ("deltas", "deltaDetails")):
        for a in manifest.get(kind) or []:
            d = a.get(details) or {}
            digests = []
            for h in d.get("hashes") or []:
                if h.get("algorithm") == "sha256":
                    digests += (h.get("unencryptedChunkDigests") or []) + (h.get("encryptedChunkDigests") or [])
            out[a.get("assetPath", "")] = {"size": d.get("compressedSize"), "digests": digests}
    return out


def cmd_fetch(args: argparse.Namespace, get=http) -> int:
    adp = payload(args.event)
    version = adp["appleVersionId"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    status, body = get(adp["zip"])
    if status != 200:
        emit("error", f"the package zip did not download ({status}); its URL may have expired")
        return 1
    entries = zipfile.ZipFile(io.BytesIO(body))
    manifest_bytes = entries.read("manifest.json")
    manifest = json.loads(manifest_bytes)
    if str(manifest.get("appleVersionId")) != str(version) or str(manifest.get("appleItemId")) != str(adp["appleItemId"]):
        emit("error", "the package's manifest names a different app or version than the event")
        return 1
    checks = expected(manifest)
    for f in adp["files"]:
        name, source = f["name"], f["source"]
        if "/" in name or not name.startswith(f"{PREFIX}-{version}-"):
            emit("error", f"refusing the asset name {name}")
            return 1
        if source.startswith("zip:"):
            data = entries.read(source[4:])
        else:
            status, data = get(source)
            if status != 200:
                emit("error", f"{name} did not download ({status})")
                return 1
            want = checks.get(asset_path(version, name))
            if want is None:
                emit("error", f"{name} is not in the package's manifest")
                return 1
            if want["size"] and len(data) != want["size"]:
                emit("error", f"{name} is {len(data)} bytes; the manifest says {want['size']}")
                return 1
            digest = hashlib.sha256(data).hexdigest()
            if want["digests"] and len(want["digests"]) == 1 and digest not in want["digests"]:
                # A single chunk's digest is usually the file's; say so, but the manifest's own
                # signature, which iOS checks, is the authority.
                emit("warning", f"{name}'s SHA-256 is not the manifest's chunk digest")
        (out / name).write_bytes(data)
        emit("notice", f"fetched {name} ({len(data)} bytes)")
    return 0


def oidc_token() -> str:
    """This job's GitHub OIDC token for the marketplace (the job needs `id-token: write`)."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise SystemExit("no OIDC token: the job needs `permissions: id-token: write`")
    status, body = http(f"{url}&audience={AUDIENCE}", bearer)
    if status != 200:
        raise SystemExit(f"the OIDC token request failed ({status})")
    return json.loads(body)["value"]


def post(path: str, body: dict, token: str, send=http) -> tuple[int, bytes]:
    return send(
        f"{os.environ.get('MARKETPLACE_API', MARKETPLACE_API)}{path}",
        token,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


def cmd_notify(args: argparse.Namespace, send=http, token=oidc_token) -> int:
    adp = payload(args.event)
    status, body = post(
        "/v1/published",
        {"appleItemId": adp["appleItemId"], "appleVersionId": adp["appleVersionId"], "repo": args.repo, "tag": args.tag},
        token(),
        send,
    )
    if status != 200:
        emit("error", f"the marketplace did not accept the release ({status}): {body[:300].decode(errors='replace')}")
        return 1
    emit("notice", f"the marketplace now installs {adp['bundleId']} {adp['shortVersion']} from {args.repo} {args.tag}")
    return 0


def cmd_ingest(args: argparse.Namespace, send=http, token=oidc_token) -> int:
    status, body = post("/v1/ingest", {"alternativeDistributionPackageVersionId": args.version_id}, token(), send)
    if status != 200:
        emit("error", f"the marketplace did not ingest {args.version_id} ({status}): {body[:300].decode(errors='replace')}")
        return 1
    emit("notice", json.loads(body).get("queued", "queued"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adp_publish.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("plan", cmd_plan), ("fetch", cmd_fetch), ("notify", cmd_notify)):
        p = sub.add_parser(name)
        p.add_argument("--event", required=True, help="the repository_dispatch event (GITHUB_EVENT_PATH)")
        p.set_defaults(func=func)
        if name == "fetch":
            p.add_argument("--out", default="adp", help="where to put the files")
        if name == "notify":
            p.add_argument("--repo", required=True)
            p.add_argument("--tag", required=True)
    ingest = sub.add_parser("ingest", help="ask the marketplace to take a package version in again")
    ingest.add_argument("--version-id", required=True, help="the alternative distribution package version id")
    ingest.set_defaults(func=cmd_ingest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""The iOS marketplace's packages: which release they go to, and what is checked before they do.

The network is replaced by a recorder, so every case runs offline.
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import adp_publish as ap  # noqa: E402

VERSION = "869899373"
IPA = b"encrypted variant bytes"


def manifest(size=len(IPA), digest=hashlib.sha256(IPA).hexdigest()) -> bytes:
    return json.dumps(
        {
            "appleItemId": "1640618584",
            "appleVersionId": VERSION,
            "bundleId": "org.appfair.app.Net-Skip",
            "variants": [
                {
                    "publicId": "v1",
                    "assetPath": "variant/v1.ipa",
                    "variantDetails": {
                        "compressedSize": size,
                        "hashes": [{"algorithm": "sha256", "chunkSize": size, "unencryptedChunkDigests": [digest]}],
                    },
                }
            ],
        }
    ).encode()


def zip_bytes(man: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", man)
        z.writestr("signature", b"SIG")
    return buf.getvalue()


def event(dir_: Path, **over) -> str:
    adp = {
        "appleItemId": "1640618584",
        "appleVersionId": VERSION,
        "bundleId": "org.appfair.app.Net-Skip",
        "shortVersion": "2.0.0",
        "bundleVersion": "240",
        "zip": "https://cdn/adp.zip",
        "files": [
            {"name": f"adp-{VERSION}-manifest.json", "source": "zip:manifest.json"},
            {"name": f"adp-{VERSION}-signature", "source": "zip:signature"},
            {"name": f"adp-{VERSION}-variant-v1.ipa", "source": "https://cdn/v1.ipa"},
        ],
        **over,
    }
    path = dir_ / "event.json"
    path.write_text(json.dumps({"action": "adp-available", "client_payload": {"adp": adp}}))
    return str(path)


class Recorder:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, token="", data=None, headers=None):
        self.calls.append((url, token, data))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                return answer
        return 404, b""


class PlanTests(unittest.TestCase):
    def test_finds_the_release_by_its_v_tag_then_bare(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GITHUB_OUTPUT": f"{d}/out"}):
            get = Recorder({f"{ap.API}/repos/Net-Skip/Net-Skip/releases/tags/2.0.0": (200, b"{}")})
            self.assertEqual(ap.cmd_plan(Namespace(event=event(Path(d))), get), 0)
            out = Path(f"{d}/out").read_text()
            self.assertIn("repo=Net-Skip/Net-Skip", out)
            self.assertIn("tag=2.0.0", out)
            self.assertEqual([c[0].rsplit("/", 1)[1] for c in get.calls], ["v2.0.0", "2.0.0"])

    def test_refuses_unlisted_apps_and_wrong_bundle_ids(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                ap.cmd_plan(Namespace(event=event(Path(d), appleItemId="42")), Recorder({}))
            self.assertEqual(ap.cmd_plan(Namespace(event=event(Path(d), bundleId="com.evil")), Recorder({})), 1)

    def test_a_missing_release_fails_rather_than_creating_one(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ap.cmd_plan(Namespace(event=event(Path(d))), Recorder({})), 1)


class FetchTests(unittest.TestCase):
    def test_writes_every_file_under_its_asset_name_byte_for_byte(self):
        man = manifest()
        get = Recorder({"https://cdn/adp.zip": (200, zip_bytes(man)), "https://cdn/v1.ipa": (200, IPA)})
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "adp"
            self.assertEqual(ap.cmd_fetch(Namespace(event=event(Path(d)), out=str(out)), get), 0)
            self.assertEqual((out / f"adp-{VERSION}-manifest.json").read_bytes(), man)
            self.assertEqual((out / f"adp-{VERSION}-signature").read_bytes(), b"SIG")
            self.assertEqual((out / f"adp-{VERSION}-variant-v1.ipa").read_bytes(), IPA)

    def test_refuses_a_variant_of_the_wrong_size(self):
        get = Recorder({"https://cdn/adp.zip": (200, zip_bytes(manifest(size=999))), "https://cdn/v1.ipa": (200, IPA)})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ap.cmd_fetch(Namespace(event=event(Path(d)), out=f"{d}/adp"), get), 1)

    def test_refuses_a_manifest_for_another_version(self):
        man = manifest().replace(VERSION.encode(), b"1")
        get = Recorder({"https://cdn/adp.zip": (200, zip_bytes(man))})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ap.cmd_fetch(Namespace(event=event(Path(d)), out=f"{d}/adp"), get), 1)

    def test_refuses_names_outside_the_scheme(self):
        with tempfile.TemporaryDirectory() as d:
            ev = event(Path(d), files=[{"name": "../evil", "source": "zip:manifest.json"}])
            get = Recorder({"https://cdn/adp.zip": (200, zip_bytes(manifest()))})
            self.assertEqual(ap.cmd_fetch(Namespace(event=ev, out=f"{d}/adp"), get), 1)

    def test_asset_names_map_back_to_manifest_paths(self):
        self.assertEqual(ap.asset_path(VERSION, f"adp-{VERSION}-variant-v1.ipa"), "variant/v1.ipa")
        self.assertEqual(ap.asset_path(VERSION, f"adp-{VERSION}-delta-d1.ipa"), "delta/d1.ipa")
        self.assertEqual(ap.asset_path(VERSION, f"adp-{VERSION}-manifest.json"), "manifest.json")


class NotifyTests(unittest.TestCase):
    def test_tells_the_marketplace_with_the_oidc_token(self):
        send = Recorder({"https://api.appfair.cloud/v1/published": (200, b'{"available":true}')})
        with tempfile.TemporaryDirectory() as d:
            args = Namespace(event=event(Path(d)), repo="Net-Skip/Net-Skip", tag="2.0.0")
            self.assertEqual(ap.cmd_notify(args, send, token=lambda: "OIDC"), 0)
        url, token, data = send.calls[0]
        self.assertEqual(token, "OIDC")
        self.assertEqual(
            json.loads(data),
            {"appleItemId": "1640618584", "appleVersionId": VERSION, "repo": "Net-Skip/Net-Skip", "tag": "2.0.0"},
        )


class MarketplaceListTests(unittest.TestCase):
    def test_every_entry_names_a_repository_and_bundle_id(self):
        import yaml

        apps = yaml.safe_load((ROOT / "marketplace.yaml").read_text())["apps"]
        for item, entry in apps.items():
            self.assertTrue(item.isdigit(), item)
            self.assertRegex(entry["repository"], r"^[\w.-]+/[\w.-]+$")
            self.assertTrue(entry["bundle-id"])


if __name__ == "__main__":
    unittest.main()

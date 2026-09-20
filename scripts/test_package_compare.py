"""Regression cases for comparing a store flavor with the normal tagged release."""
import argparse
import contextlib
import importlib.util
import io
import json
import plistlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from package_compare import normalize_android_manifest, normalize_assets, payload, signing_file

spec = importlib.util.spec_from_file_location("appfair_queue", Path(__file__).with_name("queue.py"))
queue = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = queue
spec.loader.exec_module(queue)


def metadata(flavor=False):
    app = dict(id="org.appfair.app.Faire-Games" if flavor else "dev.daybrite.games",
               version="1.9.0" if flavor else "2.0.1", build=36 if flavor else 8,
               title="Fair Games" if flavor else "Day Games", name="day-games",
               artifact="fair-games" if flavor else "day-games",
               scheme="fairegames" if flavor else "daygames")
    android = {**app, "id": app["id"].replace("-", "_")}
    return {"flavor": "appfair" if flavor else None, "project": {
        **app, "targets": ["ios-uikit", "android-mdc"],
        "resolved": {"ios-uikit": app, "android-mdc": android}}}


def plist(meta):
    app = meta["project"]
    return plistlib.dumps({"CFBundleIdentifier": app["id"],
                          "CFBundleShortVersionString": app["version"],
                          "CFBundleVersion": str(app["build"]),
                          "CFBundleDisplayName": app["title"],
                          "CFBundleURLTypes": [{"CFBundleURLName": app["id"],
                                                "CFBundleURLSchemes": [app["scheme"]]}]})


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ipa(self, name, meta, changes=None):
        files = {"Payload/Game.app/Info.plist": plist(meta),
                 "Payload/Game.app/Game": b"native executable",
                 "Payload/Game.app/data.json": b'{"level": 1}',
                 "Payload/Game.app/AppIcon60x60@2x.png": meta["project"]["title"].encode(),
                 "Payload/Game.app/_CodeSignature/CodeResources": name.encode()}
        files.update(changes or {})
        path = self.root / name
        with zipfile.ZipFile(path, "w") as z:
            for filename, data in files.items():
                z.writestr(filename, data)
        return path

    def test_flavored_metadata_and_icons_match_base_but_executable_changes_do_not(self):
        base, flavor = metadata(), metadata(True)
        a, _ = payload(self.ipa("base.ipa", base), base, "ios-uikit")
        b, excluded = payload(self.ipa("flavor.ipa", flavor), flavor, "ios-uikit")
        self.assertEqual(a, b)
        self.assertIn("Payload/Game.app/_CodeSignature/CodeResources", excluded)
        for changed in ["Game", "data.json"]:
            bad, _ = payload(self.ipa("bad.ipa", flavor, {f"Payload/Game.app/{changed}": b"changed"}),
                             flavor, "ios-uikit")
            self.assertNotEqual(a, bad)

    def named_ipa(self, name, meta, bundle, executable, binary=b"native executable"):
        """An .ipa whose bundle and executable are named after the app, as Xcode writes them."""
        app = meta["project"]
        info = plistlib.loads(plist(meta))
        info["CFBundleExecutable"] = executable
        info["CFBundleName"] = executable
        files = {f"Payload/{bundle}.app/Info.plist": plistlib.dumps(info),
                 f"Payload/{bundle}.app/{executable}": binary,
                 f"Payload/{bundle}.app/data.json": b'{"level": 1}',
                 f"Payload/{bundle}.app/AppIcon60x60@2x.png": app["title"].encode()}
        path = self.root / name
        with zipfile.ZipFile(path, "w") as z:
            for filename, data in files.items():
                z.writestr(filename, data)
        return path

    def test_renamed_bundle_and_executable_are_compared_by_role(self):
        base, flavor = metadata(), metadata(True)
        a, _ = payload(self.named_ipa("base.ipa", base, "DayGames", "DayGames"), base, "ios-uikit")
        b, _ = payload(self.named_ipa("flavor.ipa", flavor, "FairGames", "FairGames"), flavor,
                       "ios-uikit")
        # The bundle name, the executable name and the plist keys that carry them are identity,
        # so a flavor that renames the app compares equal to the base it was built from.
        self.assertEqual(a, b)
        self.assertIn("Payload/App.app/<executable>", a)
        # What the executable holds is still compared under that key.
        c, _ = payload(self.named_ipa("other.ipa", flavor, "FairGames", "FairGames", b"other code"),
                       flavor, "ios-uikit")
        self.assertNotEqual(a["Payload/App.app/<executable>"], c["Payload/App.app/<executable>"])
        self.assertEqual({k: v for k, v in a.items() if k != "Payload/App.app/<executable>"},
                         {k: v for k, v in c.items() if k != "Payload/App.app/<executable>"})

    def test_extra_permissions_are_not_metadata_exemptions(self):
        meta = metadata(True)
        info = plistlib.loads(plist(meta))
        info["NSCameraUsageDescription"] = "unexpected permission"
        a, _ = payload(self.ipa("base.ipa", metadata()), metadata(), "ios-uikit")
        b, _ = payload(self.ipa("flavor.ipa", meta, {"Payload/Game.app/Info.plist": plistlib.dumps(info)}),
                       meta, "ios-uikit")
        self.assertNotEqual(a, b)

    def test_wrong_identity_is_rejected_before_normalization(self):
        with self.assertRaisesRegex(ValueError, "CFBundleIdentifier"):
            payload(self.ipa("base.ipa", metadata()), metadata(True), "ios-uikit")

    def test_android_identity_normalization_keeps_permissions_and_providers(self):
        def manifest(app, permission="android.permission.INTERNET"):
            return f'''  E: manifest (line=2)
    A: package="{app['id']}"
    A: android:versionCode(0x1)={app['build']} (Raw: "{app['build']}")
    A: android:versionName(0x2)="{app['version']}"
    A: android:label(0x3)="{app['title']}"
    A: android:scheme(0x4)="{app['scheme']}"
    A: android:authorities(0x5)="{app['id']}.day.transfer"
    A: android:exported(0x6)=false
    A: android:name(0x7)="{permission}"
'''
        base = metadata()["project"]["resolved"]["android-mdc"]
        flavor = metadata(True)["project"]["resolved"]["android-mdc"]
        a = normalize_android_manifest(manifest(base), base)
        b = normalize_android_manifest(manifest(flavor), flavor)
        self.assertEqual(a, b)
        self.assertNotEqual(a, normalize_android_manifest(manifest(flavor, "android.permission.CAMERA"), flavor))
        self.assertNotEqual(a, normalize_android_manifest(manifest(flavor).replace("=false", "=true"), flavor))
        with self.assertRaises(ValueError):
            normalize_android_manifest(manifest(base), flavor)

    def test_asset_catalog_retains_non_icon_digests(self):
        header = {"Timestamp": 123}
        icon = {"AssetType": "Icon Image", "Name": "AppIcon", "SHA1Digest": "icon1"}
        image = {"AssetType": "Image", "Name": "board", "SHA1Digest": "board1"}
        with patch("package_compare.shutil.which", return_value="assetutil"), patch(
            "package_compare.run_tool", return_value=json.dumps([header, icon, image])
        ):
            a = normalize_assets(b"car")
        icon["SHA1Digest"] = "icon2"
        with patch("package_compare.shutil.which", return_value="assetutil"), patch(
            "package_compare.run_tool", return_value=json.dumps([header, icon, image])
        ):
            self.assertEqual(a, normalize_assets(b"car"))
        image["SHA1Digest"] = "different board"
        with patch("package_compare.shutil.which", return_value="assetutil"), patch(
            "package_compare.run_tool", return_value=json.dumps([header, icon, image])
        ):
            self.assertNotEqual(a, normalize_assets(b"car"))
        with patch("package_compare.shutil.which", return_value=None), self.assertRaises(ValueError):
            normalize_assets(b"car")

    def test_signing_exclusions_do_not_hide_runtime_meta_inf(self):
        self.assertTrue(signing_file("META-INF/DAY-DEV.RSA"))
        self.assertTrue(signing_file("Payload/Game.app/_CodeSignature/CodeResources"))
        self.assertFalse(signing_file("META-INF/services/a.runtime.Service"))

    def test_android_native_and_dex_payloads_are_always_compared(self):
        def aab(name, changed=None):
            files = {"base/lib/arm64-v8a/libdayapp.so": b"native",
                     "base/dex/classes.dex": b"dex", "base/assets/level.json": b"level"}
            if changed:
                files[changed] = b"modified"
            path = self.root / name
            with zipfile.ZipFile(path, "w") as z:
                for key, value in files.items():
                    z.writestr(key, value)
            return payload(path)[0]
        base = aab("base.aab")
        for name in ["base/lib/arm64-v8a/libdayapp.so", "base/dex/classes.dex", "base/assets/level.json"]:
            self.assertNotEqual(base, aab("modified.aab", name))

    def test_release_selection_uses_base_identity_and_rejects_ambiguity(self):
        for name in ["fair-games-android-mdc.aab", "day-games-android-mdc.aab"]:
            (self.root / name).touch()
        self.assertEqual(queue.select_release(self.root, metadata(), "android-mdc").name,
                         "day-games-android-mdc.aab")
        (self.root / "day-games-2.0.1-android-mdc.aab").touch()
        with self.assertRaises(ValueError):
            queue.select_release(self.root, metadata(), "android-mdc")
        (self.root / "day-games-ios-uikit-unsigned.ipa").touch()
        self.assertEqual(queue.select_release(self.root, metadata(), "ios-uikit").name,
                         "day-games-ios-uikit-unsigned.ipa")

    def test_the_tag_names_the_version_and_the_commit_must_match(self):
        app = queue.App(self.root / "Faire-Games.yaml", {
            "token": "Faire-Games", "title": "Fair Games", "tag": "v1.9.0", "commit": "a" * 40,
            "distribution": {"ios-uikit": ["apple-app-store"], "android-mdc": ["google-play-store"]}})
        path = self.root / "metadata.json"
        meta = metadata(True)
        path.write_text(json.dumps(meta))
        args = argparse.Namespace(app="Faire-Games", metadata=str(path), tag_commit="a" * 40)
        with patch.object(queue, "catalog", return_value=[app]), patch.dict(
            "os.environ", {"GITHUB_REPOSITORY": "appfair/appfair-apps"}
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            # v1.9.0 builds 1.9.0, at the commit the submission pins.
            self.assertEqual(queue.cmd_verify(args), 0)
            # A tag that names another version, whatever the flavor was given.
            app.data["tag"] = "v2.0.1"
            self.assertEqual(queue.cmd_verify(args), 1)
            # A target resolved to a version of its own is caught the same way.
            app.data["tag"] = "v1.9.0"
            meta["project"]["resolved"]["android-mdc"]["version"] = "1.9.1"
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)
            meta["project"]["resolved"]["android-mdc"]["version"] = "1.9.0"
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 0)
            # A tag that has moved off the pinned commit.
            args.tag_commit = "b" * 40
            self.assertEqual(queue.cmd_verify(args), 1)
            args.tag_commit = "a" * 40
            meta["project"]["targets"] = ["ios-uikit"]
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)


if __name__ == "__main__":
    unittest.main()

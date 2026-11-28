"""Regression cases for comparing a store flavor with the normal tagged release."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
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
    # `flavors` is what the project declares, which is how the checks tell an app that carries
    # no Day-appfair.toml from one whose flavor the workflow forgot.
    return {"flavor": "appfair" if flavor else None,
            "flavors": ["appfair"] if flavor else [], "project": {
        **app, "targets": ["ios-uikit", "android-material"],
        "resolved": {"ios-uikit": app, "android-material": android}}}


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

    def test_build_machine_os_is_normalized_but_the_toolchain_is_not(self):
        base, flavor = metadata(), metadata(True)
        def ipa(name, meta, extra):
            info = plistlib.loads(plist(meta))
            info.update(extra)
            return self.ipa(name, meta, {"Payload/Game.app/Info.plist": plistlib.dumps(info)})
        # Two runners of the same image generation stamp different OS builds.
        a, _ = payload(ipa("base.ipa", base, {"BuildMachineOSBuild": "26A428",
                                              "DTXcodeBuild": "27A266a"}), base, "ios-uikit")
        b, _ = payload(ipa("flavor.ipa", flavor, {"BuildMachineOSBuild": "26A5406e",
                                                  "DTXcodeBuild": "27A266a"}), flavor, "ios-uikit")
        self.assertEqual(a["Payload/App.app/Info.plist"], b["Payload/App.app/Info.plist"])
        # A different Xcode is a real difference between the two builds.
        c, _ = payload(ipa("other.ipa", flavor, {"BuildMachineOSBuild": "26A428",
                                                 "DTXcodeBuild": "27B100"}), flavor, "ios-uikit")
        self.assertNotEqual(a["Payload/App.app/Info.plist"], c["Payload/App.app/Info.plist"])

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
        base = metadata()["project"]["resolved"]["android-material"]
        flavor = metadata(True)["project"]["resolved"]["android-material"]
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
        for name in ["fair-games-android-material.aab", "day-games-android-material.aab"]:
            (self.root / name).touch()
        self.assertEqual(queue.select_release(self.root, metadata(), "android-material").name,
                         "day-games-android-material.aab")
        (self.root / "day-games-2.0.1-android-material.aab").touch()
        with self.assertRaises(ValueError):
            queue.select_release(self.root, metadata(), "android-material")
        (self.root / "day-games-ios-uikit-unsigned.ipa").touch()
        self.assertEqual(queue.select_release(self.root, metadata(), "ios-uikit").name,
                         "day-games-ios-uikit-unsigned.ipa")

    def test_the_tag_names_the_version_and_the_commit_must_match(self):
        app = queue.App(self.root / "Faire-Games.yaml", {
            "token": "Faire-Games", "title": "Fair Games", "tag": "v1.9.0", "commit": "a" * 40,
            "distribution": {"ios-uikit": ["apple-app-store"], "android-material": ["google-play-store"]}})
        path = self.root / "metadata.json"
        meta = metadata(True)
        path.write_text(json.dumps(meta))
        args = argparse.Namespace(app="Faire-Games", metadata=str(path), tag_commit="a" * 40, offline=True)
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
            meta["project"]["resolved"]["android-material"]["version"] = "1.9.1"
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)
            meta["project"]["resolved"]["android-material"]["version"] = "1.9.0"
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 0)
            # A tag that has moved off the pinned commit.
            args.tag_commit = "b" * 40
            self.assertEqual(queue.cmd_verify(args), 1)
            args.tag_commit = "a" * 40
            meta["project"]["targets"] = ["ios-uikit"]
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)

    def test_an_app_may_carry_no_flavor_and_publish_under_a_renamed_id(self):
        """A flavor and the id an app publishes under are both the app's own business.

        An app whose Day.toml already states the App Fair identity carries no
        `Day-appfair.toml`, and an app publishes under whatever id its manifest states, with or
        without a pin in the submission. What stays fixed: an app that DOES declare the flavor
        has to be read through it, and a pin the build contradicts is a problem.
        """
        meta = metadata(True)
        meta["flavor"], meta["flavors"] = None, []  # no Day-appfair.toml in the app
        path = self.root / "metadata.json"
        path.write_text(json.dumps(meta))
        app = queue.App(self.root / "Games-Fair.yaml", {
            "token": "Games-Fair", "title": "Games Fair", "tag": "v1.9.0", "commit": "a" * 40,
            "id": "org.appfair.app.Faire-Games",
            "distribution": {"ios-uikit": ["apple-app-store"], "android-material": ["google-play-store"]}})
        args = argparse.Namespace(app="Games-Fair", metadata=str(path), tag_commit="a" * 40, offline=True)
        with patch.object(queue, "catalog", return_value=[app]), patch.dict(
            "os.environ", {"GITHUB_REPOSITORY": "appfair/appfair-apps"}
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(queue.cmd_verify(args), 0)
            # And without the pin: the build still says which records it publishes to.
            del app.data["id"]
            self.assertEqual(queue.cmd_verify(args), 0)
            # A pin the build contradicts is caught here rather than at the store.
            app.data["id"] = "org.appfair.app.Something-Else"
            self.assertEqual(queue.cmd_verify(args), 1)
            app.data["id"] = "org.appfair.app.Faire-Games"
            # An app that declares the flavor is read through it, or the workflow has a bug.
            meta["flavors"] = ["appfair"]
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)
            # And one that declares none is read as it stands, never with a flavor.
            meta["flavors"], meta["flavor"] = [], "appfair"
            path.write_text(json.dumps(meta))
            self.assertEqual(queue.cmd_verify(args), 1)

    def test_an_id_is_anything_inside_the_namespace_and_not_another_app_s(self):
        """What an id has to be, and what it cannot be.

        Inside `org.appfair.app.` an id is the app's own business: any spelling the stores accept,
        with no relation to the token. What is refused: a string neither store would take, a Play
        package name with a hyphen in it, an id outside the namespace the App Fair publishes in,
        and an id another submission already publishes under.
        """
        policy = queue.Policy.load()
        def app(path_name, data):
            return queue.App(self.root / path_name, data)
        base = {"token": "Games-Fair", "title": "Games Fair", "tag": "v1.9.0", "commit": "a" * 40,
                "distribution": {"ios-uikit": ["apple-app-store"],
                                 "android-material": ["google-play-store"]}}
        mine = app("Games-Fair.yaml", {**base, "id": "org.appfair.app.Faire-Games"})
        self.assertEqual(queue.validate_app(mine, policy), [])
        self.assertEqual(queue.published_android_id(mine, policy), "org.appfair.app.Faire_Games")

        # An id of the app's own choosing, as long as it is inside the namespace.
        elsewhere = app("Games-Fair.yaml", {**base, "id": "org.appfair.app.anything_at_all",
                                            "android-id": "org.appfair.app.something_else"})
        self.assertEqual(queue.validate_app(elsewhere, policy), [])
        self.assertEqual(queue.published_id(elsewhere, policy), "org.appfair.app.anything_at_all")
        self.assertEqual(
            queue.published_android_id(elsewhere, policy), "org.appfair.app.something_else"
        )

        # And the refusals.
        for data, wanted in (
            ({**base, "id": "fair games"}, "not one the stores accept"),
            ({**base, "android-id": "org.appfair.app.Faire-Games"}, "not one the stores accept"),
            ({**base, "id": "com.example.games"}, "is outside"),
        ):
            problems = queue.validate_app(app("Games-Fair.yaml", data), policy)
            self.assertTrue(any(wanted in p.message for p in problems), problems)
        theirs = app("Faire-Games.yaml", {"token": "Faire-Games", "title": "Fair Games",
                                          "tag": "v1.9.0", "commit": "a" * 40,
                                          "distribution": {"ios-uikit": ["apple-app-store"]}})
        self.assertTrue(any("already taken" in p.message
                            for p in queue.validate_app(mine, policy, [mine, theirs])))


MARKER = b"day-piece-appfair/"


def binary(*versions, size=4096):
    """A stand-in for a compiled binary: filler, with the marker of each version a build with the
    App Fair module leaves in it."""
    filler = bytes(range(256)) * (size // 256)
    return filler + b"".join(MARKER + v.encode() + b"\0" + filler for v in versions)


class ModuleMarkerTests(unittest.TestCase):
    """Stage B reads the App Fair module's marker out of the binaries Rust is linked into."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, name, files):
        path = self.root / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for filename, data in files.items():
                z.writestr(filename, data)
        return path

    def ipa(self, name, executable_bytes):
        info = plistlib.loads(plist(metadata(True)))
        info["CFBundleExecutable"] = "FairGames"
        return self.package(name, {
            "Payload/FairGames.app/Info.plist": plistlib.dumps(info),
            "Payload/FairGames.app/FairGames": executable_bytes,
            # Neither of these is the app's main executable.
            "Payload/FairGames.app/Frameworks/Other.framework/Other": binary("9.9.9"),
            "Payload/FairGames.app/PlugIns/Share.appex/FairGames": binary("9.9.9"),
        })

    def run_command(self, command, **arguments):
        """A subcommand's exit code and everything it printed, kept out of a real run's summary
        and annotations."""
        out = io.StringIO()
        with patch.dict("os.environ"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            for name in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
                os.environ.pop(name, None)
            code = command(argparse.Namespace(**arguments))
        return code, out.getvalue()

    def inspect(self, package):
        report = self.root / "report.json"
        code, _ = self.run_command(queue.cmd_inspect, package=str(package), sibling=None, out=str(report))
        self.assertEqual(code, 0)
        return json.loads(report.read_text())

    def audit(self, package, target="ios-uikit"):
        """Inspect a package, then audit the report as stage B does."""
        self.inspect(package)
        meta = self.root / "metadata.json"
        meta.write_text(json.dumps(metadata(True)))
        app = queue.App(self.root / "Faire-Games.yaml", {
            "token": "Faire-Games", "title": "Fair Games", "tag": "v1.9.0", "commit": "a" * 40,
            "distribution": {"ios-uikit": ["apple-app-store"], "android-material": ["google-play-store"]}})
        with patch.object(queue, "catalog", return_value=[app]):
            return self.run_command(
                queue.cmd_audit, app="Faire-Games", report=str(self.root / "report.json"),
                metadata=str(meta), target=target, sidecars=None, expect_commit=None)

    def test_a_marker_is_read_from_any_block_size(self):
        data = binary("0.1.12")
        for block_size in (1, 2, 3, 7, 17, 18, 19, 64, 127, 128, 129, 1000, len(data), 1 << 20):
            with self.subTest(block_size=block_size):
                self.assertEqual(queue.module_version(io.BytesIO(data), block_size), "0.1.12")
        self.assertIsNone(queue.module_version(io.BytesIO(binary())))
        self.assertIsNone(queue.module_version(io.BytesIO(b"")))

    def test_a_binary_is_never_read_whole(self):
        class Counted(io.BytesIO):
            asked: list = []

            def read(self, size=-1):
                self.asked.append(size)
                return super().read(size)

        stream = Counted(binary("0.1.0", size=1 << 16))
        self.assertEqual(queue.module_version(stream, 4096), "0.1.0")
        self.assertEqual(set(stream.asked), {4096})
        self.assertGreater(len(stream.asked), 16)

    def test_a_marker_lying_across_two_blocks_is_read_whole(self):
        # Every block boundary falls on every byte of the marker in turn, the digits of the
        # version included: `0.1.12` cut after `0.1.1` is still 0.1.12, and a pre-release cut
        # before or inside its suffix is still that pre-release.
        for version in ("0.1.12", "1.0.0-rc.1"):
            marker = MARKER + version.encode()
            for block_size, leads in ((64, range(420)), (100, range(420)), (128, range(420)),
                                      (129, range(420)), (1, range(0, 300, 13)), (7, range(0, 300, 5))):
                for lead in leads:
                    with self.subTest(version=version, block_size=block_size, lead=lead):
                        data = b"\0" * lead + marker + b"\0" * 300
                        self.assertEqual(queue.module_version(io.BytesIO(data), block_size), version)
                        # And as the last bytes of the file, with nothing after the version.
                        ending = io.BytesIO(data[:lead + len(marker)])
                        self.assertEqual(queue.module_version(ending, block_size), version)

    def test_the_overlap_covers_the_longest_marker(self):
        longest = MARKER + b"999999999.999999999.999999999-" + b"a" * 32
        self.assertLessEqual(len(longest), queue.MARKER_OVERLAP)
        self.assertEqual(queue.module_marker().fullmatch(longest).end(), len(longest))
        for lead in range(0, 200, 7):
            data = b"\0" * lead + longest + b"\0" * 300
            self.assertEqual(queue.module_version(io.BytesIO(data), 100), longest[len(MARKER):].decode())

    def test_the_version_ends_at_the_nul_the_crate_puts_after_it(self):
        # What a built app holds: the marker, the NUL the crate ends it with, and whatever the
        # linker placed next. None of what follows the NUL is read into the version.
        followers = (b"-junk", b"-rc.9", b".5", b"7", b"+build.1", b"/rustc/0ef1bc386059596d")
        for version in ("0.1.0", "0.1.0-rc.1", "1.2.3"):
            for after in followers:
                with self.subTest(version=version, after=after):
                    data = binary() + MARKER + version.encode() + b"\0" + after + binary()
                    self.assertEqual(queue.module_version(io.BytesIO(data)), version)
        # With a block boundary on each byte of the marker, the NUL and the bytes after it.
        marker = MARKER + b"0.1.0\0-junk"
        for lead in range(130):
            with self.subTest(lead=lead):
                data = b"\xff" * lead + marker + b"\xff" * 200
                self.assertEqual(queue.module_version(io.BytesIO(data), 64), "0.1.0")
        # The package check then takes the version for what it is: the minimum, and no
        # pre-release of it.
        package = self.ipa("fair-games-ios-uikit.ipa", binary() + marker + binary())
        self.assertEqual(self.inspect(package)["appfair-module"], {"Payload/FairGames.app/FairGames": "0.1.0"})
        code, output = self.audit(package)
        self.assertEqual(code, 0, output)

    def test_any_byte_that_cannot_continue_a_version_ends_it(self):
        # A literal the linker placed straight after the version, as in a build of the crate
        # before it wrote the NUL.
        for after in (b"DAY_SCRIPTappfair", b"day_piece_appfair", b" (demo)", b"\n", b"/", b"_1", b"\xff"):
            with self.subTest(after=after):
                data = binary() + MARKER + b"0.1.0" + after + binary()
                self.assertEqual(queue.module_version(io.BytesIO(data)), "0.1.0")
        # Build metadata is not part of a version's order.
        self.assertEqual(queue.module_version(io.BytesIO(MARKER + b"0.1.0+build.7")), "0.1.0")
        # A hyphen does continue a version, as a pre-release, which is what the NUL rules out.
        self.assertEqual(queue.module_version(io.BytesIO(MARKER + b"0.1.0-junk")), "0.1.0-junk")
        self.assertEqual(queue.module_version(io.BytesIO(MARKER + b"0.1.0\0-junk")), "0.1.0")

    def test_only_a_version_after_the_name_is_a_marker(self):
        paths = (b"/checkout/day-piece-appfair/src/lib.rs\0"
                 b"/registry/src/day-piece-appfair-0.1.0/src/lib.rs\0"
                 # Cargo's build directory for the crate, which its generated sources name.
                 b"/target/debug/build/day-piece-appfair/605c3b6417ec69b6/out\0"
                 b"day-piece-appfair/\0day-piece-appfair/0.1\0day_piece_appfair/0.1.0\0")
        self.assertIsNone(queue.module_version(io.BytesIO(binary() + paths)))
        # With two releases linked in, the newest is the one the package is held to.
        self.assertEqual(queue.module_version(io.BytesIO(binary("0.1.0", "0.2.3", "0.2.3-rc.1"))), "0.2.3")
        self.assertEqual(queue.module_version(io.BytesIO(binary("0.10.0", "0.9.0"))), "0.10.0")

    def test_inspect_records_the_marker_of_every_abi(self):
        aab = self.package("fair-games-android-material.aab", {
            "base/lib/arm64-v8a/libdayapp.so": binary("0.1.0"),
            "base/lib/armeabi-v7a/libdayapp.so": binary("0.1.0"),
            "base/lib/x86_64/libdayapp.so": binary(),
            # Another library's bytes say nothing about the app's.
            "base/lib/arm64-v8a/libother.so": binary("0.1.0"),
            "base/dex/classes.dex": b"dex",
        })
        self.assertEqual(self.inspect(aab)["appfair-module"], {
            "base/lib/arm64-v8a/libdayapp.so": "0.1.0",
            "base/lib/armeabi-v7a/libdayapp.so": "0.1.0",
            "base/lib/x86_64/libdayapp.so": None,
        })
        # An .apk carries the same libraries without the module directory.
        apk = self.package("fair-games.apk", {"lib/arm64-v8a/libdayapp.so": binary("0.3.1")})
        self.assertEqual(queue.module_markers(apk), {"lib/arm64-v8a/libdayapp.so": "0.3.1"})

    def test_inspect_reads_the_ios_executable_the_bundle_names(self):
        report = self.inspect(self.ipa("fair-games-ios-uikit.ipa", binary("0.1.0")))
        self.assertEqual(report["appfair-module"], {"Payload/FairGames.app/FairGames": "0.1.0"})

    def test_the_audit_passes_a_package_built_with_the_module(self):
        minimum = queue.appfair_lint().APPFAIR_MODULE_MINIMUM
        for version in (minimum, "0.1.1", "1.0.0"):
            with self.subTest(version=version):
                code, output = self.audit(self.ipa("fair-games-ios-uikit.ipa", binary(version)))
                self.assertEqual(code, 0, output)

    def test_the_audit_refuses_a_package_built_without_the_module(self):
        code, output = self.audit(self.ipa("fair-games-ios-uikit.ipa", binary()))
        self.assertEqual(code, 1)
        self.assertIn("Payload/FairGames.app/FairGames carries no `day-piece-appfair/<version>` marker", output)
        self.assertIn("appfair-module", output)

    def test_the_audit_refuses_a_module_older_than_the_lint_s_minimum(self):
        code, output = self.audit(self.ipa("fair-games-ios-uikit.ipa", binary("0.0.9")))
        self.assertEqual(code, 1)
        self.assertIn("was built with day-piece-appfair 0.0.9", output)
        self.assertIn("takes 0.1.0 or newer", output)
        # The minimum is the lint's constant, so raising that one line raises both checks.
        package = self.ipa("fair-games-ios-uikit.ipa", binary("0.1.5"))
        self.assertEqual(self.audit(package)[0], 0)
        with patch.object(queue.appfair_lint(), "APPFAIR_MODULE_MINIMUM", "0.2.0"):
            code, output = self.audit(package)
        self.assertEqual(code, 1)
        self.assertIn("takes 0.2.0 or newer", output)

    def test_every_abi_has_to_carry_the_module(self):
        good = {"base/lib/arm64-v8a/libdayapp.so": "0.1.0", "base/lib/x86_64/libdayapp.so": "0.2.0"}
        self.assertEqual(queue.module_problems(good), [])
        one_missing = queue.module_problems({**good, "base/lib/x86_64/libdayapp.so": None})
        self.assertEqual(len(one_missing), 1)
        self.assertIn("base/lib/x86_64/libdayapp.so carries no", one_missing[0])
        one_old = queue.module_problems({**good, "base/lib/arm64-v8a/libdayapp.so": "0.1.0-rc.1"})
        self.assertEqual(len(one_old), 1)
        self.assertIn("base/lib/arm64-v8a/libdayapp.so was built with day-piece-appfair 0.1.0-rc.1", one_old[0])
        self.assertEqual(len(queue.module_problems({path: None for path in good})), 2)

    def test_a_package_with_no_binary_to_read_is_refused(self):
        self.assertIn("holds no libdayapp.so and no main executable", queue.module_problems({})[0])
        # An .aab with its library under another name, and a report written before this check.
        aab = self.package("fair-games-android-material.aab", {"base/lib/arm64-v8a/libapp.so": binary("0.1.0")})
        self.assertEqual(self.inspect(aab)["appfair-module"], {})
        report = self.root / "report.json"
        facts = {"id": "org.appfair.app.Faire_Games", "version": "1.9.0", "build": "36", "permissions": []}
        report.write_text(json.dumps({"package": aab.name, "facts": facts}))
        meta = self.root / "metadata.json"
        meta.write_text(json.dumps(metadata(True)))
        app = queue.App(self.root / "Faire-Games.yaml", {"token": "Faire-Games"})
        with patch.object(queue, "catalog", return_value=[app]):
            code, output = self.run_command(
                queue.cmd_audit, app="Faire-Games", report=str(report), metadata=str(meta),
                target="android-material", sidecars=None, expect_commit=None)
        self.assertEqual(code, 1)
        self.assertIn("holds no libdayapp.so and no main executable", output)
        # The same report with a marker for its one library passes, so nothing else failed it.
        report.write_text(json.dumps({"package": aab.name, "facts": facts,
                                      "appfair-module": {"base/lib/arm64-v8a/libdayapp.so": "0.1.0"}}))
        with patch.object(queue, "catalog", return_value=[app]):
            code, output = self.run_command(
                queue.cmd_audit, app="Faire-Games", report=str(report), metadata=str(meta),
                target="android-material", sidecars=None, expect_commit=None)
        self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()

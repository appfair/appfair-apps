"""The release upload: which packages it attaches, and what it refuses.

GitHub is replaced by a recorder, so every case here runs offline.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release_upload as ru  # noqa: E402

ACCOUNT = "appfairbot"


class Fake:
    """GitHub, as far as these tests are concerned."""

    def __init__(self, login=ACCOUNT, push=True, release=None, repo_status=200, installed=True):
        self.login = login
        self.push = push
        self.release = release if release is not None else {"id": 7, "assets": []}
        self.repo_status = repo_status
        #: Whether the App Fair app is installed on the repository being asked about.
        self.installed = installed
        self.calls: list[tuple[str, str]] = []
        self.patched: dict | None = None
        #: The JWT the app signed its installation lookup with, for the signature test.
        self.jwt = ""

    def __call__(self, method, url, token, data=None, content_type=""):
        self.calls.append((method, url))
        if url.endswith("/installation"):
            self.jwt = token
            if not self.installed:
                return 404, {"message": "Not Found"}
            return 200, {"access_tokens_url": f"{ru.API}/app/installations/42/access_tokens"}
        if url.endswith("/access_tokens"):
            self.minted = json.loads(data.decode())
            return 201, {"token": "ghs_installation_token"}
        if url.endswith("/user"):
            return 200, {"login": self.login}
        if "/releases/tags/" in url:
            return (200, self.release) if self.release else (404, {"message": "Not Found"})
        if "/releases/assets/" in url and method == "DELETE":
            return 204, None
        if "/releases/" in url and method == "PATCH":
            self.patched = json.loads(data.decode())
            return 200, {}
        if "/releases/" in url and method == "POST":
            return 201, {"name": url.rsplit("name=", 1)[-1]}
        if self.repo_status != 200:
            return self.repo_status, {"message": "Not Found"}
        return 200, {"permissions": {"push": self.push}}


class ReleaseUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.env("APPFAIR_RELEASE_TOKEN", "a-token")
        self.env("GITHUB_ACTIONS", None)
        self.env("GITHUB_STEP_SUMMARY", None)

    def env(self, name, value):
        import os

        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def github(self, **kwargs) -> Fake:
        fake = Fake(**kwargs)
        original = ru.api
        ru.api = fake
        self.addCleanup(lambda: setattr(ru, "api", original))
        return fake

    def packages(self, *names) -> Path:
        for name in names:
            (self.dir / name).write_bytes(b"a package")
        return self.dir

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = ru.main(list(argv))
        return code, out.getvalue()

    # --- what gets attached -------------------------------------------------------------

    def test_only_the_flavored_packages_are_attached(self):
        """The rail: an asset the catalog attaches carries the flavor, so it can never take the
        name of one the maintainer published."""
        self.packages(
            "games-fair-appfair-android-mdc.aab",
            "games-fair-appfair-android-mdc.apk",
            "games-fair-appfair-android-mdc.aab.buildinfo.json",
            "games-fair-android-mdc.aab",
            "pack-android-mdc.json",
        )
        found, skipped = ru.attachable(self.dir, "appfair")
        self.assertEqual(
            [p.name for p in found],
            ["games-fair-appfair-android-mdc.aab", "games-fair-appfair-android-mdc.apk"],
        )
        self.assertEqual(skipped, ["games-fair-android-mdc.aab (no -appfair- in its name)"])

    def test_a_signed_ipa_loses_the_unsigned_marker(self):
        self.assertEqual(
            ru.published_name("games-fair-appfair-ios-uikit-unsigned.ipa"),
            "games-fair-appfair-ios-uikit.ipa",
        )
        self.assertEqual(ru.published_name("games-fair-appfair-android-mdc.aab"), "games-fair-appfair-android-mdc.aab")

    # --- check ---------------------------------------------------------------------------

    def test_the_access_check_passes_when_the_account_can_write(self):
        self.github()
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("can write to Games-Fair/Games-Fair", output)

    def test_an_app_that_granted_nothing_is_told_what_to_install_and_passes(self):
        """The catalog publishes either way; only the release upload is lost."""
        self.github(push=False)
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("warning", output)
        self.assertIn("cannot write to Games-Fair/Games-Fair", output)
        self.assertIn("https://github.com/apps/app-fair", output)
        # A catalog that would rather stop says so in policy.yaml, or on the command line.
        code, _ = self.run_cli(
            "check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT, "--missing-access", "error"
        )
        self.assertEqual(code, 1)

    def test_a_repository_the_account_cannot_see_says_so(self):
        self.github(repo_status=404)
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("not visible", output)

    def test_a_token_belonging_to_another_account_is_reported(self):
        """The upload replaces only assets it uploaded, which is decided by the login."""
        self.github(login="someone-else")
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("authenticates as 'someone-else'", output)
        code, _ = self.run_cli(
            "check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT, "--missing-access", "error"
        )
        self.assertEqual(code, 1)

    def test_a_job_without_the_token_says_so_and_passes(self):
        """A fork's pull request holds no secrets."""
        self.env("APPFAIR_RELEASE_TOKEN", None)
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0)
        self.assertIn("warning", output)

    def test_a_tag_with_no_release_has_nothing_to_attach_to(self):
        self.github(release={})
        code, output = self.run_cli(
            "check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT, "--tag", "v9.9.9"
        )
        self.assertEqual(code, 0)
        self.assertIn("no release for v9.9.9", output)

    # --- the App Fair app ------------------------------------------------------------------

    def app_key(self) -> str:
        """A throwaway RSA key, so the JWT this signs is a real one."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        self.env("APPFAIR_APP_PRIVATE_KEY", pem)
        self.public = key.public_key()
        return pem

    def with_app(self, **kwargs) -> Fake:
        """policy.yaml names the app; this run holds its key."""
        original = ru.policy
        ru.policy = lambda: {**original(), "app-id": "1234567", "app": "app-fair"}
        self.addCleanup(lambda: setattr(ru, "policy", original))
        self.app_key()
        return self.github(**kwargs)

    def test_the_app_mints_a_token_for_that_repository_alone(self):
        fake = self.with_app()
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair")
        self.assertEqual(code, 0, output)
        self.assertIn("installation on Games-Fair/Games-Fair", output)
        # Narrowed on the way out: one repository, one permission.
        self.assertEqual(
            fake.minted, {"repositories": ["Games-Fair"], "permissions": {"contents": "write"}}
        )
        # The repository lookup used the minted token, not the JWT.
        self.assertIn(("GET", f"{ru.API}/repos/Games-Fair/Games-Fair"), fake.calls)

    def test_the_jwt_is_signed_with_the_app_key(self):
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        fake = self.with_app()
        self.run_cli("check", "--repo", "Games-Fair/Games-Fair")
        head, claims, signature = fake.jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head)))["alg"], "RS256")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(claims)))["iss"], "1234567")
        self.public.verify(
            base64.urlsafe_b64decode(pad(signature)),
            f"{head}.{claims}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_a_repository_without_the_app_falls_back_to_the_token(self):
        """Until every app has installed it, the older account still carries the upload."""
        fake = self.with_app(installed=False)
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair", "--account", ACCOUNT)
        self.assertEqual(code, 0, output)
        self.assertIn("APPFAIR_RELEASE_TOKEN", output)
        self.assertIn(("GET", f"{ru.API}/user"), fake.calls)

    def test_a_repository_with_neither_says_what_to_install(self):
        self.env("APPFAIR_RELEASE_TOKEN", None)
        self.with_app(installed=False)
        code, output = self.run_cli("check", "--repo", "Games-Fair/Games-Fair")
        self.assertEqual(code, 0)
        self.assertIn("is not installed on Games-Fair/Games-Fair", output)
        self.assertIn("https://github.com/apps/app-fair", output)

    def test_the_app_replaces_its_own_assets(self):
        """Uploaded as app-fair[bot], so that is the login the rail recognizes."""
        name = "games-fair-appfair-android-mdc.aab"
        mine = {"id": 7, "assets": [{"id": 11, "name": name, "uploader": {"login": "app-fair[bot]"}}]}
        fake = self.with_app(release=mine)
        self.packages(name)
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair",
        )
        self.assertEqual(code, 0, output)
        self.assertIn(("DELETE", f"{ru.API}/repos/Games-Fair/Games-Fair/releases/assets/11"), fake.calls)

    # --- upload --------------------------------------------------------------------------

    def test_the_packages_are_posted_to_the_release(self):
        fake = self.github()
        self.packages("games-fair-appfair-android-mdc.aab", "games-fair-appfair-ios-uikit-unsigned.ipa")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair", "--account", ACCOUNT,
        )
        self.assertEqual(code, 0, output)
        posted = [url.rsplit("name=", 1)[-1] for method, url in fake.calls if method == "POST"]
        self.assertEqual(
            posted, ["games-fair-appfair-android-mdc.aab", "games-fair-appfair-ios-uikit.ipa"]
        )

    def test_the_catalog_replaces_its_own_asset_and_no_one_else_s(self):
        name = "games-fair-appfair-android-mdc.aab"
        mine = {"id": 7, "assets": [{"id": 11, "name": name, "uploader": {"login": ACCOUNT}}]}
        fake = self.github(release=mine)
        self.packages(name)
        args = ["upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
                "--dir", str(self.dir), "--flavor", "appfair", "--account", ACCOUNT]
        code, output = self.run_cli(*args)
        self.assertEqual(code, 0, output)
        self.assertIn(("DELETE", f"{ru.API}/repos/Games-Fair/Games-Fair/releases/assets/11"), fake.calls)

        theirs = {"id": 7, "assets": [{"id": 11, "name": name, "uploader": {"login": "the-maintainer"}}]}
        fake = self.github(release=theirs)
        code, output = self.run_cli(*args)
        self.assertEqual(code, 0)
        self.assertIn("uploaded by the-maintainer", output)
        self.assertNotIn("POST", [method for method, _ in fake.calls])
        code, _ = self.run_cli(*args, "--missing-access", "error")
        self.assertEqual(code, 1)

    def test_an_app_that_granted_nothing_keeps_its_packages_and_the_run_carries_on(self):
        """The artifact is already uploaded by the time this runs, and the message says so."""
        fake = self.github(push=False)
        self.packages("games-fair-appfair-android-mdc.aab")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair", "--account", ACCOUNT,
        )
        self.assertEqual(code, 0)
        self.assertIn("1 signed package(s)", output)
        self.assertIn("by hand", output)
        self.assertNotIn("POST", [method for method, _ in fake.calls])

    # --- the release itself ---------------------------------------------------------------

    def test_a_pre_release_becomes_the_latest_once_every_package_is_on_it(self):
        """`releases/latest/download/<name>` answers only for the latest release."""
        fake = self.github(release={"id": 7, "assets": [], "prerelease": True})
        self.packages("games-fair-appfair-android-mdc.apk")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair", "--account", ACCOUNT,
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.patched, {"prerelease": False, "make_latest": "true"})
        self.assertLess(
            [m for m, _ in fake.calls].index("POST"), [m for m, _ in fake.calls].index("PATCH")
        )

    def test_a_release_that_is_already_public_is_left_as_it_is(self):
        fake = self.github(release={"id": 7, "assets": [], "prerelease": False})
        self.packages("games-fair-appfair-android-mdc.apk")
        code, _ = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair", "--account", ACCOUNT,
        )
        self.assertEqual(code, 0)
        self.assertIsNone(fake.patched)

    def test_a_dry_run_changes_nothing(self):
        fake = self.github(release={"id": 7, "assets": [], "prerelease": True})
        self.packages("games-fair-appfair-android-mdc.apk")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1", "--dir", str(self.dir),
            "--flavor", "appfair", "--account", ACCOUNT, "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("would mark v2.1.1 as the latest release", output)
        self.assertIsNone(fake.patched)
        self.assertEqual({m for m, _ in fake.calls}, {"GET"})

    # --- stage ------------------------------------------------------------------------------

    def test_staging_gathers_the_packages_under_their_published_names(self):
        """Every run leaves this directory as an artifact, uploaded or not."""
        self.packages(
            "games-fair-appfair-android-mdc.apk",
            "games-fair-appfair-ios-uikit-unsigned.ipa",
            "games-fair-appfair-android-mdc.apk.buildinfo.json",
            "games-fair-android-mdc.apk",
        )
        out = self.dir / "staged"
        code, output = self.run_cli("stage", "--dir", str(self.dir), "--out", str(out), "--flavor", "appfair")
        self.assertEqual(code, 0, output)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["games-fair-appfair-android-mdc.apk", "games-fair-appfair-ios-uikit.ipa"],
        )

    def test_an_app_without_a_flavor_attaches_nothing(self):
        """Its packages carry the maintainer's own names, and those are not the catalog's to
        write."""
        fake = self.github()
        self.packages("games-fair-android-mdc.aab")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1", "--dir", str(self.dir)
        )
        self.assertEqual(code, 0)
        self.assertIn("builds no flavor", output)
        self.assertEqual(fake.calls, [])

    def test_packages_built_without_the_flavor_are_left_alone(self):
        """The catalog names a flavor for every app; carrying one is the app's choice, and the
        matrix cannot know. What the build produced decides."""
        fake = self.github()
        self.packages("games-fair-android-mdc.aab")
        code, output = self.run_cli(
            "upload", "--repo", "Games-Fair/Games-Fair", "--tag", "v2.1.1",
            "--dir", str(self.dir), "--flavor", "appfair",
        )
        self.assertEqual(code, 0)
        self.assertIn("nothing in", output)
        self.assertEqual(fake.calls, [])

    def test_the_policy_names_the_account_and_what_a_missing_grant_does(self):
        """The workflows read this file rather than naming an account of their own."""
        conf = ru.policy()
        self.assertEqual(conf.get("account"), ACCOUNT)
        self.assertEqual(conf.get("on-missing-access"), "warn")
        self.assertIs(conf.get("promote-prerelease"), True)


if __name__ == "__main__":
    unittest.main()

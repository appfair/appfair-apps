import base64
import json
import tempfile
import unittest
from pathlib import Path

from signing_inputs import prepare, properties


def b64(value):
    return base64.b64encode(value.encode()).decode()


class SigningInputsTests(unittest.TestCase):
    def test_bundled_credentials_are_adapted_without_changing_values(self):
        key = "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare({
                "KEYSTORE_PROPERTIES_B64": b64("keyAlias=release\nstorePassword=store\\=pass\nkeyPassword=key\\:pass\n"),
                "PLAY_KEY": b64('{"type":"service_account"}'),
                "ASC_API_KEY_B64": b64(json.dumps({"key_id": "ABC", "issuer_id": "issuer", "key": key})),
            }, tmp)
            self.assertEqual(result["APPFAIR_KEY_ALIAS"], "release")
            self.assertEqual(result["DAY_SIGN_STORE_PASS"], "store=pass")
            self.assertEqual(result["DAY_SIGN_KEY_PASS"], "key:pass")
            self.assertEqual(result["APPFAIR_ASC_KEY_ID"], "ABC")
            self.assertEqual(Path(result["APPFAIR_ASC_KEY_PATH"]).read_text(), key)
            self.assertEqual(Path(result["APPFAIR_ASC_KEY_PATH"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(Path(result["APPFAIR_PLAY_KEY_PATH"]).read_text())["type"], "service_account")

    def test_day_inputs_override_legacy_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare({"KEYSTORE_PROPERTIES_B64": b64("keyAlias=old\nstorePassword=old\n"),
                              "KEY_ALIAS": "new", "STORE_PASS": "new-pass",
                              "PLAY_KEY": '{"type":"service_account"}'}, tmp)
            self.assertEqual(result["APPFAIR_KEY_ALIAS"], "new")
            self.assertEqual(result["DAY_SIGN_STORE_PASS"], "new-pass")

    def test_properties_escapes_and_continuations(self):
        self.assertEqual(properties("! comment\nkeyAlias: rel\\u0065ase\nkeyPassword=a\\\n  b\\ c\n"),
                         {"keyAlias": "release", "keyPassword": "ab c"})

    def test_missing_keys_are_not_invented_and_malformed_keys_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(prepare({}, tmp)["APPFAIR_KEY_ALIAS"], "")
            with self.assertRaises(ValueError):
                prepare({"PLAY_KEY": "not base64"}, tmp)

    def test_complete_explicit_credentials_do_not_read_obsolete_legacy_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare({"KEY_ALIAS": "new", "STORE_PASS": "pass", "KEY_PASS": "pass",
                              "KEYSTORE_PROPERTIES_B64": "obsolete, not base64",
                              "ASC_KEY_ID": "key", "ASC_ISSUER": "issuer",
                              "ASC_KEY_B64": b64("-----BEGIN PRIVATE KEY-----\nfixture"),
                              "ASC_API_KEY_B64": "obsolete, not base64"}, tmp)
            self.assertEqual(result["APPFAIR_KEY_ALIAS"], "new")
            self.assertEqual(result["APPFAIR_ASC_KEY_ID"], "key")


if __name__ == "__main__":
    unittest.main()

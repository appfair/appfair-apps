"""Release planning uses the submitted commit's crates.io lock, never main."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_queue", ROOT / "scripts/queue.py")
queue = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = queue
spec.loader.exec_module(queue)
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
LOCK = f'''version = 4
[[package]]
name="fixture"
version="1.0.0"
[[package]]
name="day-core"
version="0.7.12"
source="{REGISTRY}"
checksum="{'a' * 64}"
'''

class ReleaseDependencies(unittest.TestCase):
    def test_cli_is_the_locked_release(self):
        self.assertEqual(queue.release_day_version(LOCK), "v0.7.12")
        self.assertEqual(queue.release_day_version(LOCK, "0.7.12"), "v0.7.12")
        for version in ["main", "latest", "v0.7.11"]:
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "must match"):
                queue.release_day_version(LOCK, version)

    def test_missing_git_and_unchecksummed_locks_fail_closed(self):
        for lock in ["version=4", LOCK.replace(REGISTRY, "git+https://example.test/day"),
                     LOCK.split("checksum=")[0]]:
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                queue.release_day_version(lock)

    def test_sources_are_read_from_the_pinned_commit(self):
        app = queue.App(path=Path("apps/Fixture.yaml"), data={"token":"Fixture", "commit":"a" * 40})
        policy = type("PolicyFixture", (), {"day_version":"auto"})()
        with patch.object(queue, "fetch_text", return_value=LOCK) as fetch:
            self.assertEqual(queue.submission_day_version(app, policy), "v0.7.12")
            self.assertEqual(fetch.call_args.args[1:], ("a" * 40, "Cargo.lock"))
        with patch.object(queue, "fetch_text", return_value=None), self.assertRaisesRegex(ValueError, "no readable Cargo.lock"):
            queue.submission_day_version(app, policy)

if __name__ == "__main__":
    unittest.main()

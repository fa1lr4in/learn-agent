import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_windows


class WindowsBuildSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="learn-agent-build-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.patch = patch.object(build_windows, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Build Test")
        self.git("config", "user.email", "build@example.invalid")
        (self.root / "api.py").write_text("print('api')\n", encoding="utf-8")
        (self.root / "desktop").mkdir()
        (self.root / "desktop/package.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
        self.git("add", "api.py", "desktop/package.json")
        self.git("commit", "-qm", "Initial fixture")

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.root, text=True).strip()

    def test_committed_inputs_have_reproducible_fingerprint(self):
        source = build_windows.build_source()
        self.assertEqual(source["commit"], self.git("rev-parse", "HEAD"))
        self.assertFalse(source["dirty"])
        self.assertEqual(source, build_windows.build_source())
        encoded = json.dumps(source["files"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(source["sha256"], hashlib.sha256(encoded).hexdigest())

    def test_modified_staged_and_new_inputs_are_dirty(self):
        original = build_windows.build_source()
        (self.root / "api.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertTrue(build_windows.build_source()["dirty"])
        self.assertNotEqual(original["sha256"], build_windows.build_source()["sha256"])
        self.git("add", "api.py")
        self.assertTrue(build_windows.build_source()["dirty"])
        self.git("commit", "-qm", "Update fixture")
        self.assertFalse(build_windows.build_source()["dirty"])
        (self.root / "desktop/new.cjs").write_text("module.exports = {};", encoding="utf-8")
        self.assertTrue(build_windows.build_source()["dirty"])

    def test_deleted_input_is_dirty(self):
        (self.root / "api.py").unlink()
        # Git must see deletions even though the file is no longer in the hash list.
        self.assertTrue(build_windows.build_source()["dirty"])

    def test_exercises_articles_and_logs_are_not_build_inputs(self):
        original = build_windows.build_source()
        for directory in ("workspace", "公众号文章", "logs"):
            target = self.root / directory
            target.mkdir()
            (target / "private.txt").write_text("local only", encoding="utf-8")
        self.assertEqual(original, build_windows.build_source())


if __name__ == "__main__":
    unittest.main()

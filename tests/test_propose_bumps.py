import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.propose_bumps as bumps

SHA = "a" * 64


class ProposeBumpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.packages.mkdir()
        self.patches = [
            mock.patch.object(bumps.catalog, "ROOT", self.root),
            mock.patch.object(bumps.catalog, "PACKAGES_DIR", self.packages),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_manifest(self, name, release, raw=None):
        directory = self.packages / name
        directory.mkdir()
        path = directory / "manifest.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({"packages": [{
                "name": name,
                "url": "https://github.com/owner/repo",
                "asset": "foo.nro",
                "release": release,
            }]}), encoding="utf-8")
        return path

    def test_release_replacement_preserves_formatting(self):
        raw = (
            '{\n'
            '  "packages": [\n'
            '    {\n'
            '      "title": "Foo",\n'
            '      "name": "foo",\n'
            '      "release": "v1",\n'
            '      "other": 1\n'
            '    }\n'
            '  ]\n'
            '}\n'
        )
        expected = raw.replace('"release": "v1"', '"release": "v2"')
        self.assertEqual(bumps.update_release_text(raw, "foo", "v1", "v2"), expected)

    def test_dry_run_prints_without_writing(self):
        path = self.write_manifest("foo", "v1")
        original = path.read_text(encoding="utf-8")
        release = {"tag_name": "v2", "draft": False, "prerelease": False,
                   "assets": [{"name": "foo.nro", "browser_download_url": "https://x", "url": "https://x", "size": 1}]}
        asset = {"name": "foo.nro", "browser_download_url": "https://x", "url": "https://x", "size": 1}
        output = io.StringIO()
        with mock.patch.object(bumps.catalog, "fetch_releases", return_value=("github", [release])), \
             mock.patch.object(bumps.catalog, "choose_release", return_value=(release, asset, "v2", "v2", None)), \
             mock.patch.object(bumps.catalog, "resolve_sha256", return_value=SHA), \
             mock.patch.object(sys, "argv", ["propose_bumps.py", "--dry-run"]), \
             contextlib.redirect_stdout(output):
            bumps.main()
        self.assertIn('"new": "v2"', output.getvalue())
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_bump_only_changes_release(self):
        raw = (
            '{\n  "packages": [\n    {\n      "name": "foo",\n'
            '      "url": "https://github.com/owner/repo",\n'
            '      "release": "v1",\n      "asset": "foo.nro"\n    }\n  ]\n}\n'
        )
        path = self.write_manifest("foo", "v1", raw=raw)
        release = {"tag_name": "v2", "draft": False, "prerelease": False,
                   "assets": [{"name": "foo.nro", "browser_download_url": "https://x", "url": "https://x", "size": 1}]}
        asset = {"name": "foo.nro", "browser_download_url": "https://x", "url": "https://x", "size": 1}
        with mock.patch.object(bumps.catalog, "fetch_releases", return_value=("github", [release])), \
             mock.patch.object(bumps.catalog, "choose_release", return_value=(release, asset, "v2", "v2", None)), \
             mock.patch.object(bumps.catalog, "resolve_sha256", return_value=SHA), \
             mock.patch.object(sys, "argv", ["propose_bumps.py"]), \
             contextlib.redirect_stdout(io.StringIO()):
            bumps.main()
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            raw.replace('"release": "v1"', '"release": "v2"'),
        )


if __name__ == "__main__":
    unittest.main()

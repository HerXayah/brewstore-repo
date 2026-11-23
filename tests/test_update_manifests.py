import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.update_manifests as update


class UpdateManifestTests(unittest.TestCase):
    def test_asset_template_is_expanded(self):
        self.assertEqual(
            update.asset_hint(
                {"asset": "old.zip", "assetTemplate": "{release_tag}.zip"},
                "v2", "v2",
            ),
            "v2.zip",
        )

    def test_strip_template_is_expanded(self):
        self.assertEqual(
            update.strip_prefix(
                {"stripPrefixTemplate": "{asset_stem}/bundle/"},
                "OpenRAC-Switch2-v0.2",
                "v0.2",
                "OpenRAC-Switch2-v0.2.zip",
            ),
            "OpenRAC-Switch2-v0.2/bundle/",
        )

    def test_selected_github_asset_count_is_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "packages" / "scummvm-s2" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"packages": [{
                "name": "scummvm-s2",
                "url": "https://github.com/owner/repo",
                "release": "latest",
                "asset": "default-engines.zip",
                "version": "v1",
                "app_dls": 2,
            }]}), encoding="utf-8")
            releases = [{
                "tag_name": "v1",
                "published_at": "2026-11-20T00:00:00Z",
                "assets": [
                    {"name": "scumm-only.zip", "download_count": 99},
                    {"name": "default-engines.zip", "download_count": 12},
                ],
            }]
            with mock.patch.object(update, "ROOT", root), \
                    mock.patch.object(update, "request_json", return_value=releases):
                self.assertEqual(update.main(), 0)
            refreshed = json.loads(manifest.read_text(encoding="utf-8"))["packages"][0]
            self.assertEqual(refreshed["app_dls"], 12)

    def test_missing_or_invalid_count_preserves_existing_value(self):
        for count in (None, -1, True, "12", 1.5, update.MAX_APP_DOWNLOADS + 1):
            with self.subTest(count=count):
                package = {"app_dls": 7}
                self.assertFalse(update.update_download_count(
                    package, {"download_count": count}
                ))
                self.assertEqual(package["app_dls"], 7)

    def test_zero_count_replaces_invalid_existing_value(self):
        package = {"app_dls": False}
        self.assertTrue(update.update_download_count(
            package, {"download_count": 0}
        ))
        self.assertEqual(package["app_dls"], 0)

    def test_missing_opt_in_is_not_changed(self):
        package = {}
        self.assertFalse(update.update_download_count(
            package, {"download_count": 12}
        ))
        self.assertNotIn("app_dls", package)

    def test_gitlab_count_is_not_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "packages" / "other" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"packages": [{
                "name": "other",
                "url": "https://gitlab.com/owner/repo",
                "asset": "other.nro",
                "app_dls": 7,
            }]}), encoding="utf-8")
            release = ("v1", [{"name": "other.nro", "download_count": 99}], 0)
            with mock.patch.object(update, "ROOT", root), \
                    mock.patch.object(update, "gitlab_release", return_value=release), \
                    mock.patch.object(update, "gitlab_digest", return_value=""):
                self.assertEqual(update.main(), 0)
            refreshed = json.loads(manifest.read_text(encoding="utf-8"))["packages"][0]
            self.assertEqual(refreshed["app_dls"], 7)


if __name__ == "__main__":
    unittest.main()

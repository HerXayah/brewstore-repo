import unittest

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


if __name__ == "__main__":
    unittest.main()

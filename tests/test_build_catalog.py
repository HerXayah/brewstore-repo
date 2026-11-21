import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.build_catalog as catalog


SHA_A = "a" * 64
SHA_B = "b" * 64


def package(name="foo", **extra):
    value = {
        "name": name,
        "title": name.title(),
        "url": "https://github.com/owner/repo",
        "asset": "foo.nro",
        "target": "sdmc:/switch/foo",
    }
    value.update(extra)
    return value


def release(tag, assets=None, *, draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets or [asset("foo.nro")],
        "published_at": "2026-11-20T00:00:00Z",
    }


def asset(name="foo.nro", *, digest=None, size=10):
    result = {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
        "url": f"https://example.invalid/{name}",
        "size": size,
    }
    if digest is not None:
        result["digest"] = "sha256:" + digest
    return result


def catalog_entry(name="foo", **extra):
    value = {
        "name": name,
        "url": "https://github.com/owner/repo",
        "asset": "foo.nro",
        "download_url": "https://example.invalid/foo.nro",
        "release_tag": "v1",
        "version": "v1",
        "sha256": SHA_A,
        "size": 10,
        "target": "sdmc:/switch/foo",
    }
    value.update(extra)
    return value


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.packages.mkdir()
        self.output = self.root / "catalog.json"
        self.patches = [
            mock.patch.object(catalog, "ROOT", self.root),
            mock.patch.object(catalog, "PACKAGES_DIR", self.packages),
            mock.patch.object(catalog, "OUTPUT", self.output),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        catalog._RELEASE_CACHE.clear()

    def write_manifest(self, folder, packages):
        directory = self.packages / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(
            json.dumps({"packages": packages}), encoding="utf-8"
        )

    def write_previous(self, packages):
        self.output.write_text(
            json.dumps({"schema_version": 1, "count": len(packages), "packages": packages}),
            encoding="utf-8",
        )


class ReleaseSelectionTests(Base):
    def test_drafts_and_prereleases_are_skipped(self):
        releases = [
            release("v3", draft=True),
            release("v2", prerelease=True),
            release("v1"),
        ]
        _, selected, tag, _, note = catalog.choose_release("github", releases, package())
        self.assertEqual(tag, "v1")
        self.assertEqual(selected["name"], "foo.nro")
        self.assertIsNone(note)

    def test_prerelease_opt_in(self):
        releases = [release("v2", prerelease=True), release("v1")]
        _, _, tag, _, _ = catalog.choose_release(
            "github", releases, package(prerelease=True)
        )
        self.assertEqual(tag, "v2")

    def test_non_newest_fallback_note(self):
        releases = [
            release("v2", assets=[asset("other.nro")]),
            release("v1", assets=[asset("foo.nro")]),
        ]
        _, _, tag, _, note = catalog.choose_release("github", releases, package())
        self.assertEqual(tag, "v1")
        self.assertIn("newest release has no matching asset", note)

    def test_template_fields_preferred(self):
        self.assertEqual(
            catalog.candidate_asset_hint(
                {"asset": "old.zip", "assetTemplate": "{release_tag}.zip"},
                "v2", "v2",
            ),
            "v2.zip",
        )
        self.assertEqual(
            catalog.resolved_strip_prefix(
                {"stripPrefix": "old/", "stripPrefixTemplate": "{asset_stem}/bundle/"},
                tag="v2", version="v2", asset_name="thing-v2.zip",
            ),
            "thing-v2/bundle/",
        )

    def test_openrac_template(self):
        self.assertEqual(
            catalog.candidate_asset_hint(
                {"asset": "{release_tag}.zip"}, "OpenRAC-Switch2-v0.3", "v0.3"
            ),
            "OpenRAC-Switch2-v0.3.zip",
        )
        self.assertEqual(
            catalog.resolved_strip_prefix(
                {"stripPrefix": "{asset_stem}/bundle/"},
                tag="OpenRAC-Switch2-v0.3",
                version="v0.3",
                asset_name="OpenRAC-Switch2-v0.3.zip",
            ),
            "OpenRAC-Switch2-v0.3/bundle/",
        )


class HashTests(Base):
    def test_digest_wins(self):
        selected = asset(digest=SHA_A)
        with mock.patch.object(catalog, "sidecar_sha256") as sidecar, \
             mock.patch.object(catalog, "hash_asset") as download:
            self.assertEqual(
                catalog.resolve_sha256("github", {"assets": []}, selected, "v1", 100),
                SHA_A,
            )
            sidecar.assert_not_called()
            download.assert_not_called()

    def test_sidecar_precedes_download(self):
        selected = asset()
        with mock.patch.object(catalog, "sidecar_sha256", return_value=SHA_B), \
             mock.patch.object(catalog, "hash_asset") as download:
            self.assertEqual(
                catalog.resolve_sha256("github", {"assets": []}, selected, "v1", 100),
                SHA_B,
            )
            download.assert_not_called()

    def test_download_when_nothing_else(self):
        selected = asset()
        with mock.patch.object(catalog, "sidecar_sha256", return_value=None), \
             mock.patch.object(catalog, "hash_asset", return_value=SHA_A) as download:
            self.assertEqual(
                catalog.resolve_sha256("github", {"assets": []}, selected, "v1", 100),
                SHA_A,
            )
            download.assert_called_once()

    def test_reuse_requires_size_and_matching_entry(self):
        selected = asset(size=10)
        previous = catalog_entry(size=10)
        with mock.patch.object(catalog, "sidecar_sha256", return_value=None), \
             mock.patch.object(catalog, "hash_asset", return_value=SHA_B) as download:
            self.assertEqual(
                catalog.resolve_sha256(
                    "github", {"assets": []}, selected, "v1", 100, previous=previous
                ),
                SHA_A,
            )
            download.assert_not_called()

    def test_no_reuse_without_size(self):
        selected = asset(size=None)
        previous = catalog_entry(size=None)
        with mock.patch.object(catalog, "sidecar_sha256", return_value=None), \
             mock.patch.object(catalog, "hash_asset", return_value=SHA_B) as download:
            self.assertEqual(
                catalog.resolve_sha256(
                    "github", {"assets": []}, selected, "v1", 100, previous=previous
                ),
                SHA_B,
            )
            download.assert_called_once()

    def test_rehash_skips_sidecar_and_forces_download(self):
        selected = asset()
        with mock.patch.object(catalog, "sidecar_sha256") as sidecar, \
             mock.patch.object(catalog, "hash_asset", return_value=SHA_B) as download:
            self.assertEqual(
                catalog.resolve_sha256(
                    "github", {"assets": []}, selected, "v1", 100, rehash=True
                ),
                SHA_B,
            )
            sidecar.assert_not_called()
            download.assert_called_once()

    def test_rehash_still_trusts_api_digest(self):
        selected = asset(digest=SHA_A)
        with mock.patch.object(catalog, "hash_asset") as download:
            self.assertEqual(
                catalog.resolve_sha256(
                    "github", {"assets": []}, selected, "v1", 100, rehash=True
                ),
                SHA_A,
            )
            download.assert_not_called()


class ValidationTests(Base):
    def test_paths(self):
        catalog.validate_sdmc_path("sdmc:/switch/foo", "target")
        for bad in ("sdmc:/switch/../foo", "sdmc:/switch\\foo", "sdmc:/switch/a:b"):
            with self.assertRaises(catalog.BuildError):
                catalog.validate_sdmc_path(bad, "target")
        for bad in ("../foo", "/foo", "a/../b", "a\\b", "a:b"):
            with self.assertRaises(catalog.BuildError):
                catalog.validate_strip_prefix(bad)

    def test_sensitive_targets_and_opt_in(self):
        with self.assertRaises(catalog.BuildError):
            catalog.validate_package_source(package(target="sdmc:/"), Path("m.json"))
        catalog.validate_package_source(
            package(target="sdmc:/", allowUnsafeTarget=True), Path("m.json")
        )
        for target in ("sdmc:/atmosphere", "sdmc:/bootloader", "sdmc:/config/sys-x"):
            with self.assertRaises(catalog.BuildError):
                catalog.validate_package_source(package(target=target), Path("m.json"))
            with self.assertRaises(catalog.BuildError):
                catalog.validate_package_source(
                    package(target=target, allowUnsafeTarget=True), Path("m.json")
                )

    def test_contract_limits(self):
        def base(**extra):
            value = {"name": "foo", "url": "https://github.com/a/b",
                     "target": "sdmc:/switch/foo"}
            value.update(extra)
            return value

        def valid(**extra):
            catalog.validate_package_source(base(**extra), Path("m.json"))

        def invalid(**extra):
            with self.assertRaises(catalog.BuildError):
                catalog.validate_package_source(base(**extra), Path("m.json"))

        valid(name="a" * 63)
        invalid(name="a" * 64)
        for bad in ("foo+bar", "foo@bar"):
            invalid(name=bad)
        valid(title="a" * 95)
        invalid(title="a" * 96)
        valid(author="a" * 63)
        invalid(author="a" * 64)
        valid(description="a" * 511)
        invalid(description="a" * 512)
        valid(icon="https://" + "a" * 503)
        invalid(icon="https://" + "a" * 504)
        invalid(icon="http://example.com/icon.png")
        valid(url="https://" + "a" * 503)
        invalid(url="https://" + "a" * 504)
        invalid(url="http://example.com")
        valid(version="a" * 31)
        invalid(version="a" * 32)
        valid(release_tag="a" * 63)
        invalid(release_tag="a" * 64)
        valid(asset="a" * 127)
        invalid(asset="a" * 128)
        invalid(asset="a/b")
        valid(target="sdmc:/switch/" + "a" * 242)
        invalid(target="sdmc:/switch/" + "a" * 243)
        valid(marker="sdmc:/" + "a" * 505)
        invalid(marker="sdmc:/" + "a" * 506)
        valid(stripPrefix="a" * 127)
        invalid(stripPrefix="a" * 128)
        valid(published="1" * 24)
        invalid(published="1" * 25)
        valid(size=0)
        invalid(size=-1)

    def test_resolved_contract_limits(self):
        entry = catalog_entry()
        catalog.validate_resolved_package(entry)
        for field, value in (
            ("download_url", "https://" + "a" * 1016),
            ("sha256", "A" * 64),
            ("release_tag", "a" * 64),
        ):
            broken = dict(entry)
            broken[field] = value
            with self.assertRaises(catalog.BuildError):
                catalog.validate_resolved_package(broken)

    def test_duplicate_names_case_insensitive(self):
        self.write_manifest("Foo", [package("Foo")])
        self.write_manifest("foo", [package("foo")])
        errors = catalog.validate_sources()
        self.assertTrue(any("duplicate package name" in error for error in errors))

    def test_folder_name_must_match_package_name(self):
        self.write_manifest("wrong", [package("foo")])
        errors = catalog.validate_sources()
        self.assertTrue(any("does not match package name" in error for error in errors))

    def test_validate_only_does_not_write(self):
        self.write_manifest("foo", [package("foo")])
        errors = catalog.validate_sources()
        self.assertEqual(errors, [])
        self.assertFalse(self.output.exists())


class BuildTests(Base):
    def fake_resolve(self, pkg, max_hash_bytes, previous=None):
        name = pkg["name"]
        return catalog_entry(name=name, target=pkg.get("target", f"sdmc:/switch/{name}")), None

    def test_failure_keeps_previous_and_omits_new(self):
        self.write_manifest("a", [package("a")])
        self.write_manifest("b", [package("b")])
        self.write_previous([catalog_entry("a")])
        calls = []

        def resolve(pkg, max_hash_bytes, previous=None):
            calls.append(pkg["name"])
            if pkg["name"] == "b":
                raise catalog.BuildError("boom")
            return catalog_entry("a"), None

        with mock.patch.object(catalog, "resolve_package", side_effect=resolve):
            result, errors, notes, counts = catalog.build_catalog(
                max_hash_bytes=100, strict=False
            )
        self.assertEqual([entry["name"] for entry in result["packages"]], ["a"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(counts["kept_previous"], 0)
        self.assertTrue(any("boom" in error for error in errors))

    def test_only_keeps_source_packages_and_drops_orphans(self):
        self.write_manifest("a", [package("a")])
        self.write_manifest("b", [package("b")])
        self.write_previous(
            [catalog_entry("a"), catalog_entry("b"), catalog_entry("orphan")]
        )
        with mock.patch.object(catalog, "resolve_package", side_effect=self.fake_resolve):
            result, _, _, _ = catalog.build_catalog(
                max_hash_bytes=100, strict=False, only={"a"}
            )
        names = [entry["name"] for entry in result["packages"]]
        self.assertEqual(names, ["a", "b"])
        self.assertEqual(result["count"], 2)

    def test_failure_keeps_previous_entry(self):
        self.write_manifest("a", [package("a")])
        old = catalog_entry("a", version="old", download_url="https://example.invalid/old.nro")
        self.write_previous([old])
        with mock.patch.object(
            catalog, "resolve_package", side_effect=catalog.BuildError("boom")
        ):
            result, errors, _, counts = catalog.build_catalog(
                max_hash_bytes=100, strict=False
            )
        self.assertEqual(result["packages"], [old])
        self.assertEqual(counts["kept_previous"], 1)
        self.assertTrue(errors)

    def test_strict_fails_on_package_error(self):
        self.write_manifest("a", [package("a")])
        self.write_manifest("b", [package("b")])
        with mock.patch.object(catalog, "resolve_package", side_effect=self.fake_resolve):
            with mock.patch.object(
                catalog, "resolve_package", side_effect=catalog.BuildError("boom")
            ):
                with self.assertRaises(catalog.BuildError):
                    catalog.build_catalog(max_hash_bytes=100, strict=True)

    def test_output_is_stable_and_counted(self):
        self.write_manifest("a", [package("a")])
        self.write_manifest("b", [package("b")])
        with mock.patch.object(catalog, "resolve_package", side_effect=self.fake_resolve):
            first, _, _, _ = catalog.build_catalog(max_hash_bytes=100, strict=False)
            second, _, _, _ = catalog.build_catalog(max_hash_bytes=100, strict=False)
        self.assertEqual(first, second)
        self.assertEqual(first["count"], len(first["packages"]))
        self.assertEqual(
            json.dumps(first, indent=2, ensure_ascii=False),
            json.dumps(second, indent=2, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()

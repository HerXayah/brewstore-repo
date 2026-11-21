#!/usr/bin/env python3
"""Propose pinned-release bumps as JSON lines for the bump workflow.

Only manifests with a non-latest `release` value are considered. The script
prints one JSON object per newer stable tag and edits only the manifest's
`release` field. `--dry-run` prints the proposals without writing.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

import re

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_catalog as catalog  # noqa: E402


def release_url(package_url: str, tag: str) -> str:
    base = package_url.rstrip("/")
    if base.startswith("https://gitlab.com/"):
        return f"{base}/-/releases/{tag}"
    return f"{base}/releases/tag/{tag}"


def update_release_text(text: str, name: str, old: str, new: str) -> str:
    """Replace only the package's release value, preserving all formatting."""
    name_match = re.search(r'"name"\s*:\s*' + re.escape(json.dumps(name)), text)
    if not name_match:
        raise ValueError(f"package {name!r} not found")
    tail = text[name_match.end():]
    next_name = re.search(r'"name"\s*:', tail)
    block_end = name_match.end() + (next_name.start() if next_name else len(tail))
    block = text[name_match.start():block_end]
    pattern = re.compile(r'("release"\s*:\s*)' + re.escape(json.dumps(old)))
    updated, count = pattern.subn(lambda match: match.group(1) + json.dumps(new), block, count=1)
    if count != 1:
        raise ValueError(f"release {old!r} not found for package {name!r}")
    result = text[:name_match.start()] + updated + text[block_end:]
    # Fail closed if the text no longer parses or still has the old value.
    parsed = json.loads(result)
    for package in parsed.get("packages", []):
        if package.get("name") == name:
            if package.get("release") != new:
                raise ValueError("release replacement did not take effect")
            break
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="only this package name")
    parser.add_argument("--dry-run", action="store_true", help="print proposals without writing manifests")
    parser.add_argument(
        "--max-hash-bytes",
        type=int,
        default=int(os.environ.get("BREWSTORE_HASH_MAX_BYTES", catalog.DEFAULT_HASH_MAX_BYTES)),
        help="maximum asset size to download when no published SHA-256 is available",
    )
    args = parser.parse_args()
    if args.max_hash_bytes <= 0:
        parser.error("--max-hash-bytes must be positive")

    for manifest_path in sorted(catalog.PACKAGES_DIR.glob("*/manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            catalog.log(f"{manifest_path}: cannot read manifest: {exc}")
            continue
        packages = data.get("packages", [])
        if not isinstance(packages, list):
            continue
        for package in packages:
            if not isinstance(package, dict):
                continue
            name = package.get("name")
            pin = str(package.get("release") or "latest").strip()
            if not isinstance(name, str) or not pin or pin.lower() == "latest":
                continue
            if args.only and name != args.only:
                continue
            try:
                provider, releases = catalog.fetch_releases(package["url"])
                candidate = copy.deepcopy(package)
                candidate["release"] = "latest"
                candidate.pop("prerelease", None)
                release, asset, tag, version, _ = catalog.choose_release(
                    provider, releases, candidate
                )
                if tag == pin:
                    continue
                digest = catalog.resolve_sha256(
                    provider, release, asset, tag, args.max_hash_bytes
                )
                old_pin = pin
                package["release"] = tag
                if not args.dry_run:
                    original = manifest_path.read_text(encoding="utf-8")
                    updated = update_release_text(original, name, old_pin, tag)
                    catalog.atomic_write(manifest_path, updated.encode("utf-8"))
                print(json.dumps({
                    "name": name,
                    "old": old_pin,
                    "new": tag,
                    "asset": asset["name"],
                    "sha256": digest,
                    "release_url": release_url(package["url"], tag),
                }, ensure_ascii=False))
            except Exception as exc:
                catalog.log(f"{name}: bump lookup failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

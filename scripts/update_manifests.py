#!/usr/bin/env python3
"""Refresh package manifests from their latest GitHub release."""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def request_json(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "brewstore-catalog",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def select_asset(assets, hint):
    if hint:
        for asset in assets:
            if asset["name"].lower().endswith(hint.lower()):
                return asset
        return None
    for asset in assets:
        if asset["name"].lower().endswith(".nro"):
            return asset
    for asset in assets:
        name = asset["name"].lower()
        if name.endswith(".zip") and not any(
            word in name for word in ("debug", "symbols", "source")
        ):
            return asset
    return None


def digest_hex(asset):
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return digest.lower()
    return ""


def main():
    changed = 0
    for path in sorted((ROOT / "packages").glob("*/manifest.json")):
        data = json.loads(path.read_text())
        for package in data.get("packages", []):
            match = re.match(r"https://github\.com/([^/]+)/([^/]+)", package.get("url", ""))
            if not match:
                continue
            owner, repo = match.group(1), match.group(2)
            try:
                releases = request_json(
                    f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=1"
                )
            except Exception as error:
                print(f"{package['name']}: release lookup failed: {error}", file=sys.stderr)
                continue
            if not releases:
                print(f"{package['name']}: no releases")
                continue
            release = releases[0]
            asset = select_asset(release.get("assets", []), package.get("asset", ""))
            if not asset:
                print(f"{package['name']}: no matching asset")
                continue
            version = release.get("tag_name", "")
            sha256 = digest_hex(asset)
            if not version:
                continue
            if package.get("version") != version:
                package["version"] = version
                changed += 1
            if sha256 and package.get("sha256") != sha256:
                package["sha256"] = sha256
                changed += 1
            print(f"{package['name']}: {version} {sha256 or '(no digest)'}")
        path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"updated fields: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

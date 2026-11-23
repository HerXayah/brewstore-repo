#!/usr/bin/env python3
"""Refresh package manifests from their latest GitHub release."""
import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_APP_DOWNLOADS = (1 << 64) - 1  # Sphaira stores app_dls as u64.


def request_json(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "brewstore-catalog",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # The GitHub token is invalid on GitLab and makes its API return 401.
    if TOKEN and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def request_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "brewstore-catalog"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def select_asset(assets, hint):
    if hint:
        for asset in assets:
            if asset["name"].lower().endswith(hint.lower()):
                return asset
        return None
    for asset in assets:
        if asset["name"].lower().endswith(".nro"):
            return asset
    for suffix in (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz"):
        for asset in assets:
            name = asset["name"].lower()
            if name.endswith(suffix) and not any(
                word in name for word in ("debug", "symbols", "source")
            ):
                return asset
    return None


def normalized_version(tag):
    """Release tags are not always semver; Brewstore compares versions."""
    for prefix in ("switch2-v", "switch2-"):
        if tag.lower().startswith(prefix):
            return tag[len(prefix):]
    return tag


def expand_template(value, *, tag, version, asset_name="", asset_stem=""):
    return value.format_map({
        "release_tag": tag,
        "tag": tag,
        "version": version,
        "asset": asset_name,
        "asset_stem": asset_stem or PurePosixPath(asset_name).stem,
    })


def asset_hint(package, tag, version):
    hint = str(package.get("assetTemplate") or package.get("asset") or "")
    return expand_template(hint, tag=tag, version=version) if "{" in hint else hint


def strip_prefix(package, tag, version, asset_name):
    raw = str(package.get("stripPrefixTemplate") or "")
    if not raw:
        return None
    return expand_template(raw, tag=tag, version=version, asset_name=asset_name)


def digest_hex(asset):
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return digest.lower()
    return ""


def update_download_count(package, asset):
    """Copy the selected GitHub asset's count when it fits Sphaira's u64 field."""
    if "app_dls" not in package:
        return False
    count = asset.get("download_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= MAX_APP_DOWNLOADS
    ):
        return False
    current = package.get("app_dls")
    if isinstance(current, int) and not isinstance(current, bool) and current == count:
        return False
    package["app_dls"] = count
    return True


def published_epoch(value):
    if not value:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def gitlab_release(url):
    project = url[len("https://gitlab.com/"):]
    project = urllib.parse.quote(project, safe="")
    releases = request_json(
        f"https://gitlab.com/api/v4/projects/{project}/releases?per_page=1"
    )
    if not releases:
        return None
    release = releases[0]
    links = release.get("assets", {}).get("links", [])
    assets = [
        {"name": link.get("name", ""), "url": link.get("direct_asset_url") or link.get("url", "")}
        for link in links
    ]
    return release.get("tag_name", ""), assets, published_epoch(
        release.get("released_at") or release.get("created_at"))


def gitlab_digest(assets, name):
    wanted = name + ".sha256"
    for asset in assets:
        if asset["name"] == wanted and asset["url"]:
            text = request_text(asset["url"]).strip().split()
            if text and re.fullmatch(r"[0-9a-fA-F]{64}", text[0]):
                return text[0].lower()
    return ""


def main():
    changed = 0
    for path in sorted((ROOT / "packages").glob("*/manifest.json")):
        data = json.loads(path.read_text())
        for package in data.get("packages", []):
            pinned = str(package.get("release") or "latest").strip()
            if pinned and pinned.lower() != "latest":
                print(f"{package['name']}: pinned to {pinned}; not updating")
                continue
            url = package.get("url", "")
            if url.startswith("https://gitlab.com/"):
                try:
                    release = gitlab_release(url)
                except Exception as error:
                    print(f"{package['name']}: GitLab lookup failed: {error}", file=sys.stderr)
                    continue
                if not release:
                    print(f"{package['name']}: no releases")
                    continue
                tag, assets, published = release
                version = normalized_version(tag)
                asset = select_asset(assets, asset_hint(package, tag, version))
                if not asset:
                    print(f"{package['name']}: no matching asset")
                    continue
                sha256 = gitlab_digest(assets, asset["name"])
            else:
                match = re.match(r"https://github\.com/([^/]+)/([^/]+)", url)
                if not match:
                    continue
                owner, repo = match.group(1), match.group(2)
                try:
                    releases = request_json(
                        f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=10"
                    )
                except Exception as error:
                    print(f"{package['name']}: release lookup failed: {error}", file=sys.stderr)
                    continue
                if not releases:
                    print(f"{package['name']}: no releases")
                    continue
                release = None
                asset = None
                for candidate in releases:
                    candidate_tag = candidate.get("tag_name", "")
                    candidate_version = normalized_version(candidate_tag)
                    selected = select_asset(
                        candidate.get("assets", []),
                        asset_hint(package, candidate_tag, candidate_version),
                    )
                    if selected:
                        release = candidate
                        asset = selected
                        break
                if not release:
                    print(f"{package['name']}: no matching asset")
                    continue
                tag = release.get("tag_name", "")
                version = normalized_version(tag)
                published = published_epoch(release.get("published_at"))
                sha256 = digest_hex(asset)
                if update_download_count(package, asset):
                    changed += 1
            if not version:
                continue
            if package.get("assetTemplate"):
                if package.get("asset") != asset["name"]:
                    package["asset"] = asset["name"]
                    changed += 1
            templated_strip = strip_prefix(package, tag, version, asset["name"])
            if templated_strip is not None and package.get("stripPrefix") != templated_strip:
                package["stripPrefix"] = templated_strip
                changed += 1
            if package.get("version") != version:
                package["version"] = version
                changed += 1
            if sha256 and package.get("sha256") != sha256:
                package["sha256"] = sha256
                changed += 1
            if published and package.get("published") != published:
                package["published"] = published
                changed += 1
            print(f"{package['name']}: {version} {sha256 or '(no digest)'} "
                  f"{published or '(no published timestamp)'}")
        encoded = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if encoded != path.read_text(encoding="utf-8"):
            path.write_text(encoded, encoding="utf-8")
    print(f"updated fields: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

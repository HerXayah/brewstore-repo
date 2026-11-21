#!/usr/bin/env python3
"""Build Brewstore's single-file catalogue from packages/*/manifest.json.

The source manifests remain declarative. This script resolves each package to a
specific release asset and a SHA-256 for that exact asset, then writes a stable,
versioned catalog.json. A package that cannot be refreshed keeps its previous
catalogue entry when one is available.

Environment:
    GITHUB_TOKEN                 Optional token, sent only to api.github.com.
    BREWSTORE_HASH_MAX_BYTES     Maximum asset size to download for hashing
                                 when no digest/sidecar exists (default 512 MiB).

Examples:
    python3 scripts/build_catalog.py
    python3 scripts/build_catalog.py --strict
    python3 scripts/build_catalog.py --check --strict
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"
OUTPUT = ROOT / "catalog.json"
SCHEMA_VERSION = 1
USER_AGENT = "brewstore-catalog-builder/1.0"
TIMEOUT = 40
MAX_SIDECAR_BYTES = 1024 * 1024
DEFAULT_HASH_MAX_BYTES = 512 * 1024 * 1024
TOKEN = os.environ.get("GITHUB_TOKEN", "")

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# A run-local cache avoids querying the same repository more than once.
_RELEASE_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_HASH_DOWNLOAD_COUNT = 0
_LAST_NOTES: list[str] = []
_LAST_COUNTS: dict[str, int] = {}


class BuildError(Exception):
    """A package could not be safely resolved into a catalogue entry."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def request_bytes(url: str, *, max_bytes: int, api: bool = False) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        # Never send the GitHub token to GitLab, asset hosts, or arbitrary URLs.
        if TOKEN and url.startswith("https://api.github.com/"):
            headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise BuildError(
                    f"response is {int(length)} bytes; limit is {max_bytes}"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BuildError(f"response exceeds {max_bytes}-byte limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise BuildError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BuildError(f"request failed for {url}: {exc.reason}") from exc


def request_json(url: str, *, api: bool = True) -> Any:
    try:
        return json.loads(request_bytes(url, max_bytes=16 * 1024 * 1024, api=api))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid JSON response from {url}: {exc}") from exc


def normalized_version(tag: str) -> str:
    """Keep in sync with scripts/update_manifests.py."""
    for prefix in ("switch2-v", "switch2-"):
        if tag.lower().startswith(prefix):
            return tag[len(prefix):]
    return tag


def published_epoch(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def parse_github_repo(url: str) -> tuple[str, str] | None:
    match = re.match(r"^https://github\.com/([^/]+)/([^/?#]+)", url.rstrip("/"))
    if not match:
        return None
    owner, repo = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def fetch_releases(repo_url: str) -> tuple[str, list[dict[str, Any]]]:
    """Return provider and newest release records for a GitHub/GitLab repo."""
    github = parse_github_repo(repo_url)
    if github:
        owner, repo = github
        key = ("github", f"{owner}/{repo}")
        if key not in _RELEASE_CACHE:
            api_url = (
                f"https://api.github.com/repos/{urllib.parse.quote(owner)}/"
                f"{urllib.parse.quote(repo)}/releases?per_page=10"
            )
            releases = request_json(api_url)
            if not isinstance(releases, list):
                raise BuildError(f"GitHub returned an unexpected release list for {repo_url}")
            _RELEASE_CACHE[key] = releases
        return "github", _RELEASE_CACHE[key]

    prefix = "https://gitlab.com/"
    if repo_url.startswith(prefix):
        project = repo_url[len(prefix):].split("?", 1)[0].split("#", 1)[0].strip("/")
        if not project:
            raise BuildError(f"invalid GitLab project URL: {repo_url}")
        key = ("gitlab", project)
        if key not in _RELEASE_CACHE:
            encoded = urllib.parse.quote(project, safe="")
            api_url = f"https://gitlab.com/api/v4/projects/{encoded}/releases?per_page=10"
            releases = request_json(api_url)
            if not isinstance(releases, list):
                raise BuildError(f"GitLab returned an unexpected release list for {repo_url}")
            _RELEASE_CACHE[key] = releases
        return "gitlab", _RELEASE_CACHE[key]

    raise BuildError("only GitHub and GitLab release repositories are supported")


def release_tag(release: dict[str, Any]) -> str:
    return str(release.get("tag_name") or release.get("tag") or "")


def release_assets(provider: str, release: dict[str, Any]) -> list[dict[str, Any]]:
    if provider == "github":
        raw_assets = release.get("assets", [])
        result = []
        for asset in raw_assets:
            if not isinstance(asset, dict):
                continue
            result.append({
                "name": str(asset.get("name") or ""),
                "url": str(asset.get("browser_download_url") or ""),
                "digest": str(asset.get("digest") or ""),
                "size": asset.get("size"),
            })
        return result

    links = release.get("assets", {}).get("links", [])
    result = []
    for asset in links:
        if not isinstance(asset, dict):
            continue
        result.append({
            "name": str(asset.get("name") or ""),
            "url": str(asset.get("direct_asset_url") or asset.get("url") or ""),
            "digest": "",
            "size": asset.get("size"),
        })
    return result


def expand_template(
    value: str, *, tag: str, version: str, extra: dict[str, str] | None = None
) -> str:
    variables = {"release_tag": tag, "tag": tag, "version": version}
    if extra:
        variables.update(extra)
    try:
        return value.format_map(variables)
    except (KeyError, ValueError) as exc:
        raise BuildError(f"invalid template {value!r}: {exc}") from exc


def candidate_asset_hint(package: dict[str, Any], tag: str, version: str) -> str:
    hint = str(package.get("assetTemplate") or package.get("asset") or "")
    if "{" in hint:
        return expand_template(hint, tag=tag, version=version)
    return hint


def select_asset(assets: list[dict[str, Any]], hint: str) -> dict[str, Any] | None:
    if hint:
        wanted = hint.casefold()
        for asset in assets:
            if asset["name"].casefold() == wanted:
                return asset
        for asset in assets:
            if asset["name"].casefold().endswith(wanted):
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


def choose_release(
    provider: str, releases: list[dict[str, Any]], package: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str, str, str | None]:
    configured_release = str(package.get("release") or "latest").strip()
    allow_prerelease = package.get("prerelease") is True
    eligible: list[dict[str, Any]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not allow_prerelease:
            continue
        eligible.append(release)

    chosen: tuple[dict[str, Any], dict[str, Any], str, str] | None = None
    for release in eligible:
        tag = release_tag(release)
        if not tag:
            continue
        if configured_release.lower() != "latest":
            if configured_release not in (tag, normalized_version(tag)):
                continue
        version = normalized_version(tag)
        hint = candidate_asset_hint(package, tag, version)
        asset = select_asset(release_assets(provider, release), hint)
        if asset and asset.get("url"):
            chosen = (release, asset, tag, version)
            break

    if chosen is None:
        raise BuildError("no release with a matching downloadable asset was found")

    note = None
    if configured_release.lower() == "latest" and eligible:
        newest_tag = release_tag(eligible[0])
        if newest_tag and chosen[2] != newest_tag:
            note = (f"{package.get('name', '?')}: newest release has no matching asset, "
                    f"using {chosen[2]}")
    return (*chosen, note)


def valid_sha256(value: str) -> str | None:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    return value if SHA256_RE.fullmatch(value) else None


def parse_checksum_text(data: bytes, asset_name: str, *, tied_sidecar: bool) -> str | None:
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None

    found: list[tuple[str, str | None]] = []
    for line in text.splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{64})(?:\s+\*?(.+?))?\s*$", line)
        if match:
            found.append((match.group(1).lower(), match.group(2)))

    wanted = PurePosixPath(asset_name.replace("\\", "/")).name.casefold()
    for digest, filename in found:
        if filename and PurePosixPath(filename.strip().replace("\\", "/")).name.casefold() == wanted:
            return digest
    if tied_sidecar and len(found) == 1 and not found[0][1]:
        return found[0][0]
    return None


def sidecar_sha256(assets: list[dict[str, Any]], asset_name: str) -> str | None:
    stem = PurePosixPath(asset_name).stem
    tied_names = {
        (asset_name + ".sha256").casefold(),
        (asset_name + ".sha256sum").casefold(),
        (stem + ".sha256").casefold(),
    }
    checksum_names = {"sha256sums", "sha256sums.txt", "sha256sum.txt", "checksums.txt"}

    for sidecar in assets:
        name = sidecar["name"].casefold()
        tied = name in tied_names
        generic = name in checksum_names
        if not (tied or generic) or not sidecar.get("url"):
            continue
        try:
            data = request_bytes(sidecar["url"], max_bytes=MAX_SIDECAR_BYTES, api=False)
        except BuildError as exc:
            log(f"  checksum sidecar {sidecar['name']} unavailable: {exc}")
            continue
        digest = parse_checksum_text(data, asset_name, tied_sidecar=tied)
        if digest:
            return digest
    return None


def hash_asset(asset: dict[str, Any], max_bytes: int) -> str:
    size = asset.get("size")
    if isinstance(size, int) and size > max_bytes:
        raise BuildError(
            f"asset is {size} bytes and has no published digest/checksum; "
            f"hash-download limit is {max_bytes} bytes"
        )

    # Stream rather than retaining the archive in memory.
    headers = {"User-Agent": USER_AGENT}
    request = urllib.request.Request(asset["url"], headers=headers)
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise BuildError(
                    f"asset is {int(length)} bytes and has no published digest/checksum; "
                    f"hash-download limit is {max_bytes} bytes"
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BuildError(f"asset exceeds hash-download limit of {max_bytes} bytes")
                digest.update(chunk)
    except urllib.error.HTTPError as exc:
        raise BuildError(f"asset download returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BuildError(f"asset download failed: {exc.reason}") from exc
    return digest.hexdigest()


def resolve_sha256(
    provider: str,
    release: dict[str, Any],
    selected_asset: dict[str, Any],
    tag: str,
    max_hash_bytes: int,
    previous: dict[str, Any] | None = None,
    rehash: bool = False,
) -> str:
    global _HASH_DOWNLOAD_COUNT
    digest = valid_sha256(str(selected_asset.get("digest") or ""))
    if digest:
        return digest

    if rehash:
        log("  rehash requested; skipping sidecar and reuse, downloading asset")
        _HASH_DOWNLOAD_COUNT += 1
        return hash_asset(selected_asset, max_hash_bytes)

    all_assets = release_assets(provider, release)
    digest = sidecar_sha256(all_assets, selected_asset["name"])
    if digest:
        return digest

    selected_size = selected_asset.get("size")
    if previous and isinstance(selected_size, int):
        previous_sha = valid_sha256(str(previous.get("sha256") or ""))
        if (
            previous_sha
            and previous.get("release_tag") == tag
            and previous.get("asset") == selected_asset["name"]
            and previous.get("download_url") == selected_asset["url"]
            and previous.get("size") == selected_size
        ):
            log("  reusing SHA-256 from the previous catalogue entry")
            return previous_sha

    log("  no release digest or checksum sidecar; downloading asset to calculate SHA-256")
    _HASH_DOWNLOAD_COUNT += 1
    return hash_asset(selected_asset, max_hash_bytes)


def validate_sdmc_path(value: Any, field: str, *, allow_root: bool = True) -> None:
    if not isinstance(value, str) or not value:
        raise BuildError(f"{field} must be a non-empty sdmc:/ path")
    if not value.startswith("sdmc:/"):
        raise BuildError(f"{field} must start with 'sdmc:/'")
    if "\\" in value or "\x00" in value:
        raise BuildError(f"{field} contains a forbidden character")
    tail = value[len("sdmc:/"):]
    if not tail and allow_root:
        return
    if not tail:
        raise BuildError(f"{field} cannot be the SD card root")
    parts = tail.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BuildError(f"{field} contains an empty, '.' or '..' path component")
    if any(":" in part for part in parts):
        raise BuildError(f"{field} contains a drive/scheme-like path component")


def validate_strip_prefix(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BuildError("stripPrefix must be a string")
    if not value:
        return ""
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise BuildError("stripPrefix must be a relative archive path")
    parts = value.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BuildError("stripPrefix contains an empty, '.' or '..' path component")
    if any(":" in part for part in parts):
        raise BuildError("stripPrefix contains a drive/scheme-like component")
    return value


def resolved_strip_prefix(
    package: dict[str, Any], *, tag: str, version: str, asset_name: str
) -> str:
    raw = str(package.get("stripPrefixTemplate") or package.get("stripPrefix") or "")
    if "{" in raw:
        raw = expand_template(
            raw,
            tag=tag,
            version=version,
            extra={
                "asset": asset_name,
                "asset_stem": PurePosixPath(asset_name).stem,
            },
        )
    return validate_strip_prefix(raw)


def sensitive_target(target: str) -> bool:
    return target == "sdmc:/"


def checked_text(package: dict[str, Any], field: str, limit: int, *, required: bool = False) -> str:
    value = package.get(field)
    if value is None:
        if required:
            raise BuildError(f"{package.get('name', '?')}: missing required {field}")
        return ""
    if not isinstance(value, str):
        raise BuildError(f"{package.get('name', '?')}: {field} must be a string")
    if len(value) > limit:
        raise BuildError(f"{package.get('name', '?')}: {field} is longer than {limit} characters")
    return value


def validate_target(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise BuildError("target must be a non-empty sdmc:/ path")
    if len(value) > 255:
        raise BuildError("target is longer than 255 characters")
    if value not in ("sdmc:/", "sdmc:/switch") and not value.startswith("sdmc:/switch/"):
        raise BuildError("target must be sdmc:/, sdmc:/switch, or sdmc:/switch/<path>")
    validate_sdmc_path(value, "target")


def validate_marker(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise BuildError("marker must be a non-empty sdmc:/ path")
    if len(value) > 511:
        raise BuildError("marker is longer than 511 characters")
    validate_sdmc_path(value, "marker", allow_root=False)


def validate_https(value: Any, field: str, limit: int) -> None:
    if value is None or value == "":
        return
    text = checked_text({field: value}, field, limit)
    if not text.startswith("https://"):
        raise BuildError(f"{field} must use https://")


def validate_plain_asset(package: dict[str, Any], field: str) -> None:
    value = checked_text(package, field, 127)
    if not value:
        return
    if value in (".", "..") or any(char in value for char in ("/", "\\", ":")) or "\x00" in value:
        raise BuildError(f"{field} must be a plain filename")


def validate_package_template(package: dict[str, Any], field: str, *, prefix: bool) -> None:
    value = checked_text(package, field, 127)
    if not value or "{" not in value:
        return
    extra = {"asset": "asset.zip", "asset_stem": "asset"} if prefix else None
    expand_template(value, tag="tag", version="version", extra=extra)


def validate_package_source(package: Any, manifest_path: Path, *, allow_sensitive: bool = False) -> dict[str, Any]:
    if not isinstance(package, dict):
        raise BuildError("package entry must be a JSON object")
    name = package.get("name")
    if not isinstance(name, str) or not PACKAGE_NAME_RE.fullmatch(name):
        raise BuildError(f"invalid or missing package name: {name!r}")
    checked_text(package, "title", 95)
    checked_text(package, "author", 63)
    checked_text(package, "description", 511)
    checked_text(package, "version", 31)
    checked_text(package, "release_tag", 63)
    checked_text(package, "sha256", 64)
    validate_https(package.get("icon"), "icon", 511)
    url = checked_text(package, "url", 511, required=True)
    if not url.startswith("https://"):
        raise BuildError(f"{name}: required 'url' must be an HTTPS repository URL")
    validate_plain_asset(package, "asset")
    validate_package_template(package, "assetTemplate", prefix=False)
    validate_package_template(package, "stripPrefixTemplate", prefix=True)
    validate_strip_prefix(package.get("stripPrefix", ""))
    if "stripPrefix" in package and len(str(package["stripPrefix"])) > 127:
        raise BuildError(f"{name}: stripPrefix is longer than 127 characters")
    if "target" in package:
        validate_target(package["target"])
        if (sensitive_target(package["target"]) and
                not allow_sensitive and package.get("allowUnsafeTarget") is not True):
            raise BuildError(
                f"{name}: target {package['target']!r} is a sensitive system path; "
                "set allowUnsafeTarget only as a maintainer"
            )
    if "marker" in package:
        validate_marker(package["marker"])
    published = package.get("published")
    if published is not None:
        if isinstance(published, bool) or not isinstance(published, (int, str)):
            raise BuildError(f"{name}: published must be an integer epoch")
        if isinstance(published, str) and not re.fullmatch(r"[0-9]{1,24}", published):
            raise BuildError(f"{name}: published must be an integer epoch")
        if isinstance(published, int) and (published < 0 or len(str(published)) > 24):
            raise BuildError(f"{name}: published must be a non-negative 24-character epoch")
    size = package.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise BuildError(f"{name}: size must be a non-negative integer")
    return package


def validate_resolved_package(package: dict[str, Any]) -> None:
    validate_package_source(package, Path("catalog.json"), allow_sensitive=True)
    name = package["name"]
    for field in ("asset", "download_url", "release_tag", "sha256"):
        if not package.get(field):
            raise BuildError(f"{name}: resolved entry is missing {field}")
    download_url = package["download_url"]
    if not isinstance(download_url, str) or not download_url.startswith("https://") or len(download_url) > 1023:
        raise BuildError(f"{name}: download_url must be https and at most 1023 characters")
    sha = package["sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise BuildError(f"{name}: sha256 must be 64 lowercase hex characters")
    if not isinstance(package.get("release_tag"), str) or len(package["release_tag"]) > 63:
        raise BuildError(f"{name}: release_tag is too long")
    if package.get("size") is not None:
        size = package["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BuildError(f"{name}: size must be a non-negative integer")


def resolve_package(
    package: dict[str, Any],
    max_hash_bytes: int,
    previous: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    provider, releases = fetch_releases(package["url"])
    release, asset, tag, version, note = choose_release(provider, releases, package)
    digest = resolve_sha256(
        provider,
        release,
        asset,
        tag,
        max_hash_bytes,
        previous=previous,
        rehash=package.get("rehash") is True,
    )
    download_url = asset["url"]
    if not download_url.startswith("https://"):
        raise BuildError("resolved asset URL is not HTTPS")
    asset_name = str(asset.get("name") or "")
    if (
        not asset_name
        or asset_name in (".", "..")
        or "/" in asset_name
        or "\\" in asset_name
        or "\x00" in asset_name
    ):
        raise BuildError("resolved asset name is not a plain filename")

    result = copy.deepcopy(package)
    # `release=latest` is a source-manifest selector, not an install-time URL.
    for source_only in (
        "release", "prerelease", "rehash", "allowUnsafeTarget",
        "assetTemplate", "stripPrefixTemplate",
    ):
        result.pop(source_only, None)
    result["release_tag"] = tag
    result["version"] = version
    result["asset"] = asset["name"]
    result["download_url"] = download_url
    result["sha256"] = digest
    asset_size = asset.get("size")
    if isinstance(asset_size, int):
        result["size"] = asset_size
    else:
        result.pop("size", None)
    published = published_epoch(
        release.get("published_at") or release.get("released_at") or release.get("created_at")
    )
    if published:
        result["published"] = published
    else:
        result.pop("published", None)

    if "stripPrefix" in result or "stripPrefixTemplate" in package:
        result["stripPrefix"] = resolved_strip_prefix(
            package, tag=tag, version=version, asset_name=asset["name"]
        )

    # Revalidate the final resolved data; these fields are consumed by the installer.
    validate_resolved_package(result)
    return result, note


def load_previous_catalog() -> dict[str, dict[str, Any]]:
    if not OUTPUT.exists():
        return {}
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("packages"), list):
            raise ValueError("unsupported schema or missing packages array")
        previous: dict[str, dict[str, Any]] = {}
        for package in data["packages"]:
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                continue
            if not PACKAGE_NAME_RE.fullmatch(package["name"]):
                continue
            if not str(package.get("download_url") or "").startswith("https://"):
                continue
            if not valid_sha256(str(package.get("sha256") or "")):
                continue
            asset_name = package.get("asset")
            if (
                not isinstance(asset_name, str)
                or not asset_name
                or asset_name in (".", "..")
                or "/" in asset_name
                or "\\" in asset_name
                or "\x00" in asset_name
            ):
                continue
            try:
                if "target" in package:
                    validate_sdmc_path(package["target"], "target")
                if "marker" in package:
                    validate_sdmc_path(package["marker"], "marker", allow_root=False)
                validate_strip_prefix(package.get("stripPrefix", ""))
            except BuildError:
                continue
            previous[package["name"]] = package
        return previous
    except (OSError, ValueError, json.JSONDecodeError, AttributeError) as exc:
        log(f"warning: existing {OUTPUT.name} is not usable as a fallback: {exc}")
        return {}


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def validate_sources() -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for manifest_path in sorted(PACKAGES_DIR.glob("*/manifest.json")):
        relpath = manifest_path.relative_to(ROOT).as_posix()
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
                raise BuildError("manifest must contain a 'packages' array")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, BuildError) as exc:
            errors.append(f"{relpath}: cannot read manifest: {exc}")
            continue
        for raw in data["packages"]:
            try:
                package = validate_package_source(raw, manifest_path)
                if manifest_path.parent.name != package["name"]:
                    raise BuildError(
                        f"folder name {manifest_path.parent.name!r} does not match "
                        f"package name {package['name']!r}"
                    )
                key = package["name"].casefold()
                if key in seen:
                    raise BuildError(
                        f"duplicate package name {package['name']!r}; "
                        f"already defined by {seen[key]}"
                    )
                seen[key] = relpath
            except BuildError as exc:
                errors.append(f"{relpath}: {exc}")
    return errors


def build_catalog(
    *, max_hash_bytes: int, strict: bool, only: set[str] | None = None
) -> tuple[dict[str, Any], list[str], list[str], dict[str, int]]:
    global _LAST_NOTES, _LAST_COUNTS, _HASH_DOWNLOAD_COUNT
    _HASH_DOWNLOAD_COUNT = 0
    notes: list[str] = []
    counts = {"ok": 0, "kept_previous": 0, "failed": 0,
              "hash_downloaded": 0, "missing": 0}
    previous = load_previous_catalog()
    previous_by_key = {name.casefold(): item for name, item in previous.items()}
    selected = {name.casefold() for name in only} if only is not None else None
    output_packages: list[dict[str, Any]] = []
    output_by_name: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def add_previous(key: str, reason: str) -> bool:
        old = previous_by_key.get(key)
        if not old or key in output_by_name:
            return False
        output_packages.append(copy.deepcopy(old))
        output_by_name[key] = output_packages[-1]
        counts["kept_previous"] += 1
        log(f"  keeping previous {old.get('name', key)} entry ({reason})")
        return True

    source_files = sorted(PACKAGES_DIR.glob("*/manifest.json"))
    source_keys: set[str] = set()
    for manifest_path in source_files:
        source_keys.add(manifest_path.parent.name.casefold())
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for raw in data.get("packages", []) if isinstance(data, dict) else []:
                if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                    source_keys.add(raw["name"].casefold())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    # --only keeps source packages not being resolved exactly as they were.
    # Previous entries with no source manifest are dropped.
    if selected is not None:
        for key in previous_by_key:
            if key not in selected and key in source_keys:
                add_previous(key, "not selected by --only")
    for manifest_path in source_files:
        relpath = manifest_path.relative_to(ROOT).as_posix()
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_data, dict) or not isinstance(manifest_data.get("packages"), list):
                raise BuildError("manifest must contain a 'packages' array")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, BuildError) as exc:
            message = f"{relpath}: cannot read manifest: {exc}"
            errors.append(message)
            log(message)
            if selected is None:
                add_previous(manifest_path.parent.name.casefold(), "manifest parse/structure failure")
            continue

        for raw_package in manifest_data["packages"]:
            raw_name = raw_package.get("name") if isinstance(raw_package, dict) else None
            fallback_name = raw_name if isinstance(raw_name, str) else manifest_path.parent.name
            key = fallback_name.casefold()
            if selected is not None and key not in selected:
                continue
            try:
                package = validate_package_source(raw_package, manifest_path)
                key = package["name"].casefold()
                if key in output_by_name:
                    raise BuildError(f"duplicate package name {package['name']!r}; first manifest wins")
                resolved, note = resolve_package(package, max_hash_bytes, previous_by_key.get(key))
                output_packages.append(resolved)
                output_by_name[key] = resolved
                counts["ok"] += 1
                if note:
                    notes.append(note)
                    log(f"warning: {note}")
                log(f"ok: {package['name']}: {resolved['version']} "
                    f"[{resolved['asset']}] sha256={resolved['sha256']}")
            except Exception as exc:
                message = f"{relpath} ({fallback_name}): {exc}"
                errors.append(message)
                log(f"warning: {message}")
                if not add_previous(key, str(exc)):
                    log(f"  no previous valid catalogue entry for {fallback_name} ({exc})")

    if selected is not None:
        for key in selected:
            if key not in output_by_name:
                errors.append(f"--only {key}: no package was produced")

    output_packages.sort(key=lambda item: str(item.get("name", "")).casefold())
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "count": len(output_packages),
        "packages": output_packages,
    }
    counts["failed"] = len(errors)
    counts["hash_downloaded"] = _HASH_DOWNLOAD_COUNT
    counts["missing"] = len([key for key in source_keys if key not in output_by_name])

    if source_files and not output_packages and errors:
        raise BuildError("no usable packages were generated; refusing to replace the catalogue with an empty file")
    if strict and errors:
        raise BuildError(f"strict mode: {len(errors)} package/manifest error(s); catalogue not accepted")
    _LAST_NOTES = notes
    _LAST_COUNTS = counts
    return catalog, errors, notes, counts


def write_ci_summary(errors: list[str], notes: list[str], counts: dict[str, int]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Brewstore catalogue",
        "",
        f"- ok: {counts['ok']}",
        f"- kept previous: {counts['kept_previous']}",
        f"- failed: {counts['failed']}",
        f"- hash downloaded: {counts['hash_downloaded']}",
        f"- missing: {counts['missing']}",
        "",
    ]
    if errors:
        lines.extend(["### Errors", "", *[f"- {item}" for item in errors], ""])
    if notes:
        lines.extend(["### Notes", "", *[f"- {item}" for item in notes], ""])
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log(f"warning: could not write GITHUB_STEP_SUMMARY: {exc}")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail if any package could not be refreshed")
    parser.add_argument("--check", action="store_true", help="do not write; fail if catalog.json is out of date")
    parser.add_argument("--validate-only", action="store_true", help="validate source manifests without network access")
    parser.add_argument("--only", metavar="NAME[,NAME...]", help="resolve only these packages; keep the rest unchanged")
    parser.add_argument(
        "--max-hash-bytes",
        type=int,
        default=int(os.environ.get("BREWSTORE_HASH_MAX_BYTES", DEFAULT_HASH_MAX_BYTES)),
        help="maximum asset size to download when no published SHA-256 is available",
    )
    args = parser.parse_args()
    if args.max_hash_bytes <= 0:
        parser.error("--max-hash-bytes must be positive")

    only = None
    if args.only:
        only = {name.strip().casefold() for name in args.only.split(",") if name.strip()}
        if not only:
            parser.error("--only requires at least one package name")

    if args.validate_only:
        errors = validate_sources()
        for error in errors:
            log(f"error: {error}")
        if errors:
            return 1
        print("source manifests are valid")
        return 0

    try:
        catalog, errors, notes, counts = build_catalog(
            max_hash_bytes=args.max_hash_bytes, strict=args.strict, only=only
        )
        write_ci_summary(errors, notes, counts)
        encoded = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if args.check:
            try:
                current = OUTPUT.read_bytes()
            except OSError:
                current = b""
            if current != encoded:
                log(f"{OUTPUT.name} is out of date")
                return 1
            print(f"{OUTPUT.name} is up to date ({len(catalog['packages'])} packages)")
            return 0

        try:
            current = OUTPUT.read_bytes()
        except OSError:
            current = None
        if current != encoded:
            atomic_write(OUTPUT, encoded)
            print(f"wrote {OUTPUT.name} ({len(catalog['packages'])} packages)")
        else:
            print(f"{OUTPUT.name} unchanged ({len(catalog['packages'])} packages)")
        if errors:
            print(f"completed with {len(errors)} package/manifest warning(s)")
        print(f"ok={counts['ok']} kept_previous={counts['kept_previous']} "
              f"failed={counts['failed']} hash_downloaded={counts['hash_downloaded']} "
              f"missing={counts['missing']}")
        return 0
    except BuildError as exc:
        log(f"error: {exc}")
        return 1
    except OSError as exc:
        log(f"error: filesystem operation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

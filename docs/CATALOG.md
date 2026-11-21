# Brewstore catalogue

Brewstore reads one generated catalogue, `catalog.json`, instead of resolving
releases on the console.

## Source manifests

Contributors edit `packages/<name>/manifest.json`. Each file has a `packages`
array with one entry.

Required fields: `name`, `url`.
Common optional fields:

| Field | Meaning |
|---|---|
| `title`, `author`, `description`, `icon` | Shop card metadata. `icon` must be HTTPS. |
| `version` | Displayed/installed-version hint; the build replaces it with the resolved release version. |
| `release` | `latest` (default) auto-updates; any other value pins that tag. |
| `asset` | Concrete legacy asset name; `update_manifests.py` keeps this current. |
| `assetTemplate` | Template used by the new build/update path: `{release_tag}`, `{tag}`, `{version}`. |
| `stripPrefix` | Concrete legacy archive prefix; `update_manifests.py` keeps this current. |
| `stripPrefixTemplate` | Template for the archive prefix; also supports `{asset}` and `{asset_stem}`. |
| `target` | Fixed install path. Omit to use the user's Default install path. |
| `marker` | File that proves the package is installed. |
| `sha256` | Source hint only; `catalog.json` receives the resolved hash. |
| `published` | Source hint; refreshed by the build. |
| `shortcut` | Create an hbmenu shortcut. |
| `prerelease` | Set `true` to allow prereleases for this package. |
| `rehash` | Force the build to download/hash the asset instead of reusing a previous hash. |
| `allowUnsafeTarget` | Maintainer-only opt-in for sensitive targets. |
| `branch` | README fallback branch. |

### Templates

Use templates in separate fields, keeping the concrete fields for legacy
clients:

```json
"asset": "OpenRAC-Switch2-v0.2.zip",
"assetTemplate": "{release_tag}.zip",
"stripPrefix": "OpenRAC-Switch2-v0.2/bundle/",
"stripPrefixTemplate": "{asset_stem}/bundle/"
```

`build_catalog.py` prefers the template fields. `update_manifests.py` expands
them and rewrites the concrete `asset` / `stripPrefix` so old Brewstore builds
that read the legacy fields continue to work.

`release_tag` is the upstream tag (`v1.2.3`), `asset` is the resolved asset
filename and `asset_stem` is that filename without its final extension.

### Pinned releases

`release: latest` (or no `release`) follows the newest stable release with a
matching asset.

`release: v1.2.3` pins that tag. Pinned packages do **not** auto-update in
`update_manifests.py` or `build_catalog.py`; a daily bump workflow proposes a
PR when a newer stable release exists. Merging that PR is the approval step.
Pinned packages only update when that bump PR is merged.

## Client limits

The generator enforces the same limits as the client:

| Field | Rule |
|---|---|
| `name` | `[A-Za-z0-9][A-Za-z0-9._-]{0,62}`, unique case-insensitively |
| `title` | 95 chars |
| `author` | 63 chars |
| `description` | 511 chars |
| `icon` | optional HTTPS, 511 chars |
| `url` | HTTPS, 511 chars |
| `version` | 31 chars |
| `release_tag` | 63 chars |
| `asset` | plain filename, 127 chars |
| `download_url` | HTTPS, 1023 chars |
| `sha256` | required, 64 lowercase hex |
| `size` | optional non-negative integer |
| `published` | integer epoch |
| `target` | `sdmc:/`, `sdmc:/switch`, or `sdmc:/switch/<path>`, 255 chars |
| `marker` | `sdmc:/...`, not root, 511 chars |
| `stripPrefix` | relative, no `..`, 127 chars |

Catalogue files are capped at 1 MiB by the client. Legacy manifests keep the
256 KiB cap.

## Sensitive targets

`target: sdmc:/` and targets under `sdmc:/atmosphere`, `sdmc:/bootloader` or
`sdmc:/config/sys-` are rejected unless the manifest has:

```json
"allowUnsafeTarget": true
```

This is a maintainer decision. Root-target packages also need a `marker`.

## Building

```bash
# Offline manifest validation
python3 scripts/build_catalog.py --validate-only

# Resolve releases and write catalog.json. GITHUB_TOKEN is strongly recommended;
# without it the GitHub API allows only 60 requests/hour.
GITHUB_TOKEN=... python3 scripts/build_catalog.py

# Check without writing
python3 scripts/build_catalog.py --check

# Resolve only selected packages, keeping every other source entry unchanged
python3 scripts/build_catalog.py --only openrac,starship
```

`--strict` fails on any package that could not be resolved. Scheduled/manual
catalogue builds do **not** use `--strict`; pull requests do.

## Output contract

`catalog.json` contains:

```json
{
  "schema_version": 1,
  "count": 66,
  "packages": [
    {
      "name": "foo",
      "url": "https://github.com/owner/repo",
      "version": "v1.0.0",
      "release_tag": "v1.0.0",
      "asset": "foo.nro",
      "download_url": "https://github.com/.../foo.nro",
      "sha256": "<64 lowercase hex>",
      "size": 123456,
      "target": "sdmc:/switch/foo"
    }
  ]
}
```

A package is installable only when it has `download_url`, a valid `sha256` and
`release_tag`. `count` must equal the number of package entries. Output is
sorted by case-insensitive `name`.

Legacy clients still use `packages.json` and the per-package include layout; it
is intentionally kept unchanged.

# brewstore-repo

Catalogue for [brewstore](https://github.com/HerXayah/brewstore).

```text
packages/<name>/manifest.json   one source package per file
packages.json                   legacy include index
catalog.json                    generated client catalogue
scripts/                        generators and maintenance tools
docs/CATALOG.md                 field, template and limit reference
```

New Brewstore clients read `catalog.json`. It is generated from the source
manifests and contains the resolved release tag, asset name, download URL,
SHA-256 and optional size for every package. Clients do not call the GitHub API
to resolve catalogue releases.

`packages.json` and the per-package manifests are kept for older Brewstore
clients. Do not edit `catalog.json` by hand.

## Adding a package

Create `packages/<name>/manifest.json` with a normal `packages` array containing
one entry:

```json
{
  "packages": [
    {
      "name": "example-homebrew",
      "title": "Example Homebrew",
      "author": "Example Author",
      "description": "A short description.",
      "icon": "https://example.com/icon.png",
      "url": "https://github.com/example/example-homebrew",
      "release": "latest",
      "assetTemplate": "{release_tag}.zip",
      "stripPrefixTemplate": "{asset_stem}/"
    }
  ]
}
```

Required fields are `name` and `url`. Then add the same path to the
`packages.json` include list and run:

```bash
python3 scripts/build_catalog.py --validate-only
python3 scripts/build_catalog.py
```

The scheduled `.github/workflows/catalog.yml` workflow refreshes `version`,
`sha256`, `published`, opted-in GitHub asset download counts (`app_dls`) and
`catalog.json` every eight hours. Changes to manifests on `main` also trigger
the workflow.

## Release metadata

The source manifest provides hints; `catalog.json` receives the resolved values:

```json
"version": "v1.2.3",
"published": 1763425249,
"sha256": "<64 hex characters>",
"asset": "optional-asset-name.zip"
```

`sha256` is the SHA-256 of the exact release asset brewstore downloads.
`published` is the release publication time in Unix seconds, used by the
**Updated newest** sort.

Use the separate template fields for values that follow the release:

```json
"asset": "OpenRAC-Switch2-v0.2.zip",
"assetTemplate": "{release_tag}.zip",
"stripPrefix": "OpenRAC-Switch2-v0.2/bundle/",
"stripPrefixTemplate": "{asset_stem}/bundle/"
```

The build prefers templates. `update_manifests.py` expands them back into the
concrete `asset` and `stripPrefix` fields so legacy clients keep working.

`release: latest` follows the newest stable release with a matching asset. A
pinned value such as `"release": "v1.2.3"` only changes when a bump PR is
reviewed and merged. Set `"prerelease": true` to allow prereleases, and
`"rehash": true` to force a fresh asset download during hashing.

See [docs/CATALOG.md](docs/CATALOG.md) for templates, client limits, sensitive
targets and the output contract.

## Install layout

Default target:

```text
sdmc:/brewstore/apps/<package>/
```

Manifests should omit `target` unless the app requires a specific path. Use an
explicit target only for apps that hardcode one:

```json
"target": "sdmc:/switch"
```

or, for root-drop packages:

```json
"target": "sdmc:/"
```

Root targets are exceptions and must have a `marker` or a single-file asset.
Apps that write to the SD root should be avoided unless unavoidable. Sensitive
system targets require `"allowUnsafeTarget": true` and are a maintainer
decision.

GitLab project URLs are also supported for release resolution:

```json
"url": "https://gitlab.com/Azkali/moonlight-nx2",
"release": "latest"
```

GitLab release assets use the same `asset` hint rules as GitHub.

## Homebrew path guide

This applies to any Switch 2 homebrew, not just brewstore.

Avoid hardcoding absolute paths such as:

```text
sdmc:/switch
sdmc:/retroarch
sdmc:/config
```

Resolve one **app root** and build every other path from it.

Recommended per-app layout:

```text
sdmc:/switch/<app>/
  <app>.nro
  data/
  config/
  saves/
  logs/
  cache/
```

or the same shape under `/switch2/<app>/` if that becomes the convention.

Example:

```c
char root[PATH_MAX];
fill_pathname_basedir(root, argv[0], sizeof(root));

char config[PATH_MAX];
char saves[PATH_MAX];
fill_pathname_join(config, root, "config", sizeof(config));
fill_pathname_join(saves, root, "saves", sizeof(saves));
```

Rules:

- The app root is the directory containing the NRO, or a parent chosen by the
  launcher.
- Put data, config, saves, logs, and caches under that root.
- Never write directly to `sdmc:/`, `sdmc:/switch`, or another app's folder.
- Do not rely on symlinks; FAT32 and exFAT do not support them.
- If a fixed path is unavoidable, read it from a config file or environment
  variable instead of compiling it in.
- Keep a legacy fallback when changing layouts so existing users do not lose
  saves or config.
- Separate package files from user files so uninstall can remove only what it
  installed.

This lets the same build work from `/switch`, `/brewstore/apps`, `/switch2`,
or a future launcher layout without recompiling.

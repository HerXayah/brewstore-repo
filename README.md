# brewstore-repo

Catalog for [brewstore](https://github.com/HerXayah/brewstore).

```text
packages/<name>/manifest.json   one package per file
packages.json                   include index
```

Each manifest contains a normal `packages` array with one entry. The index is:

```json
{
  "include": [
    "packages/example/manifest.json"
  ]
}
```

brewstore resolves relative includes from the catalog URL and merges the results.

## Release metadata

Manifests may include:

```json
"version": "v1.2.3",
"sha256": "<64 hex characters>",
"asset": "optional-asset-name.zip"
```

`sha256` is the SHA-256 of the exact release asset brewstore downloads. It
lets brewstore detect updates with the cached catalog and no GitHub API calls
from the console.

The scheduled workflow `.github/workflows/update-manifests.yml` refreshes
`version` and `sha256` from each package's latest GitHub release using the
workflow's own token.

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
Apps that write to the SD root should be avoided unless unavoidable.

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

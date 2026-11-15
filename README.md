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
sdmc:/switch/brewstore/apps/<package>/
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

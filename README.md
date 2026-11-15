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

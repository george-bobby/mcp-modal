# Publishing Guidelines

The release checklist for `mcp-modal`. A release lands in **three** places — keep them in
sync: [PyPI](https://pypi.org/project/mcp-modal/) (the package), the
[MCP Registry](https://registry.modelcontextprotocol.io) (`server.json`), and
[Glama](https://glama.ai/mcp/servers/george-bobby/mcp-modal) (auto-indexed from GitHub).

> One-time setup (the PyPI trusted publisher, the GitHub `release` environment, claiming the
> Glama listing) is already done and intentionally not repeated here — see
> [Automated publishing](#automated-publishing-recommended) for what that setup is.

## TL;DR — the full release

Publishing is automated. Just bump the version and push a tag — GitHub Actions does PyPI and
the MCP Registry; Glama re-indexes from the push:

```bash
# bump the version in the 3 spots below, then:
git commit -am "Release vX.Y.Z: …" && git tag vX.Y.Z && git push --follow-tags
```

The [`.github/workflows/publish.yml`](.github/workflows/publish.yml) workflow fires on the
`vX.Y.Z` tag, builds with `uv`, and publishes to **PyPI** (Trusted Publishing / OIDC) and the
**MCP Registry** (`mcp-publisher login github-oidc`) — no tokens stored anywhere. You can also
run it manually from the **Actions → Publish → Run workflow** button (`workflow_dispatch`),
which is what you do for a tag that was pushed before the workflow existed.

The manual `uv publish` / `mcp-publisher publish` commands below remain valid as a fallback.

---

## Automated publishing (recommended)

`.github/workflows/publish.yml` releases to PyPI and the MCP Registry on every `v*` tag (or
manual dispatch), authenticating entirely through GitHub Actions OIDC. The one-time setup it
depends on:

1. **PyPI Trusted Publishing** — at
   [pypi.org/manage/project/mcp-modal/settings/publishing](https://pypi.org/manage/project/mcp-modal/settings/publishing/),
   add a publisher with: owner `george-bobby`, repository `mcp-modal`, workflow `publish.yml`,
   environment `release`.
2. **GitHub `release` environment** — repo **Settings → Environments → New environment** named
   `release` (add reviewers/branch limits here if you want a manual gate before publishing).

With those in place, a tag push (or manual run) publishes both registries. The `registry` job
`needs: pypi`, so the MCP Registry is only updated after the PyPI version is live.

---

## 1. Bump the version

The version lives in **three** places and they must all match (the registry rejects a
`server.json` whose `packages[].version` isn't on PyPI yet):

- [pyproject.toml](pyproject.toml) → `version = "X.Y.Z"`
- [server.json](server.json) → top-level `"version"` **and** `packages[0].version`

## 2. Publish to PyPI

```bash
rm -rf dist        # avoid re-uploading stale artifacts from a previous release
uv build           # writes dist/mcp_modal-X.Y.Z-py3-none-any.whl + .tar.gz
uv publish         # uploads dist/* to PyPI
```

`uv publish` reads the token from `UV_PUBLISH_TOKEN` (or `~/.pypirc`). To pass it inline:
`uv publish --token pypi-…`. Re-publishing an existing version fails — bump first.

## 3. Publish to the MCP Registry

`mcp-publisher` publishes the `server.json` in the current directory.

```bash
mcp-publisher validate     # optional: sanity-check server.json before pushing
mcp-publisher login github # re-auth when the session has expired
mcp-publisher publish      # push server.json to the registry
```

The PyPI release from step 2 must already be live, since the registry validates the
referenced package version.

## 4. Update Glama

Glama re-indexes from the GitHub repo automatically — there's no separate publish command.
Just push to `main`:

```bash
git commit -am "release vX.Y.Z" && git tag vX.Y.Z
git push --follow-tags
```

[glama.json](glama.json) only needs editing when maintainers change. If the listing looks
stale after a push, trigger a manual refresh from the Glama dashboard.

## 5. Verify

```bash
uvx mcp-modal@X.Y.Z     # pulls the fresh version from PyPI in a clean env; Ctrl-C to exit the stdio server
claude mcp get mcp-modal  # confirm the client still connects
```

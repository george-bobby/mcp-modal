# Publishing Guidelines

The release checklist for `mcp-modal`. A release lands in **four** places — keep them in
sync: [PyPI](https://pypi.org/project/mcp-modal/) (the package),
[GitHub Releases](https://github.com/george-bobby/mcp-modal/releases) (the tag, its notes
and the downloadable artifacts), the
[MCP Registry](https://registry.modelcontextprotocol.io) (`server.json`), and
[Glama](https://glama.ai/mcp/servers/george-bobby/mcp-modal) (auto-indexed from GitHub).

Every published version has a `vX.Y.Z` tag and a GitHub Release carrying the exact `.whl`
and `.tar.gz` that PyPI serves, so any version can be downloaded without PyPI.

> One-time setup (the PyPI trusted publisher, claiming the Glama listing) is already done and
> intentionally not repeated here — see
> [Automated publishing](#automated-publishing-recommended) for what that setup is.

## TL;DR — the full release

Publishing is automated. Just bump the version and push a tag — GitHub Actions does PyPI,
the GitHub Release and the MCP Registry; Glama re-indexes from the push:

```bash
# bump the version in the 3 fields listed under "Bump the version", then:
git commit -am "Release vX.Y.Z: …" && git tag -a vX.Y.Z -m "vX.Y.Z" && git push --follow-tags
```

> Use an **annotated** tag (`git tag -a`). `git push --follow-tags` only pushes annotated
> tags — a lightweight `git tag vX.Y.Z` would stay local and the workflow would never fire.
> (Or push the tag explicitly: `git push origin vX.Y.Z`.)

The [`.github/workflows/publish.yml`](.github/workflows/publish.yml) workflow fires on the
`vX.Y.Z` tag, builds with `uv`, and publishes to **PyPI** (Trusted Publishing / OIDC), cuts
the **GitHub Release**, and publishes to the **MCP Registry**
(`mcp-publisher login github-oidc`) — no tokens stored anywhere. You can also
run it manually from the **Actions → Publish → Run workflow** button (`workflow_dispatch`),
which re-runs a release for a tag that is already pushed.

The manual `uv publish` / `mcp-publisher publish` commands in steps 2 and 3 are the fallback
if the workflow is unavailable.

**Re-pushing an existing tag is safe.** PyPI versions are immutable, so an artifact
rebuilt for a version that is already live would be rejected on upload. The workflow checks
PyPI for the version in `pyproject.toml` before doing anything and skips both the PyPI
upload and the registry publish when it is already published, so the run stays green and
nothing is republished. To actually release, bump the version.

---

## Automated publishing (recommended)

`.github/workflows/publish.yml` releases to PyPI and the MCP Registry on every `v*` tag (or
manual dispatch), authenticating entirely through GitHub Actions OIDC. The one-time setup it
depends on:

- **PyPI Trusted Publishing** — at
  [pypi.org/manage/project/mcp-modal/settings/publishing](https://pypi.org/manage/project/mcp-modal/settings/publishing/),
  add a publisher with: owner `george-bobby`, repository `mcp-modal`, workflow `publish.yml`,
  and **leave the environment field blank** (the workflow runs in the default context — no
  GitHub environment to create). If you later want a manual approval gate before each publish,
  create a GitHub environment, put its name in both the PyPI publisher and the `pypi` job's
  `environment:` key.

With that in place, a tag push (or manual run) publishes everything. The `registry` job
`needs: pypi`, so the MCP Registry is only updated after the PyPI version is live. The
`release` job also `needs: pypi` and attaches the *same* build artifacts the `pypi` job
uploaded (passed between jobs with `upload-artifact`/`download-artifact`) rather than
rebuilding them — a rebuilt wheel can differ byte-for-byte from what PyPI serves, and a
release asset that doesn't match the published package is worse than no asset.

The `release` job runs only for a `refs/tags/v*` ref (a `workflow_dispatch` on a branch has
no version to release) and is idempotent: if the release already exists it refreshes the
notes and re-uploads the assets with `--clobber` instead of failing, which matches how the
PyPI and registry jobs treat a re-pushed tag.

**Release notes come from the tagged commit's message**, not the tag's own message — the
tags here are annotated with just `vX.Y.Z`, while the `Release vX.Y.Z: …` commit body is the
real changelog. The job strips `Co-Authored-By:` lines and appends install instructions. So
write the release notes in the release commit message, and **put the tag on that commit** —
`v0.3.0` sits one commit later than its release commit, which is why its auto-generated
title would have read "skip publishing when the version is already on PyPI".

---

## 1. Bump the version

The version lives in **three fields across two files** and they must all match (the
registry rejects a `server.json` whose `packages[].version` isn't on PyPI yet):

- [pyproject.toml](pyproject.toml) → `version = "X.Y.Z"`
- [server.json](server.json) → top-level `"version"`
- [server.json](server.json) → `packages[0].version`

Check them in one go: `grep -n '"version"' server.json; grep -n '^version' pyproject.toml`

## 2. Publish to PyPI

```bash
rm -rf dist        # avoid re-uploading stale artifacts from a previous release
uv build           # writes dist/mcp_modal-X.Y.Z-py3-none-any.whl + .tar.gz
uv publish         # uploads dist/* to PyPI
```

`uv publish` reads the token from `UV_PUBLISH_TOKEN` (or `~/.pypirc`). To pass it inline:
`uv publish --token pypi-…`. Re-publishing an existing version fails — bump first.

## 3. Cut the GitHub Release

The workflow does this. To do it by hand (or to backfill an old tag), attach the artifacts
PyPI is already serving rather than a fresh local build, so the download matches the package:

```bash
version=X.Y.Z
mkdir -p /tmp/rel && cd /tmp/rel
# pull the published artifacts straight from PyPI
curl -fsS "https://pypi.org/pypi/mcp-modal/$version/json" \
  | python3 -c 'import json,sys,urllib.request as u; [u.urlretrieve(f["url"], f["filename"]) for f in json.load(sys.stdin)["urls"]]'
gh release create "v$version" ./* --title "$version — <headline>" --notes-file notes.md --verify-tag
```

`--verify-tag` refuses to invent a tag that doesn't exist yet, which is what you want: the
tag should already be pushed and pointing at the release commit.

## 4. Publish to the MCP Registry

`mcp-publisher` publishes the `server.json` in the current directory.

```bash
mcp-publisher validate     # optional: sanity-check server.json before pushing
mcp-publisher login github # re-auth when the session has expired
mcp-publisher publish      # push server.json to the registry
```

The PyPI release from step 2 must already be live, since the registry validates the
referenced package version.

## 5. Update Glama

Glama re-indexes from the GitHub repo automatically — there's no separate publish command.
Just push to `main`:

```bash
git commit -am "release vX.Y.Z" && git tag -a vX.Y.Z -m "vX.Y.Z"
git push --follow-tags   # --follow-tags only pushes annotated tags; use `git tag -a`
```

[glama.json](glama.json) only needs editing when maintainers change. If the listing looks
stale after a push, trigger a manual refresh from the Glama dashboard.

## 6. Verify

```bash
uvx mcp-modal@X.Y.Z       # pulls the fresh version from PyPI in a clean env; Ctrl-C to exit the stdio server
claude mcp get mcp-modal  # confirm the client still connects
gh release view vX.Y.Z    # notes render, and both .whl and .tar.gz are attached
```

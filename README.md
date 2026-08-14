# OSRS Collection Log Dependency Builder

A Python tool that builds dependency chains for OSRS items based on collection log requirements, generating `clog_restrictions.json` for the [Clogman Mode](https://github.com/mozjay/clogman-mode) RuneLite plugin.

## Overview

- Fetches collection log items, recipes, and item IDs from the OSRS Wiki
- An item is restricted only if **all** its recipes ultimately require clog items - any clog-free recipe makes it unrestricted
- Clog items can also unlock each other via crafting ("effective unlocking")
- Outputs `clog_restrictions.json` with `collectionLogItems` (clog items + variants) and `derivedItems` (items that require them)

## Usage

Dependencies are managed with [`uv`](https://docs.astral.sh/uv/). Run `uv sync` once, then prefix commands with `uv run`:

```bash
# Generate output (uses cached data if fresh)
uv run python3 clog_dependency_builder.py

# Force refresh cached data
uv run python3 clog_dependency_builder.py --refresh-cache

# Visualize dependencies for a specific item
uv run python3 clog_dependency_builder.py --visualize "Item Name"

# Force an explicit version bump (see Versioning)
uv run python3 clog_dependency_builder.py --set-version 1.6.0
```

## Output

- Default: `output/clog_restrictions.json`
- Copy to clogman repo: `../clogman-mode/src/main/resources/clog_restrictions.json`

## Versioning

- **Patch** - bumped automatically whenever the item list changes
- **Minor** - manual, via `--set-version`, for a real logic/behavior change (bug fix, new rule)
- **Major** - manual, reserved for a substantial overhaul

Every version-changing run also prepends an entry to `CHANGELOG.md`. For ad-hoc diffs between two output files:

```bash
uv run python3 changelog.py old.json new.json
```

## Dependency Explorer

`build_explorer.py` generates a self-contained `output/explorer.html` for browsing any item's full dependency chain as an interactive graph (not just the minimal set that ships in `clog_restrictions.json`):

```bash
uv run python3 build_explorer.py
```

A hosted snapshot lives at `docs/index.html`, published via GitHub Pages: https://mozjay.github.io/osrs-clog-dependencies/. It's regenerated and committed manually on release (`uv run python3 build_explorer.py --output docs/index.html`), not automatically - it tracks whatever's live in the plugin, not the latest wiki state.

## Manual Recipes & Overrides

Some items can't be auto-detected from the wiki's recipe graph:

- **`manual_recipes.json`** - items that share display names with base items, or otherwise resolve to the wrong recipe. Each run prompts you to add/decline any newly-detected candidates (`--skip-manual-review` to skip this).
- **`manual_dependency_overrides.json`** - items that are byproducts of a clog-gated recipe but have no recipe of their own in the wiki graph, so the resolver would otherwise treat them as free base materials.

## File Structure

- `clog_dependency_builder.py` - main script
- `build_explorer.py` - dependency graph explorer
- `pyproject.toml` / `uv.lock` - dependencies (managed with `uv`)
- `manual_recipes.json`, `manual_dependency_overrides.json`, `manual_candidate_decisions.json` - manual data (see above)
- `cache/` - cached wiki data (7 day TTL)
- `output/` - generated output files

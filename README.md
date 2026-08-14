# OSRS Collection Log Dependency Builder

A Python tool that builds dependency chains for Old School RuneScape items based on collection log requirements. This generates `clog_restrictions.json` for the [Clogman Mode](https://github.com/mozjay/clogman-mode) RuneLite plugin.

## Overview

This tool:
1. Fetches collection log items and recipes from OSRS Wiki
2. Builds dependency chains to determine which items require collection log unlocks
3. Outputs `clog_restrictions.json` with two main sections:
   - `collectionLogItems`: Clog items with their variants
   - `derivedItems`: Items that require clog items to create

## Usage

Dependencies are managed with [`uv`](https://docs.astral.sh/uv/). Run `uv sync` once to create `.venv`, then prefix commands with `uv run` (which keeps the environment in sync automatically):

```bash
# Generate output (uses cached data if fresh)
uv run python3 clog_dependency_builder.py

# Force refresh cached data
uv run python3 clog_dependency_builder.py --refresh-cache

# Visualize dependencies for specific item
uv run python3 clog_dependency_builder.py --visualize "Item Name"

# Custom output path
uv run python3 clog_dependency_builder.py --output path/to/output.json

# Force an explicit version (see Versioning below)
uv run python3 clog_dependency_builder.py --set-version 1.6.0
```

## Output

- Default: `output/clog_restrictions.json` (in this repo)
- Copy to clogman repo: `../clogman-mode/src/main/resources/clog_restrictions.json`

## Versioning

The `version` field is compared against the previous on-disk output every run:

- **Patch** (`1.4.0` -> `1.4.1`) - bumped automatically whenever the generated `collectionLogItems`/`derivedItems` differ from the last run (e.g. the wiki added new collection log items). This is the common case and needs no action.
- **Minor** (`1.4.1` -> `1.5.0`) - always manual, via `--set-version`. Bump this yourself when a real logic/behavior change lands (a bug fix, a new pruning/dependency rule, etc.) - this can't be reliably auto-detected from the output diff alone, since a bug fix and a routine data refresh can look identical from the outside (or even produce no diff at all, if the fix only prevents a *future* regression).
- **Major** - always manual, reserved for a substantial overhaul (unlikely to come up often).

If the item list is unchanged since the last run, the version stays the same and no `CHANGELOG.md` entry is written.

## Changelog

Every run that changes the generated item list prepends a dated entry to `CHANGELOG.md` (added/removed/changed `collectionLogItems` and `derivedItems`). For ad-hoc comparisons between two arbitrary output files (e.g. against an old git revision), use the standalone script:

```bash
uv run python3 changelog.py old.json new.json
```

## Dependency Explorer

For QA/debugging (e.g. "why was this item pruned?") or general curiosity, `build_explorer.py` generates a self-contained `output/explorer.html` (data embedded inline, no server or database) that lets you search any clog-relevant item and browse its full dependency chain as an interactive node/edge graph - drag to pan, scroll to zoom, click a node to jump the view to that item - covering every recipe alternative and every material, not just the minimal dependency set that ships in `clog_restrictions.json`. Rendering is via [`pyvis`](https://pyvis.readthedocs.io/)/vis-network, inlined into the page at build time so the file stays self-contained (no CDN, works offline). Rebuilds from the existing `cache/` data (no network calls):

```bash
uv run python3 build_explorer.py
```

Then just open `output/explorer.html` in a browser. It's gitignored and entirely independent of `clog_restrictions.json` - regenerate it on demand, never commit it.

### Hosted copy

A copy also lives at `docs/index.html` and is published via GitHub Pages at https://mozjay.github.io/osrs-clog-dependencies/. Unlike `output/explorer.html`, this one *is* committed - but it's a manual snapshot, not auto-rebuilt from the wiki on a schedule. Regenerate and commit it whenever you do a real release (i.e. whenever you'd otherwise copy `clog_restrictions.json` to the clogman-mode repo), so the hosted page tracks whatever version is actually live in the plugin, not the latest wiki state:

```bash
uv run python3 build_explorer.py --output docs/index.html
```

## Manual Recipes

Some items can't be auto-detected (e.g., items that share display names with base items). These are defined in `manual_recipes.json`.

### Format

```json
{
  "item name": {
    "name": "item name",
    "item_ids": [12345],
    "clog_dependencies": [4153, 24229]
  }
}
```

### Reviewing New Candidates

Every run (unless `--skip-manual-review` is passed) checks for clog-derived recipe outputs that have no auto-detected item ID and aren't yet in `manual_recipes.json`. If there's nothing new, this is silent and generation proceeds as normal.

Candidates with no item ID suggestion from the wiki (`page_ids` lookup comes back empty) are automatically declined without prompting. This covers both cosmetic recolours (e.g. `dark bow (green)`, `lost bag (red)`, `amulet of the eye (blue)` - which reuse their base item's ID and never get a distinct one) and POH/construction decorations (e.g. mounted heads, trophies) that have no item ID at all - in both cases there's no ID to add to `manual_recipes.json`, so there's nothing to review.

For each remaining new or changed candidate (one with at least one suggested item ID), it prints the recipe and its clog dependencies, then prompts you to:
- Enter the item's real item ID(s) (comma-separated) - looked up on the wiki/in-game - to add it to `manual_recipes.json`
- Enter `d` to decline (the item has no real distinct ID, e.g. it's a Construction decoration or cosmetic recolour)
- Press Enter to skip for now (will be asked again next run)

Decisions are recorded in `manual_candidate_decisions.json` (tracked in git), keyed by a hash of the recipe's materials - so declined candidates aren't re-prompted unless their recipe data changes.

Use `--skip-manual-review` to run non-interactively without this step.

## Manual Dependency Overrides

Some items have no production recipe in the wiki's recipe graph, so the resolver treats them as freely-obtainable base materials with zero clog dependencies. This is correct for true base materials (ores, logs, etc.) but wrong for items that are only obtainable as a byproduct of a clog-gated recipe.

For example, `Malformed infernal blend` is a failed-smithing byproduct of crafting `Infernal blend` from `Oathplate shards`, and reprocesses back into `Infernal blend`. The wiki graph has no recipe for it, so without an override it looks like a free alternative material for `Infernal blend` - which masks the `Oathplate shards` dependency for the entire `Infernal blend -> Infernal nugget -> Infernal chunk -> Infernal plate -> Oathplate helm/chest/legs` chain.

`manual_dependency_overrides.json` lets us specify the true clog dependency sets for such items, in the same `clog_dependencies` (OR-of-AND) format as `manual_recipes.json`. Once set, the override propagates automatically through the normal resolver logic - both to derived items further up the chain (e.g. `Infernal plate` becomes correctly restricted) and to `craftable_from` on clog items (e.g. `Oathplate helm` becomes effectively unlockable via `Oathplate shards`).

## File Structure

- `clog_dependency_builder.py` - Main script
- `build_explorer.py` - Local dependency-graph explorer (see Dependency Explorer above)
- `pyproject.toml` / `uv.lock` - Dependencies, managed with `uv`
- `manual_recipes.json` - Manually-defined derived items
- `manual_dependency_overrides.json` - Manual clog dependency overrides for items the recipe graph misrepresents as free base materials
- `manual_candidate_decisions.json` - Accept/decline history for `--review-manual-candidates`
- `cache/` - Cached wiki data (7 day TTL)
- `output/` - Generated output files

## How It Works

1. **Fetch Data**: Collection log items, recipes, and item IDs from OSRS Wiki
2. **Build Recipe Graph**: Map items to their crafting materials
3. **Build Variant Relationships**: Link items to their variants (charged, broken, etc.)
4. **Find Dependencies**: Determine which items require clog unlocks
5. **Add Manual Recipes**: Merge manually-defined recipes
6. **Generate Output**: Create `clog_restrictions.json`

## Excluded Page Name Suffixes

Some wiki pages document non-obtainable items (betas, interface/animation-only graphics, discontinued items, Last Man Standing replicas, etc.) that reuse a real item's display name under a different item ID. If left in, these IDs would pollute the real item's variant ID list in the output.

These are filtered out via `EXCLUDED_PAGE_NAME_SUFFIXES` in `clog_dependency_builder.py`, matched against each entry's wiki `page_name`. The wiki adds new pages like this over time (new minigames, beta tests, events, etc.), so this list may need updating - if a future `--refresh-cache` run introduces spurious duplicate IDs for a real item, check the offending page's `page_name` suffix and add it to the list if it fits this pattern.

## Variant Patterns

Auto-detected variants (see `VARIANT_PATTERNS` in code):
- Charged/uncharged
- Degraded states
- Locked/unlocked
- Broken/repaired
- Active/inactive
- Filled/empty
- Disassembled/assembled

## Notes

- Only items where **ALL** recipes require clog items are restricted
- If any recipe is clog-free, the item is not restricted
- Clog items can craft other clog items ("effective unlocking")

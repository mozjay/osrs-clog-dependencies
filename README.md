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

```bash
# Generate output (uses cached data if fresh)
python3 clog_dependency_builder.py

# Force refresh cached data
python3 clog_dependency_builder.py --refresh-cache

# Visualize dependencies for specific item
python3 clog_dependency_builder.py --visualize "Item Name"

# Custom output path
python3 clog_dependency_builder.py --output path/to/output.json
```

## Output

- Default: `output/clog_restrictions.json` (in this repo)
- Copy to clogman repo: `../clogman-mode/src/main/resources/clog_restrictions.json`

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

## File Structure

- `clog_dependency_builder.py` - Main script
- `manual_recipes.json` - Manually-defined derived items
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

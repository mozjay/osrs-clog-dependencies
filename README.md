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

## File Structure

- `clog_dependency_builder.py` - Main script
- `manual_recipes.json` - Manually-defined derived items
- `cache/` - Cached wiki data (7 day TTL)
- `output/` - Generated output files

## How It Works

1. **Fetch Data**: Collection log items, recipes, and item IDs from OSRS Wiki
2. **Build Recipe Graph**: Map items to their crafting materials
3. **Build Variant Relationships**: Link items to their variants (charged, broken, etc.)
4. **Find Dependencies**: Determine which items require clog unlocks
5. **Add Manual Recipes**: Merge manually-defined recipes
6. **Generate Output**: Create `clog_restrictions.json`

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

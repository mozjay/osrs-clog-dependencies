# OSRS Collection Log Dependency Builder

A Python tool that builds dependency chains for Old School RuneScape items based on collection log requirements. This generates the data file used by the [Clogman Mode](https://github.com/mozjay/clogman-mode) RuneLite plugin.

## Overview

This tool answers the question: **"What collection log items do I need to unlock before I can use/create this item?"**

For example:
- **Tormented bracelet** requires: Zenyte shard, Onyx
- **Soulreaper axe** requires: Leviathan's lure, Siren's staff, Executioner's axe head, Eye of the duke
- **Confliction gauntlets** requires: Zenyte shard, Onyx, Mokhaiotl cloth, Demon tear

## Installation

```bash
pip install -r requirements.txt
```

Requirements:
- Python 3.8+
- `requests` library

## Usage

### Basic Commands

```bash
# Generate full JSON output for the RuneLite plugin
python3 clog_dependency_builder.py

# Visualize dependencies for a specific item
python3 clog_dependency_builder.py --visualize "Tormented bracelet"
python3 clog_dependency_builder.py --visualize "Soulreaper axe"
python3 clog_dependency_builder.py --visualize "Confliction gauntlets"

# Specify custom output path
python3 clog_dependency_builder.py --output my_output.json

# Force refresh cached data from wiki (normally cached for 7 days)
python3 clog_dependency_builder.py --refresh-cache
```

### Example Output

```
============================================================
Dependency Chain for: Confliction gauntlets
============================================================

[Recipe Analysis - 1 recipe(s) found]
----------------------------------------
  Recipe 1: ✗ 4 CLOG deps
    Materials: tormented bracelet, mokhaiotl cloth, demon tear

[Restriction Status]
----------------------------------------
  RESTRICTED - All recipes require CLOG items

[Condensed View - Best recipe path to CLOG items]
----------------------------------------
Confliction gauntlets
│  ├─ mokhaiotl cloth [CLOG]
│  ├─ tormented bracelet
│  │  ├─ zenyte bracelet
│  │  │  ├─ zenyte
│  │  │  │  ├─ uncut zenyte
│  │  │  │  │  ├─ onyx [CLOG]
│  │  │  │  │  ├─ zenyte shard [CLOG]
│  ├─ demon tear [CLOG]

----------------------------------------
MINIMUM CLOG DEPENDENCIES (4 items):
----------------------------------------
  • Onyx (ID: 6573)
    Source: Chambers of Xeric
  • Zenyte shard (ID: 19529)
    Source: Glough's Experiments
  • Mokhaiotl cloth (ID: 31109)
    Source: Doom of Mokhaiotl
  • Demon tear (ID: 31111)
    Source: Doom of Mokhaiotl
```

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        OSRS Wiki API                            │
│  • Module:Collection_log/data.json (1,692 clog items)          │
│  • Bucket:Recipe API (7,152 recipes)                           │
│  • Prices API (for item ID resolution)                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (fetch once, cache locally)
┌─────────────────────────────────────────────────────────────────┐
│                      Local Cache (cache/)                       │
│  • clog_items.json - Collection log items with IDs & sources   │
│  • recipes.json - All crafting recipes with materials          │
│  • prices.json - Item name to ID mappings                      │
│  Cache valid for 7 days, then auto-refreshes                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (load into memory)
┌─────────────────────────────────────────────────────────────────┐
│                    DependencyResolver                           │
│  • Builds recipe graph: item → [recipe1, recipe2, ...]         │
│  • Tracks which items are collection log items                 │
│  • Calculates minimum clog dependencies per item               │
│  • Detects clog-to-clog crafting relationships                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (query in-memory)
┌─────────────────────────────────────────────────────────────────┐
│                         Output                                  │
│  • Visualization (--visualize)                                 │
│  • JSON export (--output)                                      │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Data Fetching** (only when cache is missing/expired)
   - Fetches collection log items from `Module:Collection_log/data.json`
   - Fetches all recipes from the wiki's Bucket API
   - Fetches item prices/IDs from the prices API
   - Saves to local cache files

2. **Graph Building** (every run, from cached data)
   - Loads cached data into memory
   - Builds a recipe graph: `item_name → [[recipe1_materials], [recipe2_materials], ...]`
   - Each item can have multiple recipes (different ways to craft it)

3. **Dependency Resolution**
   - For each item, recursively traces through its recipe materials
   - When a material is a collection log item, it's added as a dependency
   - When a material has recipes, recursively checks those too
   - **Key rule**: Only the recipe with MINIMUM clog dependencies is used

4. **Restriction Logic**
   - An item is **RESTRICTED** only if ALL its recipes require clog items
   - If ANY recipe is clog-free, the item is **NOT RESTRICTED**
   - Example: "Plank" has 5 recipes, all clog-free → not restricted

5. **Clog-to-Clog Crafting**
   - Some clog items can be crafted from other clog items (e.g., Onyx from Uncut onyx)
   - These relationships are tracked in the `craftable_from` field
   - Enables "effective unlocking" in the plugin

### Caching System

The cache system avoids unnecessary API calls:

| Scenario | API Calls | Speed |
|----------|-----------|-------|
| First run (no cache) | ~15 calls | ~20 seconds |
| Subsequent runs (cache valid) | 0 calls | < 1 second |
| With `--refresh-cache` | ~15 calls | ~20 seconds |

Cache files are stored in `cache/` directory:
- `clog_items.json` (~164KB) - Collection log items
- `recipes.json` (~3.7MB) - All crafting recipes
- `prices.json` (~1MB) - Item name to ID mappings

Cache expires after 7 days (configurable via `CACHE_MAX_AGE_DAYS`).

### Variant Handling

Items like charged weapons, locked capes, and broken variants don't have explicit crafting recipes but still depend on their base clog items. The tool automatically detects these relationships:

```
Variant Pattern Examples:
├─ "X (uncharged)" in clog → "X" depends on it (charged variants)
├─ "X" in clog → "X (l)" depends on it (locked variants)
├─ "X" in clog → "X (broken)" depends on it (broken variants)
└─ "X" in clog → "X (damaged)" depends on it (damaged variants)
```

**Examples:**
- `Tumeken's shadow` → depends on `Tumeken's shadow (uncharged)` [clog]
- `Infernal cape (l)` → depends on `Infernal cape` [clog]
- `Masori body (f)` → depends on `Masori body` [clog] + `Armadyl helmet` [clog via armadylean plate]

For items with existing recipes (like Masori body (f)), the base clog dependency is **added** to the recipe dependencies, ensuring both are required.

### Recipe Logic: Minimum Dependencies

The tool finds the **minimum** clog dependencies, not all possible ones:

```
Example: Gold bar has 3 recipes
├─ Recipe 1: gold ore → 0 clog deps ✓ (use this one)
├─ Recipe 2: gold ore + nature rune + fire rune (superheat) → 0 clog deps
└─ Recipe 3: gold ore + nature rune (blast furnace) → 0 clog deps

Result: Gold bar requires 0 clog items (NOT restricted)
```

```
Example: Tormented bracelet has 1 recipe
└─ Recipe 1: zenyte bracelet + runes → 2 clog deps
   └─ zenyte bracelet → zenyte → uncut zenyte → onyx + zenyte shard

Result: Tormented bracelet requires 2 clog items (RESTRICTED)
```

## Output Format

### JSON Structure (`clog_restrictions.json`)

```json
{
  "version": "1.0.0",
  "generated": "2026-01-15 14:25:45",
  "stats": {
    "total_clog_items": 1692,
    "total_derived_items": 627,
    "items_with_clog_free_recipes": 3156,
    "derived_items_with_multiple_ids": 70,
    "derived_items_without_ids": 91,
    "clog_items_with_multiple_ids": 169,
    "clog_items_craftable_from_other_clogs": 27
  },
  "collectionLogItems": {
    "6573": {
      "name": "Onyx",
      "tabs": ["Chambers of Xeric", "Fortis Colosseum"],
      "all_ids": [6573],
      "craftable_from": [[6571]]
    },
    "19529": {
      "name": "Zenyte shard",
      "tabs": ["Glough's Experiments"],
      "all_ids": [19529]
    }
  },
  "derivedItems": {
    "tormented bracelet": {
      "name": "tormented bracelet",
      "item_ids": [19544],
      "clog_dependencies": [6573, 19529]
    },
    "amulet of fury": {
      "name": "amulet of fury",
      "item_ids": [6585, 6586, 12436],
      "clog_dependencies": [6573]
    }
  }
}
```

### Field Descriptions

| Field | Description |
|-------|-------------|
| `collectionLogItems` | All items in the OSRS collection log, keyed by item ID |
| `derivedItems` | Items that require clog items to create, keyed by item name |
| `all_ids` | All variant IDs for a clog item (e.g., charged/uncharged states) |
| `item_ids` | All variant IDs for a derived item |
| `clog_dependencies` | Array of clog item IDs required to unlock this item |
| `craftable_from` | (Optional) Recipes to craft this clog item from other clog items |
| `tabs` | Collection log categories where the item appears |

### Craftable From Field

The `craftable_from` field enables "effective unlocking" - when a clog item can be crafted from other clog items:

```json
"6573": {
  "name": "Onyx",
  "craftable_from": [[6571]]  // Can be crafted from Uncut onyx (ID: 6571)
}
```

Structure: `[[recipe1_deps], [recipe2_deps], ...]`
- Outer list: OR (any recipe works)
- Inner list: AND (all deps in recipe needed)

This allows the plugin to treat Onyx as "effectively unlocked" if the player has Uncut onyx.

## Data Sources

All data comes from the [OSRS Wiki](https://oldschool.runescape.wiki/):

1. **Collection Log Items**
   - Source: `Module:Collection_log/data.json`
   - Contains all 1,692 collection log items with IDs and source tabs

2. **Crafting Recipes**
   - Source: Bucket API (`api.php?action=bucket&query=bucket('recipe')...`)
   - Contains 7,152 recipes with materials and outputs

3. **Item Prices/IDs**
   - Source: Prices API (`prices.runescape.wiki/api/v1/osrs/mapping`)
   - Used to resolve item names to IDs for derived items

## Limitations

1. **Recipe coverage**: Only items with wiki recipes are tracked. Some items may have undocumented creation methods.

2. **Derived items without IDs**: ~91 derived items couldn't have their IDs resolved (typically very obscure items).

3. **Alternative clog paths**: If an item requires "onyx" and onyx can be obtained from multiple clog sources (CoX, Fortis, etc.), only one source is listed.

## Output Statistics

When run, the tool generates:

- **1,692** collection log items tracked
- **627** derived items (require clog items for ALL recipes)
- **27** clog items craftable from other clog items
- **169** clog items with multiple variant IDs
- **70** derived items with multiple variant IDs
- **3,156** items skipped (have clog-free recipe alternatives)

## File Structure

```
osrs-clog-dependencies/
├── clog_dependency_builder.py   # Main script
├── clog_restrictions.json       # Generated output for RuneLite plugin
├── requirements.txt             # Python dependencies
├── README.md                    # This file
└── cache/
    ├── clog_items.json          # Cached collection log items
    ├── recipes.json             # Cached recipes
    └── prices.json              # Cached item prices/IDs
```

## Contributing

To refresh the data after a game update:

```bash
python3 clog_dependency_builder.py --refresh-cache --output clog_restrictions.json
```

This will fetch fresh data from the wiki and regenerate the output file.

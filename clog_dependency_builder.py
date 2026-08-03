#!/usr/bin/env python3
"""
OSRS Collection Log Dependency Builder

This script fetches collection log items and recipe data from the OSRS Wiki,
builds dependency chains, and outputs a JSON file for use with a RuneLite plugin.

Key behavior: An item is only considered "restricted" if ALL ways to create it
require collection log items. If any recipe is clog-free, the item is not restricted.

Usage:
    python clog_dependency_builder.py                      # Generate full JSON output
    python clog_dependency_builder.py --visualize "Item"   # Visualize specific item
    python clog_dependency_builder.py --refresh-cache      # Force refresh wiki data
"""

import json
import time
import argparse
import os
import hashlib
import requests
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict


# Configuration
DEFAULT_VERSION = "1.4.0"  # bootstrap fallback version, used only when no previous output file exists to read a prior version from
WIKI_API_BASE = "https://oldschool.runescape.wiki/api.php"
CLOG_DATA_URL = "https://oldschool.runescape.wiki/w/Module:Collection_log/data.json?action=raw"
PRICES_API_MAPPING = "https://prices.runescape.wiki/api/v1/osrs/mapping"
USER_AGENT = "OSRSClogDependencyBuilder/1.0 (Collection Log Plugin Data Generator)"
RATE_LIMIT_DELAY = 1.0  # seconds between API requests

# Cache configuration
CACHE_DIR = Path(__file__).parent / "cache"
CLOG_CACHE_FILE = CACHE_DIR / "clog_items.json"
RECIPES_CACHE_FILE = CACHE_DIR / "recipes.json"
ALL_ITEMS_CACHE_FILE = CACHE_DIR / "all_items.json"
PRICES_MAPPING_CACHE_FILE = CACHE_DIR / "prices_mapping.json"
CACHE_MAX_AGE_DAYS = 7

# Page name suffixes for wiki pages that document non-obtainable items
# (betas, interface-only graphics, discontinued items, minigame-exclusive
# replicas, etc.) which reuse a real item's display name under a different
# item ID. Entries with these page name suffixes are excluded when building
# the item ID map, so these IDs don't pollute the real item's variant list.
EXCLUDED_PAGE_NAME_SUFFIXES = (
    "(Last Man Standing)",
    "(beta)",
    "(unobtainable item)",
    "(interface item)",
    "(animation item)",
    "(discontinued)",
)

# Manual recipes file
# Contains manually-defined derived items that can't be auto-detected
# (e.g., items that share display names with base clog items)
MANUAL_RECIPES_FILE = Path(__file__).parent / "manual_recipes.json"

# Manual candidate review decisions file
# Records accept/decline decisions from the interactive manual recipe review,
# keyed by recipe output name, so previously-declined candidates aren't
# re-prompted unless their recipe data changes.
MANUAL_CANDIDATE_DECISIONS_FILE = Path(__file__).parent / "manual_candidate_decisions.json"

# Manual dependency overrides file
# Some items have no production recipe in the wiki's recipe graph, so the
# resolver treats them as freely-obtainable base materials with zero clog
# dependencies. This is correct for true base materials (ores, logs, etc.)
# but wrong for items that are only obtainable as byproducts of a clog-gated
# recipe (e.g. "Malformed infernal blend" is a failed-smithing byproduct of
# making Infernal blend from Oathplate shards, and reprocesses back into it -
# the wiki graph has no recipe for it, so without an override it looks like a
# free alternative material and masks the real Oathplate shards dependency
# for the whole Infernal plate chain). This file lets us specify the true
# clog dependency sets for such items, which then propagate automatically
# through the normal resolver logic.
MANUAL_DEPENDENCY_OVERRIDES_FILE = Path(__file__).parent / "manual_dependency_overrides.json"

# Variant patterns for items that don't have explicit recipes
# Format: (base_pattern, variant_pattern, description)
# If clog item matches base_pattern, look for items matching variant_pattern
VARIANT_PATTERNS = [
    # Charged variants: "X (uncharged)" in clog -> "X" is variant
    (" (uncharged)", "", "charged"),

    # Uncharged (u) variants: "X (u)" in clog -> "X" is variant (charged version)
    # This handles wilderness weapons like "Webweaver bow (u)" -> "Webweaver bow"
    (" (u)", "", "charged"),

    # Special uncharged -> "of the dead" variants: "X (uncharged)" -> "X of the dead"
    # This handles toxic staff: "Toxic staff (uncharged)" -> "Toxic staff of the dead"
    (" (uncharged)", " of the dead", "charged_of_the_dead"),

    # Degradation variants: "X (10)" in clog -> "X" is variant (fully degraded version)
    # This handles items like "Black mask (10)" -> "Black mask"
    (" (10)", "", "degraded"),

    # Locked variants: "X" in clog -> "X (l)" and "X (locked)" are variants
    ("", " (l)", "locked"),
    ("", " (locked)", "locked"),

    # Broken variants: "X" in clog -> "X (broken)" is variant
    ("", " (broken)", "broken"),

    # Damaged variants
    ("", " (damaged)", "damaged"),

    # Inactive variants: "X (inactive)" in clog -> "X" is variant
    (" (inactive)", "", "active"),

    # Reverse inactive: "X" (derived item) -> "X (inactive)" is variant
    ("", " (inactive)", "inactive"),

    # Empty variants: "X (empty)" in clog -> "X" is variant
    (" (empty)", "", "filled"),

    # Cosmetic silver variants: "X" in clog -> "X (s)" is variant
    ("", " (s)", "silver"),

    # Disassembled variants: "X (disassembled)" in clog -> "X" is variant
    (" (disassembled)", "", "assembled"),

    # Barrows degraded variants: "X" in clog -> "X 0", "X 25", etc. are variants
    # Note: space+number WITHOUT parentheses (unlike Black mask " (10)")
    ("", " 0", "barrows_degraded"),
    ("", " 25", "barrows_degraded"),
    ("", " 50", "barrows_degraded"),
    ("", " 75", "barrows_degraded"),
    ("", " 100", "barrows_degraded"),
]


@dataclass
class Item:
    """Represents an OSRS item with its dependencies."""
    item_id: int
    name: str
    is_clog_item: bool = False
    clog_tabs: List[str] = field(default_factory=list)


class CacheManager:
    """Manages local caching of wiki data."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True)

    def is_cache_valid(self, cache_file: Path, max_age_days: int = CACHE_MAX_AGE_DAYS) -> bool:
        """Check if cache file exists and is not too old."""
        if not cache_file.exists():
            return False

        age_seconds = time.time() - cache_file.stat().st_mtime
        age_days = age_seconds / (60 * 60 * 24)
        return age_days < max_age_days

    def load_cache(self, cache_file: Path) -> Optional[dict]:
        """Load data from cache file."""
        if not cache_file.exists():
            return None

        with open(cache_file, "r") as f:
            return json.load(f)

    def save_cache(self, cache_file: Path, data: dict):
        """Save data to cache file."""
        with open(cache_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Cached to {cache_file}")


class OSRSWikiClient:
    """Client for interacting with the OSRS Wiki API."""

    def __init__(self, cache_manager: CacheManager):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_request_time = 0
        self.cache = cache_manager

    def _rate_limit(self):
        """Ensure we don't exceed rate limits."""
        elapsed = time.time() - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self.last_request_time = time.time()

    def fetch_collection_log_items(self, force_refresh: bool = False) -> Dict[int, Item]:
        """Fetch all collection log items, using cache if available."""

        # Check cache first
        if not force_refresh and self.cache.is_cache_valid(CLOG_CACHE_FILE):
            print("Loading collection log items from cache...")
            cached = self.cache.load_cache(CLOG_CACHE_FILE)
            items = {}
            for item_id_str, data in cached.items():
                item_id = int(item_id_str)
                items[item_id] = Item(
                    item_id=item_id,
                    name=data["name"],
                    is_clog_item=True,
                    clog_tabs=data.get("tabs", [])
                )
            print(f"  Loaded {len(items)} collection log items from cache")
            return items

        # Fetch from wiki
        print("Fetching collection log items from wiki...")
        self._rate_limit()

        response = self.session.get(CLOG_DATA_URL)
        response.raise_for_status()

        clog_data = response.json()
        items = {}
        cache_data = {}

        for entry in clog_data:
            item_id = entry["id"]
            items[item_id] = Item(
                item_id=item_id,
                name=entry["name"],
                is_clog_item=True,
                clog_tabs=entry.get("tabs", [])
            )
            cache_data[str(item_id)] = {
                "name": entry["name"],
                "tabs": entry.get("tabs", [])
            }

        # Save to cache
        self.cache.save_cache(CLOG_CACHE_FILE, cache_data)
        print(f"  Found {len(items)} collection log items")
        return items

    def fetch_recipes_batch(self, offset: int = 0, limit: int = 500) -> List[dict]:
        """Fetch a batch of recipes from the Bucket API."""
        self._rate_limit()

        query = f"bucket('recipe').select('uses_material','production_json').offset({offset}).limit({limit}).run()"
        params = {
            "action": "bucket",
            "query": query,
            "format": "json"
        }

        response = self.session.get(WIKI_API_BASE, params=params)
        response.raise_for_status()

        data = response.json()
        return data.get("bucket", [])

    def fetch_all_recipes(self, force_refresh: bool = False) -> List[dict]:
        """Fetch all recipes, using cache if available."""

        # Check cache first
        if not force_refresh and self.cache.is_cache_valid(RECIPES_CACHE_FILE):
            print("Loading recipes from cache...")
            cached = self.cache.load_cache(RECIPES_CACHE_FILE)
            print(f"  Loaded {len(cached)} recipes from cache")
            return cached

        # Fetch from wiki
        print("Fetching recipe data from wiki...")
        all_recipes = []
        offset = 0
        batch_size = 500

        while True:
            batch = self.fetch_recipes_batch(offset, batch_size)
            if not batch:
                break
            all_recipes.extend(batch)
            print(f"  Fetched {len(all_recipes)} recipes...")
            offset += batch_size

        # Save to cache
        self.cache.save_cache(RECIPES_CACHE_FILE, all_recipes)
        print(f"  Total recipes: {len(all_recipes)}")
        return all_recipes

    def fetch_all_items_batch(self, offset: int = 0, limit: int = 500) -> List[dict]:
        """Fetch a batch of item names from the Bucket API."""
        self._rate_limit()

        query = f"bucket('infobox_item').select('item_name','item_id','page_name','quest','tradeable').offset({offset}).limit({limit}).run()"
        params = {
            "action": "bucket",
            "query": query,
            "format": "json"
        }

        response = self.session.get(WIKI_API_BASE, params=params)
        response.raise_for_status()

        data = response.json()
        return data.get("bucket", [])

    def fetch_prices_mapping(self, force_refresh: bool = False) -> Dict[str, int]:
        """Fetch item name to ID mapping from the prices API.

        This is the most reliable source for tradeable item IDs.
        """
        # Check cache first
        if not force_refresh and self.cache.is_cache_valid(PRICES_MAPPING_CACHE_FILE):
            cached = self.cache.load_cache(PRICES_MAPPING_CACHE_FILE)
            return cached

        print("Fetching item IDs from prices API...")
        self._rate_limit()
        try:
            response = self.session.get(PRICES_API_MAPPING)
            response.raise_for_status()
            data = response.json()

            mapping = {}
            for item in data:
                name = item.get("name", "").lower()
                item_id = item.get("id")
                if name and item_id:
                    mapping[name] = item_id

            self.cache.save_cache(PRICES_MAPPING_CACHE_FILE, mapping)
            print(f"  Loaded {len(mapping)} tradeable items from prices API")
            return mapping
        except Exception as e:
            print(f"  Warning: Failed to fetch prices mapping: {e}")
            return {}

    def fetch_all_items(self, force_refresh: bool = False) -> Tuple[Dict[str, int], Dict[str, List[int]], Dict[str, List[int]], Set[str]]:
        """Fetch all item names and IDs from wiki bucket API + prices API.

        The bucket API returns multiple entries for items with variants (LMS, imbued, etc.).
        We collect ALL IDs per item name, then use prices API to identify the primary ID.

        Returns a tuple of:
        - primary_ids: Dict mapping item_name (lowercase) -> primary item_id
        - all_ids: Dict mapping item_name (lowercase) -> list of all item IDs
        - page_ids: Dict mapping wiki page_name (lowercase) -> list of item IDs on that page
        - tradeable_names: Set of item_name (lowercase) considered tradeable, from
          either the prices API or the wiki infobox `tradeable` field (see below)

        For derived items:
        - Tradeable items: use primary_id (from prices API)
        - Items with real variants (e.g., imbued helms): all IDs are included

        page_ids is useful for resolving items whose display name (item_name)
        collides with another item's, but which have their own wiki page
        (e.g. "Granite maul (ornate handle)").

        tradeable_names combines two signals because neither is complete on
        its own: the prices API only lists items with real-time GE data, and
        the wiki's `tradeable` infobox field is missing on many genuinely
        tradeable items (e.g. "Dark bow", "Granite maul"). It's used as an OR
        rather than the sole signal so it can only make the redundant-item
        pruning in generate_output_json() more conservative, never less.
        """
        # Check cache first
        if not force_refresh and self.cache.is_cache_valid(ALL_ITEMS_CACHE_FILE):
            print("Loading all item names from cache...")
            cached = self.cache.load_cache(ALL_ITEMS_CACHE_FILE)
            if isinstance(cached, dict) and "primary_ids" in cached and "all_ids" in cached and "page_ids" in cached and "tradeable_names" in cached:
                print(f"  Loaded {len(cached['primary_ids'])} item names from cache")
                return cached["primary_ids"], cached["all_ids"], cached["page_ids"], set(cached["tradeable_names"])
            # Old cache format - need to regenerate
            print("  Old cache format detected, regenerating...")

        # Step 1: Fetch all items from bucket API, grouping by name
        print("Fetching all items from wiki bucket API...")
        all_ids_by_name: Dict[str, List[int]] = defaultdict(list)
        all_ids_by_page: Dict[str, List[int]] = defaultdict(list)
        # Per-name bookkeeping for the quest-duplicate filter below: every
        # entry seen for a name, tagged with whether its own `quest` field
        # marks it as a temporary quest-only duplicate, and whether its own
        # `tradeable` field is present.
        entries_by_name: Dict[str, List[Tuple[List[int], str, bool, bool]]] = defaultdict(list)
        offset = 0
        batch_size = 500
        total_entries = 0

        while True:
            batch = self.fetch_all_items_batch(offset, batch_size)
            if not batch:
                break

            for item in batch:
                name = item.get("item_name", "").lower()
                item_id_raw = item.get("item_id", [])
                page_name = item.get("page_name", "")
                quest = item.get("quest")
                # The wiki bucket represents this boolean field by key
                # presence rather than a Yes/No value: the key is present
                # (with an empty string value) when tradeable = Yes, and
                # absent entirely when tradeable = No or unset.
                is_wiki_tradeable = "tradeable" in item

                if not name:
                    continue

                # Skip pages documenting non-obtainable duplicates (betas,
                # LMS replicas, interface/animation items, etc.) that reuse
                # a real item's display name under a different item ID.
                if page_name.endswith(EXCLUDED_PAGE_NAME_SUFFIXES):
                    continue

                # The infobox `quest` field is a "[[Quest name]]" wikilink (or
                # sometimes a bare "Yes") for items tied to a quest. "Not tied
                # to a quest" is represented inconsistently: JSON null (field
                # absent), or the literal string "No" or "None" - all seen
                # across different item pages.
                is_quest_item = bool(quest) and quest not in ("No", "None")

                # Handle item_id being a list (bucket API returns arrays)
                ids = []
                if isinstance(item_id_raw, list):
                    for id_str in item_id_raw:
                        try:
                            ids.append(int(id_str))
                        except (ValueError, TypeError):
                            pass
                elif item_id_raw:
                    try:
                        ids.append(int(item_id_raw))
                    except (ValueError, TypeError):
                        pass

                entries_by_name[name].append((ids, page_name, is_quest_item, is_wiki_tradeable))

            total_entries += len(batch)
            if total_entries % 2000 == 0:
                print(f"  Fetched {total_entries} entries...")
            offset += batch_size

        wiki_tradeable_names: Set[str] = set()

        for name, entries in entries_by_name.items():
            # Some quest-flagged items are genuinely quest-only with no
            # separate permanent item under the same name (e.g. "Charcoal",
            # "Iban's staff (u)") - for those, quest status doesn't indicate
            # a duplicate and all entries should be kept as-is. Only drop
            # quest-flagged entries when a *non*-quest entry for the same
            # name also exists - that's the sign of a temporary quest-only
            # duplicate page reusing a real item's display name (e.g. "Vial
            # of blood (A Taste of Hope)" reusing "Vial of blood"'s name).
            # Without this, the duplicate's ID gets merged into the real
            # item's all_ids and can end up wrongly treated as a collection
            # log variant (e.g. incorrectly restricting "Vial of blood").
            has_non_quest_entry = any(not is_quest for _, _, is_quest, _ in entries)

            for ids, page_name, is_quest_item, is_wiki_tradeable in entries:
                if has_non_quest_entry and is_quest_item:
                    continue

                all_ids_by_name[name].extend(ids)
                if page_name:
                    all_ids_by_page[page_name.lower()].extend(ids)
                if is_wiki_tradeable:
                    wiki_tradeable_names.add(name)

        print(f"  Total: {total_entries} entries -> {len(all_ids_by_name)} unique item names")

        # De-duplicate and sort page ID lists
        page_ids: Dict[str, List[int]] = {
            page: sorted(set(ids)) for page, ids in all_ids_by_page.items() if ids
        }

        # Step 2: Get prices API mapping (authoritative for tradeable items)
        prices_mapping = self.fetch_prices_mapping(force_refresh)

        # Step 3: Build primary_ids mapping
        # Priority: prices API > manual overrides > lowest bucket ID
        primary_ids: Dict[str, int] = {}

        for name, ids in all_ids_by_name.items():
            # De-duplicate and sort the IDs list
            unique_ids = sorted(set(ids))
            all_ids_by_name[name] = unique_ids

            if not unique_ids:
                # No valid IDs for this item - skip
                continue

            if name in prices_mapping:
                # Prices API is authoritative for tradeable items
                primary_ids[name] = prices_mapping[name]
            else:
                # For untradeables, use the lowest ID (usually the original)
                primary_ids[name] = min(unique_ids)

        # Step 4: Ensure prices API IDs are included in all_ids
        # (in case bucket API didn't have them)
        for name, item_id in prices_mapping.items():
            if name in all_ids_by_name and item_id not in all_ids_by_name[name]:
                all_ids_by_name[name].append(item_id)
                all_ids_by_name[name].sort()
            elif name not in all_ids_by_name:
                all_ids_by_name[name] = [item_id]
                primary_ids[name] = item_id

        # Stats
        multi_id_count = sum(1 for ids in all_ids_by_name.values() if len(ids) > 1)
        print(f"  Items with multiple IDs: {multi_id_count}")

        # Combine both tradeability signals (see docstring for why neither
        # is used alone).
        tradeable_names: Set[str] = set(prices_mapping.keys()) | wiki_tradeable_names
        print(f"  Tradeable item names (GE or wiki-flagged): {len(tradeable_names)}")

        # Save to cache
        cache_data = {
            "primary_ids": primary_ids,
            "all_ids": dict(all_ids_by_name),
            "page_ids": page_ids,
            "tradeable_names": sorted(tradeable_names),
        }
        self.cache.save_cache(ALL_ITEMS_CACHE_FILE, cache_data)

        return primary_ids, dict(all_ids_by_name), page_ids, tradeable_names


class DependencyResolver:
    """Resolves collection log dependencies for items."""

    def __init__(self, clog_items: Dict[int, Item]):
        self.clog_items = clog_items
        self.clog_names = {item.name.lower(): item_id for item_id, item in clog_items.items()}
        self.dependency_overrides = load_manual_dependency_overrides()

        # Store recipes as list of recipes per item (not merged)
        # output_name -> [recipe1_materials, recipe2_materials, ...]
        self.recipes_by_item: Dict[str, List[List[str]]] = defaultdict(list)

        # Cache for dependency calculations
        self._dep_cache: Dict[str, Set[int]] = {}
        self._min_dep_cache: Dict[str, Tuple[Set[int], int]] = {}
        self._all_dep_sets_cache: Dict[str, List[frozenset]] = {}
        self._tradeable_intermediate_cache: Dict[str, bool] = {}

    def build_recipe_graph(self, recipes: List[dict]):
        """Build a graph of item -> list of recipes (each recipe is a list of materials)."""
        print("Building recipe graph...")

        for recipe in recipes:
            production_json = recipe.get("production_json")
            if not production_json:
                continue

            try:
                production = json.loads(production_json)
            except json.JSONDecodeError:
                continue

            # Output can be either an object or an empty string
            output = production.get("output", {})
            if not output or isinstance(output, str):
                continue

            output_name = output.get("name", "").lower().replace("#", " ")
            if not output_name:
                continue

            materials = production.get("materials", [])
            material_names = [m.get("name", "").lower().replace("#", " ") for m in materials if m.get("name")]

            if material_names:
                # Store this recipe separately (don't merge with other recipes)
                self.recipes_by_item[output_name].append(material_names)

        print(f"  Built graph with {len(self.recipes_by_item)} craftable items")

        # Count items with multiple recipes
        multi_recipe = sum(1 for recipes in self.recipes_by_item.values() if len(recipes) > 1)
        print(f"  Items with multiple recipes: {multi_recipe}")

    def build_variant_relationships(self, primary_ids: Dict[str, int], all_ids: Dict[str, List[int]]):
        """
        Build relationships between base items and their variants.

        Handles two cases:
        1. Clog items with variants (e.g., "Tumeken's shadow (uncharged)" -> "Tumeken's shadow")
        2. Derived items with variants (e.g., "Amulet of rancour" -> "Amulet of rancour (s)")

        Args:
            primary_ids: Dict mapping item_name (lowercase) -> primary item_id
            all_ids: Dict mapping item_name (lowercase) -> list of all item IDs (includes untradeable variants)
        """
        print("Building variant relationships...")

        variants_added = 0
        variants_updated = 0

        # Phase 1: Handle variants of clog items
        for clog_id, clog_item in self.clog_items.items():
            clog_name = clog_item.name.lower()
            added, updated = self._process_variant_patterns(clog_name, all_ids)
            variants_added += added
            variants_updated += updated

        print(f"  Phase 1 (clog variants): Added {variants_added}, updated {variants_updated}")

        # Phase 2: Handle variants of derived items (items with recipes that have clog deps)
        # We need to do this after phase 1, and we need to iterate over a copy since we modify recipes_by_item
        derived_added = 0
        derived_updated = 0

        derived_items_to_check = [
            name for name in list(self.recipes_by_item.keys())
            if name not in self.clog_names
        ]

        for item_name in derived_items_to_check:
            # Only process items that have clog dependencies
            deps = self.find_minimum_clog_dependencies(item_name)
            if deps:
                added, updated = self._process_variant_patterns(item_name, all_ids)
                derived_added += added
                derived_updated += updated

        print(f"  Phase 2 (derived variants): Added {derived_added}, updated {derived_updated}")
        variants_added += derived_added
        variants_updated += derived_updated

        print(f"  Total: Added {variants_added} variant items, updated {variants_updated} existing items")

    def _process_variant_patterns(self, base_name: str, all_ids: Dict[str, List[int]]) -> Tuple[int, int]:
        """
        Process variant patterns for a single base item.

        Args:
            base_name: The base item name to find variants for
            all_ids: Dict mapping item_name (lowercase) -> list of all item IDs (includes untradeable variants)

        Returns tuple of (variants_added, variants_updated)
        """
        added = 0
        updated = 0

        for base_suffix, variant_suffix, variant_type in VARIANT_PATTERNS:
            # Check if this item matches the base pattern
            if base_suffix:
                # Item has suffix (e.g., "(uncharged)"), look for item without
                if not base_name.endswith(base_suffix.lower()):
                    continue
                # Remove suffix to get base name, then add variant suffix
                stripped_name = base_name[:-len(base_suffix)]
                variant_name = stripped_name + variant_suffix.lower()
            else:
                # Item has no special suffix, look for item with variant suffix
                variant_name = base_name + variant_suffix.lower()

            # Check if variant exists in all items (including untradeable variants)
            if variant_name not in all_ids:
                continue

            # Check if variant is already a clog item (skip if so)
            if variant_name in self.clog_names:
                continue

            # Check if variant is the same as base (skip if so)
            if variant_name == base_name:
                continue

            # Check if variant already has a recipe
            if variant_name in self.recipes_by_item:
                # Has recipe - check if base item is already a material
                has_base = False
                for recipe in self.recipes_by_item[variant_name]:
                    if base_name in recipe:
                        has_base = True
                        break

                if not has_base:
                    # Add base item as an additional material to existing recipe
                    for recipe in self.recipes_by_item[variant_name]:
                        recipe.append(base_name)
                    # Clear cache since we modified recipes
                    if variant_name in self._min_dep_cache:
                        del self._min_dep_cache[variant_name]
                    if variant_name in self._all_dep_sets_cache:
                        del self._all_dep_sets_cache[variant_name]
                    updated += 1
            else:
                # No recipe - create a "virtual recipe" that just requires the base item
                self.recipes_by_item[variant_name] = [[base_name]]
                added += 1

        return added, updated

    def find_clog_dependencies_for_recipe(
        self,
        materials: List[str],
        visited: Optional[Set[str]] = None
    ) -> Set[int]:
        """Find clog dependencies for a specific recipe (list of materials)."""
        if visited is None:
            visited = set()

        dependencies = set()

        for material in materials:
            material_lower = material.lower()

            # Skip if already visited (cycle prevention)
            if material_lower in visited:
                continue

            # Check if this material is a clog item
            if material_lower in self.clog_names:
                dependencies.add(self.clog_names[material_lower])

            # Recursively check how this material is made
            material_deps = self.find_minimum_clog_dependencies(material_lower, visited.copy())
            dependencies.update(material_deps)

        return dependencies

    def find_minimum_clog_dependencies(
        self,
        item_name: str,
        visited: Optional[Set[str]] = None
    ) -> Set[int]:
        """
        Find the MINIMUM clog dependencies needed to create an item.

        If multiple recipes exist, return the one with the fewest clog dependencies.
        If any recipe has zero clog dependencies, return empty set.
        """
        if visited is None:
            visited = set()

        item_name_lower = item_name.lower()

        # Check cache
        if item_name_lower in self._min_dep_cache:
            return self._min_dep_cache[item_name_lower][0]

        # Prevent infinite loops
        if item_name_lower in visited:
            return set()
        visited.add(item_name_lower)

        # If this item is itself a clog item, it requires itself
        if item_name_lower in self.clog_names:
            result = {self.clog_names[item_name_lower]}
            self._min_dep_cache[item_name_lower] = (result, 0)
            return result

        # Manual override for items the recipe graph misrepresents as free
        # base materials (see MANUAL_DEPENDENCY_OVERRIDES_FILE)
        if item_name_lower in self.dependency_overrides:
            sets = self.dependency_overrides[item_name_lower]
            result = min(sets, key=len)
            self._min_dep_cache[item_name_lower] = (set(result), -1)
            return set(result)

        # Get all recipes for this item
        recipes = self.recipes_by_item.get(item_name_lower, [])

        if not recipes:
            # No recipes - this is a base item (ore, logs, etc.) - no clog deps
            self._min_dep_cache[item_name_lower] = (set(), -1)
            return set()

        # Find the recipe with minimum clog dependencies
        min_deps = None
        min_recipe_idx = -1

        for idx, recipe_materials in enumerate(recipes):
            recipe_deps = self.find_clog_dependencies_for_recipe(recipe_materials, visited.copy())

            if min_deps is None or len(recipe_deps) < len(min_deps):
                min_deps = recipe_deps
                min_recipe_idx = idx

                # Early exit: if we found a clog-free recipe, use it
                if len(recipe_deps) == 0:
                    break

        result = min_deps if min_deps is not None else set()
        self._min_dep_cache[item_name_lower] = (result, min_recipe_idx)
        return result

    def _find_all_dep_sets_for_recipe(
        self,
        materials: List[str],
        visited: Set[str]
    ) -> List[frozenset]:
        """
        Find all possible dependency sets for a single recipe.

        For each material, get all possible dependency sets (recursively).
        Return the Cartesian product of all materials' dependency sets,
        with each combination merged (union) into a single set.

        Returns empty list if recipe has no dependencies (base materials only).
        """
        # Collect all possible dep sets for each material
        material_dep_sets: List[List[frozenset]] = []

        for material in materials:
            material_lower = material.lower()

            # Skip if already visited (cycle prevention)
            if material_lower in visited:
                continue

            # Check if this material is a clog item
            if material_lower in self.clog_names:
                # This material IS a clog item - it has one dep set: itself
                material_dep_sets.append([frozenset({self.clog_names[material_lower]})])
            else:
                # Recursively get all dep sets for this material
                material_all_sets = self.find_all_minimum_clog_dependency_sets(material_lower, visited.copy())
                if material_all_sets:
                    material_dep_sets.append(material_all_sets)
                # If empty, material has no clog deps - doesn't contribute to Cartesian product

        # If no materials have dependencies, return empty list (no restriction)
        if not material_dep_sets:
            return []

        # Compute Cartesian product: all combinations of picking one dep set from each material
        # Each combination is the union of the selected dep sets
        import itertools

        result_sets = []
        for combination in itertools.product(*material_dep_sets):
            # Union all dep sets in this combination
            merged = frozenset().union(*combination)
            result_sets.append(merged)

        return result_sets

    def find_all_minimum_clog_dependency_sets(
        self,
        item_name: str,
        visited: Optional[Set[str]] = None
    ) -> List[frozenset]:
        """
        Find ALL minimum clog dependency sets needed to create an item.

        Returns a list of frozensets, where each frozenset represents one valid
        combination of clog items that would unlock this item.

        Only returns sets of the minimum size - if multiple recipes exist,
        only the shortest dependency paths are included.

        Example: Armadylean plate can be made from any Armadyl piece, so returns:
            [frozenset({11826}), frozenset({11828}), frozenset({11830})]

        Returns empty list if any recipe has zero clog dependencies.
        """
        if visited is None:
            visited = set()

        item_name_lower = item_name.lower()

        # Check cache
        if item_name_lower in self._all_dep_sets_cache:
            return self._all_dep_sets_cache[item_name_lower]

        # Prevent infinite loops
        if item_name_lower in visited:
            return []
        visited.add(item_name_lower)

        # If this item is itself a clog item, it requires itself
        if item_name_lower in self.clog_names:
            result = [frozenset({self.clog_names[item_name_lower]})]
            self._all_dep_sets_cache[item_name_lower] = result
            return result

        # Manual override for items the recipe graph misrepresents as free
        # base materials (see MANUAL_DEPENDENCY_OVERRIDES_FILE)
        if item_name_lower in self.dependency_overrides:
            result = self.dependency_overrides[item_name_lower]
            self._all_dep_sets_cache[item_name_lower] = result
            return result

        # Get all recipes for this item
        recipes = self.recipes_by_item.get(item_name_lower, [])

        if not recipes:
            # No recipes - this is a base item (ore, logs, etc.) - no clog deps
            self._all_dep_sets_cache[item_name_lower] = []
            return []

        # Collect all dep sets from all recipes
        all_dep_sets: List[frozenset] = []

        for recipe_materials in recipes:
            recipe_dep_sets = self._find_all_dep_sets_for_recipe(recipe_materials, visited.copy())

            # If this recipe has no dependencies, return empty immediately (unrestricted)
            if not recipe_dep_sets:
                self._all_dep_sets_cache[item_name_lower] = []
                return []

            all_dep_sets.extend(recipe_dep_sets)

        # If we have no dep sets at all, return empty
        if not all_dep_sets:
            self._all_dep_sets_cache[item_name_lower] = []
            return []

        # Find minimum size
        min_size = min(len(s) for s in all_dep_sets)

        # Keep only sets of minimum size and deduplicate
        minimal_sets = list({s for s in all_dep_sets if len(s) == min_size})

        # Cap at 50 combinations to prevent explosion
        if len(minimal_sets) > 50:
            minimal_sets = minimal_sets[:50]

        self._all_dep_sets_cache[item_name_lower] = minimal_sets
        return minimal_sets

    def has_tradeable_clog_gated_intermediate(
        self,
        item_name: str,
        tradeable_names: Set[str],
        visited: Optional[Set[str]] = None
    ) -> bool:
        """
        Check whether ANY recipe path below item_name (not just the minimal
        dependency-set path) passes through a tradeable, non-clog
        intermediate material that itself sits on a path to a terminal clog
        item (i.e. find_minimum_clog_dependencies(material) is non-empty).

        Used to validate the "redundant untradeable restriction" pruning in
        generate_output_json()/process_manual_recipes(): if a pre-built,
        clog-gated intermediate can be bought on the GE (e.g. "Soulreaper
        axe", tradeable, itself requires the 4 DT2 uniques), a player can buy
        it and skip ever personally unlocking the terminal clog item(s)
        themselves - so pruning the item as a "no-op restriction" would be
        wrong.

        Clog-free branches (materials with no clog dependency of their own,
        e.g. a display case built from mahogany plank + gold leaf) are
        deliberately NOT flagged even if tradeable - their tradeability is
        irrelevant to whether a clog-gated branch elsewhere in the recipe can
        be bypassed. Clog items themselves are leaves, matching
        _find_all_dep_sets_for_recipe's treatment - their own tradeability is
        already checked separately by callers.

        Cached by item name only (not by visited-path), matching
        _min_dep_cache's existing behavior - the item graph is effectively a
        DAG in practice so this is safe.
        """
        if visited is None:
            visited = set()

        item_name_lower = item_name.lower()

        if item_name_lower in self._tradeable_intermediate_cache:
            return self._tradeable_intermediate_cache[item_name_lower]

        if item_name_lower in visited:
            return False
        visited.add(item_name_lower)

        if item_name_lower in self.clog_names:
            self._tradeable_intermediate_cache[item_name_lower] = False
            return False

        result = False
        for recipe_materials in self.recipes_by_item.get(item_name_lower, []):
            for material in recipe_materials:
                material_lower = material.lower()
                if material_lower in visited or material_lower in self.clog_names:
                    continue
                # Only materials that themselves carry a clog-dependency
                # footprint are relevant - a clog-free branch can't be a
                # bypass route for a clog-gated one.
                if not self.find_minimum_clog_dependencies(material_lower):
                    continue
                if material_lower in tradeable_names:
                    result = True
                    break
                if self.has_tradeable_clog_gated_intermediate(material_lower, tradeable_names, visited.copy()):
                    result = True
                    break
            if result:
                break

        self._tradeable_intermediate_cache[item_name_lower] = result
        return result

    def is_item_restricted(self, item_name: str) -> bool:
        """
        Check if an item should be restricted.

        Returns True only if ALL recipes require clog items.
        """
        deps = self.find_minimum_clog_dependencies(item_name)
        return len(deps) > 0

    def get_all_recipes_with_deps(self, item_name: str) -> List[Tuple[List[str], Set[int]]]:
        """Get all recipes for an item with their clog dependencies."""
        item_name_lower = item_name.lower()
        recipes = self.recipes_by_item.get(item_name_lower, [])

        result = []
        for recipe_materials in recipes:
            deps = self.find_clog_dependencies_for_recipe(recipe_materials, set())
            result.append((recipe_materials, deps))

        return result

    def get_dependency_chain(
        self,
        item_name: str,
        indent: int = 0,
        visited: Optional[Set[str]] = None,
        clog_only: bool = False,
        show_best_recipe: bool = True
    ) -> List[str]:
        """
        Get a visual representation of the dependency chain.

        Args:
            item_name: The item to visualize
            indent: Current indentation level
            visited: Set of already visited items (to prevent cycles)
            clog_only: If True, only show items that lead to clog dependencies
            show_best_recipe: If True, only show the recipe with minimum clog deps
        """
        if visited is None:
            visited = set()

        item_name_lower = item_name.lower()
        lines = []

        # Prevent infinite loops
        if item_name_lower in visited:
            return []
        visited.add(item_name_lower)

        # Check if this item or any of its children have clog dependencies
        is_clog = item_name_lower in self.clog_names
        min_deps = self.find_minimum_clog_dependencies(item_name_lower, set())

        # If clog_only mode and this branch has no clog items, skip it
        if clog_only and not min_deps and not is_clog:
            return []

        # Mark clog items
        clog_marker = " [CLOG]" if is_clog else ""

        # Get the best recipe (minimum clog deps)
        recipes = self.recipes_by_item.get(item_name_lower, [])

        if show_best_recipe and len(recipes) > 1:
            # Find best recipe
            best_idx = self._min_dep_cache.get(item_name_lower, (None, 0))[1]
            if best_idx >= 0 and best_idx < len(recipes):
                recipes = [recipes[best_idx]]

        # Get unique materials from selected recipe(s)
        all_materials = set()
        for recipe in recipes:
            all_materials.update(recipe)
        materials = list(all_materials)

        # Build child lines
        child_lines = []
        for material in materials:
            sub_lines = self.get_dependency_chain(
                material, indent + 3, visited.copy(), clog_only, show_best_recipe
            )
            child_lines.extend(sub_lines)

        # Add this node
        prefix = "│  " * (indent // 3) if indent > 0 else ""
        connector = "├─ " if indent > 0 else ""
        lines.append(f"{prefix}{connector}{item_name}{clog_marker}")

        # Add children
        lines.extend(child_lines)

        return lines

    def get_clog_only_chain(self, item_name: str) -> List[str]:
        """Get a condensed view showing only the path to clog items."""
        return self.get_dependency_chain(item_name, clog_only=True)


def visualize_item(resolver: DependencyResolver, item_name: str, clog_items: Dict[int, Item]):
    """Print a visual representation of an item's dependency chain."""
    print(f"\n{'='*60}")
    print(f"Dependency Chain for: {item_name}")
    print(f"{'='*60}")

    # Get all recipes with their dependencies
    all_recipes = resolver.get_all_recipes_with_deps(item_name)

    if all_recipes:
        print(f"\n[Recipe Analysis - {len(all_recipes)} recipe(s) found]")
        print(f"{'-'*40}")
        for idx, (materials, deps) in enumerate(all_recipes):
            dep_count = len(deps)
            status = "✓ CLOG-FREE" if dep_count == 0 else f"✗ {dep_count} CLOG deps"
            print(f"  Recipe {idx + 1}: {status}")
            print(f"    Materials: {', '.join(materials[:5])}{'...' if len(materials) > 5 else ''}")

    # Get minimum clog dependencies
    min_deps = resolver.find_minimum_clog_dependencies(item_name)
    is_restricted = len(min_deps) > 0

    print(f"\n[Restriction Status]")
    print(f"{'-'*40}")
    if is_restricted:
        print(f"  RESTRICTED - All recipes require CLOG items")
    else:
        print(f"  NOT RESTRICTED - At least one CLOG-free recipe exists")

    # Show condensed clog-only chain
    if min_deps:
        print(f"\n[Condensed View - Best recipe path to CLOG items]")
        print(f"{'-'*40}")
        clog_chain_lines = resolver.get_clog_only_chain(item_name)
        for line in clog_chain_lines:
            print(line)

        # Summary of minimum clog dependencies
        print(f"\n{'-'*40}")
        print(f"MINIMUM CLOG DEPENDENCIES ({len(min_deps)} items):")
        print(f"{'-'*40}")

        for dep_id in sorted(min_deps):
            if dep_id in clog_items:
                item = clog_items[dep_id]
                tabs = ", ".join(item.clog_tabs[:2])
                if len(item.clog_tabs) > 2:
                    tabs += "..."
                print(f"  • {item.name} (ID: {dep_id})")
                print(f"    Source: {tabs}")
            else:
                print(f"  • Unknown item (ID: {dep_id})")

    print()


def find_clog_crafting_recipes(
    item_name_lower: str,
    resolver: 'DependencyResolver'
) -> Optional[List[List[int]]]:
    """
    Find the clog items that can effectively unlock this clog item via crafting,
    by resolving this item's own recipes' transitive clog dependencies.

    Returns the minimal sets of clog item IDs needed (excluding this item itself),
    or None if no such recipes exist.

    Structure: [[recipe1_deps], [recipe2_deps], ...]
    - Outer list: OR (any recipe works)
    - Inner list: AND (all deps in recipe needed)
    """
    self_id = resolver.clog_names[item_name_lower]
    recipes = resolver.recipes_by_item.get(item_name_lower, [])

    all_dep_sets: List[frozenset] = []
    for recipe_materials in recipes:
        for dep_set in resolver._find_all_dep_sets_for_recipe(recipe_materials, {item_name_lower}):
            dep_set = dep_set - {self_id}
            if dep_set:
                all_dep_sets.append(dep_set)

    if not all_dep_sets:
        return None

    # Keep only the minimal-size sets and deduplicate
    min_size = min(len(s) for s in all_dep_sets)
    minimal_sets = {s for s in all_dep_sets if len(s) == min_size}

    return sorted(sorted(s) for s in minimal_sets)


def load_manual_dependency_overrides() -> Dict[str, List[frozenset]]:
    """
    Load manual clog dependency overrides from manual_dependency_overrides.json.

    Returns a dict mapping item name (lowercase) -> list of frozensets, in the
    same format as DependencyResolver.find_all_minimum_clog_dependency_sets().
    """
    if not MANUAL_DEPENDENCY_OVERRIDES_FILE.exists():
        return {}

    try:
        with open(MANUAL_DEPENDENCY_OVERRIDES_FILE, 'r') as f:
            raw = json.load(f)
            overrides = {
                name.lower(): [frozenset(s) for s in entry["clog_dependencies"]]
                for name, entry in raw.items()
            }
            print(f"  Loaded {len(overrides)} manual dependency overrides from {MANUAL_DEPENDENCY_OVERRIDES_FILE.name}")
            return overrides
    except Exception as e:
        print(f"  Warning: Failed to load {MANUAL_DEPENDENCY_OVERRIDES_FILE}: {e}")
        return {}


def load_manual_recipes() -> Dict[str, dict]:
    """
    Load manually-defined derived items from manual_recipes.json.

    Returns dict in the same format as derivedItems output.
    """
    if not MANUAL_RECIPES_FILE.exists():
        print(f"  No manual recipes file found at {MANUAL_RECIPES_FILE}")
        return {}

    try:
        with open(MANUAL_RECIPES_FILE, 'r') as f:
            manual_recipes = json.load(f)
            print(f"  Loaded {len(manual_recipes)} manual recipes from {MANUAL_RECIPES_FILE.name}")
            return manual_recipes
    except Exception as e:
        print(f"  Warning: Failed to load {MANUAL_RECIPES_FILE}: {e}")
        return {}


def process_manual_recipes(clog_items_output, derived_items_output, manual_recipes, clog_id_to_name, tradeable_names, resolver):
    """
    Add manual recipes to derived items and remove their IDs from clog item variants.

    Args:
        clog_items_output: Dict of clog items being built for output
        derived_items_output: Dict of derived items being built for output
        manual_recipes: Dict of manual recipes loaded from JSON
        clog_id_to_name: Dict mapping clog item_id -> item name (lowercase)
        tradeable_names: Set of item names (lowercase) considered tradeable
        resolver: DependencyResolver, used to check for tradeable clog-gated
            intermediates (see has_tradeable_clog_gated_intermediate)
    """
    if not manual_recipes:
        return

    added = 0
    redundant = 0

    for item_name, recipe in manual_recipes.items():
        item_name_lower = item_name.lower()

        # Same untradeable-dependency pruning as the auto-detected recipes in
        # generate_output_json(): if neither this item, nor any of its
        # dependencies, nor any tradeable clog-gated intermediate along the
        # way (e.g. a tradeable base item used to craft an untradeable
        # variant) can be traded, restricting it is a no-op (see that
        # function's comment for the full reasoning).
        dep_sets = recipe["clog_dependencies"]
        if (
            item_name_lower not in tradeable_names
            and all(
                all(clog_id_to_name.get(cid, "") not in tradeable_names for cid in dep_set)
                for dep_set in dep_sets
            )
            and not resolver.has_tradeable_clog_gated_intermediate(item_name_lower, tradeable_names)
        ):
            redundant += 1
            continue

        # Add directly to derived items (already in correct format)
        derived_items_output[item_name_lower] = recipe
        added += 1

        # Remove these IDs from clog item all_ids to prevent double-counting
        ids_to_remove = set(recipe["item_ids"])
        for clog_id, clog_entry in clog_items_output.items():
            original = clog_entry.get("all_ids", [])
            filtered = [id for id in original if id not in ids_to_remove]
            if len(filtered) < len(original):
                clog_entry["all_ids"] = filtered

    print(f"  Added {added} manual recipes to derived items")
    if redundant:
        print(f"  Pruned {redundant} manual recipes with untradeable-only dependencies (restriction would be a no-op)")

    return redundant


def _recipe_hash(recipes_list: List[List[str]]) -> str:
    """Compute a stable hash of a recipe's material lists, used to detect
    when a candidate's recipe data has changed since it was last reviewed."""
    normalized = sorted(sorted(materials) for materials in recipes_list)
    return hashlib.md5(json.dumps(normalized).encode()).hexdigest()[:12]


def _parse_version(version_str: str) -> Tuple[int, int, int]:
    """Parse a plain X.Y.Z version string into a (major, minor, patch) tuple."""
    parts = (version_str.split(".") + ["0", "0", "0"])[:3]
    return tuple(int(p) for p in parts)


def _bump_patch_version(version_str: str) -> str:
    """Bump the patch component (e.g. '1.4.0' -> '1.4.1'). Used for the
    automatic version bump when a routine data refresh changes the item
    list. Minor/major bumps are always manual (via --set-version) - they
    signal an intentional logic/behavior change, which can't be reliably
    distinguished from "the wiki added new items" by diffing output alone."""
    major, minor, patch = _parse_version(version_str)
    return f"{major}.{minor}.{patch + 1}"


def find_manual_recipe_candidates(
    resolver: 'DependencyResolver',
    primary_ids: Dict[str, int],
    all_ids: Dict[str, List[int]],
    page_ids: Dict[str, List[int]],
    manual_recipes: Dict[str, dict]
) -> List[dict]:
    """
    Find recipe outputs that are clog-derived but have no auto-detected item ID
    and aren't already covered by manual_recipes.json.

    These are candidates for manual_recipes.json entries - typically items that
    share a display name with another item and so have no distinct entry in the
    wiki's item ID database.
    """
    manual_names = {name.lower() for name in manual_recipes.keys()}

    candidates = []
    for output_name, recipes_list in resolver.recipes_by_item.items():
        if output_name in resolver.clog_names:
            continue
        if output_name in manual_names:
            continue
        if output_name in primary_ids or output_name in all_ids:
            continue  # auto-resolvable

        all_dep_sets = resolver.find_all_minimum_clog_dependency_sets(output_name)
        if not all_dep_sets:
            continue  # not clog-derived

        candidates.append({
            'name': output_name,
            'recipes': recipes_list,
            'clog_dependencies': [sorted(s) for s in all_dep_sets],
            'suggested_item_ids': page_ids.get(output_name, []),
        })

    return candidates


def review_manual_candidates(
    resolver: 'DependencyResolver',
    primary_ids: Dict[str, int],
    all_ids: Dict[str, List[int]],
    page_ids: Dict[str, List[int]],
    clog_items: Dict[int, Item],
    manual_recipes: Dict[str, dict]
):
    """
    Interactively review candidates for manual_recipes.json.

    For each candidate not previously reviewed (or whose recipe has changed
    since it was last reviewed), prompts the user to either provide item ID(s)
    to add it to manual_recipes.json, or decline it. Declined candidates are
    recorded in MANUAL_CANDIDATE_DECISIONS_FILE and won't be re-prompted unless
    their recipe data changes.
    """
    candidates = find_manual_recipe_candidates(resolver, primary_ids, all_ids, page_ids, manual_recipes)

    if MANUAL_CANDIDATE_DECISIONS_FILE.exists():
        with open(MANUAL_CANDIDATE_DECISIONS_FILE, 'r') as f:
            decisions = json.load(f)
    else:
        decisions = {}

    clog_id_to_name = {item_id: item.name for item_id, item in clog_items.items()}

    new_count = 0
    for candidate in candidates:
        name = candidate['name']
        h = _recipe_hash(candidate['recipes'])

        decision = decisions.get(name)
        if decision and decision.get('recipe_hash') == h and decision.get('status') == 'declined':
            continue

        if not candidate['suggested_item_ids']:
            decisions[name] = {"status": "declined", "recipe_hash": h, "reason": "no item ID found on wiki - no resolution possible"}
            with open(MANUAL_CANDIDATE_DECISIONS_FILE, 'w') as f:
                json.dump(decisions, f, indent=2)
            continue

        new_count += 1
        print("\n" + "=" * 60)
        print(f"Candidate: {name}")
        print("Recipes:")
        for materials in candidate['recipes']:
            print(f"  - {' + '.join(materials)}")
        print("Clog dependencies:")
        for dep_set in candidate['clog_dependencies']:
            dep_names = ", ".join(clog_id_to_name.get(i, str(i)) for i in dep_set)
            print(f"  - {dep_set} ({dep_names})")

        suggested_item_ids = candidate['suggested_item_ids']
        print(f"Suggested item IDs (from wiki page): {suggested_item_ids}")
        prompt = (
            "Enter item ID(s) comma-separated to add to manual_recipes.json, "
            "'a' to accept the suggested IDs, 'd' to decline, or Enter to skip for now: "
        )

        response = input(prompt).strip()

        if not response:
            print("  Skipped (will ask again next time)")
            continue

        if response.lower() == 'd':
            decisions[name] = {"status": "declined", "recipe_hash": h}
            print("  Declined")
        else:
            if response.lower() == 'a':
                item_ids = suggested_item_ids
            else:
                try:
                    item_ids = [int(x.strip()) for x in response.split(",") if x.strip()]
                except ValueError:
                    print(f"  Could not parse '{response}' as item ID(s) - skipped")
                    continue

            manual_recipes[name] = {
                "name": name,
                "item_ids": item_ids,
                "clog_dependencies": candidate['clog_dependencies'],
            }
            decisions[name] = {"status": "accepted", "recipe_hash": h}
            print(f"  Added to manual_recipes.json with item_ids {item_ids}")

        with open(MANUAL_RECIPES_FILE, 'w') as f:
            json.dump(manual_recipes, f, indent=2)
        with open(MANUAL_CANDIDATE_DECISIONS_FILE, 'w') as f:
            json.dump(decisions, f, indent=2)

    if new_count == 0:
        print("\nNo new manual recipe candidates to review.")
    else:
        print(f"\nReviewed {new_count} candidate(s).")


CHANGELOG_FILE = Path(__file__).parent / "CHANGELOG.md"
CHANGELOG_HEADER = "# Changelog\n\n"


def _normalize_dep_sets(dep_sets: Optional[List[List[int]]]) -> List[Tuple[int, ...]]:
    """Order-independent normal form for an OR-of-AND clog-id list-of-lists,
    so harmless list-ordering nondeterminism upstream (e.g. the resolver
    dedups dependency sets via a set comprehension in
    find_all_minimum_clog_dependency_sets) doesn't register as a diff."""
    return sorted(tuple(sorted(s)) for s in (dep_sets or []))


def compute_output_diff(old_output: Optional[dict], new_output: dict) -> dict:
    """
    Compare two parsed clog_restrictions.json-shaped dicts (only
    collectionLogItems/derivedItems are inspected; version/generated/stats
    are ignored) and return a structured diff used both to write
    CHANGELOG.md entries and to decide the auto version bump.

    old_output may be None (bootstrap: no previous file on disk yet).
    """
    diff = {
        "initial": old_output is None,
        "collectionLogItems": {"added": [], "removed": [], "changed": []},
        "derivedItems": {"added": [], "removed": [], "changed": []},
    }

    new_clog = new_output.get("collectionLogItems", {})
    new_derived = new_output.get("derivedItems", {})

    if old_output is None:
        diff["collectionLogItems"]["added"] = sorted(new_clog.keys(), key=int)
        diff["derivedItems"]["added"] = sorted(new_derived.keys())
        diff["has_changes"] = bool(diff["collectionLogItems"]["added"] or diff["derivedItems"]["added"])
        return diff

    old_clog = old_output.get("collectionLogItems", {})
    old_ids, new_ids = set(old_clog.keys()), set(new_clog.keys())
    diff["collectionLogItems"]["added"] = sorted(new_ids - old_ids, key=int)
    diff["collectionLogItems"]["removed"] = sorted(old_ids - new_ids, key=int)

    for cid in sorted(old_ids & new_ids, key=int):
        old_e, new_e = old_clog[cid], new_clog[cid]
        changes = {}
        if sorted(old_e.get("all_ids", [])) != sorted(new_e.get("all_ids", [])):
            changes["all_ids"] = (sorted(old_e.get("all_ids", [])), sorted(new_e.get("all_ids", [])))
        old_cf = _normalize_dep_sets(old_e.get("craftable_from"))
        new_cf = _normalize_dep_sets(new_e.get("craftable_from"))
        if old_cf != new_cf:
            changes["craftable_from"] = (old_cf, new_cf)
        if changes:
            diff["collectionLogItems"]["changed"].append(
                {"id": cid, "name": new_e.get("name", old_e.get("name", "")), "changes": changes}
            )

    old_derived = old_output.get("derivedItems", {})
    old_names, new_names = set(old_derived.keys()), set(new_derived.keys())
    diff["derivedItems"]["added"] = sorted(new_names - old_names)
    diff["derivedItems"]["removed"] = sorted(old_names - new_names)

    for name in sorted(old_names & new_names):
        old_e, new_e = old_derived[name], new_derived[name]
        changes = {}
        if sorted(old_e.get("item_ids", [])) != sorted(new_e.get("item_ids", [])):
            changes["item_ids"] = (sorted(old_e.get("item_ids", [])), sorted(new_e.get("item_ids", [])))
        old_deps = _normalize_dep_sets(old_e.get("clog_dependencies"))
        new_deps = _normalize_dep_sets(new_e.get("clog_dependencies"))
        if old_deps != new_deps:
            changes["clog_dependencies"] = (old_deps, new_deps)
        if changes:
            diff["derivedItems"]["changed"].append({"name": name, "changes": changes})

    diff["has_changes"] = any(
        diff["collectionLogItems"][k] for k in ("added", "removed", "changed")
    ) or any(
        diff["derivedItems"][k] for k in ("added", "removed", "changed")
    )
    return diff


def format_changelog_entry(diff: dict, version: str, date: Optional[str] = None) -> Optional[str]:
    """Render a compute_output_diff() result as a Keep-a-Changelog-style
    Markdown entry. Returns None when there's nothing to report (no
    changes, not an initial run) so callers can skip writing anything."""
    if date is None:
        date = time.strftime("%Y-%m-%d")

    if diff.get("initial"):
        return f"## [{version}] - {date}\n\nInitial generation.\n"

    if not diff.get("has_changes"):
        return None

    lines = [f"## [{version}] - {date}", ""]

    clog = diff["collectionLogItems"]
    if clog["added"] or clog["removed"] or clog["changed"]:
        lines.append("### Collection Log Items")
        lines += [f"+ Added: {cid}" for cid in clog["added"]]
        lines += [f"- Removed: {cid}" for cid in clog["removed"]]
        lines += [
            f"~ Changed: {e['name']} (ID {e['id']}) - {', '.join(e['changes'].keys())} updated"
            for e in clog["changed"]
        ]
        lines.append("")

    derived = diff["derivedItems"]
    if derived["added"] or derived["removed"] or derived["changed"]:
        lines.append("### Derived Items")
        lines += [f"+ Added: {name}" for name in derived["added"]]
        lines += [f"- Removed: {name}" for name in derived["removed"]]
        lines += [
            f"~ Changed: {e['name']} - {', '.join(e['changes'].keys())} updated"
            for e in derived["changed"]
        ]
        lines.append("")

    return "\n".join(lines)


def prepend_changelog_entry(entry_text: str, changelog_path: Path = CHANGELOG_FILE):
    """Prepend entry_text under the '# Changelog' header, most-recent-first."""
    existing = ""
    if changelog_path.exists():
        with open(changelog_path, "r") as f:
            existing = f.read()
    body = existing[len(CHANGELOG_HEADER):] if existing.startswith(CHANGELOG_HEADER) else existing
    with open(changelog_path, "w") as f:
        f.write(CHANGELOG_HEADER + entry_text.rstrip("\n") + "\n\n" + body)


def generate_output_json(
    clog_items: Dict[int, Item],
    resolver: DependencyResolver,
    primary_ids: Dict[str, int],
    all_ids: Dict[str, List[int]],
    tradeable_names: Set[str],
    output_path: str = "clog_restrictions.json",
    set_version: Optional[str] = None
):
    """Generate the final JSON output for the RuneLite plugin."""
    print(f"\nGenerating output JSON: {output_path}")

    # Read the previous run's output (if any) before it gets overwritten, so
    # we can diff against it for the CHANGELOG.md entry and the auto
    # minor-version bump decision below.
    old_output: Optional[dict] = None
    if Path(output_path).exists():
        try:
            with open(output_path, "r") as f:
                old_output = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: could not read previous output at {output_path}: {e}")

    clog_id_to_name = {item_id: item.name.lower() for item_id, item in clog_items.items()}

    # Build derived items (items where ALL recipes require clog items)
    derived_items = {}
    skipped_items = 0
    multi_id_items = 0
    no_id_items = 0
    redundant_untradeable_items = 0

    for output_name in resolver.recipes_by_item.keys():
        # Skip if this item is itself a clog item
        if output_name in resolver.clog_names:
            continue

        all_dep_sets = resolver.find_all_minimum_clog_dependency_sets(output_name)

        if all_dep_sets:
            # Restriction is a no-op if the item itself can't be traded, every
            # recipe path to it only requires untradeable clog items, AND
            # there's no tradeable intermediate item anywhere in the recipe
            # chain that itself depends on those clog items. Since none of
            # the ingredients can be bought from another player, the only way
            # to ever hold this item is to have legitimately obtained the
            # untradeable clog item(s) yourself - which is the same act that
            # unlocks them in the log. There's no route to owning it that
            # skips the unlock, so restricting it can never actually block
            # anything (e.g. "Dizana's quiver (l)" can only be made from an
            # already-owned, untradeable, drop-only "Dizana's quiver
            # (uncharged)").
            #
            # This does NOT hold when a tradeable, clog-gated intermediate
            # exists partway through the chain - e.g. "Soulreaper axe (o)" is
            # untradeable and requires the 4 untradeable DT2 uniques, but its
            # recipe includes plain "Soulreaper axe", which is itself
            # tradeable despite requiring those same uniques. A player can
            # buy a Soulreaper axe on the GE and ornament it without ever
            # personally unlocking the DT2 collection log entries, so the
            # restriction is NOT a no-op here. has_tradeable_clog_gated_intermediate
            # checks for exactly this case.
            if (
                output_name not in tradeable_names
                and all(
                    all(clog_id_to_name.get(cid, "") not in tradeable_names for cid in dep_set)
                    for dep_set in all_dep_sets
                )
                and not resolver.has_tradeable_clog_gated_intermediate(output_name, tradeable_names)
            ):
                redundant_untradeable_items += 1
                continue

            # All recipes require clog items - this is a derived item
            primary_id = primary_ids.get(output_name)
            item_ids = all_ids.get(output_name, [])

            # Ensure primary_id is in item_ids
            if primary_id and primary_id not in item_ids:
                item_ids = [primary_id] + item_ids

            if not item_ids and primary_id:
                item_ids = [primary_id]

            if item_ids:
                # Build the item entry - always use item_ids array
                # Convert frozensets to sorted lists for OR-of-AND format
                item_entry = {
                    "name": output_name,
                    "item_ids": item_ids,
                    "clog_dependencies": [sorted(list(s)) for s in all_dep_sets]
                }

                if len(item_ids) > 1:
                    multi_id_items += 1

                derived_items[output_name] = item_entry
            else:
                # No ID found - skip with warning
                no_id_items += 1
        else:
            # At least one recipe is clog-free - not restricted
            skipped_items += 1

    # Build collectionLogItems with variant IDs from bucket API
    # Only include extra IDs if they are NOT themselves clog items
    # This handles:
    # - Blood moon chestplate: 1 clog slot, multiple item IDs (new/used states) -> include all
    # - Chompy bird hats: multiple clog slots with same name -> each only includes its own ID
    clog_items_output = {}
    clog_items_with_extra_ids = 0
    clog_items_with_crafting = 0

    # Build set of all clog item IDs for filtering
    all_clog_ids = set(clog_items.keys())

    for item_id, item in clog_items.items():
        item_name_lower = item.name.lower()

        # Get all IDs for this item name from bucket API
        bucket_ids = all_ids.get(item_name_lower, [])

        # Start with the primary ID (always included)
        all_item_ids = [item_id]

        # Add extra IDs only if they are NOT clog items themselves
        # This prevents merging separate clog entries that share a name
        for extra_id in bucket_ids:
            if extra_id != item_id and extra_id not in all_clog_ids:
                all_item_ids.append(extra_id)

        # Sort for consistent output
        all_item_ids = sorted(all_item_ids)

        if len(all_item_ids) > 1:
            clog_items_with_extra_ids += 1

        # Build the clog item entry
        clog_item_entry = {
            "name": item.name,
            "tabs": item.clog_tabs,
            "all_ids": all_item_ids
        }

        # Check if this clog item can be crafted from other clog items
        # This enables "effective unlocking" - e.g., having Uncut onyx effectively unlocks Onyx
        craftable_from = find_clog_crafting_recipes(item_name_lower, resolver)
        if craftable_from:
            clog_item_entry["craftable_from"] = craftable_from
            clog_items_with_crafting += 1

        clog_items_output[str(item_id)] = clog_item_entry

    print(f"  Clog items with variant IDs (non-clog): {clog_items_with_extra_ids}")
    print(f"  Clog items craftable from other clogs: {clog_items_with_crafting}")

    # Load and add manual recipes
    manual_recipes = load_manual_recipes()
    redundant_manual_recipes = process_manual_recipes(clog_items_output, derived_items, manual_recipes, clog_id_to_name, tradeable_names, resolver)

    # Diff against the previous on-disk output to drive both the CHANGELOG.md
    # entry and the auto patch-version bump decision below.
    diff = compute_output_diff(
        old_output,
        {"collectionLogItems": clog_items_output, "derivedItems": derived_items}
    )

    if set_version:
        version = set_version
    elif old_output is None:
        version = DEFAULT_VERSION
    else:
        prev_version = old_output.get("version", DEFAULT_VERSION)
        version = _bump_patch_version(prev_version) if diff["has_changes"] else prev_version

    entry_text = format_changelog_entry(diff, version)
    if entry_text:
        prepend_changelog_entry(entry_text)
        print(f"  Wrote CHANGELOG.md entry for version {version}")

    # Build the output structure
    output = {
        "version": version,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {
            "total_clog_items": len(clog_items),
            "total_derived_items": len(derived_items),
            "items_with_clog_free_recipes": skipped_items,
            "derived_items_with_multiple_ids": multi_id_items,
            "derived_items_without_ids": no_id_items,
            "redundant_untradeable_items_pruned": redundant_untradeable_items + redundant_manual_recipes,
            "clog_items_with_multiple_ids": clog_items_with_extra_ids,
            "clog_items_craftable_from_other_clogs": clog_items_with_crafting
        },
        "collectionLogItems": clog_items_output,
        "derivedItems": derived_items
    }

    with open(output_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    pretty_path = Path(output_path).with_stem(Path(output_path).stem + ".pretty")
    with open(pretty_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Also written human-readable copy: {pretty_path}")

    print(f"  Written {len(clog_items)} clog items and {len(derived_items)} derived items")
    print(f"  Derived items with multiple IDs: {multi_id_items}")
    if no_id_items > 0:
        print(f"  WARNING: {no_id_items} derived items have no ID (will not be restricted)")
    print(f"  Skipped {skipped_items} items with clog-free recipes")
    print(f"  Pruned {redundant_untradeable_items} untradeable items with untradeable-only dependencies (restriction would be a no-op)")


def main():
    parser = argparse.ArgumentParser(description="OSRS Collection Log Dependency Builder")
    parser.add_argument("--visualize", type=str, help="Visualize dependencies for a specific item")
    parser.add_argument("--output", type=str, default="output/clog_restrictions.json", help="Output JSON file path")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refresh of cached wiki data")
    parser.add_argument("--skip-manual-review", action="store_true",
                         help="Skip interactive review of new manual_recipes.json candidates")
    parser.add_argument("--set-version", type=str, default=None,
                         help="Force an explicit output version (e.g. 1.5.0) for an intentional logic/behavior change - "
                              "bypasses the auto patch-version bump, which only fires for routine item-list changes")
    args = parser.parse_args()

    # Initialize cache and client
    cache_manager = CacheManager()
    wiki_client = OSRSWikiClient(cache_manager)

    # Fetch data (uses cache unless --refresh-cache)
    clog_items = wiki_client.fetch_collection_log_items(force_refresh=args.refresh_cache)
    recipes = wiki_client.fetch_all_recipes(force_refresh=args.refresh_cache)
    primary_ids, all_ids, page_ids, tradeable_names = wiki_client.fetch_all_items(force_refresh=args.refresh_cache)

    # Build resolver
    resolver = DependencyResolver(clog_items)
    resolver.build_recipe_graph(recipes)
    resolver.build_variant_relationships(primary_ids, all_ids)  # Pass both primary and all IDs for variant lookup

    if args.visualize:
        visualize_item(resolver, args.visualize, clog_items)
    else:
        if not args.skip_manual_review:
            manual_recipes = load_manual_recipes()
            review_manual_candidates(resolver, primary_ids, all_ids, page_ids, clog_items, manual_recipes)

        generate_output_json(clog_items, resolver, primary_ids, all_ids, tradeable_names, args.output, args.set_version)


if __name__ == "__main__":
    main()

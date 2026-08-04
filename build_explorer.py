#!/usr/bin/env python3
"""
Local dependency-chain explorer.

Generates a self-contained output/explorer.html (data embedded inline, no
server/database needed) for browsing the FULL recipe/dependency tree behind
any clog-relevant item - not just the minimal set that ships in
output/clog_restrictions.json. For QA ("why was this item pruned?") and
general exploration. Rebuilds the resolver from the existing cache/ data
(no network calls) - run clog_dependency_builder.py first if the cache is
stale or missing.

Never touches output/clog_restrictions.json or its pretty twin.

Usage:
    uv run python3 build_explorer.py
    uv run python3 build_explorer.py --output output/explorer.html
"""
import argparse
import json
from pathlib import Path
from typing import Dict, Set, Tuple

from clog_dependency_builder import (
    CacheManager,
    OSRSWikiClient,
    DependencyResolver,
    load_manual_recipes,
    is_redundant_untradeable_restriction,
    find_clog_crafting_recipes,
)


def load_vis_assets() -> Tuple[str, str]:
    """
    Read the vis-network JS/CSS that pyvis bundles as installed package data,
    so the generated HTML can inline them (stays a self-contained file, no
    CDN/network access needed to open it). Picks the newest vis-* version
    directory present rather than hardcoding a version, since pyvis ships
    more than one and bumps this over time.
    """
    import pyvis

    lib_dir = Path(pyvis.__file__).parent / "templates" / "lib"
    version_dirs = sorted(
        (p for p in lib_dir.glob("vis-*") if p.is_dir()),
        key=lambda p: tuple(int(part) for part in p.name[len("vis-"):].split(".")),
    )
    if not version_dirs:
        raise RuntimeError(f"No vis-* asset directory found under {lib_dir}")
    vis_dir = version_dirs[-1]
    js_text = (vis_dir / "vis-network.min.js").read_text()
    css_text = (vis_dir / "vis-network.css").read_text()
    # Strip the trailing sourceMappingURL comment - the .map file isn't
    # inlined alongside it, so left in place it's a guaranteed 404/harmless
    # console error the moment devtools are opened on the generated file.
    js_text = js_text.split("//# sourceMappingURL=")[0]
    return js_text, css_text


def compute_item_ids(name: str, primary_ids: Dict[str, int], all_ids: Dict[str, list]) -> list:
    """Same item_ids resolution generate_output_json() uses for auto-detected items."""
    primary_id = primary_ids.get(name)
    item_ids = list(all_ids.get(name, []))
    if primary_id and primary_id not in item_ids:
        item_ids = [primary_id] + item_ids
    if not item_ids and primary_id:
        item_ids = [primary_id]
    return item_ids


def build_reachable_items(resolver: DependencyResolver, manual_recipes: Dict[str, dict]) -> Set[str]:
    """
    BFS over resolver.recipes_by_item, seeded with every clog item and every
    clog-derived item (same predicate generate_output_json uses), expanding
    through every recipe's materials until reaching base items. This scopes
    the export to clog-relevant items instead of all ~3931 wiki craftables,
    most of which (food, ordinary gear, etc.) have nothing to do with the
    collection log.
    """
    seeds = set(resolver.clog_names.keys())
    seeds.update(name.lower() for name in manual_recipes.keys())
    for name in resolver.recipes_by_item.keys():
        if name in resolver.clog_names:
            continue
        if resolver.find_all_minimum_clog_dependency_sets(name):
            seeds.add(name)

    visited: Set[str] = set()
    queue = list(seeds)
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        visited.add(name)
        for recipe_materials in resolver.recipes_by_item.get(name, []):
            for material in recipe_materials:
                material_lower = material.lower()
                if material_lower not in visited:
                    queue.append(material_lower)
    return visited


def build_export_data(
    resolver: DependencyResolver,
    clog_items,
    primary_ids: Dict[str, int],
    all_ids: Dict[str, list],
    tradeable_names: Set[str],
    manual_recipes: Dict[str, dict],
) -> dict:
    clog_id_to_name = {item_id: item.name.lower() for item_id, item in clog_items.items()}
    reachable = build_reachable_items(resolver, manual_recipes)

    items = {}
    for name in reachable:
        entry = {
            "recipes": resolver.recipes_by_item.get(name, []),
            "is_clog": False,
            "clog_id": None,
            "tabs": None,
            "is_tradeable": name in tradeable_names,
            "min_deps": [],
            "item_ids": None,
            "status": "base_material",
            "best_recipe_idx": None,
            "craftable_from": None,
        }

        if name in resolver.clog_names:
            clog_id = resolver.clog_names[name]
            entry["is_clog"] = True
            entry["clog_id"] = clog_id
            entry["tabs"] = clog_items[clog_id].clog_tabs
            entry["min_deps"] = [clog_id]
            entry["item_ids"] = sorted(set([clog_id] + all_ids.get(name, [])))
            entry["status"] = "clog_item"
            # "Effective unlocking" - the same craftable_from the plugin's
            # isEffectivelyUnlocked() recurses through at runtime, so this
            # clog item can also be unlocked via crafting from these OTHER
            # clog items (OR-of-AND clog id sets), not just by obtaining it
            # directly. Reuses the exact function generate_output_json calls
            # for the real clog_restrictions.json, to avoid diverging.
            entry["craftable_from"] = find_clog_crafting_recipes(name, resolver)
        elif name in manual_recipes:
            recipe = manual_recipes[name]
            dep_sets = recipe["clog_dependencies"]
            entry["min_deps"] = sorted(dep_sets[0]) if dep_sets else []
            entry["manual_override_deps"] = dep_sets
            entry["item_ids"] = recipe.get("item_ids")
            if is_redundant_untradeable_restriction(name, dep_sets, tradeable_names, clog_id_to_name, resolver):
                entry["status"] = "pruned_redundant"
            else:
                entry["status"] = "derived"
        else:
            entry["best_recipe_idx"] = resolver.get_best_recipe_index(name)
            all_dep_sets = resolver.find_all_minimum_clog_dependency_sets(name)
            if all_dep_sets:
                entry["min_deps"] = sorted(min(all_dep_sets, key=len))
                if is_redundant_untradeable_restriction(name, all_dep_sets, tradeable_names, clog_id_to_name, resolver):
                    entry["status"] = "pruned_redundant"
                else:
                    item_ids = compute_item_ids(name, primary_ids, all_ids)
                    if item_ids:
                        entry["item_ids"] = item_ids
                        entry["status"] = "derived"
                    else:
                        entry["status"] = "pruned_no_id"
            elif entry["recipes"]:
                entry["status"] = "not_restricted"
            # else: leave as "base_material" default (no recipe at all)

        items[name] = entry

    return items


HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>OSRS Clog Dependency Explorer</title>
<style>__VIS_CSS__</style>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 1100px; }
  #search { width: 100%; padding: 0.5rem; font-size: 1rem; box-sizing: border-box; }
  #suggestions { border: 1px solid #ccc; max-height: 200px; overflow-y: auto; display: none; }
  #suggestions div { padding: 0.3rem 0.5rem; cursor: pointer; }
  #suggestions div:hover { background: #eee; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.8rem; margin-right: 0.4rem; color: white; }
  .b-clog { background: #6a4fb3; }
  .b-derived { background: #2a8f4d; }
  .b-pruned { background: #b3552a; }
  .b-notrestricted { background: #888; }
  .b-tradeable { background: #2a7ab3; }
  .b-untradeable { background: #b32a4f; }
  #panel { margin-top: 1rem; }
  #graph { height: 600px; border: 1px solid #ccc; margin-top: 0.5rem; }
  #graph-hint { color: #666; font-size: 0.85rem; margin-top: 0.3rem; }
  #graph-hint button { font-size: 0.8rem; padding: 0.1rem 0.5rem; cursor: pointer; }
  #toolbar { margin-top: 0.5rem; font-size: 0.9rem; }
  #toolbar label { margin-right: 1.5rem; cursor: pointer; }
  #legend { display: none; margin-top: 0.7rem; padding: 0.7rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.85rem; }
  #legend h3 { margin: 0 0 0.5rem 0; font-size: 0.9rem; }
  .legend-row { display: flex; align-items: center; margin-bottom: 0.3rem; }
  .legend-swatch { width: 1rem; height: 1rem; border-radius: 3px; margin-right: 0.5rem; flex-shrink: 0; }
  .legend-line { width: 1.5rem; height: 0; border-top: 3px solid #333; margin-right: 0.5rem; flex-shrink: 0; }
  .legend-line.alt { border-top: 3px dashed #999; }
  .legend-box { width: 1.5rem; height: 0.9rem; border-radius: 2px; margin-right: 0.5rem; flex-shrink: 0; background: #6a4fb3; }
  .legend-box.alt { opacity: 0.35; }
</style>
</head>
<body>
<h1>OSRS Clog Dependency Explorer</h1>
<p>Generated __GENERATED__ &mdash; __COUNT__ items. Local QA tool, independent of output/clog_restrictions.json.</p>
<input id="search" placeholder="Search item name..." autocomplete="off">
<div id="suggestions"></div>
<div id="toolbar">
  <label><input type="checkbox" id="show-upstream"> Also show what depends on this item (upstream)</label>
  <label><input type="checkbox" id="prune-unrestricted"> Prune unrestricted materials (hides not_restricted/base_material entirely)</label>
  <label><input type="checkbox" id="show-legend"> Show legend</label>
</div>
<div id="legend"></div>
<div id="panel"></div>
<script>__VIS_JS__</script>
<script>
const DATA = __DATA_JSON__;
const STATUS_LABELS = {
  clog_item: "CLOG ITEM",
  derived: "RESTRICTED",
  pruned_redundant: "PRUNED (redundant untradeable)",
  pruned_no_id: "PRUNED (no item ID)",
  not_restricted: "NOT RESTRICTED",
  base_material: "BASE MATERIAL",
};
const STATUS_CLASS = {
  clog_item: "b-clog",
  derived: "b-derived",
  pruned_redundant: "b-pruned",
  pruned_no_id: "b-pruned",
  not_restricted: "b-notrestricted",
  base_material: "b-notrestricted",
};
const STATUS_COLORS = {
  clog_item: "#6a4fb3",
  derived: "#2a8f4d",
  pruned_redundant: "#b3552a",
  pruned_no_id: "#b3552a",
  not_restricted: "#888888",
  base_material: "#888888",
};
const TRADEABLE_COLORS = { tradeable: "#2a7ab3", untradeable: "#b32a4f" };
const CRAFTABLE_FROM_COLOR = "#1f8a8a";

const searchBox = document.getElementById("search");
const suggestions = document.getElementById("suggestions");
const panel = document.getElementById("panel");
const names = Object.keys(DATA).sort();

searchBox.addEventListener("input", () => {
  const q = searchBox.value.trim().toLowerCase();
  if (!q) { suggestions.style.display = "none"; return; }
  const matches = names.filter(n => n.includes(q)).slice(0, 30);
  suggestions.innerHTML = "";
  matches.forEach(n => {
    const div = document.createElement("div");
    div.textContent = n;
    div.onclick = () => { searchBox.value = n; suggestions.style.display = "none"; showItem(n); };
    suggestions.appendChild(div);
  });
  suggestions.style.display = matches.length ? "block" : "none";
});

function badge(cls, text) {
  const span = document.createElement("span");
  span.className = "badge " + cls;
  span.textContent = text;
  return span;
}

// Reverse index (material -> items that use it as a material in some
// recipe), built once from the already-embedded DATA. Powers the "show
// upstream" toggle - same edge semantics as forward traversal, just walked
// backwards to find dependents instead of dependencies.
const REVERSE = {};
for (const [name, entry] of Object.entries(DATA)) {
  for (const materials of entry.recipes) {
    for (const material of materials) {
      if (!REVERSE[material]) REVERSE[material] = [];
      REVERSE[material].push(name);
    }
  }
}

// clog_id -> item name, for resolving craftable_from (expressed in clog IDs)
// and min_deps to display names without an O(n) scan over DATA each time.
const CLOG_ID_TO_NAME = {};
for (const [name, entry] of Object.entries(DATA)) {
  if (entry.is_clog) CLOG_ID_TO_NAME[entry.clog_id] = name;
}

// Statuses that never actually appear as an enforced restriction in
// clog_restrictions.json - not_restricted (has a clog-free recipe) and
// base_material (no recipe at all, e.g. ores/logs/coins). Missing DATA
// entries are treated the same as base_material, matching how renderGraph
// already falls back for them. Used by the "prune unrestricted" toggle.
const PRUNABLE_STATUSES = new Set(["not_restricted", "base_material"]);

function isPrunable(name) {
  const entry = DATA[name];
  return !entry || PRUNABLE_STATUSES.has(entry.status);
}

function buildDownstream(rootName, pruneUnrestricted) {
  // BFS over DATA[name].recipes from rootName - pure traversal, no layout
  // math. Dedupes by item name (a shared material like Coins appears once
  // with multiple incoming edges) and naturally stops at leaves/cycles via
  // the visited set. Tracks each node's shortest-path depth from root as we
  // go, so the renderer can hand vis-network an explicit level per node
  // instead of letting it infer one - see the comment on `level` in
  // renderGraph for why that matters.
  //
  // Also follows craftable_from for clog item nodes - "effective unlocking"
  // via crafting from OTHER clog items, the same relation the RuneLite
  // plugin's isEffectivelyUnlocked() recurses through at runtime. This is a
  // structurally different relation from ordinary recipe materials (it's
  // clog-id to clog-id, chainable, and always a meaningful OR-alternative -
  // not "one recipe among several the raw wiki graph happens to record"),
  // so it's tracked/styled separately from the primary/alternative
  // material-recipe distinction.
  const nodeNames = new Set([rootName]);
  const depths = new Map([[rootName, 0]]);
  const edges = [];
  // Keyed by the unordered pair (alphabetical "a|b"), not by direction -
  // craftable_from is frequently reciprocal (e.g. Oathplate helm lists
  // Oathplate shards, and Oathplate shards separately lists helm/chest/legs
  // back), and discovering both directions during BFS previously produced
  // two overlapping directed edges between the same two nodes. Collapsing
  // to one entry per pair lets renderGraph draw a single edge, with a
  // double-headed arrow only when both directions were actually found.
  const craftableFromPairs = new Map();
  const altUnlockNodes = new Set();

  function addCraftableFromEdge(from, to) {
    const [a, b] = from < to ? [from, to] : [to, from];
    const key = a + "|" + b;
    let pair = craftableFromPairs.get(key);
    if (!pair) {
      pair = { a, b, aToB: false, bToA: false };
      craftableFromPairs.set(key, pair);
    }
    if (from === a) pair.aToB = true;
    else pair.bToA = true;
    altUnlockNodes.add(from);
    altUnlockNodes.add(to);
  }

  const queue = [rootName];
  while (queue.length) {
    const name = queue.shift();
    const depth = depths.get(name);
    const entry = DATA[name];
    if (!entry) continue;
    for (const materials of entry.recipes) {
      for (const material of materials) {
        if (pruneUnrestricted && isPrunable(material)) continue;
        edges.push({ from: name, to: material });
        if (!nodeNames.has(material)) {
          nodeNames.add(material);
          depths.set(material, depth + 1);
          queue.push(material);
        }
      }
    }
    if (entry.is_clog && entry.craftable_from) {
      for (const idSet of entry.craftable_from) {
        for (const id of idSet) {
          const altName = CLOG_ID_TO_NAME[id];
          if (!altName || altName === name) continue;
          addCraftableFromEdge(name, altName);
          if (!nodeNames.has(altName)) {
            nodeNames.add(altName);
            depths.set(altName, depth + 1);
            queue.push(altName);
          }
        }
      }
    }
  }
  return { nodeNames, edges, depths, craftableFromPairs, altUnlockNodes };
}

function buildUpstream(rootName, pruneUnrestricted) {
  // BFS over REVERSE from rootName - items that (transitively) require
  // rootName as a material. Edges keep the natural "item requires material"
  // direction (from dependent to rootName/ancestor), just discovered by
  // walking the reverse index instead of DATA[name].recipes. Depths are
  // negative (ancestors render above root) and also shortest-path, same
  // rationale as buildDownstream.
  const nodeNames = new Set();
  const depths = new Map();
  const edges = [];
  const queue = [rootName];
  const visited = new Set([rootName]);
  const nodeDepth = new Map([[rootName, 0]]);
  while (queue.length) {
    const name = queue.shift();
    const depth = nodeDepth.get(name);
    for (const parent of REVERSE[name] || []) {
      if (pruneUnrestricted && isPrunable(parent)) continue;
      edges.push({ from: parent, to: name });
      nodeNames.add(parent);
      if (!visited.has(parent)) {
        visited.add(parent);
        nodeDepth.set(parent, depth - 1);
        depths.set(parent, depth - 1);
        queue.push(parent);
      }
    }
  }
  return { nodeNames, edges, depths };
}

function buildSubgraph(rootName, includeUpstream, pruneUnrestricted) {
  const down = buildDownstream(rootName, pruneUnrestricted);
  const nodeNames = new Set(down.nodeNames);
  const depths = new Map(down.depths);
  let edges = down.edges;
  if (includeUpstream) {
    const up = buildUpstream(rootName, pruneUnrestricted);
    for (const n of up.nodeNames) nodeNames.add(n);
    for (const [n, d] of up.depths) {
      if (!depths.has(n)) depths.set(n, d);
    }
    edges = edges.concat(up.edges);
  }
  return { nodeNames, edges, depths, craftableFromPairs: down.craftableFromPairs, altUnlockNodes: down.altUnlockNodes };
}

function computePrimaryChain(rootName) {
  // Follows only entry.best_recipe_idx at each step - the exact recipe
  // find_minimum_clog_dependencies picked, i.e. the chain that actually
  // determines what ships in clog_restrictions.json, as opposed to every
  // other recipe alternative the raw wiki graph happens to record. A clog
  // item terminates the chain: unlocking it satisfies the requirement
  // regardless of what else it can be crafted into.
  const primaryNodes = new Set([rootName]);
  const primaryEdges = new Set();
  const queue = [rootName];
  const visited = new Set([rootName]);
  while (queue.length) {
    const name = queue.shift();
    const entry = DATA[name];
    if (!entry || entry.is_clog) continue;
    const idx = entry.best_recipe_idx;
    if (idx === null || idx === undefined || idx < 0 || idx >= entry.recipes.length) continue;
    for (const material of entry.recipes[idx]) {
      primaryNodes.add(material);
      primaryEdges.add(name + "|" + material);
      if (!visited.has(material)) {
        visited.add(material);
        queue.push(material);
      }
    }
  }
  return { primaryNodes, primaryEdges };
}

let currentNetwork = null;
let currentRootName = null;

function renderGraph(rootName) {
  currentRootName = rootName;
  const includeUpstream = document.getElementById("show-upstream").checked;
  const pruneUnrestricted = document.getElementById("prune-unrestricted").checked;
  const { nodeNames, edges, depths, craftableFromPairs, altUnlockNodes } = buildSubgraph(rootName, includeUpstream, pruneUnrestricted);
  const { primaryNodes, primaryEdges } = computePrimaryChain(rootName);

  const nodes = [...nodeNames].map(name => {
    const entry = DATA[name];
    const status = entry ? entry.status : "base_material";
    const tradeable = entry ? entry.is_tradeable : false;
    const isPrimary = primaryNodes.has(name) || altUnlockNodes.has(name);
    return {
      id: name,
      label: name,
      shape: "box",
      widthConstraint: { maximum: 180 },
      font: { color: "#ffffff" },
      color: { background: STATUS_COLORS[status], border: tradeable ? TRADEABLE_COLORS.tradeable : TRADEABLE_COLORS.untradeable },
      borderWidth: name === rootName ? 3 : 1,
      opacity: isPrimary ? 1 : 0.35,
      // Explicit shortest-path depth from root, rather than letting
      // vis-network infer levels itself. Its default "directed" sort method
      // assigns levels via longest-path across the WHOLE rendered graph, so
      // a node that's genuinely one hop away can still get pushed many
      // levels deep if it also sits on some other, much longer path
      // elsewhere in the same view (recipe alternatives/cycles in the raw
      // wiki data make this common) - producing a huge empty gap with
      // nothing in between. Shortest-path depth is bounded by what's
      // actually visible and can't blow up like that.
      level: depths.get(name) || 0,
    };
  });

  const materialEdgeObjs = edges.map(e => {
    const isPrimary = primaryEdges.has(e.from + "|" + e.to);
    return {
      from: e.from,
      to: e.to,
      dashes: !isPrimary,
      color: { color: isPrimary ? "#333333" : "#aaaaaa", opacity: isPrimary ? 1 : 0.6 },
    };
  });

  // One edge per pair (not per direction) - arrows reflect exactly which
  // direction(s) craftable_from actually listed, so a reciprocal pair (e.g.
  // Oathplate helm <-> Oathplate shards) renders as a single double-headed
  // edge instead of two overlapping directed ones.
  const craftableFromEdgeObjs = [...craftableFromPairs.values()].map(({ a, b, aToB, bToA }) => ({
    from: a,
    to: b,
    dashes: false,
    width: 2,
    color: { color: CRAFTABLE_FROM_COLOR, opacity: 1 },
    arrows: aToB && bToA ? "to, from" : (bToA ? "from" : "to"),
    label: "OR unlock via",
    font: { size: 9, color: CRAFTABLE_FROM_COLOR, strokeWidth: 0, align: "middle" },
  }));

  const edgeObjs = materialEdgeObjs.concat(craftableFromEdgeObjs);

  const container = document.getElementById("graph");
  const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edgeObjs) };
  const options = {
    layout: {
      hierarchical: { enabled: true, direction: "UD", sortMethod: "directed", levelSeparation: 130, nodeSpacing: 140 },
    },
    physics: { enabled: false },
    edges: { arrows: "to", smooth: { type: "cubicBezier", forceDirection: "vertical", roundness: 0.4 } },
    interaction: { hover: true, dragNodes: true, zoomView: true, dragView: true },
  };

  if (currentNetwork) currentNetwork.destroy();
  currentNetwork = new vis.Network(container, data, options);
  currentNetwork.on("click", (params) => {
    if (params.nodes.length) {
      const name = params.nodes[0];
      searchBox.value = name;
      showItem(name);
    }
  });
}

function showItem(name) {
  const entry = DATA[name];
  panel.innerHTML = "";
  if (!entry) {
    panel.textContent = "Not found: " + name;
    return;
  }
  const header = document.createElement("h2");
  header.textContent = name;
  panel.appendChild(header);

  const badges = document.createElement("div");
  badges.appendChild(badge(STATUS_CLASS[entry.status], STATUS_LABELS[entry.status]));
  badges.appendChild(entry.is_tradeable ? badge("b-tradeable", "tradeable") : badge("b-untradeable", "untradeable"));
  panel.appendChild(badges);

  if (entry.tabs) {
    const tabs = document.createElement("p");
    tabs.textContent = "Tabs: " + entry.tabs.join(", ");
    panel.appendChild(tabs);
  }
  if (entry.min_deps.length) {
    const deps = document.createElement("p");
    const depNames = entry.min_deps.map(id => {
      const found = CLOG_ID_TO_NAME[id];
      return found ? found + " (" + id + ")" : id;
    });
    deps.textContent = "Minimum clog dependencies: " + depNames.join(", ");
    panel.appendChild(deps);
  }
  if (entry.craftable_from && entry.craftable_from.length) {
    const alt = document.createElement("p");
    const altText = entry.craftable_from
      .map(idSet => idSet.map(id => CLOG_ID_TO_NAME[id] || id).join(" + "))
      .join(" OR ");
    alt.textContent = "Also effectively unlocked via crafting from: " + altText;
    panel.appendChild(alt);
  }
  if (entry.item_ids) {
    const ids = document.createElement("p");
    ids.textContent = "Item IDs: " + entry.item_ids.join(", ");
    panel.appendChild(ids);
  }

  const includeUpstream = document.getElementById("show-upstream").checked;
  const hasUpstream = includeUpstream && (REVERSE[name] || []).length > 0;
  if (entry.recipes.length || hasUpstream) {
    const treeHeader = document.createElement("h3");
    treeHeader.textContent = "Dependency graph";
    panel.appendChild(treeHeader);
    const hint = document.createElement("p");
    hint.id = "graph-hint";
    hint.textContent = "Drag to pan, scroll to zoom, click a node to jump to that item. ";
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.textContent = "Reset view";
    resetBtn.onclick = () => { if (currentNetwork) currentNetwork.fit(); };
    hint.appendChild(resetBtn);
    panel.appendChild(hint);
    const graphDiv = document.createElement("div");
    graphDiv.id = "graph";
    panel.appendChild(graphDiv);
    renderGraph(name);
  } else {
    const none = document.createElement("p");
    none.textContent = "No recipe (base material).";
    panel.appendChild(none);
  }
}

function legendRow(swatchEl, text) {
  const row = document.createElement("div");
  row.className = "legend-row";
  row.appendChild(swatchEl);
  const span = document.createElement("span");
  span.textContent = text;
  row.appendChild(span);
  return row;
}

function buildLegend() {
  const legend = document.getElementById("legend");
  legend.innerHTML = "";

  const statusHeader = document.createElement("h3");
  statusHeader.textContent = "Node fill = status";
  legend.appendChild(statusHeader);
  for (const [status, label] of Object.entries(STATUS_LABELS)) {
    const swatch = document.createElement("div");
    swatch.className = "legend-swatch";
    swatch.style.background = STATUS_COLORS[status];
    legend.appendChild(legendRow(swatch, label));
  }

  const tradeHeader = document.createElement("h3");
  tradeHeader.textContent = "Node border = tradeability";
  legend.appendChild(tradeHeader);
  for (const [key, color] of Object.entries(TRADEABLE_COLORS)) {
    const swatch = document.createElement("div");
    swatch.className = "legend-swatch";
    swatch.style.background = "#fff";
    swatch.style.border = "3px solid " + color;
    legend.appendChild(legendRow(swatch, key));
  }

  const chainHeader = document.createElement("h3");
  chainHeader.textContent = "Edge/opacity = recipe chain";
  legend.appendChild(chainHeader);
  const primaryLine = document.createElement("div");
  primaryLine.className = "legend-line";
  legend.appendChild(legendRow(primaryLine, "Actual dependency used in clog_restrictions.json"));
  const altLine = document.createElement("div");
  altLine.className = "legend-line alt";
  legend.appendChild(legendRow(altLine, "Other recipe alternative (informational only, dimmed)"));
  const craftableLine = document.createElement("div");
  craftableLine.className = "legend-line";
  craftableLine.style.borderTopColor = CRAFTABLE_FROM_COLOR;
  legend.appendChild(legendRow(craftableLine, 'OR unlock via (effective unlocking via crafting from another clog item)'));
}

buildLegend();

document.getElementById("show-legend").addEventListener("change", (e) => {
  document.getElementById("legend").style.display = e.target.checked ? "block" : "none";
});

document.getElementById("show-upstream").addEventListener("change", () => {
  if (currentRootName) showItem(currentRootName);
});

document.getElementById("prune-unrestricted").addEventListener("change", () => {
  if (currentRootName) showItem(currentRootName);
});

document.addEventListener("click", (e) => {
  if (e.target !== searchBox) suggestions.style.display = "none";
});
</script>
</body>
</html>
"""


def render_html(export_data: dict, vis_js: str, vis_css: str) -> str:
    import time
    return (
        HTML_TEMPLATE
        .replace("__VIS_JS__", vis_js)
        .replace("__VIS_CSS__", vis_css)
        .replace("__GENERATED__", time.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__COUNT__", str(len(export_data)))
        .replace("__DATA_JSON__", json.dumps(export_data, separators=(",", ":")))
    )


def main():
    parser = argparse.ArgumentParser(description="Build the local dependency-chain explorer")
    parser.add_argument("--output", type=str, default="output/explorer.html", help="Output HTML file path")
    args = parser.parse_args()

    cache_manager = CacheManager()
    wiki_client = OSRSWikiClient(cache_manager)

    print("Loading cached data (no network calls; run clog_dependency_builder.py first if cache is stale)...")
    clog_items = wiki_client.fetch_collection_log_items(force_refresh=False)
    recipes = wiki_client.fetch_all_recipes(force_refresh=False)
    primary_ids, all_ids, page_ids, tradeable_names = wiki_client.fetch_all_items(force_refresh=False)

    resolver = DependencyResolver(clog_items)
    resolver.build_recipe_graph(recipes)
    resolver.build_variant_relationships(primary_ids, all_ids)

    manual_recipes = load_manual_recipes()

    print("Computing reachable clog-relevant item set...")
    export_data = build_export_data(resolver, clog_items, primary_ids, all_ids, tradeable_names, manual_recipes)
    print(f"  {len(export_data)} items included")

    vis_js, vis_css = load_vis_assets()
    html = render_html(export_data, vis_js, vis_css)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        f.write(html)
    print(f"Wrote {args.output} ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

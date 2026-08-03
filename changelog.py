#!/usr/bin/env python3
"""Ad-hoc comparison tool: diff two clog_restrictions.json snapshots.

Usage:
    python3 changelog.py old.json new.json [--version X.Y.Z] [--write CHANGELOG.md]
"""
import argparse
import json
from pathlib import Path

from clog_dependency_builder import (
    compute_output_diff,
    format_changelog_entry,
    prepend_changelog_entry,
    DEFAULT_VERSION,
)


def main():
    parser = argparse.ArgumentParser(description="Diff two clog_restrictions.json files")
    parser.add_argument("old", help="Path to the old/previous JSON file")
    parser.add_argument("new", help="Path to the new/current JSON file")
    parser.add_argument("--version", help="Version label for the entry header (default: new file's 'version' field)")
    parser.add_argument("--write", help="Path to prepend the entry to instead of printing (e.g. CHANGELOG.md)")
    args = parser.parse_args()

    with open(args.old, "r") as f:
        old_output = json.load(f)
    with open(args.new, "r") as f:
        new_output = json.load(f)

    diff = compute_output_diff(old_output, new_output)
    version = args.version or new_output.get("version", DEFAULT_VERSION)
    entry = format_changelog_entry(diff, version)

    if entry is None:
        print("No differences found.")
        return

    if args.write:
        prepend_changelog_entry(entry, Path(args.write))
        print(f"Wrote entry to {args.write}")
    else:
        print(entry)


if __name__ == "__main__":
    main()

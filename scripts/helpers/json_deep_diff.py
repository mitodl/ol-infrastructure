#!/usr/bin/env python3
"""Deep diff two JSON files and report differences."""

import json
import sys
from typing import Any


def get_all_paths(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Recursively get all paths and their values from a nested structure."""
    paths = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                paths.update(get_all_paths(value, current_path))
            else:
                paths[current_path] = value
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            current_path = f"{prefix}[{idx}]"
            if isinstance(item, (dict, list)):
                paths.update(get_all_paths(item, current_path))
            else:
                paths[current_path] = item

    return paths


def deep_diff(file1_path: str, file2_path: str) -> None:
    """Compare two JSON files and report differences."""
    # Load JSON files
    try:
        with open(file1_path) as f:
            data1 = json.load(f)
        with open(file2_path) as f:
            data2 = json.load(f)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Get all paths from both files
    paths1 = get_all_paths(data1)
    paths2 = get_all_paths(data2)

    keys1 = set(paths1.keys())
    keys2 = set(paths2.keys())

    # Find keys only in file1
    only_in_file1 = sorted(keys1 - keys2)

    # Find keys only in file2
    only_in_file2 = sorted(keys2 - keys1)

    # Find keys with different values
    different_values = []
    for key in sorted(keys1 & keys2):
        if paths1[key] != paths2[key]:
            different_values.append((key, paths1[key], paths2[key]))

    # Print results
    print("Keys in File1 not found in File2")
    print("=" * 80)
    if only_in_file1:
        for key in only_in_file1:
            print(f"  {key}: {paths1[key]}")
    else:
        print("  (none)")
    print()

    print("Keys in File2 not found in File1")
    print("=" * 80)
    if only_in_file2:
        for key in only_in_file2:
            print(f"  {key}: {paths2[key]}")
    else:
        print("  (none)")
    print()

    print("Key:Values in File1 that do not match with Key:Value found in File2")
    print("=" * 80)
    if different_values:
        for key, val1, val2 in different_values:
            print(f"  {key}:")
            print(f"    File1: {val1}")
            print(f"    File2: {val2}")
    else:
        print("  (none)")


def main() -> None:
    """Entry point for the script."""
    if len(sys.argv) != 3:
        print("Usage: json_deep_diff.py <file1.json> <file2.json>", file=sys.stderr)
        sys.exit(1)

    file1 = sys.argv[1]
    file2 = sys.argv[2]

    deep_diff(file1, file2)


if __name__ == "__main__":
    main()

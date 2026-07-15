#!/usr/bin/env python3
"""
merge_gtfs.py — Merge multiple unzipped GTFS feeds into one.

Usage:
    python merge_gtfs.py <input_feed_dir1> <input_feed_dir2> ... <output_dir>

Each input feed directory should contain unzipped GTFS .txt files.
All IDs in each feed are prefixed with the feed's directory name to avoid
collisions. The output directory will be created if it doesn't exist.

Handles differing CSV headers across feeds gracefully — the union of all
headers is used for each output file.

Covers all standard GTFS Schedule files as of the April 2026 spec, including
Fares v2 tables, pathways, levels, transfers with trip/route references, etc.
"""

import csv
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# ID field registry
#
# For each GTFS file, this maps column names to the "ID type" they carry.
# The ID type is a logical name (e.g. "agency_id", "stop_id") rather than
# the column name, so that the same logical ID referenced under different
# column names (e.g. "parent_station" referencing a stop_id) gets prefixed
# consistently.
#
# Format: { filename: { column_name: "logical_id_type" } }
#
# Every column listed here will have its non-empty values prefixed.
# Columns not listed are copied verbatim.
# ---------------------------------------------------------------------------

ID_COLUMNS: Dict[str, Dict[str, str]] = {
    "agency.txt": {
        "agency_id": "agency_id",
    },
    "stops.txt": {
        "stop_id":        "stop_id",
        "parent_station": "stop_id",   # FK → stops.stop_id
        "level_id":       "level_id",  # FK → levels.level_id
        "zone_id":        "zone_id",   # referenced by fare_rules
    },
    "routes.txt": {
        "route_id":   "route_id",
        "agency_id":  "agency_id",   # FK → agency.agency_id
        "network_id": "network_id",  # FK → networks.network_id (Fares v2)
    },
    "trips.txt": {
        "trip_id":    "trip_id",
        "route_id":   "route_id",    # FK → routes.route_id
        "service_id": "service_id",  # FK → calendar / calendar_dates
        "shape_id":   "shape_id",    # FK → shapes.shape_id
        "block_id":   "block_id",    # logical block grouping
    },
    "stop_times.txt": {
        "trip_id": "trip_id",  # FK → trips.trip_id
        "stop_id": "stop_id",  # FK → stops.stop_id
        # location_id and location_group_id are Flex (on-demand) fields
        "location_id":       "location_id",
        "location_group_id": "location_group_id",
    },
    "calendar.txt": {
        "service_id": "service_id",
    },
    "calendar_dates.txt": {
        "service_id": "service_id",
    },
    "fare_attributes.txt": {
        "fare_id":   "fare_id",
        "agency_id": "agency_id",  # FK → agency.agency_id
    },
    "fare_rules.txt": {
        "fare_id":         "fare_id",    # FK → fare_attributes.fare_id
        "route_id":        "route_id",   # FK → routes.route_id
        "origin_id":       "zone_id",    # FK → stops.zone_id
        "destination_id":  "zone_id",
        "contains_id":     "zone_id",
    },
    "shapes.txt": {
        "shape_id": "shape_id",
    },
    "frequencies.txt": {
        "trip_id": "trip_id",  # FK → trips.trip_id
    },
    "transfers.txt": {
        "from_stop_id":  "stop_id",   # FK → stops.stop_id
        "to_stop_id":    "stop_id",
        "from_route_id": "route_id",  # FK → routes.route_id
        "to_route_id":   "route_id",
        "from_trip_id":  "trip_id",   # FK → trips.trip_id
        "to_trip_id":    "trip_id",
    },
    "pathways.txt": {
        "pathway_id":  "pathway_id",
        "from_stop_id": "stop_id",  # FK → stops.stop_id
        "to_stop_id":   "stop_id",
    },
    "levels.txt": {
        "level_id": "level_id",
    },
    # --- Fares v2 tables ---
    "timeframes.txt": {
        "timeframe_group_id": "timeframe_group_id",
        "service_id":         "service_id",  # FK → calendar / calendar_dates
    },
    "rider_categories.txt": {
        "rider_category_id": "rider_category_id",
    },
    "fare_media.txt": {
        "fare_media_id": "fare_media_id",
    },
    "fare_products.txt": {
        "fare_product_id":   "fare_product_id",
        "fare_media_id":     "fare_media_id",      # FK → fare_media
        "rider_category_id": "rider_category_id",  # FK → rider_categories
    },
    "fare_leg_rules.txt": {
        "leg_group_id":          "leg_group_id",
        "network_id":            "network_id",
        "from_timeframe_group_id": "timeframe_group_id",
        "to_timeframe_group_id":   "timeframe_group_id",
        "fare_product_id":       "fare_product_id",   # FK → fare_products
        "from_area_id":          "area_id",
        "to_area_id":            "area_id",
        "from_rider_category_id": "rider_category_id",
        "to_rider_category_id":   "rider_category_id",
    },
    "fare_leg_join_rules.txt": {
        "from_leg_group_id": "leg_group_id",
        "to_leg_group_id":   "leg_group_id",
    },
    "fare_transfer_rules.txt": {
        "from_leg_group_id":  "leg_group_id",
        "to_leg_group_id":    "leg_group_id",
        "fare_product_id":    "fare_product_id",
        "transfer_id":        "transfer_id",
    },
    "areas.txt": {
        "area_id": "area_id",
    },
    "stop_areas.txt": {
        "area_id": "area_id",
        "stop_id": "stop_id",  # FK → stops.stop_id
    },
    "networks.txt": {
        "network_id": "network_id",
    },
    "route_networks.txt": {
        "network_id": "network_id",
        "route_id":   "route_id",  # FK → routes.route_id
    },
    "location_groups.txt": {
        "location_group_id": "location_group_id",
    },
    "location_group_stops.txt": {
        "location_group_id": "location_group_id",
        "stop_id":           "stop_id",  # FK → stops.stop_id
    },
    "booking_rules.txt": {
        "booking_rule_id": "booking_rule_id",
    },
    "attributions.txt": {
        "attribution_id": "attribution_id",
        "agency_id":      "agency_id",   # FK → agency
        "route_id":       "route_id",    # FK → routes
        "trip_id":        "trip_id",     # FK → trips
    },
    # feed_info.txt and translations.txt contain no ID fields that need
    # prefixing (feed_publisher_name is not referenced elsewhere).
    # translations.txt uses record_id which references values in other tables
    # by their string value — we handle this specially below.
}

# translations.txt requires special handling: record_id holds the actual ID
# value from the table named in table_name, so we need to know which logical
# ID type each table's primary key is.
TRANSLATIONS_TABLE_ID_TYPES: Dict[str, str] = {
    "agency":           "agency_id",
    "stops":            "stop_id",
    "routes":           "route_id",
    "trips":            "trip_id",
    "stop_times":       "trip_id",    # record_id=trip_id, record_sub_id=stop_sequence
    "calendar":         "service_id",
    "calendar_dates":   "service_id",
    "fare_attributes":  "fare_id",
    "fare_rules":       "fare_id",
    "shapes":           "shape_id",
    "frequencies":      "trip_id",
    "transfers":        "from_stop_id",  # ambiguous — skip prefixing for safety
    "pathways":         "pathway_id",
    "levels":           "level_id",
    "attributions":     "attribution_id",
    "booking_rules":    "booking_rule_id",
    "fare_products":    "fare_product_id",
    "fare_media":       "fare_media_id",
    "rider_categories": "rider_category_id",
    "areas":            "area_id",
    "networks":         "network_id",
    "timeframes":       "timeframe_group_id",
}

# Transfers is ambiguous for translations (composite key), so we skip it.
TRANSLATIONS_SKIP_TABLES: Set[str] = {"transfers"}

# Only process files that are part of the official GTFS Schedule spec.
# Any other .txt files (e.g. license_terms.txt, readme.txt) are ignored.
STANDARD_GTFS_FILES: Set[str] = {
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "fare_attributes.txt",
    "fare_rules.txt",
    "timeframes.txt",
    "rider_categories.txt",
    "fare_media.txt",
    "fare_products.txt",
    "fare_leg_rules.txt",
    "fare_leg_join_rules.txt",
    "fare_transfer_rules.txt",
    "areas.txt",
    "stop_areas.txt",
    "networks.txt",
    "route_networks.txt",
    "shapes.txt",
    "frequencies.txt",
    "transfers.txt",
    "pathways.txt",
    "levels.txt",
    "location_groups.txt",
    "location_group_stops.txt",
    "booking_rules.txt",
    "translations.txt",
    "feed_info.txt",
    "attributions.txt",
}


def prefix_value(value: str, prefix: str) -> str:
    """Add prefix to a non-empty ID value."""
    if value and value.strip():
        return f"{prefix}_{value}"
    return value


def process_row(
    row: Dict[str, str],
    prefix: str,
    id_columns: Dict[str, str],
) -> Dict[str, str]:
    """Return a new row dict with ID columns prefixed."""
    new_row = {}
    for col, val in row.items():
        if col in id_columns:
            new_row[col] = prefix_value(val, prefix)
        else:
            new_row[col] = val
    return new_row


def process_translations_row(
    row: Dict[str, str],
    prefix: str,
) -> Dict[str, str]:
    """Handle translations.txt specially due to its dynamic record_id column."""
    new_row = dict(row)
    table_name = row.get("table_name", "")
    if table_name in TRANSLATIONS_SKIP_TABLES:
        return new_row
    if table_name and "record_id" in row and row["record_id"]:
        id_type = TRANSLATIONS_TABLE_ID_TYPES.get(table_name)
        if id_type:
            new_row["record_id"] = prefix_value(row["record_id"], prefix)
    return new_row


def read_csv(filepath: Path) -> tuple[List[str], List[Dict[str, str]]]:
    """Read a CSV file, returning (headers, rows). Handles BOM and strips
    leading/trailing whitespace from header names (some feeds include spaces
    after commas in the header row, which would otherwise cause duplicate
    columns in the merged output)."""
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        raw_headers = reader.fieldnames or []
        headers = [h.strip() for h in raw_headers]
        # Map raw header names to stripped versions for renaming row keys
        header_map = {raw: stripped for raw, stripped in zip(raw_headers, headers)}
        for row in reader:
            rows.append({header_map.get(k, k).strip(): v.strip() for k, v in row.items()})
    return list(headers), rows


def write_csv(filepath: Path, headers: List[str], rows: List[Dict[str, str]]) -> None:
    """Write rows to a CSV file using the given header order."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=headers,
            extrasaction="ignore",
            lineterminator="\r\n",
        )
        writer.writeheader()
        for row in rows:
            # Fill missing columns with empty string
            writer.writerow({h: row.get(h, "") for h in headers})


def merge_feeds(input_dirs: List[Path], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect all standard GTFS filenames present across all feeds.
    # Non-standard files (e.g. license_terms.txt) are silently skipped.
    all_files: Set[str] = set()
    for feed_dir in input_dirs:
        for f in feed_dir.iterdir():
            if f.suffix == ".txt" and f.name in STANDARD_GTFS_FILES:
                all_files.add(f.name)

    print(f"Found {len(input_dirs)} input feeds, {len(all_files)} distinct file types.")

    for filename in sorted(all_files):
        if filename == "translations.txt":
            continue  # handled separately below

        id_columns = ID_COLUMNS.get(filename, {})
        combined_headers: List[str] = []  # ordered union of all headers
        seen_headers: Set[str] = set()
        all_rows: List[Dict[str, str]] = []

        for feed_dir in input_dirs:
            filepath = feed_dir / filename
            if not filepath.exists():
                continue

            prefix = feed_dir.name
            headers, rows = read_csv(filepath)

            # Extend the header union while preserving order
            for h in headers:
                if h not in seen_headers:
                    combined_headers.append(h)
                    seen_headers.add(h)

            for row in rows:
                processed = process_row(row, prefix, id_columns)
                all_rows.append(processed)

        if all_rows:
            out_path = output_dir / filename
            write_csv(out_path, combined_headers, all_rows)
            print(f"  {filename}: {len(all_rows)} rows from "
                  f"{sum(1 for d in input_dirs if (d / filename).exists())} feed(s)")

    # Handle translations.txt separately
    if "translations.txt" in all_files:
        combined_headers: List[str] = []
        seen_headers: Set[str] = set()
        all_rows: List[Dict[str, str]] = []

        for feed_dir in input_dirs:
            filepath = feed_dir / "translations.txt"
            if not filepath.exists():
                continue

            prefix = feed_dir.name
            headers, rows = read_csv(filepath)

            for h in headers:
                if h not in seen_headers:
                    combined_headers.append(h)
                    seen_headers.add(h)

            for row in rows:
                processed = process_translations_row(row, prefix)
                all_rows.append(processed)

        if all_rows:
            out_path = output_dir / "translations.txt"
            write_csv(out_path, combined_headers, all_rows)
            print(f"  translations.txt: {len(all_rows)} rows")

    # Warn about locations.geojson (not handled — it's GeoJSON not CSV)
    for feed_dir in input_dirs:
        if (feed_dir / "locations.geojson").exists():
            print(
                f"  WARNING: {feed_dir.name}/locations.geojson found but not merged "
                "(GeoJSON Flex files require manual merging)."
            )

    print(f"\nDone. Output written to: {output_dir}")


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python merge_gtfs.py <input_dir1> [input_dir2 ...] <output_dir>")
        print()
        print("  Each input directory should contain unzipped GTFS .txt files.")
        print("  The last argument is the output directory.")
        print()
        print("  IDs in each feed are prefixed with the feed directory's name.")
        print("  Example: stop_id '1234' in feed 'agency_a' becomes 'agency_a_1234'.")
        sys.exit(1)

    *input_paths, output_path = sys.argv[1:]

    input_dirs = [Path(p) for p in input_paths]
    output_dir = Path(output_path)

    for d in input_dirs:
        if not d.is_dir():
            print(f"Error: input path is not a directory: {d}")
            sys.exit(1)

    merge_feeds(input_dirs, output_dir)


if __name__ == "__main__":
    main()
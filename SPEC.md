# Frequency Map Specification

## Overview

Add frequency-based coloring to the LOOM pipeline for the Chicagoland bus network. Each route segment is colored according to its service headway during a user-specified time window, following the Jarrett Walker / Human Transit convention.

## Goals

- Bus-only frequency map of the Chicagoland area (CTA + Pace; Metra excluded via gtfs2graph `-m` flag)
- Per-segment headway computation (correctly reflects short-turns and branches)
- User-configurable frequency buckets (headway ranges → colors)
- Routes with no service in the window go into a special configurable bucket (optionally invisible)
- Output: SVG and MVT from `transitmap`
- Re-running for a different time window or color scheme must **not** require re-running `topo` or `loom`

## Non-goals

- Variable line width (deferred; see appendix)
- Rail routes (excluded via `gtfs2graph -m bus`)
- Schematic/octilinear layout (`octi`)
- Modifying loom's optimizer

---

## Pipeline

```
# Run once; cache. topo takes 30 min – 2 h and ~14 GiB RAM on the Chicagoland dataset.
gtfs2graph -m bus gtfs/chicagoland > chicagoland-bus-graph
topo < chicagoland-bus-graph > chicagoland-bus-topo
loom < chicagoland-bus-topo > chicagoland-bus-loom

# Run whenever time window, metric, or bucket colors change. Fast.
python3 freq.py \
    --config freq.toml \
    --gtfs gtfs/chicagoland.zip \
    chicagoland-bus-loom \       # ← reads loom output, not topo output
    > chicagoland-bus-freq

transitmap -l < chicagoland-bus-freq > chicagoland-bus.svg
transitmap -l --mvt chicagoland-bus.mvt < chicagoland-bus-freq
```

**Why after loom**: loom's ILP optimizer uses only topological structure (crossing counts), never colors. Running `freq.py` after loom means a time-window change only reruns `freq.py` + `transitmap` — not loom.

---

## Configuration File (`freq.toml`)

```toml
[window]
# A specific calendar date. Active service_ids are resolved from calendar.txt
# and calendar_dates.txt. Use a typical midday weekday (Tuesday or Wednesday,
# when school is in session, no public holidays).
date = "2026-05-13"
start_time = "10:00"   # HH:MM 24-hour. Times ≥ 24:00 in stop_times are supported.
end_time   = "14:00"

[metric]
# How to aggregate inter-trip gaps within the window.
# "max"    – longest gap (worst-case wait)
# "mean"   – arithmetic mean of all gaps
# "median" – 50th-percentile gap (robust to outliers)
type = "max"

# Frequency buckets, evaluated in order (smallest max_headway_min first).
# The first bucket where max_headway_min >= computed headway is used.
# max_headway_min = "inf" is the catch-all (matches any headway).
# color: 6-digit hex WITHOUT '#'.
# If color is null, the route is invisible: its line entries are removed from
# the output JSON, and edges that become empty are also removed entirely.
# Invisible routes do not appear in loom or transitmap output.
[[buckets]]
max_headway_min = 10
color = "d32f2f"

[[buckets]]
max_headway_min = 15
color = "f57c00"

[[buckets]]
max_headway_min = 20
color = "fbc02d"

[[buckets]]
max_headway_min = 30
color = "388e3c"

[[buckets]]
max_headway_min = 60
color = "1976d2"

[[buckets]]
max_headway_min = "inf"
color = "9e9e9e"

# Routes with zero qualifying trips in the window.
# color = null makes them invisible (removed from output).
[no_service]
color = null
```

---

## Required C++ Change: Per-Segment Color Override

### Problem

`Line` objects in LOOM's in-memory graph are **shared globally by ID** across all edges. In `LineGraph::extractLine` (LineGraph.cpp:1462):

```cpp
const Line* l = getLine(id);
if (!l) {
    l = new Line(id, label, color);  // color set here, ONCE
    addLine(l);
}
e->pl().addLine(l, dir, ls);        // all subsequent edges reuse l; color ignored
```

The `color` field in the JSON is read only on the first edge that mentions a route. All subsequent edges reuse the same `Line` object with the same color. Setting different colors on different edges in the JSON has no effect after the first occurrence.

The existing `style`/CSS override (`"style": "stroke:#hex"`) partially works for SVG edge segments (the last `stroke:` in an inline style wins), but:
- SVG junction arcs use `c.geoms[i].from.line->color()` directly with no CSS applied
- The MVT renderer ignores `css` entirely and always calls `line->color()` for its params

### Solution: `colorOverride` on `LineOcc`

Add an optional per-edge color override that both renderers check before falling back to the shared Line object's color. Changes are in 5 files, ~80 lines total.

#### 1. `src/shared/linegraph/LineEdgePL.h` — add field to `LineOcc`

```cpp
struct LineOcc {
  LineOcc(const Line* l, const LineNode* dir,
          util::Nullable<shared::style::LineStyle> s)
      : line(l), direction(dir), style(s) {}

  const Line* line;
  const LineNode* direction;
  util::Nullable<shared::style::LineStyle> style;
  std::string colorOverride;  // NEW: per-edge color, overrides line->color()
};
```

#### 2. `src/shared/linegraph/LineEdgePL.cpp` — output `freq_color` in `getAttrs()`

In `LineEdgePL::getAttrs()`, after the existing style output (around line 115):

```cpp
if (!r.colorOverride.empty()) line["freq_color"] = r.colorOverride;
```

#### 3. `src/shared/linegraph/LineGraph.cpp` — read `freq_color` in `extractLine()`

At the end of `extractLine`, before `e->pl().addLine(...)`:

```cpp
// Read per-segment frequency color override if present.
if (line.count("freq_color")) {
    std::string fc = line.at("freq_color").get<std::string>();
    if (!fc.empty()) {
        // addLine stores the LineOcc; retrieve it and set colorOverride.
        // We must set it after addLine, since addLine constructs the LineOcc.
        e->pl().addLine(l, dir, ls);
        e->pl().setColorOverride(l, util::normHtmlColor(fc));
        return;
    }
}
e->pl().addLine(l, dir, ls);
```

Add `setColorOverride` to `LineEdgePL`:

```cpp
void LineEdgePL::setColorOverride(const Line* l, const std::string& color) {
    auto it = _lineToIdx.find(l);
    if (it != _lineToIdx.end()) _lines[it->second].colorOverride = color;
}
```

#### 4. `src/transitmap/output/SvgRenderer.cpp` — use override in `renderLinePart`

Change the signature to accept an optional override:

```cpp
void SvgRenderer::renderLinePart(const PolyLine<double> p, double width,
                                 const Line& line, const std::string& css,
                                 const std::string& oCss,
                                 const std::string& endMarker,
                                 const std::string& colorOverride) {
    // ...
    std::string color = colorOverride.empty() ? line.color() : colorOverride;
    styleStr << "fill:none;stroke:#" << color << ";" << css;
    // ...
}
```

In `renderEdgeTripGeom`, pass `lo.colorOverride` to each `renderLinePart` call.

Junction arcs in `renderClique` (line 434) are left using `c.geoms[i].from.line->color()` — the global Line color. This is acceptable because `freq.py` sets the global color to the route's **best** (minimum headway) bucket color, so junction arcs always show the most frequent service color. The only visual artifact is at frequency-change nodes (short-turn termini), which are rare during midday.

#### 5. `src/transitmap/output/MvtRenderer.cpp` — use override in `renderEdgeTripGeom`

```cpp
std::string color = lo.colorOverride.empty() ? line->color() : lo.colorOverride;
params["color"] = color;
params["line-color"] = color;
```

Apply the same for the outline params (`paramsOut["line-color"]`).

---

## `freq.py` — Algorithm

### Inputs

| Argument | Description |
|---|---|
| positional: `loom_file` | Path to loom output GeoJSON (or `-` for stdin) |
| `--config` | Path to `freq.toml` |
| `--gtfs` | Path to GTFS zip or extracted directory |

Output: modified GeoJSON written to stdout.

### Step 1 — Load GTFS tables

Load with `dtype=str` everywhere. Cast `stop_sequence` to int after loading.

Required files and columns:
- `routes.txt`: `route_id`, `route_short_name`, `route_type`
- `trips.txt`: `trip_id`, `route_id`, `service_id`, `direction_id`
- `stop_times.txt`: `trip_id`, `stop_id`, `departure_time`, `stop_sequence`, `pickup_type`, `drop_off_type`
- `stops.txt`: `stop_id`, `stop_lat`, `stop_lon`
- `calendar.txt`: `service_id`, `monday`–`sunday`, `start_date`, `end_date`
- `calendar_dates.txt`: `service_id`, `date`, `exception_type`

**Time parsing** — GTFS allows times ≥ 24:00:00:

```python
def parse_time(s: str) -> int:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)
```

**Memory**: `stop_times.txt` has ~3.9M rows. Load once; filter to active trips immediately (Step 3).

### Step 2 — Determine active service_ids for the configured date

```python
date_str = config.window.date   # e.g. "20260513"
dow = weekday_name(date_str)    # "monday" … "sunday"
```

1. `base_services`: service_ids from `calendar.txt` where `dow` column == "1" AND `start_date <= date_str <= end_date`.
2. Apply `calendar_dates.txt` for `date_str`:
   - `exception_type == "1"`: add to active set
   - `exception_type == "2"`: remove from active set
3. Warn (stderr) if `active_service_ids` is empty — the date may be outside the feed's range.

### Step 3 — Filter trips and stop_times to active service

```python
active_trips = trips[trips["service_id"].isin(active_service_ids)].copy()
active_stop_times = stop_times.merge(
    active_trips[["trip_id", "route_id", "direction_id"]], on="trip_id"
)
```

**Filter out non-revenue trips**: exclude trips where ALL stops have `pickup_type == "1"` AND `drop_off_type == "1"`. These are deadhead/positioning moves that don't carry passengers.

```python
# Trips with at least one revenue stop
revenue_trips = (
    active_stop_times
    .groupby("trip_id")
    .apply(lambda g: ((g["pickup_type"] != "1") | (g["drop_off_type"] != "1")).any())
)
active_stop_times = active_stop_times[
    active_stop_times["trip_id"].isin(revenue_trips[revenue_trips].index)
]
```

### Step 4 — Build data structures

```python
# For each trip: sorted list of (stop_sequence_int, stop_id, dep_sec)
# Group by trip_id for fast lookup.
active_stop_times["dep_sec"] = active_stop_times["departure_time"].map(parse_time)
active_stop_times["stop_sequence"] = active_stop_times["stop_sequence"].astype(int)
trip_stops = (
    active_stop_times
    .sort_values(["trip_id", "stop_sequence"])
    .groupby("trip_id", sort=False)
    .apply(lambda g: list(zip(g["stop_sequence"], g["stop_id"], g["dep_sec"])))
    .to_dict()
)

# {route_id: set of stop_ids visited by any active trip of that route}
route_stop_set = (
    active_stop_times
    .groupby("route_id")["stop_id"]
    .apply(set)
    .to_dict()
)

# {(route_id, direction_id): [trip_id, ...]}
route_dir_trips = (
    active_trips
    .groupby(["route_id", "direction_id"])["trip_id"]
    .apply(list)
    .to_dict()
)

# KD-tree over stop coordinates for geographic fallback
stop_coords = stops[["stop_lat", "stop_lon"]].astype(float).values
stop_ids = stops["stop_id"].values
kdtree = scipy.spatial.KDTree(stop_coords)
```

Also build `label_to_routes`: `{route_short_name: [route_id, ...]}` restricted to bus routes (`route_type == "3"`).

### Step 5 — Build node and edge maps from the GeoJSON

```python
node_map = {f["properties"]["id"]: f for f in features if f["geometry"]["type"] == "Point"}
edges = [f for f in features if f["geometry"]["type"] == "LineString"]
```

### Step 6 — Resolve the stop_id for a node, given a specific route

This is called for both the `from` node and `to` node of each edge, per route.

```python
def resolve_stop(node, route_id, route_stop_set):
    sid = node["properties"].get("station_id", "")
    if sid and sid in route_stop_set.get(route_id, set()):
        return sid    # fast path: station_id is valid for this route

    # Geographic fallback: find nearest stop in this route's stop set
    lat = node["geometry"]["coordinates"][1]
    lon = node["geometry"]["coordinates"][0]
    # Query KD-tree; restrict candidates to route_stop_set[route_id]
    # Accept within 200 m (haversine). Return None if no match found.
    ...
```

**200 m threshold** (not 100 m): suburban Pace routes have stops spaced 500 m–1 km apart; topo intersection nodes can be up to ~150 m from the nearest stop on dense city grids. 200 m provides enough slack while excluding clearly wrong matches.

Always validate station_id against `route_stop_set[route_id]` even when station_id is present — a node may carry the stop_id of one route while a different route on the adjacent edge has a nearby-but-distinct stop.

Log to stderr any resolution that uses the geographic fallback, and any that fail entirely (no stop within 200 m).

### Step 7 — Compute headway for each route on each edge

```python
window_start = parse_time(config.window.start_time)
window_end   = parse_time(config.window.end_time)
window_dur_min = (window_end - window_start) / 60.0

for edge in edges:
    for line_entry in edge["properties"]["lines"]:
        label = line_entry["label"]
        candidates = label_to_routes.get(label, [])
        # ...
```

For each line entry:

1. **Look up route_id(s)** from `label_to_routes[label]`. If multiple routes share the label (including the empty-label Pace Pulse routes), identify the correct one by finding which route has active trips visiting the from_stop and to_stop resolved in Step 6. Use the first such route; warn if none found.

2. **Collect qualifying trips** for each direction (direction_id ∈ {"0", "1"}):
   - From `route_dir_trips[(route_id, direction_id)]`, get trip_ids.
   - For each trip, check `trip_stops[trip_id]` for the presence of both from_stop_id and to_stop_id:
     - Direction 0: from_stop has lower stop_sequence than to_stop.
     - Direction 1: to_stop has lower stop_sequence than from_stop.
   - For qualifying trips, record the departure time at the earlier of the two stops (the "entry" into this segment).

3. **Apply time window filter**: keep departure_seconds in `[window_start, window_end)`.

4. **Compute gaps and apply metric** (per direction independently):
   - Sort departure seconds.
   - `gaps = [t[i+1] - t[i] for i in range(len(t)-1)]`
   - 0 trips in window → headway undefined (handle as no_service below)
   - 1 trip in window → 0 gaps; use `window_dur_min` as the headway (conservative)
   - 2+ trips → apply `mean`, `median`, or `max` over gaps, convert to minutes

5. **Take minimum headway across both directions** (best available service in either direction on this corridor).

6. **Assign bucket**:
   - If no qualifying trips in either direction → assign `[no_service]` bucket.
   - Otherwise → walk `[[buckets]]` in config order; use first where `max_headway_min >= headway_min`.

7. **Determine per-route global color** (used for the shared `Line` object; appears in SVG junction arcs):
   - At the end of processing all edges, for each route_id, take the **minimum computed headway** across all its segments (best service anywhere on the route).
   - Look up the corresponding bucket color.
   - Set `line_entry["color"]` to this global color on all edges for the route.

8. **Set `freq_color`** (per-segment override; read by the modified renderers):
   - Set `line_entry["freq_color"]` to the bucket color for this specific segment.
   - This may differ from `line_entry["color"]` only at infrequent outer segments.

9. **Invisible routes**: if the bucket color is `null`:
   - Mark the line entry for removal.
   - After processing all lines on an edge, remove null-color entries.
   - If `lines` array becomes empty, remove the entire edge feature.

### Step 8 — Output

Write the modified GeoJSON to stdout. Preserve all properties other than `color` and `freq_color` on line entries. Remove edges whose `lines` array is empty. Optionally remove nodes no longer referenced by any edge.

Log a summary to stderr:
```
Bucket ≤10 min:    234 route-segments
Bucket ≤15 min:    891 route-segments
...
No service bucket: 47 route-segments
Geographic fallback used: 1823 node-endpoint resolutions
Unresolvable (no stop within 200 m): 12 node-endpoint resolutions
```

---

## Output JSON format

```json
{
  "type": "Feature",
  "geometry": { "type": "LineString", "coordinates": [...] },
  "properties": {
    "from": "0x...",
    "to":   "0x...",
    "id":   "0x...",
    "dbg_lines": "77,8,66",
    "lines": [
      {
        "id":         "0x...",
        "label":      "77",
        "color":      "d32f2f",      ← route's global (best-segment) color
        "freq_color": "d32f2f"       ← this segment's color (may differ)
      },
      {
        "id":         "0x...",
        "label":      "8",
        "color":      "f57c00",
        "freq_color": "9e9e9e"       ← outer segment of a short-turn route
      }
    ]
  }
}
```

The C++ renderer change reads `freq_color` for actual rendering and uses `color` only as a fallback (junction arcs).

---

## Rendering behavior after the C++ change

| Location | What color is used |
|---|---|
| SVG edge segments | `freq_color` (per-segment, correct) |
| SVG junction arcs | `color` (global = route's best-frequency color; approximate) |
| MVT edge features | `freq_color` (per-segment, correct) |
| MVT junction features | `color` (global; approximate) |

Junction arcs are small and only show an approximate color at short-turn termini. This is acceptable for v1.

---

## Updated `chicagoland.sh`

```bash
#!/bin/bash
set -e

GTFS=gtfs/chicagoland
GTFS_ZIP=gtfs/chicagoland.zip
CONFIG=freq.toml

# Step 1: build graph and run topo (slow; cache result)
if [ ! -f chicagoland-bus-topo ]; then
    gtfs2graph -m bus "$GTFS" > chicagoland-bus-graph
    topo < chicagoland-bus-graph > chicagoland-bus-topo
fi

# Step 2: optimize line orderings (moderate cost; cache result)
if [ ! -f chicagoland-bus-loom ]; then
    loom < chicagoland-bus-topo > chicagoland-bus-loom
fi

# Step 3: apply frequency coloring (fast; re-run to change time window or colors)
python3 freq.py --config "$CONFIG" --gtfs "$GTFS_ZIP" chicagoland-bus-loom \
    > chicagoland-bus-freq

# Step 4: render
transitmap -l < chicagoland-bus-freq > chicagoland-bus.svg
transitmap -l --mvt chicagoland-bus.mvt < chicagoland-bus-freq
```

---

## Empirical data notes

| Item | Value |
|---|---|
| `stop_times.txt` rows | ~3.9 M |
| `trips.txt` rows | ~73 K |
| `calendar_dates.txt` rows | 126 (exceptions only; `calendar.txt` is primary) |
| `frequencies.txt` | Empty (not used) |
| Pre-topo nodes with `station_id` | 25,038 / 25,038 (100%) |
| Post-topo nodes with `station_id` | 13,461 / 15,236 (88%) |
| Post-topo edges | 17,534 |
| Bus routes (by `route_short_name`) | 255 distinct labels + 3 with empty label |
| Empty-label routes | `pace_100` (Pulse Milwaukee), `pace_101` (Pulse Dempster), `pace_905` (Schaumburg Trolley) — disambiguate by stop-pair matching |
| Line IDs in topo JSON | C++ pointer addresses; change between separate gtfs2graph runs; use `label` for GTFS matching |
| loom preserves `color` field | Confirmed ✓ |
| loom preserves `style` field | Confirmed ✓ (via `LineEdgePL::getAttrs`) |

---

## Dependencies (managed via pixi)

```toml
[dependencies]
python = ">=3.11"
pandas = ">=2.0"
scipy  = ">=1.11"   # KDTree for geographic fallback
numpy  = ">=1.25"
# tomllib is in Python 3.11+ stdlib; no extra package needed
```

---

## Appendix: Variable Line Width — Why It's Hard

Implementing frequency-proportional line width requires coordinated changes to three components.

### 1. `loom` optimizer

The ILP cost model counts crossings between adjacent line bundles assuming uniform width, so crossing cost is uniform. With variable widths:
- A wide line crossing a narrow one is visually more disruptive than two narrow lines crossing — the cost function must weight crossings by the product of the two lines' widths.
- Total bundle width on each edge changes per-segment, affecting how offsets are computed at nodes.

### 2. `transitmap` offset math

Lines are currently placed at:
```
offset_i = (i − (N−1)/2) × (lineWidth + 2×outlineWidth + lineSpacing)
```
With variable widths, the offset of line `i` is:
```
offset_i = Σ(width[0..i−1]) + width[i]/2 − totalBundleWidth/2
```
This must be updated in the SVG renderer, MVT renderer, and the node junction arc computation.

### 3. Node junction geometry

Connecting arcs at nodes assume the perpendicular offset uses the same spacing step on both the incoming and outgoing edge. With variable widths, routes on different edges have different offsets, requiring per-edge offset lookups in the junction drawing code.

### 4. Taper caps at splits

When a line branches off partway through an edge, a tapered cap is drawn at uniform width. The cap would need to match the route's specific width.

### 5. Ordering heuristics

loom has no objective for grouping similar-width lines or placing wider (more frequent) lines toward the center of a bundle. A secondary objective would be needed for good visual results.

**Conclusion**: Achievable but requires coordinated changes to loom's ILP formulation and transitmap's geometry calculations. Out of scope for initial implementation.

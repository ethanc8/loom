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

- Variable line width (deferred; specified separately in SPEC-thickness.md)
- Rail routes (excluded via `gtfs2graph -m bus`)
- Schematic/octilinear layout (`octi`)
- Modifying loom's optimizer

---

## Pipeline

```
# Run once; cache. topo takes 30 min – 2 h and ~14 GiB RAM on the Chicagoland dataset.
gtfs2graph -m bus gtfs/chicagoland > chicagoland-bus-graph
topo < chicagoland-bus-graph > chicagoland-bus-topo

# Run whenever time window, metric, or bucket colors change.
python3 freq.py \
    --config freq.toml \
    --gtfs gtfs/chicagoland \    # extracted GTFS directory
    chicagoland-bus-topo \       # ← reads topo output
    > chicagoland-bus-freq

loom < chicagoland-bus-freq > chicagoland-bus-loom

transitmap -l < chicagoland-bus-loom > chicagoland-bus.svg
transitmap -l --mvt chicagoland-bus.mvt < chicagoland-bus-loom
```

**Pipeline order — why freq.py runs before loom**: topo is the expensive step (30 min–2 h) and is never re-run on a config change. loom only optimizes line *orderings*; running it after freq.py means it optimizes for exactly the set of lines that will render — invisible routes are already removed, so loom never accepts a crossing between two visible lines just to avoid crossing a ghost line. The `freq_color` field round-trips through loom automatically: the read (`LineGraph::extractLine`) and write (`LineEdgePL::getAttrs`) both live in `shared/linegraph`, which loom uses.

The cost is that loom runs on every config change. **Time loom on the first real run.** If it proves slow (tens of minutes), flip to the fallback order — cache `loom < chicagoland-bus-topo` once and run freq.py on the loom output instead. Both orders produce valid input for transitmap; the fallback only loses some ordering quality (occasional unnecessary crossings from ghost lines) and note that offsets are unaffected either way, since transitmap computes them from the lines actually present on each edge.

**Note**: the gtfs2graph changes below (route_id as line id) must land **before** the first cached `gtfs2graph -m bus` run, since changing gtfs2graph invalidates the topo/loom caches.

---

## Configuration File (`freq.toml`)

```toml
[window]
# A specific calendar date. Active service_ids are resolved from calendar.txt
# and calendar_dates.txt. Use a typical midday weekday (Tuesday or Wednesday,
# when school is in session, no public holidays).
date = "2026-05-13"
start_time = "10:00"   # HH:MM 24-hour. Times ≥ 24:00 are allowed (GTFS convention),
end_time   = "14:00"   # e.g. a late-evening window may be "22:00" – "26:00".

[metric]
# How to aggregate inter-trip gaps within the window.
# "max"       – longest gap (worst-case wait; hair-trigger: one odd gap
#               reclassifies the span it falls in)
# "mean"      – arithmetic mean of all gaps
# "median"    – 50th-percentile gap (robust to outliers)
# "count"     – window duration / number of trips ("buses per hour";
#               most stable, ignores spacing entirely)
# "effective" – sum(g²)/sum(g): the average headway experienced by a
#               randomly arriving rider. Equals the plain headway for even
#               service and degrades smoothly with bunching — recommended.
type = "effective"

# Optional polish pass over each route's span sequence (see Step 7b).
# Disabled when the section is absent or enabled = false.
[smoothing]
enabled = true
# A run of equal-bucket spans shorter than this is collapsed into the
# surrounding bucket when the runs on both sides agree with each other.
# Genuine changes (branch points, short turns) form long runs and survive.
min_run_m = 400
# Spans with no resolvable boundary stop on either end (rare: connector
# spans between two branch nodes) inherit a neighbor's bucket — the slower
# one if neighbors disagree — instead of falling to no_service.
fill_unknown = true

# Frequency buckets, evaluated in order (smallest max_headway_min first).
# The first bucket where max_headway_min >= computed headway is used.
# max_headway_min = "inf" is the catch-all (matches any headway).
# color: 6-digit hex WITHOUT '#'.
# OMITTING color makes the bucket invisible (TOML has no null type): its line
# entries are removed from the output JSON, and edges that become empty are
# also removed entirely. Invisible routes do not appear in the transitmap
# output.
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
# Omit color to make them invisible (removed from output).
[no_service]
# color = "cccccc"   # uncomment to render no-service routes in grey
```

---

## Required C++ Change 1: gtfs2graph emits GTFS route_id and label fallback

### Problem

`gtfs2graph` currently sets each line's `id` to a C++ pointer address (`util::toString(r.route)`), which is unique but changes between runs and cannot be matched back to the GTFS. It also emits an empty `label` for routes without a `route_short_name` (the three Pace Pulse / Schaumburg Trolley routes).

### Solution

The line `id` field survives the topo/loom round-trip verbatim (`LineGraph::extractLine` reads it as the dedup key; `LineEdgePL::getAttrs` re-emits it), so emitting the GTFS `route_id` as the `id` gives freq.py a direct, stable GTFS key with no downstream changes. The merged feed's `cta_`/`pace_` prefixes guarantee uniqueness across agencies.

1. `src/gtfs2graph/graph/EdgePL.cpp` (~line 189):
   ```cpp
   route["id"] = r.route->getId();          // GTFS route_id, was util::toString(r.route)
   route["label"] = r.route->getShortName().empty()
                        ? r.route->getLongName()   // fallback for Pace Pulse etc.
                        : r.route->getShortName();
   ```

2. `src/gtfs2graph/graph/NodePL.cpp` (~line 126): the node-level `excluded_conn`
   entries reference lines by the same id and **must stay consistent**:
   ```cpp
   obj["line"] = r.route->getId();          // was util::toString(r.route)
   ```

Consequences:
- freq.py matches lines to GTFS routes by `line["id"] == route_id` directly. No label-based reverse lookup, no empty-label disambiguation.
- The Pulse routes render with their long names ("Pulse Milwaukee Line") in `transitmap -l` output.

---

## Required C++ Change 2: Per-Segment Color Override

> **Status: partially implemented** in the working tree (LineEdgePL.h/.cpp, LineGraph.cpp, SvgRenderer.h/.cpp, MvtRenderer.cpp — the `colorOverride` plumbing and edge-segment rendering). The junction-arc gradient below is **not yet implemented**. Needs a compile + smoke test.

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

An optional per-edge color override that both renderers check before falling back to the shared Line object's color:

1. `LineEdgePL.h`: `std::string colorOverride;` field on `LineOcc`.
2. `LineEdgePL.cpp`: `getAttrs()` emits `line["freq_color"]` when non-empty; new `setColorOverride(const Line*, const std::string&)` method.
3. `LineGraph.cpp` (`extractLine`): after the existing `addLine` calls, read `freq_color` and call `e->pl().setColorOverride(l, util::normHtmlColor(fc))`.
4. `SvgRenderer.cpp`: `renderLinePart` takes a `colorOverride` parameter; `renderEdgeTripGeom` passes `lo.colorOverride`; the stroke color is `colorOverride.empty() ? line.color() : colorOverride`.
5. `MvtRenderer.cpp` (`renderEdgeTripGeom`): same fallback expression for `params["color"]`, `params["line-color"]`, and the outline's `paramsOut["line-color"]`.

### Junction arcs: gradient color transitions

A route's bucket color can change at a node (e.g. a short-turn terminus where the inner segment is ≤10 min and the outer segment ≤30 min). An abrupt color change at the junction would read as two separate routes, so connecting arcs blend between the two segment colors:

- Each `InnerGeom` connects a slot on a `from` edge to a slot on a `to` edge (its `Partner`s carry edge + line). Look up the `LineOcc` on each edge; the effective color per side is `colorOverride` if non-empty, else `line->color()`.
- If both sides have the same effective color → solid stroke, as today.
- If they differ →
  - **SVG** (`renderClique`): emit a `<linearGradient>` into the document `<defs>` (alongside the existing end-marker defs mechanism) with `gradientUnits="userSpaceOnUse"` and the gradient axis running between the arc's two endpoints (after front-cropping); stop 0 = from-side color, stop 1 = to-side color. Stroke with `url(#...)`. Arcs are short, so a linear axis approximates the curve well.
  - **MVT**: MapLibre's `line-gradient` cannot be driven per-feature, so split the arc geometry at its midpoint into two features, one per color.
- Terminus arcs (`getTerminusLine`/`getTerminusBezier`, single edge) use that edge's effective color directly.

freq.py still sets the global `color` field to the route's best-bucket color on all edges — it serves as the fallback wherever no `freq_color` override is present.

---

## `freq.py` — Algorithm

### Inputs

| Argument | Description |
|---|---|
| positional: `graph_file` | Path to topo output GeoJSON (or `-` for stdin); loom output also accepted in the fallback pipeline order |
| `--config` | Path to `freq.toml` |
| `--gtfs` | Path to an **extracted GTFS directory** (zip support optional, not needed for v1) |

Output: modified GeoJSON written to stdout.

**Coordinate system**: the loom GeoJSON is WGS84 lon/lat (verified empirically on `chicagoland-all-loom`; gtfs2graph works in web mercator internally but serializes lat/lng). No reprojection needed to compare with `stops.txt`.

### Step 1 — Load GTFS tables

Load with `dtype=str` everywhere. Cast `stop_sequence` to int after loading.

Required files and columns:
- `routes.txt`: `route_id`, `route_short_name`, `route_type`
- `trips.txt`: `trip_id`, `route_id`, `service_id`
- `stop_times.txt`: `trip_id`, `stop_id`, `departure_time`, `stop_sequence`; `pickup_type`, `drop_off_type` **if present** (they are optional GTFS columns — guard for absence)
- `stops.txt`: `stop_id`, `stop_lat`, `stop_lon`
- `calendar.txt`: `service_id`, `monday`–`sunday`, `start_date`, `end_date`
- `calendar_dates.txt`: `service_id`, `date`, `exception_type`

(`direction_id` is **not** used — see Step 7.)

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
active_trips = trips[trips["service_id"].isin(active_service_ids)]
st = stop_times.merge(active_trips[["trip_id", "route_id"]], on="trip_id")
```

**Filter out non-revenue trips**: exclude trips where ALL stops have `pickup_type == "1"` AND `drop_off_type == "1"` (deadhead/positioning moves). Use the vectorized form — a `groupby.apply` over 3.9M rows takes minutes:

```python
if "pickup_type" in st.columns and "drop_off_type" in st.columns:
    revenue_mask = (st["pickup_type"].fillna("") != "1") | \
                   (st["drop_off_type"].fillna("") != "1")
    revenue_trip_ids = st.loc[revenue_mask, "trip_id"].unique()
    st = st[st["trip_id"].isin(revenue_trip_ids)]
```

### Step 4 — Build data structures

All hot-loop lookups must be O(1) dicts. Avoid `groupby.apply` for construction (too slow at this scale); iterate over sorted numpy arrays instead.

```python
st["dep_sec"] = st["departure_time"].map(parse_time)
st["stop_sequence"] = st["stop_sequence"].astype(int)

# {trip_id: {stop_id: [(stop_sequence, dep_sec), ...]}}
# The inner list handles loop routes where a trip visits a stop more than once.
trip_stops = ...

# {route_id: {stop_id: (lat, lon)}} — stops served by any active trip of the route,
# with coordinates from stops.txt. Used for both validation and geographic fallback.
route_stops = ...

# {route_id: [trip_id, ...]} — active trips per route (no direction split).
route_trips = ...

bus_route_ids = set(routes[routes["route_type"] == "3"]["route_id"])
```

No KD-tree / scipy: route stop sets are at most a few hundred stops, so the geographic fallback in Step 6 brute-forces haversine over `route_stops[route_id]`. This is simpler and avoids the degree-space distortion and "nearest overall stop isn't on this route" problems a global KD-tree has.

### Step 5 — Build node and edge maps from the GeoJSON

```python
node_map = {f["properties"]["id"]: f for f in features if f["geometry"]["type"] == "Point"}
edges = [f for f in features if f["geometry"]["type"] == "LineString"]
```

### Step 6 — Resolve the candidate stop_ids for a node, given a specific route

Called for the `from` and `to` node of each edge, per route. Memoize on `(node_id, route_id)`. Returns a **(possibly empty) set of stop_ids**, not a single stop:

```python
def resolve_stops(node, route_id) -> frozenset:
    cands = set()
    sid = node["properties"].get("station_id", "")
    if sid and sid in route_stops.get(route_id, {}):
        cands.add(sid)    # station_id is valid for this route
    # plus ALL stops of the route within 200 m of the node
    # (brute-force haversine against route_stops[route_id])
    ...
```

**Why a set** (found empirically on the 2026-07 Chicagoland feeds): the two travel directions of a street use distinct GTFS stops a few metres apart on opposite curbs, and a topo node stands for the whole street corner. Resolving each node to a *single* nearest stop picks a curb arbitrarily; when an edge's two nodes pick opposite curbs (e.g. `pace_226e0245` / `pace_226w0250`), **no single trip visits both stops** and the segment is falsely classified no_service — on the real data this hit 79% of resolved no_service entries and made frequent routes flicker between their color and no_service every few edges. With candidate sets, a trip qualifies if it visits *any* from-candidate and *any* to-candidate (Step 7 merges the occurrence lists per set), which is direction-blind and immune to curb choice.

**200 m threshold** (not 100 m): suburban Pace routes have stops spaced 500 m–1 km apart; topo intersection nodes can be up to ~150 m from the nearest stop on dense city grids. 200 m provides enough slack while excluding clearly wrong matches. The same radius collects the candidate set; sets of neighboring nodes may overlap — harmless, since qualification only needs strict `stop_sequence` order between a from- and a to-occurrence.

Always validate station_id against `route_stops[route_id]` even when station_id is present — a node may carry the stop_id of one route while a different route on the adjacent edge has a nearby-but-distinct stop. A valid station_id does **not** short-circuit the geographic scan: its opposite-curb partner stop must still enter the set.

Resolution itself is silent (an empty set is *normal* for interior nodes a route passes without stopping); problems are logged at span level in Step 7.

### Step 7 — Walk each route into stop-to-stop spans; compute headway per span

**Classification is per span, not per graph edge.** Topo nodes are an implementation detail of the graph: they appear at junctions with other routes and at other routes' stops. From the viewer's perspective a route's color must only be able to change at *its own* stops (or where its variants diverge). Coloring individual edges instead produces flapping at invisible junction nodes and false no_service on express corridors whose interior nodes resolve to nothing.

**7a.1 — Build spans.** Group all line entries by `route_id` (a GTFS route_id after C++ Change 1; routes not in `bus_route_ids` or without active trips get no_service wholesale). For each route, collect its edges and walk them into maximal runs bounded by:
- nodes whose candidate set for this route is **non-empty** (the route stops there),
- **branch nodes** of the route's own subgraph (degree ≠ 2 over the route's edges — variants diverge, so frequency may genuinely change),
- self-loop edges and graph boundaries.

Interior nodes (empty candidate set, degree 2) are walked through. Each span records its edges, its two boundary node ids, and its geometric length in metres (for smoothing).

**7a.2 — Qualify trips per span.** With `from_stops` / `to_stops` = the boundary nodes' candidate sets:

   **Normal case** (both non-empty, distinct): a trip qualifies if it visits at least one from-candidate **and** one to-candidate as *distinct* stop occurrences (a single visit to a stop the two sets share does not count). Its span-entry departure is the `dep_sec` of its earliest qualifying occurrence.

   **Degenerate case** (`from_stops == to_stops`, or exactly one set non-empty): presence-only — every trip serving any stop in the union qualifies, with its departure at its earliest such occurrence. Count degenerate spans in the summary.

   **Both sets empty**: mark the span **unknown** and log it. Unknown spans fall to no_service unless smoothing's `fill_unknown` is on (Step 7b).

**7a.3 — Group departures by `direction_id`.** Trips lacking a `direction_id` fall back to observed stop order (forward if a from-occurrence precedes the last to-occurrence, else reverse; degenerate spans use a single group). Note the change from earlier revisions: `direction_id` is still never mapped to edge orientation — it is used *only* to partition trips into consistent groups, and the min over groups is taken regardless. Observed-order grouping alone breaks down when the candidate sets overlap (short spans): one-way trips then pass the strict order test in *both* directions, interleaving the two directions' departures in each group and roughly halving the computed headway.

**7a.4 — Window filter and metric** (per group independently):
   - Keep entry departures with `window_start <= dep_sec < window_end`; sort; `gaps = [t[i+1] - t[i]]`
   - 0 trips in window → group has no headway
   - `count` metric: `window_dur_min / n_trips` (no gaps needed)
   - otherwise 1 trip in window → use `window_dur_min` as the headway (conservative)
   - otherwise apply `max`, `mean`, `median`, or `effective` = `sum(g²)/sum(g)` (also used when all departures coincide: fall back to `window_dur_min`)

**7a.5 — Take the minimum across groups**; no groups → no_service. **Assign the bucket** (first `[[buckets]]` entry with `max_headway_min >= headway_min`) to **every edge of the span**.

### Step 7b — Optional smoothing (config `[smoothing]`)

Runs per route over its span sequence; span adjacency crosses only boundary nodes shared by exactly two spans of the route, so branch points always break runs.

1. **fill_unknown**: unknown spans inherit a neighboring span's bucket across *any* shared boundary node (unknown spans typically sit between branch nodes); if neighbors disagree, take the slower bucket. Iterate to a fixpoint; leftovers fall to no_service.
2. **Collapse short runs**: partition spans into maximal runs of equal bucket; a run shorter than `min_run_m` with at least two adjacent runs that all agree on one bucket is reassigned to that bucket. Iterate to a fixpoint (merges can cascade).

After processing all routes:

**Per-route global color** (used for the shared `Line` object): the best (lowest-index) *final* bucket over all the route's spans, set as `line_entry["color"]` on **all** edges of the route (it must be consistent, since only the first occurrence is read by `extractLine`). **`freq_color`** = the span's bucket color per entry. **Invisible buckets**: entries whose bucket has no `color` are removed; edges whose `lines` array becomes empty are removed.

### Known limitation: mixed local/skip-stop patterns under one route_id

A trip only qualifies on a span if it serves a boundary stop on each side. When a single route_id mixes local and skip-stop *patterns*, a skip-stop trip serving neither boundary of a local-only span doesn't count there, undercounting the headway. (Express routes with their own route_id are fine: their spans run between their own stops.) **Required mitigation**: track, per route, the set of active trips that qualified on at least one span; report `active − qualified` counts to stderr so the amount of dropped service is visible.

### Step 8 — Output and cleanup

Write the modified GeoJSON to stdout. Preserve all properties other than `color` and `freq_color` on line entries (including `direction`).

Cleanup after invisible-route removal is **mandatory**, not optional — stale references can crash transitmap or render ghost stations:

1. Remove edges whose `lines` array is empty.
2. Remove node features no longer referenced by any remaining edge's `from`/`to` (orphan nodes would otherwise still render as station labels under `transitmap -l`).
3. Strip node-level connection-exception entries (`excluded_conn` etc.) that reference a removed line id or a removed node/edge.

Test this path explicitly: run once with a bucket deliberately set to `null` that catches real routes, and confirm transitmap renders without errors or ghost stations.

Log a summary to stderr:
```
Bucket ≤10 min:    234 route-segments
Bucket ≤15 min:    891 route-segments
...
No service bucket: 47 route-segments
Spans: 23123 across 259 routes
Span boundaries resolved geographically (invalid/missing station_id): 5685
Span boundaries with no stop within 200 m: 533
Degenerate (equal/one-sided boundary) spans: 96
Spans with no resolvable boundary at all: 12
Smoothing: filled 12 unknown spans (0 left as no_service), collapsed 179 short runs (212 spans)
Route cta_X9: 41 active trips, 39 qualified somewhere, 2 never qualified
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
        "id":         "cta_77",
        "label":      "77",
        "color":      "d32f2f",      ← route's global (best-segment) color
        "freq_color": "d32f2f"       ← this segment's color (may differ)
      },
      {
        "id":         "cta_8",
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
| SVG junction arcs | `freq_color` of both adjacent segments; linear gradient between them when they differ |
| MVT edge features | `freq_color` (per-segment, correct) |
| MVT junction features | `freq_color` of both adjacent segments; arc split at midpoint when they differ |
| SVG line labels (`-l`) | effective color of the edge the label is placed on (`freq_color` override if present, else line color) — a route's labels differ along its length |

The global `color` (route's best-frequency bucket) is only a fallback for line entries without a `freq_color` override.

With span-based classification a route's `freq_color` can only change at its own stops or branch points, so junction-arc gradients and MVT midpoint splits occur (mostly) under station geometry; the exposed round line-end caps at nodes are invisible whenever both sides share a color.

---

## Updated `chicagoland.sh`

```bash
#!/bin/bash
set -e

GTFS=gtfs/chicagoland
CONFIG=freq.toml

# Step 1: build graph and run topo (slow; cache result)
if [ ! -f chicagoland-bus-topo ]; then
    gtfs2graph -m bus "$GTFS" > chicagoland-bus-graph
    topo < chicagoland-bus-graph > chicagoland-bus-topo
fi

# Step 2: apply frequency coloring (fast; re-run to change time window or colors)
python3 freq.py --config "$CONFIG" --gtfs "$GTFS" chicagoland-bus-topo \
    > chicagoland-bus-freq

# Step 3: optimize line orderings on the filtered graph.
# Time this on the first run; if it proves slow, flip to the fallback order
# (cache loom on the topo output, run freq.py on the loom output).
loom < chicagoland-bus-freq > chicagoland-bus-loom

# Step 4: render
transitmap -l < chicagoland-bus-loom > chicagoland-bus.svg
transitmap -l --mvt chicagoland-bus.mvt < chicagoland-bus-loom
```

---

## Future Extensions

Deliberately excluded from the MVP; developed in stages once the core map works. (Travel-time annotations are specified separately in SPEC-traveltimes.md, variable width in SPEC-thickness.md.)

### Pinned lines (user-specified color and width)

Allow the config to fix the appearance of specific routes. Pinned routes are **not analyzed for frequency at all** — they render exactly as the user specifies.

```toml
[[pinned]]
route_id = "cta_9"      # GTFS route_id == line id in the graph (C++ Change 1)
color = "000000"
width = 1.25            # optional; takes effect once SPEC-thickness.md is implemented

[[pinned]]
route_id = "pace_905"
hidden = true           # remove this route from the map regardless of frequency
```

Semantics — entirely inside freq.py, no C++ changes:

- **Matching** is by `route_id` only, which equals the line `id` in the graph JSON after C++ Change 1. Two `[[pinned]]` entries with the same `route_id` are a config error. Warn on stderr for any pinned route_id that never appears in the graph (typo detection).
- **Pinned, not hidden**: Steps 6–7 are skipped entirely for this route — no stop resolution, no trip qualification, no bucket assignment. Every edge's line entry gets `freq_color` = the pinned color, the global `color` field = the pinned color, and (once thickness lands) `freq_width` = the pinned width. Because the color is uniform across all segments, junction gradients degenerate to solid strokes automatically — no special-casing in the renderers.
- **Pinned routes bypass `no_service`**: they render even with zero trips in the window. This is the point — e.g. keeping a route visible on a map of a time period when it doesn't run, or overlaying hand-styled rail lines if rail is later merged into the graph.
- **`hidden = true`**: the route is removed exactly like an invisible bucket, going through the same mandatory Step 8 cleanup (empty edges, orphan nodes, stale `excluded_conn` refs). `hidden` and `color` are mutually exclusive in one entry.
- **Reporting**: pinned routes are excluded from bucket statistics and from the express-undercount active-vs-qualified report; the stderr summary gets its own line, e.g. `Pinned: 2 routes (1 hidden)`.

### Legend generation

A frequency map is unreadable without a legend, and the buckets are user-configured, so the legend must be generated from the same config. freq.py gets a `--legend legend.svg` option emitting a **standalone** SVG: one row per visible bucket showing a short line sample (bucket color; bucket width once SPEC-thickness.md lands) and the headway range text ("every 10 min or better", "10–15 min", …), plus rows for a visible `no_service` bucket and optionally for pinned lines. Composited onto the final map manually, in QGIS, or by the web page — transitmap stays out of it (it doesn't know buckets exist). Rebuild is automatic since the legend comes from the same `freq.toml` parse as the coloring.

### QA / diagnostics dump

The dominant quality risk of the whole system is *silent* stop-resolution errors (wrong stop matched → wrong headway → wrong color, no crash). freq.py gets a `--debug out.geojson` option dumping, for QGIS inspection:

- **Point features**: every geographic-fallback resolution (with `route_id`, matched `stop_id`, distance in m), every unresolved endpoint, every degenerate (same-stop / one-stop) edge endpoint.
- **Line features**: edges assigned `no_service`; and adjacent same-route segments whose buckets differ by more than one step — legitimate at short-turn termini, suspicious anywhere else, so these are the first places to audit.

Cheap to implement (all the data already flows through Step 7's loop) and should be used to spot-check a few corridors against known references (2024 Pace JW map, CTA frequent-network list) before trusting any output.

### MapLibre style deliverable

The MVT output is unusable without a style, and the specs so far assume one exists. Deliverable: a `style.json` (MapLibre) checked into `playground/`, containing:

- line layers for edge outlines and fills with data-driven paint: `line-color: ["get", "color"]` (the renderer emits per-feature colors, including `freq_color` overrides), and once thickness lands `line-width: ["*", baseWidthForZoom, ["to-number", ["get", "width"], 1.0]]`;
- the inner-connections layer above the edge layers;
- placeholders for the `traveltimes` layer (SPEC-traveltimes.md) and a basemap source underneath.

Small task, but it must be tracked or the web/basemap output path silently has no owner.

### Route badges on the lines

Today `Labeller::labelLines` places route-name labels *alongside* the corridor (a `LineLabel` holds a geometry offset from the bundle plus the list of lines). Instead, render highway-shield-style **badges on the lines themselves**: a rounded rect with the route number sitting on the line's own stroke, repeated at intervals along long corridors.

- **SVG**: at candidate points along each line's offset polyline (not the bundle centerline — each route badges its own stroke), emit rect + centered text. Badge fill = the line's `freq_color` with white text, or white fill with black text for legibility at small widths (thin infrequent lines can't carry text inside their stroke — the badge is deliberately larger than the line and acts as a marker). Reuse the `Labeller` collision machinery: a badge is a small `band` rectangle scored with the existing overlap penalties; repeat spacing (e.g. every N map-km) as a config flag.
- **MVT**: mostly free — the features already carry a `line` attribute with the label; a MapLibre `symbol` layer with `symbol-placement: "line"` renders repeated along-line text natively. The badge box is a `text-halo` or an `icon` in the style; no C++ needed beyond what exists.
- Interaction: on a frequency map, color no longer encodes route identity, which makes on-line badges *more* valuable than on a classic map — they're the only route identification left besides the side labels.

### Combined-frequency corridor grouping

Routes that interleave along a shared trunk provide better effective frequency than any of them alone (two 20-min routes → 10-min corridor). Show the *combined* frequency on shared segments, under two constraints: don't group express with local, and don't reward uneven interleaving.

- **Eligibility**: routes group on a maximal shared edge chain only if (a) the overlap is long enough (`min_overlap_km`, or a minimum shared-stop count) and (b) their stop patterns on the overlap are near-identical — Jaccard similarity of served stops ≥ `pattern_similarity` (default ~0.9). Constraint (b) is what excludes express/limited overlays: an X-route sharing the street but skipping stops fails the similarity test.
- **Combined headway**: merge the qualifying departure times of all grouped routes at the segment entry stop and apply the configured metric to the merged gap sequence. **With `metric = "max"` the evenness requirement falls out automatically**: two 30-min routes leaving at :00/:05 produce a merged max gap of 25 min — barely better than either route alone — while a :00/:15 interleave yields 15. No separate evenness heuristic needed; for `mean`/`median` metrics add a guard: use the combined value only if it beats the best individual headway by ≥ `improvement_factor` (default 1.5×).
- **Rendering (v1 of this extension)**: keep the parallel strokes, color every participating line entry on the shared edges with the combined bucket's color. Documented caveat: a reader may misread each stroke as individually that frequent — mitigated by the route badges above ("77" + "49" badges on two red lines reads correctly). The cleaner alternative — replacing the bundle with one synthetic merged line — is rejected for now: it requires graph surgery in freq.py and breaks junction-arc continuity at split nodes (arcs are drawn per line id; a synthetic trunk line has no arc partner on the branch edges, leaving visible gaps).
- Gradient junction arcs already handle the color transition where a route enters/leaves a grouped corridor.

### Station-label filtering and scale control

transitmap already has most of the needed knobs — the MVP should use them before any code is written: labels are opt-in (`-l`), `--no-render-stations` hides stop geometry entirely, `--no-deg2-labels` suppresses labels on pass-through (degree-2) stops — which is most of the 200 m-spaced city stops — and `--station-label-textsize` / `--line-label-textsize` / `--resolution` / `--padding` control sizing. Note on scale: `--resolution` multiplies *everything* (geometry and line widths alike), so it changes output size, not visual density; to change how dominant the lines are relative to stop spacing, tune `--line-width`/`--line-spacing` (which are in map units ≈ meters) instead.

The actual extension: a **majorness whitelist** for station labels. New transitmap flag `--station-label-filter <file>` taking a list of station_ids; `Labeller::labelStations` skips nodes not in the set (and optionally the station-geometry rendering follows the same filter). The file comes from the traveltimes majorness machinery (`traveltimes.py --debug-nodes` output, SPEC-traveltimes.md) — one definition of "major" shared by labels and travel-time ticks.

### Dense-core treatment (the Loop)

The Loop renders badly while the rest of the network is fine: everything is major there, bundles converge, node fronts expand, and residual crossings concentrate. Ranked approaches:

1. **Inset panel (recommended first)**: render the core twice. A small script crops the post-loom GeoJSON to a bounding box (keep every edge intersecting the bbox plus its endpoint nodes — no geometry surgery, ordering and colors already fixed), then a second transitmap run renders the crop with a larger line-width-to-geography ratio. Composite as a classic map inset. Cheap, no C++ changes, and iterating on the bbox is instant since it's post-loom.
2. **Parameter tuning on the main render**: `--tight-stations`, smaller `--line-width`/`--line-spacing`, higher `--smoothing` may each help legibility downtown without hurting the suburbs; worth a sweep before writing any code.
3. **Spatially varying topo aggregation**: topo's stop-clustering distance is a single global parameter; the Loop arguably wants a smaller one than suburban Pace territory. Making it spatially varying (e.g., by local stop density) is a real modification to topo and the most expensive option — only pursue if 1–2 prove insufficient. Note any topo change invalidates the cache (30 min–2 h re-run) per experiment, which is exactly why this is ranked last.

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
| Bus routes | 255 with `route_short_name` + 3 without (`pace_100` Pulse Milwaukee, `pace_101` Pulse Dempster, `pace_905` Schaumburg Trolley) — handled by the label fallback in C++ Change 1; no special-casing in freq.py |
| Line IDs in graph JSON | GTFS route_ids after C++ Change 1 (stable across runs, direct GTFS key) |
| Loom GeoJSON coordinates | WGS84 lon/lat (verified) |
| loom preserves `color` field | Confirmed ✓ |
| loom preserves `style` field | Confirmed ✓ (via `LineEdgePL::getAttrs`) |

---

## Dependencies (managed via pixi)

```toml
[dependencies]
python = ">=3.11"
pandas = ">=2.0"
numpy  = ">=1.25"
# tomllib is in Python 3.11+ stdlib; no extra package needed
# no scipy — geographic fallback brute-forces per-route stop sets (Step 4)
```

---

## Appendix: Variable Line Width

Superseded — frequency-proportional line width is fully specified in **SPEC-thickness.md** (per-bucket width multipliers, prefix-sum offset math in RenderGraph and both renderers, junction handling, and future extensions for tapered junction wedges and width-aware loom optimization).

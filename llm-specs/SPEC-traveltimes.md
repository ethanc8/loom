# Travel-Time Annotations Specification (Future Extension)

## Overview

Annotate the frequency map with scheduled travel times between **major nodes**, the way topographical maps mark distances between reference points: small tick marks on the corridor at each major node, and the time in minutes labeled midway along each span. Builds on SPEC.md (requires freq.py, the visible-line filtering, and the C++ Change 1 route_ids); independent of SPEC-thickness.md except where noted.

Example: a corridor with major nodes A–B–C gets three ticks and two numbers ("7", "4"). Consecutive spans share ticks, so annotation density stays low even on long corridors.

## Visual design (topographical convention)

- **Tick**: a short stroke perpendicular to the corridor at each major node, placed just outside the line bundle (offset `getTotalWidth(e)/2 + margin` from the corridor centerline; length ≈ 1.5 × base line width; dark grey, thin). One tick per major node per corridor — shared by the spans on either side.
- **Label**: the span's time in minutes as a compact text label ("7′") with a white halo, placed alongside the corridor near the span's midpoint, offset outside the bundle on the same side as the ticks.
- Side selection (which side of the corridor) is delegated to transitmap's `Labeller` collision scoring (see Rendering below); ticks follow the side chosen for their adjacent labels where possible.

The engineering dimension convention (`|<— 3 min —>|`) was considered and rejected: extension lines and straight dimension chains fit schematic drawings, not dense curved geographic corridors, where every extension line is a collision with neighboring corridors or labels.

## Major node determination

Majorness is the union of three sources, followed by a thinning pass. All parameters live in `freq.toml` under `[traveltimes]`.

1. **Junction score (automatic)**: graph nodes where ≥ `min_routes_junction` (default 3) distinct *visible* routes meet across ≥ 3 incident edges. Plain two-route crossings don't qualify by default; raise or lower per taste.
2. **Rail-station proximity (automatic)**: the merged GTFS still contains rail stops and routes even though the graph is bus-only. Collect every stop served by an active trip of a `route_type` ∈ {0, 1, 2} route, plus all `location_type = 1` parent stations; any bus-graph node within `rail_station_radius_m` (default 150 m) of one is major. This is deliberately independent of LOOM's stop clustering, which does not reliably merge bus stops with the adjacent rail station.
3. **Manual waypoints (config)**: `[[traveltimes.waypoint]]` entries with either a `stop_id` or a `lat`/`lon` pair (snapped to the nearest graph node within `waypoint_snap_m`, default 200 m); required `name` for diagnostics. For malls and anything else with no GTFS signal. `[[traveltimes.exclude]]` entries (same matching) veto auto-detected nodes.
4. **Thinning**: after span construction, any span shorter than `min_span_minutes` (default 3) is collapsed by dropping the **lower-priority** endpoint and merging its two spans. Priority order: manual waypoint > rail station > junction score (ties: higher route count wins). Repeat until no span is below threshold. This keeps the Loop and other dense junction clusters from annotating every block.

## Span construction

Operate on the **visible** graph (after invisible-route removal):

1. Split the graph at every major node and at every branch node (degree ≥ 3). The result is a set of chains (paths through degree-2 non-major nodes).
2. A chain is an annotatable **span** iff both endpoints are major. Chains ending at a non-major branch node are not annotated in v1 (a span "through" a branch point is ambiguous — which continuation? — and branch nodes usually clear the junction-score bar anyway; revisit only if real output shows gaps).
3. Deduplicate: each physical span is annotated once, regardless of how many routes traverse it.

## Time computation

For each span with endpoints A, B:

1. Candidate routes: lines present on **all** edges of the span.
2. Per route, resolve A and B to stop_ids with the same `resolve_stop` machinery as SPEC.md Step 6 (station_id validation + haversine fallback).
3. Qualifying trips: active-window trips (same service-date and window logic as freq.py) serving both stops, classified by observed stop-sequence order (SPEC.md Step 7.3 rules, including the loop-route rule).
4. Per trip, travel time = `dep(later stop) − dep(earlier stop)` — taken directly from stop_times, **not** by summing per-edge times, so intermediate-stop resolution errors can't accumulate.
5. Aggregate: **median over all qualifying trips of all candidate routes, both directions pooled**, rounded to the nearest minute. Scheduled times are direction-asymmetric under traffic; pooling hides that, but per-direction display doubles clutter for little gain (deferred, see Future work).
6. Spans with zero qualifying trips get no annotation (log to stderr). One-way couplets (opposite directions on parallel streets, common downtown) work automatically: each one-way corridor's span only ever qualifies trips in its own direction.

## Pipeline and data flow

```
topo → freq.py → loom → traveltimes.py → transitmap --annotations tt.json
                  │                          ▲
                  └── loom output ───────────┘ (graph stream, unchanged)
```

- **`traveltimes.py` runs after loom** and emits a **sidecar file**, leaving the graph stream untouched. Two hard reasons:
  - Node ids are pointer addresses regenerated by each tool; ids captured pre-loom would dangle. Reading the loom output gives ids and geometry exactly as transitmap will see them (loom reorders lines but never moves geometry).
  - Injecting non-graph features into the graph stream risks `LineGraph` misparsing them as nodes; a sidecar avoids the question entirely.
- **Shared library**: refactor freq.py into a small package (`freqlib`: GTFS loading, service-date resolution, `resolve_stop`, trip qualification) imported by both scripts, rather than duplicating ~half of freq.py.
- Inputs: loom output (positional), `--config freq.toml` (reads `[window]` + `[traveltimes]`), `--gtfs` directory.

### Sidecar format (GeoJSON FeatureCollection)

```json
{ "type": "Feature", "geometry": { "type": "Point", "coordinates": [...] },
  "properties": { "type": "tt-tick", "angle_deg": 34.5, "node": "0x..." } }

{ "type": "Feature", "geometry": { "type": "LineString", "coordinates": [...] },
  "properties": { "type": "tt-span", "label": "7", "minutes": 7,
                  "from": "0x...", "to": "0x..." } }
```

Ticks carry their bearing (perpendicular to the local corridor direction, computed by traveltimes.py from the span geometry). Spans carry their full path geometry so transitmap can place the label anywhere along it, not just the literal midpoint.

## Rendering (transitmap, C++)

- New flag `--annotations <file>`; absent → feature is off, zero behavior change.
- **Ticks**: transitmap locates the nearest graph edge to the tick point (existing spatial-index machinery), computes the outward offset from `getTotalWidth(e)/2 + margin`, and draws the stroke at the given angle. With SPEC-thickness.md in place, `getTotalWidth` is already per-edge-correct for variable widths.
- **Labels**: integrate with `transitmap/label/Labeller` rather than free placement — it already scores candidate positions by overlaps with lines, stations, and other labels (Labeller.h:59–66). Add an annotation-label class with penalty weights below station labels (station names win conflicts; a travel time can slide along its span, a station name cannot). Candidate positions: sampled along the span geometry on both sides of the corridor.
- **SVG**: ticks as line elements, labels as text with halo (same mechanism as station labels).
- **MVT**: a separate `traveltimes` layer; ticks as point features with `angle` attribute, labels as point features with `text` attribute — placement and font are the style consumer's job, per MVT convention.

## Configuration

```toml
[traveltimes]
enabled = true
min_routes_junction  = 3
rail_station_radius_m = 150
waypoint_snap_m       = 200
min_span_minutes      = 3

[[traveltimes.waypoint]]
name = "Woodfield Mall"
lat  = 42.0455
lon  = -88.0384

[[traveltimes.waypoint]]
name    = "Davis Station"
stop_id = "cta_DAVIS"

[[traveltimes.exclude]]
stop_id = "pace_..."
```

## Diagnostics

traveltimes.py writes to stderr: major-node counts per source (junction / rail / waypoint), waypoints that failed to snap, spans dropped by thinning, spans with no qualifying trips. Optionally (`--debug-nodes out.geojson`) a dump of all major nodes with their source and name, for QGIS inspection before committing to a waypoint list.

## Future work

- **Per-direction times** ("7/9") where scheduled asymmetry exceeds a threshold.
- **Spans through non-major branch nodes**, following the dominant route's continuation.
- **Smarter majorness**: ridership data (CTA publishes stop-level boardings; Pace less so) as a fourth source, if the manual waypoint list proves tedious.
- **Cumulative chaining**: topo-map style running totals from a chosen origin, rather than per-span times.

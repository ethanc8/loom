#!/usr/bin/env python3
"""freq.py — per-segment frequency coloring for the LOOM pipeline.

Reads a topo (or loom) GeoJSON graph, computes each route's service headway
per stop-to-stop span within a configured time window from GTFS, assigns
every (edge, route) pair a frequency bucket, and writes the modified GeoJSON
to stdout.

Classification is per SPAN, not per graph edge: for each route, its edges
are walked into maximal runs bounded by nodes that stand for stops of that
route (strictly: valid station_id or nearest node to a stop) or by branch
nodes of the route's subgraph. All edges of a span get the span's bucket,
so a route's color can only change at its own stops or where variants
diverge — never at other routes' stops or junction nodes, which are
implementation details of the graph. A trip counts toward a span if it
traverses it — it visits stops on both sides, where each side includes
the neighboring stops just beyond the span's ends. Departures are grouped
by GTFS direction_id (observed stop order for trips without one) and the
headway is the min over groups.

On every line entry it sets:

  color       the route's global color: the bucket of its best (minimum)
              headway over all segments. Must be identical on all edges,
              since LOOM reads it only from the first occurrence; used as
              the fallback wherever no freq_color override is present.
  freq_color  this segment's bucket color; read per-edge by transitmap.

Line entries whose bucket has no color configured are removed from the
output, along with edges that become empty, nodes no longer referenced by
any edge, and connection-exception entries referencing removed lines/nodes.

Usage:
  python3 freq.py --config freq.toml --gtfs gtfs/chicagoland topo-out > freq-out
"""

import argparse
import json
import math
import os
import sys
import tomllib
from datetime import date as _date, datetime

import pandas as pd

NO_SERVICE = -1  # sentinel bucket index
UNKNOWN = -2     # span with no resolvable boundary stop on either end

FALLBACK_MAX_DIST_M = 200.0


def log(msg):
    print(msg, file=sys.stderr)


def parse_time(s: str) -> int:
    """GTFS HH:MM:SS to seconds; hours may be >= 24."""
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _norm_color(c):
    # LOOM expects bare hex ("d32f2f"); tolerate a leading '#' in the config.
    if c is None:
        return None
    return c.lstrip("#")


class Config:
    def __init__(self, path):
        with open(path, "rb") as f:
            cfg = tomllib.load(f)

        window = cfg["window"]
        d = window["date"]
        # quoted ("2026-05-13") parses as str, unquoted as a TOML date
        self.date_str = (d.strftime("%Y%m%d") if isinstance(d, _date)
                         else str(d).replace("-", ""))
        self.window_start = parse_hhmm(window["start_time"])
        self.window_end = parse_hhmm(window["end_time"])
        if self.window_end <= self.window_start:
            sys.exit("freq.py: end_time must be after start_time")

        self.metric = cfg.get("metric", {}).get("type", "max")
        if self.metric not in ("max", "mean", "median", "count", "effective"):
            sys.exit(f"freq.py: unknown metric type '{self.metric}'")

        sm = cfg.get("smoothing", {})
        self.smoothing_enabled = bool(sm.get("enabled", False))
        self.smoothing_min_run_m = float(sm.get("min_run_m", 400.0))
        self.smoothing_fill_unknown = bool(sm.get("fill_unknown", True))

        # [(max_headway_min, color-or-None), ...] in config order
        self.buckets = []
        for b in cfg["buckets"]:
            mh = b["max_headway_min"]
            mh = math.inf if mh == "inf" else float(mh)
            self.buckets.append((mh, _norm_color(b.get("color"))))
        if not any(mh == math.inf for mh, _ in self.buckets):
            log("freq.py: warning: no catch-all bucket "
                "(max_headway_min = \"inf\"); long headways will fall into "
                "the no_service bucket")

        self.no_service_color = _norm_color(
            cfg.get("no_service", {}).get("color"))

    def bucket_for(self, headway_min):
        for i, (mh, _) in enumerate(self.buckets):
            if mh >= headway_min:
                return i
        return NO_SERVICE

    def bucket_color(self, idx):
        if idx == NO_SERVICE:
            return self.no_service_color
        return self.buckets[idx][1]

    def bucket_name(self, idx):
        if idx == NO_SERVICE:
            return "no service"
        mh = self.buckets[idx][0]
        return "≤inf min" if mh == math.inf else f"≤{mh:g} min"


def read_gtfs_table(gtfs_dir, name, required_cols, optional_cols=(),
                    required=True):
    path = os.path.join(gtfs_dir, name)
    if not os.path.exists(path):
        if required:
            sys.exit(f"freq.py: missing required GTFS file {path}")
        return None
    wanted = set(required_cols) | set(optional_cols)
    df = pd.read_csv(path, dtype=str, usecols=lambda c: c in wanted)
    missing = set(required_cols) - set(df.columns)
    if missing:
        sys.exit(f"freq.py: {path} is missing columns: {sorted(missing)}")
    return df


def active_service_ids(gtfs_dir, date_str):
    dow = datetime.strptime(date_str, "%Y%m%d").strftime("%A").lower()

    cal = read_gtfs_table(
        gtfs_dir, "calendar.txt",
        ["service_id", dow, "start_date", "end_date"])
    mask = ((cal[dow] == "1")
            & (cal["start_date"] <= date_str)
            & (cal["end_date"] >= date_str))
    active = set(cal.loc[mask, "service_id"])

    cd = read_gtfs_table(
        gtfs_dir, "calendar_dates.txt",
        ["service_id", "date", "exception_type"], required=False)
    if cd is None:
        log("freq.py: no calendar_dates.txt; not applying service exceptions")
    else:
        cd = cd[cd["date"] == date_str]
        active |= set(cd.loc[cd["exception_type"] == "1", "service_id"])
        active -= set(cd.loc[cd["exception_type"] == "2", "service_id"])

    if not active:
        log(f"freq.py: warning: no active service on {date_str} — is the "
            "date within the feed's validity range?")
    return active


def load_gtfs(gtfs_dir, cfg):
    """Build the hot-loop lookup structures from the GTFS tables."""
    routes = read_gtfs_table(gtfs_dir, "routes.txt",
                             ["route_id", "route_type"],
                             ["route_short_name"])
    trips = read_gtfs_table(gtfs_dir, "trips.txt",
                            ["trip_id", "route_id", "service_id"],
                            ["direction_id"])
    stops = read_gtfs_table(gtfs_dir, "stops.txt",
                            ["stop_id", "stop_lat", "stop_lon"])
    st = read_gtfs_table(
        gtfs_dir, "stop_times.txt",
        ["trip_id", "stop_id", "departure_time", "stop_sequence"],
        ["pickup_type", "drop_off_type"])

    active = active_service_ids(gtfs_dir, cfg.date_str)
    active_trips = trips[trips["service_id"].isin(active)]
    st = st.merge(active_trips[["trip_id", "route_id"]], on="trip_id")

    # exclude non-revenue trips (every stop pickup_type=1 AND drop_off_type=1)
    if "pickup_type" in st.columns and "drop_off_type" in st.columns:
        revenue_mask = ((st["pickup_type"].fillna("") != "1")
                        | (st["drop_off_type"].fillna("") != "1"))
        revenue_trip_ids = st.loc[revenue_mask, "trip_id"].unique()
        before = st["trip_id"].nunique()
        st = st[st["trip_id"].isin(revenue_trip_ids)]
        dropped = before - len(revenue_trip_ids)
        if dropped:
            log(f"freq.py: dropped {dropped} non-revenue trips")

    timeless = st["departure_time"].isna()
    if timeless.any():
        log(f"freq.py: dropped {int(timeless.sum())} stop_times rows without "
            "departure_time")
        st = st[~timeless]

    st = st.copy()
    st["dep_sec"] = st["departure_time"].map(parse_time)
    st["stop_sequence"] = st["stop_sequence"].astype(int)

    # {trip_id: {stop_id: [(stop_sequence, dep_sec), ...] sorted by seq}}
    # the inner list handles loop trips visiting a stop more than once
    trip_stops = {}
    for tid, sid, seq, dep in zip(st["trip_id"].values, st["stop_id"].values,
                                  st["stop_sequence"].values,
                                  st["dep_sec"].values):
        trip_stops.setdefault(tid, {}).setdefault(sid, []).append((seq, dep))
    for stops_of_trip in trip_stops.values():
        for occs in stops_of_trip.values():
            occs.sort()

    stop_ll = {}
    for sid, lat, lon in zip(stops["stop_id"].values,
                             stops["stop_lat"].values,
                             stops["stop_lon"].values):
        try:
            stop_ll[sid] = (float(lat), float(lon))
        except (TypeError, ValueError):
            pass

    # {route_id: {stop_id: (lat, lon)}} for stops served by active trips
    rs_pairs = st[["route_id", "stop_id"]].drop_duplicates()
    route_stops = {}
    for rid, sid in zip(rs_pairs["route_id"].values,
                        rs_pairs["stop_id"].values):
        if sid in stop_ll:
            route_stops.setdefault(rid, {})[sid] = stop_ll[sid]

    # {route_id: [trip_id, ...]} for trips that survived all filters
    route_trips = {}
    for tid, rid in (st[["trip_id", "route_id"]].drop_duplicates()
                     .itertuples(index=False)):
        route_trips.setdefault(rid, []).append(tid)

    bus_route_ids = set(routes.loc[routes["route_type"] == "3", "route_id"])

    # {trip_id: direction_id} where present; used to group departures by
    # travel direction (grouping only — never mapped to edge orientation)
    trip_dir = {}
    if "direction_id" in trips.columns:
        for tid, d in zip(trips["trip_id"].values,
                          trips["direction_id"].values):
            if isinstance(d, str) and d != "":
                trip_dir[tid] = d
    if not trip_dir:
        log("freq.py: trips.txt has no usable direction_id; grouping "
            "departures by observed stop order instead")

    return trip_stops, route_stops, route_trips, bus_route_ids, trip_dir


class FreqAnalyzer:
    def __init__(self, cfg, trip_stops, route_stops, route_trips,
                 bus_route_ids, trip_dir):
        self.cfg = cfg
        self.trip_stops = trip_stops
        self.route_stops = route_stops
        self.route_trips = route_trips
        self.bus_route_ids = bus_route_ids
        self.trip_dir = trip_dir

        self._resolve_memo = {}
        self._headway_memo = {}

        self.route_qualified = {}  # route_id -> set of trip_ids

    def resolve_stops(self, node, route_id):
        """Candidate stop_ids of `node` on `route_id` as a (possibly empty)
        frozenset: the node's station_id if it belongs to the route, plus
        every stop of the route within FALLBACK_MAX_DIST_M. A set, not a
        single stop: the two directions of a street use distinct GTFS stops
        a few metres apart, and a topo node stands for the whole street
        corner — resolving to one curb arbitrarily means no single trip
        visits both stops. An empty set is normal for interior nodes the
        route passes without stopping. Memoized."""
        node_id = node["properties"].get("id")
        key = (node_id, route_id)
        if key in self._resolve_memo:
            return self._resolve_memo[key]

        stops_of_route = self.route_stops.get(route_id, {})
        cands = set()

        sid = node["properties"].get("station_id", "")
        if sid and sid in stops_of_route:
            cands.add(sid)

        lon, lat = node["geometry"]["coordinates"]
        for cand, (clat, clon) in stops_of_route.items():
            if haversine_m(lat, lon, clat, clon) < FALLBACK_MAX_DIST_M:
                cands.add(cand)

        res = frozenset(cands)
        self._resolve_memo[key] = res
        return res

    def _mark_qualified(self, route_id, trip_id):
        self.route_qualified.setdefault(route_id, set()).add(trip_id)

    def _group_headway_min(self, deps):
        """Headway in minutes from a group's entry departures, or None.

        The window edges count as gaps — (first dep - window start) and
        (window end - last dep) — so the gaps tile the whole window. A
        couple of stray trips in an otherwise empty window then read as
        the sparse service they are, not as their mutual spacing."""
        cfg = self.cfg
        deps = sorted(d for d in deps
                      if cfg.window_start <= d < cfg.window_end)
        if not deps:
            return None
        window_min = (cfg.window_end - cfg.window_start) / 60.0
        if cfg.metric == "count":
            # trips per window, expressed as an average headway; ignores
            # spacing entirely (a "buses per hour" measure)
            return window_min / len(deps)
        gaps = [deps[0] - cfg.window_start]
        gaps.extend(b - a for a, b in zip(deps, deps[1:]))
        gaps.append(cfg.window_end - deps[-1])
        if cfg.metric == "max":
            return max(gaps) / 60.0
        if cfg.metric == "mean":
            return (sum(gaps) / len(gaps)) / 60.0
        if cfg.metric == "effective":
            # average headway experienced by a randomly arriving rider:
            # sum(g^2)/sum(g); equals g for even service, degrades smoothly
            # with bunching instead of max's single-outlier cliff
            total = sum(gaps)  # == the window length
            return (sum(g * g for g in gaps) / total) / 60.0
        # median: time-weighted — the gap in effect at the median minute
        # of the window. A plain median of gaps would still read two
        # bunched trips in an empty window as frequent service.
        gaps.sort()
        half = sum(gaps) / 2.0
        acc = 0.0
        for g in gaps:
            acc += g
            if acc >= half:
                return g / 60.0
        return window_min

    def headway(self, route_id, from_stops, to_stops):
        """Headway in minutes between two boundary candidate stop sets
        (either possibly empty), or None for no service in the window.

        A trip qualifies if it visits at least one from-candidate and one
        to-candidate as distinct stop occurrences (presence-only at the
        union if the sets are equal or one is empty). Its entry departure
        is at its earliest qualifying occurrence. Departures are grouped
        by direction_id — grouping only, never mapped to edge orientation,
        since the min over groups is taken anyway — with observed stop
        order as the per-trip fallback; headway is the min over groups."""
        key = (route_id, from_stops, to_stops)
        if key in self._headway_memo:
            return self._headway_memo[key]

        degenerate = from_stops == to_stops or not from_stops \
            or not to_stops
        anchor = from_stops | to_stops
        groups = {}

        for tid in self.route_trips.get(route_id, ()):
            ts = self.trip_stops.get(tid)
            if ts is None:
                continue
            gkey = self.trip_dir.get(tid)
            if degenerate:
                occs = sorted(o for s in anchor for o in ts.get(s, ()))
                if not occs:
                    continue
                dep = occs[0][1]
                if gkey is None:
                    gkey = "_any"
            else:
                fr = sorted(o for s in from_stops for o in ts.get(s, ()))
                to = sorted(o for s in to_stops for o in ts.get(s, ()))
                if not fr or not to:
                    continue
                # reject a trip whose only qualifying pair is one identical
                # visit to a stop the two sets share
                if len(fr) == 1 and len(to) == 1 and fr[0] == to[0]:
                    continue
                dep = min(fr[0], to[0])[1]
                if gkey is None:
                    gkey = "_fwd" if fr[0][0] < to[-1][0] else "_rev"
            groups.setdefault(gkey, []).append(dep)
            self._mark_qualified(route_id, tid)

        hs = [h for h in (self._group_headway_min(deps)
                          for deps in groups.values())
              if h is not None]
        res = min(hs) if hs else None
        self._headway_memo[key] = res
        return res


def geom_length_m(coords):
    return sum(haversine_m(a[1], a[0], b[1], b[0])
               for a, b in zip(coords, coords[1:]))


def build_spans(route_id, items, node_map, az):
    """Group a route's (edge, line_entry) items into stop-to-stop spans.

    A span is a maximal run of the route's edges bounded by nodes that
    stand for stops of the route, by branch nodes of the route's subgraph
    (degree != 2), or by self-loop edges. Interior nodes — junctions and
    other routes' stops — are walked through, so a route's bucket can only
    change where it actually stops or where variants diverge.

    The boundary test is strict: a node stands for a stop only if its
    station_id belongs to the route, or it is the NEAREST of the route's
    nodes to one of its stops (within FALLBACK_MAX_DIST_M). Being merely
    within the fallback radius of some stop is not enough — in dense
    networks nearly every junction node is, and using that as the boundary
    test chops interstations into micro-spans whose jittery headways can
    straddle a bucket edge mid-segment. The generous radius remains right
    for qualification (see span_sides).

    Returns a list of dicts: {"items", "ends" (two node ids), "sides"
    (two frozensets of qualification stop_ids), "length_m"}.
    """
    adj = {}
    for it in items:
        p = it[0]["properties"]
        for nid in (p.get("from"), p.get("to")):
            adj.setdefault(nid, []).append(it)

    stops_of_route = az.route_stops.get(route_id, {})

    node_pos = {}
    stop_nodes = set()
    for nid in adj:
        node = node_map.get(nid)
        if node is None:
            continue
        lon, lat = node["geometry"]["coordinates"]
        node_pos[nid] = (lat, lon)
        sid = node["properties"].get("station_id", "")
        if sid and sid in stops_of_route:
            stop_nodes.add(nid)

    cell = 0.003  # ~330 m grid for the nearest-node search
    grid = {}
    for nid, (lat, lon) in node_pos.items():
        grid.setdefault((math.floor(lat / cell), math.floor(lon / cell)),
                        []).append(nid)
    for sid, (slat, slon) in stops_of_route.items():
        ci, cj = math.floor(slat / cell), math.floor(slon / cell)
        best, bd = None, FALLBACK_MAX_DIST_M
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for nid in grid.get((ci + di, cj + dj), ()):
                    nlat, nlon = node_pos[nid]
                    d = haversine_m(slat, slon, nlat, nlon)
                    if d < bd:
                        best, bd = nid, d
        if best is not None:
            stop_nodes.add(best)

    def is_boundary(nid):
        if nid is None or nid not in node_map:
            return True
        if len(adj.get(nid, ())) != 2:
            return True
        return nid in stop_nodes

    spans, assigned = [], set()
    for it in items:
        if id(it[1]) in assigned:
            continue
        span_items = [it]
        assigned.add(id(it[1]))
        ends = []
        for side in (0, 1):
            p = it[0]["properties"]
            nid = p.get("from") if side == 0 else p.get("to")
            if p.get("from") == p.get("to"):  # self-loop edge
                ends.append(nid)
                continue
            while not is_boundary(nid):
                nxt = [x for x in adj[nid] if id(x[1]) not in assigned]
                if not nxt:
                    break  # closed ring: back at an already-assigned edge
                x = nxt[0]
                assigned.add(id(x[1]))
                span_items.append(x)
                q = x[0]["properties"]
                if q.get("from") == q.get("to"):
                    break
                nid = q["to"] if q["from"] == nid else q["from"]
            ends.append(nid)
        length = sum(geom_length_m(e["geometry"]["coordinates"])
                     for e, _ in span_items)
        spans.append({"items": span_items, "ends": ends,
                      "length_m": length})
    for sp in spans:
        sp["sides"] = span_sides(route_id, sp, adj, node_map, az)
    return spans


def span_sides(route_id, sp, adj, node_map, az):
    """The two qualification stop sets of a span: each end's own candidates
    plus, walking outward along the route's subgraph (away from the span,
    down every branch, never through the span's other end), the first stop
    cluster beyond it.

    A trip visiting both sides has traversed the span. The plain per-node
    candidate sets are not enough for that test: where the two directions
    stop at different curbs or offset locations, each end resolves to one
    direction's stop only and no single trip visits both ("Sibley &
    Greenwood Rd" eastbound vs "Sibley & Greenwood Ave" westbound); and a
    median split or expressway ramp bounds spans with stop-less branch
    nodes that resolve to nothing at all. Walking out to the neighboring
    stops makes both cases qualify exactly the trips that ride across.
    Trips that terminate at a boundary stop still fail the far side."""
    span_ids = {id(it[1]) for it in sp["items"]}
    ends = sp["ends"]
    sides = []
    for i, end in enumerate(ends):
        other = ends[1 - i] if ends[0] != ends[1] else None
        node = node_map.get(end)
        base = az.resolve_stops(node, route_id) if node else frozenset()
        collected = set(base)
        seen = {end}
        queue = [end]
        while queue:
            nid = queue.pop()
            for it in adj.get(nid, ()):
                if id(it[1]) in span_ids:
                    continue
                p = it[0]["properties"]
                nxt = p["to"] if p["from"] == nid else p["from"]
                if nxt in seen or nxt == other:
                    continue
                seen.add(nxt)
                nnode = node_map.get(nxt)
                if nnode is None:
                    continue
                cands = az.resolve_stops(nnode, route_id)
                if cands - base:
                    collected |= cands  # next stop cluster; end this path
                else:
                    queue.append(nxt)
        sides.append(frozenset(collected))
    return sides


def smooth_route(spans, cfg, stats):
    """Optional polish on one route's spans, in place.

    1. fill_unknown: spans with no resolvable boundary on either end
       inherit a neighbor's bucket (the slower one if neighbors disagree).
    2. Runs of equal-bucket spans shorter than min_run_m are collapsed
       into the surrounding bucket when the runs on both sides agree.
    Neighbor relations only cross boundary nodes shared by exactly two
    spans, so branch points always break runs.
    """
    node_spans = {}
    for sp in spans:
        for nid in sp["ends"]:
            node_spans.setdefault(nid, []).append(sp)

    def neighbors(sp):
        res = []
        for nid in sp["ends"]:
            lst = node_spans.get(nid, ())
            if len(lst) == 2:
                other = lst[0] if lst[1] is sp else lst[1]
                if other is not sp:
                    res.append(other)
        return res

    def rank(b):  # slowness; no_service is slowest
        return math.inf if b == NO_SERVICE else b

    if cfg.smoothing_fill_unknown:
        # unknown spans usually sit between branch nodes, so inherit across
        # any shared end node, not just the degree-2 relation runs use
        changed = True
        while changed:
            changed = False
            for sp in spans:
                if sp["bucket"] != UNKNOWN:
                    continue
                nb = [n["bucket"] for nid in sp["ends"]
                      for n in node_spans.get(nid, ())
                      if n is not sp and n["bucket"] != UNKNOWN]
                if not nb:
                    continue
                sp["bucket"] = max(nb, key=rank)
                stats["filled"] += 1
                changed = True
    for sp in spans:
        if sp["bucket"] == UNKNOWN:
            sp["bucket"] = NO_SERVICE
            stats["unknown_left"] += 1

    for _ in range(10):  # short runs can merge; iterate to a fixpoint
        # partition into runs of equal-bucket spans
        run_of = {}
        runs = []
        for sp in spans:
            if id(sp) in run_of:
                continue
            run = [sp]
            run_of[id(sp)] = run
            queue = [sp]
            while queue:
                cur = queue.pop()
                for n in neighbors(cur):
                    if id(n) not in run_of and n["bucket"] == cur["bucket"]:
                        run_of[id(n)] = run
                        run.append(n)
                        queue.append(n)
            runs.append(run)

        changed = False
        for run in runs:
            length = sum(sp["length_m"] for sp in run)
            if length >= cfg.smoothing_min_run_m:
                continue
            nb_buckets = {n["bucket"] for sp in run for n in neighbors(sp)
                          if run_of.get(id(n)) is not run}
            n_nb = sum(1 for sp in run for n in neighbors(sp)
                       if run_of.get(id(n)) is not run)
            if n_nb >= 2 and len(nb_buckets) == 1:
                new = nb_buckets.pop()
                if new != run[0]["bucket"]:
                    for sp in run:
                        sp["bucket"] = new
                    stats["collapsed_runs"] += 1
                    stats["collapsed_spans"] += len(run)
                    changed = True
        if not changed:
            break


def main():
    ap = argparse.ArgumentParser(
        description="Per-segment frequency coloring for LOOM graphs")
    ap.add_argument("graph_file",
                    help="topo/loom output GeoJSON ('-' for stdin)")
    ap.add_argument("--config", required=True, help="path to freq.toml")
    ap.add_argument("--gtfs", required=True,
                    help="path to an extracted GTFS directory")
    args = ap.parse_args()

    cfg = Config(args.config)

    if args.graph_file == "-":
        gj = json.load(sys.stdin)
    else:
        with open(args.graph_file) as f:
            gj = json.load(f)

    features = gj["features"]
    node_map = {f["properties"]["id"]: f for f in features
                if f["geometry"]["type"] == "Point"}
    edges = [f for f in features if f["geometry"]["type"] == "LineString"]

    trip_stops, route_stops, route_trips, bus_route_ids, trip_dir = \
        load_gtfs(args.gtfs, cfg)
    az = FreqAnalyzer(cfg, trip_stops, route_stops, route_trips,
                      bus_route_ids, trip_dir)

    # pass 1: group line entries by route, walk each route's edges into
    # stop-to-stop spans, bucket per span
    route_entries = {}
    for edge in edges:
        for entry in edge["properties"].get("lines", []):
            route_entries.setdefault(entry["id"], []).append((edge, entry))

    entry_bucket = {}  # id(entry) -> final bucket idx
    route_best = {}    # route_id -> best (lowest) final bucket idx
    n_spans = n_degenerate = n_unknown = 0
    end_stats = {}     # (node_id, route_id) -> "ok" | "fallback" | "none"
    smooth_stats = {"filled": 0, "unknown_left": 0,
                    "collapsed_runs": 0, "collapsed_spans": 0}

    for rid, items in route_entries.items():
        if rid not in bus_route_ids or rid not in route_trips:
            for _, entry in items:
                entry_bucket[id(entry)] = NO_SERVICE
            continue
        spans = build_spans(rid, items, node_map, az)
        n_spans += len(spans)
        for sp in spans:
            for nid in sp["ends"]:
                node = node_map.get(nid)
                if node is None:
                    continue
                cands = az.resolve_stops(node, rid)
                sid = node["properties"].get("station_id", "")
                end_stats[(nid, rid)] = (
                    "none" if not cands
                    else "ok" if sid in route_stops.get(rid, {})
                    else "fallback")
            fs, ts = sp["sides"]
            if not fs and not ts:
                n_unknown += 1
                log(f"freq.py: route {rid}: span of {len(sp['items'])} "
                    "edge(s) found no stops even walking outward; "
                    + ("deferring to smoothing"
                       if cfg.smoothing_enabled and cfg.smoothing_fill_unknown
                       else "assigning no_service"))
                sp["bucket"] = UNKNOWN
                continue
            if fs == ts or not fs or not ts:
                n_degenerate += 1
            h = az.headway(rid, fs, ts)
            sp["bucket"] = cfg.bucket_for(h) if h is not None else NO_SERVICE
        if cfg.smoothing_enabled:
            smooth_route(spans, cfg, smooth_stats)
        else:
            for sp in spans:
                if sp["bucket"] == UNKNOWN:
                    sp["bucket"] = NO_SERVICE
        for sp in spans:
            b = sp["bucket"]
            for _, entry in sp["items"]:
                entry_bucket[id(entry)] = b
            if b != NO_SERVICE and (rid not in route_best
                                    or b < route_best[rid]):
                route_best[rid] = b

    bucket_counts = {}
    for b in entry_bucket.values():
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    # pass 2: write colors / collect removals
    removed_entries = {}  # id(edge) -> set of entry ids to drop
    for edge in edges:
        for entry in edge["properties"].get("lines", []):
            idx = entry_bucket[id(entry)]
            seg_color = cfg.bucket_color(idx)
            if seg_color is None:
                removed_entries.setdefault(id(edge), set()).add(id(entry))
                continue
            rid = entry["id"]
            if rid in route_best:
                global_color = cfg.bucket_color(route_best[rid])
            else:
                global_color = cfg.no_service_color
            # an invisible best bucket with a visible segment bucket is a
            # pathological config; fall back to the segment color
            entry["color"] = global_color if global_color is not None \
                else seg_color
            entry["freq_color"] = seg_color

    # cleanup: drop removed entries, empty edges, orphan nodes, stale refs
    kept_features = []
    kept_line_ids = set()
    kept_node_ids = set()
    n_removed_entries = n_removed_edges = 0
    for f in features:
        if f["geometry"]["type"] != "LineString":
            continue
        props = f["properties"]
        dropped = removed_entries.get(id(f), ())
        if dropped:
            n_removed_entries += len(dropped)
            props["lines"] = [e for e in props["lines"]
                              if id(e) not in dropped]
        if not props.get("lines"):
            n_removed_edges += 1
            continue
        kept_line_ids.update(e["id"] for e in props["lines"])
        kept_node_ids.add(props.get("from"))
        kept_node_ids.add(props.get("to"))
        kept_features.append(f)

    n_removed_nodes = 0
    node_features = []
    for f in features:
        if f["geometry"]["type"] != "Point":
            continue
        props = f["properties"]
        if props.get("id") not in kept_node_ids:
            n_removed_nodes += 1
            continue
        if "not_serving" in props:
            props["not_serving"] = [lid for lid in props["not_serving"]
                                    if lid in kept_line_ids]
            if not props["not_serving"]:
                del props["not_serving"]
        if "excluded_conn" in props:
            props["excluded_conn"] = [
                c for c in props["excluded_conn"]
                if c.get("line") in kept_line_ids
                and c.get("node_from") in kept_node_ids
                and c.get("node_to") in kept_node_ids]
            if not props["excluded_conn"]:
                del props["excluded_conn"]
        node_features.append(f)

    # preserve original feature order (minus removals)
    kept = {id(f) for f in kept_features} | {id(f) for f in node_features}
    gj["features"] = [f for f in features
                      if id(f) in kept or f["geometry"]["type"] not in
                      ("Point", "LineString")]

    json.dump(gj, sys.stdout)

    # summary
    log("")
    for i in range(len(cfg.buckets)):
        vis = "" if cfg.bucket_color(i) is not None else " (invisible)"
        log(f"Bucket {cfg.bucket_name(i)}:{vis} "
            f"{bucket_counts.get(i, 0)} route-segments")
    vis = "" if cfg.no_service_color is not None else " (invisible)"
    log(f"No service bucket:{vis} "
        f"{bucket_counts.get(NO_SERVICE, 0)} route-segments")
    n_fallback = sum(1 for v in end_stats.values() if v == "fallback")
    n_unresolved = sum(1 for v in end_stats.values() if v == "none")
    log(f"Spans: {n_spans} across {len(route_entries)} routes")
    log(f"Span boundaries resolved geographically (invalid/missing "
        f"station_id): {n_fallback}")
    log(f"Span boundaries with no stop within {FALLBACK_MAX_DIST_M:.0f} m: "
        f"{n_unresolved}")
    log(f"Degenerate (equal/one-sided boundary) spans: {n_degenerate}")
    log(f"Spans with no resolvable boundary at all: {n_unknown}")
    if cfg.smoothing_enabled:
        log(f"Smoothing: filled {smooth_stats['filled']} unknown spans "
            f"({smooth_stats['unknown_left']} left as no_service), "
            f"collapsed {smooth_stats['collapsed_runs']} short runs "
            f"({smooth_stats['collapsed_spans']} spans)")
    log(f"Removed: {n_removed_entries} line entries, {n_removed_edges} "
        f"edges, {n_removed_nodes} orphan nodes")
    for rid in sorted(route_trips):
        active = len(route_trips[rid])
        qual = len(az.route_qualified.get(rid, ()))
        if qual < active and qual > 0:
            log(f"Route {rid}: {active} active trips, {qual} qualified "
                f"somewhere, {active - qual} never qualified")


if __name__ == "__main__":
    main()

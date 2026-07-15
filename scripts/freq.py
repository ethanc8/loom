#!/usr/bin/env python3
"""freq.py — per-segment frequency coloring for the LOOM pipeline.

Reads a topo (or loom) GeoJSON graph, computes each route's service headway
on each edge within a configured time window from GTFS, assigns every
(edge, route) pair a frequency bucket, and writes the modified GeoJSON to
stdout. On every line entry it sets:

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
import statistics
import sys
import tomllib
from datetime import date as _date, datetime

import pandas as pd

NO_SERVICE = -1  # sentinel bucket index

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
        if self.metric not in ("max", "mean", "median"):
            sys.exit(f"freq.py: unknown metric type '{self.metric}'")

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
                            ["trip_id", "route_id", "service_id"])
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

    return trip_stops, route_stops, route_trips, bus_route_ids


class FreqAnalyzer:
    def __init__(self, cfg, trip_stops, route_stops, route_trips,
                 bus_route_ids):
        self.cfg = cfg
        self.trip_stops = trip_stops
        self.route_stops = route_stops
        self.route_trips = route_trips
        self.bus_route_ids = bus_route_ids

        self._resolve_memo = {}
        self._headway_memo = {}

        self.n_fallback = 0
        self.n_unresolved = 0
        self.n_degenerate = 0
        self.route_qualified = {}  # route_id -> set of trip_ids

    def resolve_stops(self, node, route_id):
        """Candidate stop_ids of `node` on `route_id` as a (possibly empty)
        frozenset: the node's station_id if it belongs to the route, plus
        every stop of the route within FALLBACK_MAX_DIST_M. A set, not a
        single stop: the two directions of a street use distinct GTFS stops
        a few metres apart, and a topo node stands for the whole street
        corner — resolving to one curb arbitrarily means no single trip
        visits both endpoints of an edge whose nodes picked opposite curbs.
        Memoized."""
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

        if not cands:
            self.n_unresolved += 1
            log(f"freq.py: node {node_id} (station_id '{sid}'): no stop of "
                f"route {route_id} within {FALLBACK_MAX_DIST_M:.0f} m")
        elif not (sid and sid in stops_of_route):
            self.n_fallback += 1
            log(f"freq.py: node {node_id} (station_id '{sid}') resolved "
                f"geographically to {len(cands)} stop(s) of route "
                f"{route_id}")

        res = frozenset(cands)
        self._resolve_memo[key] = res
        return res

    def _mark_qualified(self, route_id, trip_id):
        self.route_qualified.setdefault(route_id, set()).add(trip_id)

    def _group_headway_min(self, deps):
        """Headway in minutes from a group's entry departures, or None."""
        cfg = self.cfg
        deps = sorted(d for d in deps
                      if cfg.window_start <= d < cfg.window_end)
        if not deps:
            return None
        if len(deps) == 1:
            return (cfg.window_end - cfg.window_start) / 60.0
        gaps = [b - a for a, b in zip(deps, deps[1:])]
        if cfg.metric == "max":
            return max(gaps) / 60.0
        if cfg.metric == "mean":
            return (sum(gaps) / len(gaps)) / 60.0
        return statistics.median(gaps) / 60.0

    def headway(self, route_id, from_stops, to_stops):
        """Headway in minutes for a route between two resolved candidate
        stop sets (either possibly empty), or None for no service in the
        window."""
        key = (route_id, from_stops, to_stops)
        if key in self._headway_memo:
            return self._headway_memo[key]

        fwd, rev = [], []
        degenerate = from_stops == to_stops or not from_stops \
            or not to_stops
        anchor = from_stops | to_stops

        for tid in self.route_trips.get(route_id, ()):
            ts = self.trip_stops.get(tid)
            if ts is None:
                continue
            if degenerate:
                # presence-only: every trip serving any candidate qualifies
                occs = sorted(o for s in anchor for o in ts.get(s, ()))
                if occs:
                    fwd.append(occs[0][1])
                    self._mark_qualified(route_id, tid)
                continue
            fr = sorted(o for s in from_stops for o in ts.get(s, ()))
            to = sorted(o for s in to_stops for o in ts.get(s, ()))
            if not fr or not to:
                continue
            # trip qualifies forward if some from-occurrence precedes some
            # to-occurrence; entry departure at the earliest such occurrence
            if fr[0][0] < to[-1][0]:
                fwd.append(fr[0][1])
                self._mark_qualified(route_id, tid)
            if to[0][0] < fr[-1][0]:
                rev.append(to[0][1])
                self._mark_qualified(route_id, tid)

        headways = [h for h in (self._group_headway_min(fwd),
                                self._group_headway_min(rev))
                    if h is not None]
        res = min(headways) if headways else None
        self._headway_memo[key] = res
        return res

    def classify(self, edge, entry, node_map):
        """Bucket index for one line entry on one edge; None headway maps
        to NO_SERVICE. Returns (bucket_idx, headway_min_or_None)."""
        route_id = entry["id"]
        if route_id not in self.bus_route_ids \
                or route_id not in self.route_trips:
            return NO_SERVICE, None

        props = edge["properties"]
        from_node = node_map.get(props.get("from"))
        to_node = node_map.get(props.get("to"))
        from_stops = (self.resolve_stops(from_node, route_id)
                      if from_node else frozenset())
        to_stops = (self.resolve_stops(to_node, route_id)
                    if to_node else frozenset())

        if not from_stops and not to_stops:
            log(f"freq.py: edge {props.get('id')}: neither endpoint resolved "
                f"for route {route_id}; assigning no_service")
            return NO_SERVICE, None

        if from_stops == to_stops or not from_stops or not to_stops:
            self.n_degenerate += 1
            log(f"freq.py: edge {props.get('id')}: degenerate endpoints for "
                f"route {route_id} ({len(from_stops)} from-candidates, "
                f"{len(to_stops)} to-candidates); using presence-only "
                "qualification")

        h = self.headway(route_id, from_stops, to_stops)
        if h is None:
            return NO_SERVICE, None
        return self.cfg.bucket_for(h), h


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

    trip_stops, route_stops, route_trips, bus_route_ids = \
        load_gtfs(args.gtfs, cfg)
    az = FreqAnalyzer(cfg, trip_stops, route_stops, route_trips,
                      bus_route_ids)

    # pass 1: bucket every (edge, line entry); track per-route best headway
    assignments = []  # (edge, entry, bucket_idx) in edge order
    route_best = {}   # route_id -> min headway over all its segments
    bucket_counts = {}
    for edge in edges:
        for entry in edge["properties"].get("lines", []):
            idx, h = az.classify(edge, entry, node_map)
            assignments.append((edge, entry, idx))
            bucket_counts[idx] = bucket_counts.get(idx, 0) + 1
            if h is not None:
                rid = entry["id"]
                if rid not in route_best or h < route_best[rid]:
                    route_best[rid] = h

    # pass 2: write colors / collect removals
    removed_entries = {}  # id(edge) -> set of entry ids to drop
    for edge, entry, idx in assignments:
        seg_color = cfg.bucket_color(idx)
        if seg_color is None:
            removed_entries.setdefault(id(edge), set()).add(id(entry))
            continue
        rid = entry["id"]
        if rid in route_best:
            global_color = cfg.bucket_color(cfg.bucket_for(route_best[rid]))
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
    log(f"Geographic fallback used: {az.n_fallback} node-endpoint "
        "resolutions")
    log(f"Degenerate (same-stop/one-stop) edges: {az.n_degenerate}")
    log(f"Unresolvable endpoints: {az.n_unresolved}")
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

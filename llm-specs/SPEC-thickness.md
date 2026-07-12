# Frequency-Based Line Thickness Specification

## Overview

Extend the frequency map (SPEC.md) so that each route segment's **line width** also reflects its frequency bucket, in addition to its color. More frequent service renders as thicker lines, following the Jarrett Walker / Human Transit convention.

This spec builds directly on SPEC.md and assumes it is fully implemented: the `colorOverride` plumbing, `freq.py`, and the junction-arc gradient. The `freq_width` field introduced here rides through the pipeline on exactly the same mechanism as `freq_color`.

## Goals

- Per-segment width, chosen per frequency bucket (a width multiplier next to each bucket's color in `freq.toml`)
- Correct line offsets within bundles of mixed-width lines (SVG and MVT)
- Correct junction arc geometry and width at nodes
- No changes to `gtfs2graph`, `topo`, or `loom`

## Non-goals

- Width-aware loom optimization (crossing penalties weighted by width; wide lines pulled toward bundle centers). loom keeps its uniform-width cost model; wide lines may occasionally cross where a width-aware optimizer would avoid it. Specified as Future Extension B.
- Tapered width transitions at junctions (v1 draws arcs at the thinner of the two widths). Specified as Future Extension A.
- Continuous width as a function of headway. Width is discrete per-bucket, matching the color legend; near-equal frequencies would be visually indistinguishable anyway.

---

## Configuration

Each bucket (and `no_service`, if visible) gains a `width` key: a multiplier of transitmap's base line width (`-w` flag, default 20). `1.0` = base width. Omitted → `1.0`.

```toml
[[buckets]]
max_headway_min = 10
color = "d32f2f"
width = 2.0

[[buckets]]
max_headway_min = 15
color = "f57c00"
width = 1.5

[[buckets]]
max_headway_min = 30
color = "388e3c"
width = 1.0

[[buckets]]
max_headway_min = "inf"
color = "9e9e9e"
width = 0.5
```

Multipliers are relative so that changing transitmap's `-w` scales the whole map uniformly. Keep multipliers within roughly [0.4, 2.5]; below ~0.4 the black outline (constant width, see below) visually swallows the fill.

**Line spacing and outline width stay constant** (not scaled per line). Scaling spacing per-line would make bundle gaps irregular; scaling outlines makes thin lines disappear. Only the fill width varies.

---

## freq.py changes

Step 7 additionally sets `freq_width` on each line entry, from the same bucket lookup that produces `freq_color`:

```json
{
  "id":         "cta_8",
  "label":      "8",
  "color":      "f57c00",
  "freq_color": "9e9e9e",
  "freq_width": 0.5
}
```

Emit `freq_width` only when ≠ 1.0 (keeps the JSON small). No global-width analogue of the global `color` is needed — width has no shared-`Line`-object fallback path.

`freq_width` round-trips through loom for the same reason `freq_color` does: the read and write live in `shared/linegraph`, which loom uses. Both pipeline orders from SPEC.md remain valid.

---

## C++ changes

### 1. `shared/linegraph` — plumbing (mirrors `colorOverride`)

`LineEdgePL.h`, `LineOcc`:

```cpp
double widthMul = 1.0;  // per-edge width multiplier set by freq.py
```

`LineEdgePL.cpp`:
- `getAttrs()`: emit `line["freq_width"] = r.widthMul;` when `r.widthMul != 1.0`.
- New `setWidthMul(const Line* l, double w)`, same shape as `setColorOverride`.

`LineGraph.cpp`, `extractLine()`: after the existing `addLine`/`setColorOverride` calls:

```cpp
if (line.count("freq_width"))
  e->pl().setWidthMul(l, line.at("freq_width").get<double>());
```

### 2. `shared/rendergraph/RenderGraph` — offset math

This is the core of the change. All slot-position math currently assumes a uniform step `w + 2·ow + spacing`; it must become prefix sums over per-slot widths.

Define, for edge `e` with lines at positions `0..N−1`:

```
slotW(e, i)     = getWidth(e) · widthMul_i + 2 · getOutlineWidth(e)
offsetCenter(e, i) = Σ_{j<i} (slotW(e, j) + getSpacing(e)) + slotW(e, i) / 2
totalWidth(e)   = Σ_i slotW(e, i) + getSpacing(e) · (N − 1)
```

Concrete changes:

- **`getTotalWidth(e)`** (RenderGraph.cpp:453): currently `(2·ow + w)·N + spacing·(N−1)`. Replace with the sum above. All node-front sizing (`getMaxNdFrontWidth`, front expansion, station hull sizing) picks up variable widths automatically through this function.
- **New `offsetCenter(e, pos)`** helper implementing the prefix sum. O(N) per call is fine (bundles are small); memoization is unnecessary.
- **`linePosOn(nf, e, pos, inv, origG)`** (RenderGraph.cpp:531): replace the uniform-step expressions:
  - non-inverted: `p = offsetCenter(e, pos)`
  - inverted: `p = getTotalWidth(e) − offsetCenter(e, pos)`

  (The current inverted branch `step·(N−1−pos) + slotW/2` is only equivalent to this under uniform widths.)

  Both `linePosOn` overloads and everything downstream of them — inner line/bezier construction (`getInnerLine`, `getInnerBezier`), terminus arcs, and individual stop polygons (RenderGraph.cpp:400) — are covered by this one fix.
- **New per-slot width accessor** for the renderers, e.g. `double lineWidthAt(e, pos)` returning `getWidth(e) · widthMul` (fill width, without outline).

### 3. `transitmap/output/SvgRenderer.cpp`

**`renderEdgeTripGeom`** (line ~504): replace the decrementing-uniform-offset loop. Currently:

```cpp
double offsetStep = lineW + 2.0 * outlineW + lineSpc;
double o = oo;                                   // oo = getTotalWidth(e)
...
double offset = -(o - oo / 2.0 - (2.0 * outlineW + lineW) / 2.0);
...
o -= offsetStep;
```

Under uniform widths this equals `offset_i = offsetCenter(e,i) − totalWidth/2` (up to the perpendicular sign convention). Generalize to exactly that:

```cpp
double offset = outG.offsetCenter(e, i) - oo / 2.0;   // adjust sign to match current convention
double lineWi = outG.lineWidthAt(e, i);               // lineW * widthMul
```

and pass `lineWi` everywhere `lineW` is used per line:
- `renderLinePart(p, lineWi, ...)` — the width parameter already exists; the outline (`width + _cfg->outlineWidth`) scales correctly for free.
- Direction markers: `getMarkerPathMale(lineWi)`, `EndMarker(..., lineWi, lineWi)`, and `arrowLength = lineWi * 2.5`.

**`renderClique`** (line ~380): two fixes.

1. **Stroke width**: per the design decision, an arc is stroked at the **thinner** of its two sides:
   ```cpp
   double wFrom = lineWidthAt(from.edge, from-slot);
   double wTo   = to.edge ? lineWidthAt(to.edge, to-slot) : wFrom;  // terminus arcs: one side
   double arcW  = std::min(wFrom, wTo);
   ```
   Use `arcW` in both the fill stroke-width and the outline stroke-width (`arcW + outlineWidth`). This composes with the color gradient from SPEC.md: an arc at a short-turn terminus is both thinner-of-the-two and color-blended.

2. **The parallel-offset trick** (line ~392): arcs in a clique are drawn by offsetting the longest ("ref") arc by `uniformStep × slotDifference`. With variable widths the spacing between slots is not uniform; replace with prefix-sum differences on the ref edge:
   ```cpp
   double off = -(offsetCenter(ref.from.edge, c.geoms[i].slotFrom)
                - offsetCenter(ref.from.edge, ref.slotFrom));
   ```
   (keep the existing sign flip for `ref.from.edge->getTo() == n`). The `ref.geom.getLength() > uniformStep * 4` length guard can use the average slot step `totalWidth(e)/N` instead.

### 4. `transitmap/output/MvtRenderer.cpp`

- **`renderEdgeTripGeom`** (line ~396): same offset generalization as SVG, scaled by `_res`: `offset_i = (offsetCenter(e,i) − totalWidth/2) · _res`.
- **Width styling**: MVT features carry no geometry width — the consumer's MapLibre style sets `line-width`. Add the multiplier as a feature attribute on both the fill and outline features:
  ```cpp
  params["width"] = util::toString(lo.widthMul);
  ```
  and document that the style should use a data-driven expression, e.g.
  ```json
  "line-width": ["*", baseWidthForZoom, ["to-number", ["get", "width", 1.0]]]
  ```
- Inner/junction features get `params["width"]` = the min-side multiplier, matching the SVG rule.

---

## Crossing z-order (config option)

### Current behavior

Which line paints on top at a junction-arc crossing is currently an accident of memory layout. In `SvgRenderer::renderDelegates` (SvgRenderer.cpp:587), junction arcs are grouped per clique into a `std::map<uintptr_t, …>` keyed by the `Line` object's **pointer address** (SvgRenderer.cpp:443); iteration order = allocation order = the order routes first appear in the input JSON. Per line, all black outlines are drawn before all fills — this makes same-line arcs merge seamlessly while a later-drawn line's outline+fill produces the cased "over" look across an earlier line. loom minimizes how *many* crossings exist but has no opinion on their stacking.

With uniform widths this barely matters (cased crossings are visually symmetric). With variable widths it does: whichever direction is chosen, the choice is a real cartographic trade-off —

- **thick over thin**: the frequent network reads as continuous (Jarrett Walker convention); a thin line painted over a trunk would cut a black-cased notch through it.
- **thin over thick**: infrequent lines stay traceable through junctions instead of disappearing under the trunk.

### New transitmap flag

```
--crossing-order = thick-over-thin | thin-over-thick    (default: thick-over-thin)
```

A rendering-only policy, deliberately **not** in `freq.toml`: changing it reruns only transitmap, not freq.py or loom.

### Implementation

- **SVG** (`renderDelegates`): within each inner-delegate clique, collect the per-line buckets into a vector and sort by the line's width in that clique (max `widthMul` over the line's arcs in the clique, since a line's width can differ per arc), ascending for thick-over-thin (thick painted last = on top), descending for thin-over-thick. Tie-break by pointer, preserving determinism. The per-line outlines-then-fills structure is untouched.
- **MVT**: MVT renderers paint features within a layer in order, so sort the inner-connection features of each layer by the same key before serializing.
- Edge strokes (drawn before all arcs) keep their current order — parallel strokes within an edge never overlap, and cross-edge stroke overlaps are cropped by node fronts.

### Regression / testing

With no `freq_width` in the input, all widths tie and the pointer tie-break reproduces today's output byte-identically under either flag value. Add that as a test alongside the offset-math regression.

### Future work

Smarter per-crossing heuristics (e.g., deciding each crossing individually to minimize perceived breaks, angle-aware choices, or letting near-equal widths alternate) are explicitly deferred until the two global policies prove insufficient in practice.

---

## Interaction with the junction gradient (SPEC.md)

The gradient and the width rule are independent per-arc properties computed from the same two `LineOcc`s (from-side and to-side): color blends, width takes the minimum. Implement the gradient first (it's in SPEC.md's scope); the width rule then only touches the stroke-width expressions in the same code paths.

## What needs no changes

- **loom / topo / gtfs2graph** — untouched; `freq_width` passes through loom via shared code.
- **Node front expansion & station hulls** — driven by `getTotalWidth`, correct automatically. (`getStopGeoms`'s padding `d` uses `_defWidth`; station bubbles keep base-width padding, which is fine.)
- **Offset computation in transitmap's graph smoothing** — operates on edge geometry before per-line offsetting.

## Testing

1. **Synthetic graph**: hand-write a small GeoJSON with one edge carrying three lines with `freq_width` 0.5 / 1.0 / 2.0. Verify in the SVG: no overlaps, no gaps beyond `lineSpacing`, bundle centered on the edge geometry, stroke-widths equal to `-w × mul`.
2. **Junction**: extend the synthetic graph with a 3-edge node where one line's width changes across the node; verify arcs are stroked at the thinner width and endpoints land exactly on the (prefix-sum) slot centers of both fronts — any drift here means `linePosOn` and `renderEdgeTripGeom` disagree on offsets.
3. **Regression**: run on a graph with **no** `freq_width` fields and diff the SVG against the pre-change output — with all multipliers at 1.0, every formula must reduce to the current uniform math (byte-identical output modulo float formatting).
4. **Chicagoland**: full pipeline; check short-turn corridors (thicker inner segment) and a Pace corridor next to a CTA corridor.

---

## Future Extension A: Tapered width transitions at junctions

Replace the thinner-of-the-two rule with a wedge whose width interpolates linearly from `wFrom` to `wTo` along the arc. SVG strokes cannot vary in width along a path, so a tapering arc must become a **filled polygon**.

### A.1 Geometry construction

New helper (suggested home: a free function next to `RenderGraph`, since both renderers need it):

```cpp
util::geo::Polygon<double> varWidthPolygon(const PolyLine<double>& arc,
                                           double wFrom, double wTo,
                                           size_t minSamples = 8);
```

1. **Sample** the (already front-cropped) arc at `S` points at equal arc-length steps, with `S = max(minSamples, ceil(len / (min(wFrom, wTo) / 2)))` — sampling density tied to the narrower width keeps curvature artifacts below half a line width.
2. **Tangent/normal** per sample: central differences on the sampled points (forward/backward differences at the ends); normal = tangent rotated 90°.
3. **Half-width** per sample: `h(t) = (wFrom + (wTo − wFrom) · t) / 2`, where `t` = cumulative arc-length fraction of the sample.
4. **Polygon**: `left_i = p_i + n_i · h_i`, `right_i = p_i − n_i · h_i`; ring = all `left` forward, then all `right` reversed, closed.
5. **Self-intersection guard**: where the local curvature radius is smaller than `h_i`, the inner offset folds over itself. Two-part mitigation:
   - after construction, drop any inner-side point whose segment direction reverses against its neighbors (a simple winding check pass);
   - for very short arcs (`len < wFrom + wTo`) skip the polygon entirely and fall back to the v1 constant stroke at `min(wFrom, wTo)` — tapering is invisible at that scale anyway.

The wedge's bases sit flush against the node fronts by construction: the arc endpoints are the `linePosOn` slot centers (already prefix-sum-correct after this spec), the base widths equal the adjacent edges' fill widths, and edge strokes are cropped to the same fronts.

### A.2 SVG rendering (`renderClique`)

- Emit `<path d="M … Z" />` with `fill` instead of stroke. Solid case: `fill:#hex;stroke:none`. Gradient case: the same `<linearGradient>` def as SPEC.md's junction gradient, applied as `fill:url(#id)` — `gradientUnits="userSpaceOnUse"` with the axis on the arc's chord works identically for fills.
- **Outline**: a second polygon underneath, built by the same helper with `wFrom + outlineWidth` / `wTo + outlineWidth` (the stroke equivalent of `width + _cfg->outlineWidth`), filled `#000000`. The existing `OutlinePrintPair` / `_innerDelegates` mechanism already renders outline delegates beneath fill delegates per line — reuse it unchanged; only the `PrintDelegate` payload switches from a stroked polyline to a filled ring.
- Only build polygons when `wFrom != wTo`; equal widths keep the cheaper stroked path (with gradient stroke if colors differ), so the uniform-width map is byte-identical to v1 output.

### A.3 MVT rendering

Recommended: **stepped approximation in the line layer**. Split the arc into `k = 4` equal sub-segments; sub-segment `j` gets `params["width"]` interpolated at its midpoint and (if colors differ) the color interpolated in RGB at its midpoint. This keeps everything in the existing line layers with the data-driven `line-width` expression from this spec — no new fill layer, no client-side style changes beyond what v1 thickness already requires. A true polygon fill layer is the higher-fidelity alternative but forces every style consumer to add a fill layer; not worth it at junction-arc scale.

### A.4 Testing

- Short-turn terminus fixture: verify wedge base widths equal the adjacent segments' stroke widths within float epsilon (any drift = disagreement between `varWidthPolygon` endpoints and `linePosOn`).
- Run on the full Chicagoland output and scan all generated rings for self-intersection (`util::geo` has polygon validity helpers); the guard in A.1.5 must leave zero invalid rings.

---

## Future Extension B: Width-aware loom optimization

Make loom's ordering cost account for visual weight: a crossing between two 2.0× lines is ~4× as disruptive as between two 1.0× lines.

### B.1 Where the costs live (verified against the code)

- All heuristic backends (`ExhaustiveOptimizer`, `GreedyOptimizer`, `HillClimbOptimizer`, `SimulatedAnnealingOptimizer`, `CombNoILPOptimizer`) score candidate orderings through **`OptGraphScorer`** (`src/loom/optim/OptGraphScorer.cpp`).
- The scorer counts crossings as **inversion counts**: `getNumCrossSeps` / `getNumCrossDiffSeg` build a `relOrderCross` vector of positions and return `util::inversions(relOrderCross)` (OptGraphScorer.cpp:224, :293). Pair identity is lost inside `util::inversions`, so per-pair weighting requires replacing the inversion count, not wrapping it.
- Node-level multipliers (`getCrossingPenSameSeg(n)`, `getCrossingPenDiffSeg(n)`, `getSeparationPen(n)`) are orthogonal and stay as they are.
- The **ILP backends** (`ILPOptimizer`, `ILPEdgeOrderOptimizer`) already have one decision variable per line pair; objective coefficients are set at ILPOptimizer.cpp:217/:334 and ILPEdgeOrderOptimizer.cpp:350/:497/:530 via `setObjCoef`. The ILP is the *easy* part: multiply each coefficient by the pair's width product.

### B.2 Implementation steps

1. **Plumb widths into the OptGraph.** loom already parses `freq_width` into `LineOcc::widthMul` (shared code, after this spec). When `OptGraph` is built from the `LineGraph`, copy `widthMul` onto the per-edge line entries (`OptLO` in `OptEdgePL`). OptGraph contractions merge edges that carry the same line with different per-segment multipliers; merge with **max** (visual weight is dominated by the widest part) and document this choice.
2. **Scorer: weighted inversions.** Change the crossing/separation count return types from `size_t` to `double`. In `getNumCrossSeps(n, ea, eb, c)` and `getNumCrossDiffSeg`, carry `(position, widthMul)` pairs in `relOrder*` instead of bare positions, and replace `util::inversions(v)` with a weighted variant: `Σ over inverted pairs (i, j) of w_i · w_j`. O(k²) pairwise is fine — bundles on this dataset are ≤ ~15 lines. Weight separations by the pair's product too, for consistency.
3. **Regression invariant**: with all multipliers at 1.0, every weighted sum equals the old integer count exactly (products of 1.0), so optimizer decisions on a no-`freq_width` graph must be bit-identical. Add a test asserting identical orderings on such a graph before/after.
4. **ILP**: multiply the crossing coefficients (ILPOptimizer.cpp:217/:334, ILPEdgeOrderOptimizer.cpp:350/:530) and separation coefficient (:497) by `w_A · w_B` for the variable's line pair. Note the LP objective becomes fractional — verify the solver interface accepts non-integer coefficients (it sets doubles already; the current values just happen to be integral).
5. **Centering objective (optional, separate flag)**: a secondary term pulling wide lines toward bundle centers, `centerPen · w_l · |pos_l − (N−1)/2|` summed per edge. Trivial in the scorer; in `ILPEdgeOrderOptimizer` it attaches as linear coefficients on the line-at-position assignment variables. Keep `centerPen` an order of magnitude below the same-segment crossing penalty so it only breaks ties and never buys a crossing.
6. **CLI**: gate everything behind a new loom flag (e.g. `--width-aware`, default off) so default behavior is untouched.

### B.3 Risk notes

- The contained changes are the scorer and the ILP coefficient sites (~2 files plus OptGraph plumbing). The risky surface is **incremental scoring** in HillClimb/SimulatedAnnealing, which re-score only the nodes affected by a swap — audit those paths for `size_t` accumulation of what are now doubles (silent truncation would corrupt hill-climbing decisions without crashing).
- Do this extension only if ghost crossings between wide lines prove visually annoying in practice; on a midday frequency map most bundles are width-homogeneous corridors, so the uniform model may well be good enough.

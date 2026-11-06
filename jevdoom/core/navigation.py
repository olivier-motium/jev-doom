"""Navigation over what the player has seen. Model selects destinations; code plans legal paths."""

import struct
import math
import heapq
from collections import defaultdict
from .geometry import dist
from shapely.geometry import LineString, Point, MultiPoint, box, GeometryCollection
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.prepared import prep

STEP = 24
PLAYER_RADIUS = 16


def read_wad(data, mapname):
    _, n, off = struct.unpack_from("<4sii", data)
    lumps = []
    for i in range(n):
        p, s, name = struct.unpack_from("<ii8s", data, off + i * 16)
        lumps.append((name.rstrip(b"\0").decode(), data[p : p + s]))
    i = next(i for i, x in enumerate(lumps) if x[0] == mapname.upper())
    ls = dict(lumps[i + 1 : i + 11])
    vertices = list(struct.iter_unpack("<hh", ls["VERTEXES"]))
    sides = list(struct.iter_unpack("<hh8s8s8sH", ls["SIDEDEFS"]))

    def clean(side):
        return [x.rstrip(b"\0").decode(errors="ignore") for x in side[2:5]]

    lines = []
    for j, (v1, v2, _flags, _special, _tag, right, left) in enumerate(
        struct.iter_unpack("<7H", ls["LINEDEFS"])
    ):
        lines.append(
            dict(
                id=f"line{j}",
                a=vertices[v1],
                b=vertices[v2],
                r=sides[right][-1] if right != 65535 else None,
                l=sides[left][-1] if left != 65535 else None,
                ftex=clean(sides[right]) if right != 65535 else [],
                btex=clean(sides[left]) if left != 65535 else [],
            )
        )
    return lines


class Navigator:
    def __init__(self, wad_bytes, mapname):
        # Discovery, walls, floor cells, heights and line identity come only from what the player has seen. The WAD answers two observed-point questions only: which sector a seen floor point lies in, and which wall face a view ray touched.
        self.observed_lines = {}
        self.observed_heights = {}
        self.frame_seen = set()
        self.frame_hits = set()
        self._nb_cache = {}
        self._edge_cache = {}
        self.door_push_started = {}
        self._reach = None
        self._geom_version = 0
        self._observed_union = GeometryCollection()
        self._observed_blocked = GeometryCollection()
        self._union_prep = prep(self._observed_union)
        self._blocked_prep = prep(self._observed_blocked)
        self.obs_cells = {}
        self._cell_sector_cache = {}
        self.lines = read_wad(wad_bytes, mapname)
        # Sector polygons answer which sector a seen floor point lies in; nothing else of the static map is used.
        grouped = defaultdict(list)
        for line in self.lines:
            for sec in {line["r"], line["l"]} - {None}:
                grouped[sec].append(LineString([line["a"], line["b"]]))
        polygons = []
        for s, segs in grouped.items():
            poly = None
            for p in polygonize(segs):
                poly = p if poly is None else poly.symmetric_difference(p)
            if poly is not None:
                polygons.append((s, poly))
        self.visits = defaultdict(int)
        self.seen = set()
        self.trail = []
        self.failed = defaultdict(int)
        self.used = defaultdict(int)
        self.cache = {}
        self.actor_blocks = GeometryCollection()
        self.blocked_cells = set()
        self.obstacle_signature = ()
        self.actor_bounds = None
        self._line_geoms = [LineString([line["a"], line["b"]]) for line in self.lines]
        self._line_tree = STRtree(self._line_geoms)
        self._poly_geoms = [poly for _, poly in polygons]
        self._poly_sectors = [s for s, _ in polygons]
        self._poly_tree = STRtree(self._poly_geoms)

    @property
    def walls(self):
        return self._observed_union

    def sector_of_point(self, x, y):
        """Sector containing an observed floor point (the only WAD lookup for floor); None when the point is not floor."""
        pt = Point(x, y)
        for i in self._poly_tree.query(pt):
            if self._poly_geoms[i].covers(pt):
                return self._poly_sectors[i]
        return None

    @staticmethod
    def face_textures(line, p):
        ax, ay = line["a"]
        bx, by = line["b"]
        cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
        return [
            n
            for n in (line.get("ftex", []) if cross <= 0 else line.get("btex", []))
            if n and n != "-"
        ]

    @staticmethod
    def face_texture_kind(line, p):
        """Classification from the texture of the face the player sees: the front face when the player stands on the right of the line, else the back face."""
        ax, ay = line["a"]
        bx, by = line["b"]
        cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
        names = [
            n
            for n in (line.get("ftex", []) if cross <= 0 else line.get("btex", []))
            if n and n != "-"
        ]
        if any(n.startswith(("SW1", "SW2")) for n in names):
            return "switch"
        if any("EXIT" in n for n in names):
            return "exit_sign"
        if any("DOOR" in n for n in names):
            return "door"
        if any(n.startswith("PLAT") for n in names):
            return "platform"
        return None

    def observe_view(self, p, facing, depth_columns, tick):
        """Observable discovery from the depth buffer: each column of the 90-degree view marks floor cells along its ray up to the measured wall (distance 7.1*depth+14.4 map units, calibrated) and records the touched wall only over the span the rays actually reached; nothing behind the camera or beyond a hit is discovered."""
        self.frame_seen = set()
        self.frame_hits = set()
        c = (round(p[0] / STEP), round(p[1] / STEP))
        new_cells = []
        changed_lines = set()
        if c not in self.obs_cells:
            sec = self.sector_of_point(p[0], p[1])
            if sec is not None:
                self.obs_cells[c] = sec
        if c in self.obs_cells:
            if c not in self.seen:
                new_cells.append(c)
            self.seen.add(c)
            self.frame_seen.add(c)
        self.visits[c] += 1
        if not self.trail or dist(self.trail[-1], p) > 20:
            self.trail.append(list(p))
        n = len(depth_columns)
        changed = False
        rays = []
        hit_positions = defaultdict(list)
        for i, dv in enumerate(depth_columns):
            # Depth calibration against a known face at 15 distances (v64): distance = 7.1*dv + 14.4 map units (residuals within 16 units from 24 to 448); a zero reads as a surface about 14 units away, not as nothing.
            off = 45 - 90 * (i + 0.5) / n
            ang = math.radians(facing + off)
            ray = (7.1 * float(dv) + 14.4) / max(0.2, math.cos(math.radians(off)))
            steps = int(max(0, (ray - 12) // 12))
            for k in range(1, steps + 1):
                d = k * 12
                q = (p[0] + d * math.cos(ang), p[1] + d * math.sin(ang))
                cell = (round(q[0] / STEP), round(q[1] / STEP))
                if cell not in self.obs_cells:
                    sec = self.sector_of_point(q[0], q[1])
                    if sec is None:
                        continue
                    self.obs_cells[cell] = sec
                if cell not in self.seen:
                    new_cells.append(cell)
                self.seen.add(cell)
                self.frame_seen.add(cell)
            reach = ray if dv < 255 else 2040.0
            rays.append(
                LineString([p, (p[0] + reach * math.cos(ang), p[1] + reach * math.sin(ang))])
            )
            if dv < 255:
                hx, hy = p[0] + ray * math.cos(ang), p[1] + ray * math.sin(ang)
                hit = Point(hx, hy)
                # The view stopped at an opaque face: among lines within the depth quantum, take the nearest one that can be a wall (one-sided, or a middle texture on the seen face); a two-sided trim line without a face does not stop a view ray.
                near = sorted(
                    (
                        (self._line_geoms[i].distance(hit), i)
                        for i in self._line_tree.query(hit.buffer(20))
                    ),
                    key=lambda x: x[0],
                )  # the depth buffer quantizes to 8 units, so the face the view stopped at may sit up to two quanta from the sample

                def opaque(line):
                    if line["l"] is None or line["r"] is None:
                        return True
                    ax, ay = line["a"]
                    bx, by = line["b"]
                    cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
                    face = line.get("ftex", []) if cross <= 0 else line.get("btex", [])
                    return any(n not in ("", "-") for n in face)

                pick = next(
                    ((dd, i) for dd, i in near if opaque(self.lines[i])), near[0] if near else None
                )
                i_line = pick[1] if pick else None
                seg = self._line_geoms[i_line] if pick else None
                if pick and pick[0] <= 20:
                    line = self.lines[i_line]
                    tpos = seg.project(hit, normalized=True)
                    rec = self.observed_lines.get(line["id"])
                    if rec is None:
                        rec = self.observed_lines[line["id"]] = dict(
                            id=line["id"],
                            a=line["a"],
                            b=line["b"],
                            kind=self.face_texture_kind(line, p),
                            texture=self.face_textures(line, p),
                            seen_tick=tick,
                            uses=0,
                            effects=[],
                            spans=[],
                        )
                    self.frame_hits.add(line["id"])
                    hit_positions[line["id"]].append(tpos)
                    margin = 8 / max(1e-6, seg.length)
                    lo, hi = max(0, tpos - margin), min(1, tpos + margin)
                    if not any(s0 <= lo and hi <= s1 for s0, s1 in rec["spans"]):
                        rec["spans"].append((lo, hi))
                        rec["spans"] = self._merge(rec["spans"])
                        changed = True
                        changed_lines.add(line["id"])
        if rays:
            ray_union = prep(unary_union(rays))
            for lid, v in self.observed_lines.items():
                base = LineString([v["a"], v["b"]])
                opened = {tuple(span) for span in v.get("open_spans", [])}
                opened.intersection_update(map(tuple, v["spans"]))
                before = {tuple(span) for span in v.get("open_spans", [])}
                for s0, s1 in v["spans"]:
                    span = (s0, s1)
                    if any(s0 <= t <= s1 for t in hit_positions.get(lid, [])):
                        opened.discard(span)
                        continue
                    a_ = base.interpolate(s0, normalized=True)
                    b_ = base.interpolate(s1, normalized=True)
                    segment = LineString([(a_.x, a_.y), (b_.x, b_.y)])
                    if ray_union.crosses(segment) and any(
                        r.crosses(segment) and Point(r.coords[-1]).distance(base) >= 24
                        for r in rays
                    ):
                        opened.add(span)
                if opened != before:
                    v["open_spans"] = sorted(opened)
                    changed = True
                    changed_lines.add(lid)
                v["open_tick"] = tick if v["spans"] and len(opened) == len(v["spans"]) else None
        if changed:
            self._rebuild_walls()
        if changed or new_cells:
            # Route caches are keyed to real change: a new seen cell only re-opens its own 3x3 neighbourhood; a changed wall piece only the cells near it.
            self.cache.clear()
            self._reach = None
            self._geom_version += 1
            for cell in new_cells:
                self._invalidate_cell(cell)
            for lid in changed_lines:
                v = self.observed_lines[lid]
                self._invalidate_line(v["a"], v["b"])

    @staticmethod
    def span_open(record, span):
        if "open_spans" not in record:
            return record.get("open_tick") is not None
        return tuple(span) in {tuple(item) for item in record["open_spans"]}

    def _rebuild_walls(self):
        segs = []
        for record in self.observed_lines.values():
            base = LineString([record["a"], record["b"]])
            for span in record["spans"]:
                if self.span_open(record, span):
                    continue
                a, b = (base.interpolate(t, normalized=True) for t in span)
                if a.distance(b) > 0.5:
                    segs.append(LineString([(a.x, a.y), (b.x, b.y)]))
        self._observed_union = unary_union(segs) if segs else GeometryCollection()
        margin = PLAYER_RADIUS + 0.1
        self._observed_blocked = unary_union([
            MultiPoint([(x + dx, y + dy) for x, y in seg.coords
                        for dx in (-margin, margin) for dy in (-margin, margin)]).convex_hull
            for seg in segs
        ]) if segs else GeometryCollection()
        self._union_prep = prep(self._observed_union)
        self._blocked_prep = prep(self._observed_blocked)

    def close_door(self, line_id, point):
        record = self.observed_lines[line_id]
        base = LineString([record["a"], record["b"]])
        t = base.project(Point(point), normalized=True)
        opened = [tuple(span) for span in record.get("open_spans", record["spans"]
                  if record.get("open_tick") is not None else [])]
        remaining = [span for span in opened if not span[0] <= t <= span[1]]
        if len(remaining) == len(opened):
            return False
        record["open_spans"] = remaining
        record["open_tick"] = None
        self._rebuild_walls()
        self.cache.clear()
        self._reach = None
        self._geom_version += 1
        self._invalidate_line(record["a"], record["b"])
        return True

    def point_is_open(self, record, point):
        base = LineString([record["a"], record["b"]])
        t = base.project(Point(point), normalized=True)
        return any(s0 <= t <= s1 and self.span_open(record, (s0, s1))
                   for s0, s1 in record["spans"])

    def _invalidate_cell(self, c):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (c[0] + dx, c[1] + dy)
                self._nb_cache.pop(k, None)
                self._edge_cache.pop(k, None)

    def _invalidate_line(self, a, b):
        m = STEP * 3
        x0, x1 = min(a[0], b[0]) - m, max(a[0], b[0]) + m
        y0, y1 = min(a[1], b[1]) - m, max(a[1], b[1]) + m
        for cache in (self._nb_cache, self._edge_cache):
            for k in [k for k in cache if x0 <= k[0] * STEP <= x1 and y0 <= k[1] * STEP <= y1]:
                cache.pop(k, None)

    def reach(self, p):
        """Single-source route costs over seen cells from the player's cell; one expansion serves every candidate test instead of one search per candidate."""
        a = self.nearest(p)
        if self._reach and self._reach[0] == a and self._reach[3] == self._geom_version:
            return self._reach
        todo = [(0, a)]
        previous = {a: None}
        cost = {a: 0}
        bounds = self.actor_bounds
        margin = STEP * 1.5
        while todo:
            g, c = heapq.heappop(todo)
            if g > cost[c]:
                continue
            cx, cy = self.xy(c)
            near = (
                bounds is not None
                and bounds[0] - margin <= cx <= bounds[2] + margin
                and bounds[1] - margin <= cy <= bounds[3] + margin
            )
            for k in self.neighbors(c):
                if k in self.blocked_cells or (
                    near and self.actor_blocks.intersects(LineString([(cx, cy), self.xy(k)]))
                ):
                    continue
                gg = g + dist(c, k)
                if gg < cost.get(k, 1e20):
                    cost[k] = gg
                    previous[k] = c
                    heapq.heappush(todo, (gg, k))
        self._reach = (a, cost, previous, self._geom_version)
        return self._reach

    def reach_path(self, p, goal):
        """Path from the shared expansion, [] when the goal cell is not reachable; fills the pairwise cache the waypoint uses."""
        a, cost, previous, _ = self.reach(p)
        b = self.nearest(goal)
        key = (a, b)
        if key in self.cache:
            return self.cache[key]
        path = []
        if b in previous:
            c = b
            while c is not None:
                path.append(self.xy(c))
                c = previous[c]
            path.reverse()
        if len(self.cache) > 2000:
            self.cache.clear()
        self.cache[key] = path
        return path

    @staticmethod
    def _merge(spans):
        spans = sorted(spans)
        out = []
        for s0, s1 in spans:
            if out and s0 <= out[-1][1] + 1e-9:
                out[-1] = (out[-1][0], max(out[-1][1], s1))
            else:
                out.append((s0, s1))
        return out

    def _span_mid(self, v):
        base = LineString([v["a"], v["b"]])
        s0, s1 = v["spans"][0]
        m = base.interpolate((s0 + s1) / 2, normalized=True)
        return [m.x, m.y]

    def observed_segments(self):
        """Observed wall pieces as (a,b) pairs, only the spans the view rays reached; pieces seen open carry open=True."""
        out = []
        for v in self.observed_lines.values():
            base = LineString([v["a"], v["b"]])
            for s0, s1 in v["spans"]:
                a = base.interpolate(s0, normalized=True)
                b = base.interpolate(s1, normalized=True)
                out.append(
                    dict(
                        id=v["id"],
                        a=[round(a.x, 1), round(a.y, 1)],
                        b=[round(b.x, 1), round(b.y, 1)],
                        kind=v["kind"],
                        open=self.span_open(v, (s0, s1)),
                    )
                )
        return out

    def record_heights(self, sector, floor, ceiling, tick):
        """Runtime floor and ceiling of a sector that is in view now; the only source of heights."""
        prev = self.observed_heights.get(sector)
        if prev is None or prev[0] != floor or prev[1] != ceiling:
            self._nb_cache.clear()
            self._edge_cache.clear()
            self.cache.clear()
            self._reach = None
            self._geom_version += 1
        self.observed_heights[sector] = (floor, ceiling, tick)

    def neighbors(self, c):
        if c in self._nb_cache:
            return self._nb_cache[c]
        out = []
        s = self.obs_cells.get(c)
        hs = self.observed_heights.get(s) if s is not None else None
        if hs is not None:
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
                k = (c[0] + dx, c[1] + dy)
                if (
                    k not in self.seen
                    or k not in self.obs_cells
                    or self._blocked_prep.intersects(Point(self.xy(k)))
                ):
                    continue
                ht = self.observed_heights.get(self.obs_cells[k])
                if ht is None or ht[0] - hs[0] > 24 or min(hs[1], ht[1]) - max(hs[0], ht[0]) < 56:
                    continue
                if self._union_prep.intersects(LineString([self.xy(c), self.xy(k)])):
                    continue
                out.append(k)
        self._nb_cache[c] = out
        return out

    def xy(self, c):
        return [c[0] * STEP, c[1] * STEP]

    def sector_at(self, p):
        return self.obs_cells.get((round(p[0] / STEP), round(p[1] / STEP)))

    def nearest(self, p):
        c = round(p[0] / STEP), round(p[1] / STEP)
        if c in self.obs_cells and c not in self.blocked_cells:
            return c
        return min(
            (k for k in self.obs_cells if k not in self.blocked_cells),
            key=lambda k: dist(self.xy(k), p),
            default=c,
        )

    def update_obstacles(self, obstacles):
        signature = tuple(sorted((e["id"], round(e["x"], 1), round(e["y"], 1)) for e in obstacles))
        if signature == self.obstacle_signature:
            return
        self.obstacle_signature = signature
        r = PLAYER_RADIUS + 10 + 0.2
        shapes = [box(x - r, y - r, x + r, y + r) for _, x, y in signature]
        self.actor_blocks = unary_union(shapes) if shapes else GeometryCollection()
        self.blocked_cells = set()
        self.actor_bounds = self.actor_blocks.bounds if shapes else None
        for _, x, y in signature:
            for cx in range(math.floor((x - r) / STEP) - 1, math.ceil((x + r) / STEP) + 2):
                for cy in range(math.floor((y - r) / STEP) - 1, math.ceil((y + r) / STEP) + 2):
                    c = (cx, cy)
                    if self.actor_blocks.intersects(Point(self.xy(c))):
                        self.blocked_cells.add(c)
        self.cache.clear()
        self._reach = None
        self._geom_version += 1

    def segment_clear(self, a, b):
        segment = LineString([a, b])
        # Clearance against the observed wall lines themselves: the player may stand within its own radius of a wall and slide along it.
        return not self._union_prep.intersects(segment) and not self.actor_blocks.intersects(
            segment
        )

    def path(self, start, goal):
        a, b = self.nearest(start), self.nearest(goal)
        key = (a, b)
        if key in self.cache:
            return self.cache[key]
        todo = [(0, 0, a)]
        previous = {a: None}
        cost = {a: 0}
        bounds = self.actor_bounds
        margin = STEP * 1.5
        while todo:
            _, g, c = heapq.heappop(todo)
            if c == b:
                break
            if g > cost[c]:
                continue
            cx, cy = self.xy(c)
            # Observed-barrel collision is tested only for edges near a known barrel box; without barrels no geometry is built.
            near = (
                bounds is not None
                and bounds[0] - margin <= cx <= bounds[2] + margin
                and bounds[1] - margin <= cy <= bounds[3] + margin
            )
            for k in self.neighbors(c):
                if k in self.blocked_cells or (
                    near and self.actor_blocks.intersects(LineString([(cx, cy), self.xy(k)]))
                ):
                    continue
                gg = g + dist(c, k)
                if gg < cost.get(k, 1e20):
                    cost[k] = gg
                    previous[k] = c
                    heapq.heappush(todo, (gg + dist(k, b), gg, k))
        if b not in previous:
            # Cache unreachable targets too; the cache clears when observed obstacles change.
            if len(self.cache) > 2000:
                self.cache.clear()
            self.cache[key] = []
            return []
        path = []
        c = b
        while c is not None:
            path.append(self.xy(c))
            c = previous[c]
        path.reverse()
        if len(self.cache) > 2000:
            self.cache.clear()
        self.cache[key] = path
        return path

    def candidates(self, p):
        """Frontiers at the edge of seen floor not closed by an observed wall piece, and observed wall faces whose visible texture reads as a switch, door, platform or exit sign; no WAD special is consulted."""
        out = []
        picked = []

        def open_edge(c):
            if c in self._edge_cache:
                return self._edge_cache[c]
            r = False
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
                k = (c[0] + dx, c[1] + dy)
                if k in self.seen:
                    continue
                if not self._union_prep.intersects(
                    LineString([self.xy(c), [k[0] * STEP, k[1] * STEP]])
                ):
                    r = True
                    break
            self._edge_cache[c] = r
            return r

        front = [
            c
            for c in self.seen
            if c in self.obs_cells
            and dist(self.xy(c), p) <= 640
            and self.failed[f"front_{c[0]}_{c[1]}"] < 2
            and not self._blocked_prep.intersects(Point(self.xy(c)))
            and open_edge(c)
        ]
        if not front:
            front = [
                c
                for c in self.seen
                if c in self.obs_cells
                and self.failed[f"front_{c[0]}_{c[1]}"] < 2
                and not self._blocked_prep.intersects(Point(self.xy(c)))
                and open_edge(c)
            ]
        front.sort(key=lambda c: (self.failed[f"front_{c[0]}_{c[1]}"], dist(self.xy(c), p)))
        for c in front:
            pt = self.xy(c)
            if dist(pt, p) < 70 or any(dist(pt, x) < 180 for x in picked):
                continue
            path = self.reach_path(p, pt)
            if not path or dist(path[-1], pt) > 20:
                continue
            picked.append(pt)
            id = f"front_{c[0]}_{c[1]}"
            out.append(
                dict(
                    id=id,
                    kind="frontier",
                    x=pt[0],
                    y=pt[1],
                    distance=round(len(path) * STEP),
                    failed=self.failed[id],
                    description="Edge of seen floor not closed by a seen wall; explore beyond it",
                )
            )
            if len(picked) >= 7:
                break
        walls_out = []
        exit_mids = [
            self._span_mid(w)
            for w in self.observed_lines.values()
            if w["kind"] == "exit_sign" and w["spans"]
        ]
        for id, v in self.observed_lines.items():
            if not v["spans"]:
                continue
            span = min(v["spans"], key=lambda s: dist(self._span_mid(dict(v, spans=[s])), p))
            is_open = self.span_open(v, span)
            # An open piece is a way through, not a target, unless it is a seen door: that one is offered as a walk-through while open.
            if is_open and v["kind"] != "door":
                continue
            if v["kind"] not in ("switch", "door", "platform", "exit_sign"):
                base = LineString([v["a"], v["b"]])
                span_len = sum((s1 - s0) for s0, s1 in v["spans"]) * base.length
                # a seen face with no texture is a step or ledge edge, not a wall to press
                if (
                    not v.get("texture")
                    or span_len < 32
                    or self.used[id] >= 2
                    or self.failed[id] >= 1
                ):
                    continue
            elif self.failed[id] >= 2:
                continue
            mid = self._span_mid(dict(v, spans=[span]))
            if dist(mid, p) > 400:
                continue
            dx = v["b"][0] - v["a"][0]
            dy = v["b"][1] - v["a"][1]
            length = math.hypot(dx, dy) or 1
            normal = [dy / length, -dx / length]
            side = 1 if (p[0] - mid[0]) * normal[0] + (p[1] - mid[1]) * normal[1] >= 0 else -1
            approach = [mid[0] + normal[0] * 32 * side, mid[1] + normal[1] * 32 * side]
            # Both floors across an untextured-kind piece were seen and differ by a walkable step with headroom: a step riser, not a wall to press.
            if v["kind"] is None and self._observed_step(mid, normal):
                continue
            through = None
            far_visited = False
            if v["kind"] == "door":
                through = [mid[0] - normal[0] * 72 * side, mid[1] - normal[1] * 72 * side]
                tc = (round(through[0] / STEP), round(through[1] / STEP))
                far_visited = any(
                    self.visits.get((tc[0] + dx, tc[1] + dy), 0) > 0
                    for dx in (-2, -1, 0, 1, 2)
                    for dy in (-2, -1, 0, 1, 2)
                )
                # A door seen open leads somewhere only if the floor beyond it is unvisited; the walk-through target is the far side of the doorway.
                if is_open and far_visited:
                    continue
            goal = through if is_open else approach
            path = self.reach_path(p, goal)
            if not path or dist(path[-1], goal) > 20:
                continue
            exit_like = v["kind"] == "exit_sign" or any(dist(em, mid) < 160 for em in exit_mids)
            tk = v["kind"] or "wall"
            if tk == "wall" and len(walls_out) >= 2:
                continue
            if tk == "wall":
                walls_out.append(id)
            if v["kind"] == "door":
                desc = (
                    (
                        "Seen door %s is open now (the view passes through it); walk through before it closes"
                        % id
                    )
                    if is_open
                    else "Seen door face (texture %s); approach, face it, USE to open it, then walk through while it is open"
                    % (",".join(v.get("texture") or ["door"]))
                )
            else:
                desc = (
                    (
                        "Seen wall face with a %s texture; approach, face it and USE; its effect is unknown until observed"
                        % v["kind"].replace("_", " ")
                    )
                    if v["kind"]
                    else "Seen wall face (texture %s); USE may reveal a door or switch, function unknown"
                    % (",".join(v.get("texture") or ["none"]))
                )
            rec = dict(
                id=id,
                kind="exit" if (exit_like and v["kind"] == "switch") else "door_or_switch",
                activation="use",
                x=goal[0],
                y=goal[1],
                line_x=mid[0],
                line_y=mid[1],
                distance=round(len(path) * STEP),
                failed=self.failed[id],
                uses=self.used[id],
                texture_kind=tk,
                description=desc,
            )
            if through:
                rec.update(
                    through=[round(through[0], 1), round(through[1], 1)],
                    open=is_open,
                    far_visited=far_visited,
                    line_a=list(v["a"]),
                    line_b=list(v["b"]),
                )
            out.append(rec)
        return sorted(
            out,
            key=lambda x: (
                x["failed"],
                0
                if x["kind"] == "exit"
                else 1
                if x.get("open")
                else 2
                if x.get("texture_kind") != "wall"
                else 3,
                bool(x.get("far_visited")),
                x["distance"],
            ),
        )[:12]

    def _observed_step(self, mid, normal):
        """True when the seen floor cells on both sides of a wall piece have observed heights that make it a walkable step (rise at most 24, headroom at least 56)."""
        sa = self.sector_at([mid[0] + normal[0] * 16, mid[1] + normal[1] * 16])
        sb = self.sector_at([mid[0] - normal[0] * 16, mid[1] - normal[1] * 16])
        if sa is None or sb is None or sa == sb:
            return False
        ha = self.observed_heights.get(sa)
        hb = self.observed_heights.get(sb)
        if ha is None or hb is None:
            return False
        return abs(ha[0] - hb[0]) <= 24 and min(ha[1], hb[1]) - max(ha[0], hb[0]) >= 56

    def waypoint(self, p, goal):
        path = self.path(p, goal)
        if not path:
            return None
        # Avoid corner clipping: only skip along clearance-checked lines.
        # If the current pose is too close to a corner, first recover to the
        # actual clearance-checked start node. Never jump to an unchecked edge.
        target = path[0]
        for q in path[1:12]:
            if self.segment_clear(p, q):
                target = q
            else:
                break
        return target

    def escape_point(self, p, enemies, projectiles):
        """Short collision-clear escape using only observed threats and local floor."""
        threats = [[e["x"], e["y"]] for e in enemies]
        if not threats:
            return None
        nearest = min(dist(p, e) for e in threats)
        candidates = []
        for cell in self.seen:
            q = self.xy(cell)
            travel = dist(p, q)
            if not 64 <= travel <= 144:
                continue
            route = LineString([p, q])
            if not self.segment_clear(p, q):
                continue
            # Avoid walking through a currently observed monster's collision body.
            if any(route.distance(Point(e)) < 40 for e in threats):
                continue
            separation = min(dist(q, e) for e in threats)
            cover = sum(self._observed_union.intersects(LineString([q, e])) for e in threats)
            current_cover = sum(
                self._observed_union.intersects(LineString([p, e])) for e in threats
            )
            projectile_clearance = 0
            for shot in projectiles:
                vx, vy = shot.get("velocity", [0, 0])
                end = [shot["x"] + vx, shot["y"] + vy]
                if vx or vy:
                    projectile_clearance += min(
                        80, LineString([[shot["x"], shot["y"]], end]).distance(Point(q))
                    )
            score = (
                separation - nearest + 100 * (cover - current_cover) + 0.3 * projectile_clearance
            )
            candidates.append((score, travel, q, separation, cover))
        if not candidates:
            return None
        score, travel, q, separation, cover = max(candidates)
        return dict(
            x=q[0],
            y=q[1],
            distance=round(travel),
            nearest_threat_distance=round(separation),
            static_cover_lines=cover,
            score=round(score, 1),
        )

    def ui(self):
        return dict(
            mode="observable",
            lines=[
                dict(a=s["a"], b=s["b"], kind=s["kind"], blocking=not s["open"])
                for s in self.observed_segments()
            ],
            trail=self.trail[-3000:],
            visited=sum(n > 0 for n in self.visits.values()),
            discovered=len(self.seen),
            total_cells=len(self.obs_cells),
            seen_cells=[list(c) for c in self.seen][:6000],
            assistance="Observed wall pieces and seen floor cells only; no static map geometry",
        )

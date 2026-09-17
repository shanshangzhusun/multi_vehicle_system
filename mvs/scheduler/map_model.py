from __future__ import annotations

import heapq
import json
import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class Node:
    node_id: str
    x: float
    y: float
    kind: str = "road"
    lon: Optional[float] = None
    lat: Optional[float] = None
    alt: float = 0.0


@dataclass
class EdgeMeta:
    edge_id: str
    src: str
    dst: str
    geom_from: str
    geom_to: str
    cost: float
    width: float
    lanes_forward: int
    lanes_backward: int
    speed_limit_mps: float
    curvature: float
    geometry: List[Dict[str, float]]
    base_from_node: Optional[str] = None
    base_to_node: Optional[str] = None
    base_start_ratio: float = 0.0
    base_end_ratio: float = 1.0

    @property
    def lane_width(self) -> float:
        lane_count = max(1, self.lanes_forward + self.lanes_backward)
        return self.width / lane_count


class RoadGraph:
    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, List[Tuple[str, float]]] = {}
        self.edge_meta: Dict[Tuple[str, str], EdgeMeta] = {}
        self._edge_index_version = 0
        self._edge_index_built_version = -1
        self._edge_index_cell_size = 1000.0
        self._edge_spatial_index: Dict[Tuple[int, int], List[Tuple[str, str]]] = {}
        self._edge_spatial_cells: Dict[Tuple[str, str], Set[Tuple[int, int]]] = {}

    def add_node(
        self,
        node_id: str,
        x: float,
        y: float,
        kind: str = "road",
        lon: Optional[float] = None,
        lat: Optional[float] = None,
        alt: float = 0.0,
    ) -> None:
        self.nodes[node_id] = Node(node_id, x, y, kind=kind, lon=lon, lat=lat, alt=alt)
        self.edges.setdefault(node_id, [])

    def add_bidirectional_edge(
        self,
        a: str,
        b: str,
        cost: float,
        edge_id: Optional[str] = None,
        width: float = 6.0,
        lanes_forward: int = 1,
        lanes_backward: int = 1,
        speed_limit_mps: float = 8.0,
        curvature: float = 0.0,
        geometry: Optional[List[Dict[str, float]]] = None,
        geom_from: Optional[str] = None,
        geom_to: Optional[str] = None,
        base_from_node: Optional[str] = None,
        base_to_node: Optional[str] = None,
        base_start_ratio: float = 0.0,
        base_end_ratio: float = 1.0,
    ) -> None:
        self.edges.setdefault(a, []).append((b, cost))
        self.edges.setdefault(b, []).append((a, cost))
        key = self._edge_key(a, b)
        has_explicit_base_ref = base_from_node is not None or base_to_node is not None
        if has_explicit_base_ref and key != (a, b):
            base_start_ratio, base_end_ratio = base_end_ratio, base_start_ratio
        self.edge_meta[key] = EdgeMeta(
            edge_id=edge_id or f"{key[0]}__{key[1]}",
            src=key[0],
            dst=key[1],
            geom_from=geom_from or a,
            geom_to=geom_to or b,
            cost=cost,
            width=width,
            lanes_forward=lanes_forward,
            lanes_backward=lanes_backward,
            speed_limit_mps=speed_limit_mps,
            curvature=curvature,
            geometry=geometry or [],
            base_from_node=base_from_node or key[0],
            base_to_node=base_to_node or key[1],
            base_start_ratio=base_start_ratio,
            base_end_ratio=base_end_ratio,
        )
        self._edge_index_version += 1
        if self._edge_index_built_version >= 0:
            self._add_edge_to_spatial_index(key, self.edge_meta[key])
            self._edge_index_built_version = self._edge_index_version

    @staticmethod
    def _edge_key(a: str, b: str) -> Tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def get_edge_cost(self, a: str, b: str) -> Optional[float]:
        for nb, w in self.edges.get(a, []):
            if nb == b:
                return w
        return None

    def get_edge_meta(self, a: str, b: str) -> Optional[EdgeMeta]:
        return self.edge_meta.get(self._edge_key(a, b))

    def remove_edge(self, a: str, b: str) -> None:
        key = self._edge_key(a, b)
        meta = self.edge_meta.get(key)
        self.edges[a] = [(nb, w) for nb, w in self.edges.get(a, []) if nb != b]
        self.edges[b] = [(nb, w) for nb, w in self.edges.get(b, []) if nb != a]
        self.edge_meta.pop(key, None)
        self._edge_index_version += 1
        if meta is not None and self._edge_index_built_version >= 0:
            self._remove_edge_from_spatial_index(key)
            self._edge_index_built_version = self._edge_index_version

    def _add_edge_to_spatial_index(self, key: Tuple[str, str], meta: EdgeMeta) -> None:
        bbox = self._edge_bbox(meta)
        if bbox is None:
            return
        cells = set(self._edge_index_cells_for_bbox(bbox))
        self._edge_spatial_cells[key] = cells
        for cell in cells:
            keys = self._edge_spatial_index.setdefault(cell, [])
            if key not in keys:
                keys.append(key)

    def _remove_edge_from_spatial_index(self, key: Tuple[str, str]) -> None:
        empty_cells: List[Tuple[int, int]] = []
        cells = self._edge_spatial_cells.pop(key, None)
        if cells is None:
            cells = set(self._edge_spatial_index.keys())
        for cell in cells:
            keys = self._edge_spatial_index.get(cell)
            if not keys:
                continue
            if key in keys:
                self._edge_spatial_index[cell] = [item for item in keys if item != key]
                if not self._edge_spatial_index[cell]:
                    empty_cells.append(cell)
        for cell in empty_cells:
            self._edge_spatial_index.pop(cell, None)

    def _edge_index_cells_for_bbox(self, bbox: Tuple[float, float, float, float]) -> List[Tuple[int, int]]:
        min_x, min_y, max_x, max_y = bbox
        cell_size = max(1e-9, self._edge_index_cell_size)
        ix0, iy0 = math.floor(min_x / cell_size), math.floor(min_y / cell_size)
        ix1, iy1 = math.floor(max_x / cell_size), math.floor(max_y / cell_size)
        return [(ix, iy) for ix in range(ix0, ix1 + 1) for iy in range(iy0, iy1 + 1)]

    def _edge_bbox(self, meta: EdgeMeta) -> Optional[Tuple[float, float, float, float]]:
        if len(meta.geometry) >= 2:
            xs = [float(p["x"]) for p in meta.geometry]
            ys = [float(p["y"]) for p in meta.geometry]
        else:
            a = self.nodes.get(meta.src)
            b = self.nodes.get(meta.dst)
            if not a or not b:
                return None
            xs = [a.x, b.x]
            ys = [a.y, b.y]
        return min(xs), min(ys), max(xs), max(ys)

    def add_point_on_edge(
        self,
        point_id: str,
        from_node: str,
        to_node: str,
        ratio: float,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> str:
        ratio = min(0.999999, max(0.000001, ratio))
        meta = self.get_edge_meta(from_node, to_node)
        cost = self.get_edge_cost(from_node, to_node)
        if meta is None or cost is None:
            raise ValueError(f"edge not found for point {point_id}: {from_node}->{to_node}")

        a = self.nodes[from_node]
        b = self.nodes[to_node]
        px = x if x is not None else a.x + (b.x - a.x) * ratio
        py = y if y is not None else a.y + (b.y - a.y) * ratio
        lon = None
        lat = None
        if a.lon is not None and b.lon is not None:
            lon = a.lon + (b.lon - a.lon) * ratio
        if a.lat is not None and b.lat is not None:
            lat = a.lat + (b.lat - a.lat) * ratio
        alt = a.alt + (b.alt - a.alt) * ratio
        self.add_node(point_id, px, py, kind="roadside", lon=lon, lat=lat, alt=alt)
        self.remove_edge(from_node, to_node)
        base_geometry = list(meta.geometry)
        if meta.geometry and not (meta.geom_from == from_node and meta.geom_to == to_node):
            base_geometry = list(reversed(meta.geometry))
        if from_node == meta.src and to_node == meta.dst:
            base_start = meta.base_start_ratio
            base_end = meta.base_end_ratio
        else:
            base_start = meta.base_end_ratio
            base_end = meta.base_start_ratio
        base_point = base_start + (base_end - base_start) * ratio
        geom1, geom2 = self._split_geometry(base_geometry, ratio, px, py, a.x, a.y, b.x, b.y)
        self.add_bidirectional_edge(
            from_node,
            point_id,
            cost * ratio,
            edge_id=f"{meta.edge_id}__part1",
            width=meta.width,
            lanes_forward=meta.lanes_forward,
            lanes_backward=meta.lanes_backward,
            speed_limit_mps=meta.speed_limit_mps,
            curvature=meta.curvature,
            geometry=geom1,
            geom_from=from_node,
            geom_to=point_id,
            base_from_node=meta.base_from_node,
            base_to_node=meta.base_to_node,
            base_start_ratio=base_start,
            base_end_ratio=base_point,
        )
        self.add_bidirectional_edge(
            point_id,
            to_node,
            cost * (1.0 - ratio),
            edge_id=f"{meta.edge_id}__part2",
            width=meta.width,
            lanes_forward=meta.lanes_forward,
            lanes_backward=meta.lanes_backward,
            speed_limit_mps=meta.speed_limit_mps,
            curvature=meta.curvature,
            geometry=geom2,
            geom_from=point_id,
            geom_to=to_node,
            base_from_node=meta.base_from_node,
            base_to_node=meta.base_to_node,
            base_start_ratio=base_point,
            base_end_ratio=base_end,
        )
        return point_id

    @staticmethod
    def _point_at_polyline_ratio(poly: List[Dict[str, float]], ratio: float) -> Tuple[float, float]:
        points = [(float(item["x"]), float(item["y"])) for item in poly]
        lengths = [
            math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
            for i in range(len(points) - 1)
        ]
        total = sum(lengths)
        if total <= 1e-9:
            return points[0]
        target = min(1.0, max(0.0, float(ratio))) * total
        traversed = 0.0
        for index, length in enumerate(lengths):
            if length <= 1e-9:
                continue
            if traversed + length >= target:
                local = (target - traversed) / length
                ax, ay = points[index]
                bx, by = points[index + 1]
                return ax + (bx - ax) * local, ay + (by - ay) * local
            traversed += length
        return points[-1]

    @staticmethod
    def _split_geometry(
        geometry: List[Dict[str, float]],
        ratio: float,
        px: float,
        py: float,
        ax: float,
        ay: float,
        bx: float,
        by: float,
    ) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        if len(geometry) < 2:
            return ([{"x": ax, "y": ay}, {"x": px, "y": py}], [{"x": px, "y": py}, {"x": bx, "y": by}])

        pts = [(float(p["x"]), float(p["y"])) for p in geometry]
        total = 0.0
        seg_lens: List[float] = []
        for i in range(len(pts) - 1):
            seg_len = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
            seg_lens.append(seg_len)
            total += seg_len
        if total <= 1e-6:
            return ([{"x": ax, "y": ay}, {"x": px, "y": py}], [{"x": px, "y": py}, {"x": bx, "y": by}])

        target = total * ratio
        acc = 0.0
        split_idx = 0
        for i, seg_len in enumerate(seg_lens):
            if acc + seg_len >= target:
                split_idx = i
                break
            acc += seg_len

        p1 = list(pts[: split_idx + 1])
        p2 = list(pts[split_idx + 1 :])
        split_point = (px, py)
        if not p1 or math.hypot(p1[-1][0] - px, p1[-1][1] - py) > 1e-3:
            p1.append(split_point)
        if not p2 or math.hypot(p2[0][0] - px, p2[0][1] - py) > 1e-3:
            p2 = [split_point] + p2
        return (
            [{"x": round(x, 3), "y": round(y, 3)} for x, y in p1],
            [{"x": round(x, 3), "y": round(y, 3)} for x, y in p2],
        )

    def nearest_node(self, x: float, y: float) -> Optional[str]:
        best_node = None
        best_d = float("inf")
        for node_id, n in self.nodes.items():
            d = math.hypot(n.x - x, n.y - y)
            if d < best_d:
                best_d = d
                best_node = node_id
        return best_node

    def shortest_path(self, start: str, goal: str) -> Tuple[List[str], float]:
        if start == goal:
            return [start], 0.0

        pq = [(0.0, start)]
        prev: Dict[str, Optional[str]] = {start: None}
        dist: Dict[str, float] = {start: 0.0}

        while pq:
            d, u = heapq.heappop(pq)
            if u == goal:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, w in self.edges.get(u, []):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        if goal not in prev:
            return [], float("inf")

        path = []
        cur = goal
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        path.reverse()
        return path, dist[goal]

    def fastest_path(self, start: str, goal: str, speed_cap_mps: Optional[float] = None) -> Tuple[List[str], float, float]:
        if start == goal:
            return [start], 0.0, 0.0

        pq = [(0.0, start)]
        prev: Dict[str, Optional[str]] = {start: None}
        time_cost: Dict[str, float] = {start: 0.0}
        dist_cost: Dict[str, float] = {start: 0.0}

        while pq:
            cur_time, u = heapq.heappop(pq)
            if u == goal:
                break
            if cur_time > time_cost.get(u, float("inf")):
                continue
            for v, _ in self.edges.get(u, []):
                meta = self.get_edge_meta(u, v)
                edge_dist = self.get_edge_cost(u, v)
                if meta is None or edge_dist is None:
                    continue
                eff_speed = max(0.5, meta.speed_limit_mps)
                if speed_cap_mps is not None:
                    eff_speed = min(eff_speed, max(0.5, speed_cap_mps))
                edge_time = edge_dist / eff_speed
                nd_time = cur_time + edge_time
                if nd_time < time_cost.get(v, float("inf")):
                    time_cost[v] = nd_time
                    dist_cost[v] = dist_cost[u] + edge_dist
                    prev[v] = u
                    heapq.heappush(pq, (nd_time, v))

        if goal not in prev:
            return [], float("inf"), float("inf")

        path = []
        cur = goal
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        path.reverse()
        return path, dist_cost[goal], time_cost[goal]


@dataclass
class SpecialPoints:
    hide_points: List[str]
    launch_points: List[str]
    depots: List[str]


class MapLoader:
    @staticmethod
    def load_graph_from_config(map_cfg: dict) -> RoadGraph:
        # 任务书“信息读取模块”：这里统一接入路网拓扑。
        # 当前支持 SHP、OpenDRIVE 和已导出的 graph_json，后续调度/车辆/贮备库共用同一张图。
        source_type = str(map_cfg.get("source_type", "")).lower()
        shp_path = map_cfg.get("shp_path")
        xodr_path = map_cfg.get("xodr_path")
        graph_json_path = map_cfg.get("graph_json")

        if source_type == "shp" and shp_path:
            return MapLoader.shp_to_graph(
                shp_path,
                default_lane_width=float(map_cfg.get("default_lane_width_m", 8.0)),
                default_speed_limit_mps=float(map_cfg.get("default_speed_limit_mps", 8.0)),
                default_lanes_forward=int(map_cfg.get("default_lanes_forward", 1)),
                default_lanes_backward=int(map_cfg.get("default_lanes_backward", 1)),
                node_snap_tol=float(map_cfg.get("node_snap_tol_m", 0.5)),
            )
        if xodr_path:
            return MapLoader.opendrive_to_graph(xodr_path)
        if graph_json_path:
            return MapLoader.load_graph(graph_json_path)
        raise ValueError("map config must provide graph_json, xodr_path, or shp_path")

    @staticmethod
    def load_points_from_config(map_cfg: dict, graph: RoadGraph) -> SpecialPoints:
        # 任务书“点位集合输入”：发射点、隐蔽点、贮备库都在这里进入图模型。
        # 点位随后会投影到路网边上，保证路径搜索只在可通行路网上进行。
        points_json_path = map_cfg.get("points_json")
        shp_points_path = map_cfg.get("shp_points_path")
        if points_json_path:
            return MapLoader.load_points(points_json_path, graph)
        if shp_points_path:
            return MapLoader.load_points_from_shp(shp_points_path, graph)
        return SpecialPoints(hide_points=[], launch_points=[], depots=[])

    @staticmethod
    def load_graph(graph_json_path: str) -> RoadGraph:
        graph = RoadGraph()
        p = Path(graph_json_path)
        obj = json.loads(p.read_text(encoding="utf-8"))
        for n in obj.get("nodes", []):
            lon = n.get("lon")
            lat = n.get("lat")
            graph.add_node(
                n["id"],
                float(n["x"]),
                float(n["y"]),
                kind=n.get("kind", "road"),
                lon=float(lon) if lon is not None else None,
                lat=float(lat) if lat is not None else None,
                alt=float(n.get("alt", 0.0)),
            )
        for e in obj.get("edges", []):
            geom_from = e.get("geom_from")
            geom_to = e.get("geom_to")
            if not geom_from or not geom_to:
                geom_from, geom_to = MapLoader._infer_geometry_direction(
                    graph,
                    e["from"],
                    e["to"],
                    list(e.get("geometry", [])),
                )
            graph.add_bidirectional_edge(
                e["from"],
                e["to"],
                float(e["cost"]),
                edge_id=e.get("id"),
                width=float(e.get("width", 6.0)),
                lanes_forward=int(e.get("lanes_forward", 1)),
                lanes_backward=int(e.get("lanes_backward", 1)),
                speed_limit_mps=float(e.get("speed_limit_mps", 8.0)),
                curvature=float(e.get("curvature", 0.0)),
                geometry=list(e.get("geometry", [])),
                geom_from=geom_from,
                geom_to=geom_to,
            )
        return graph

    @staticmethod
    def _infer_geometry_direction(
        graph: RoadGraph,
        from_node: str,
        to_node: str,
        geometry: List[Dict[str, float]],
    ) -> Tuple[str, str]:
        if len(geometry) < 2:
            return from_node, to_node
        start = geometry[0]
        end = geometry[-1]
        fn = graph.nodes[from_node]
        tn = graph.nodes[to_node]
        dsf = math.hypot(float(start["x"]) - fn.x, float(start["y"]) - fn.y)
        dse = math.hypot(float(start["x"]) - tn.x, float(start["y"]) - tn.y)
        defn = math.hypot(float(end["x"]) - fn.x, float(end["y"]) - fn.y)
        detn = math.hypot(float(end["x"]) - tn.x, float(end["y"]) - tn.y)
        aligned = dsf + detn
        reversed_cost = dse + defn
        if aligned <= reversed_cost:
            return from_node, to_node
        return to_node, from_node

    @staticmethod
    def load_points(points_json_path: str, graph: RoadGraph) -> SpecialPoints:
        obj = json.loads(Path(points_json_path).read_text(encoding="utf-8"))
        return SpecialPoints(
            hide_points=MapLoader._materialize_points(obj.get("hide_points", []), "hide", graph),
            launch_points=MapLoader._materialize_points(obj.get("launch_points", []), "launch", graph),
            depots=MapLoader._materialize_points(obj.get("depots", []), "depot", graph),
        )

    @staticmethod
    def _materialize_points(points: List, prefix: str, graph: RoadGraph) -> List[str]:
        out: List[str] = []
        for idx, point in enumerate(points):
            if isinstance(point, str):
                out.append(point)
                continue
            if not isinstance(point, dict):
                continue
            point_id = point.get("id", f"{prefix}_{idx:03d}")
            node_id = point.get("node_id")
            if node_id:
                out.append(node_id)
                continue
            if "edge_id" in point:
                edge_id = point["edge_id"]
                match = None
                for meta in graph.edge_meta.values():
                    if meta.edge_id == edge_id:
                        match = meta
                        break
                if match is None:
                    continue
                out.append(
                    graph.add_point_on_edge(
                        point_id=point_id,
                        from_node=match.src,
                        to_node=match.dst,
                        ratio=float(point.get("ratio", 0.5)),
                        x=float(point["x"]) if "x" in point else None,
                        y=float(point["y"]) if "y" in point else None,
                    )
                )
                continue
            if "from" in point and "to" in point:
                out.append(
                    graph.add_point_on_edge(
                        point_id=point_id,
                        from_node=point["from"],
                        to_node=point["to"],
                        ratio=float(point.get("ratio", 0.5)),
                        x=float(point["x"]) if "x" in point else None,
                        y=float(point["y"]) if "y" in point else None,
                    )
                )
        return out

    @staticmethod
    def load_points_from_shp(points_shp_path: str, graph: RoadGraph) -> SpecialPoints:
        # SHP 点位按属性自动识别 kind/type/name，兼容甲方给出的中文点名。
        # 识别后立即做最近边投影，形成后续规划和冲突消解使用的图节点。
        try:
            import shapefile  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "SHP points loading requires the optional 'pyshp' package. "
                "Install it or convert points to points_json first."
            ) from exc

        reader = shapefile.Reader(points_shp_path)
        fields = [f[0] for f in reader.fields[1:]]
        groups = {"hide": [], "launch": [], "depot": []}
        counters = {"hide": 0, "launch": 0, "depot": 0}

        for sr in reader.iterShapeRecords():
            if not sr.shape.points:
                continue
            attrs = {fields[i].lower(): sr.record[i] for i in range(len(fields))}
            kind = MapLoader._infer_point_kind(attrs)
            if kind is None:
                continue
            x, y = sr.shape.points[0]
            nearest = MapLoader._nearest_edge_projection(graph, x, y)
            point_id = f"{kind}_{counters[kind]:03d}"
            counters[kind] += 1
            if nearest is not None:
                edge, ratio = nearest
                groups[kind].append(
                    graph.add_point_on_edge(
                        point_id=point_id,
                        from_node=edge.src,
                        to_node=edge.dst,
                        ratio=ratio,
                        x=x,
                        y=y,
                    )
                )
            else:
                node_id = graph.nearest_node(x, y)
                if node_id:
                    groups[kind].append(node_id)

        return SpecialPoints(
            hide_points=groups["hide"],
            launch_points=groups["launch"],
            depots=groups["depot"],
        )

    @staticmethod
    def shp_to_graph(
        shp_path: str,
        default_lane_width: float = 8.0,
        default_speed_limit_mps: float = 8.0,
        default_lanes_forward: int = 1,
        default_lanes_backward: int = 1,
        node_snap_tol: float = 0.5,
    ) -> RoadGraph:
        try:
            import shapefile  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "SHP road loading requires the optional 'pyshp' package. "
                "Install it or convert SHP to graph_json first."
            ) from exc

        graph = RoadGraph()
        reader = shapefile.Reader(shp_path)
        fields = [f[0] for f in reader.fields[1:]]
        node_index: Dict[Tuple[int, int], str] = {}
        next_node_idx = 1
        next_edge_idx = 1

        def coord_node_id(x: float, y: float) -> str:
            nonlocal next_node_idx
            key = (round(x / node_snap_tol), round(y / node_snap_tol))
            node_id = node_index.get(key)
            if node_id:
                return node_id
            node_id = f"shp_n{next_node_idx}"
            next_node_idx += 1
            node_index[key] = node_id
            graph.add_node(node_id, float(x), float(y), kind="road")
            return node_id

        for shape_record in reader.iterShapeRecords():
            shape = shape_record.shape
            if not shape.points:
                continue
            attrs = {fields[i].lower(): shape_record.record[i] for i in range(len(fields))}
            parts = list(shape.parts) + [len(shape.points)]
            lanes_forward, lanes_backward = MapLoader._infer_lane_counts(
                attrs,
                default_lanes_forward=default_lanes_forward,
                default_lanes_backward=default_lanes_backward,
            )
            width = MapLoader._infer_width(attrs, default_lane_width, lanes_forward, lanes_backward)
            speed_limit_mps = MapLoader._infer_speed_limit(attrs, default_speed_limit_mps)

            for part_idx in range(len(parts) - 1):
                pts = shape.points[parts[part_idx] : parts[part_idx + 1]]
                if len(pts) < 2:
                    continue
                prev_node = None
                prev_pt = None
                for seg_idx, pt in enumerate(pts):
                    cur_node = coord_node_id(float(pt[0]), float(pt[1]))
                    if prev_node is not None and prev_pt is not None:
                        ax, ay = float(prev_pt[0]), float(prev_pt[1])
                        bx, by = float(pt[0]), float(pt[1])
                        dist = math.hypot(bx - ax, by - ay)
                        geometry = [{"x": round(ax, 3), "y": round(ay, 3)}, {"x": round(bx, 3), "y": round(by, 3)}]
                        graph.add_bidirectional_edge(
                            prev_node,
                            cur_node,
                            dist,
                            edge_id=f"shp_e{next_edge_idx}",
                            width=width,
                            lanes_forward=lanes_forward,
                            lanes_backward=lanes_backward,
                            speed_limit_mps=speed_limit_mps,
                            curvature=MapLoader._segment_curvature(prev_pt, pt, pts, seg_idx),
                            geometry=geometry,
                        )
                        next_edge_idx += 1
                    prev_node = cur_node
                    prev_pt = pt
        return graph

    @staticmethod
    def _infer_point_kind(attrs: Dict[str, object]) -> Optional[str]:
        keys = ["kind", "type", "category", "point_type", "name"]
        raw = ""
        for key in keys:
            if key in attrs and attrs[key] is not None:
                raw = str(attrs[key]).lower()
                break
        if not raw:
            return None
        if any(token in raw for token in ["hide", "conceal", "hidden", "隐蔽"]):
            return "hide"
        if any(token in raw for token in ["launch", "fire", "firing", "发射"]):
            return "launch"
        if any(token in raw for token in ["depot", "store", "reload", "supply", "库", "贮备", "补给"]):
            return "depot"
        return None

    @staticmethod
    def _infer_lane_counts(
        attrs: Dict[str, object],
        default_lanes_forward: int,
        default_lanes_backward: int,
    ) -> Tuple[int, int]:
        def parse_int(*keys: str) -> Optional[int]:
            for key in keys:
                if key in attrs and attrs[key] not in {None, ""}:
                    try:
                        return max(0, int(float(attrs[key])))
                    except Exception:
                        continue
            return None

        lanes_total = parse_int("lanes", "lane_num", "lane_count")
        lanes_forward = parse_int("lanes_fwd", "forward_lanes", "fwd_lanes")
        lanes_backward = parse_int("lanes_bwd", "backward_lanes", "bwd_lanes")
        oneway = str(attrs.get("oneway", attrs.get("direction", ""))).lower()
        if lanes_forward is None and lanes_backward is None and lanes_total is not None:
            if oneway in {"1", "true", "yes", "forward", "single"}:
                lanes_forward = max(1, lanes_total)
                lanes_backward = 0
            else:
                lanes_forward = max(1, lanes_total // 2 or 1)
                lanes_backward = max(1, lanes_total - lanes_forward)
        return (
            lanes_forward if lanes_forward is not None else default_lanes_forward,
            lanes_backward if lanes_backward is not None else default_lanes_backward,
        )

    @staticmethod
    def _infer_width(
        attrs: Dict[str, object],
        default_lane_width: float,
        lanes_forward: int,
        lanes_backward: int,
    ) -> float:
        for key in ["width", "road_width", "total_width", "width_m"]:
            if key in attrs and attrs[key] not in {None, ""}:
                try:
                    return float(attrs[key])
                except Exception:
                    pass
        for key in ["lane_width", "lane_w", "lane_width_m"]:
            if key in attrs and attrs[key] not in {None, ""}:
                try:
                    return float(attrs[key]) * max(1, lanes_forward + lanes_backward)
                except Exception:
                    pass
        return default_lane_width * max(1, lanes_forward + lanes_backward)

    @staticmethod
    def _infer_speed_limit(attrs: Dict[str, object], default_speed_limit_mps: float) -> float:
        for key in ["speed", "speed_mps", "maxspeed", "speed_limit"]:
            if key in attrs and attrs[key] not in {None, ""}:
                try:
                    value = float(attrs[key])
                    return value / 3.6 if value > 30.0 else value
                except Exception:
                    pass
        return default_speed_limit_mps

    @staticmethod
    def _segment_curvature(prev_pt, cur_pt, part_points, seg_idx: int) -> float:
        if seg_idx <= 0 or seg_idx >= len(part_points) - 1:
            return 0.0
        ax, ay = part_points[seg_idx - 1]
        bx, by = prev_pt
        cx, cy = cur_pt
        h1 = math.atan2(by - ay, bx - ax)
        h2 = math.atan2(cy - by, cx - bx)
        diff = abs((h2 - h1 + math.pi) % (2.0 * math.pi) - math.pi)
        seg_len = max(1e-6, math.hypot(cx - bx, cy - by))
        return diff / seg_len

    @staticmethod
    #按照点到整条路段几何这先的最小距离
    def _nearest_edge_projection(graph: RoadGraph, x: float, y: float) -> Optional[Tuple[EdgeMeta, float]]:
        indexed_result: Optional[Tuple[EdgeMeta, float]] = None
        try:
            MapLoader._ensure_edge_spatial_index(graph)
            candidates = MapLoader._edge_candidates_near_point(graph, x, y)
            if candidates:
                indexed_result = MapLoader._nearest_edge_projection_from_candidates(graph, x, y, candidates)
                if indexed_result is not None and os.environ.get("MVS_EDGE_PROJECTION_VERIFY_BRUTE", "").strip().lower() not in {"1", "true", "yes", "on"}:
                    return indexed_result
        except Exception:
            indexed_result = None
        if os.environ.get("MVS_EDGE_PROJECTION_VERIFY_BRUTE", "").strip().lower() in {"1", "true", "yes", "on"}:
            brute_result = MapLoader._nearest_edge_projection_bruteforce(graph, x, y)
            if indexed_result is None:
                return brute_result
            if brute_result is None:
                return indexed_result
            indexed_dist = MapLoader._projection_distance_for_edge(graph, indexed_result[0], x, y)
            brute_dist = MapLoader._projection_distance_for_edge(graph, brute_result[0], x, y)
            if brute_dist + 1e-9 < indexed_dist:
                return brute_result
            return indexed_result
        return MapLoader._nearest_edge_projection_bruteforce(graph, x, y)

    @staticmethod
    def _nearest_edge_projection_bruteforce(graph: RoadGraph, x: float, y: float) -> Optional[Tuple[EdgeMeta, float]]:
        best_meta: Optional[EdgeMeta] = None
        best_dist = float("inf")
        best_ratio = 0.5
        # Vehicle scoring / planning can read projections while another worker inserts points.
        # Snapshot edge metadata before scanning so concurrent edge splits do not abort the caller.
        for meta in list(graph.edge_meta.values()):
            poly = meta.geometry
            if len(poly) < 2:
                a = graph.nodes.get(meta.src)
                b = graph.nodes.get(meta.dst)
                if not a or not b:
                    continue
                poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
            ratio, dist = MapLoader._project_ratio_on_polyline(poly, x, y)
            if dist < best_dist:
                best_dist = dist
                best_meta = meta
                best_ratio = ratio
        if best_meta is None:
            return None
        return best_meta, best_ratio

    @staticmethod
    def _nearest_edge_projection_from_candidates(
        graph: RoadGraph,
        x: float,
        y: float,
        candidate_keys: Set[Tuple[str, str]],
    ) -> Optional[Tuple[EdgeMeta, float]]:
        best_meta: Optional[EdgeMeta] = None
        best_dist = float("inf")
        best_ratio = 0.5
        for key in candidate_keys:
            meta = graph.edge_meta.get(key)
            if meta is None:
                continue
            poly = meta.geometry
            if len(poly) < 2:
                a = graph.nodes.get(meta.src)
                b = graph.nodes.get(meta.dst)
                if not a or not b:
                    continue
                poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
            ratio, dist = MapLoader._project_ratio_on_polyline(poly, x, y)
            if dist < best_dist:
                best_dist = dist
                best_meta = meta
                best_ratio = ratio
        if best_meta is None:
            return None

        # Any truly closer edge must have a bounding box within best_dist of the query point.
        # Query that square once to keep indexed results equivalent to a full scan.
        exact_keys = MapLoader._edge_keys_intersecting_square(graph, x, y, best_dist)
        if exact_keys and not exact_keys.issubset(candidate_keys):
            return MapLoader._nearest_edge_projection_from_candidates(graph, x, y, exact_keys)
        return best_meta, best_ratio

    @staticmethod
    def _ensure_edge_spatial_index(graph: RoadGraph) -> None:
        if graph._edge_index_built_version == graph._edge_index_version and graph._edge_spatial_index:
            return
        cell_size = MapLoader._edge_index_cell_size(graph)
        index: Dict[Tuple[int, int], List[Tuple[str, str]]] = {}
        edge_cells: Dict[Tuple[str, str], Set[Tuple[int, int]]] = {}
        # Build the spatial index from a stable edge snapshot; vehicle-side projection may run
        # concurrently with path planning that splits edges for special points.
        for key, meta in list(graph.edge_meta.items()):
            bbox = MapLoader._edge_bbox(graph, meta)
            if bbox is None:
                continue
            min_x, min_y, max_x, max_y = bbox
            ix0, iy0 = math.floor(min_x / cell_size), math.floor(min_y / cell_size)
            ix1, iy1 = math.floor(max_x / cell_size), math.floor(max_y / cell_size)
            cells: Set[Tuple[int, int]] = set()
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    cell = (ix, iy)
                    index.setdefault(cell, []).append(key)
                    cells.add(cell)
            edge_cells[key] = cells
        graph._edge_index_cell_size = cell_size
        graph._edge_spatial_index = index
        graph._edge_spatial_cells = edge_cells
        graph._edge_index_built_version = graph._edge_index_version

    @staticmethod
    def _edge_index_cell_size(graph: RoadGraph) -> float:
        if not graph.nodes:
            return 1000.0
        xs = [node.x for node in graph.nodes.values()]
        ys = [node.y for node in graph.nodes.values()]
        if min(xs) >= -180.0 and max(xs) <= 180.0 and min(ys) >= -90.0 and max(ys) <= 90.0:
            return 0.01
        return 1000.0

    @staticmethod
    def _edge_candidates_near_point(graph: RoadGraph, x: float, y: float) -> Set[Tuple[str, str]]:
        cell_size = graph._edge_index_cell_size
        cx, cy = math.floor(x / cell_size), math.floor(y / cell_size)
        found: Set[Tuple[str, str]] = set()
        max_radius = 64
        for radius in range(max_radius + 1):
            for ix in range(cx - radius, cx + radius + 1):
                for iy in range(cy - radius, cy + radius + 1):
                    if radius and cx - radius < ix < cx + radius and cy - radius < iy < cy + radius:
                        continue
                    found.update(graph._edge_spatial_index.get((ix, iy), []))
            if found:
                return found
        return set(graph.edge_meta.keys())

    @staticmethod
    def _edge_keys_intersecting_square(graph: RoadGraph, x: float, y: float, radius: float) -> Set[Tuple[str, str]]:
        cell_size = graph._edge_index_cell_size
        if not math.isfinite(radius) or radius < 0:
            return set()
        ix0, iy0 = math.floor((x - radius) / cell_size), math.floor((y - radius) / cell_size)
        ix1, iy1 = math.floor((x + radius) / cell_size), math.floor((y + radius) / cell_size)
        keys: Set[Tuple[str, str]] = set()
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                keys.update(graph._edge_spatial_index.get((ix, iy), []))
        return keys

    @staticmethod
    def _edge_bbox(graph: RoadGraph, meta: EdgeMeta) -> Optional[Tuple[float, float, float, float]]:
        if len(meta.geometry) >= 2:
            xs = [float(p["x"]) for p in meta.geometry]
            ys = [float(p["y"]) for p in meta.geometry]
        else:
            a = graph.nodes.get(meta.src)
            b = graph.nodes.get(meta.dst)
            if not a or not b:
                return None
            xs = [a.x, b.x]
            ys = [a.y, b.y]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _project_ratio_on_polyline(poly: List[Dict[str, float]], x: float, y: float) -> Tuple[float, float]:
        total_len = 0.0
        seg_lens: List[float] = []
        for i in range(len(poly) - 1):
            seg_len = math.hypot(poly[i + 1]["x"] - poly[i]["x"], poly[i + 1]["y"] - poly[i]["y"])
            seg_lens.append(seg_len)
            total_len += seg_len
        if total_len <= 1e-6:
            return 0.5, float("inf")
        best_dist = float("inf")
        best_along = 0.0
        acc = 0.0
        for i, seg_len in enumerate(seg_lens):
            if seg_len <= 1e-6:
                continue
            ax, ay = poly[i]["x"], poly[i]["y"]
            bx, by = poly[i + 1]["x"], poly[i + 1]["y"]
            vx, vy = bx - ax, by - ay
            t = ((x - ax) * vx + (y - ay) * vy) / (seg_len * seg_len)
            t = min(1.0, max(0.0, t))
            px = ax + vx * t
            py = ay + vy * t
            dist = math.hypot(x - px, y - py)
            if dist < best_dist:
                best_dist = dist
                best_along = acc + seg_len * t
            acc += seg_len
        return best_along / total_len, best_dist

    @staticmethod
    def _projection_distance_for_edge(graph: RoadGraph, meta: EdgeMeta, x: float, y: float) -> float:
        poly = meta.geometry
        if len(poly) < 2:
            a = graph.nodes.get(meta.src)
            b = graph.nodes.get(meta.dst)
            if not a or not b:
                return float("inf")
            poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
        _, dist = MapLoader._project_ratio_on_polyline(poly, x, y)
        return dist

    @staticmethod
    def opendrive_to_graph(xodr_path: str, sample_step: float = 40.0) -> RoadGraph:
        """
        Simplified OpenDRIVE adapter: each road is sampled into nodes by s coordinate,
        then consecutive samples are connected bidirectionally.
        """
        graph = RoadGraph()
        root = ET.parse(xodr_path).getroot()

        for road in root.findall("road"):
            road_id = road.attrib.get("id", "r0")
            length = float(road.attrib.get("length", "0"))
            plan_view = road.find("planView")
            if plan_view is None:
                continue

            geoms = plan_view.findall("geometry")
            if not geoms:
                continue

            # Use only the first line geometry as a pragmatic fallback.
            g = geoms[0]
            x0 = float(g.attrib.get("x", "0"))
            y0 = float(g.attrib.get("y", "0"))
            hdg = float(g.attrib.get("hdg", "0"))
            lane_width = 3.6
            lane_left = len(road.findall("./lanes/laneSection/left/lane"))
            lane_right = len(road.findall("./lanes/laneSection/right/lane"))
            width_elem = road.find("./lanes/laneSection/right/lane/width")
            if width_elem is not None:
                lane_width = float(width_elem.attrib.get("a", lane_width))
            speed_limit_mps = 8.0
            speed_elem = road.find("./type/speed")
            if speed_elem is not None:
                max_v = float(speed_elem.attrib.get("max", "8.0"))
                unit = speed_elem.attrib.get("unit", "m/s")
                if unit == "km/h":
                    speed_limit_mps = max_v / 3.6
                else:
                    speed_limit_mps = max_v
            total_width = lane_width * max(2, lane_left + lane_right)

            sample_count = max(2, int(length / sample_step) + 1)
            prev_node = None
            for i in range(sample_count):
                s = min(length, i * sample_step)
                x = x0 + s * math.cos(hdg)
                y = y0 + s * math.sin(hdg)
                node_id = f"{road_id}_{i}"
                graph.add_node(node_id, x, y)
                if prev_node is not None:
                    graph.add_bidirectional_edge(
                        prev_node,
                        node_id,
                        sample_step,
                        width=total_width,
                        lanes_forward=max(1, lane_right),
                        lanes_backward=max(1, lane_left),
                        speed_limit_mps=speed_limit_mps,
                    )
                prev_node = node_id

        return graph

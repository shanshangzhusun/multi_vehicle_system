from __future__ import annotations

import argparse
import json
import math
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from mvs.common.event_log import EventLogger
from mvs.common.depot_assignment import score_depot_vehicles
from mvs.common.lane_graph import LaneGraph
from mvs.common.models import Envelope, utc_now_iso
from mvs.common.platform_interfaces import (
    MSG_DEPOT_DIAN,
    MSG_DEPOT_CONTEXT,
    MSG_DEPOT_ASSIGN_REQUEST,
    MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
    MSG_DEPOT_ASSIGNMENT_RESULT,
    MSG_DEPOT_SCORE_RESPONSE,
    MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
    MSG_DEPOT_VEHICLE_SCORE_RESULT,
    MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT,
    MSG_REQUEST_DEPOT_SCORE,
    MSG_FA_SHE_DIAN,
    MSG_VEHICLE_DIAN,
    MSG_YIN_BI_DIAN,
    MSG_ZHU_BEI_DIAN,
    MSG_ZHU_BEI_CONTEXT,
    MSG_ZHU_BEI_KU_DIAN,
    correlation_fields,
    reply_address,
)
from mvs.common.scoring import DepotScorer, DepotState, score_depot_task_execution
from mvs.common.transport import MessageAddress, create_transport_node, normalize_transport_config
from mvs.scheduler.map_model import MapLoader


class DepotApp:
    def __init__(self, config_path: str) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.node_id = str(self.cfg.get("node_id", "depot_platform"))
        self.depot_id = str(self.cfg.get("depot_id") or self.cfg.get("listen_port") or self.node_id)
        self.listen_host = str(self.cfg.get("listen_host", "0.0.0.0"))
        self.listen_port = int(self.cfg.get("listen_port", 9130))
        self.model_message_type_tag = str(self.cfg.get("model_message_type_tag") or self.cfg.get("group") or "")
        self.depot_index = max(0, int(self.cfg.get("depot_index", 0) or 0))
        self.transport_cfg = normalize_transport_config(self.cfg.get("transport"))
        self.event_log = EventLogger(
            path=self.cfg.get("event_log_path", "logs/depot_events.jsonl"),
            node_id=self.node_id,
        )
        self.message_capture_cfg = dict(self.cfg.get("message_capture", {}))
        self.message_capture_enabled = bool(self.message_capture_cfg.get("enabled", False))
        self.message_capture_dir = Path(
            self.message_capture_cfg.get("dir", f"result/message_capture/depot/{self.depot_id}")
        )
        self._message_capture_seq = 0
        self._message_capture_lock = threading.Lock()
        self.graph = MapLoader.load_graph_from_config(self.cfg["map"])
        self.points = MapLoader.load_points_from_config(self.cfg["map"], self.graph)
        managed_depots = self.cfg.get("depot_ids")
        if managed_depots is None and self.cfg.get("depot_id"):
            managed_depots = [self.cfg.get("depot_id")]
        if isinstance(managed_depots, list) and managed_depots:
            wanted = {str(item) for item in managed_depots if item not in {None, ""}}
            self.points.depots = [depot_id for depot_id in self.points.depots if depot_id in wanted]
        self.lane_graph = LaneGraph(self.graph)
        self.lane_graph.set_cache_limit(int(self.cfg.get("lane_graph_cache_limit", 32768)))
        self.reload_duration_sec = float(self.cfg.get("reload_duration_sec", 60.0))
        self.default_capacity = int(self.cfg.get("depot_capacity", 16))
        self.ammo_capacity = int(self.cfg.get("ammo_capacity", self.cfg.get("resource_limit", 20)) or 20)
        self.resource_limit = self.ammo_capacity
        self.scorer = DepotScorer(
            graph=self.graph,
            points=self.points,
            lane_graph=self.lane_graph,
            reload_duration_sec=self.reload_duration_sec,
            default_capacity=self.default_capacity,
        )
        self.assignment_context: Dict[str, Any] = {}
        self.runtime_dian: Dict[str, List[Dict[str, Any]]] = {
            "launch": [],
            "hide": [],
            "vehicle": [],
            "depot": [],
        }
        self.runtime_node_by_public_id: Dict[str, str] = {}
        self.depot_states: Dict[str, DepotState] = self._load_depot_states()
        self._lock = threading.Lock()
        self._running = False
        self.depot_model_cfg = dict(self.cfg.get("depot_model", {}))
        self._depot_context_thread: Optional[threading.Thread] = None
        self.transport = create_transport_node(
            node_id=self.node_id,
            host=self.listen_host,
            port=self.listen_port,
            on_message=self.on_message,
            cfg=self.transport_cfg,
        )

    def _load_depot_states(self) -> Dict[str, DepotState]:
        raw = self.cfg.get("depot_states", {}) or {}
        states: Dict[str, DepotState] = {}
        for depot_id in self.points.depots:
            row = dict(raw.get(depot_id, {}))
            states[depot_id] = DepotState(
                depot_id=depot_id,
                capacity=int(row.get("capacity", self.default_capacity)),
                queue_count=int(row.get("queue_count", 0)),
                occupied=bool(row.get("occupied", False)),
                wait_prep_sec=float(row.get("wait_prep_sec", 0.0)),
                supported_ammo_types=list(row.get("supported_ammo_types", [])) or None,
            )
        return states

    def start(self) -> None:
        self._running = True
        self.transport.start()
        self._start_depot_context_requester()

    def stop(self) -> None:
        self._running = False
        self.transport.stop()

    def run_forever(self) -> None:
        self.start()
        print(f"[Depot] started at {self.listen_host}:{self.listen_port}", flush=True)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def on_message(self, env: Envelope, addr: MessageAddress) -> None:
        with self._lock:
            payload = env.payload or {}
            self._capture_message(
                "recv",
                env.msg_type,
                payload,
                addr=addr,
                sender=env.sender,
                target=env.target,
                group=payload.get("_group") if isinstance(payload, dict) else None,
            )
            self.event_log.log(
                "depot_message_received",
                msg_type=env.msg_type,
                from_addr=f"{addr[0]}:{addr[1]}" if addr else None,
                payload_keys=sorted(payload.keys()) if isinstance(payload, dict) else [],
                group=payload.get("_group") if isinstance(payload, dict) else None,
            )
            incoming_group = str((env.payload or {}).get("_group") or "")
            if incoming_group and self.model_message_type_tag and incoming_group != self.model_message_type_tag:
                self.event_log.log(
                    "message_group_ignored",
                    msg_type=env.msg_type,
                    incoming_group=incoming_group,
                    expected_group=self.model_message_type_tag,
                )
                return
            if env.msg_type == MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT:
                self._on_assignment_context(env)
            elif env.msg_type in {MSG_REQUEST_DEPOT_SCORE, MSG_DEPOT_CONTEXT, MSG_ZHU_BEI_CONTEXT}:
                self._on_score_request(env, addr)
            elif env.msg_type == MSG_DEPOT_VEHICLE_SCORE_CONTEXT:
                self._on_vehicle_score_context(env, addr)
            elif env.msg_type == MSG_DEPOT_ASSIGN_REQUEST:
                self._on_assign_request(env, addr)
            elif env.msg_type == MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT:
                self._on_context_request(env, addr)
            elif env.msg_type in {
                MSG_FA_SHE_DIAN,
                MSG_YIN_BI_DIAN,
                MSG_VEHICLE_DIAN,
                MSG_DEPOT_DIAN,
                MSG_ZHU_BEI_DIAN,
                MSG_ZHU_BEI_KU_DIAN,
            }:
                self._on_dian_context(env)
            else:
                self.event_log.log(
                    "depot_message_ignored",
                    msg_type=env.msg_type,
                    reason="unsupported_msg_type",
                    expected=[
                        MSG_REQUEST_DEPOT_SCORE,
                        MSG_DEPOT_CONTEXT,
                        MSG_ZHU_BEI_CONTEXT,
                        MSG_DEPOT_ASSIGN_REQUEST,
                        MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT,
                        MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
                        MSG_FA_SHE_DIAN,
                        MSG_YIN_BI_DIAN,
                        MSG_VEHICLE_DIAN,
                        MSG_DEPOT_DIAN,
                        MSG_ZHU_BEI_DIAN,
                        MSG_ZHU_BEI_KU_DIAN,
                        MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
                    ],
                )

    def _capture_message(
        self,
        direction: str,
        msg_type: str,
        payload: Dict[str, Any],
        *,
        addr: MessageAddress = None,
        sender: Optional[str] = None,
        target: Optional[str] = None,
        group: Optional[str] = None,
    ) -> None:
        if not self.message_capture_enabled:
            return
        try:
            with self._message_capture_lock:
                self._message_capture_seq += 1
                safe_msg_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(msg_type or "UNKNOWN"))
                out_dir = self.message_capture_dir / direction
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{self._message_capture_seq:06d}_{safe_msg_type}.json"
                row = {
                    "ts": utc_now_iso(),
                    "node_type": "depot",
                    "node_id": self.node_id,
                    "depot_id": self.depot_id,
                    "direction": direction,
                    "msg_type": msg_type,
                    "sender": sender,
                    "target": target,
                    "group": group,
                    "addr": f"{addr[0]}:{addr[1]}" if addr else None,
                    "payload": payload,
                }
                out_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _normalize_dian_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("rows", "points", "depots", "vehicles"):
                if isinstance(data.get(key), list):
                    return [dict(row) for row in data[key] if isinstance(row, dict)]
            return [dict(data)]
        return []

    @staticmethod
    def _row_public_id(row: Dict[str, Any], fallback: str) -> str:
        return str(
            row.get("id")
            or row.get("point_id")
            or row.get("name")
            or row.get("vehicle_id")
            or row.get("port")
            or row.get("index")
            or fallback
        )

    def _graph_xy_looks_lonlat(self) -> bool:
        if not self.graph.nodes:
            return False
        xs = [node.x for node in self.graph.nodes.values()]
        ys = [node.y for node in self.graph.nodes.values()]
        return min(xs) >= -180.0 and max(xs) <= 180.0 and min(ys) >= -90.0 and max(ys) <= 90.0

    def _row_xy(self, row: Dict[str, Any]) -> Optional[tuple[float, float]]:
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        lat = row.get("lat", row.get("latitude"))
        if lon is not None and lat is not None:
            lon_f, lat_f = float(lon), float(lat)
            if self._graph_xy_looks_lonlat():
                return lon_f, lat_f
            samples = [
                (float(node.lon), float(node.lat), float(node.x), float(node.y))
                for node in self.graph.nodes.values()
                if node.lon is not None and node.lat is not None
            ]
            if len(samples) >= 2:
                def fit(src: List[float], dst: List[float]) -> tuple[float, float]:
                    src_mean = sum(src) / len(src)
                    dst_mean = sum(dst) / len(dst)
                    variance = sum((value - src_mean) ** 2 for value in src)
                    scale = sum((a - src_mean) * (b - dst_mean) for a, b in zip(src, dst)) / max(variance, 1e-12)
                    return scale, dst_mean - scale * src_mean

                sx, bx = fit([row[0] for row in samples], [row[2] for row in samples])
                sy, by = fit([row[1] for row in samples], [row[3] for row in samples])
                return sx * lon_f + bx, sy * lat_f + by
        if row.get("x") is not None and row.get("y") is not None:
            return float(row["x"]), float(row["y"])
        return None

    def _project_rows(self, rows: List[Dict[str, Any]], prefix: str) -> List[str]:
        projected: List[str] = []
        for index, row in enumerate(rows):
            xy = self._row_xy(row)
            if xy is None:
                continue
            public_id = self._row_public_id(row, f"{prefix}_{index:03d}")
            safe_id = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in public_id)
            node_id = f"{prefix}_{safe_id}"
            if node_id not in self.graph.nodes:
                base_from = row.get("base_projection_from_node") or row.get("projection_from_node")
                base_to = row.get("base_projection_to_node") or row.get("projection_to_node")
                base_ratio = row.get("base_projection_ratio")
                if base_ratio in {None, ""}:
                    base_ratio = row.get("projection_ratio")
                if base_from not in {None, ""} and base_to not in {None, ""} and base_ratio not in {None, ""}:
                    try:
                        self.graph.add_point_on_edge(
                            node_id,
                            str(base_from),
                            str(base_to),
                            float(base_ratio),
                            x=float(row["projected_x"]) if row.get("projected_x") not in {None, ""} else None,
                            y=float(row["projected_y"]) if row.get("projected_y") not in {None, ""} else None,
                        )
                    except (KeyError, ValueError):
                        pass
                if node_id in self.graph.nodes:
                    self.runtime_node_by_public_id[public_id] = node_id
                    for key in ("id", "point_id", "name", "index", "vehicle_id", "port"):
                        value = row.get(key)
                        if value not in {None, ""}:
                            self.runtime_node_by_public_id[str(value)] = node_id
                    projected.append(node_id)
                    continue
                nearest = MapLoader._nearest_edge_projection(self.graph, xy[0], xy[1])
                if nearest is None:
                    continue
                edge, ratio = nearest
                try:
                    poly = list(edge.geometry or [])
                    if len(poly) < 2:
                        a = self.graph.nodes[edge.src]
                        b = self.graph.nodes[edge.dst]
                        poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
                    projected_x, projected_y = self.graph._point_at_polyline_ratio(poly, ratio)
                    oriented_ratio = float(ratio)
                    if edge.geometry and not (edge.geom_from == edge.src and edge.geom_to == edge.dst):
                        oriented_ratio = 1.0 - oriented_ratio
                    self.graph.add_point_on_edge(
                        node_id,
                        edge.src,
                        edge.dst,
                        oriented_ratio,
                        x=projected_x,
                        y=projected_y,
                    )
                except (KeyError, ValueError):
                    continue
            self.runtime_node_by_public_id[public_id] = node_id
            for key in ("id", "point_id", "name", "index", "vehicle_id", "port"):
                value = row.get(key)
                if value not in {None, ""}:
                    self.runtime_node_by_public_id[str(value)] = node_id
            projected.append(node_id)
        return projected

    def _on_dian_context(self, env: Envelope) -> None:
        rows = self._normalize_dian_rows(env.payload or {})
        if env.msg_type == MSG_FA_SHE_DIAN:
            self.runtime_dian["launch"] = rows
            self.points.launch_points = self._project_rows(rows, "launch")
        elif env.msg_type == MSG_YIN_BI_DIAN:
            self.runtime_dian["hide"] = rows
            self.points.hide_points = self._project_rows(rows, "hide")
        elif env.msg_type == MSG_VEHICLE_DIAN:
            self.runtime_dian["vehicle"] = rows
            self._project_rows(rows, "vehicle")
        else:
            # 任务书“贮备库位置、容量输入”：每个贮备库进程只管理自己 depot_index 对应的点。
            # 若外部点位带 capacity，就覆盖默认容量；不带则沿用配置中的 depot_capacity。
            self.runtime_dian["depot"] = rows
            all_depots = self._project_rows(rows, "depot")
            self.points.depots = [all_depots[self.depot_index]] if self.depot_index < len(all_depots) else []
            if self.depot_index < len(rows):
                runtime_capacity = rows[self.depot_index].get("capacity")
                if runtime_capacity not in {None, ""}:
                    self.default_capacity = max(0, int(runtime_capacity))
            self.depot_states = self._load_depot_states()
        self.lane_graph = LaneGraph(self.graph)
        self.lane_graph.set_cache_limit(int(self.cfg.get("lane_graph_cache_limit", 32768)))
        self.scorer = DepotScorer(
            graph=self.graph,
            points=self.points,
            lane_graph=self.lane_graph,
            reload_duration_sec=self.reload_duration_sec,
            default_capacity=self.default_capacity,
        )
        self.event_log.log(
            "depot_dian_context_received",
            msg_type=env.msg_type,
            group=self.model_message_type_tag,
            rows=len(rows),
            managed_depots=list(self.points.depots),
            capacity=self.default_capacity,
        )

    def _resolve_runtime_node(self, *values: Any) -> Optional[str]:
        for value in values:
            if value in {None, ""}:
                continue
            text = str(value)
            if text in self.graph.nodes:
                return text
            mapped = self.runtime_node_by_public_id.get(text)
            if mapped in self.graph.nodes:
                return mapped
        return None

    def _on_vehicle_score_context(self, env: Envelope, addr: MessageAddress) -> None:
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        requested_depot_id = str(payload.get("depot_id") or self.depot_id)
        requested_port = str(payload.get("depot_port") or "")
        if (
            requested_depot_id not in {str(self.depot_id), str(self.node_id)}
            and requested_port != str(self.listen_port)
        ):
            self.event_log.log(
                "depot_vehicle_score_context_ignored",
                task_id=task_id,
                requested_depot_id=requested_depot_id,
                depot_id=self.depot_id,
            )
            return
        depot_node = self._resolve_runtime_node(
            payload.get("depot_node"),
            payload.get("depot_id"),
            payload.get("depot_port"),
            self.depot_id,
        )
        if depot_node is None and self.points.depots:
            depot_node = self.points.depots[0]
        assignments = [dict(row) for row in (payload.get("assignments") or []) if isinstance(row, dict)]
        weights = dict(payload.get("score_weights") or {})
        distance_metric = str(weights.get("distance_metric", "euclidean")).strip().lower()
        if distance_metric not in {"euclidean", "road_network"}:
            distance_metric = "euclidean"
        distances: List[tuple[str, float]] = []
        assignment_by_vehicle: Dict[str, Dict[str, Any]] = {}
        unreachable: List[str] = []
        resource_mismatch: List[str] = []
        # 任务书“贮备库类型匹配冲突”：支持字段存在时启用过滤。
        # 服务器当前若不下发 supported_ammo_types，本分支为空，不改变原有距离评分结果。
        supported_ammo_types = set(
            str(item)
            for item in (
                payload.get("supported_ammo_types")
                or self.depot_states.get(depot_node or "", DepotState(depot_id=self.depot_id)).supported_ammo_types
                or []
            )
            if item not in {None, ""}
        )
        for row in assignments:
            vehicle_id = str(row.get("vehicle_id") or "")
            required_ammo_type = row.get("required_ammo_type") or row.get("ammo_type")
            if supported_ammo_types and required_ammo_type not in {None, ""} and str(required_ammo_type) not in supported_ammo_types:
                if vehicle_id:
                    resource_mismatch.append(vehicle_id)
                continue
            launch_node = self._resolve_runtime_node(
                row.get("launch_node"),
                row.get("fire_point_id"),
                row.get("launch_name"),
            )
            if not vehicle_id or launch_node is None or depot_node is None:
                if vehicle_id:
                    unreachable.append(vehicle_id)
                continue
            if distance_metric == "road_network":
                _path, distance_m = self.graph.shortest_path(launch_node, depot_node)
            else:
                launch = self.graph.nodes[launch_node]
                depot = self.graph.nodes[depot_node]
                distance_m = math.hypot(float(launch.x) - float(depot.x), float(launch.y) - float(depot.y))
            if not math.isfinite(distance_m):
                unreachable.append(vehicle_id)
                continue
            distances.append((vehicle_id, distance_m))
            assignment_by_vehicle[vehicle_id] = row
        # 本库只负责对“已选车辆-发射点”集合打本库分数。
        # 分数使用 a*距离 + b*距离排名；调度端会汇总所有库的分数矩阵后再做全局分配。
        distance_weight = float(weights.get("distance_weight_a", 1.0) or 0.0)
        rank_weight = float(weights.get("distance_rank_weight_b", 100.0) or 0.0)
        # 贮备库两阶段评分实际调用点：每个库独立计算本库到所有已选车辆的分数行。
        # 输出 scores 后由调度端 greedy_capacity_assignment 合成为全局容量分配结果。
        scores = score_depot_vehicles(
            distances,
            distance_weight=distance_weight,
            rank_weight=rank_weight,
        )
        for row in scores:
            source = assignment_by_vehicle.get(str(row.get("vehicle_id")), {})
            row["subtask_id"] = source.get("subtask_id")
            row["launch_node"] = source.get("launch_node")
            row["fire_point_id"] = source.get("fire_point_id")
        capacity = max(0, int(payload.get("capacity", self.default_capacity) or 0))
        response_addr = reply_address(payload, addr)
        if response_addr is None:
            self.event_log.log(
                "depot_vehicle_score_result_failed",
                task_id=task_id,
                reason="missing_reply_address",
            )
            return
        result = {
            # 返回容量和完整分数数组，调度端据此执行“取全局最小、删车辆列、扣库容量”。
            # 这里不直接决定某车用哪个库，保证贮备库只评分、不做全局决策。
            "task_id": task_id,
            "depot_id": requested_depot_id,
            "depot_node": payload.get("depot_node") or depot_node,
            "depot_port": int(payload.get("depot_port") or self.listen_port),
            "capacity": capacity,
            "ammo_capacity": self.ammo_capacity,
            "resource_limit": self.resource_limit,
            "distance_weight_a": distance_weight,
            "distance_rank_weight_b": rank_weight,
            "distance_metric": distance_metric,
            "scores": scores,
            "unreachable_vehicle_ids": unreachable,
            "resource_mismatch_vehicle_ids": resource_mismatch,
        }
        self._send_raw_json(MSG_DEPOT_VEHICLE_SCORE_RESULT, result, response_addr)
        self.event_log.log(
            "depot_vehicle_score_result_sent",
            task_id=task_id,
            depot_id=requested_depot_id,
            capacity=capacity,
            score_count=len(scores),
            unreachable_count=len(unreachable),
            resource_mismatch_count=len(resource_mismatch),
            distance_metric=distance_metric,
            addr=f"{response_addr[0]}:{response_addr[1]}",
        )

    def _on_assignment_context(self, env: Envelope) -> None:
        self.assignment_context = self._normalize_assignment_context(env.payload or {})
        self.event_log.log(
            "depot_assignment_context_received",
            task_id=self.assignment_context.get("task_id"),
            wave_id=self.assignment_context.get("wave_id"),
            assignments=len(self._assignment_rows()),
        )

    def _on_context_request(self, env: Envelope, addr: MessageAddress) -> None:
        self.event_log.log(
            "depot_context_request_served",
            task_id=self.assignment_context.get("task_id"),
            wave_id=self.assignment_context.get("wave_id"),
            assignments=len(self._assignment_rows()),
        )
        self._send_response(
            MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
            env,
            self._build_assignment_context_response(),
            addr,
        )

    def _start_depot_context_requester(self) -> None:
        cfg = self.depot_model_cfg
        if not bool(cfg.get("enabled", False)):
            return
        self._depot_context_thread = threading.Thread(
            target=self._depot_context_request_loop,
            daemon=True,
        )
        self._depot_context_thread.start()

    def _depot_context_request_loop(self) -> None:
        cfg = self.depot_model_cfg
        host = str(cfg.get("host", "127.0.0.1"))
        port = int(cfg.get("port", 9150))
        interval_sec = float(cfg.get("request_interval_sec", 0.0))
        initial_delay_sec = float(cfg.get("initial_delay_sec", 0.5))
        target = str(cfg.get("target", "depot_model"))
        task_id = cfg.get("task_id")
        wave_id = cfg.get("wave_id")
        if initial_delay_sec > 0:
            time.sleep(initial_delay_sec)
        while self._running:
            request_id = str(uuid.uuid4())
            payload = {
                "request_id": request_id,
                "reply_host": self.listen_host if self.listen_host != "0.0.0.0" else "127.0.0.1",
                "reply_port": self.listen_port,
                "response_require_ack": bool(cfg.get("response_require_ack", False)),
            }
            if task_id:
                payload["task_id"] = task_id
            if wave_id:
                payload["wave_id"] = wave_id
            try:
                self._send_raw_json(MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT, payload, (host, port))
                self.event_log.log(
                    "depot_assignment_context_requested",
                    request_id=request_id,
                    target=f"{host}:{port}",
                    task_id=task_id,
                    wave_id=wave_id,
                )
            except Exception as exc:
                self.event_log.log(
                    "depot_assignment_context_request_failed",
                    request_id=request_id,
                    target=f"{host}:{port}",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if interval_sec <= 0:
                return
            time.sleep(interval_sec)

    def _on_score_request(self, env: Envelope, addr: MessageAddress) -> None:
        resolved_payload, result = self._score_from_payload(env.payload or {})
        task_book_result = self._build_task_book_depot_result(resolved_payload, result, mode="score")
        self.event_log.log(
            "depot_score_computed",
            task_id=resolved_payload.get("task_id"),
            subtask_id=resolved_payload.get("subtask_id"),
            vehicle_id=resolved_payload.get("vehicle_id"),
            launch_node=resolved_payload.get("launch_node") or resolved_payload.get("target_node"),
            ammo_type=resolved_payload.get("ammo_type"),
            selected_depot=result.get("selected_depot"),
            depot_candidates=len(result.get("depot_scores") or []),
            task_book_selected_depot=task_book_result.get("selected_depot"),
        )
        self._send_response(
            MSG_DEPOT_SCORE_RESPONSE,
            env,
            {
                "depot_score_result": result,
                "task_book_depot_score_result": task_book_result,
            },
            addr,
        )

    def _on_assign_request(self, env: Envelope, addr: MessageAddress) -> None:
        resolved_payload = dict(env.payload or {})
        provided_result = resolved_payload.get("depot_score_result") if isinstance(resolved_payload.get("depot_score_result"), dict) else None
        if provided_result:
            result = dict(provided_result)
        else:
            resolved_payload, result = self._score_from_payload(resolved_payload)
        task_book_result = self._build_task_book_depot_result(resolved_payload, result, mode="assign")
        self.event_log.log(
            "depot_assignment_computed",
            task_id=resolved_payload.get("task_id"),
            subtask_id=resolved_payload.get("subtask_id"),
            vehicle_id=resolved_payload.get("vehicle_id"),
            launch_node=resolved_payload.get("launch_node") or resolved_payload.get("target_node"),
            ammo_type=resolved_payload.get("ammo_type"),
            selected_depot=result.get("selected_depot"),
            depot_candidates=len(result.get("depot_scores") or []),
            score_source="provided_score" if provided_result else "computed",
            task_book_selected_depot=task_book_result.get("selected_depot"),
        )
        self._send_response(
            MSG_DEPOT_ASSIGNMENT_RESULT,
            env,
            {
                "depot_score_result": result,
                "selected_depot": result.get("selected_depot"),
                "task_book_depot_assignment_result": task_book_result,
            },
            addr,
        )

    def _score_from_payload(self, payload: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        merged_payload = dict(payload or {})
        launch_node = str(merged_payload.get("fire_point_id") or merged_payload.get("launch_node") or merged_payload.get("target_node") or "")
        launch_node = self.runtime_node_by_public_id.get(launch_node, launch_node)
        if not launch_node:
            assignment = self._find_assignment(merged_payload)
            launch_node = str(assignment.get("fire_point_id") or assignment.get("launch_node") or assignment.get("assigned_launch_point") or "")
            merged_payload = {**assignment, **merged_payload}
        if not launch_node:
            return merged_payload, self._score_depot_readiness(merged_payload)
        result = self.scorer.score_depots(
            launch_node=launch_node,
            vehicle_id=merged_payload.get("vehicle_id"),
            ammo_type=merged_payload.get("ammo_type"),
            vehicle_node=merged_payload.get("vehicle_node") or merged_payload.get("current_node"),
            speed_mps=float(merged_payload.get("speed_mps", 8.0)),
            depot_states=self._states_from_payload(merged_payload) or self.depot_states,
            candidate_depots=merged_payload.get("candidate_depots"),
            next_node=merged_payload.get("next_node") or merged_payload.get("home_node"),
        )
        return merged_payload, result

    def _score_depot_readiness(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 任务书“贮备库流程调度/行动模拟”的轻量状态口径：
        # 用容量、队列、占用、等待和资源匹配给出本库当前可服务能力。
        depot_node = self.points.depots[0] if self.points.depots else ""
        state = self.depot_states.get(
            depot_node,
            DepotState(depot_id=depot_node or self.depot_id, capacity=self.default_capacity),
        )
        capacity = max(1, int(state.capacity))
        occupied_count = 1 if state.occupied else 0
        busy_ratio = min(1.0, max(0.0, (int(state.queue_count) + occupied_count) / capacity))
        wait_sec = max(0.0, float(state.wait_prep_sec))
        wait_sec += max(0, int(state.queue_count)) * self.reload_duration_sec
        if state.occupied:
            wait_sec += self.reload_duration_sec
        score_wait = max(0.0, 1.0 - wait_sec / 1800.0)
        score_busy = max(0.0, 1.0 - busy_ratio)
        ammo_type = payload.get("ammo_type")
        supported = set(state.supported_ammo_types or [])
        resource_ok = ammo_type is None or not supported or ammo_type in supported
        score_resource = 1.0 if resource_ok else 0.0
        total = 100.0 * (0.45 * score_wait + 0.35 * score_busy + 0.20 * score_resource)
        public_depot_id = str(payload.get("depot_id") or payload.get("port") or self.depot_id)
        row = {
            "depot_id": public_depot_id,
            "depot_node": depot_node or public_depot_id,
            "depot_name": payload.get("name"),
            "vehicle_id": payload.get("vehicle_id"),
            "launch_node": "",
            "ammo_type": ammo_type,
            "feasible": bool(resource_ok),
            "travel_sec_from_launch": 0.0,
            "distance_m_from_launch": 0.0,
            "estimated_wait_sec": round(wait_sec, 3),
            "reload_sec": round(self.reload_duration_sec, 3),
            "queue_count": int(state.queue_count),
            "capacity": capacity,
            "occupied": bool(state.occupied),
            "score_travel": 0.0,
            "score_wait": round(score_wait * 100.0, 3),
            "score_busy": round(score_busy * 100.0, 3),
            "score_resource": round(score_resource * 100.0, 3),
            "score_next": 0.0,
            "score_total": round(total, 3),
            "score_semantics": "depot_self_readiness_without_task_route",
        }
        return {
            "vehicle_id": payload.get("vehicle_id"),
            "launch_node": "",
            "ammo_type": ammo_type,
            "depot_scores": [row],
            "selected_depot": public_depot_id if resource_ok else None,
            "ranking_note": "No launch task was supplied; score uses depot capacity, queue, wait and resource readiness.",
        }

    def _states_from_payload(self, payload: Dict[str, Any]) -> Dict[str, DepotState]:
        raw = payload.get("depot_states")
        if not isinstance(raw, dict):
            return {}
        states: Dict[str, DepotState] = {}
        for depot_id, row in raw.items():
            if not isinstance(row, dict):
                continue
            queue_ids = row.get("queue_vehicle_ids") if isinstance(row.get("queue_vehicle_ids"), list) else []
            queue_count = int(row.get("queue_count", len(queue_ids)) or 0)
            states[str(depot_id)] = DepotState(
                depot_id=str(depot_id),
                capacity=int(row.get("capacity", self.default_capacity) or self.default_capacity),
                queue_count=queue_count,
                occupied=bool(row.get("occupied") or row.get("occupied_vehicle_id")),
                wait_prep_sec=float(row.get("wait_prep_sec", 0.0) or 0.0),
                supported_ammo_types=list(row.get("supported_ammo_types", [])) or None,
            )
        return states

    def _find_assignment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        assignments = self._assignment_rows()
        task_id = payload.get("task_id")
        subtask_id = payload.get("subtask_id")
        vehicle_id = payload.get("vehicle_id")
        for row in assignments:
            if task_id and row.get("task_id") != task_id:
                continue
            if subtask_id and row.get("subtask_id") != subtask_id:
                continue
            if vehicle_id and row.get("vehicle_id") != vehicle_id:
                continue
            return dict(row)
        return {}

    def _normalize_assignment_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = dict(payload or {})
        nested = context.get("depot_assignment_context")
        if isinstance(nested, dict):
            context = dict(nested)
        if "point_assignment" in context and "vehicle_assignment_result" not in context:
            context["vehicle_assignment_result"] = context.get("point_assignment")
        if "vehicle_assignment_result" in context and "point_assignment" not in context:
            context["point_assignment"] = context.get("vehicle_assignment_result")
        return context

    def _assignment_rows(self) -> List[Dict[str, Any]]:
        rows = self.assignment_context.get("point_assignment")
        if rows is None:
            rows = self.assignment_context.get("vehicle_assignment_result")
        return list(rows or [])

    def _build_assignment_context_response(self) -> Dict[str, Any]:
        rows = self._assignment_rows()
        context = dict(self.assignment_context)
        context["schema"] = "depot_assignment_context_v1"
        context["point_assignment"] = rows
        context["vehicle_assignment_result"] = rows
        return {
            "depot_assignment_context": context,
            "task_book_depot_assignment_context": {
                "schema": "depot_assignment_context_v1",
                "task_id": context.get("task_id"),
                "wave_id": context.get("wave_id"),
                "assignment_count": len(rows),
                "point_assignment": rows,
                "depot_states": self._export_depot_states(),
            },
        }

    def _export_depot_states(self) -> Dict[str, Dict[str, Any]]:
        exported: Dict[str, Dict[str, Any]] = {}
        for depot_id, state in self.depot_states.items():
            exported[depot_id] = {
                "capacity": int(state.capacity),
                "queue_count": int(state.queue_count),
                "occupied": bool(state.occupied),
                "wait_prep_sec": float(state.wait_prep_sec),
                "supported_ammo_types": list(state.supported_ammo_types or []),
            }
        return exported

    def _build_task_book_depot_result(
        self,
        payload: Dict[str, Any],
        result: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        success_rate_score = float(payload.get("success_rate_score", 100.0) or 100.0)
        scored_rows = list(result.get("depot_scores") or [])
        task_book_rows = [
            score_depot_task_execution(
                depot_score_row=row,
                success_rate_score=success_rate_score,
            )
            for row in scored_rows
        ]
        selected_depot = result.get("selected_depot")
        selected_row = next((row for row in task_book_rows if row.get("depot_id") == selected_depot), None)
        return {
            "schema": f"depot_{mode}_result_v1",
            "task_id": payload.get("task_id"),
            "wave_id": payload.get("wave_id"),
            "subtask_id": payload.get("subtask_id"),
            "vehicle_id": payload.get("vehicle_id"),
            "launch_node": result.get("launch_node"),
            "fire_point_id": payload.get("fire_point_id") or payload.get("launch_node") or payload.get("target_node"),
            "ammo_type": payload.get("ammo_type"),
            "selected_depot": selected_depot,
            "selected_depot_score": selected_row,
            "depot_scores": task_book_rows,
            "ranking_note": (
                "按任务书贮备库评分语义输出：位置、繁忙度、等待、资源匹配、后续衔接、成功率。"
            ),
        }

    def _send_response(self, msg_type: str, env: Envelope, payload: Dict[str, Any], addr: Optional[tuple[str, int]]) -> None:
        response_addr = self._response_address(env, addr)
        if response_addr is None:
            self.event_log.log("query_response_no_reply_address", request_type=env.msg_type, response_type=msg_type)
            return
        response_payload = dict(correlation_fields(env.payload or {}))
        response_payload.update(payload)
        self._send_raw_json(msg_type, response_payload, response_addr)
        self.event_log.log("query_response_sent", request_type=env.msg_type, response_type=msg_type)

    def _response_address(self, env: Envelope, addr: Optional[tuple[str, int]]) -> Optional[tuple[str, int]]:
        # ZHU_BEI_CONTEXT uses msg_ip as the callback host and depot_id as
        # the callback port. depot_id identifies the depot channel here; it is
        # not a vehicle destination.
        if env.msg_type == MSG_ZHU_BEI_CONTEXT:
            payload = env.payload or {}
            host = payload.get("msg_ip")
            port = payload.get("depot_id")
            if host and port:
                return str(host), int(port)
        return reply_address(env.payload or {}, addr)

    def _send_raw_json(self, msg_type: str, payload: Dict[str, Any], addr: tuple[str, int]) -> None:
        wrapped: Dict[str, Any] = {"msg_type": msg_type, "data": payload}
        if self.model_message_type_tag:
            wrapped["group"] = self.model_message_type_tag
        self._capture_message(
            "send",
            msg_type,
            payload,
            addr=addr,
            sender=self.node_id,
            target="model",
            group=wrapped.get("group"),
        )
        raw = json.dumps(wrapped, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.create_connection(addr, timeout=2.0) as sock:
            sock.sendall(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="MVS Depot Platform Simulation")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    DepotApp(args.config).run_forever()


if __name__ == "__main__":
    main()

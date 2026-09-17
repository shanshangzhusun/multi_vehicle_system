from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mvs.common.lane_graph import LaneGraph
from mvs.common.models import Envelope, parse_iso_time, utc_now_iso
from mvs.common.event_log import EventLogger
from mvs.common.debug_append_log import configure_manual_debug, debug_append_log, manual_debug_enabled
from mvs.common.platform_interfaces import (
    MSG_DEPOT_DIAN,
    MSG_FA_SHE_DIAN,
    MSG_DISPATCH_TRAJECTORY_BUNDLE,
    MSG_REQUEST_VEHICLE_SCORE,
    MSG_TIME_BACKPLAN_CONTEXT,
    MSG_VEHICLE_DIAN,
    MSG_VEHICLE_CANDIDATE_CONTEXT,
    MSG_VEHICLE_CANDIDATE_PATH_RESULT,
    MSG_VEHICLE_DEPOT_ASSIGNMENT,
    MSG_VEHICLE_POST_FIRE_PATH_RESULT,
    MSG_VEHICLE_SCORE_RESULT,
    MSG_YIN_BI_DIAN,
    MSG_ZHU_BEI_DIAN,
    MSG_ZHU_BEI_KU_DIAN,
    correlation_fields,
    reply_address,
)
from mvs.common.scoring import score_vehicle_task_execution, vehicle_score
from mvs.common.vehicle_scoring import node_in_bounds, task_book_vehicle_score
from mvs.common.transport import MessageAddress, create_transport_node, normalize_transport_config
from mvs.scheduler.map_model import MapLoader, RoadGraph
from mvs.vehicle.hybrid_astar import HybridAStarConfig, HybridAStarPlanner, TrajectoryPoint
from mvs.vehicle.path_constraints import KinematicPathPlanner, PathConstraintReport, VehicleKinematics
from mvs.vehicle.time_predictor import MLPTimePredictor


@dataclass
class VehicleState:
    vehicle_id: str
    home_node: str
    current_node: str
    status: str
    ammo_types: List[str]
    ammo_count: int


class VehicleApp:
    def __init__(self, config_path: str) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        manual_debug_cfg = self.cfg.get("manual_debug") if isinstance(self.cfg.get("manual_debug"), dict) else {}
        configure_manual_debug(bool(manual_debug_cfg.get("enabled", self.cfg.get("manual_debug_enabled", manual_debug_enabled()))))
        self.vehicle_id = self.cfg["vehicle_id"]
        self.listen_host = self.cfg["listen_host"]
        self.listen_port = int(self.cfg["listen_port"])
        self.advertise_host = self.cfg.get("advertise_host", self.listen_host if self.listen_host != "0.0.0.0" else "127.0.0.1")
        self.advertise_port = int(self.cfg.get("advertise_port", self.listen_port))
        # 候选路径结果先发给模型入口，再由模型转发给调度。
        # 新配置字段使用 candidate_result_host/candidate_result_port；
        # scheduler_host/scheduler_port 只保留为旧配置兼容兜底。
        candidate_result_host = self.cfg.get("candidate_result_host", self.cfg.get("scheduler_host"))
        candidate_result_port = self.cfg.get("candidate_result_port", self.cfg.get("scheduler_port"))
        if candidate_result_host is None or candidate_result_port is None:
            raise KeyError("vehicle config missing candidate_result_host/candidate_result_port")
        self.candidate_result_addr = (str(candidate_result_host), int(candidate_result_port))
        self.transport_cfg = normalize_transport_config(self.cfg.get("transport"))

        self.graph: RoadGraph = MapLoader.load_graph_from_config(self.cfg["map"])
        self.points = MapLoader.load_points_from_config(self.cfg["map"], self.graph)
        self.lane_graph = LaneGraph(self.graph)
        self.lane_graph_cache_limit = int(self.cfg.get("lane_graph_cache_limit", 32768))
        self.lane_graph.set_cache_limit(self.lane_graph_cache_limit)
        self._lane_graph_road_version = int(getattr(self.graph, "_edge_index_version", 0))

        self.speed_mps = float(self.cfg.get("speed_mps", 8.0))
        self.model_bias = float(self.cfg.get("predict_model_bias", 1.05))
        self.hide_strategy_cfg = dict(self.cfg.get("hide_strategy", {}))
        self.hide_trigger_slack_sec = float(self.hide_strategy_cfg.get("trigger_slack_sec", 60.0))
        self.hide_min_wait_sec = float(self.hide_strategy_cfg.get("min_wait_sec", 30.0))
        self.launch_timing_cfg = dict(self.cfg.get("launch_timing", {}))
        self.hot_distance_threshold_m = float(self.launch_timing_cfg.get("hot_distance_threshold_m", 5000.0))
        self.launch_prepare_sec = float(self.launch_timing_cfg.get("launch_prepare_sec", 300.0))
        self.hot_standby_sec = float(
            self.launch_timing_cfg.get("hot_standby_sec", self.launch_timing_cfg.get("hot_startup_sec", 180.0))
        )
        self.cold_standby_sec = float(
            self.launch_timing_cfg.get("cold_standby_sec", self.launch_timing_cfg.get("cold_startup_sec", 420.0))
        )
        # 任务书“热/冷待机、发射准备时间”：车辆端用这些配置判断到发射点前的时间约束。
        # 它们只影响候选路径可行性和评分，不改变网络收发与调度主链路。
        kinematics_cfg = dict(self.cfg.get("kinematics", {}))
        self.max_accel_mps2 = float(kinematics_cfg.get("max_accel_mps2", self.cfg.get("max_accel_mps2", 1.0)))
        self.kinematics = VehicleKinematics(
            width_m=float(kinematics_cfg.get("width_m", 2.8)),
            length_m=float(kinematics_cfg.get("length_m", 7.5)),
            wheelbase_m=float(kinematics_cfg.get("wheelbase_m", 4.2)),
            min_turn_radius_m=float(kinematics_cfg.get("min_turn_radius_m", 12.0)),
            max_steer_deg=float(kinematics_cfg.get("max_steer_deg", 28.0)),
            max_turn_angle_deg=float(kinematics_cfg.get("max_turn_angle_deg", 105.0)),
            min_clearance_m=float(kinematics_cfg.get("min_clearance_m", 0.8)),
            turn_penalty=float(kinematics_cfg.get("turn_penalty", 10.0)),
            max_edge_curvature=float(kinematics_cfg.get("max_edge_curvature", 1.0 / max(1.0, float(kinematics_cfg.get("min_turn_radius_m", 12.0))))),
            max_speed_mps=float(kinematics_cfg.get("max_speed_mps", self.speed_mps)),
        )
        planning_cfg = dict(self.cfg.get("planning", {}))
        self.hybrid_astar_cfg = HybridAStarConfig(
            step_m=float(planning_cfg.get("step_m", 3.0)),
            yaw_resolution_deg=float(planning_cfg.get("yaw_resolution_deg", 10.0)),
            position_resolution_m=float(planning_cfg.get("position_resolution_m", 3.0)),
            max_steer_deg=float(planning_cfg.get("max_steer_deg", self.kinematics.max_steer_deg)),
            steering_samples=int(planning_cfg.get("steering_samples", 7)),
            goal_pos_tolerance_m=float(planning_cfg.get("goal_pos_tolerance_m", 8.0)),
            goal_yaw_tolerance_deg=float(planning_cfg.get("goal_yaw_tolerance_deg", 35.0)),
            reverse_enabled=bool(planning_cfg.get("reverse_enabled", False)),
            steering_change_penalty=float(planning_cfg.get("steering_change_penalty", 1.2)),
            gear_switch_penalty=float(planning_cfg.get("gear_switch_penalty", 8.0)),
            corridor_margin_m=float(planning_cfg.get("corridor_margin_m", 3.0)),
            max_expansions=int(planning_cfg.get("max_expansions", 30000)),
        )
        self.max_hide_candidates = max(0, int(planning_cfg.get("max_hide_candidates", 2)))
        self.max_depot_candidates = max(1, int(planning_cfg.get("max_depot_candidates", 1)))
        self.reload_duration_sec = max(0.0, float(self.cfg.get("reload_duration_sec", 60.0)))
        self.max_candidate_path_options = max(1, int(planning_cfg.get("max_candidate_path_options", 30)))
        self.trajectory_sample_sec = max(0.5, float(planning_cfg.get("trajectory_sample_sec", 10.0)))
        self.candidate_fire_time_grace_sec = max(1.0, float(planning_cfg.get("candidate_fire_time_grace_sec", 15.0)))
        self.candidate_fire_time_early_grace_sec = max(
            0.0,
            float(planning_cfg.get("candidate_fire_time_early_grace_sec", 60.0)),
        )
        self.path_planner = HybridAStarPlanner(self.graph, self.kinematics, self.hybrid_astar_cfg)
        self.coarse_planner = KinematicPathPlanner(self.graph, self.kinematics)
        self.event_log = EventLogger(
            path=self.cfg.get("event_log_path", f"logs/{self.vehicle_id}_events.jsonl"),
            node_id=self.vehicle_id,
        )
        self.timing_log_path = Path(self.cfg.get("timing_log_path", f"logs/{self.vehicle_id}_timing.jsonl"))
        self.time_predictor = MLPTimePredictor(
            model_path=self.cfg.get("time_predict_model_path", ""),
            fallback_bias=self.model_bias,
        )
        self.event_log.log("time_predictor_ready", **self.time_predictor.model_summary())
        self.vehicle_scoring_cfg = dict(self.cfg.get("vehicle_scoring", {}))
        self.vehicle_health = float(self.vehicle_scoring_cfg.get("health", self.cfg.get("health", 1.0)) or 1.0)
        self.fire_pool_bounds = list(self.vehicle_scoring_cfg.get("fire_pool_bounds", []))
        self.fire_pool_bonus = float(self.vehicle_scoring_cfg.get("fire_pool_bonus", 0.0) or 0.0)
        self.vehicle_health_weight = float(self.vehicle_scoring_cfg.get("health_weight", 0.15) or 0.15)

        self.state = VehicleState(
            vehicle_id=self.vehicle_id,
            home_node=self.cfg["home_node"],
            current_node=self.cfg["home_node"],
            status="IDLE",
            ammo_types=list(self.cfg.get("ammo_types", ["HE"])),
            ammo_count=int(self.cfg.get("ammo_capacity", 6)),
        )
        self.ammo_capacity = self.state.ammo_count

        self.transport = create_transport_node(
            node_id=self.vehicle_id,
            host=self.listen_host,
            port=self.listen_port,
            on_message=self.on_message,
            cfg=self.transport_cfg,
        )
        self.message_capture_cfg = dict(self.cfg.get("message_capture", {}))
        self.message_capture_enabled = bool(self.message_capture_cfg.get("enabled", False))
        self.message_capture_dir = Path(
            self.message_capture_cfg.get("dir", f"result/message_capture/vehicle/{self.vehicle_id}")
        )
        self._message_capture_seq = 0

        self._lock = threading.Lock()
        self._running = False
        self.external_vehicle_state: Dict[str, Any] = {}
        self.external_time_backplan_context: Dict[str, Any] = {}
        self.external_candidate_context: Dict[str, Any] = {}
        self.external_raw_launch_rows: List[Dict[str, Any]] = []
        self.external_raw_hide_rows: List[Dict[str, Any]] = []
        self.external_point_node_map: Dict[str, str] = {}
        self.external_dispatch_plan: Dict[str, Any] = {}
        self.external_mission_origin_node = ""
        self.received_dian_capture_cfg = dict(self.cfg.get("received_dian_capture", {}))
        self.received_dian_enabled = bool(
            self.received_dian_capture_cfg.get("enabled", self.cfg.get("received_dian_enabled", True))
        )
        self.received_dian_dir = Path(
            self.received_dian_capture_cfg.get("dir", self.cfg.get("received_dian_dir", "result/received_dian"))
        )
        self._has_external_vehicle_state = False
        self._vehicle_context_score_submitted = False
        self._candidate_paths_submitted_task_id = ""
        self._candidate_paths_inflight_task_ids: Set[str] = set()
        self._external_launch_points_loaded = False
        self._external_hide_points_loaded = False
        self._external_depot_points_loaded = False
        self.external_launch_rows_by_node: Dict[str, Dict[str, Any]] = {}
        self.external_depot_rows_by_node: Dict[str, Dict[str, Any]] = {}
        self.theater_id = str(self.cfg.get("theater_id") or "")
        self.model_message_type_tag = str(self.cfg.get("model_message_type_tag") or self.cfg.get("group") or "")
        if not self.model_message_type_tag:
            theater_key = self.theater_id.strip().lower()
            if theater_key == "henan":
                self.model_message_type_tag = "26D"
            elif theater_key == "guangdong":
                self.model_message_type_tag = "27"
        self.theater_bounds = self._parse_bounds(self.cfg.get("theater_bounds"))
        self.vehicle_context_score_submit_host = str(self.cfg.get("vehicle_context_score_submit_host", "192.168.2.12"))
        self.vehicle_context_score_submit_port_base = int(self.cfg.get("vehicle_context_score_submit_port_base", 8414))
        self._lane_graph_prewarm_cfg = dict(self.cfg.get("lane_graph_prewarm", {}))
        self._lane_graph_prewarm_thread: Optional[threading.Thread] = None
        # Runtime integration mode: keep the local road graph, but do not let local
        # special points participate in planning before external point contexts arrive.
        self.points.launch_points.clear()
        self.points.hide_points.clear()
        self.event_log.log(
            "init_points_loaded",
            launch_points=len(self.points.launch_points),
            hide_points=len(self.points.hide_points),
            home_node=self.state.home_node,
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] init_points_loaded "
            f"launch_points={len(self.points.launch_points)} hide_points={len(self.points.hide_points)} "
            f"home_node={self.state.home_node}"
        )

    def _timing_log(
        self,
        stage: str,
        *,
        started_at: Optional[float] = None,
        elapsed_sec: Optional[float] = None,
        **fields: Any,
    ) -> None:
        try:
            if elapsed_sec is None:
                if started_at is None:
                    return
                elapsed_sec = time.perf_counter() - started_at
            record = {
                "ts": utc_now_iso(),
                "node_id": self.vehicle_id,
                "stage": stage,
                "elapsed_sec": round(float(elapsed_sec), 6),
            }
            record.update(fields)
            self.timing_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.timing_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def start(self) -> None:
        self.transport.start()
        self._running = True
        self._start_lane_graph_prewarm()

    def stop(self) -> None:
        self._running = False
        self.transport.stop()

    def on_message(self, env: Envelope, addr: MessageAddress) -> None:
        try:
            self._capture_message("recv", env.msg_type, env.payload or {}, addr=addr, sender=env.sender, target=env.target)
            payload = env.payload or {}
            payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] on_message msg_type={env.msg_type} "
                f"target={env.target} addr={addr} payload_keys={payload_keys}"
            )
            if not self._message_targets_me(env):
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] ignored msg_type={env.msg_type} target={env.target}"
                )
                return
            if env.msg_type == MSG_REQUEST_VEHICLE_SCORE:
                self._on_vehicle_score_request(env, addr)
            elif env.msg_type == "VEHICLE_CONTEXT":
                if self._is_vehicle_candidate_context_payload(env.payload or {}):
                    self._on_vehicle_candidate_context(env)
                else:
                    self._on_external_vehicle_state_result(env)
            elif env.msg_type == MSG_VEHICLE_DIAN:
                self._on_external_vehicle_state_result(env)
            elif env.msg_type in {MSG_FA_SHE_DIAN, MSG_YIN_BI_DIAN}:
                self._on_dian_context(env)
            elif env.msg_type in {MSG_DEPOT_DIAN, MSG_ZHU_BEI_DIAN, MSG_ZHU_BEI_KU_DIAN}:
                self._on_depot_dian_context(env)
            elif env.msg_type == MSG_TIME_BACKPLAN_CONTEXT:
                self._on_time_backplan_context(env)
            elif env.msg_type == MSG_VEHICLE_CANDIDATE_CONTEXT:
                self._on_vehicle_candidate_context(env)
            elif env.msg_type == MSG_VEHICLE_DEPOT_ASSIGNMENT:
                self._on_vehicle_depot_assignment(env, addr)
            elif env.msg_type == MSG_DISPATCH_TRAJECTORY_BUNDLE:
                self._on_dispatch_trajectory_bundle(env)
        except Exception as exc:
            subtask_id = None
            if isinstance(env.payload, dict):
                subtask_id = env.payload.get("subtask_id")
            detail = f"{type(exc).__name__}: {exc}"
            print(
                f"[Vehicle {self.vehicle_id}] message error msg_type={env.msg_type} "
                f"subtask={subtask_id or 'n/a'} detail={detail}"
            )
            self.event_log.log(
                "message_error",
                msg_type=env.msg_type,
                subtask_id=subtask_id,
                detail=detail,
            )

    def _message_targets_me(self, env: Envelope) -> bool:
        if env.msg_type != MSG_REQUEST_VEHICLE_SCORE:
            return True
        if not env.target:
            return True
        if env.target not in {self.vehicle_id, "all_vehicles", "*"}:
            return False
        payload = env.payload if isinstance(env.payload, dict) else {}
        assigned_vehicle_id = payload.get("assigned_vehicle_id") or payload.get("vehicle_id")
        if assigned_vehicle_id and assigned_vehicle_id != self.vehicle_id:
            return False
        return True

    def _reply_to_request(self, msg_type: str, env: Envelope, payload: Dict[str, Any], addr: MessageAddress) -> None:
        response_addr = reply_address(env.payload or {}, addr)
        payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] reply_prepare request_type={env.msg_type} response_type={msg_type} "
            f"response_addr={response_addr} payload_keys={payload_keys}"
        )
        if response_addr is None:
            self.event_log.log("query_response_no_reply_address", request_type=env.msg_type, response_type=msg_type)
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] reply_no_addr request_type={env.msg_type}"
            )
            return
        response_payload = dict(correlation_fields(env.payload or {}))
        response_payload.update(payload)
        if msg_type == MSG_VEHICLE_SCORE_RESULT:
            response_payload = {
                "port": response_payload.get("port") or response_payload.get("vehicle_id") or self.advertise_port,
                "score_total": response_payload.get("score_total", 0.0),
            }
        self._send_raw_json(msg_type, response_payload, response_addr)
        response_keys = sorted(response_payload.keys()) if isinstance(response_payload, dict) else []
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] reply_sent request_type={env.msg_type} response_type={msg_type} "
            f"to={response_addr} payload_keys={response_keys}"
        )
        self.event_log.log("query_response_sent", request_type=env.msg_type, response_type=msg_type)

    def _send_raw_json(self, msg_type: str, payload: Dict[str, Any], addr: MessageAddress) -> None:
        wrapped = {"msg_type": msg_type, "data": payload}
        if msg_type == MSG_VEHICLE_CANDIDATE_PATH_RESULT and self.model_message_type_tag:
            wrapped["group"] = self.model_message_type_tag
        data = json.dumps(wrapped, ensure_ascii=False).encode("utf-8") + b"\n"
        self._capture_message("send", msg_type, payload, addr=addr, sender=self.vehicle_id, group=wrapped.get("group"))
        payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] raw_json_send msg_type={msg_type} addr={addr} payload_keys={payload_keys}"
        )
        with socket.create_connection(addr, timeout=1.5) as sock:
            sock.sendall(data)

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
            self._message_capture_seq += 1
            safe_msg_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(msg_type or "UNKNOWN"))
            out_dir = self.message_capture_dir / direction
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{self._message_capture_seq:06d}_{safe_msg_type}.json"
            row = {
                "ts": utc_now_iso(),
                "node_type": "vehicle",
                "node_id": self.vehicle_id,
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

    def _on_vehicle_score_request(self, env: Envelope, addr: MessageAddress) -> None:
        p = env.payload or {}
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] score_request_received addr={addr} payload={p}"
        )
        candidates = p.get("candidate_launch_points") or p.get("launch_candidates") or []
        launch_nodes: List[Optional[str]] = []
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict):
                    launch_nodes.append(
                        self._resolve_external_point_node(
                            item.get("launch_node") or item.get("fire_point_id") or item.get("target_node")
                        )
                    )
                else:
                    launch_nodes.append(self._resolve_external_point_node(str(item)))
        if not launch_nodes:
            launch_nodes = [
                self._resolve_external_point_node(p.get("launch_node") or p.get("target_node") or p.get("fire_point_id"))
            ]
        if not any(launch_nodes):
            self.event_log.log(
                "vehicle_score_skipped",
                vehicle_id=self.vehicle_id,
                reason="missing_runtime_launch_points",
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] vehicle_score_skipped reason=missing_runtime_launch_points"
            )
            self._reply_to_request(MSG_VEHICLE_SCORE_RESULT, env, {"score_total": 0.0}, addr)
            return
        scores = [
            {
                **score_vehicle_task_execution(
                    vehicle_id=self.vehicle_id,
                    current_node=self.state.current_node,
                    launch_node=launch_node,
                    ammo_types=self.state.ammo_types,
                    required_ammo_type=p.get("ammo_type") or p.get("required_ammo_type"),
                    speed_mps=self.speed_mps,
                    graph=self.graph,
                    lane_graph=self.lane_graph,
                    request_time=p.get("issued_at") or p.get("request_time"),
                    desired_fire_time=p.get("fire_time") or p.get("desired_fire_time"),
                    fire_time_grace_sec=float(p.get("fire_time_grace_sec", 15.0) or 15.0),
                    wait_seconds=0.0,
                    launch_prepare_seconds=self.launch_prepare_sec,
                    hide_selected=False,
                    fire_time_error_sec=None,
                    ignore_speed_limits=True,
                ),
                **{
                    key: value
                    for key, value in task_book_vehicle_score(
                        vehicle_id=self.vehicle_id,
                        current_node=self.state.current_node,
                        launch_node=launch_node,
                        ammo_types=self.state.ammo_types,
                        required_ammo_type=p.get("ammo_type") or p.get("required_ammo_type"),
                        speed_mps=self.speed_mps,
                        health=self.vehicle_health,
                        graph=self.graph,
                        lane_graph=None,
                        request_time=p.get("issued_at") or p.get("request_time"),
                        desired_fire_time=p.get("fire_time") or p.get("desired_fire_time"),
                        fire_time_grace_sec=float(p.get("fire_time_grace_sec", 15.0) or 15.0),
                        ignore_speed_limits=True,
                        fire_pool_bounds_xy=self.fire_pool_bounds,
                        fire_pool_bonus=self.fire_pool_bonus,
                        health_weight=self.vehicle_health_weight,
                    ).items()
                    if key
                    in {
                        "vehicle_health",
                        "score_health",
                        "fire_pool_in_zone",
                        "score_fire_pool_bonus",
                        "score_total_before_fire_pool",
                        "score_total",
                        "score_semantics",
                    }
                },
            }
            for launch_node in launch_nodes
            if launch_node
        ]
        best_score = max(
            scores,
            key=lambda row: (bool(row.get("feasible", False)), float(row.get("score_total", 0.0) or 0.0)),
            default={},
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] score_computed current_node={self.state.current_node} "
            f"launch_nodes={launch_nodes} best_score={best_score}"
        )
        payload = {
            "score_total": round(float(best_score.get("score_total", 0.0) or 0.0), 3),
        }
        self._reply_to_request(MSG_VEHICLE_SCORE_RESULT, env, payload, addr)

    def _on_dian_context(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        rows = self._normalize_dian_rows(env.payload or {})
        if self.theater_bounds is not None:
            rows = [row for row in rows if self._row_in_theater(row)]
        prefix = "launch" if env.msg_type == MSG_FA_SHE_DIAN else "hide"
        target_points = self.points.launch_points if env.msg_type == MSG_FA_SHE_DIAN else self.points.hide_points
        if env.msg_type == MSG_FA_SHE_DIAN and not self._external_launch_points_loaded:
            target_points.clear()
            self._external_launch_points_loaded = True
        elif env.msg_type == MSG_YIN_BI_DIAN and not self._external_hide_points_loaded:
            target_points.clear()
            self._external_hide_points_loaded = True
        # 调度端会把已投影 node_id 放进候选上下文；原始全量点位如果没有
        # node_id，这里先缓存、不逐车重复投影，避免 128 车重复扫描路网。
        has_projected_refs = any(self._dian_projected_node_ref(row) for row in rows)
        if rows and not has_projected_refs:
            if env.msg_type == MSG_FA_SHE_DIAN:
                self.external_launch_rows_by_node.clear()
                self.external_raw_launch_rows = [dict(row) for row in rows]
            elif env.msg_type == MSG_YIN_BI_DIAN:
                self.external_raw_hide_rows = [dict(row) for row in rows]
            self.event_log.log(
                "dian_context_projection_deferred",
                msg_type=env.msg_type,
                rows=len(rows),
                reason="missing_projected_node_id",
            )
            self._timing_log(
                "dian_context_received",
                started_at=started_at,
                msg_type=env.msg_type,
                rows=len(rows),
                matched=0,
                point_count=len(target_points),
                projection_deferred=True,
                projection_sec=0.0,
                lane_refresh_sec=0.0,
            )
            return
        before_count = len(target_points)
        matched = 0
        mapped_rows: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows):
            node_id = self._nearest_graph_node_for_dian(row, f"{prefix}_{idx:03d}")
            if not node_id:
                continue
            if node_id not in target_points:
                target_points.append(node_id)
            if env.msg_type == MSG_FA_SHE_DIAN:
                self.external_launch_rows_by_node[node_id] = dict(row)
            for key in self._dian_keys(row, idx):
                self.external_point_node_map[key] = node_id
            for key in self._dian_aliases(row, idx, prefix):
                self.external_point_node_map[key] = node_id
            mapped_rows.append(self._point_snapshot_row(row, node_id))
            matched += 1
        projected_at = time.perf_counter()
        if mapped_rows:
            self._ensure_lane_graph_current()
        lane_refreshed_at = time.perf_counter()
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] dian_context_received msg_type={env.msg_type} "
            f"rows={len(rows)} matched={matched}"
        )
        self.event_log.log("dian_context_received", msg_type=env.msg_type, rows=len(rows), matched=matched)
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] dian_context_points_after msg_type={env.msg_type} "
            f"before={before_count} after={len(target_points)} matched={matched}"
        )
        self.event_log.log(
            "dian_context_points_after",
            msg_type=env.msg_type,
            before=before_count,
            after=len(target_points),
            matched=matched,
        )
        print(
            f"[Vehicle {self.vehicle_id}] dian_context_received"
        )
        self._save_received_point_snapshot(env.msg_type, rows, mapped_rows)
        self._timing_log(
            "dian_context_received",
            started_at=started_at,
            msg_type=env.msg_type,
            rows=len(rows),
            matched=matched,
            point_count=len(target_points),
            projection_sec=round(projected_at - started_at, 6),
            lane_refresh_sec=round(lane_refreshed_at - projected_at, 6),
        )
        if matched and self._runtime_vehicle_position_ready() and self.external_candidate_context:
            self._submit_vehicle_context_score()
            self._submit_candidate_paths_to_model()

    def _on_depot_dian_context(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        rows = self._normalize_dian_rows(env.payload or {})
        if self.theater_bounds is not None:
            rows = [row for row in rows if self._row_in_theater(row)]
        if not self._external_depot_points_loaded:
            self.points.depots.clear()
            self.external_depot_rows_by_node.clear()
            self._external_depot_points_loaded = True
        has_projected_refs = any(self._dian_projected_node_ref(row) for row in rows)
        if rows and not has_projected_refs:
            self.event_log.log(
                "depot_dian_context_projection_deferred",
                vehicle_id=self.vehicle_id,
                rows=len(rows),
                reason="missing_projected_node_id",
            )
            self._timing_log(
                "depot_dian_context_received",
                started_at=started_at,
                msg_type=env.msg_type,
                rows=len(rows),
                mapped=0,
                projection_deferred=True,
                projection_sec=0.0,
                lane_refresh_sec=0.0,
            )
            return
        mapped_rows: List[Dict[str, Any]] = []
        for index, row in enumerate(rows):
            node_id = self._nearest_graph_node_for_dian(row, f"depot_{index:03d}")
            if not node_id:
                continue
            if node_id not in self.points.depots:
                self.points.depots.append(node_id)
            self.external_depot_rows_by_node[node_id] = dict(row)
            for key in self._dian_keys(row, index):
                self.external_point_node_map[key] = node_id
            for key in self._dian_aliases(row, index, "depot"):
                self.external_point_node_map[key] = node_id
            mapped_rows.append(self._point_snapshot_row(row, node_id))
        projected_at = time.perf_counter()
        if mapped_rows:
            self._ensure_lane_graph_current()
        lane_refreshed_at = time.perf_counter()
        self._save_received_point_snapshot(env.msg_type, rows, mapped_rows)
        self.event_log.log(
            "depot_dian_context_received",
            vehicle_id=self.vehicle_id,
            rows=len(rows),
            mapped=len(mapped_rows),
        )
        self._timing_log(
            "depot_dian_context_received",
            started_at=started_at,
            msg_type=env.msg_type,
            rows=len(rows),
            mapped=len(mapped_rows),
            projection_sec=round(projected_at - started_at, 6),
            lane_refresh_sec=round(lane_refreshed_at - projected_at, 6),
        )
        # Depot coordinates are consumed only after the scheduler assigns one
        # depot.  Receiving them must not retrigger the pre-launch candidate run.

    @staticmethod
    def _parse_bounds(value: Any) -> Optional[Tuple[float, float, float, float]]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if len(parts) == 4:
                try:
                    return tuple(float(part) for part in parts)  # type: ignore[return-value]
                except Exception:
                    return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return tuple(float(part) for part in value)  # type: ignore[return-value]
            except Exception:
                return None
        return None

    def _row_in_theater(self, row: Dict[str, Any]) -> bool:
        if self.theater_bounds is None:
            return True
        lon = row.get("lon", row.get("lng", row.get("longitude", row.get("platform_LocationLLA_Lon"))))
        lat = row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat")))
        if lon in {None, ""} or lat in {None, ""}:
            return False
        try:
            lon_f = float(lon)
            lat_f = float(lat)
        except Exception:
            return False
        lon_min, lat_min, lon_max, lat_max = self.theater_bounds
        return lon_min <= lon_f <= lon_max and lat_min <= lat_f <= lat_max

    def _point_snapshot_row(self, row: Dict[str, Any], node_id: Optional[str]) -> Dict[str, Any]:
        item = dict(row)
        item["mapped_node"] = node_id
        if node_id and node_id in self.graph.nodes:
            node = self.graph.nodes[node_id]
            item["mapped_x"] = node.x
            item["mapped_y"] = node.y
            item["mapped_lon"] = node.lon
            item["mapped_lat"] = node.lat
        return item

    def _save_received_point_snapshot(
        self,
        msg_type: str,
        raw_rows: List[Dict[str, Any]],
        mapped_rows: List[Dict[str, Any]],
    ) -> None:
        if not self.received_dian_enabled:
            return
        try:
            self.received_dian_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.received_dian_dir / f"{self.vehicle_id}_{msg_type}.json"
            payload = {
                "vehicle_id": self.vehicle_id,
                "msg_type": msg_type,
                "raw_count": len(raw_rows),
                "mapped_count": len(mapped_rows),
                "raw_rows": raw_rows,
                "mapped_rows": mapped_rows,
            }
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] save_received_point_snapshot_failed "
                f"msg_type={msg_type} error={type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _row_lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        lon = row.get("lon", row.get("lng", row.get("longitude", row.get("platform_LocationLLA_Lon"))))
        lat = row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat")))
        if lon in {None, ""} or lat in {None, ""}:
            return None
        try:
            return float(lon), float(lat)
        except Exception:
            return None

    @staticmethod
    def _lon_lat_distance_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
        mid_lat = math.radians((float(lat_a) + float(lat_b)) * 0.5)
        dx = (float(lon_a) - float(lon_b)) * 111000.0 * math.cos(mid_lat)
        dy = (float(lat_a) - float(lat_b)) * 111000.0
        return math.hypot(dx, dy)

    def _append_exact_launch_endpoint(self, launch_node: str, points: List[dict]) -> List[dict]:
        if not points:
            return points
        row = self.external_launch_rows_by_node.get(launch_node)
        if not isinstance(row, dict):
            return points
        lon_lat = self._row_lon_lat(row)
        if lon_lat is None:
            return points
        lon, lat = lon_lat
        last = points[-1]
        try:
            last_lon = float(last.get("lon"))
            last_lat = float(last.get("lat"))
        except Exception:
            last_lon = lon
            last_lat = lat
        delta_m = self._lon_lat_distance_m(last_lon, last_lat, lon, lat)
        if delta_m <= 0.5:
            return points
        exact = dict(last)
        exact["lon"] = lon
        exact["lat"] = lat
        if row.get("alt", row.get("height")) not in {None, ""}:
            try:
                exact["alt"] = float(row.get("alt", row.get("height")))
            except Exception:
                pass
        out = list(points)
        out.append(exact)
        self.event_log.log(
            "launch_endpoint_exact_appended",
            vehicle_id=self.vehicle_id,
            launch_node=launch_node,
            distance_m=round(delta_m, 3),
            lon=lon,
            lat=lat,
        )
        return out

    @staticmethod
    def _normalize_dian_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("points", "items", "vehicles", "fa_she_dian", "yin_bi_dian", "vehicle_dian"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
            else:
                rows = [data]
        else:
            rows = []
        return [dict(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _dian_keys(row: Dict[str, Any], idx: int) -> List[str]:
        keys = []
        for key in ("id", "point_id", "fire_point_id", "launch_node", "hide_node", "depot_node", "node_id", "name", "index"):
            value = row.get(key)
            if value not in {None, ""}:
                keys.append(str(value))
        if not keys:
            keys.append(str(idx))
        return keys

    @staticmethod
    def _dian_projected_node_ref(row: Dict[str, Any]) -> Optional[str]:
        for key in ("node_id", "launch_node", "hide_node", "depot_node", "current_node"):
            value = row.get(key)
            if value not in {None, ""}:
                return str(value)
        return None

    @staticmethod
    def _safe_point_token(value: Any) -> str:
        return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))

    def _dian_aliases(self, row: Dict[str, Any], idx: int, prefix: str) -> List[str]:
        aliases = [f"{prefix}_{idx:03d}"]
        name = row.get("name")
        index = row.get("index")
        if name not in {None, ""}:
            safe_name = self._safe_point_token(name)
            aliases.append(f"{prefix}_{safe_name}")
            aliases.append(f"{prefix}_{idx:03d}_{safe_name}")
            if index not in {None, ""}:
                aliases.append(f"{prefix}_{index}_{safe_name}")
                try:
                    aliases.append(f"{prefix}_{int(index):03d}_{safe_name}")
                except (TypeError, ValueError):
                    pass
        if index not in {None, ""}:
            aliases.append(f"{prefix}_{index}")
            try:
                aliases.append(f"{prefix}_{int(index):03d}")
            except (TypeError, ValueError):
                pass
        return aliases

    def _resolve_external_point_node(self, value: Any) -> Optional[str]:
        if value in {None, ""}:
            return None
        text = str(value)
        if text in self.graph.nodes:
            return text
        mapped = self.external_point_node_map.get(text)
        if mapped and mapped in self.graph.nodes:
            return mapped
        return None

    def _ensure_assigned_depot_node(self, depot: Dict[str, Any]) -> Optional[str]:
        # 正式贮备库分配可能只携带调度侧 node_id。车辆侧如果没有该节点，
        # 就用分配消息里的 mapped_lon/mapped_lat 或 lon/lat 本地投影一次，保证射后路径可规划。
        for key in ("depot_node", "node_id", "depot_id", "depot_port", "point_id", "name", "index"):
            node = self._resolve_external_point_node(depot.get(key))
            if node:
                return node

        projection_row = dict(depot)
        if projection_row.get("port") in {None, ""} and projection_row.get("depot_port") not in {None, ""}:
            projection_row["port"] = projection_row.get("depot_port")
        if projection_row.get("id") in {None, ""} and projection_row.get("depot_id") not in {None, ""}:
            projection_row["id"] = projection_row.get("depot_id")
        projection_input = dict(projection_row)
        original_aliases = {
            key: projection_input.pop(key, None)
            for key in ("node_id", "depot_node")
            if projection_input.get(key) not in {None, ""}
        }
        node = self._nearest_graph_node_for_dian(projection_input, "assigned_depot")
        if not node or node not in self.graph.nodes:
            return None

        if node not in self.points.depots:
            self.points.depots.append(node)
        self._external_depot_points_loaded = True
        projection_row.update(original_aliases)
        self.external_depot_rows_by_node[node] = projection_row
        alias_values = [
            node,
            projection_row.get("depot_node"),
            projection_row.get("node_id"),
            projection_row.get("depot_id"),
            projection_row.get("depot_port"),
            projection_row.get("point_id"),
            projection_row.get("name"),
            projection_row.get("index"),
            projection_row.get("port"),
            projection_row.get("id"),
        ]
        for alias in alias_values:
            if alias not in {None, ""}:
                self.external_point_node_map[str(alias)] = node
        for alias in self._dian_keys(projection_row, 0):
            self.external_point_node_map[alias] = node
        for alias in self._dian_aliases(projection_row, 0, "depot"):
            self.external_point_node_map[alias] = node
        self.event_log.log(
            "assigned_depot_projected",
            vehicle_id=self.vehicle_id,
            depot_id=projection_row.get("depot_id"),
            depot_port=projection_row.get("depot_port"),
            depot_node=node,
        )
        return node

    def _nearest_graph_node_for_dian(self, row: Dict[str, Any], prefix: str = "dian") -> Optional[str]:
        node_id = self._dian_projected_node_ref(row)
        if node_id and str(node_id) in self.graph.nodes:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_existing_node "
                f"prefix={prefix} node={node_id}"
            )
            return str(node_id)
        projected = self._add_projected_dian_from_ref(row, prefix, node_id)
        if projected:
            return projected
        point_id = self._dian_graph_point_id(row, prefix)
        if point_id in self.graph.nodes:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_reuse_existing_point "
                f"prefix={prefix} point_id={point_id}"
            )
            return point_id
        xy = self._dian_xy(row)
        if xy is None:
            return None
        x_f, y_f = xy
        nearest_edge = MapLoader._nearest_edge_projection(self.graph, x_f, y_f)
        if nearest_edge is not None:
            edge, ratio = nearest_edge
            projected_xy = self._point_on_edge_geometry(edge, ratio)
            oriented_ratio = float(ratio)
            if edge.geometry and not (edge.geom_from == edge.src and edge.geom_to == edge.dst):
                oriented_ratio = 1.0 - oriented_ratio
            projected_x, projected_y = projected_xy if projected_xy is not None else (x_f, y_f)
            if point_id in self.graph.nodes:
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] dian_projection_reuse_edge_point "
                    f"prefix={prefix} point_id={point_id} edge={edge.src}->{edge.dst} ratio={round(float(ratio), 6)}"
                )
                return point_id
            try:
                projected = self.graph.add_point_on_edge(
                    point_id=point_id,
                    from_node=edge.src,
                    to_node=edge.dst,
                    ratio=oriented_ratio,
                    x=projected_x,
                    y=projected_y,
                )
                projected_lonlat = self._graph_xy_to_lonlat(projected_x, projected_y)
                if projected_lonlat is not None:
                    self.graph.nodes[projected].lon, self.graph.nodes[projected].lat = projected_lonlat
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] dian_projection_edge_success "
                    f"prefix={prefix} point_id={projected} edge={edge.src}->{edge.dst} "
                    f"ratio={round(float(oriented_ratio), 6)} "
                    f"source_x={round(float(x_f), 3)} source_y={round(float(y_f), 3)} "
                    f"projected_x={round(float(projected_x), 3)} projected_y={round(float(projected_y), 3)}"
                )
                return projected
            except Exception as exc:
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] dian_edge_project_failed point_id={point_id} "
                    f"edge={edge.src}->{edge.dst} error={type(exc).__name__}: {exc}"
                )
        # 这里可能与候选路径规划线程同时运行；先固定节点快照，避免遍历中 graph.nodes 被插点修改。
        node_items = list(self.graph.nodes.items())
        fallback_node = min(
            node_items,
            key=lambda item: (item[1].x - x_f) ** 2 + (item[1].y - y_f) ** 2,
            default=None,
        )
        fallback = fallback_node[0] if fallback_node else None
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] dian_projection_fallback_nearest_node "
            f"prefix={prefix} node={fallback} x={round(float(x_f), 3)} y={round(float(y_f), 3)}"
        )
        return fallback

    def _add_projected_dian_from_ref(
        self,
        row: Dict[str, Any],
        prefix: str,
        node_id: Optional[str],
    ) -> Optional[str]:
        def first_present(*keys: str) -> Any:
            for key in keys:
                value = row.get(key)
                if value not in {None, ""}:
                    return value
            return None

        # 调度端动态图可能已经被特殊点切分；base_projection_* 永远指向原始路网边，
        # 车辆端优先用它复现同一个投影点，避免退回最近边扫描。
        from_node = first_present("base_projection_from_node", "projection_from_node", "projected_from_node")
        to_node = first_present("base_projection_to_node", "projection_to_node", "projected_to_node")
        ratio = first_present("base_projection_ratio", "projection_ratio", "projected_ratio")
        if from_node in {None, ""} or to_node in {None, ""} or ratio in {None, ""}:
            return None
        from_node = str(from_node)
        to_node = str(to_node)
        if from_node not in self.graph.nodes or to_node not in self.graph.nodes:
            return None
        point_id = str(node_id or self._dian_graph_point_id(row, prefix))
        if point_id in self.graph.nodes:
            return point_id
        try:
            ratio_f = float(ratio)
            x_value = row.get("projected_x", row.get("mapped_x"))
            y_value = row.get("projected_y", row.get("mapped_y"))
            x_f = float(x_value) if x_value not in {None, ""} else None
            y_f = float(y_value) if y_value not in {None, ""} else None
            projected = self.graph.add_point_on_edge(
                point_id=point_id,
                from_node=from_node,
                to_node=to_node,
                ratio=ratio_f,
                x=x_f,
                y=y_f,
            )
            lon = row.get("mapped_lon")
            lat = row.get("mapped_lat")
            if lon in {None, ""} or lat in {None, ""}:
                node = self.graph.nodes.get(projected)
                if node is not None:
                    lon_lat = self._graph_xy_to_lonlat(node.x, node.y)
                    if lon_lat is not None:
                        lon, lat = lon_lat
            if lon not in {None, ""} and lat not in {None, ""}:
                self.graph.nodes[projected].lon = float(lon)
                self.graph.nodes[projected].lat = float(lat)
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_ref_success "
                f"prefix={prefix} point_id={projected} edge={from_node}->{to_node} ratio={round(ratio_f, 6)}"
            )
            return projected
        except Exception as exc:
            recovered = self._add_projected_dian_from_split_base(
                row=row,
                prefix=prefix,
                point_id=point_id,
                base_from=from_node,
                base_to=to_node,
                base_ratio=ratio,
            )
            if recovered:
                return recovered
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_ref_failed "
                f"prefix={prefix} point_id={point_id} edge={from_node}->{to_node} "
                f"error={type(exc).__name__}: {exc}"
            )
            return None

    def _add_projected_dian_from_split_base(
        self,
        *,
        row: Dict[str, Any],
        prefix: str,
        point_id: str,
        base_from: str,
        base_to: str,
        base_ratio: Any,
    ) -> Optional[str]:
        try:
            target_ratio = float(base_ratio)
        except Exception:
            return None
        candidates: List[Tuple[float, str, str, float]] = []
        for meta in self.graph.edge_meta.values():
            if meta.base_from_node != base_from or meta.base_to_node != base_to:
                continue
            start = float(meta.base_start_ratio)
            end = float(meta.base_end_ratio)
            lo, hi = (start, end) if start <= end else (end, start)
            if target_ratio < lo - 1e-9 or target_ratio > hi + 1e-9:
                continue
            span = end - start
            if abs(span) <= 1e-12:
                continue
            local_ratio = (target_ratio - start) / span
            local_ratio = min(0.999999, max(0.000001, local_ratio))
            candidates.append((abs(span), meta.src, meta.dst, local_ratio))
        if not candidates:
            return None
        _span, from_node, to_node, local_ratio = min(candidates, key=lambda item: item[0])
        if point_id in self.graph.nodes:
            return point_id
        try:
            x_value = row.get("projected_x", row.get("mapped_x"))
            y_value = row.get("projected_y", row.get("mapped_y"))
            x_f = float(x_value) if x_value not in {None, ""} else None
            y_f = float(y_value) if y_value not in {None, ""} else None
            projected = self.graph.add_point_on_edge(
                point_id=point_id,
                from_node=from_node,
                to_node=to_node,
                ratio=local_ratio,
                x=x_f,
                y=y_f,
            )
            lon = row.get("mapped_lon")
            lat = row.get("mapped_lat")
            if lon in {None, ""} or lat in {None, ""}:
                node = self.graph.nodes.get(projected)
                if node is not None:
                    lon_lat = self._graph_xy_to_lonlat(node.x, node.y)
                    if lon_lat is not None:
                        lon, lat = lon_lat
            if lon not in {None, ""} and lat not in {None, ""}:
                self.graph.nodes[projected].lon = float(lon)
                self.graph.nodes[projected].lat = float(lat)
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_split_ref_success "
                f"prefix={prefix} point_id={projected} edge={from_node}->{to_node} "
                f"base={base_from}->{base_to} base_ratio={round(target_ratio, 6)} "
                f"local_ratio={round(local_ratio, 6)}"
            )
            return projected
        except Exception as exc:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dian_projection_split_ref_failed "
                f"prefix={prefix} point_id={point_id} edge={from_node}->{to_node} "
                f"base={base_from}->{base_to} base_ratio={round(target_ratio, 6)} "
                f"error={type(exc).__name__}: {exc}"
            )
            return None

    def _point_on_edge_geometry(self, edge: Any, ratio: float) -> Optional[Tuple[float, float]]:
        poly = list(edge.geometry or [])
        if len(poly) < 2:
            a = self.graph.nodes.get(edge.src)
            b = self.graph.nodes.get(edge.dst)
            if not a or not b:
                return None
            poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
        lengths = [
            math.hypot(float(poly[i + 1]["x"]) - float(poly[i]["x"]), float(poly[i + 1]["y"]) - float(poly[i]["y"]))
            for i in range(len(poly) - 1)
        ]
        total = sum(lengths)
        if total <= 1e-9:
            return float(poly[0]["x"]), float(poly[0]["y"])
        target = min(1.0, max(0.0, float(ratio))) * total
        traversed = 0.0
        for idx, length in enumerate(lengths):
            if length <= 1e-9:
                continue
            if traversed + length >= target:
                local = (target - traversed) / length
                ax, ay = float(poly[idx]["x"]), float(poly[idx]["y"])
                bx, by = float(poly[idx + 1]["x"]), float(poly[idx + 1]["y"])
                return ax + (bx - ax) * local, ay + (by - ay) * local
            traversed += length
        return float(poly[-1]["x"]), float(poly[-1]["y"])

    def _dian_graph_point_id(self, row: Dict[str, Any], prefix: str) -> str:
        raw = self._dian_keys(row, 0)[0]
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(raw))
        return f"{prefix}_{safe}"

    def _dian_xy(self, row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        lon = row.get("mapped_lon", row.get("lon", row.get("lng", row.get("longitude"))))
        lat = row.get("mapped_lat", row.get("lat", row.get("latitude")))
        if lon is not None and lat is not None:
            lon_f = float(lon)
            lat_f = float(lat)
            if self._graph_xy_looks_lonlat():
                return lon_f, lat_f
            graph_xy = self._lonlat_to_graph_xy(lon_f, lat_f)
            if graph_xy is not None:
                return graph_xy
        x = row.get("x")
        y = row.get("y")
        if x is not None and y is not None:
            return float(x), float(y)
        return None

    def _lonlat_to_graph_xy(self, lon: float, lat: float) -> Optional[Tuple[float, float]]:
        transform = getattr(self, "_graph_lonlat_xy_transform", None)
        if transform is None:
            transform = self._build_graph_lonlat_xy_transform()
            self._graph_lonlat_xy_transform = transform
        if not transform:
            return None
        x_scale, x_bias, y_scale, y_bias = transform
        return x_scale * lon + x_bias, y_scale * lat + y_bias

    def _graph_xy_to_lonlat(self, x: float, y: float) -> Optional[Tuple[float, float]]:
        transform = getattr(self, "_graph_lonlat_xy_transform", None)
        if transform is None:
            transform = self._build_graph_lonlat_xy_transform()
            self._graph_lonlat_xy_transform = transform
        if not transform:
            return None
        x_scale, x_bias, y_scale, y_bias = transform
        if abs(x_scale) <= 1e-12 or abs(y_scale) <= 1e-12:
            return None
        return (x - x_bias) / x_scale, (y - y_bias) / y_scale

    def _build_graph_lonlat_xy_transform(self) -> Optional[Tuple[float, float, float, float]]:
        samples: List[Tuple[float, float, float, float]] = []
        # 评分链路会和候选路径规划并发运行；这里固定节点快照，避免规划插点时
        # graph.nodes 变化导致 VEHICLE_CONTEXT 回分阶段抛 RuntimeError。
        for node in list(self.graph.nodes.values()):
            if node.lon is None or node.lat is None:
                continue
            samples.append((float(node.lon), float(node.lat), float(node.x), float(node.y)))
        if len(samples) < 2:
            return None

        def _fit_linear(src_vals: List[float], dst_vals: List[float]) -> Optional[Tuple[float, float]]:
            src_mean = sum(src_vals) / len(src_vals)
            dst_mean = sum(dst_vals) / len(dst_vals)
            var = sum((v - src_mean) ** 2 for v in src_vals)
            if var <= 1e-12:
                return None
            cov = sum((s - src_mean) * (d - dst_mean) for s, d in zip(src_vals, dst_vals))
            scale = cov / var
            bias = dst_mean - scale * src_mean
            return scale, bias

        x_fit = _fit_linear([s[0] for s in samples], [s[2] for s in samples])
        y_fit = _fit_linear([s[1] for s in samples], [s[3] for s in samples])
        if x_fit is None or y_fit is None:
            return None
        return x_fit[0], x_fit[1], y_fit[0], y_fit[1]

    def _graph_xy_looks_lonlat(self) -> bool:
        if not self.graph.nodes:
            return False
        # 只读判断也要用快照；否则重复 VEHICLE_CONTEXT 到来时可能与插点线程冲突，
        # 导致分数还没发出就被 dictionary changed size 中断。
        node_values = list(self.graph.nodes.values())
        if not node_values:
            return False
        xs = [node.x for node in node_values]
        ys = [node.y for node in node_values]
        return min(xs) >= -180.0 and max(xs) <= 180.0 and min(ys) >= -90.0 and max(ys) <= 90.0

    def _matches_external_vehicle_key(self, vehicle_key: Any) -> bool:
        if vehicle_key is None:
            return False
        key = str(vehicle_key)
        if key in {str(self.advertise_port), self.vehicle_id}:
            return True
        try:
            numeric = int(str(vehicle_key))
        except ValueError:
            return False
        if numeric == self.advertise_port:
            return True
        return (8400 + numeric) == self.advertise_port

    def _runtime_vehicle_position_ready(self) -> bool:
        node_id = self.external_vehicle_state.get("current_node")
        return bool(
            self._has_external_vehicle_state
            and node_id
            and node_id == self.state.current_node
            and node_id in self.graph.nodes
        )

    def _candidate_path_busy_for_task(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self._lock:
            return (
                self._candidate_paths_submitted_task_id == task_id
                or task_id in self._candidate_paths_inflight_task_ids
            )

    def _vehicle_score_position_xy(self) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        node_id = self.external_vehicle_state.get("current_node")
        if node_id and str(node_id) in self.graph.nodes:
            node = self.graph.nodes[str(node_id)]
            return float(node.x), float(node.y), str(node_id)

        x = self.external_vehicle_state.get("x")
        y = self.external_vehicle_state.get("y")
        if x not in {None, ""} and y not in {None, ""}:
            return float(x), float(y), None

        lon = self.external_vehicle_state.get("lon")
        lat = self.external_vehicle_state.get("lat")
        if lon in {None, ""} or lat in {None, ""}:
            return None, None, None
        lon_f = float(lon)
        lat_f = float(lat)
        if self._graph_xy_looks_lonlat():
            return lon_f, lat_f, None
        xy = self._lonlat_to_graph_xy(lon_f, lat_f)
        if xy is None:
            return None, None, None
        return float(xy[0]), float(xy[1]), None

    def _on_external_vehicle_state_result(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        payload = env.payload or {}
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else None
        source = data if data is not None else payload
        rows = source if isinstance(source, list) else source.get("vehicles") if isinstance(source, dict) else None
        if rows is None:
            rows = [source]
        elif isinstance(rows, dict):
            rows = [rows]
        previous_reply_host = self.external_vehicle_state.get("reply_host")
        previous_reply_port = self.external_vehicle_state.get("reply_port")
        context_reply_host = source.get("msg_ip") or source.get("reply_host") if isinstance(source, dict) else None
        context_reply_port = source.get("reply_port") if isinstance(source, dict) else None
        matched = 0
        identity_matched = 0
        score_submitted = False
        raw_rows: List[Dict[str, Any]] = []
        mapped_rows: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_rows.append(dict(row))
            vehicle_key = row.get("vehicle_id") or row.get("port") or row.get("vehicle_port")
            if not self._matches_external_vehicle_key(vehicle_key):
                continue
            identity_matched += 1
            position = row.get("position") if isinstance(row.get("position"), dict) else {}
            lon = position.get("lon", row.get("lon"))
            lat = position.get("lat", row.get("lat"))
            alt = position.get("alt", row.get("alt", row.get("height")))
            current_time = None
            for time_key in ("time", "sim_time", "simTime", "timestamp", "current_time"):
                if row.get(time_key) not in {None, ""}:
                    current_time = row.get(time_key)
                    break
            current_node = row.get("current_node") or row.get("node_id") or row.get("position_node")
            reply_host = row.get("msg_ip") or row.get("reply_host") or context_reply_host or previous_reply_host
            reply_port = row.get("reply_port") or context_reply_port or previous_reply_port
            previous_runtime_ready = self._runtime_vehicle_position_ready()
            candidate_task_id = str(self.external_candidate_context.get("task_id") or "")
            score_state = {
                "port": row.get("port") or vehicle_key or self.advertise_port,
                "vehicle_id": vehicle_key,
                "lon": lon,
                "lat": lat,
                "alt": alt,
                "x": row.get("x"),
                "y": row.get("y"),
                "current_time": current_time,
                "current_node": (
                    str(current_node)
                    if current_node and str(current_node) in self.graph.nodes
                    else self.state.current_node
                    if previous_runtime_ready
                    else ""
                ),
            }
            if reply_host:
                score_state["reply_host"] = str(reply_host)
            if reply_port not in {None, ""}:
                score_state["reply_port"] = int(reply_port)
            self.external_vehicle_state = score_state
            self._has_external_vehicle_state = True
            if not score_submitted:
                # 车辆状态评分只需要当前位置坐标和候选发射点，不依赖路网插点。
                # 先回分数，避免大路网最近边投影阻塞评分链路；投影仍在后面为真实规划补齐。
                score_submitted = self._submit_vehicle_context_score(force_send=True)
            if score_submitted and previous_runtime_ready:
                self.event_log.log(
                    "external_vehicle_state_score_only",
                    vehicle_port=vehicle_key,
                    task_id=candidate_task_id,
                    reason="runtime_position_already_ready",
                    candidate_path_busy=self._candidate_path_busy_for_task(candidate_task_id),
                    reply_host=self.external_vehicle_state.get("reply_host"),
                    reply_port=self.external_vehicle_state.get("reply_port"),
                )
                self._timing_log(
                    "external_vehicle_state_score_only",
                    started_at=started_at,
                    msg_type=env.msg_type,
                    task_id=candidate_task_id,
                    raw_rows=len(raw_rows),
                    matched=1,
                    score_submitted=True,
                    reason="runtime_position_already_ready",
                    candidate_path_busy=self._candidate_path_busy_for_task(candidate_task_id),
                )
                return

            resolved_node = None
            if current_node and str(current_node) in self.graph.nodes:
                resolved_node = str(current_node)
            elif lon is not None and lat is not None:
                projection_row = dict(row)
                projection_row["lon"] = lon
                projection_row["lat"] = lat
                projection_row["alt"] = alt
                try:
                    resolved_node = self._nearest_graph_node_for_dian(
                        projection_row,
                        f"vehicle_{vehicle_key or self.advertise_port}",
                    )
                except Exception as exc:
                    debug_append_log(
                        f"[Vehicle {self.vehicle_id}] external_state_project_failed "
                        f"port={vehicle_key or self.advertise_port} lon={lon} lat={lat} "
                        f"error={type(exc).__name__}: {exc}"
                    )
            mapped_rows.append(self._point_snapshot_row(row, resolved_node))
            if not resolved_node or resolved_node not in self.graph.nodes:
                self.event_log.log(
                    "external_vehicle_state_invalid",
                    vehicle_port=vehicle_key,
                    lon=lon,
                    lat=lat,
                    current_node=current_node,
                    reason="missing_or_unprojectable_position",
                )
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] external_vehicle_state_invalid "
                    f"port={vehicle_key} lon={lon} lat={lat} current_node={current_node} "
                    f"reason=missing_or_unprojectable_position"
                )
                continue

            self.state.current_node = resolved_node
            self.external_mission_origin_node = resolved_node
            self.external_vehicle_state["current_node"] = self.state.current_node
            matched += 1
            print(
                f"[Vehicle {self.vehicle_id}] external state received "
                f"port={vehicle_key} node={self.state.current_node} lon={lon} lat={lat} alt={alt} time={current_time}",
                flush=True,
            )
            self.event_log.log(
                "external_vehicle_state_received",
                vehicle_port=vehicle_key,
                current_node=self.state.current_node,
                lon=lon,
                lat=lat,
                alt=alt,
                current_time=current_time,
                reply_host=self.external_vehicle_state.get("reply_host"),
                reply_port=self.external_vehicle_state.get("reply_port"),
            )
        if matched == 0:
            self.event_log.log(
                "external_vehicle_state_ignored",
                vehicle_port=self.advertise_port,
                identity_matched=identity_matched,
                reason="vehicle_not_found" if identity_matched == 0 else "no_valid_runtime_position",
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] external state not ready "
                f"identity_matched={identity_matched} reason="
                f"{'vehicle_not_found' if identity_matched == 0 else 'no_valid_runtime_position'}"
            )
            return

        self._save_received_point_snapshot(env.msg_type, raw_rows, mapped_rows)
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] external state ready for score submission "
            f"current_node={self.state.current_node}"
        )
        self._timing_log(
            "external_vehicle_state_received",
            started_at=started_at,
            msg_type=env.msg_type,
            raw_rows=len(raw_rows),
            mapped_rows=len(mapped_rows),
            matched=matched,
            current_node=self.state.current_node,
            score_submitted_before_projection=score_submitted,
        )
        if not score_submitted:
            self._submit_vehicle_context_score()
        self._submit_candidate_paths_to_model()
    # 收到调度倒排出来的时间约束：
    # 车辆后续评分和路径规划都依赖这里的发射时间/准备时间窗口。
    def _on_time_backplan_context(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        payload = env.payload or {}
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
        self.external_time_backplan_context = dict(payload)
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] time_backplan_context_received payload={payload}"
        )
        self._timing_log(
            "time_backplan_context_received",
            started_at=started_at,
            task_id=payload.get("task_id"),
            keys=sorted(payload.keys()) if isinstance(payload, dict) else [],
        )
        if self._runtime_vehicle_position_ready():
            self._submit_vehicle_context_score()
            self._submit_candidate_paths_to_model()

    # 收到“本车可选哪些发射点”的候选上下文。
    # 注意这里只保存候选信息，真正路径要等状态、时间、点位都齐了才开始规划。
    def _on_vehicle_candidate_context(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        payload = env.payload or {}
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
        self.external_candidate_context = dict(payload)
        candidates = self._candidate_launch_points_for_vehicle(payload, self.advertise_port)
        projected_at = time.perf_counter()
        score_candidates = self._vehicle_context_launch_score_candidates()
        parsed_hides = sum(
            len(candidate.get("hide_candidates") or candidate.get("candidate_hide_points") or [])
            for candidate in score_candidates
            if isinstance(candidate, dict)
        )
        nodes_ready_at = time.perf_counter()
        # 候选上下文接收阶段只做轻量解析，不能触发最近边扫描/插点。
        # 评分直接用候选点坐标或已有 node；真实规划阶段再解析为路网节点。
        lane_refreshed_at = nodes_ready_at
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] vehicle_candidate_context_received candidates={candidates} "
            f"hide_loaded={self._external_hide_points_loaded}"
        )
        self._timing_log(
            "vehicle_candidate_context_received",
            started_at=started_at,
            task_id=payload.get("task_id"),
            candidate_count=len(candidates),
            parsed_candidate_count=len(score_candidates),
            parsed_hide_count=parsed_hides,
            hide_loaded=self._external_hide_points_loaded,
            launch_loaded=self._external_launch_points_loaded,
            projection_sec=0.0,
            lightweight_parse_sec=round(nodes_ready_at - projected_at, 6),
            lane_refresh_deferred=True,
            lane_refresh_sec=round(lane_refreshed_at - nodes_ready_at, 6),
        )
        if self._runtime_vehicle_position_ready():
            self._submit_vehicle_context_score()
            self._submit_candidate_paths_to_model()

    def _on_dispatch_trajectory_bundle(self, env: Envelope) -> None:
        payload = env.payload or {}
        plans = payload.get("plans") or payload.get("trajectories") or []
        if isinstance(plans, dict):
            plans = list(plans.values())
        if not isinstance(plans, list):
            plans = []
        own_plan = None
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            key = plan.get("vehicle_id") or plan.get("port") or plan.get("vehicle_port")
            if self._matches_external_vehicle_key(key):
                own_plan = dict(plan)
                break
        if own_plan is None:
            self.event_log.log(
                "dispatch_trajectory_bundle_ignored",
                reason="no_matching_vehicle",
                task_id=payload.get("task_id"),
                trajectory_count=len(plans),
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] dispatch_bundle_ignored task_id={payload.get('task_id')} "
                f"trajectory_count={len(plans)}"
            )
            return
        self.external_dispatch_plan = own_plan
        self.event_log.log(
            "dispatch_trajectory_bundle_received",
            task_id=payload.get("task_id") or own_plan.get("task_id"),
            subtask_id=own_plan.get("subtask_id"),
            path_points=len(own_plan.get("path_points") or own_plan.get("trajectory_geo") or []),
            node_path=len(own_plan.get("node_path") or []),
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] dispatch_bundle_received task_id={payload.get('task_id')} "
            f"subtask_id={own_plan.get('subtask_id')} path_points={len(own_plan.get('path_points') or [])}"
        )

    def _candidate_launch_points_for_vehicle(self, payload: Dict[str, Any], vehicle_key: Any) -> List[dict]:
        rows = payload.get("vehicles") or payload.get("vehicle_candidate_launch_points") or []
        if not isinstance(rows, list):
            return []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("vehicle_id") or row.get("vehicle_port") or row.get("port")
            if self._matches_external_vehicle_key(key or vehicle_key):
                candidates = row.get("candidate_launch_points") or []
                return candidates if isinstance(candidates, list) else []
        return []

    @staticmethod
    def _is_vehicle_candidate_context_payload(payload: Dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return False
        if str(data.get("schema") or "") == "vehicle_candidate_context_v1":
            return True
        rows = data.get("vehicles") or data.get("vehicle_candidate_launch_points")
        if not isinstance(rows, list):
            return False
        return any(
            isinstance(row, dict) and isinstance(row.get("candidate_launch_points"), list)
            for row in rows
        )

    @staticmethod
    def _vehicle_index(vehicle_id: str) -> Optional[int]:
        digits = "".join(ch for ch in str(vehicle_id or "") if ch.isdigit())
        if not digits:
            return None
        try:
            numeric = int(digits)
            return max(0, numeric - 8414) if numeric >= 8414 else max(0, numeric - 1)
        except ValueError:
            return None

    # 车辆评分主入口：
    # 结合自身位置、时间约束和候选发射点，对本车当前可执行性做一个快速打分并回传。
    def _submit_vehicle_context_score(self, force_send: bool = False) -> bool:
        started_at = time.perf_counter()
        if self._vehicle_context_score_submitted and not force_send:
            self.event_log.log(
                "vehicle_context_score_skipped",
                reason="already_submitted",
                vehicle_id=self.vehicle_id,
            )
            return True
        score_x, score_y, score_node_id = self._vehicle_score_position_xy()
        if score_x is None or score_y is None:
            self.event_log.log(
                "vehicle_context_score_skipped",
                reason="missing_score_position",
                vehicle_id=self.vehicle_id,
            )
            return False
        if not self.external_time_backplan_context:
            self.event_log.log(
                "vehicle_context_score_skipped",
                reason="missing_time_backplan_context",
                vehicle_id=self.vehicle_id,
            )
            return False
        idx = self._vehicle_index(self.vehicle_id)
        if idx is None:
            self.event_log.log(
                "vehicle_context_score_skipped",
                reason="invalid_vehicle_index",
                vehicle_id=self.vehicle_id,
            )
            return False
        score_host = str(self.external_vehicle_state.get("reply_host") or self.vehicle_context_score_submit_host)
        score_port = int(self.external_vehicle_state.get("reply_port") or (self.vehicle_context_score_submit_port_base + idx))
        launch_candidates = self._vehicle_context_launch_score_candidates()
        if not launch_candidates:
            self.event_log.log(
                "vehicle_context_score_skipped",
                reason="no_launch_points",
                vehicle_id=self.vehicle_id,
                target=f"{score_host}:{score_port}",
                has_candidate_context=bool(self.external_candidate_context),
                raw_launch_rows=len(self.external_raw_launch_rows),
            )
            return False
        scores = [self._fast_vehicle_context_launch_score(row) for row in launch_candidates]
        best_score = max(
            scores,
            key=lambda item: (bool(item.get("feasible", False)), float(item.get("score_total", 0.0) or 0.0)),
            default={},
        )
        payload = {
            "port": self.external_vehicle_state.get("port") or self.advertise_port,
            "score_total": round(float(best_score.get("score_total", 0.0) or 0.0), 3),
        }
        self.event_log.log(
            "vehicle_context_score_prepare",
            vehicle_id=self.vehicle_id,
            target=f"{score_host}:{score_port}",
            current_node=self.state.current_node,
            score_node=score_node_id,
            score_x=round(float(score_x), 3),
            score_y=round(float(score_y), 3),
            launch_point_count=len(launch_candidates),
            candidate_source=(
                "vehicle_candidate_context"
                if self.external_candidate_context
                else "raw_dian_fallback"
            ),
            scoring_mode="fast_no_route_search",
            score_total=payload["score_total"],
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] vehicle_context_score_prepare target={score_host}:{score_port} "
            f"current_node={self.state.current_node} score_total={payload['score_total']}"
        )
        try:
            self._send_raw_json(MSG_VEHICLE_SCORE_RESULT, payload, (score_host, score_port))
            self._vehicle_context_score_submitted = True
            self.event_log.log(
                "vehicle_context_score_sent",
                vehicle_id=self.vehicle_id,
                target=f"{score_host}:{score_port}",
                score_total=payload["score_total"],
                force_send=force_send,
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] vehicle_context_score_sent target={score_host}:{score_port} "
                f"score_total={payload['score_total']}"
            )
            self._timing_log(
                "vehicle_context_score_sent",
                started_at=started_at,
                target=f"{score_host}:{score_port}",
                launch_point_count=len(launch_candidates),
                scoring_mode="fast_no_route_search",
                score_total=payload["score_total"],
                force_send=force_send,
            )
            return True
        except Exception as exc:
            self._vehicle_context_score_submitted = True
            self.event_log.log(
                "vehicle_context_score_send_failed",
                vehicle_id=self.vehicle_id,
                target=f"{score_host}:{score_port}",
                error=f"{type(exc).__name__}: {exc}",
                force_send=force_send,
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] vehicle_context_score_send_failed target={score_host}:{score_port} "
                f"error={type(exc).__name__}: {exc}"
            )
            self._timing_log(
                "vehicle_context_score_send_failed",
                started_at=started_at,
                target=f"{score_host}:{score_port}",
                error=f"{type(exc).__name__}: {exc}",
                force_send=force_send,
            )
            return True

    def _vehicle_context_launch_score_candidates(self) -> List[Dict[str, Any]]:
        raw = self._candidate_launch_points_for_vehicle(
            self.external_candidate_context,
            self.external_vehicle_state.get("port") or self.advertise_port,
        )
        rows: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for idx, item in enumerate(raw[:6]):
                row = dict(item) if isinstance(item, dict) else {"point_id": item}
                rank = int(row.get("rank", idx + 1) or idx + 1)
                node = None
                for key in ("node_id", "launch_node", "fire_point_id", "target_node", "point_id", "name", "index"):
                    node = self._resolve_external_point_node(row.get(key))
                    if node:
                        break
                if node:
                    row["launch_node"] = str(node)
                row["rank"] = rank
                rows.append(row)
        if rows:
            return rows

        fallback_rows: List[Dict[str, Any]] = []
        for idx, item in enumerate(self.external_raw_launch_rows[:6]):
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["rank"] = int(row.get("rank", idx + 1) or idx + 1)
            row["candidate_source"] = "raw_dian_lightweight_fallback"
            fallback_rows.append(row)
        return fallback_rows

    # 从外部候选上下文中提取“只属于本车”的候选发射点，并把点位解析成可规划的 launch_node。
    def _vehicle_context_launch_candidates(self) -> List[Dict[str, Any]]:
        raw = self._candidate_launch_points_for_vehicle(
            self.external_candidate_context,
            self.external_vehicle_state.get("port") or self.advertise_port,
        )
        rows: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for idx, item in enumerate(raw):
                if isinstance(item, dict):
                    rank = int(item.get("rank", idx + 1) or idx + 1)
                    node = None
                    for key in ("node_id", "launch_node", "fire_point_id", "target_node", "point_id", "name", "index"):
                        node = self._resolve_external_point_node(item.get(key))
                        if node:
                            break
                else:
                    rank = idx + 1
                    node = self._resolve_external_point_node(item)
                if not node and isinstance(item, dict):
                    node = self._nearest_graph_node_for_dian(item, "launch_candidate")
                if node:
                    row = dict(item) if isinstance(item, dict) else {}
                    row.update({"launch_node": str(node), "rank": rank})
                    if str(node) not in self.points.launch_points:
                        self.points.launch_points.append(str(node))
                    self.external_launch_rows_by_node[str(node)] = row
                    for key in self._dian_keys(row, idx):
                        self.external_point_node_map[key] = str(node)
                    for key in self._dian_aliases(row, idx, "launch"):
                        self.external_point_node_map[key] = str(node)
                    rows.append(row)
        if rows:
            return rows[:6]
        fallback_rows = self._fallback_launch_candidates_from_raw_dian()
        if fallback_rows:
            self.event_log.log(
                "vehicle_candidate_context_fallback_used",
                vehicle_id=self.vehicle_id,
                raw_launch_rows=len(self.external_raw_launch_rows),
                candidate_count=len(fallback_rows),
            )
            return fallback_rows
        return []

    def _fallback_launch_candidates_from_raw_dian(self) -> List[Dict[str, Any]]:
        if not self.external_raw_launch_rows:
            return []
        origin = self.graph.nodes.get(self.state.current_node)
        ranked: List[Tuple[float, int, Dict[str, Any], str]] = []
        for idx, item in enumerate(self.external_raw_launch_rows):
            if not isinstance(item, dict):
                continue
            node = self._nearest_graph_node_for_dian(item, f"launch_fallback_{idx:03d}")
            if not node or node not in self.graph.nodes:
                continue
            row = dict(item)
            point = self.graph.nodes[node]
            dist_sq = 0.0
            if origin is not None:
                dist_sq = (point.x - origin.x) ** 2 + (point.y - origin.y) ** 2
            ranked.append((dist_sq, idx, row, node))
        ranked.sort(key=lambda item: (item[0], item[1]))
        rows: List[Dict[str, Any]] = []
        for rank, (_dist_sq, idx, row, node) in enumerate(ranked[:6], start=1):
            row.update(
                {
                    "launch_node": str(node),
                    "node_id": str(node),
                    "rank": rank,
                    "candidate_source": "raw_dian_fallback",
                }
            )
            if str(node) not in self.points.launch_points:
                self.points.launch_points.append(str(node))
            self.external_launch_rows_by_node[str(node)] = row
            for key in self._dian_keys(row, idx):
                self.external_point_node_map[key] = str(node)
            for key in self._dian_aliases(row, idx, "launch"):
                self.external_point_node_map[key] = str(node)
            rows.append(row)
        return rows

    # 针对某一个候选发射点，提取它绑定的候选隐蔽点列表。
    # 这里会把原始点位 key 解析成已经投影到路网后的 hide node。
    def _candidate_hide_points_for_launch(self, candidate: Dict[str, Any]) -> List[str]:
        raw = candidate.get("hide_candidates")
        if raw is None:
            raw = candidate.get("candidate_hide_points") or []
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for idx, item in enumerate(raw):
            if isinstance(item, dict):
                node_id = None
                for key in ("node_id", "hide_node", "hide_point_id", "point_id", "name", "index"):
                    node_id = self._resolve_external_point_node(item.get(key))
                    if node_id:
                        break
            else:
                node_id = self._resolve_external_point_node(item)
            if not node_id and isinstance(item, dict):
                node_id = self._nearest_graph_node_for_dian(item, f"hide_candidate_{idx:03d}")
            if node_id and node_id in self.graph.nodes:
                if node_id not in self.points.hide_points:
                    self.points.hide_points.append(node_id)
                if isinstance(item, dict):
                    row = dict(item)
                    for key in self._dian_keys(row, idx):
                        self.external_point_node_map[key] = node_id
                    for key in self._dian_aliases(row, idx, "hide"):
                        self.external_point_node_map[key] = node_id
                if node_id not in out:
                    out.append(node_id)
        return out

    # 给外部节点的车辆评分采用轻量模式：
    # 这里只用于快速告诉节点“这辆车大致适不适合”，不参与真实路径生成。
    # 因此禁止在这里跑 route_between_nodes/HybridA*，避免评分链路被细路网拖慢。
    def _fast_vehicle_context_launch_score(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        launch_node = str(candidate.get("launch_node") or "")
        rank = max(1, int(candidate.get("rank", 1) or 1))
        current_x, current_y, current_node_id = self._vehicle_score_position_xy()
        target = self.graph.nodes.get(launch_node)
        target_x: Optional[float] = float(target.x) if target is not None else None
        target_y: Optional[float] = float(target.y) if target is not None else None
        if target_x is None or target_y is None:
            target_xy = self._dian_xy(candidate)
            if target_xy is not None:
                target_x, target_y = float(target_xy[0]), float(target_xy[1])
        distance_m = 0.0
        feasible = bool(current_x is not None and current_y is not None and target_x is not None and target_y is not None)
        if current_x is not None and current_y is not None and target_x is not None and target_y is not None:
            distance_m = math.hypot(target_x - current_x, target_y - current_y)
        travel_sec = distance_m / max(float(self.speed_mps), 1e-6) if feasible else 0.0
        distance_score = 100.0 / (1.0 + travel_sec / 5400.0) if feasible else 20.0
        rank_score = max(0.0, 100.0 - float(rank - 1) * 8.0)
        timing_score = 100.0 if (
            self.external_time_backplan_context.get("launch_prepare_time") is not None
            or self.external_time_backplan_context.get("launch_standby_time") is not None
            or self.external_time_backplan_context.get("fire_time") is not None
            or self.external_time_backplan_context.get("subtasks")
            or self.external_time_backplan_context.get("time_backplan_result")
            or self.external_time_backplan_context.get("stage_timeline")
        ) else 50.0
        health_score = 100.0 * max(0.0, min(1.0, float(self.vehicle_health)))
        fire_pool_in_zone = False
        current_node = self.graph.nodes.get(current_node_id or "")
        if current_node is not None and self.fire_pool_bounds:
            fire_pool_in_zone = node_in_bounds(current_node, self.fire_pool_bounds)
        elif current_x is not None and current_y is not None and len(self.fire_pool_bounds) >= 4:
            min_x, min_y, max_x, max_y = [float(v) for v in self.fire_pool_bounds[:4]]
            fire_pool_in_zone = min_x <= float(current_x) <= max_x and min_y <= float(current_y) <= max_y
        fire_pool_bonus = float(self.fire_pool_bonus) if fire_pool_in_zone else 0.0
        total = (
            0.45 * distance_score
            + 0.25 * rank_score
            + 0.20 * timing_score
            + 0.10 * health_score
            + fire_pool_bonus
        )
        if not feasible:
            total = min(total, 20.0)
        return {
            "vehicle_id": self.vehicle_id,
            "launch_node": launch_node,
            "feasible": feasible,
            "candidate_rank": rank,
            "travel_sec": round(travel_sec, 3),
            "distance_m": round(distance_m, 3),
            "score_distance": round(distance_score, 3),
            "score_candidate_rank": round(rank_score, 3),
            "score_timing": round(timing_score, 3),
            "score_health": round(health_score, 3),
            "fire_pool_in_zone": bool(fire_pool_in_zone),
            "score_fire_pool_bonus": round(fire_pool_bonus, 3),
            "score_total": round(max(0.0, min(100.0, total)), 3),
            "score_semantics": "fast_vehicle_context_score_no_route_search",
        }
    # 单个候选发射点评分：
    # 如果已经有规划结果，就按真实规划后的时间/等待/隐蔽点情况评分；
    # 否则退化成快速估计评分。
    def _score_vehicle_context_launch_candidate(
        self,
        candidate: Dict[str, Any],
        path_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        launch_node = str(candidate.get("launch_node") or "")
        rank = max(1, int(candidate.get("rank", 1) or 1))
        if path_report is not None:
            travel_sec = max(
                0.0,
                float(
                    path_report.get("travel_seconds")
                    or path_report.get("estimated_travel_seconds")
                    or 0.0
                ),
            )
            base = {
                "vehicle_id": self.vehicle_id,
                "launch_node": launch_node,
                "feasible": bool(path_report.get("feasible", True)),
                "travel_sec": round(travel_sec, 3),
                "score_distance": round(100.0 / (1.0 + travel_sec / 5400.0), 3) if travel_sec > 0.0 else 100.0,
                "timing_margin_sec": None,
                "timing_feasible": True,
            }
            fire_error = path_report.get("fire_time_error_sec")
            timing_score = 70.0
            late_penalty: Optional[float] = None
            if fire_error is not None:
                try:
                    fire_error_sec = float(fire_error)
                    timing_feasible = self._fire_time_error_acceptable(fire_error_sec)
                    timing_score = self._candidate_timing_score(0.0 if timing_feasible else -abs(fire_error_sec))
                    base["timing_margin_sec"] = round(0.0 if timing_feasible else -abs(fire_error_sec), 3)
                    base["fire_time_error_sec"] = round(fire_error_sec, 3)
                    base["timing_feasible"] = timing_feasible
                    if fire_error_sec > self.candidate_fire_time_grace_sec:
                        # Late arrivals are operationally bad: keep enough gradation for ranking,
                        # but force them far below any on-time candidate.
                        late_penalty = min(80.0, 35.0 + fire_error_sec / 30.0)
                except Exception:
                    pass
            hide_score = 100.0 if bool(path_report.get("hide_selected", False)) else 55.0
            distance_score = float(base["score_distance"])
            task_book_score = score_vehicle_task_execution(
                vehicle_id=self.vehicle_id,
                current_node=self.state.current_node,
                launch_node=launch_node,
                ammo_types=self.state.ammo_types,
                required_ammo_type=None,
                speed_mps=self.speed_mps,
                graph=self.graph,
                lane_graph=self.lane_graph,
                request_time=self.external_vehicle_state.get("current_time"),
                desired_fire_time=(
                    self.external_time_backplan_context.get("launch_prepare_time")
                    or self.external_time_backplan_context.get("fire_time")
                ),
                fire_time_grace_sec=self.candidate_fire_time_grace_sec,
                wait_seconds=float(path_report.get("wait_seconds", 0.0) or 0.0),
                launch_prepare_seconds=self.launch_prepare_sec,
                hide_selected=bool(path_report.get("hide_selected", False)),
                fire_time_error_sec=path_report.get("fire_time_error_sec"),
                ignore_speed_limits=True,
            )
        else:
            desired_time = (
                self.external_time_backplan_context.get("launch_prepare_time")
                or self.external_time_backplan_context.get("fire_time")
            )
            base = vehicle_score(
                vehicle_id=self.vehicle_id,
                current_node=self.state.current_node,
                launch_node=launch_node,
                ammo_types=self.state.ammo_types,
                required_ammo_type=None,
                speed_mps=self.speed_mps,
                health=float(self.cfg.get("health", 1.0)),
                graph=self.graph,
                lane_graph=self.lane_graph,
                request_time=self.external_vehicle_state.get("current_time"),
                desired_fire_time=desired_time,
                fire_time_grace_sec=15.0,
                ignore_speed_limits=True,
            )
            travel_sec = max(0.0, float(base.get("travel_sec", 0.0) or 0.0))
            base_distance_score = float(base.get("score_distance", 0.0) or 0.0)
            smooth_distance_score = 100.0 / (1.0 + travel_sec / 5400.0) if travel_sec > 0.0 else 100.0
            distance_score = max(
                self._amplify_candidate_score(base_distance_score, power=1.4),
                smooth_distance_score,
            )
            timing_score = self._candidate_timing_score(base.get("timing_margin_sec"))
            hide_score = self._hide_route_score(launch_node)
            task_book_score = score_vehicle_task_execution(
                vehicle_id=self.vehicle_id,
                current_node=self.state.current_node,
                launch_node=launch_node,
                ammo_types=self.state.ammo_types,
                required_ammo_type=None,
                speed_mps=self.speed_mps,
                graph=self.graph,
                lane_graph=self.lane_graph,
                request_time=self.external_vehicle_state.get("current_time"),
                desired_fire_time=desired_time,
                fire_time_grace_sec=self.candidate_fire_time_grace_sec,
                wait_seconds=0.0,
                launch_prepare_seconds=self.launch_prepare_sec,
                hide_selected=False,
                fire_time_error_sec=None,
                ignore_speed_limits=True,
            )
        hide_route_score = self._amplify_candidate_score(hide_score, power=1.6)
        backplan_score = 100.0 if (
            self.external_time_backplan_context.get("launch_prepare_time") is not None
            or self.external_time_backplan_context.get("launch_standby_time") is not None
            or self.external_time_backplan_context.get("fire_time") is not None
            or self.external_time_backplan_context.get("subtasks")
            or self.external_time_backplan_context.get("time_backplan_result")
            or self.external_time_backplan_context.get("stage_timeline")
        ) else 50.0
        total = float(task_book_score.get("score_total", 0.0) or 0.0)
        if backplan_score < 100.0:
            total -= 3.0
        if path_report is not None and not bool(base.get("feasible", True)):
            total = min(total, 10.0)
        if path_report is not None and late_penalty is not None:
            total = max(0.0, min(total, 45.0) - late_penalty)
        base.update(
            {
                "score_total_base": round(float(base.get("score_total", 0.0) or 0.0), 3),
                "candidate_rank": rank,
                "score_candidate_rank": 0.0,
                "score_distance_amplified": round(distance_score, 3),
                "score_timing_amplified": round(timing_score, 3),
                "score_hide_route": round(hide_score, 3),
                "score_hide_route_amplified": round(hide_route_score, 3),
                "score_backplan": round(backplan_score, 3),
                "score_task_book_maneuver": round(float(task_book_score.get("score_maneuver", 0.0) or 0.0), 3),
                "score_task_book_waiting": round(float(task_book_score.get("score_waiting", 0.0) or 0.0), 3),
                "score_task_book_launch_prepare": round(float(task_book_score.get("score_launch_prepare", 0.0) or 0.0), 3),
                "score_task_book_hide_action": round(float(task_book_score.get("score_hide_action", 0.0) or 0.0), 3),
                "score_task_book_total": round(float(task_book_score.get("score_total", 0.0) or 0.0), 3),
                "score_total": round(max(0.0, min(100.0, total)), 3),
            }
        )
        bonus_row = task_book_vehicle_score(
            vehicle_id=self.vehicle_id,
            current_node=self.state.current_node,
            launch_node=launch_node,
            ammo_types=self.state.ammo_types,
            required_ammo_type=None,
            speed_mps=self.speed_mps,
            health=self.vehicle_health,
            graph=self.graph,
            lane_graph=None,
            request_time=self.external_vehicle_state.get("current_time"),
            desired_fire_time=(
                self.external_time_backplan_context.get("launch_prepare_time")
                or self.external_time_backplan_context.get("fire_time")
            ),
            fire_time_grace_sec=self.candidate_fire_time_grace_sec,
            ignore_speed_limits=True,
            fire_pool_bounds_xy=self.fire_pool_bounds,
            fire_pool_bonus=self.fire_pool_bonus,
            health_weight=self.vehicle_health_weight,
        )
        bonus = float(bonus_row.get("score_fire_pool_bonus", 0.0) or 0.0)
        existing_bonus = float(base.get("score_fire_pool_bonus", 0.0) or 0.0)
        bonus_delta = max(0.0, bonus - existing_bonus)
        adjusted_total = max(0.0, min(100.0, float(base.get("score_total", 0.0) or 0.0) + bonus_delta))
        base.update(
            {
                "vehicle_health": bonus_row.get("vehicle_health"),
                "fire_pool_in_zone": bonus_row.get("fire_pool_in_zone"),
                "score_fire_pool_bonus": round(bonus, 3),
                "score_total_before_fire_pool": base.get("score_total"),
                "score_total": round(adjusted_total, 3),
            }
        )
        return base

    @staticmethod
    def _amplify_candidate_score(score: float, *, power: float) -> float:
        clamped = max(0.0, min(100.0, float(score)))
        return 100.0 * ((clamped / 100.0) ** max(1.0, float(power)))

    def _candidate_timing_score(self, timing_margin_sec: Optional[Any]) -> float:
        if timing_margin_sec is None:
            return 50.0
        try:
            margin = float(timing_margin_sec)
        except Exception:
            return 50.0
        grace = 15.0
        if margin <= -grace:
            return 0.0
        if margin < 0.0:
            return max(0.0, 40.0 * (margin + grace) / grace)
        if margin <= 180.0:
            return 40.0 + 40.0 * (margin / 180.0)
        if margin <= 900.0:
            return 80.0 + 20.0 * ((margin - 180.0) / 720.0)
        return 100.0

    def _fire_time_error_acceptable(self, fire_time_error_sec: Any) -> bool:
        try:
            error = float(fire_time_error_sec)
        except Exception:
            return False
        return -self.candidate_fire_time_early_grace_sec <= error <= self.candidate_fire_time_grace_sec

    def _hide_route_score(self, launch_node: str) -> float:
        if not launch_node or launch_node not in self.graph.nodes or not self.points.hide_points:
            return 50.0
        best_sec: Optional[float] = None
        for hide in self._rank_hide_points(self.state.current_node, launch_node, list(self.points.hide_points)):
            if hide not in self.graph.nodes:
                continue
            lane1, route1 = self.lane_graph.route_between_nodes(
                self.state.current_node,
                hide,
                speed_cap_mps=self.speed_mps,
                ignore_speed_limits=True,
            )
            lane2, route2 = self.lane_graph.route_between_nodes(
                hide,
                launch_node,
                speed_cap_mps=self.speed_mps,
                ignore_speed_limits=True,
            )
            if not lane1 or not lane2:
                continue
            travel_sec = float(route1.travel_sec) + float(route2.travel_sec)
            best_sec = travel_sec if best_sec is None else min(best_sec, travel_sec)
        if best_sec is None:
            return 30.0
        return max(0.0, min(100.0, 100.0 - best_sec / 3600.0 * 100.0))
    # 单个候选发射点的完整路径规划：
    # 先按冷发射做一次，如果没有用上隐蔽点且有必要，再尝试热待机版本。
    def _plan_candidate_path_option(
        self,
        candidate: Dict[str, Any],
        hide_points: List[str],
        current_time: Any,
        launch_prepare_time: Any,
        desired_fire_time: Any,
        option_index: int,
    ) -> Dict[str, Any]:
        option_started = time.perf_counter()
        launch_node = str(candidate.get("launch_node") or "")
        desired_arrival = self._subtract_time_seconds(launch_prepare_time, self.cold_standby_sec)
        launch_startup_sec = self.launch_prepare_sec + self.cold_standby_sec
        launch_startup_mode = "cold"
        # 真正的单条路径生成入口，内部会决定是直达、去隐蔽点等待还是起点等待。
        node_path, edge_sec, wait_sec, wait_node_index, _trajectory, path_report = self._propose_path(
            phase="to_fire",
            start_node=self.state.current_node,
            target_node=launch_node,
            desired_arrival=desired_arrival,
            desired_fire_time=desired_fire_time,
            earliest_start=current_time,
            hide_points=hide_points,
            launch_startup_sec=launch_startup_sec,
            launch_startup_mode=launch_startup_mode,
        )
        if not bool(path_report.get("hide_selected", False)) and wait_sec > 0.0 and hide_points:
            desired_arrival = self._subtract_time_seconds(launch_prepare_time, self.hot_standby_sec)
            launch_startup_sec = self.launch_prepare_sec + self.hot_standby_sec
            launch_startup_mode = "hot"
            hot_result = self._propose_path(
                phase="to_fire",
                start_node=self.state.current_node,
                target_node=launch_node,
                desired_arrival=desired_arrival,
                desired_fire_time=desired_fire_time,
                earliest_start=current_time,
                hide_points=hide_points,
                launch_startup_sec=launch_startup_sec,
                launch_startup_mode=launch_startup_mode,
            )
            if bool(hot_result[5].get("hide_selected", False)):
                node_path, edge_sec, wait_sec, wait_node_index, _trajectory, path_report = hot_result

        score = self._score_vehicle_context_launch_candidate(candidate, path_report)
        wait_start_time, wait_end_time = self._path_wait_interval_times(
            current_time,
            edge_sec,
            wait_sec,
            wait_node_index,
        )
        timing_strategy = str(path_report.get("timing_strategy") or "direct")
        violations = [str(item) for item in path_report.get("violations") or []]
        reject_reason = ""
        if not node_path or len(node_path) < 2:
            reject_reason = "no_reachable_launch"
        elif not bool(path_report.get("feasible", True)):
            reject_reason = violations[0] if violations else "time_infeasible"
        fallback_only = timing_strategy == "start_wait"
        result = {
            "rank": int(candidate.get("rank", 1) or 1),
            "launch_node": launch_node,
            "fire_point_id": candidate.get("fire_point_id") or launch_node,
            "hide_point": path_report.get("hide_point"),
            "hide_selected": bool(path_report.get("hide_selected", False)),
            "timing_strategy": timing_strategy,
            "wait_node_index": wait_node_index,
            "wait_start_time": wait_start_time,
            "wait_end_time": wait_end_time,
            "wait_seconds": round(float(wait_sec), 3),
            "fire_time_error_sec": path_report.get("fire_time_error_sec"),
            "detour_ratio": path_report.get("detour_ratio", 1.0),
            "route_distance_m": path_report.get("route_distance_m") or path_report.get("path_distance_m"),
            "travel_seconds": path_report.get("travel_seconds") or path_report.get("estimated_travel_seconds"),
            "score_total": score.get("score_total", 0.0),
            "feasible": bool(path_report.get("feasible", True)) and not reject_reason,
            "fallback_only": fallback_only,
            "reject_reason": reject_reason,
            "path_points": self._trajectory_geo_from_node_path(
                node_path,
                edge_sec,
                current_time,
                wait_seconds=wait_sec,
                wait_node_index=wait_node_index,
            ),
            "timing_total_until_fire_sec": path_report.get("timing_total_until_fire_sec"),
            "launch_startup_mode": path_report.get("launch_startup_mode"),
        }
        self._timing_log(
            "candidate_path_option_planned",
            started_at=option_started,
            launch_node=launch_node,
            rank=candidate.get("rank"),
            option_index=option_index,
            hide_count=len(hide_points),
            hide_selected=bool(result.get("hide_selected")),
            timing_strategy=result.get("timing_strategy"),
            feasible=bool(result.get("feasible")),
            reject_reason=result.get("reject_reason"),
            fire_time_error_sec=result.get("fire_time_error_sec"),
        )
        return result

    def _plan_post_fire_depot_candidates(
        self,
        launch_node: str,
        fire_time: Any,
        candidate_depots: Any,
    ) -> List[Dict[str, Any]]:
        # 任务书“发射后到贮备库补给再回等待点/起点”：这里只在收到调度正式分配的贮备库后执行。
        # 第一次候选路径规划只覆盖射前段，避免贮备库数据放大候选 JSON。
        if (
            not self._external_depot_points_loaded
            or not self.points.depots
            or not isinstance(candidate_depots, list)
            or not candidate_depots
            or fire_time in {None, ""}
            or launch_node not in self.graph.nodes
        ):
            return []
        return_node = (
            self.external_mission_origin_node
            or (self.state.home_node if self.state.home_node in self.graph.nodes else self.state.current_node)
        )
        if return_node not in self.graph.nodes:
            return []
        requested_nodes: List[str] = []
        requested_rank: Dict[str, int] = {}
        for index, item in enumerate(candidate_depots):
            if isinstance(item, dict):
                depot_node = None
                for key in ("depot_node", "depot_id", "depot_port", "point_id", "name", "index"):
                    depot_node = self._resolve_external_point_node(item.get(key))
                    if depot_node:
                        break
                rank = int(item.get("rank", index + 1) or index + 1)
            else:
                depot_node = self._resolve_external_point_node(item)
                rank = index + 1
            if depot_node and depot_node in self.points.depots and depot_node not in requested_nodes:
                requested_nodes.append(depot_node)
                requested_rank[depot_node] = rank
        if not requested_nodes:
            return []
        rows: List[Dict[str, Any]] = []
        for depot_node in requested_nodes:
            to_depot_nodes, _to_depot_trajectory, to_depot_report = self._plan_route_bundle(
                launch_node,
                depot_node,
                "to_depot",
            )
            return_nodes, _return_trajectory, return_report = self._plan_route_bundle(
                depot_node,
                return_node,
                "return_home",
            )
            if not to_depot_nodes or not bool(to_depot_report.feasible):
                continue
            if not return_nodes or not bool(return_report.feasible):
                continue
            to_depot_edges = self._edge_seconds_from_path(to_depot_nodes, "to_depot")
            return_edges = self._edge_seconds_from_path(return_nodes, "return_home")
            to_depot_sec = sum(to_depot_edges)
            return_sec = sum(return_edges)
            depot_row = self.external_depot_rows_by_node.get(depot_node, {})
            reload_sec = max(
                0.0,
                float(depot_row.get("reload_duration_sec", self.reload_duration_sec) or self.reload_duration_sec),
            )
            depot_arrival = self._add_time_seconds(fire_time, to_depot_sec)
            reload_end = self._add_time_seconds(depot_arrival, reload_sec)
            finish_time = self._add_time_seconds(reload_end, return_sec)
            to_depot_points = self._trajectory_geo_from_node_path(
                to_depot_nodes,
                to_depot_edges,
                fire_time,
            )
            return_points = self._trajectory_geo_from_node_path(
                return_nodes,
                return_edges,
                reload_end,
            )
            post_points = list(to_depot_points)
            if post_points:
                reload_point = dict(post_points[-1])
                reload_point["time"] = reload_end
                post_points.append(reload_point)
            if return_points:
                post_points.extend(return_points[1:] if post_points else return_points)
            depot_id = str(
                depot_row.get("id")
                or depot_row.get("point_id")
                or depot_row.get("name")
                or depot_row.get("index")
                or depot_node
            )
            rows.append(
                {
                    "depot_id": depot_id,
                    "depot_node": depot_node,
                    "depot_port": depot_row.get("port") or depot_row.get("vehicle_id"),
                    "scheduler_candidate_rank": requested_rank.get(depot_node),
                    "capacity": int(depot_row.get("capacity", 16) or 16),
                    "reload_duration_sec": round(reload_sec, 3),
                    "travel_sec_from_launch": round(to_depot_sec, 3),
                    "return_home_sec": round(return_sec, 3),
                    "total_post_fire_sec": round(to_depot_sec + reload_sec + return_sec, 3),
                    "depot_arrival_time": depot_arrival,
                    "reload_end_time": reload_end,
                    "finish_time": finish_time,
                    "to_depot_point_count": len(to_depot_points),
                    "path_points": post_points,
                    "segments": [
                        {"kind": "to_depot", "start_at": fire_time, "end_at": depot_arrival},
                        {"kind": "reload", "start_at": depot_arrival, "end_at": reload_end},
                        {"kind": "return_home", "start_at": reload_end, "end_at": finish_time},
                    ],
                }
            )
        rows.sort(
            key=lambda row: (
                float(row.get("total_post_fire_sec", float("inf")) or float("inf")),
                str(row.get("depot_id") or ""),
            )
        )
        for rank, row in enumerate(rows[: self.max_depot_candidates], start=1):
            row["rank"] = rank
        return rows[: self.max_depot_candidates]

    def _on_vehicle_depot_assignment(self, env: Envelope, addr: MessageAddress) -> None:
        payload = dict(env.payload or {})
        assigned_vehicle_id = str(payload.get("vehicle_id") or "")
        if assigned_vehicle_id and assigned_vehicle_id != str(self.vehicle_id):
            return
        task_id = str(payload.get("task_id") or "")
        launch_value = payload.get("launch_node") or payload.get("fire_point_id")
        launch_node = self._resolve_external_point_node(launch_value)
        if launch_node is None and payload.get("fire_point_id") not in {None, ""}:
            launch_node = self._resolve_external_point_node(payload.get("fire_point_id"))
        depot = dict(payload.get("depot") or {})
        for key in ("depot_id", "depot_node", "depot_port", "capacity", "reload_duration_sec"):
            if depot.get(key) in {None, ""} and payload.get(key) not in {None, ""}:
                depot[key] = payload.get(key)
        assigned_depot_node = self._ensure_assigned_depot_node(depot)
        fire_time = payload.get("fire_time")
        response_addr = reply_address(payload, addr)
        if launch_node is None or response_addr is None:
            self.event_log.log(
                "post_fire_path_planning_failed",
                task_id=task_id,
                vehicle_id=self.vehicle_id,
                reason="missing_launch_node_or_reply_address",
                launch_value=launch_value,
            )
            return
        rows = self._plan_post_fire_depot_candidates(launch_node, fire_time, [depot])
        if not rows:
            self.event_log.log(
                "post_fire_path_planning_failed",
                task_id=task_id,
                vehicle_id=self.vehicle_id,
                reason="assigned_depot_unreachable_or_unresolved",
                launch_node=launch_node,
                resolved_depot_node=assigned_depot_node,
                depot_id=depot.get("depot_id"),
                depot_port=depot.get("depot_port"),
            )
            return
        result = dict(rows[0])
        result.update(
            {
                "task_id": task_id,
                "subtask_id": payload.get("subtask_id"),
                "vehicle_id": str(self.vehicle_id),
                "port": self.advertise_port,
                "launch_node": launch_node,
                "fire_point_id": payload.get("fire_point_id"),
            }
        )
        self._send_raw_json(MSG_VEHICLE_POST_FIRE_PATH_RESULT, result, response_addr)
        self.event_log.log(
            "post_fire_path_result_sent",
            task_id=task_id,
            vehicle_id=self.vehicle_id,
            depot_id=result.get("depot_id"),
            path_points=len(result.get("path_points") or []),
            finish_time=result.get("finish_time"),
            addr=f"{response_addr[0]}:{response_addr[1]}",
        )
    # 车辆候选路径生成主入口：
    # 对本车前几个候选发射点逐个规划，产出 paths 后回传给模型/调度。
    def _submit_candidate_paths_to_model(self) -> None:
        started_at = time.perf_counter()
        if not self._runtime_vehicle_position_ready():
            self.event_log.log(
                "candidate_path_skipped",
                vehicle_id=self.vehicle_id,
                reason="missing_valid_runtime_vehicle_position",
            )
            self._timing_log(
                "candidate_path_skipped",
                started_at=started_at,
                reason="missing_valid_runtime_vehicle_position",
            )
            return
        if not self.external_time_backplan_context or not self.external_candidate_context:
            self._timing_log(
                "candidate_path_skipped",
                started_at=started_at,
                reason="missing_context",
                has_time_backplan=bool(self.external_time_backplan_context),
                has_candidate_context=bool(self.external_candidate_context),
            )
            return
        task_id = str(self.external_candidate_context.get("task_id") or "")
        if task_id:
            with self._lock:
                if self._candidate_paths_submitted_task_id == task_id:
                    skip_reason = "already_submitted"
                elif task_id in self._candidate_paths_inflight_task_ids:
                    skip_reason = "submit_in_progress"
                else:
                    skip_reason = ""
                    self._candidate_paths_inflight_task_ids.add(task_id)
            if skip_reason:
                self.event_log.log(
                    "candidate_path_skipped",
                    vehicle_id=self.vehicle_id,
                    task_id=task_id,
                    reason=skip_reason,
                )
                self._timing_log(
                    "candidate_path_skipped",
                    started_at=started_at,
                    task_id=task_id,
                    reason=skip_reason,
                )
                return
        candidates = self._vehicle_context_launch_candidates()
        if not candidates:
            if task_id:
                with self._lock:
                    self._candidate_paths_inflight_task_ids.discard(task_id)
            self.event_log.log(
                "candidate_path_skipped",
                vehicle_id=self.vehicle_id,
                reason="missing_runtime_candidate_launch_points",
                launch_loaded=self._external_launch_points_loaded,
                hide_loaded=self._external_hide_points_loaded,
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_skipped "
                f"reason=missing_runtime_candidate_launch_points launch_loaded={self._external_launch_points_loaded} "
                f"hide_loaded={self._external_hide_points_loaded}"
            )
            self._timing_log(
                "candidate_path_skipped",
                started_at=started_at,
                reason="missing_runtime_candidate_launch_points",
                launch_loaded=self._external_launch_points_loaded,
                hide_loaded=self._external_hide_points_loaded,
            )
            return
        if not self._external_launch_points_loaded or not self._external_hide_points_loaded:
            self.event_log.log(
                "candidate_path_runtime_dian_partial",
                vehicle_id=self.vehicle_id,
                launch_loaded=self._external_launch_points_loaded,
                hide_loaded=self._external_hide_points_loaded,
                candidates=len(candidates),
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_runtime_dian_partial "
                f"launch_loaded={self._external_launch_points_loaded} hide_loaded={self._external_hide_points_loaded} "
                f"candidates={len(candidates)}"
            )
        hide_points = list(self.points.hide_points) if self._external_hide_points_loaded else []
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] submit_candidate_hide_points "
            f"hide_points={len(hide_points)} launch_points={len(self.points.launch_points)} "
            f"candidates={len(candidates[:6])}"
        )
        self.event_log.log(
            "submit_candidate_hide_points",
            vehicle_id=self.vehicle_id,
            hide_points=len(hide_points),
            launch_points=len(self.points.launch_points),
            candidates=len(candidates[:6]),
        )
        launch_prepare_time = self.external_time_backplan_context.get("launch_prepare_time")
        desired_fire_time = self.external_time_backplan_context.get("fire_time")
        current_time = self.external_vehicle_state.get("current_time")
        planning_started = time.perf_counter()
        paths: List[Dict[str, Any]] = []
        fallback_paths: List[Dict[str, Any]] = []
        rejected_paths = 0
        # 逐个候选发射点规划路径，并分别判断可用/兜底/拒绝原因。
        for candidate in candidates[:6]:
            candidate_started = time.perf_counter()
            launch_node = str(candidate.get("launch_node") or "")
            if not launch_node:
                continue
            raw_candidate_hide_points = candidate.get("hide_candidates")
            # 每个候选发射点可以携带自己的候选隐蔽点集合，优先使用这一组做规划。
            if raw_candidate_hide_points is None:
                raw_candidate_hide_points = candidate.get("candidate_hide_points")
            candidate_hide_points = self._candidate_hide_points_for_launch(candidate)
            if isinstance(raw_candidate_hide_points, list) and raw_candidate_hide_points and not candidate_hide_points:
                self.event_log.log(
                    "candidate_hide_points_unresolved",
                    vehicle_id=self.vehicle_id,
                    launch_node=launch_node,
                    raw_count=len(raw_candidate_hide_points),
                    fallback_hide_points=0,
                )
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] candidate_hide_points_unresolved "
                    f"launch_node={launch_node} raw_count={len(raw_candidate_hide_points)} "
                    f"fallback_hide_points=0"
                )
            # 调度端已经按“车辆-发射点”绑定了候选隐蔽点；这里必须全部尝试，
            # 否则第 3/4 个可行隐蔽点会被车辆端直接丢掉，表现成明明有隐蔽点却 start_wait。
            option_hide_sets = [[hide_node] for hide_node in candidate_hide_points[: self.max_hide_candidates]]
            if bool(candidate.get("allow_direct", True)) or not option_hide_sets:
                option_hide_sets.append([])
            for option_index, option_hide_points in enumerate(option_hide_sets, start=1):
                if len(paths) + len(fallback_paths) >= self.max_candidate_path_options:
                    break
                path_payload = self._plan_candidate_path_option(
                    candidate,
                    option_hide_points,
                    current_time,
                    launch_prepare_time,
                    desired_fire_time,
                    option_index,
                )
                if self._candidate_path_payload_usable(path_payload):
                    paths.append(path_payload)
                else:
                    fallback_paths.append(path_payload)
                    rejected_paths += 1
                    debug_append_log(
                        f"[Vehicle {self.vehicle_id}] candidate_path_rejected launch={launch_node} "
                        f"rank={path_payload.get('rank')} option={path_payload.get('option_id')} "
                        f"reason={path_payload.get('reject_reason')} "
                        f"fire_error={path_payload.get('fire_time_error_sec')}"
                    )
            candidate_sec = time.perf_counter() - candidate_started
            self.event_log.log(
                "candidate_path_planning_candidate_timing",
                vehicle_id=self.vehicle_id,
                task_id=self.external_candidate_context.get("task_id"),
                launch_node=launch_node,
                rank=candidate.get("rank"),
                option_count=len(option_hide_sets),
                elapsed_sec=round(candidate_sec, 6),
            )
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_planning_candidate_timing "
                f"launch={launch_node} rank={candidate.get('rank')} "
                f"options={len(option_hide_sets)} "
                f"elapsed_sec={candidate_sec:.6f}"
            )
        if not paths and fallback_paths:
            fallback_paths.sort(key=lambda item: (int(item.get("rank", 999) or 999), abs(float(item.get("fire_time_error_sec", 0.0) or 0.0))))
            fallback_paths[0]["fallback_only"] = True
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_fallback_only "
                f"launch={fallback_paths[0].get('launch_node')} "
                f"fire_error={fallback_paths[0].get('fire_time_error_sec')}"
            )
        returned_paths = sorted(
            paths + fallback_paths,
            key=lambda item: int(item.get("rank", 999) or 999),
        )
        payload = {
            "vehicle_id": self.vehicle_id,
            "port": self.advertise_port,
            "task_id": self.external_candidate_context.get("task_id"),
            "paths": returned_paths,
            "candidate_count": len(candidates[:6]),
            "usable_path_count": len(paths),
            "rejected_path_count": rejected_paths,
        }
        planning_sec = time.perf_counter() - planning_started
        send_started = time.perf_counter()
        send_success = False
        try:
            self._send_raw_json(MSG_VEHICLE_CANDIDATE_PATH_RESULT, payload, self.candidate_result_addr)
            send_success = True
            if task_id:
                with self._lock:
                    self._candidate_paths_submitted_task_id = task_id
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_paths_sent target={self.candidate_result_addr} "
                f"paths={len(returned_paths)} usable={len(paths)} rejected={rejected_paths}"
            )
        except Exception as exc:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_paths_send_failed target={self.candidate_result_addr} "
                f"error={type(exc).__name__}: {exc}"
            )
        send_sec = time.perf_counter() - send_started
        total_sec = time.perf_counter() - planning_started
        self.event_log.log(
            "candidate_path_planning_timing",
            vehicle_id=self.vehicle_id,
            task_id=self.external_candidate_context.get("task_id"),
            candidate_count=len(candidates[:6]),
            path_count=len(returned_paths),
            usable_path_count=len(paths),
            rejected_path_count=rejected_paths,
            planning_sec=round(planning_sec, 6),
            send_sec=round(send_sec, 6),
            total_sec=round(total_sec, 6),
            send_success=send_success,
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] candidate_path_planning_timing "
            f"candidates={len(candidates[:6])} paths={len(returned_paths)} "
            f"usable={len(paths)} rejected={rejected_paths} "
            f"planning_sec={planning_sec:.6f} send_sec={send_sec:.6f} "
            f"total_sec={total_sec:.6f} send_success={send_success}"
        )
        self._timing_log(
            "candidate_path_planning_total",
            elapsed_sec=total_sec,
            task_id=self.external_candidate_context.get("task_id"),
            candidate_count=len(candidates[:6]),
            path_count=len(returned_paths),
            usable_path_count=len(paths),
            rejected_path_count=rejected_paths,
            planning_sec=round(planning_sec, 6),
            send_sec=round(send_sec, 6),
            send_success=send_success,
        )
        if task_id:
            with self._lock:
                self._candidate_paths_inflight_task_ids.discard(task_id)

    def _candidate_path_payload_usable(self, path: Dict[str, Any]) -> bool:
        if not bool(path.get("feasible", True)):
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_unusable "
                f"reason=not_feasible launch={path.get('launch_node')} rank={path.get('rank')} "
                f"fire_error={path.get('fire_time_error_sec')} strategy={path.get('timing_strategy')}"
            )
            return False
        points = path.get("path_points")
        if not isinstance(points, list) or len(points) < 2:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_unusable "
                f"reason=path_points launch={path.get('launch_node')} rank={path.get('rank')} "
                f"path_points={0 if not isinstance(points, list) else len(points)}"
            )
            return False
        fire_error = path.get("fire_time_error_sec")
        if fire_error in {None, ""}:
            return True
        try:
            usable = self._fire_time_error_acceptable(fire_error)
            if not usable:
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] candidate_path_unusable "
                    f"reason=fire_time_error launch={path.get('launch_node')} rank={path.get('rank')} "
                    f"fire_error={fire_error} early_grace={self.candidate_fire_time_early_grace_sec} "
                    f"late_grace={self.candidate_fire_time_grace_sec}"
                )
            return usable
        except Exception:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] candidate_path_unusable "
                f"reason=fire_time_parse launch={path.get('launch_node')} rank={path.get('rank')} "
                f"fire_error={fire_error}"
            )
            return False

    def _path_wait_interval_times(
        self,
        start_at: Optional[Any],
        edge_seconds: List[float],
        wait_seconds: float,
        wait_node_index: Optional[int],
    ) -> Tuple[Optional[Any], Optional[Any]]:
        if wait_seconds <= 0 or wait_node_index is None:
            return None, None
        try:
            wait_idx = max(0, int(wait_node_index))
        except Exception:
            return None, None
        current_time, numeric_time = self._trajectory_start_time(start_at)
        travel_before_wait = sum(float(item or 0.0) for item in edge_seconds[:wait_idx])
        if numeric_time:
            wait_start = float(current_time) + travel_before_wait
            return round(wait_start, 3), round(wait_start + float(wait_seconds), 3)
        wait_start_dt = current_time + timedelta(seconds=travel_before_wait)
        wait_end_dt = wait_start_dt + timedelta(seconds=float(wait_seconds))
        return wait_start_dt.isoformat(), wait_end_dt.isoformat()

    def _start_lane_graph_prewarm(self) -> None:
        prewarm_cfg = self._lane_graph_prewarm_cfg
        if not bool(prewarm_cfg.get("enabled", False)):
            return
        mode = str(prewarm_cfg.get("mode", "background")).lower()
        if mode in {"off", "disabled", "false"}:
            return
        if mode == "sync":
            self._prewarm_lane_graph(prewarm_cfg)
            return
        self._lane_graph_prewarm_thread = threading.Thread(
            target=self._prewarm_lane_graph,
            args=(prewarm_cfg,),
            daemon=True,
            name=f"{self.vehicle_id}-lane-prewarm",
        )
        self._lane_graph_prewarm_thread.start()

    def _prewarm_lane_graph(self, prewarm_cfg: dict) -> None:
        include_hide = bool(prewarm_cfg.get("include_hide_points", True))
        start_nodes = {self.state.home_node}
        if include_hide:
            start_nodes.update(self.points.hide_points)
        goal_nodes = set(self.points.launch_points)
        goal_nodes.add(self.state.home_node)
        if include_hide:
            goal_nodes.update(self.points.hide_points)
        started = time.perf_counter()
        stats = self.lane_graph.prewarm_routes(
            sorted(start_nodes),
            sorted(goal_nodes),
            speed_caps_mps=[min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps)],
        )
        stats["wall_sec"] = round(time.perf_counter() - started, 3)
        self.event_log.log("lane_graph_prewarm", **stats)

    def _trajectory_geo_from_node_path(
        self,
        node_path: List[str],
        edge_seconds: List[float],
        start_at: Optional[Any],
        wait_seconds: float = 0.0,
        wait_node_index: Optional[int] = None,
    ) -> List[dict]:
        current_time, numeric_time = self._trajectory_start_time(start_at)

        def point_for(node_id: str, when: Any) -> Optional[dict]:
            node = self.graph.nodes.get(node_id)
            if node is None:
                return None
            lon = node.lon if node.lon is not None else node.x
            lat = node.lat if node.lat is not None else node.y
            return {
                "lon": round(float(lon), 8),
                "lat": round(float(lat), 8),
                "alt": round(float(node.alt), 3),
                "time": round(float(when), 3) if numeric_time else when.isoformat(),
            }

        def edge_polyline(a_id: str, b_id: str) -> List[Dict[str, float]]:
            meta = self.graph.get_edge_meta(a_id, b_id)
            pts: List[Dict[str, float]] = []
            if meta and meta.geometry:
                raw = list(meta.geometry)
                if not (meta.geom_from == a_id and meta.geom_to == b_id):
                    raw = list(reversed(raw))
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    x = item.get("x")
                    y = item.get("y")
                    if x is None or y is None:
                        continue
                    lon = item.get("lon")
                    lat = item.get("lat")
                    if lon is None or lat is None:
                        mapped = self._graph_xy_to_lonlat(float(x), float(y))
                        if mapped is not None:
                            lon, lat = mapped
                    if lon is None or lat is None:
                        continue
                    pts.append(
                        {
                            "x": float(x),
                            "y": float(y),
                            "lon": float(lon),
                            "lat": float(lat),
                            "alt": float(item.get("alt", 0.0)),
                        }
                    )
            if len(pts) >= 2:
                return pts
            a = self.graph.nodes.get(a_id)
            b = self.graph.nodes.get(b_id)
            if a is None or b is None:
                return []
            a_lon = a.lon if a.lon is not None else a.x
            a_lat = a.lat if a.lat is not None else a.y
            b_lon = b.lon if b.lon is not None else b.x
            b_lat = b.lat if b.lat is not None else b.y
            return [
                {"x": float(a.x), "y": float(a.y), "lon": float(a_lon), "lat": float(a_lat), "alt": float(a.alt)},
                {"x": float(b.x), "y": float(b.y), "lon": float(b_lon), "lat": float(b_lat), "alt": float(b.alt)},
            ]

        def interp_point(a_id: str, b_id: str, ratio: float, when: Any) -> Optional[dict]:
            poly = edge_polyline(a_id, b_id)
            if len(poly) < 2:
                return None
            lengths = [
                math.hypot(poly[i + 1]["x"] - poly[i]["x"], poly[i + 1]["y"] - poly[i]["y"])
                for i in range(len(poly) - 1)
            ]
            total = sum(lengths)
            if total <= 1e-9:
                p = poly[0]
                return {
                    "lon": round(float(p["lon"]), 8),
                    "lat": round(float(p["lat"]), 8),
                    "alt": round(float(p["alt"]), 3),
                    "time": round(float(when), 3) if numeric_time else when.isoformat(),
                }
            target = min(1.0, max(0.0, float(ratio))) * total
            traversed = 0.0
            for idx, length in enumerate(lengths):
                if length <= 1e-9:
                    continue
                if traversed + length >= target:
                    local = (target - traversed) / length
                    a_pt = poly[idx]
                    b_pt = poly[idx + 1]
                    lon = float(a_pt["lon"]) + (float(b_pt["lon"]) - float(a_pt["lon"])) * local
                    lat = float(a_pt["lat"]) + (float(b_pt["lat"]) - float(a_pt["lat"])) * local
                    alt = float(a_pt["alt"]) + (float(b_pt["alt"]) - float(a_pt["alt"])) * local
                    return {
                        "lon": round(lon, 8),
                        "lat": round(lat, 8),
                        "alt": round(alt, 3),
                        "time": round(float(when), 3) if numeric_time else when.isoformat(),
                    }
                traversed += length
            p = poly[-1]
            return {
                "lon": round(float(p["lon"]), 8),
                "lat": round(float(p["lat"]), 8),
                "alt": round(float(p["alt"]), 3),
                "time": round(float(when), 3) if numeric_time else when.isoformat(),
            }

        def add_seconds(when: Any, seconds: float) -> Any:
            return float(when) + seconds if numeric_time else when + timedelta(seconds=seconds)

        out: List[dict] = []
        if not node_path:
            return out
        first = point_for(node_path[0], current_time)
        if first:
            out.append(first)
        sample = self.trajectory_sample_sec
        for idx, node_id in enumerate(node_path):
            if wait_node_index is not None and idx == wait_node_index and wait_seconds > 0:
                elapsed = sample
                while elapsed < wait_seconds:
                    wait_pt = point_for(node_id, add_seconds(current_time, elapsed))
                    if wait_pt:
                        out.append(wait_pt)
                    elapsed += sample
                current_time = add_seconds(current_time, float(wait_seconds))
                wait_pt = point_for(node_id, current_time)
                if wait_pt:
                    out.append(wait_pt)
            if idx < len(edge_seconds):
                edge_time = max(0.0, float(edge_seconds[idx]))
                if idx + 1 >= len(node_path):
                    current_time = add_seconds(current_time, edge_time)
                    continue
                elapsed = sample
                while elapsed < edge_time:
                    pt = interp_point(node_id, node_path[idx + 1], elapsed / max(edge_time, 1e-6), add_seconds(current_time, elapsed))
                    if pt:
                        out.append(pt)
                    elapsed += sample
                current_time = add_seconds(current_time, edge_time)
                end_pt = point_for(node_path[idx + 1], current_time)
                if end_pt:
                    out.append(end_pt)
        return out

    @staticmethod
    def _trajectory_start_time(start_at: Optional[Any]) -> Tuple[Any, bool]:
        if start_at not in {None, ""}:
            try:
                return float(start_at), True
            except Exception:
                pass
            try:
                return parse_iso_time(str(start_at)), False
            except Exception:
                pass
        return 0.0, True

    @staticmethod
    def _time_delta_seconds(later: Optional[Any], earlier: Optional[Any]) -> Optional[float]:
        if later in {None, ""} or earlier in {None, ""}:
            return None
        try:
            return float(later) - float(earlier)
        except Exception:
            pass
        try:
            return (parse_iso_time(str(later)) - parse_iso_time(str(earlier))).total_seconds()
        except Exception:
            return None

    @staticmethod
    def _add_time_seconds(value: Optional[Any], seconds: float) -> Any:
        if value in {None, ""}:
            return utc_now_iso()
        try:
            return round(float(value) + float(seconds), 3)
        except Exception:
            pass
        try:
            return (parse_iso_time(str(value)) + timedelta(seconds=float(seconds))).isoformat()
        except Exception:
            return value

    @classmethod
    def _subtract_time_seconds(cls, value: Optional[Any], seconds: float) -> Optional[Any]:
        if value in {None, ""}:
            return None
        return cls._add_time_seconds(value, -float(seconds))

    def _standby_mode_for_path(
        self,
        node_path: List[str],
        wait_seconds: float,
        wait_node_index: Optional[int],
        launch_node: str,
    ) -> str:
        if wait_seconds <= 0 or wait_node_index is None:
            return "cold"
        try:
            idx = int(wait_node_index)
        except Exception:
            return "cold"
        if idx < 0 or idx >= len(node_path):
            return "cold"
        wait_node = node_path[idx]
        lane_ids, route = self.lane_graph.route_between_nodes(
            wait_node,
            launch_node,
            speed_cap_mps=self.speed_mps,
            ignore_speed_limits=True,
        )
        if lane_ids or wait_node == launch_node:
            distance_m = float(route.distance_m)
        else:
            a = self.graph.nodes.get(wait_node)
            b = self.graph.nodes.get(launch_node)
            if not a or not b:
                return "cold"
            distance_m = math.hypot(a.x - b.x, a.y - b.y)
        return "hot" if distance_m <= self.hot_distance_threshold_m else "cold"

    def _travel_time_seconds(
        self,
        path_cost: float,
        phase: str,
        speed_limit_mps: Optional[float] = None,
        curvature: float = 0.0,
        enter_speed_mps: Optional[float] = None,
    ) -> float:
        return self._travel_time_segment(path_cost, phase, speed_limit_mps, curvature, enter_speed_mps)[0]

    def _travel_time_segment(
        self,
        path_cost: float,
        phase: str,
        speed_limit_mps: Optional[float] = None,
        curvature: float = 0.0,
        enter_speed_mps: Optional[float] = None,
    ) -> Tuple[float, float]:
        # Mission vehicles use their configured maximum speed on every road.
        # Keep the predictor interface, but do not reintroduce OSM/default road
        # class limits as an artificial cap on segment travel time.
        # 对应任务书“神经网络预测路段耗时”的主流程调用点。
        # 每条候选路径在计算到达发射点/隐蔽点/贮备库的 ETA 时，都会把路段长度、
        # 曲率、速度、最大加速度、任务阶段等特征传给 MLPTimePredictor。
        # MLP 有模型时执行真实前向推理；缺模型时才降级为运动学耗时，保证流程不中断。
        speed_mps = min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps)
        enter_speed = max(0.1, float(enter_speed_mps if enter_speed_mps is not None else speed_mps))
        seconds, exit_speed = self.time_predictor.predict_segment(
            {
                "distance_m": float(path_cost or 0.0),
                "path_length_m": float(path_cost or 0.0),
                "length_m": float(path_cost or 0.0),
                "speed_mps": max(0.1, speed_mps),
                "enter_speed_mps": enter_speed,
                "speed_limit_mps": float(speed_mps),
                "allowed_speed_mps": float(speed_mps),
                "grade": 0.0,
                "max_accel": self.max_accel_mps2,
                "max_accel_mps2": self.max_accel_mps2,
                "phase": phase,
                "edge_count": 1.0,
                "curvature": float(curvature or 0.0),
            }
        )
        return seconds, exit_speed

    def _edge_seconds_from_path(self, node_path: List[str], phase: str) -> List[float]:
        edge_sec = []
        speed_in = min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps)
        for i in range(len(node_path) - 1):
            a, b = node_path[i], node_path[i + 1]
            meta = self.graph.get_edge_meta(a, b)
            dist = self.graph.get_edge_cost(a, b) or 50.0
            seconds, speed_in = self._travel_time_segment(
                dist,
                phase,
                speed_limit_mps=meta.speed_limit_mps if meta else None,
                curvature=meta.curvature if meta else 0.0,
                enter_speed_mps=speed_in,
            )
            edge_sec.append(seconds)
        return edge_sec

    def _ensure_lane_graph_current(self) -> None:
        road_version = int(getattr(self.graph, "_edge_index_version", 0))
        if road_version == self._lane_graph_road_version:
            return
        old_version = self._lane_graph_road_version
        self.lane_graph = LaneGraph(self.graph)
        self.lane_graph.set_cache_limit(self.lane_graph_cache_limit)
        self._lane_graph_road_version = road_version
        self.event_log.log(
            "lane_graph_rebuilt_after_projection",
            old_version=old_version,
            new_version=road_version,
            lane_count=len(self.lane_graph.lanes),
        )
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] lane_graph_rebuilt_after_projection "
            f"old_version={old_version} new_version={road_version} lanes={len(self.lane_graph.lanes)}"
        )
    # 单条路径规划核心：
    # 在“直达 / 去隐蔽点等待 / 起点等待”之间选择，并把时间约束一起折进去。
    def _propose_path(
        self,
        phase: str,
        start_node: str,
        target_node: str,
        desired_arrival: Optional[str],
        desired_fire_time: Optional[str] = None,
        earliest_start: Optional[str] = None,
        hide_points: Optional[List[str]] = None,
        launch_startup_sec: float = 0.0,
        launch_startup_mode: Optional[str] = None,
    ) -> Tuple[List[str], List[float], float, Optional[int], list, dict]:
        started_at = time.perf_counter()
        hide_points = hide_points or []
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] propose_path_input phase={phase} start={start_node} target={target_node} "
            f"desired_arrival={desired_arrival} desired_fire_time={desired_fire_time} "
            f"earliest_start={earliest_start} hide_points={len(hide_points or [])} "
            f"sample_hide={list(hide_points or [])[:8]}"
        )
        self.event_log.log(
            "propose_path_input",
            vehicle_id=self.vehicle_id,
            phase=phase,
            start_node=start_node,
            target_node=target_node,
            hide_points=len(hide_points),
        )
        # 先求一条起点到发射点的直达路，后面所有 hide 策略都拿它当基线比较。
        direct_path, direct_traj, direct_report = self._plan_route_bundle(start_node, target_node, phase)
        if not direct_path:
            report = direct_report.to_dict()
            report["launch_startup_sec"] = round(float(launch_startup_sec), 3)
            report["launch_startup_mode"] = launch_startup_mode or "hot"
            report["hide_selected"] = False
            report["hide_point"] = None
            return [start_node], [], 0.0, None, [], report
        direct_dist = direct_report.path_distance_m
        direct_edge_sec = self._edge_seconds_from_path(direct_path, phase)
        direct_travel = sum(direct_edge_sec)

        wait_sec = 0.0
        wait_node_index: Optional[int] = None
        best_report = direct_report
        best_traj = direct_traj
        hide_selected = False
        hide_point: Optional[str] = None
        selected_detour_ratio = 1.0
        request_now = earliest_start if earliest_start not in {None, ""} else utc_now_iso()
        direct_slack = 0.0
        if phase == "to_fire" and desired_arrival:
            delta_to_arrival = self._time_delta_seconds(desired_arrival, request_now)
            if delta_to_arrival is None:
                delta_to_arrival = 0.0
            delta_to_fire = self._time_delta_seconds(desired_fire_time, request_now)
            slack = delta_to_arrival - direct_travel
            direct_slack = slack
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] hide_wait_gate "
                f"start={start_node} target={target_node} "
                f"direct_ok={bool(direct_path)} "
                f"direct_dist={direct_report.path_distance_m if direct_report else None} "
                f"direct_travel={direct_travel if 'direct_travel' in locals() else None} "
                f"slack={slack if 'slack' in locals() else None} "
                f"hide_points={len(hide_points or [])} "
                f"trigger_slack={self.hide_trigger_slack_sec} "
                f"min_wait={self.hide_min_wait_sec}"
            )

            # 只有“明显提前到达”时，才有必要考虑插入隐蔽点等待。
            hide_wait_required = slack > self.candidate_fire_time_early_grace_sec
            hide_wait_eligible = (
                slack >= self.hide_trigger_slack_sec
                and slack >= self.hide_min_wait_sec
            )
            if hide_wait_required and hide_wait_eligible and hide_points:
                best_path = direct_path
                best_score = (float("inf"), float("inf"))
                best_report = direct_report
                best_traj = direct_traj
                best_wait_sec = 0.0
                best_wait_node_index = None
                best_hide_point = None
                best_launch_startup_sec = float(launch_startup_sec)
                best_launch_startup_mode = launch_startup_mode
                hide_eval_total = 0
                hide_eval_p1_unreachable = 0
                hide_eval_p2_unreachable = 0
                hide_eval_detour_ratio_rejected = 0
                hide_eval_detour_distance_rejected = 0
                hide_eval_time_infeasible = 0
                hide_eval_wait_too_short = 0
                hide_eval_valid = 0
                hide_detour_ratio_limit = float(self.hide_strategy_cfg.get("detour_ratio_limit", 999999.0))
                hide_detour_distance_limit_m = float(self.hide_strategy_cfg.get("detour_distance_limit_m", 999999999.0))
                detail_budget = 5
                selected_hide_for_log = None
                # 对候选隐蔽点逐个试：起点->隐蔽点->发射点，
                # 同时检查绕行程度、剩余时间和最终是否还能按时到点。
                for idx, h in enumerate(self._rank_hide_points(start_node, target_node, hide_points)):
                    hide_eval_total += 1
                    p1, t1, r1 = self._plan_route_bundle(start_node, h, phase)
                    if not p1:
                        hide_eval_p1_unreachable += 1
                        if detail_budget > 0:
                            debug_append_log(
                                f"[Vehicle {self.vehicle_id}] hide_candidate_eval_detail "
                                f"idx={idx} hide={h} p1_ok={bool(p1)} p2_ok=None "
                                f"p1_dist={r1.path_distance_m if r1 else None} p2_dist=None "
                                f"direct_dist={direct_report.path_distance_m if direct_report else None} "
                                f"detour_ratio=None candidate_travel=None remaining_slack=None "
                                f"reject_reason=p1_unreachable"
                            )
                            detail_budget -= 1
                        continue
                    p2, t2, r2 = self._plan_route_bundle(h, target_node, phase)
                    if not p2:
                        hide_eval_p2_unreachable += 1
                        if detail_budget > 0:
                            debug_append_log(
                                f"[Vehicle {self.vehicle_id}] hide_candidate_eval_detail "
                                f"idx={idx} hide={h} p1_ok={bool(p1)} p2_ok={bool(p2)} "
                                f"p1_dist={r1.path_distance_m if r1 else None} "
                                f"p2_dist={r2.path_distance_m if r2 else None} "
                                f"direct_dist={direct_report.path_distance_m if direct_report else None} "
                                f"detour_ratio=None candidate_travel=None remaining_slack=None "
                                f"reject_reason=p2_unreachable"
                            )
                            detail_budget -= 1
                        continue
                    candidate_path = p1 + p2[1:]
                    candidate_travel = sum(self._edge_seconds_from_path(candidate_path, phase))
                    candidate_launch_startup_sec, candidate_launch_startup_mode = self._launch_startup_profile(
                        r2.path_distance_m
                    )
                    if delta_to_fire is None:
                        available_travel_sec = delta_to_arrival
                    else:
                        available_travel_sec = delta_to_fire - candidate_launch_startup_sec
                    remaining_slack = available_travel_sec - candidate_travel
                    detour = max(0.0, (r1.path_distance_m + r2.path_distance_m) - direct_dist)
                    detour_ratio = candidate_travel / max(direct_travel, 1.0)
                    reject_reason = ""
                    if detour_ratio > hide_detour_ratio_limit:
                        hide_eval_detour_ratio_rejected += 1
                        reject_reason = "detour_ratio"
                    elif detour > hide_detour_distance_limit_m:
                        hide_eval_detour_distance_rejected += 1
                        reject_reason = "detour_distance"
                    elif remaining_slack < 0.0:
                        hide_eval_time_infeasible += 1
                        reject_reason = "time_infeasible"
                    elif remaining_slack < self.hide_min_wait_sec:
                        hide_eval_wait_too_short += 1
                        reject_reason = "hide_wait_too_short"
                    if reject_reason:
                        if detail_budget > 0:
                            debug_append_log(
                                f"[Vehicle {self.vehicle_id}] hide_candidate_eval_detail "
                                f"idx={idx} hide={h} p1_ok={bool(p1)} p2_ok={bool(p2)} "
                                f"p1_dist={r1.path_distance_m if r1 else None} "
                                f"p2_dist={r2.path_distance_m if r2 else None} "
                                f"direct_dist={direct_report.path_distance_m if direct_report else None} "
                                f"detour_ratio={detour_ratio} candidate_travel={candidate_travel} "
                                f"remaining_slack={remaining_slack} "
                                f"startup_mode={candidate_launch_startup_mode} "
                                f"startup_sec={candidate_launch_startup_sec} "
                                f"reject_reason={reject_reason}"
                            )
                            detail_budget -= 1
                        continue
                    score = (
                        detour_ratio,
                        r2.path_distance_m,
                        r1.path_distance_m + r2.path_distance_m,
                    )
                    hide_eval_valid += 1
                    if detail_budget > 0:
                        debug_append_log(
                            f"[Vehicle {self.vehicle_id}] hide_candidate_eval_detail "
                            f"idx={idx} hide={h} p1_ok={bool(p1)} p2_ok={bool(p2)} "
                            f"p1_dist={r1.path_distance_m if r1 else None} "
                            f"p2_dist={r2.path_distance_m if r2 else None} "
                            f"direct_dist={direct_report.path_distance_m if direct_report else None} "
                            f"detour_ratio={detour_ratio} candidate_travel={candidate_travel} "
                            f"remaining_slack={remaining_slack} "
                            f"startup_mode={candidate_launch_startup_mode} "
                            f"startup_sec={candidate_launch_startup_sec} reject_reason="
                        )
                        detail_budget -= 1
                    # 只有能在隐蔽点形成有效等待的路径才算 hide_wait。
                    # 不能为了“经过隐蔽点”把超时或几乎不停留的路线标成隐蔽等待。
                    if score < best_score:
                        best_score = score
                        best_path = candidate_path
                        best_traj = t1 + t2[1:] if t1 and t2 else []
                        best_report = self._combine_reports(r1, r2, best_traj)
                        best_report.estimated_travel_seconds = candidate_travel
                        best_wait_sec = max(0.0, remaining_slack)
                        best_wait_node_index = len(p1) - 1
                        best_hide_point = h
                        best_launch_startup_sec = candidate_launch_startup_sec
                        best_launch_startup_mode = candidate_launch_startup_mode
                        selected_hide_for_log = h
                if best_score[0] != float("inf"):
                    direct_path = best_path
                    wait_sec = best_wait_sec
                    wait_node_index = best_wait_node_index
                    hide_selected = True
                    hide_point = best_hide_point
                    launch_startup_sec = best_launch_startup_sec
                    launch_startup_mode = best_launch_startup_mode
                    selected_hide_for_log = hide_point
                    selected_detour_ratio = float(best_score[0])
                else:
                    # 隐蔽点全都不合适时，才退化成起点等待。
                    if slack > 0.0:
                        wait_sec = max(0.0, slack)
                        wait_node_index = 0
                        best_report = direct_report
                        best_traj = direct_traj
                        debug_append_log(
                            f"[Vehicle {self.vehicle_id}] start_wait_fallback "
                            f"start={start_node} target={target_node} wait_sec={round(wait_sec, 3)}"
                        )
                    else:
                        debug_append_log(
                            f"[Vehicle {self.vehicle_id}] hide_wait_required_but_unavailable "
                            f"start={start_node} target={target_node} slack={round(slack, 3)}"
                        )
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] hide_candidate_eval_summary "
                    f"start={start_node} target={target_node} "
                    f"total={hide_eval_total} "
                    f"p1_unreachable={hide_eval_p1_unreachable} "
                    f"p2_unreachable={hide_eval_p2_unreachable} "
                    f"detour_ratio_rejected={hide_eval_detour_ratio_rejected} "
                    f"detour_distance_rejected={hide_eval_detour_distance_rejected} "
                    f"time_infeasible={hide_eval_time_infeasible} "
                    f"wait_too_short={hide_eval_wait_too_short} "
                    f"valid={hide_eval_valid} "
                    f"selected={selected_hide_for_log}"
                )
            elif hide_wait_required:
                wait_sec = max(0.0, slack)
                wait_node_index = 0
                best_report = direct_report
                best_traj = direct_traj
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] start_wait_fallback "
                    f"start={start_node} target={target_node} wait_sec={round(wait_sec, 3)} "
                    f"reason={'no_hide_points' if not hide_points else 'hide_wait_below_trigger'}"
                )
        edge_sec = self._edge_seconds_from_path(direct_path, phase)
        report = best_report.to_dict()
        report["hide_selected"] = hide_selected
        report["hide_point"] = hide_point
        report["launch_startup_sec"] = round(float(launch_startup_sec), 3)
        report["launch_startup_mode"] = launch_startup_mode or self._launch_startup_profile(direct_dist)[1]
        travel_sec = sum(edge_sec)
        total_until_fire_sec = travel_sec + wait_sec + max(0.0, float(launch_startup_sec))
        report["travel_seconds"] = round(travel_sec, 3)
        report["route_distance_m"] = round(float(report.get("path_distance_m", 0.0) or 0.0), 3)
        report["detour_ratio"] = round(selected_detour_ratio, 6)
        report["hide_wait_seconds"] = round(wait_sec, 3)
        if hide_selected:
            report["timing_strategy"] = "hide_wait"
            self.event_log.log(
                "hide_candidate_selected",
                vehicle_id=self.vehicle_id,
                start_node=start_node,
                target_node=target_node,
                hide_point=hide_point,
                wait_sec=round(wait_sec, 3),
            )
        elif wait_sec > 0:
            report["timing_strategy"] = "start_wait"
            self.event_log.log(
                "start_wait_fallback_selected",
                vehicle_id=self.vehicle_id,
                start_node=start_node,
                target_node=target_node,
                wait_sec=round(wait_sec, 3),
            )
        else:
            report["timing_strategy"] = "direct"
        report["timing_total_until_fire_sec"] = round(total_until_fire_sec, 3)
        # 最后统一核算“真正到达发射点的时刻”和期望发射时间的偏差。
        fire_delta = self._time_delta_seconds(self._add_time_seconds(request_now, total_until_fire_sec), desired_fire_time)
        if fire_delta is not None:
            fire_time_error = fire_delta
            report["desired_fire_time"] = desired_fire_time
            report["fire_time_error_sec"] = round(fire_time_error, 3)
            if not self._fire_time_error_acceptable(fire_time_error):
                report["feasible"] = False
                violations = list(report.get("violations") or [])
                violations.append(f"fire_time_error:{round(fire_time_error, 3)}")
                if fire_time_error < 0 and direct_slack > self.hide_trigger_slack_sec and not hide_selected:
                    violations.append("early_without_hide_wait")
                report["violations"] = violations
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] propose_path_result "
            f"start={start_node} target={target_node} "
            f"timing_strategy={report.get('timing_strategy')} "
            f"hide_selected={report.get('hide_selected')} "
            f"hide_point={report.get('hide_point')} "
            f"wait_sec={wait_sec} wait_node_index={wait_node_index} "
            f"fire_time_error={report.get('fire_time_error_sec')} "
            f"feasible={report.get('feasible')}"
        )
        self._timing_log(
            "propose_path",
            started_at=started_at,
            phase=phase,
            start_node=start_node,
            target_node=target_node,
            hide_input_count=len(hide_points),
            timing_strategy=report.get("timing_strategy"),
            hide_selected=bool(report.get("hide_selected")),
            feasible=bool(report.get("feasible")),
            fire_time_error_sec=report.get("fire_time_error_sec"),
        )
        return direct_path, edge_sec, wait_sec, wait_node_index, best_traj, report

    # 隐蔽点预排序：
    # 先按“离直达主路径走廊是否近”排，再看它离目标和起点的几何距离，
    # 目的是优先尝试那些不太绕路的隐蔽点。
    def _rank_hide_points(
        self,
        start_node: str,
        target_node: str,
        hide_points: List[str],
        *,
        limit: bool = True,
    ) -> List[str]:
        if self.max_hide_candidates <= 0 or len(hide_points) <= self.max_hide_candidates:
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] hide_ranked_for_route "
                f"start={start_node} target={target_node} "
                f"input={len(hide_points or [])} ranked={len(hide_points or [])} "
                f"max_hide_candidates={self.max_hide_candidates} limit={limit} top={list(hide_points or [])[:8]}"
            )
            return hide_points
        start = self.graph.nodes.get(start_node)
        target = self.graph.nodes.get(target_node)
        if target is None:
            ranked = hide_points[: self.max_hide_candidates] if limit else list(hide_points)
            debug_append_log(
                f"[Vehicle {self.vehicle_id}] hide_ranked_for_route "
                f"start={start_node} target={target_node} "
                f"input={len(hide_points or [])} ranked={len(ranked)} "
                f"max_hide_candidates={self.max_hide_candidates} limit={limit} top={ranked[:8]}"
            )
            return ranked
        direct_polyline = self._route_node_polyline(start_node, target_node)
        ranked = []
        for h in hide_points:
            node = self.graph.nodes.get(h)
            if node is None:
                continue
            corridor_dist = self._point_to_polyline_distance(node.x, node.y, direct_polyline)
            target_dist = math.hypot(node.x - target.x, node.y - target.y)
            start_dist = math.hypot(node.x - start.x, node.y - start.y) if start else 0.0
            ranked.append((corridor_dist, target_dist, start_dist, h))
        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        ranked_points = [h for _, _, _, h in (ranked[: self.max_hide_candidates] if limit else ranked)]
        debug_append_log(
            f"[Vehicle {self.vehicle_id}] hide_ranked_for_route "
            f"start={start_node} target={target_node} "
            f"input={len(hide_points or [])} ranked={len(ranked_points)} "
            f"max_hide_candidates={self.max_hide_candidates} limit={limit} "
            f"top={[(item[3], round(item[0], 1), round(item[1], 1)) for item in ranked[:5]]}"
        )
        return ranked_points

    # 生成 start->target 的路网折线，供隐蔽点走廊筛选时计算“点到主路径”的距离。
    def _route_node_polyline(self, start_node: str, target_node: str) -> List[Tuple[float, float]]:
        self._ensure_lane_graph_current()
        try:
            _, route = self.lane_graph.route_between_nodes(
                start_node=start_node,
                goal_node=target_node,
                speed_cap_mps=min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps),
                ignore_speed_limits=True,
            )
        except Exception:
            route = None
        node_path = list(route.road_node_path) if route and route.road_node_path else []
        pts = [
            (float(self.graph.nodes[nid].x), float(self.graph.nodes[nid].y))
            for nid in node_path
            if nid in self.graph.nodes
        ]
        if len(pts) >= 2:
            return pts
        start = self.graph.nodes.get(start_node)
        target = self.graph.nodes.get(target_node)
        if start is None or target is None:
            return []
        return [(float(start.x), float(start.y)), (float(target.x), float(target.y))]

    @staticmethod
    def _point_to_polyline_distance(x: float, y: float, polyline: List[Tuple[float, float]]) -> float:
        if len(polyline) < 2:
            return float("inf")
        best = float("inf")
        px = float(x)
        py = float(y)
        for (ax, ay), (bx, by) in zip(polyline[:-1], polyline[1:]):
            dx = bx - ax
            dy = by - ay
            denom = dx * dx + dy * dy
            if denom <= 1e-9:
                dist = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
                qx = ax + t * dx
                qy = ay + t * dy
                dist = math.hypot(px - qx, py - qy)
            if dist < best:
                best = dist
        return best
#冷热待机
    def _launch_startup_profile(self, path_distance_m: float) -> Tuple[float, str]:
        if path_distance_m <= max(0.0, self.hot_distance_threshold_m):
            return self.launch_prepare_sec + self.hot_standby_sec, "hot"
        return self.launch_prepare_sec + self.cold_standby_sec, "cold"

    def _plan_route_bundle(self, start_node: str, target_node: str, phase: str):
        self._ensure_lane_graph_current()
        lane_ids, route = self.lane_graph.route_between_nodes(
            start_node=start_node,
            goal_node=target_node,
            speed_cap_mps=min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps),
            ignore_speed_limits=True,
        )
        if not route.road_node_path:
            node_path, report = self.coarse_planner.plan(start_node, target_node)
            if not node_path:
                return [], [], report
            trajectory = self._polyline_to_trajectory(
                [(self.graph.nodes[nid].x, self.graph.nodes[nid].y) for nid in node_path if nid in self.graph.nodes]
            )
            report.algorithm = "constrained_a_star_centerline"
            report.estimated_travel_seconds = max(report.estimated_travel_seconds, self._trajectory_seconds(trajectory))
            return node_path, trajectory, report

        road_node_path = route.road_node_path
        center_traj = self._centerline_to_trajectory(route.centerline)
        entry_sec = 0.0
        exit_sec = 0.0
        trajectory = self._postprocess_trajectory(center_traj)
        report = PathConstraintReport(
            feasible=True,
            algorithm="lane_dijkstra_centerline",
            path_distance_m=route.distance_m,
            estimated_travel_seconds=0.0,
            narrowest_width_m=None,
            max_turn_angle_deg=0.0,
            min_turn_radius_m=self.kinematics.min_turn_radius_m,
            total_turn_penalty=0.0,
        )
        narrowest_width = float("inf")
        for lane_id in lane_ids:
            lane = self.lane_graph.lanes[lane_id]
            narrowest_width = min(narrowest_width, lane.lane_width_m)
            if lane.lane_width_m < self.kinematics.min_lane_width_m:
                debug_append_log(
                    f"[Vehicle {self.vehicle_id}] lane_width_below_threshold "
                    f"lane={lane_id} width={round(float(lane.lane_width_m), 3)} "
                    f"threshold={round(float(self.kinematics.min_lane_width_m), 3)}"
                )
        if narrowest_width != float("inf"):
            report.narrowest_width_m = narrowest_width
        report.max_turn_angle_deg = self._trajectory_max_turn_angle(trajectory)
        report.algorithm = "lane_dijkstra_centerline"
        max_curvature = max((self.lane_graph.lanes[lane_id].curvature for lane_id in lane_ids), default=0.0)
        route_predict_sec = self.time_predictor.predict_with_context(
            {
                "distance_m": route.distance_m,
                "path_length_m": route.distance_m,
                "speed_mps": min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps),
                "phase": phase,
                "edge_count": float(max(1, len(road_node_path) - 1)),
                "curvature": max_curvature,
            }
        )
        report.estimated_travel_seconds = max(route_predict_sec + entry_sec + exit_sec, self._trajectory_seconds(trajectory))
        return road_node_path, trajectory, report

    def _centerline_to_trajectory(self, centerline: List[Tuple[float, float]]) -> List[TrajectoryPoint]:
        poly = self._clean_polyline(centerline)
        poly = self._chaikin_smooth(poly, iterations=1)
        poly = self._resample_polyline(poly, step_m=max(15.0, self.hybrid_astar_cfg.position_resolution_m * 4.0))
        return self._polyline_to_trajectory(poly)

    @staticmethod
    def _merge_trajectories(*parts: List[TrajectoryPoint]) -> List[TrajectoryPoint]:
        out: List[TrajectoryPoint] = []
        for part in parts:
            for pt in part:
                if not out:
                    out.append(pt)
                    continue
                if abs(pt.x - out[-1].x) > 1e-3 or abs(pt.y - out[-1].y) > 1e-3:
                    out.append(pt)
        return out

    def _postprocess_trajectory(self, points: List[TrajectoryPoint]) -> List[TrajectoryPoint]:
        if len(points) < 3:
            return points
        poly = [(pt.x, pt.y) for pt in points]
        poly = self._clean_polyline(poly)
        poly = self._chaikin_smooth(poly, iterations=1)
        poly = self._resample_polyline(poly, step_m=max(15.0, self.hybrid_astar_cfg.position_resolution_m * 4.0))
        return self._polyline_to_trajectory(poly)

    @staticmethod
    def _polyline_to_trajectory(polyline: List[Tuple[float, float]]) -> List[TrajectoryPoint]:
        if not polyline:
            return []
        out: List[TrajectoryPoint] = []
        for idx, (x, y) in enumerate(polyline):
            if idx + 1 < len(polyline):
                nx, ny = polyline[idx + 1]
                yaw_deg = math.degrees(math.atan2(ny - y, nx - x))
            elif out:
                yaw_deg = out[-1].yaw_deg
            else:
                yaw_deg = 0.0
            out.append(TrajectoryPoint(x=x, y=y, yaw_deg=yaw_deg, steer_deg=0.0, direction=1))
        return out

    @staticmethod
    def _clean_polyline(polyline: List[Tuple[float, float]], eps: float = 1e-3) -> List[Tuple[float, float]]:
        if not polyline:
            return []
        out: List[Tuple[float, float]] = []
        for x, y in polyline:
            if out and math.hypot(x - out[-1][0], y - out[-1][1]) <= eps:
                continue
            if len(out) >= 2 and math.hypot(x - out[-2][0], y - out[-2][1]) <= eps:
                out.pop()
                continue
            out.append((x, y))
        return out

    @staticmethod
    def _resample_polyline(polyline: List[Tuple[float, float]], step_m: float) -> List[Tuple[float, float]]:
        if len(polyline) < 2 or step_m <= 0.5:
            return polyline
        out: List[Tuple[float, float]] = [polyline[0]]
        carry = 0.0
        for idx in range(len(polyline) - 1):
            x1, y1 = polyline[idx]
            x2, y2 = polyline[idx + 1]
            seg_len = math.hypot(x2 - x1, y2 - y1)
            if seg_len <= 1e-6:
                continue
            ux = (x2 - x1) / seg_len
            uy = (y2 - y1) / seg_len
            dist = step_m - carry
            while dist < seg_len:
                out.append((x1 + ux * dist, y1 + uy * dist))
                dist += step_m
            carry = max(0.0, seg_len - (dist - step_m))
        if math.hypot(polyline[-1][0] - out[-1][0], polyline[-1][1] - out[-1][1]) > 1e-3:
            out.append(polyline[-1])
        return out

    @staticmethod
    def _chaikin_smooth(polyline: List[Tuple[float, float]], iterations: int = 1) -> List[Tuple[float, float]]:
        out = list(polyline)
        for _ in range(max(0, iterations)):
            if len(out) < 3:
                return out
            nxt: List[Tuple[float, float]] = [out[0]]
            for i in range(len(out) - 1):
                x1, y1 = out[i]
                x2, y2 = out[i + 1]
                q = (0.75 * x1 + 0.25 * x2, 0.75 * y1 + 0.25 * y2)
                r = (0.25 * x1 + 0.75 * x2, 0.25 * y1 + 0.75 * y2)
                nxt.extend([q, r])
            nxt.append(out[-1])
            out = nxt
        return out

    def _trajectory_seconds(self, points: List[TrajectoryPoint]) -> float:
        if len(points) < 2:
            return 0.0
        total = 0.0
        for i in range(len(points) - 1):
            total += math.hypot(points[i + 1].x - points[i].x, points[i + 1].y - points[i].y)
        return total / max(0.5, min(self.speed_mps, self.kinematics.max_speed_mps or self.speed_mps))

    @staticmethod
    def _trajectory_max_turn_angle(points: List[TrajectoryPoint]) -> float:
        if len(points) < 3:
            return 0.0
        best = 0.0
        for i in range(1, len(points) - 1):
            a = points[i - 1]
            b = points[i]
            c = points[i + 1]
            h1 = math.atan2(b.y - a.y, b.x - a.x)
            h2 = math.atan2(c.y - b.y, c.x - b.x)
            diff = (h2 - h1 + math.pi) % (2.0 * math.pi) - math.pi
            best = max(best, abs(math.degrees(diff)))
        return best

    def _combine_reports(
        self,
        r1: PathConstraintReport,
        r2: PathConstraintReport,
        trajectory: List[TrajectoryPoint],
    ) -> PathConstraintReport:
        radii = [x for x in [r1.min_turn_radius_m, r2.min_turn_radius_m] if x is not None]
        widths = [x for x in [r1.narrowest_width_m, r2.narrowest_width_m] if x is not None]
        return PathConstraintReport(
            feasible=r1.feasible and r2.feasible,
            algorithm="lane_dijkstra+hybrid_a_star",
            violations=list(r1.violations) + list(r2.violations),
            path_distance_m=r1.path_distance_m + r2.path_distance_m,
            estimated_travel_seconds=r1.estimated_travel_seconds + r2.estimated_travel_seconds,
            narrowest_width_m=min(widths) if widths else None,
            max_turn_angle_deg=max(r1.max_turn_angle_deg, r2.max_turn_angle_deg, self._trajectory_max_turn_angle(trajectory)),
            min_turn_radius_m=min(radii) if radii else None,
            total_turn_penalty=r1.total_turn_penalty + r2.total_turn_penalty,
        )





def main() -> None:
    parser = argparse.ArgumentParser(description="MVS Vehicle")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    app = VehicleApp(args.config)
    app.start()
    print(f"[Vehicle {app.vehicle_id}] started at {app.listen_host}:{app.listen_port}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == "__main__":
    main()

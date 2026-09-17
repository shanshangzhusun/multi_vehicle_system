from __future__ import annotations

import argparse
import heapq
import json
import math
import socket
import threading
import time
import traceback
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mvs.common.formula_utils import (
    depot_slot_delta,
    hide_point_capacity_delta,
    time_window_overlap,
)
from mvs.common.lane_graph import LaneGraph
from mvs.common.models import Envelope, TaskPackage, parse_iso_time, utc_now_iso
from mvs.common.event_log import EventLogger
from mvs.common.depot_assignment import greedy_capacity_assignment
from mvs.common.debug_append_log import configure_manual_debug, debug_append_log, manual_debug_enabled
from mvs.common.scoring import compute_dispatch_priority, score_launch_point_for_vehicle
from mvs.common.platform_interfaces import (
    MSG_DISPATCH_TRAJECTORY_BUNDLE,
    MSG_REQUEST_DIAN,
    MSG_FA_SHE_DIAN,
    MSG_YIN_BI_DIAN,
    MSG_DEPOT_DIAN,
    MSG_ZHU_BEI_DIAN,
    MSG_ZHU_BEI_KU_DIAN,
    MSG_SELECTED_VEHICLE_RESULT,
    MSG_SELECTED_DEPOT_RESULT,
    MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
    MSG_DEPOT_VEHICLE_SCORE_RESULT,
    MSG_VEHICLE_DEPOT_ASSIGNMENT,
    MSG_VEHICLE_POST_FIRE_PATH_RESULT,
    MSG_TIME_BACKPLAN_CONTEXT,
    MSG_VEHICLE_DIAN,
    MSG_VEHICLE_CANDIDATE_CONTEXT,
    MSG_VEHICLE_CANDIDATE_PATH_RESULT,
    MSG_VEHICLE_SCORE_RESULT,
    correlation_fields,
    reply_address,
)
from mvs.common.task_validation import TaskPackageValidator
from mvs.common.transport import MessageAddress, create_transport_node, normalize_transport_config
from mvs.scheduler.dashboard_server import DashboardServer
from mvs.scheduler.map_model import MapLoader, RoadGraph, SpecialPoints


SIM_TIME_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class VehicleRuntime:
    vehicle_id: str
    endpoint: Optional[Tuple[str, int]]
    home_node: str
    ammo_types: Set[str]
    speed_mps: float = 8.0
    realtime_scale: float = 0.05
    kinematics: dict = None
    status: str = "UNKNOWN"
    current_node: str = ""
    last_seen: str = ""
    busy: bool = False
    active_subtask_id: Optional[str] = None
    theater_id: str = ""



@dataclass
class SubTask:
    subtask_id: str
    task_id: str
    ammo_type: str
    fire_time: str
    sim_fire_time: Optional[float] = None
    status: str = "PENDING"
    phase: str = "to_fire"
    assigned_vehicle: Optional[str] = None
    assigned_launch_point: Optional[str] = None
    last_request: Optional[dict] = None
    proposal_sent_at: float = 0.0
    proposal_retries: int = 0
    reset_count: int = 0
    no_candidate_count: int = 0
    rejected_launch_points: Set[str] = field(default_factory=set)
    rejected_vehicle_ids: Set[str] = field(default_factory=set)
    score_breakdown: dict = field(default_factory=dict)
    depot_node: Optional[str] = None
    assignment_mode: str = "idle"
    is_redundant: bool = False
    redundant_for_subtask_id: Optional[str] = None
    route_candidate_limit: int = 0
    last_lateness_sec: float = 0.0
    preferred_launch_point: Optional[str] = None

class SchedulerApp:
    def __init__(self, config_path: str) -> None:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        manual_debug_cfg = cfg.get("manual_debug") if isinstance(cfg.get("manual_debug"), dict) else {}
        configure_manual_debug(bool(manual_debug_cfg.get("enabled", cfg.get("manual_debug_enabled", manual_debug_enabled()))))
        self.node_id = cfg.get("node_id", "scheduler")
        self.listen_host = cfg["listen_host"]
        self.listen_port = int(cfg["listen_port"])
        self.advertise_host = str(cfg.get("advertise_host") or (self.listen_host if self.listen_host != "0.0.0.0" else "127.0.0.1"))
        self.transport_cfg = normalize_transport_config(cfg.get("transport"))
        self.transport_type = str(self.transport_cfg.get("type", "tcp")).lower()
        self.tick_sec = float(cfg.get("tick_sec", 0.5))
        self.dashboard_host = cfg.get("dashboard_host", "0.0.0.0")
        self.dashboard_port = int(cfg.get("dashboard_port", 18080))
        self.heartbeat_timeout_sec = float(cfg.get("heartbeat_timeout_sec", 5.0))
        self.reservation_retention_sec = float(cfg.get("reservation_retention_sec", 30.0))
        self.reservation_deadlock_backoff_sec = float(cfg.get("reservation_deadlock_backoff_sec", 5.0))
        self.reload_duration_sec = float(cfg.get("reload_duration_sec", 60.0))
        self.depot_capacity = int(cfg.get("depot_capacity", 16) or 16)
        self.model_callback_cfg = dict(cfg.get("model_callback", {}))
        self.model_callback_enabled = bool(self.model_callback_cfg.get("enabled", False))
        self.model_callback_host = str(self.model_callback_cfg.get("host", "127.0.0.1"))
        self.model_callback_port = int(self.model_callback_cfg.get("port", 9160))
        self.scheduler_theater_id = str(cfg.get("scheduler_theater_id") or "")
        self.model_message_type_tag = str(cfg.get("model_message_type_tag") or "")
        self.vehicle_scoring_context_cfg = dict(cfg.get("vehicle_scoring_context", {}))
        self.vehicle_scoring_context_enabled = bool(self.vehicle_scoring_context_cfg.get("enabled", True))
        self.score_submit_host = str(self.vehicle_scoring_context_cfg.get("score_submit_host") or self.advertise_host)
        self.score_port_base = int(self.vehicle_scoring_context_cfg.get("score_port_base", 8000) or 8000)
        self.score_port_count = int(self.vehicle_scoring_context_cfg.get("score_port_count", 128) or 128)
        self.launch_timing_cfg = dict(cfg.get("launch_timing", {}))
        self.hot_distance_threshold_m = float(self.launch_timing_cfg.get("hot_distance_threshold_m", 5000.0))
        self.launch_prepare_sec = float(self.launch_timing_cfg.get("launch_prepare_sec", 300.0))
        self.hot_standby_sec = float(
            self.launch_timing_cfg.get("hot_standby_sec", self.launch_timing_cfg.get("hot_startup_sec", 180.0))
        )
        self.cold_standby_sec = float(
            self.launch_timing_cfg.get("cold_standby_sec", self.launch_timing_cfg.get("cold_startup_sec", 420.0))
        )
        self.event_log = EventLogger(
            path=cfg.get("event_log_path", "logs/scheduler_events.jsonl"),
            node_id=self.node_id,
        )
        self.timing_log_path = Path(cfg.get("timing_log_path", f"logs/{self.node_id}_timing.jsonl"))

        self.allowed_ammo_types = set(cfg.get("allowed_ammo_types", ["HE", "AP", "SMOKE"]))
        self.task_validator = TaskPackageValidator(
            allowed_ammo_types=sorted(self.allowed_ammo_types),
            max_launches=int(cfg.get("max_task_launches", 256)),
            reject_past_fire_time=bool(cfg.get("reject_past_fire_time", False)),
            past_time_grace_sec=float(cfg.get("past_time_grace_sec", 30.0)),
        )

        self.graph: RoadGraph = MapLoader.load_graph_from_config(cfg["map"])
        self.points: SpecialPoints = MapLoader.load_points_from_config(cfg["map"], self.graph)
        self.lane_graph = LaneGraph(self.graph)
        self.lane_graph.set_cache_limit(int(cfg.get("lane_graph_cache_limit", 65536)))
        self.fire_zone_cfg = dict(cfg.get("fire_zone", {}))
        self.assignment_scoring_cfg = dict(cfg.get("assignment_scoring", {}))
        self.max_route_candidates = max(1, int(self.assignment_scoring_cfg.get("max_route_candidates", 40)))
        self.max_route_candidates_real = max(
            1,
            int(self.assignment_scoring_cfg.get("max_route_candidates_real", min(self.max_route_candidates, 20))),
        )
        self.max_route_candidates_reserved = max(
            1,
            int(self.assignment_scoring_cfg.get("max_route_candidates_reserved", min(self.max_route_candidates_real, 12))),
        )
        self.max_route_candidates_redundant = max(
            1,
            int(self.assignment_scoring_cfg.get("max_route_candidates_redundant", min(self.max_route_candidates_reserved, 6))),
        )
        self.max_lateness_sec = max(0.0, float(self.assignment_scoring_cfg.get("max_lateness_sec", 5.0)))
        self.fire_window_grace_sec = max(
            self.max_lateness_sec,
            float(self.assignment_scoring_cfg.get("fire_window_grace_sec", max(15.0, self.max_lateness_sec))),
        )
        self.fire_window_early_grace_sec = max(
            0.0,
            float(self.assignment_scoring_cfg.get("fire_window_early_grace_sec", 60.0)),
        )
        self.fire_window_grace_after_resets = max(
            1,
            int(self.assignment_scoring_cfg.get("fire_window_grace_after_resets", 2)),
        )
        self.to_fire_local_deadlock_retries = max(
            0,
            int(self.assignment_scoring_cfg.get("to_fire_local_deadlock_retries", 0)),
        )
        self.to_fire_local_deadlock_backoff_sec = max(
            0.0,
            float(self.assignment_scoring_cfg.get("to_fire_local_deadlock_backoff_sec", 0.0)),
        )
        self.max_assignment_resets = max(1, int(self.assignment_scoring_cfg.get("max_assignment_resets", 8)))
        self.to_fire_deadlock_bypass_after_resets = max(
            0,
            int(self.assignment_scoring_cfg.get("to_fire_deadlock_bypass_after_resets", 0)),
        )
        self.max_no_candidate_ticks = max(
            1,
            int(self.assignment_scoring_cfg.get("max_no_candidate_ticks", self.max_assignment_resets * 4)),
        )
        self.max_active_real_planning = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_real_planning_subtasks", 0)),
        )
        self.max_active_redundant_planning = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_redundant_planning_subtasks", 0)),
        )
        self.max_active_real_planning_per_task = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_real_planning_per_task", 0)),
        )
        self.max_active_redundant_planning_per_task = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_redundant_planning_per_task", 0)),
        )
        self.max_active_real_task_groups = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_real_task_groups", 0)),
        )
        self.max_active_redundant_task_groups = max(
            0,
            int(self.assignment_scoring_cfg.get("max_active_redundant_task_groups", 0)),
        )
        self.redundancy_cfg = dict(cfg.get("redundancy", {}))
        self.redundancy_disabled = bool(cfg.get("disable_redundancy", self.redundancy_cfg.get("disabled", True)))
        self.redundancy_enabled = bool(self.redundancy_cfg.get("enabled", False)) and not self.redundancy_disabled
        self.redundancy_ratio = 0.0 if not self.redundancy_enabled else max(0.0, float(self.redundancy_cfg.get("ratio", 0.0)))
        self.allow_dynamic_vehicle_registration = bool(cfg.get("allow_dynamic_vehicle_registration", False))
        self._graph_bounds = self._compute_graph_bounds()
        self.theaters = [dict(row) for row in cfg.get("theaters", []) if isinstance(row, dict)]

        self.launch_caps: Dict[str, Set[str]] = {}
        for lp, types in cfg.get("launch_capability", {}).items():
            self.launch_caps[lp] = set(types)

        self.vehicles: Dict[str, VehicleRuntime] = {}
        for v in cfg.get("vehicles", []):
            self.vehicles[v["vehicle_id"]] = VehicleRuntime(
                vehicle_id=v["vehicle_id"],
                endpoint=(v["host"], int(v["port"])),
                home_node=v["home_node"],
                ammo_types=set(v.get("ammo_types", [])),
                speed_mps=float(v.get("speed_mps", 8.0)),
                realtime_scale=float(v.get("realtime_scale", 0.05)),
                kinematics=dict(v.get("kinematics", {})),
                status="OFFLINE",
                current_node=v["home_node"],
                last_seen="",
                theater_id=str(v.get("theater_id") or ""),
            )

        self.transport = create_transport_node(
            node_id=self.node_id,
            host=self.listen_host,
            port=self.listen_port,
            on_message=self.on_message,
            cfg=self.transport_cfg,
        )
        self.message_capture_cfg = dict(cfg.get("message_capture", {}))
        self.message_capture_enabled = bool(self.message_capture_cfg.get("enabled", False))
        self.message_capture_dir = Path(
            self.message_capture_cfg.get("dir", f"result/message_capture/scheduler/{self.node_id}")
        )
        # 兼容旧版单调度目录名，避免 scheduler_001 / scheduler_002 混写到 scheduler_shp_demo。
        if self.message_capture_dir.name == "scheduler_shp_demo":
            self.message_capture_dir = self.message_capture_dir.parent / self.node_id
        self._message_capture_seq = 0

        self._lock = threading.Lock()
        self._running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_queue_lock = threading.Lock()
        self._pending_heartbeats: Dict[str, Tuple[dict, MessageAddress]] = {}
        self.dashboard = DashboardServer(
            host=self.dashboard_host,
            port=self.dashboard_port,
            state_provider=self.get_dashboard_state,
        )

        self.subtasks: Dict[str, SubTask] = {}
        self.pending_subtasks: List[str] = []
        self.queued_subtasks: List[str] = []
        self.external_vehicle_selection: Dict[str, List[str]] = {}
        self.external_depot_selection: Dict[str, str] = {}
        self.external_depot_selection_by_task: Dict[str, Set[str]] = defaultdict(set)
        self.external_fa_she_dian: List[dict] = []
        self.external_yin_bi_dian: List[dict] = []
        self.external_vehicle_dian: List[dict] = []
        self.external_depot_dian: List[dict] = []
        self.external_launch_point_rows_by_node: Dict[str, dict] = {}
        self.external_hide_point_rows_by_node: Dict[str, dict] = {}
        self.external_depot_point_rows_by_node: Dict[str, dict] = {}
        self.external_vehicle_candidate_paths: Dict[str, dict] = {}
        self.resolved_dispatch_trajectory_bundles: Dict[str, dict] = {}
        self.vehicle_scoring_context_sent_tasks: Set[str] = set()
        self.selected_depot_wait_deadlines: Dict[str, float] = {}
        self.selected_depot_wait_timers: Dict[str, threading.Timer] = {}
        self.active_plans: Dict[str, dict] = {}
        self.dispatched_to_fire_subtasks_by_task: Dict[str, Set[str]] = defaultdict(set)
        self.dispatched_trajectory_bundle_tasks: Set[str] = set()
        self.prelaunch_dispatch_states: Dict[str, dict] = {}
        self.depot_vehicle_score_results: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.depot_vehicle_assignments: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.post_fire_path_results: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.recent_events: List[dict] = []
        self.junction_nodes = {
            nid
            for nid, edges in self.graph.edges.items()
            if self.graph.nodes[nid].kind == "road" and len(edges) > 2
        }
        external_conflict_cfg = dict(cfg.get("external_conflict_resolution", {}))
        self.external_conflict_slot_sec = max(1.0, float(external_conflict_cfg.get("slot_sec", 5.0)))
        self.external_conflict_distance_m = max(0.0, float(external_conflict_cfg.get("distance_m", 5.0)))
        self.external_hide_capacity = max(1, int(external_conflict_cfg.get("hide_capacity", 1)))
        self.external_conflict_delay_step_sec = max(1.0, float(external_conflict_cfg.get("delay_step_sec", 5.0)))
        self.external_conflict_max_delay_sec = max(0.0, float(external_conflict_cfg.get("max_delay_sec", 900.0)))
        self.external_post_fire_max_delay_sec = max(
            self.external_conflict_max_delay_sec,
            float(external_conflict_cfg.get("post_fire_max_delay_sec", 7200.0)),
        )
        self.external_conflict_fallback_to_rank1 = bool(external_conflict_cfg.get("fallback_to_rank1", True))
        self.external_force_emit_bundle_when_unresolved = bool(
            external_conflict_cfg.get("force_emit_bundle_when_unresolved", False)
        )
        self.selected_depot_wait_timeout_sec = max(
            0.0,
            float(external_conflict_cfg.get("selected_depot_wait_timeout_sec", 10.0)),
        )
        self.selected_depot_expected_count = max(
            1,
            int(external_conflict_cfg.get("selected_depot_expected_count", 10)),
        )
        two_stage_cfg = dict(cfg.get("two_stage_depot_assignment", {}))
        depot_platform_cfg = dict(cfg.get("depot_platform", {}))
        self.two_stage_depot_enabled = bool(two_stage_cfg.get("enabled", True))
        self.depot_direct_host = str(
            two_stage_cfg.get("depot_host")
            or depot_platform_cfg.get("host")
            or self.advertise_host
        )
        self.depot_distance_weight_a = float(two_stage_cfg.get("distance_weight_a", 1.0))
        self.depot_distance_rank_weight_b = float(two_stage_cfg.get("distance_rank_weight_b", 100.0))
        self.depot_load_ratio_weight_c = float(two_stage_cfg.get("load_ratio_weight_c", 1000.0))
        self.depot_distance_metric = str(two_stage_cfg.get("distance_metric", "euclidean")).strip().lower()
        self.external_component_repair_max_options_per_vehicle = max(
            1,
            int(external_conflict_cfg.get("component_repair_max_options_per_vehicle", 6)),
        )
        self.external_component_repair_second_tier_multiplier = max(
            1,
            int(external_conflict_cfg.get("component_repair_second_tier_multiplier", 2)),
        )
        self.metrics: Dict[str, int] = {
            "tasks_received": 0,
            "subtasks_created": 0,
            "subtasks_assigned": 0,
            "path_proposals_received": 0,
            "path_rejected": 0,
            "plan_execute": 0,
            "subtask_done": 0,
            "subtask_reset": 0,
            "proposal_retries": 0,
            "deadlock_risk": 0,
            "deferred_assignments": 0,
            "direct_reassign_after_reload": 0,
            "subtask_failed_window": 0,
            "heartbeats_queued": 0,
            "heartbeats_applied": 0,
        }
        self._last_metrics_log_ts = 0.0
        self._lane_graph_prewarm_cfg = dict(cfg.get("lane_graph_prewarm", {}))
        self._lane_graph_prewarm_thread: Optional[threading.Thread] = None

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
                "node_id": self.node_id,
                "stage": stage,
                "elapsed_sec": round(float(elapsed_sec), 6),
            }
            record.update(fields)
            self.timing_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.timing_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _push_recent_event(self, kind: str, **fields: object) -> None:
        row = {"ts": utc_now_iso(), "kind": kind}
        row.update(fields)
        self.recent_events.append(row)
        if len(self.recent_events) > 80:
            self.recent_events = self.recent_events[-80:]

    def _compute_graph_bounds(self) -> Tuple[float, float, float, float]:
        xs = [n.x for n in self.graph.nodes.values()]
        ys = [n.y for n in self.graph.nodes.values()]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _parse_bounds(value: Any) -> Optional[Tuple[float, float, float, float]]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if len(parts) == 4:
                try:
                    lon_min, lat_min, lon_max, lat_max = [float(part) for part in parts]
                    return lon_min, lat_min, lon_max, lat_max
                except Exception:
                    return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                lon_min, lat_min, lon_max, lat_max = [float(part) for part in value]
                return lon_min, lat_min, lon_max, lat_max
            except Exception:
                return None
        return None

    def _theater_for_vehicle(self, vehicle: VehicleRuntime) -> Optional[Dict[str, Any]]:
        if vehicle.theater_id:
            for row in self.theaters:
                if str(row.get("id") or "") == vehicle.theater_id:
                    return row
        port = vehicle.endpoint[1] if vehicle.endpoint else None
        if port is None:
            try:
                port = int(vehicle.vehicle_id)
            except Exception:
                return None
        for row in self.theaters:
            start = int(row.get("vehicle_port_start", 0) or 0)
            count = int(row.get("vehicle_port_count", 0) or 0)
            if start > 0 and count > 0 and start <= int(port) < start + count:
                return row
        return None

    def _row_in_theater(self, row: Dict[str, Any], theater: Optional[Dict[str, Any]]) -> bool:
        if not theater:
            return True
        bounds = self._parse_bounds(theater.get("bounds"))
        if not bounds:
            return True
        try:
            lon = float(row.get("lon"))
            lat = float(row.get("lat"))
        except Exception:
            return False
        lon_min, lat_min, lon_max, lat_max = bounds
        return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max

    def _scheduler_theater(self) -> Optional[Dict[str, Any]]:
        if not self.scheduler_theater_id:
            return None
        for row in self.theaters:
            if str(row.get("id") or "") == self.scheduler_theater_id:
                return row
        return None

    def _launch_nodes_for_vehicle(self, vehicle: VehicleRuntime) -> List[str]:
        theater = self._theater_for_vehicle(vehicle)
        if not theater:
            return list(self.points.launch_points)
        return [
            node_id
            for node_id in self.points.launch_points
            if self._row_in_theater(self.external_launch_point_rows_by_node.get(str(node_id), {}), theater)
        ]

    def _hide_nodes_for_vehicle(self, vehicle: VehicleRuntime) -> List[str]:
        theater = self._theater_for_vehicle(vehicle)
        if not theater:
            return list(self.points.hide_points)
        return [
            node_id
            for node_id in self.points.hide_points
            if self._row_in_theater(self.external_hide_point_rows_by_node.get(str(node_id), {}), theater)
        ]

    def _task_subtasks_total(self, task_id: str) -> int:
        return sum(1 for st in self.subtasks.values() if st.task_id == task_id)

    def _build_dispatch_trajectory_bundle(self, task_id: str) -> dict:
        resolved = self.resolved_dispatch_trajectory_bundles.get(str(task_id))
        if resolved:
            return dict(resolved)
        return {
            "task_id": task_id,
            "trajectories": [],
        }

    @staticmethod
    def _canonical_launch_id(value: Any) -> str:
        raw = str(value or "").strip()
        for prefix in ("launch_candidate_", "launch_"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
                break
        # 本地点位可能是 launch_076_发射阵地_76，运行时投影点则是
        # launch_candidate_发射阵地_76；二者必须视为同一个物理发射点。
        marker = raw.find("发射阵地")
        if marker >= 0:
            return raw[marker:]
        return re.sub(r"^\d+[_-]+", "", raw)

    def _external_launch_public_id(self, launch_node: str) -> str:
        row = self.external_launch_point_rows_by_node.get(str(launch_node), {})
        for key in ("name", "fire_point_id", "launch_point", "index"):
            value = row.get(key)
            if value not in {None, ""}:
                return self._canonical_launch_id(value)
        return self._canonical_launch_id(launch_node)

    def _conflict_stats(self) -> dict:
        return {
            "disabled": False,
            "mode": "model_forwarded_candidate_paths_with_slot_conflict_check",
            "slot_sec": self.external_conflict_slot_sec,
            "distance_m": self.external_conflict_distance_m,
            "hide_capacity": self.external_hide_capacity,
            "delay_step_sec": self.external_conflict_delay_step_sec,
            "max_delay_sec": self.external_conflict_max_delay_sec,
            "fallback_to_rank1": self.external_conflict_fallback_to_rank1,
            "force_emit_when_unresolved": self.external_force_emit_bundle_when_unresolved,
        }

    def _selected_vehicle_rows_for_task(self, task_id: str) -> List[Tuple[str, str]]:
        rows: List[Tuple[str, str]] = []
        seen: Set[str] = set()
        for sid, candidates in self.external_vehicle_selection.items():
            st = self.subtasks.get(sid)
            if not st or st.task_id != task_id or not candidates:
                continue
            vehicle_id = str(candidates[0])
            if not vehicle_id or vehicle_id in seen:
                continue
            rows.append((sid, vehicle_id))
            seen.add(vehicle_id)
        rows.sort(key=lambda item: (self.subtasks[item[0]].fire_time, item[0]))
        return rows

    def _candidate_payload_for_vehicle(self, vehicle_id: str) -> Optional[dict]:
        if vehicle_id in self.external_vehicle_candidate_paths:
            return self.external_vehicle_candidate_paths[vehicle_id]
        try:
            target_port = int(float(vehicle_id))
        except Exception:
            target_port = None
        for key, payload in self.external_vehicle_candidate_paths.items():
            if str(key) == str(vehicle_id):
                return payload
            if target_port is None:
                continue
            try:
                if int(float(key)) == target_port:
                    return payload
            except Exception:
                continue
        return None

    def _depot_queue_waits(self, task_id: str, selected: List[Tuple[str, str]]) -> Dict[str, float]:
        requests: Dict[str, List[dict]] = defaultdict(list)
        for subtask_id, vehicle_id in selected:
            payload = self._candidate_payload_for_vehicle(vehicle_id) or {}
            best: Optional[dict] = None
            for path in payload.get("paths") or []:
                if not isinstance(path, dict):
                    continue
                for depot in path.get("depot_candidates") or []:
                    if not isinstance(depot, dict):
                        continue
                    if not self._depot_candidate_selected_for_subtask(task_id, subtask_id, depot):
                        continue
                    arrival = self._iso_to_sim_seconds(depot.get("depot_arrival_time"))
                    if arrival is None:
                        continue
                    selected_depot = str(
                        depot.get("depot_id")
                        or depot.get("depot_port")
                        or depot.get("depot_node")
                        or ""
                    )
                    row = {
                        "subtask_id": subtask_id,
                        "arrival": float(arrival),
                        "reload": float(depot.get("reload_duration_sec", self.reload_duration_sec) or 0.0),
                        "capacity": max(1, int(depot.get("capacity", self.depot_capacity) or 1)),
                        "rank": int(depot.get("rank", 999) or 999),
                        "selected_depot": selected_depot,
                    }
                    if best is None or (row["rank"], row["arrival"]) < (best["rank"], best["arrival"]):
                        best = row
            if best is not None:
                requests[str(best["selected_depot"])].append(best)
        waits: Dict[str, float] = {}
        for depot_id, rows in requests.items():
            capacity = max(row["capacity"] for row in rows)
            available = [0.0 for _ in range(capacity)]
            heapq.heapify(available)
            for row in sorted(rows, key=lambda item: (item["arrival"], item["subtask_id"])):
                free_at = heapq.heappop(available)
                service_start = max(row["arrival"], free_at)
                waits[row["subtask_id"]] = round(service_start - row["arrival"], 3)
                heapq.heappush(available, service_start + row["reload"])
            self.event_log.log(
                "depot_queue_scheduled",
                depot_id=depot_id,
                vehicles=len(rows),
                capacity=capacity,
                max_queue_wait_sec=max((waits[row["subtask_id"]] for row in rows), default=0.0),
            )
        return waits

    def _path_with_selected_depot(
        self,
        path: dict,
        task_id: str,
        subtask_id: str,
        queue_wait_sec: float = 0.0,
    ) -> Optional[dict]:
        depot_candidates = path.get("depot_candidates")
        if not isinstance(depot_candidates, list) or not depot_candidates:
            return dict(path)
        depot = self._best_selected_depot_candidate(task_id, subtask_id, depot_candidates)
        if depot is None:
            return None
        to_fire_points = self._normalise_external_path_points(path)
        post_points = depot.get("path_points") or []
        if not isinstance(post_points, list):
            post_points = []
        post_points = [dict(point) for point in post_points if isinstance(point, dict)]
        queue_wait_sec = max(0.0, float(queue_wait_sec))
        to_depot_count = int(depot.get("to_depot_point_count", 0) or 0)
        if queue_wait_sec > 0.0 and len(post_points) >= 2 and to_depot_count >= 2:
            queue_index = min(len(post_points) - 2, to_depot_count - 2)
            queue_time = self._iso_to_sim_seconds(post_points[queue_index].get("time"))
            if queue_time is not None:
                queue_point = dict(post_points[queue_index])
                queue_point["time"] = round(queue_time + queue_wait_sec, 3)
                post_points.insert(queue_index + 1, queue_point)
                for index in range(queue_index + 2, len(post_points)):
                    shifted_time = self._iso_to_sim_seconds(post_points[index].get("time"))
                    if shifted_time is not None:
                        post_points[index]["time"] = round(shifted_time + queue_wait_sec, 3)
        combined = list(to_fire_points)
        normalized_post = self._normalise_external_path_points({"path_points": post_points})
        if combined and normalized_post:
            first_post = normalized_post[0]
            last_fire = combined[-1]
            same_point = (
                first_post.get("lon") == last_fire.get("lon")
                and first_post.get("lat") == last_fire.get("lat")
            )
            same_time = False
            try:
                same_time = abs(
                    float(first_post.get("time", 0.0) or 0.0)
                    - float(last_fire.get("time", 0.0) or 0.0)
                ) < 1e-6
            except Exception:
                same_time = first_post.get("time") == last_fire.get("time")
            combined.extend(normalized_post[1:] if same_point and same_time else normalized_post)
        else:
            combined.extend(normalized_post)
        merged = dict(path)
        merged["path_points"] = combined
        merged["selected_depot"] = dict(depot)
        merged["depot_id"] = depot.get("depot_id")
        merged["depot_node"] = depot.get("depot_node")
        merged["depot_port"] = depot.get("depot_port")
        merged["depot_capacity"] = int(depot.get("capacity", self.depot_capacity) or self.depot_capacity)
        merged["reload_duration_sec"] = float(depot.get("reload_duration_sec", self.reload_duration_sec) or 0.0)
        merged["depot_arrival_time"] = depot.get("depot_arrival_time")
        merged["reload_end_time"] = depot.get("reload_end_time")
        if queue_wait_sec > 0.0:
            merged["depot_arrival_time"] = self._add_sim_seconds(
                merged["depot_arrival_time"],
                queue_wait_sec,
            )
            merged["reload_end_time"] = self._add_sim_seconds(
                merged["reload_end_time"],
                queue_wait_sec,
            )
        segments = [dict(segment) for segment in (depot.get("segments") or []) if isinstance(segment, dict)]
        if queue_wait_sec > 0.0:
            for segment in segments:
                kind = str(segment.get("kind") or "")
                if kind != "to_depot" and segment.get("start_at") not in {None, ""}:
                    segment["start_at"] = self._add_sim_seconds(segment.get("start_at"), queue_wait_sec)
                if segment.get("end_at") not in {None, ""}:
                    segment["end_at"] = self._add_sim_seconds(segment.get("end_at"), queue_wait_sec)
        merged["post_fire_segments"] = segments
        merged["depot_queue_wait_sec"] = round(queue_wait_sec, 3)
        return merged

    def _depot_queue_wait_for_path(
        self,
        path: dict,
        post_delay_sec: float,
        reservations: Dict[str, List[Tuple[float, float, str]]],
    ) -> float:
        depot_id = str(path.get("depot_id") or "")
        arrival = self._iso_to_sim_seconds(path.get("depot_arrival_time"))
        reload_end = self._iso_to_sim_seconds(path.get("reload_end_time"))
        if not depot_id or arrival is None or reload_end is None:
            return 0.0
        arrival += max(0.0, post_delay_sec)
        duration = max(0.0, reload_end - self._iso_to_sim_seconds(path.get("depot_arrival_time")))
        capacity = max(1, int(path.get("depot_capacity", self.depot_capacity) or 1))
        service_start = arrival
        rows = reservations.get(depot_id, [])
        for _ in range(len(rows) + 1):
            service_end = service_start + duration
            # 公式(2) time_window_overlap 实际使用点：判断同一贮备库补给服务时间窗是否重叠。
            # 重叠车辆数未超过 capacity 时允许排队服务，否则把 service_start 推到下一空窗。
            overlaps = [
                row
                for row in rows
                if time_window_overlap(service_start, service_end, row[0], row[1]) > 0.0
            ]
            if len(overlaps) < capacity:
                return round(max(0.0, service_start - arrival), 3)
            next_start = min((row[1] for row in overlaps if row[1] > service_start), default=service_start)
            if next_start <= service_start + 1e-6:
                break
            service_start = next_start
        return round(max(0.0, service_start - arrival), 3)

    def _shift_post_fire_suffix(
        self,
        path: dict,
        pre_fire_count: int,
        delay_sec: float,
    ) -> Tuple[dict, List[dict], List[dict]]:
        points = self._normalise_external_path_points(path)
        pre_fire_count = max(0, min(int(pre_fire_count), len(points)))
        prefix = [dict(point) for point in points[:pre_fire_count]]
        suffix = [dict(point) for point in points[pre_fire_count:]]
        delay_sec = max(0.0, float(delay_sec))
        if delay_sec > 0.0:
            for point in suffix:
                point["time"] = round(float(point.get("time", 0.0) or 0.0) + delay_sec, 3)
            if prefix:
                wait_point = dict(prefix[-1])
                wait_point["time"] = round(float(wait_point.get("time", 0.0) or 0.0) + delay_sec, 3)
                suffix.insert(0, wait_point)
        shifted = dict(path)
        shifted["path_points"] = prefix + suffix
        shifted["post_fire_delay_sec"] = round(delay_sec, 3)
        if delay_sec > 0.0:
            shifted["depot_arrival_time"] = self._add_sim_seconds(path.get("depot_arrival_time"), delay_sec)
            shifted["reload_end_time"] = self._add_sim_seconds(path.get("reload_end_time"), delay_sec)
            segments = [dict(segment) for segment in (path.get("post_fire_segments") or []) if isinstance(segment, dict)]
            for segment in segments:
                for key in ("start_at", "end_at"):
                    if segment.get(key) not in {None, ""}:
                        segment[key] = self._add_sim_seconds(segment.get(key), delay_sec)
            shifted["post_fire_segments"] = segments
        return shifted, prefix, suffix

    def _resolve_selected_depot_suffixes(self, task_id: str, entries: List[dict]) -> dict:
        reserved_slots: Dict[int, List[Tuple[float, float, str]]] = {}
        depot_reservations: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
        for entry in entries:
            self._reserve_external_path(
                list(entry.get("points") or []),
                reserved_slots,
                str(entry.get("vehicle_id") or ""),
            )

        attached = 0
        omitted_unselected = 0
        omitted_conflict = 0
        ordered = sorted(
            entries,
            key=lambda entry: (
                float((entry.get("points") or [{}])[-1].get("time", 0.0) or 0.0),
                str(entry.get("subtask_id") or ""),
            ),
        )
        for entry in ordered:
            subtask_id = str(entry.get("subtask_id") or "")
            vehicle_id = str(entry.get("vehicle_id") or "")
            base_path = dict(entry.get("path") or {})
            pre_fire_points = [dict(point) for point in (entry.get("points") or [])]
            base_path["path_points"] = pre_fire_points
            selected_depot = self._path_with_selected_depot(base_path, task_id, subtask_id, 0.0)
            row = entry.get("trajectory_row")
            if selected_depot is None or not selected_depot.get("depot_id"):
                omitted_unselected += 1
                if isinstance(row, dict):
                    row["depot_omitted_reason"] = "candidate_depot_not_selected"
                self.event_log.log(
                    "external_dispatch_depot_suffix_omitted",
                    task_id=task_id,
                    subtask_id=subtask_id,
                    vehicle_id=vehicle_id,
                    reason="candidate_depot_not_selected",
                )
                continue

            accepted: Optional[Tuple[dict, List[dict], List[dict]]] = None
            delay = 0.0
            while delay <= self.external_post_fire_max_delay_sec + 1e-6:
                queue_wait = self._depot_queue_wait_for_path(selected_depot, delay, depot_reservations)
                merged = self._path_with_selected_depot(base_path, task_id, subtask_id, queue_wait)
                if merged is None:
                    break
                shifted, _prefix, suffix = self._shift_post_fire_suffix(
                    merged,
                    len(pre_fire_points),
                    delay,
                )
                if self._external_path_conflict_detail(suffix, reserved_slots) is None:
                    accepted = shifted, shifted.get("path_points") or [], suffix
                    break
                delay += self.external_conflict_delay_step_sec

            if accepted is None:
                omitted_conflict += 1
                if isinstance(row, dict):
                    row["depot_omitted_reason"] = "post_fire_conflict_unresolved"
                self.event_log.log(
                    "external_dispatch_depot_suffix_omitted",
                    task_id=task_id,
                    subtask_id=subtask_id,
                    vehicle_id=vehicle_id,
                    reason="post_fire_conflict_unresolved",
                    max_delay_sec=self.external_post_fire_max_delay_sec,
                )
                continue

            final_path, final_points, suffix_points = accepted
            entry["path"] = final_path
            entry["points"] = final_points
            self._reserve_external_path(suffix_points, reserved_slots, vehicle_id)
            depot_id = str(final_path.get("depot_id") or "")
            depot_arrival = self._iso_to_sim_seconds(final_path.get("depot_arrival_time"))
            reload_end = self._iso_to_sim_seconds(final_path.get("reload_end_time"))
            if depot_id and depot_arrival is not None and reload_end is not None:
                depot_reservations[depot_id].append((depot_arrival, reload_end, vehicle_id))
            if isinstance(row, dict):
                row["path_points"] = final_points
                for key in (
                    "depot_id",
                    "depot_node",
                    "depot_port",
                    "depot_arrival_time",
                    "reload_end_time",
                    "reload_duration_sec",
                    "post_fire_segments",
                    "depot_queue_wait_sec",
                    "post_fire_delay_sec",
                ):
                    if key in final_path:
                        row[key] = final_path.get(key)
            attached += 1
            self.event_log.log(
                "external_dispatch_depot_suffix_attached",
                task_id=task_id,
                subtask_id=subtask_id,
                vehicle_id=vehicle_id,
                depot_id=depot_id,
                depot_port=final_path.get("depot_port"),
                queue_wait_sec=final_path.get("depot_queue_wait_sec", 0.0),
                post_fire_delay_sec=final_path.get("post_fire_delay_sec", 0.0),
                path_points=len(final_points),
            )
        return {
            "attached_count": attached,
            "omitted_unselected_count": omitted_unselected,
            "omitted_conflict_count": omitted_conflict,
        }

    def _canonical_depot_selection_id(self, value: Any) -> str:
        selected = str(value or "").strip()
        if not selected:
            return ""
        for row in self._compact_depot_points():
            aliases = {
                str(row.get("depot_id") or ""),
                str(row.get("depot_node") or ""),
                str(row.get("depot_port") or ""),
                str(row.get("name") or ""),
            }
            if selected in aliases:
                return str(row.get("depot_id") or selected)
        return selected

    def _depot_candidate_aliases(self, depot: dict) -> Set[str]:
        return {
            str(depot.get("depot_id") or ""),
            str(depot.get("depot_node") or ""),
            str(depot.get("depot_port") or ""),
            str(depot.get("name") or ""),
        } - {""}

    def _selected_depot_pool_for_task(self, task_id: str) -> Set[str]:
        pool = {
            self._canonical_depot_selection_id(value)
            for value in self.external_depot_selection_by_task.get(str(task_id or ""), set())
        }
        return {value for value in pool if value}

    def _depot_candidate_selected_for_subtask(self, task_id: str, subtask_id: str, depot: dict) -> bool:
        aliases = self._depot_candidate_aliases(depot)
        selected_for_subtask = self._canonical_depot_selection_id(
            self.external_depot_selection.get(str(subtask_id or ""))
        )
        if selected_for_subtask:
            return selected_for_subtask in aliases
        selected_pool = self._selected_depot_pool_for_task(task_id)
        if selected_pool:
            return bool(aliases & selected_pool)
        return False

    def _depot_candidate_sort_key(self, depot: dict) -> Tuple[float, float, str]:
        scheduler_rank = depot.get("scheduler_candidate_rank")
        rank = depot.get("rank", 999)
        try:
            primary_rank = float(scheduler_rank if scheduler_rank is not None else rank)
        except Exception:
            primary_rank = 999.0
        try:
            total_sec = float(depot.get("total_post_fire_sec", float("inf")) or float("inf"))
        except Exception:
            total_sec = float("inf")
        return (
            primary_rank,
            total_sec,
            str(depot.get("depot_id") or depot.get("depot_port") or depot.get("depot_node") or ""),
        )

    def _best_selected_depot_candidate(
        self,
        task_id: str,
        subtask_id: str,
        depot_candidates: List[dict],
    ) -> Optional[dict]:
        matched = [
            depot
            for depot in depot_candidates
            if isinstance(depot, dict)
            and self._depot_candidate_selected_for_subtask(task_id, subtask_id, depot)
        ]
        if not matched:
            return None
        return min(matched, key=self._depot_candidate_sort_key)

    def _task_ids_for_depot_selection(self, payload: dict, row: dict) -> List[str]:
        explicit = str(row.get("task_id") or payload.get("task_id") or "").strip()
        if explicit:
            return [explicit]
        task_ids = {
            self.subtasks[sid].task_id
            for sid in self.external_vehicle_selection
            if sid in self.subtasks
        }
        if not task_ids:
            task_ids = {
                st.task_id
                for st in self.subtasks.values()
                if st.status not in {"DONE", "FAILED"}
                and not st.is_redundant
                and st.task_id not in self.dispatched_trajectory_bundle_tasks
            }
        return sorted(str(task_id) for task_id in task_ids if str(task_id))

    @classmethod
    def _add_sim_seconds(cls, value: Any, seconds: float) -> Any:
        parsed = cls._iso_to_sim_seconds(value)
        if parsed is None:
            return value
        return round(parsed + float(seconds), 3)

    def _candidate_path_launch_id(self, path: dict) -> str:
        launch_node = str(
            path.get("launch_node")
            or path.get("fire_point_id")
            or path.get("target_node")
            or path.get("launch_point")
            or ""
        )
        return self._external_launch_public_id(launch_node) if launch_node else ""

    @staticmethod
    def _candidate_path_hide_id(path: dict) -> str:
        if not bool(path.get("hide_selected", False)):
            return ""
        return str(path.get("hide_point") or "")

    @staticmethod
    # 车辆候选路径排序键：
    # 先优先正常路径，再尽量避免 `start_wait`，再比较路程时间、评分、候选排名与是否真的使用了隐蔽点。
    def _external_candidate_path_sort_key(path: dict) -> tuple:
        try:
            score = float(path.get("score_total", 0.0) or 0.0)
        except Exception:
            score = 0.0
        try:
            rank = int(path.get("rank", 999) or 999)
        except Exception:
            rank = 999
        strategy = str(path.get("timing_strategy") or "")
        try:
            travel_sec = float(path.get("travel_seconds", float("inf")) or float("inf"))
        except Exception:
            travel_sec = float("inf")
        try:
            wait_sec = float(path.get("wait_seconds", 0.0) or 0.0)
        except Exception:
            wait_sec = 0.0
        meaningful_hide = bool(path.get("hide_selected", False)) and wait_sec > 0.0
        return (
            1 if bool(path.get("fallback_only", False)) else 0,
            0 if meaningful_hide else 1,
            1 if strategy == "start_wait" else 0,
            rank,
            travel_sec,
            -score,
        )

    @staticmethod
    def _external_candidate_path_score(path: dict) -> float:
        try:
            return float(path.get("score_total", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _recover_hide_unavailable_candidate(self, path: dict) -> dict:
        """兼容旧车辆端：隐蔽点不可达时，保留其中已经生成的直达路径。"""
        row = dict(path)
        reason = str(row.get("reject_reason") or "")
        if reason not in {"no_reachable_hide", "hide_unavailable", "no_valid_hide"}:
            return row
        points = self._normalise_external_path_points(row)
        if len(points) < 2 or not self._candidate_path_launch_id(row):
            return row
        fire_error = row.get("fire_time_error_sec")
        if fire_error is None and isinstance(row.get("path_report"), dict):
            fire_error = row["path_report"].get("fire_time_error_sec")
        try:
            if fire_error not in {None, ""} and not (
                -self.fire_window_early_grace_sec
                <= float(fire_error)
                <= max(15.0, float(self.fire_window_grace_sec))
            ):
                return row
        except Exception:
            return row
        row["reject_reason"] = ""
        row["feasible"] = True
        row["hide_selected"] = False
        row["hide_point"] = None
        row["hide_node"] = None
        row["timing_strategy"] = "start_wait" if float(row.get("wait_seconds", 0.0) or 0.0) > 0.0 else "direct"
        row["fallback_only"] = True
        row["score_total"] = max(1.0, self._external_candidate_path_score(row))
        return row

    # 判断车辆返回的候选路径能否进入调度阶段：
    # 这里只做硬性可用性筛选，不做多车冲突消解。
    def _external_candidate_path_usable(self, path: dict) -> bool:
        if path.get("feasible") is False:
            return False
        if path.get("reject_reason") not in {None, ""}:
            return False
        if self._external_candidate_path_score(path) <= 0.0:
            return False
        if not self._candidate_path_launch_id(path):
            return False
        if not self._normalise_external_path_points(path):
            return False
        try:
            if float(path.get("detour_ratio", 1.0) or 1.0) > 3.0:
                return False
        except Exception:
            return False
        fire_error = path.get("fire_time_error_sec")
        if fire_error is None and isinstance(path.get("path_report"), dict):
            fire_error = path["path_report"].get("fire_time_error_sec")
        if fire_error not in {None, ""}:
            try:
                error = float(fire_error)
                return (
                    -self.fire_window_early_grace_sec
                    <= error
                    <= max(15.0, float(self.fire_window_grace_sec))
                )
            except Exception:
                return False
        return True

    # 给不可用路径归类拒绝原因，方便后续日志和调试定位。
    def _external_candidate_reject_reason(self, path: dict) -> str:
        reason = str(path.get("reject_reason") or "")
        if reason:
            if reason.startswith("fire_time_error"):
                return "time_infeasible"
            return reason
        if path.get("feasible") is False:
            return "time_infeasible"
        try:
            if float(path.get("detour_ratio", 1.0) or 1.0) > 3.0:
                return "severe_detour"
        except Exception:
            return "severe_detour"
        if not self._candidate_path_launch_id(path):
            return "no_candidate_launch"
        if not self._normalise_external_path_points(path):
            return "no_reachable_launch"
        fire_error = path.get("fire_time_error_sec")
        if fire_error not in {None, ""}:
            try:
                if not (
                    -self.fire_window_early_grace_sec
                    <= float(fire_error)
                    <= max(15.0, float(self.fire_window_grace_sec))
                ):
                    return "time_infeasible"
            except Exception:
                return "time_infeasible"
        return "no_unique_launch_or_conflict_free_path"

    # 统一外部路径点时间格式：
    # 车辆端可能给 ISO 时间，也可能给相对秒数；调度端在冲突检测前统一转成仿真秒。
    def _normalise_external_path_points(self, path: dict) -> List[dict]:
        points = path.get("path_points") or path.get("trajectory_geo") or path.get("trajectory") or []
        if not isinstance(points, list):
            return []
        out: List[dict] = []
        last_time: Optional[float] = None
        for idx, point in enumerate(points):
            if not isinstance(point, dict):
                continue
            row = dict(point)
            sim_time = self._iso_to_sim_seconds(row.get("time"))
            if sim_time is None:
                sim_time = (last_time + self.external_conflict_slot_sec) if last_time is not None else idx * self.external_conflict_slot_sec
            row["time"] = round(float(sim_time), 3)
            last_time = float(sim_time)
            out.append(row)
        return out

    # 解析路径中的“等待区间”。
    # 优先使用车辆端显式给出的等待起止时间，缺失时再根据 wait_node_index 和 edge_seconds 反推。
    def _wait_interval_for_external_path(self, path: dict, points: List[dict]) -> Optional[Tuple[float, float]]:
        try:
            wait_seconds = float(path.get("wait_seconds", 0.0) or 0.0)
        except Exception:
            wait_seconds = 0.0
        if wait_seconds <= 0 or not points:
            return None
        wait_start_raw = path.get("wait_start_time")
        wait_end_raw = path.get("wait_end_time")
        if wait_start_raw not in {None, ""} and wait_end_raw not in {None, ""}:
            start = self._iso_to_sim_seconds(wait_start_raw)
            end = self._iso_to_sim_seconds(wait_end_raw)
            if start is not None and end is not None and end > start:
                return round(float(start), 3), round(float(end), 3)
        try:
            wait_node_index = int(path.get("wait_node_index"))
        except Exception:
            return None
        edge_seconds = []
        for item in path.get("edge_seconds") or []:
            try:
                edge_seconds.append(float(item))
            except Exception:
                edge_seconds.append(0.0)
        if wait_node_index < 0:
            return None
        start_time = float(points[0].get("time", 0.0) or 0.0)
        travel_before_wait = sum(edge_seconds[: min(wait_node_index, len(edge_seconds))])
        wait_start = start_time + travel_before_wait
        return round(wait_start, 3), round(wait_start + wait_seconds, 3)
    # 计算路径中实际用于隐蔽点等待的时间段，用于后续容量冲突检查。
    def _hide_wait_interval_for_external_path(
        self,
        path: dict,
        points: List[dict],
        delay_sec: float = 0.0,
    ) -> Optional[Tuple[str, float, float]]:
        hide_id = self._candidate_path_hide_id(path)
        if not hide_id:
            return None
        interval = self._wait_interval_for_external_path(path, points)
        if interval is None:
            return None
        start, end = interval
        if delay_sec > 0.0:
            start = min(end, start + delay_sec)
        if end <= start:
            return None
        return hide_id, round(start, 3), round(end, 3)
    # 隐蔽点容量冲突：同一隐蔽点在重叠时间内停靠车辆数不能超过容量。
    def _external_hide_wait_conflicts(
        self,
        path: dict,
        points: List[dict],
        reserved: Dict[str, List[Tuple[float, float, str]]],
        delay_sec: float = 0.0,
    ) -> bool:
        return self._external_hide_wait_conflict_detail(path, points, reserved, delay_sec) is not None

    def _external_hide_wait_conflict_detail(
        self,
        path: dict,
        points: List[dict],
        reserved: Dict[str, List[Tuple[float, float, str]]],
        delay_sec: float = 0.0,
    ) -> Optional[dict]:
        interval = self._hide_wait_interval_for_external_path(path, points, delay_sec)
        if interval is None:
            return None
        hide_id, start, end = interval
        overlaps = [
            {
                "vehicle_id": str(other_vehicle),
                "start_time": round(float(other_start), 3),
                "end_time": round(float(other_end), 3),
            }
            for other_start, other_end, other_vehicle in reserved.get(hide_id, [])
            # 公式(2) time_window_overlap 实际使用点：统计同一隐蔽点等待区间的重叠车辆。
            # 后续接公式(4) hide_point_capacity_delta 判断是否超过隐蔽点容量。
            if time_window_overlap(start, end, other_start, other_end) > 0.0
        ]
        # 公式(4) hide_point_capacity_delta 实际使用点：已有重叠车辆 + 当前车辆是否超过容量。
        # 不超过容量则认为隐蔽等待可并行，超过容量才返回 hide_capacity 冲突。
        if hide_point_capacity_delta(len(overlaps) + 1, self.external_hide_capacity) <= 0:
            return None
        return {
            "conflict_type": "hide_capacity",
            "hide_point": hide_id,
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "capacity": self.external_hide_capacity,
            "conflicting_vehicles": overlaps,
        }

    def _next_external_conflict_delay(
        self,
        delay_sec: float,
        hide_conflict: Optional[dict],
    ) -> float:
        """直接跳过已知的隐蔽点占用区间，避免按 5 秒逐次空转。"""
        next_delay = delay_sec + self.external_conflict_delay_step_sec
        if hide_conflict:
            conflict_end = max(
                (
                    float(row.get("end_time", 0.0) or 0.0)
                    for row in hide_conflict.get("conflicting_vehicles") or []
                ),
                default=0.0,
            )
            wait_start = float(hide_conflict.get("start_time", 0.0) or 0.0)
            next_delay = max(next_delay, delay_sec + max(0.0, conflict_end - wait_start))
        step = self.external_conflict_delay_step_sec
        return math.ceil((next_delay - 1e-9) / step) * step

    def _reserve_external_hide_wait(
        self,
        path: dict,
        points: List[dict],
        reserved: Dict[str, List[Tuple[float, float, str]]],
        vehicle_id: str,
        delay_sec: float = 0.0,
    ) -> None:
        interval = self._hide_wait_interval_for_external_path(path, points, delay_sec)
        if interval is None:
            return
        hide_id, start, end = interval
        reserved.setdefault(hide_id, []).append((start, end, str(vehicle_id)))
    # 冲突消解辅助：尝试把车辆出发时间向后推，但压缩隐蔽点/起点等待时间，
    # 使最终到达发射点的时间保持不变。
    def _delay_before_wait_preserving_arrival(
        self,
        path: dict,
        points: List[dict],
        delay_sec: float,
    ) -> Optional[List[dict]]:
        if delay_sec <= 0:
            return [dict(point) for point in points]
        interval = self._wait_interval_for_external_path(path, points)
        if interval is None:
            # 没有可压缩的等待区间，无法在保持到达时间不变的前提下延迟。
            return None
        wait_start, wait_end = interval
        if delay_sec > (wait_end - wait_start) + 1e-6:
            # 延迟量超过等待时长，压缩完等待也无法保持原到达时间。
            return None
        shifted: List[dict] = []
        for point in points:
            row = dict(point)
            t = float(row.get("time", 0.0) or 0.0)
            if t < wait_start:
                # 等待开始前的行程整体后移，相当于晚出发。
                row["time"] = round(t + delay_sec, 3)
                shifted.append(row)
            elif wait_start <= t < wait_start + delay_sec:
                # 删除等待区间前半段，抵消前面增加的延迟。
                continue
            else:
                # 等待压缩后的后续轨迹保持原时间，确保最终到达时间不变。
                row["time"] = round(t, 3)
                shifted.append(row)
        return shifted

    def _external_conflict_delay_limit(self, path: dict, points: List[dict]) -> float:
        interval = self._wait_interval_for_external_path(path, points)
        wait_slack = (interval[1] - interval[0]) if interval is not None else 0.0
        return max(self.external_conflict_max_delay_sec, wait_slack)

    def _path_conflict_fixed_after_wait(
        self,
        path: dict,
        points: List[dict],
        path_conflict: Optional[dict],
    ) -> bool:
        """Return true when delaying departure cannot move the conflicting point.

        `_delay_before_wait_preserving_arrival` only shifts points before the
        wait interval and compresses the wait. Points at/after wait_end keep
        their original timestamps, so a path conflict there will be hit again
        for every later delay attempt. Treating that as a dead candidate avoids
        repeatedly scanning the same fixed suffix in dense regions.
        """
        if not path_conflict:
            return False
        interval = self._wait_interval_for_external_path(path, points)
        if interval is None:
            return False
        try:
            conflict_time = float(path_conflict.get("time", 0.0) or 0.0)
        except Exception:
            return False
        _wait_start, wait_end = interval
        return conflict_time >= wait_end - 1e-6

    def _advance_after_wait_preserving_finish(
        self,
        path: dict,
        points: List[dict],
        advance_sec: float,
    ) -> Optional[Tuple[dict, List[dict]]]:
        if advance_sec <= 0.0 or not points:
            return dict(path), [dict(point) for point in points]
        interval = self._wait_interval_for_external_path(path, points)
        if interval is None:
            return None
        wait_start, wait_end = interval
        wait_duration = wait_end - wait_start
        if advance_sec > wait_duration + 1e-6:
            return None
        shortened_wait_end = wait_end - advance_sec
        original_finish = float(points[-1].get("time", 0.0) or 0.0)
        post_start: Optional[float] = None
        for segment in path.get("post_fire_segments") or []:
            if isinstance(segment, dict) and segment.get("kind") == "to_depot":
                post_start = self._iso_to_sim_seconds(segment.get("start_at"))
                break
        shifted: List[dict] = []
        for point in points:
            row = dict(point)
            point_time = float(row.get("time", 0.0) or 0.0)
            if post_start is not None and point_time >= post_start - 1e-6:
                shifted.append(row)
                continue
            if shortened_wait_end < point_time < wait_end:
                continue
            if point_time >= wait_end:
                row["time"] = round(point_time - advance_sec, 3)
            shifted.append(row)
        if not shifted:
            return None
        arrival = dict(shifted[-1])
        if post_start is None and float(arrival.get("time", 0.0) or 0.0) < original_finish - 1e-6:
            finish_wait = dict(arrival)
            finish_wait["time"] = round(original_finish, 3)
            shifted.append(finish_wait)
        adjusted = dict(path)
        adjusted["wait_seconds"] = round(max(0.0, wait_duration - advance_sec), 3)
        adjusted["wait_start_time"] = round(wait_start, 3)
        adjusted["wait_end_time"] = round(shortened_wait_end, 3)
        adjusted["conflict_retime_advance_sec"] = round(advance_sec, 3)
        return adjusted, shifted

    @staticmethod
    def _point_conflict_xy(point: dict) -> Optional[Tuple[float, float]]:
        try:
            if point.get("lon") is not None and point.get("lat") is not None:
                lon = float(point.get("lon"))
                lat = float(point.get("lat"))
                cos_lat = max(0.1, math.cos(math.radians(lat)))
                return lon * 111320.0 * cos_lat, lat * 110540.0
            if point.get("x") is not None and point.get("y") is not None:
                return float(point.get("x")), float(point.get("y"))
        except Exception:
            return None
        return None

    # 当全局分配或冲突消解把正常候选都刷掉时，
    # 从原始可用路径里再找一个“至少能发出去”的兜底方案。
    def _fallback_external_candidate_path(
        self,
        raw_paths: List[dict],
        used_launch_points: Set[str],
        hide_wait_reservations: Optional[Dict[str, List[Tuple[float, float, str]]]] = None,
        reserved_slots: Optional[Dict[int, List[Tuple[float, float, str]]]] = None,
    ) -> Tuple[Optional[dict], List[dict], str]:
        ranked: List[dict] = []
        for path in raw_paths:
            if not self._external_candidate_path_usable(path):
                continue
            launch_id = self._candidate_path_launch_id(path)
            points = self._normalise_external_path_points(path)
            if not launch_id or not points:
                continue
            item = dict(path)
            item["_fallback_points"] = points
            ranked.append(item)
        ranked.sort(key=self._external_candidate_path_sort_key)
        if not ranked:
            return None, [], ""
        for require_positive_score in (True, False):
            for path in ranked:
                if require_positive_score and self._external_candidate_path_score(path) <= 0.0:
                    continue
                launch_id = self._candidate_path_launch_id(path)
                if launch_id not in used_launch_points:
                    points = list(path["_fallback_points"])
                    if hide_wait_reservations is not None and self._external_hide_wait_conflicts(
                        path,
                        points,
                        hide_wait_reservations,
                    ):
                        continue
                    if reserved_slots is not None and self._external_path_conflicts(points, reserved_slots):
                        continue
                    return path, points, launch_id
        return None, [], ""

    def _external_reservations_from_entries(
        self,
        entries: List[dict],
    ) -> Tuple[Set[str], Dict[int, List[Tuple[float, float, str]]], Dict[str, List[Tuple[float, float, str]]]]:
        used_launch_points: Set[str] = set()
        reserved_slots: Dict[int, List[Tuple[float, float, str]]] = {}
        hide_wait_reservations: Dict[str, List[Tuple[float, float, str]]] = {}
        for entry in entries:
            launch_id = str(entry.get("launch") or "")
            vehicle_id = str(entry.get("vehicle_id") or "")
            path = entry.get("path") or {}
            points = entry.get("points") or []
            if launch_id:
                used_launch_points.add(launch_id)
            self._reserve_external_path(points, reserved_slots, vehicle_id)
            self._reserve_external_hide_wait(
                path,
                points,
                hide_wait_reservations,
                vehicle_id,
                float(entry.get("delay", 0.0) or 0.0),
            )
        return used_launch_points, reserved_slots, hide_wait_reservations

    def _audit_external_dispatch_entries(self, entries: List[dict]) -> dict:
        used_launches: Set[str] = set()
        reserved_slots: Dict[int, List[Tuple[float, float, str]]] = {}
        hide_reservations: Dict[str, List[Tuple[float, float, str]]] = {}
        depot_reservations: Dict[str, List[Tuple[float, float, str]]] = {}
        conflicts: List[dict] = []
        for entry in entries:
            vehicle_id = str(entry.get("vehicle_id") or "")
            launch = str(entry.get("launch") or "")
            path = entry.get("path") or {}
            points = entry.get("points") or []
            delay = float(entry.get("delay", 0.0) or 0.0)
            if not launch or launch in used_launches:
                conflicts.append(
                    {
                        "vehicle_id": vehicle_id,
                        "type": "launch_conflict",
                        "launch": launch,
                    }
                )
                continue
            hide_detail = self._external_hide_wait_conflict_detail(
                path,
                points,
                hide_reservations,
                delay,
            )
            path_detail = self._external_path_conflict_detail(points, reserved_slots)
            depot_id = str(path.get("depot_id") or "")
            depot_arrival = self._iso_to_sim_seconds(path.get("depot_arrival_time"))
            reload_end = self._iso_to_sim_seconds(path.get("reload_end_time"))
            depot_capacity = max(1, int(path.get("depot_capacity", self.depot_capacity) or 1))
            depot_overlaps: List[Tuple[float, float, str]] = []
            if depot_id and depot_arrival is not None and reload_end is not None:
                # 公式(2) time_window_overlap 实际使用点：审计最终轨迹里同库补给区间重叠。
                # 这里不重新分配，只用于发现调度结果是否违反贮备库容量。
                depot_overlaps = [
                    row
                    for row in depot_reservations.get(depot_id, [])
                    if time_window_overlap(depot_arrival, reload_end, row[0], row[1]) > 0.0
                ]
            if hide_detail is not None:
                conflicts.append(
                    {
                        "vehicle_id": vehicle_id,
                        "type": "hide_time_conflict",
                        "detail": hide_detail,
                    }
                )
            if path_detail is not None:
                conflicts.append(
                    {
                        "vehicle_id": vehicle_id,
                        "type": "path_space_time_conflict",
                        "detail": path_detail,
                    }
                )
            # 公式(6) depot_slot_delta 实际使用点：同一补给时间窗内车辆数超过库容量即记冲突。
            # 这个审计结果会阻止把仍有容量冲突的最终轨迹当作成功结果。
            if depot_slot_delta(len(depot_overlaps) + 1, depot_capacity) > 0:
                conflicts.append(
                    {
                        "vehicle_id": vehicle_id,
                        "type": "depot_capacity_conflict",
                        "depot_id": depot_id,
                        "start_time": depot_arrival,
                        "end_time": reload_end,
                        "capacity": depot_capacity,
                        "conflicting_vehicles": [row[2] for row in depot_overlaps],
                    }
                )
            used_launches.add(launch)
            self._reserve_external_path(points, reserved_slots, vehicle_id)
            self._reserve_external_hide_wait(
                path,
                points,
                hide_reservations,
                vehicle_id,
                delay,
            )
            if depot_id and depot_arrival is not None and reload_end is not None:
                depot_reservations.setdefault(depot_id, []).append(
                    (float(depot_arrival), float(reload_end), vehicle_id)
                )
        return {
            "ok": not conflicts,
            "trajectory_count": len(entries),
            "unique_launch_count": len(used_launches),
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        }

    def _try_external_launch_swap_repair(
        self,
        task_id: str,
        subtask_id: str,
        vehicle_id: str,
        paths: List[dict],
        accepted_entries: List[dict],
        assigned_launch_points: Set[str],
        assigned_launch: str,
    ) -> Optional[Tuple[dict, List[dict], str, float, str, dict]]:
        """当前车辆被已占用发射点卡住时，尝试把占用者换到其它候选点。

        这一步只在调度端内部做，不改变模型/车辆消息流程。它解决的是
        A 已经占了 launch_1，B 后来只有 launch_1 可用，而 A 其实也能去
        launch_2 的局部交换问题。
        """
        if not accepted_entries:
            return None
        owner_by_launch = {
            str(entry.get("launch") or ""): idx
            for idx, entry in enumerate(accepted_entries)
            if entry.get("launch")
        }
        current_paths = sorted(paths, key=self._external_candidate_path_sort_key)
        for current_path in current_paths:
            current_launch = self._candidate_path_launch_id(current_path)
            owner_idx = owner_by_launch.get(current_launch)
            if owner_idx is None:
                continue
            current_points = self._normalise_external_path_points(current_path)
            if not current_points:
                continue
            owner_entry = accepted_entries[owner_idx]
            owner_paths = list(owner_entry.get("paths") or [])
            base_entries = [entry for idx, entry in enumerate(accepted_entries) if idx != owner_idx]
            used_base, reserved_base, hide_base = self._external_reservations_from_entries(base_entries)
            owner_assigned_launch = str(owner_entry.get("assigned_launch") or "")
            owner_protected = assigned_launch_points - ({owner_assigned_launch} if owner_assigned_launch else set())
            owner_candidates = sorted(owner_paths, key=self._external_candidate_path_sort_key)
            for owner_path in owner_candidates:
                owner_launch = self._candidate_path_launch_id(owner_path)
                if (
                    not owner_launch
                    or owner_launch == current_launch
                    or owner_launch in used_base
                    or owner_launch in owner_protected
                ):
                    continue
                owner_points_base = self._normalise_external_path_points(owner_path)
                if not owner_points_base:
                    continue
                owner_last_time = max(float(p.get("time", 0.0) or 0.0) for p in owner_points_base)
                owner_delay = 0.0
                owner_delay_limit = self._external_conflict_delay_limit(owner_path, owner_points_base)
                while owner_delay <= owner_delay_limit + 1e-6:
                    owner_shifted = self._delay_before_wait_preserving_arrival(
                        owner_path,
                        owner_points_base,
                        owner_delay,
                    )
                    if owner_shifted is None:
                        break
                    shifted_last_time = max(float(p.get("time", 0.0) or 0.0) for p in owner_shifted)
                    if abs(shifted_last_time - owner_last_time) > 1.0:
                        break
                    if (
                        self._external_hide_wait_conflict_detail(
                            owner_path,
                            owner_shifted,
                            hide_base,
                            owner_delay,
                        )
                        is None
                        and self._external_path_conflict_detail(owner_shifted, reserved_base) is None
                    ):
                        owner_used, owner_reserved, owner_hide = self._external_reservations_from_entries(base_entries)
                        owner_used.add(owner_launch)
                        self._reserve_external_path(owner_shifted, owner_reserved, str(owner_entry.get("vehicle_id") or ""))
                        self._reserve_external_hide_wait(
                            owner_path,
                            owner_shifted,
                            owner_hide,
                            str(owner_entry.get("vehicle_id") or ""),
                            owner_delay,
                        )
                        current_last_time = max(float(p.get("time", 0.0) or 0.0) for p in current_points)
                        current_delay = 0.0
                        current_delay_limit = self._external_conflict_delay_limit(current_path, current_points)
                        while current_delay <= current_delay_limit + 1e-6:
                            current_shifted = self._delay_before_wait_preserving_arrival(
                                current_path,
                                current_points,
                                current_delay,
                            )
                            if current_shifted is None:
                                break
                            shifted_current_last = max(float(p.get("time", 0.0) or 0.0) for p in current_shifted)
                            if abs(shifted_current_last - current_last_time) > 1.0:
                                break
                            current_hide_conflict = self._external_hide_wait_conflict_detail(
                                current_path,
                                current_shifted,
                                owner_hide,
                                current_delay,
                            )
                            current_path_conflict = self._external_path_conflict_detail(
                                current_shifted,
                                owner_reserved,
                            )
                            if current_hide_conflict is None and current_path_conflict is None:
                                owner_entry.update(
                                    {
                                        "path": owner_path,
                                        "points": owner_shifted,
                                        "launch": owner_launch,
                                        "delay": owner_delay,
                                        "source": "conflict_free_swap_owner",
                                    }
                                )
                                trajectory_row = owner_entry.get("trajectory_row")
                                if isinstance(trajectory_row, dict):
                                    trajectory_row["fire_point_id"] = self._external_launch_public_id(owner_launch)
                                    trajectory_row["hide_point"] = self._candidate_path_hide_id(owner_path)
                                    trajectory_row["depot_id"] = owner_path.get("depot_id")
                                    trajectory_row["depot_port"] = owner_path.get("depot_port")
                                    trajectory_row["path_points"] = owner_shifted
                                    trajectory_row["dispatch_selection_source"] = "conflict_free_swap_owner"
                                    trajectory_row["conflict_resolved"] = True
                                    for key in (
                                        "timing_strategy",
                                        "hide_selected",
                                        "wait_seconds",
                                        "fire_time_error_sec",
                                        "timing_total_until_fire_sec",
                                        "launch_startup_mode",
                                        "conflict_retime_advance_sec",
                                        "depot_arrival_time",
                                        "reload_end_time",
                                        "reload_duration_sec",
                                        "post_fire_segments",
                                        "depot_queue_wait_sec",
                                    ):
                                        if key in owner_path:
                                            trajectory_row[key] = owner_path.get(key)
                                detail = {
                                    "moved_vehicle_id": str(owner_entry.get("vehicle_id") or ""),
                                    "from_launch": current_launch,
                                    "to_launch": owner_launch,
                                    "moved_rank": owner_path.get("rank"),
                                    "current_rank": current_path.get("rank"),
                                    "owner_delay_sec": round(owner_delay, 3),
                                    "current_delay_sec": round(current_delay, 3),
                                }
                                self.event_log.log(
                                    "external_dispatch_launch_swap_repair",
                                    task_id=task_id,
                                    subtask_id=subtask_id,
                                    vehicle_id=vehicle_id,
                                    **detail,
                                )
                                debug_append_log(
                                    f"[external-dispatch] launch_swap_repair task={task_id} sid={subtask_id} "
                                    f"vehicle={vehicle_id} moved_vehicle={detail['moved_vehicle_id']} "
                                    f"from={current_launch} to={owner_launch}"
                                )
                                return (
                                    current_path,
                                    current_shifted,
                                    current_launch,
                                    current_delay,
                                    "conflict_free_launch_swap",
                                    detail,
                                )
                            current_delay = self._next_external_conflict_delay(
                                current_delay,
                                current_hide_conflict,
                            )
                    owner_delay = self._next_external_conflict_delay(
                        owner_delay,
                        self._external_hide_wait_conflict_detail(
                            owner_path,
                            owner_shifted,
                            hide_base,
                            owner_delay,
                        ),
                    )
        return None

    def _try_external_conflict_component_repair(
        self,
        *,
        task_id: str,
        subtask_id: str,
        vehicle_id: str,
        paths: List[dict],
        accepted_entries: List[dict],
        assigned_launch_points: Set[str],
        assigned_launch: str,
        hide_conflict: Optional[dict],
        path_conflict: Optional[dict],
    ) -> Optional[Tuple[dict, List[dict], str, float, str, dict]]:
        blocker_ids: Set[str] = set()
        if path_conflict and path_conflict.get("other_vehicle_id"):
            blocker_ids.add(str(path_conflict["other_vehicle_id"]))
        for row in (hide_conflict or {}).get("conflicting_vehicles") or []:
            if isinstance(row, dict) and row.get("vehicle_id"):
                blocker_ids.add(str(row["vehicle_id"]))
        if not blocker_ids:
            return None

        component_entries = [
            entry for entry in accepted_entries if str(entry.get("vehicle_id") or "") in blocker_ids
        ]
        if not component_entries:
            return None
        base_entries = [entry for entry in accepted_entries if entry not in component_entries]
        base_used, base_reserved, base_hide = self._external_reservations_from_entries(base_entries)

        specs: List[dict] = []
        for entry in component_entries:
            specs.append(
                {
                    "subtask_id": str(entry.get("subtask_id") or ""),
                    "vehicle_id": str(entry.get("vehicle_id") or ""),
                    "assigned_launch": str(entry.get("assigned_launch") or ""),
                    "paths": list(entry.get("paths") or []),
                    "entry": entry,
                }
            )
        specs.append(
            {
                "subtask_id": subtask_id,
                "vehicle_id": str(vehicle_id),
                "assigned_launch": assigned_launch,
                "paths": list(paths),
                "entry": None,
            }
        )
        component_assigned = {
            str(spec.get("assigned_launch") or "") for spec in specs if spec.get("assigned_launch")
        }
        protected_launches = assigned_launch_points - component_assigned

        def path_cost(path: dict, delay: float) -> Tuple[float, int, float]:
            try:
                travel = float(path.get("travel_seconds", float("inf")) or float("inf"))
            except Exception:
                travel = float("inf")
            try:
                rank = int(path.get("rank", 999) or 999)
            except Exception:
                rank = 999
            return travel, rank, float(delay)

        def first_feasible_variant(
            path: dict,
            reserved_slots: Dict[int, List[Tuple[float, float, str]]],
            hide_reservations: Dict[str, List[Tuple[float, float, str]]],
        ) -> Optional[Tuple[dict, List[dict], float, float]]:
            base_points = self._normalise_external_path_points(path)
            if not base_points:
                return None
            base_last = max(float(point.get("time", 0.0) or 0.0) for point in base_points)
            delay = 0.0
            delay_limit = self._external_conflict_delay_limit(path, base_points)
            while delay <= delay_limit + 1e-6:
                shifted = self._delay_before_wait_preserving_arrival(path, base_points, delay)
                if shifted is None:
                    return None
                shifted_last = max(float(point.get("time", 0.0) or 0.0) for point in shifted)
                if abs(shifted_last - base_last) > 1.0:
                    return None
                hide_detail = self._external_hide_wait_conflict_detail(
                    path,
                    shifted,
                    hide_reservations,
                    delay,
                )
                path_detail = self._external_path_conflict_detail(shifted, reserved_slots)
                if hide_detail is None and path_detail is None:
                    return path, shifted, delay, delay
                if path_detail is not None and self._path_conflict_fixed_after_wait(path, base_points, path_detail):
                    break
                delay = self._next_external_conflict_delay(delay, hide_detail)
            wait_interval = self._wait_interval_for_external_path(path, base_points)
            wait_slack = (wait_interval[1] - wait_interval[0]) if wait_interval is not None else 0.0
            advance = self.external_conflict_delay_step_sec
            while advance <= wait_slack + 1e-6:
                retimed = self._advance_after_wait_preserving_finish(path, base_points, advance)
                if retimed is None:
                    break
                adjusted_path, adjusted_points = retimed
                hide_detail = self._external_hide_wait_conflict_detail(
                    adjusted_path,
                    adjusted_points,
                    hide_reservations,
                )
                path_detail = self._external_path_conflict_detail(adjusted_points, reserved_slots)
                if hide_detail is None and path_detail is None:
                    return adjusted_path, adjusted_points, 0.0, advance
                advance += self.external_conflict_delay_step_sec
            return None

        full_option_cache: Dict[str, List[dict]] = {}
        for spec in specs:
            ordered = sorted(
                [path for path in spec["paths"] if self._external_candidate_path_usable(path)],
                key=lambda path: (
                    0
                    if self._candidate_path_launch_id(path) == spec.get("assigned_launch")
                    else 1,
                    self._external_candidate_path_sort_key(path),
                ),
            )
            full_option_cache[spec["vehicle_id"]] = ordered

        max_full_options = max((len(options) for options in full_option_cache.values()), default=0)
        first_tier = self.external_component_repair_max_options_per_vehicle
        second_tier = first_tier * self.external_component_repair_second_tier_multiplier
        option_tiers: List[int] = []
        for limit in (first_tier, second_tier, max_full_options):
            if limit > 0 and limit not in option_tiers:
                option_tiers.append(limit)

        best_cost: Optional[Tuple[float, int, float]] = None
        best_choices: Optional[Dict[str, dict]] = None
        best_option_cache: Dict[str, List[dict]] = {}
        best_option_tier_index = 0
        best_option_tier_limit = 0

        def search(
            active_specs: List[dict],
            option_cache: Dict[str, List[dict]],
            index: int,
            used_launches: Set[str],
            reserved_slots: Dict[int, List[Tuple[float, float, str]]],
            hide_reservations: Dict[str, List[Tuple[float, float, str]]],
            choices: Dict[str, dict],
            cost: Tuple[float, int, float],
        ) -> None:
            nonlocal best_cost, best_choices
            if best_cost is not None and cost >= best_cost:
                return
            if index >= len(active_specs):
                best_cost = cost
                best_choices = dict(choices)
                return
            spec = active_specs[index]
            current_vehicle = spec["vehicle_id"]
            for path in option_cache[current_vehicle]:
                launch = self._candidate_path_launch_id(path)
                if not launch or launch in used_launches or launch in protected_launches:
                    continue
                variant = first_feasible_variant(path, reserved_slots, hide_reservations)
                if variant is None:
                    continue
                effective_path, shifted, reservation_delay, timing_shift = variant
                next_reserved = {slot: list(rows) for slot, rows in reserved_slots.items()}
                next_hide = {hide: list(rows) for hide, rows in hide_reservations.items()}
                self._reserve_external_path(shifted, next_reserved, current_vehicle)
                self._reserve_external_hide_wait(
                    effective_path,
                    shifted,
                    next_hide,
                    current_vehicle,
                    reservation_delay,
                )
                item_cost = path_cost(effective_path, timing_shift)
                next_cost = (
                    cost[0] + item_cost[0],
                    cost[1] + item_cost[1],
                    cost[2] + item_cost[2],
                )
                choices[current_vehicle] = {
                    "path": effective_path,
                    "points": shifted,
                    "launch": launch,
                    "delay": reservation_delay,
                    "timing_shift": timing_shift,
                }
                search(
                    active_specs,
                    option_cache,
                    index + 1,
                    used_launches | {launch},
                    next_reserved,
                    next_hide,
                    choices,
                    next_cost,
                )
                choices.pop(current_vehicle, None)

        for tier_index, option_limit in enumerate(option_tiers, start=1):
            option_cache = {
                vehicle_id: options[:option_limit]
                for vehicle_id, options in full_option_cache.items()
            }
            active_specs = sorted(
                specs,
                key=lambda spec: (len(option_cache[spec["vehicle_id"]]), spec["vehicle_id"]),
            )
            best_cost = None
            best_choices = None
            search(active_specs, option_cache, 0, set(base_used), base_reserved, base_hide, {}, (0.0, 0, 0.0))
            if best_choices:
                best_option_cache = option_cache
                best_option_tier_index = tier_index
                best_option_tier_limit = option_limit
                break
        if not best_choices:
            return None

        for spec in specs:
            entry = spec.get("entry")
            if not isinstance(entry, dict):
                continue
            choice = best_choices[spec["vehicle_id"]]
            path = choice["path"]
            entry.update(
                {
                    "path": path,
                    "points": choice["points"],
                    "launch": choice["launch"],
                    "delay": choice["delay"],
                    "source": "conflict_free_component_repair",
                }
            )
            row = entry.get("trajectory_row")
            if isinstance(row, dict):
                row["fire_point_id"] = self._external_launch_public_id(choice["launch"])
                row["hide_point"] = self._candidate_path_hide_id(path)
                row["depot_id"] = path.get("depot_id")
                row["depot_port"] = path.get("depot_port")
                row["path_points"] = choice["points"]
                row["dispatch_selection_source"] = "conflict_free_component_repair"
                row["conflict_resolved"] = True
                for key in (
                    "timing_strategy",
                    "hide_selected",
                    "wait_seconds",
                    "fire_time_error_sec",
                    "timing_total_until_fire_sec",
                    "launch_startup_mode",
                    "conflict_retime_advance_sec",
                    "depot_arrival_time",
                    "reload_end_time",
                    "reload_duration_sec",
                    "post_fire_segments",
                    "depot_queue_wait_sec",
                ):
                    if key in path:
                        row[key] = path.get(key)

        current = best_choices[str(vehicle_id)]
        detail = {
            "component_vehicle_ids": sorted(best_choices),
            "component_size": len(best_choices),
            "option_tier_index": best_option_tier_index,
            "option_tier_limit": best_option_tier_limit,
            "option_tiers": option_tiers,
            "initial_options_per_vehicle": self.external_component_repair_max_options_per_vehicle,
            "option_counts": {
                str(vehicle_id): len(options)
                for vehicle_id, options in best_option_cache.items()
            },
            "full_option_counts": {
                str(vehicle_id): len(options)
                for vehicle_id, options in full_option_cache.items()
            },
            "travel_seconds": round(best_cost[0], 3) if best_cost else None,
            "rank_total": best_cost[1] if best_cost else None,
            "delay_seconds": round(best_cost[2], 3) if best_cost else None,
        }
        self.event_log.log(
            "external_dispatch_conflict_component_repair",
            task_id=task_id,
            subtask_id=subtask_id,
            vehicle_id=vehicle_id,
            **detail,
        )
        return (
            current["path"],
            current["points"],
            current["launch"],
            current["delay"],
            "conflict_free_component_repair",
            detail,
        )

    # 对所有已选车辆做一次全局发射点唯一性分配。
    # 这里解决的是“谁占哪个发射点”问题，还没有进入路径时空冲突检查。
    def _global_external_launch_assignment(
        self,
        selected: List[Tuple[str, str]],
        path_sets: Dict[str, Tuple[List[dict], List[dict]]],
    ) -> Dict[str, str]:
        launch_options: Dict[str, List[str]] = {}
        for sid, _vehicle_id in selected:
            raw_paths, usable_paths = path_sets.get(sid, ([], []))
            ordered = sorted(
                usable_paths,
                key=lambda path: (
                    int(path.get("rank", 999) or 999),
                    self._external_candidate_path_sort_key(path),
                ),
            )
            options: List[str] = []
            for path in ordered:
                launch_id = self._candidate_path_launch_id(path)
                if launch_id and launch_id not in options and self._normalise_external_path_points(path):
                    options.append(launch_id)
            launch_options[sid] = options

        launch_owner: Dict[str, str] = {}
        assignment: Dict[str, str] = {}

        def assign(sid: str, visited: Set[str]) -> bool:
            for launch_id in launch_options.get(sid, []):
                if launch_id in visited:
                    continue
                visited.add(launch_id)
                owner = launch_owner.get(launch_id)
                if owner is None or assign(owner, visited):
                    launch_owner[launch_id] = sid
                    assignment[sid] = launch_id
                    return True
            return False

        ordered_sids = sorted(
            (sid for sid, _vehicle_id in selected),
            key=lambda sid: (len(launch_options.get(sid, [])), sid),
        )
        for sid in ordered_sids:
            assign(sid, set())
        return assignment
    # 路径冲突检测：
    # 把轨迹点按时间分桶，再按空间距离阈值判断是否与已保留轨迹发生同时空占用。
    def _external_path_conflicts(
        self,
        points: List[dict],
        reserved: Dict[int, List[Tuple[float, float, str]]],
    ) -> bool:
        return self._external_path_conflict_detail(points, reserved) is not None

    def _external_path_conflict_detail(
        self,
        points: List[dict],
        reserved: Dict[int, List[Tuple[float, float, str]]],
    ) -> Optional[dict]:
        if self.external_conflict_distance_m <= 0:
            return None
        limit_sq = self.external_conflict_distance_m * self.external_conflict_distance_m
        for point in self._external_conflict_points(points):
            xy = self._point_conflict_xy(point)
            if xy is None:
                continue
            # 把轨迹点按时间切到离散 slot，用近似时空占用来做快速冲突判断。
            point_time = float(point.get("time", 0.0) or 0.0)
            slot = int(round(point_time / self.external_conflict_slot_sec))
            for other_x, other_y, other_vehicle in reserved.get(slot, []):
                dx = xy[0] - other_x
                dy = xy[1] - other_y
                if dx * dx + dy * dy <= limit_sq:
                    return {
                        "conflict_type": "path_space_time",
                        "other_vehicle_id": str(other_vehicle),
                        "time": round(point_time, 3),
                        "slot": slot,
                        "lon": point.get("lon"),
                        "lat": point.get("lat"),
                        "x": point.get("x"),
                        "y": point.get("y"),
                        "distance_m": round(math.sqrt(dx * dx + dy * dy), 3),
                        "threshold_m": self.external_conflict_distance_m,
                    }
        return None

    # 路径一旦被接纳，就把其占用写入 reserved，供后续车辆避让。
    def _reserve_external_path(
        self,
        points: List[dict],
        reserved: Dict[int, List[Tuple[float, float, str]]],
        vehicle_id: str,
    ) -> None:
        for point in self._external_conflict_points(points):
            xy = self._point_conflict_xy(point)
            if xy is None:
                continue
            slot = int(round(float(point.get("time", 0.0) or 0.0) / self.external_conflict_slot_sec))
            reserved.setdefault(slot, []).append((xy[0], xy[1], str(vehicle_id)))

    @staticmethod
    # 冲突检测时只保留真正“在运动”的轨迹点，去掉原地不动的重复点，减少误判和计算量。
    def _external_conflict_points(points: List[dict]) -> List[dict]:
        if len(points) <= 1:
            return []
        moving: List[dict] = []
        last_xy: Optional[Tuple[float, float]] = None
        for point in points[:-1]:
            xy = SchedulerApp._point_conflict_xy(point)
            if xy is not None and last_xy is not None:
                dx = xy[0] - last_xy[0]
                dy = xy[1] - last_xy[1]
                if dx * dx + dy * dy <= 1e-6:
                    continue
            moving.append(point)
            if xy is not None:
                last_xy = xy
        trimmed = moving
        final_xy = SchedulerApp._point_conflict_xy(points[-1])
        if final_xy is None:
            return trimmed
        while trimmed:
            xy = SchedulerApp._point_conflict_xy(trimmed[-1])
            if xy is None:
                break
            dx = xy[0] - final_xy[0]
            dy = xy[1] - final_xy[1]
            if dx * dx + dy * dy > 1e-6:
                break
            trimmed.pop()
        return trimmed
    # 调度端外部主流程：
    # 等选车结果 -> 等车辆候选路径 -> 全局分配发射点 -> 做冲突检查/延迟/兜底 -> 生成最终轨迹。
    def _try_resolve_external_dispatch_for_task(self, task_id: str) -> bool:
        resolve_started = time.perf_counter()
        task_id = str(task_id or "")
        if (
            not task_id
            or task_id in self.dispatched_trajectory_bundle_tasks
            or task_id in self.prelaunch_dispatch_states
        ):
            return False
        selected = self._selected_vehicle_rows_for_task(task_id)
        if not selected:
            return False
        expected = self._task_subtasks_total(task_id)
        # 先等模型把这一批需要执行任务的车辆都选出来。
        if expected > 0 and len(selected) < expected:
            self.event_log.log(
                "external_dispatch_waiting_selected_vehicles",
                task_id=task_id,
                selected_count=len(selected),
                expected_count=expected,
            )
            self._timing_log(
                "external_dispatch_waiting_selected_vehicles",
                started_at=resolve_started,
                task_id=task_id,
                selected_count=len(selected),
                expected_count=expected,
            )
            return False
        missing = [
            vehicle_id
            for _, vehicle_id in selected
            if not self._candidate_payload_for_vehicle(vehicle_id)
        ]
        # 再等这些车辆把自己的候选路径都规划并回传回来。
        if missing:
            self.event_log.log(
                "external_dispatch_waiting_candidate_paths",
                task_id=task_id,
                missing_vehicle_ids=missing,
                selected_count=len(selected),
            )
            self._timing_log(
                "external_dispatch_waiting_candidate_paths",
                started_at=resolve_started,
                task_id=task_id,
                selected_count=len(selected),
                missing_count=len(missing),
            )
            return False

        depot_selection_missing: List[str] = []
        selected_depot_pool = self._selected_depot_pool_for_task(task_id)
        selected_depot_pool_incomplete = (
            not self.two_stage_depot_enabled
            and len(selected_depot_pool) < self.selected_depot_expected_count
        )
        for subtask_id, vehicle_id in selected:
            payload = self._candidate_payload_for_vehicle(vehicle_id) or {}
            has_depot_candidates = any(
                isinstance(path, dict) and bool(path.get("depot_candidates"))
                for path in (payload.get("paths") or [])
            )
            if (
                has_depot_candidates
                and subtask_id not in self.external_depot_selection
                and selected_depot_pool_incomplete
            ):
                depot_selection_missing.append(subtask_id)
        depot_selection_timed_out = False
        if depot_selection_missing:
            now = time.monotonic()
            deadline = self.selected_depot_wait_deadlines.get(task_id)
            if deadline is None:
                deadline = now + self.selected_depot_wait_timeout_sec
                self.selected_depot_wait_deadlines[task_id] = deadline
                if self.selected_depot_wait_timeout_sec > 0.0 and task_id not in self.selected_depot_wait_timers:
                    timer = threading.Timer(
                        self.selected_depot_wait_timeout_sec + 0.05,
                        self._retry_external_dispatch_after_depot_wait,
                        args=(task_id,),
                    )
                    timer.daemon = True
                    self.selected_depot_wait_timers[task_id] = timer
                    timer.start()
            remaining = max(0.0, deadline - now)
            depot_selection_timed_out = now >= deadline
            if not depot_selection_timed_out:
                self.event_log.log(
                    "external_dispatch_waiting_selected_depots",
                    task_id=task_id,
                    missing_subtask_ids=depot_selection_missing,
                    selected_count=len(selected_depot_pool),
                    expected_count=self.selected_depot_expected_count,
                    wait_timeout_sec=self.selected_depot_wait_timeout_sec,
                    wait_remaining_sec=round(remaining, 3),
                )
                return False
            self.event_log.log(
                "external_dispatch_selected_depots_timeout",
                task_id=task_id,
                missing_subtask_ids=depot_selection_missing,
                selected_count=len(selected_depot_pool),
                expected_count=self.selected_depot_expected_count,
                wait_timeout_sec=self.selected_depot_wait_timeout_sec,
                fallback="use_received_depot_pool_or_omit_when_empty",
            )

        path_sets: Dict[str, Tuple[List[dict], List[dict]]] = {}
        for sid, vehicle_id in selected:
            payload = self._candidate_payload_for_vehicle(vehicle_id) or {}
            payload_paths = payload.get("paths") or []
            if not isinstance(payload_paths, list):
                payload_paths = []
            raw_paths = []
            for path in payload_paths:
                if not isinstance(path, dict):
                    continue
                recovered = self._recover_hide_unavailable_candidate(path)
                if depot_selection_timed_out and sid in depot_selection_missing:
                    recovered = dict(recovered)
                    recovered.pop("depot_candidates", None)
                    recovered["depot_selection_timeout"] = True
                raw_paths.append(recovered)
            # 这里只保留单车层面可用的候选路径，后面再做多车冲突消解。
            usable_paths = [path for path in raw_paths if self._external_candidate_path_usable(path)]
            path_sets[sid] = (raw_paths, usable_paths)
        # 先从全局上尽量把发射点分开，避免多车天然争用同一个目标点。
        launch_assignment = self._global_external_launch_assignment(selected, path_sets)
        self.event_log.log(
            "external_dispatch_global_launch_assignment",
            task_id=task_id,
            selected_count=len(selected),
            assigned_count=len(launch_assignment),
            unmatched_subtask_ids=[sid for sid, _vehicle_id in selected if sid not in launch_assignment],
        )
        debug_append_log(
            f"[external-dispatch] global_launch_assignment task={task_id} "
            f"assigned={len(launch_assignment)}/{len(selected)}"
        )

        assigned_launch_points = set(launch_assignment.values())
        used_launch_points: Set[str] = set()
        reserved_slots: Dict[int, List[Tuple[float, float, str]]] = {}
        hide_wait_reservations: Dict[str, List[Tuple[float, float, str]]] = {}
        trajectories: List[dict] = []
        unresolved_vehicle_ids: List[str] = []
        unresolved_reasons: Dict[str, str] = {}
        resolution_started = time.perf_counter()
        conflict_checks = 0
        conflict_hits = 0
        hide_wait_conflict_hits = 0
        delay_attempts = 0
        fixed_suffix_pruned = 0
        fallback_count = 0
        accepted_entries: List[dict] = []

        for sid, vehicle_id in selected:
            raw_paths, paths = path_sets.get(sid, ([], []))
            raw_path_count = len(raw_paths)
            if raw_path_count and not paths:
                self.event_log.log(
                    "external_dispatch_no_usable_candidate_paths",
                    task_id=task_id,
                    subtask_id=sid,
                    vehicle_id=vehicle_id,
                    raw_path_count=raw_path_count,
                )
                debug_append_log(
                    f"[external-dispatch] no_usable_candidate_paths task={task_id} sid={sid} "
                    f"vehicle={vehicle_id} raw_paths={raw_path_count}"
                )
            paths.sort(key=self._external_candidate_path_sort_key)
            assigned_launch = launch_assignment.get(sid, "")
            if assigned_launch:
                paths.sort(
                    key=lambda path: (
                        0 if self._candidate_path_launch_id(path) == assigned_launch else 1,
                        self._external_candidate_path_sort_key(path),
                    )
                )
            protected_launches = assigned_launch_points - ({assigned_launch} if assigned_launch else set())

            chosen: Optional[dict] = None
            chosen_points: List[dict] = []
            chosen_launch = ""
            chosen_delay = 0.0
            chosen_source = "conflict_free"
            saw_launch_conflict = False
            saw_hide_conflict = False
            saw_path_conflict = False
            last_hide_conflict_detail: Optional[dict] = None
            last_path_conflict_detail: Optional[dict] = None

            def evaluate_candidate_paths(
                candidate_paths: List[dict],
                *,
                source: str,
            ) -> Optional[Tuple[dict, List[dict], str, float, str]]:
                nonlocal conflict_checks
                nonlocal conflict_hits
                nonlocal hide_wait_conflict_hits
                nonlocal delay_attempts
                nonlocal fixed_suffix_pruned
                nonlocal saw_launch_conflict
                nonlocal saw_hide_conflict
                nonlocal saw_path_conflict
                nonlocal last_hide_conflict_detail
                nonlocal last_path_conflict_detail

                best: Optional[Tuple[Tuple[float, tuple], dict, List[dict], str, float, str]] = None
                for path in candidate_paths:
                    launch_id = self._candidate_path_launch_id(path)
                    if (
                        not launch_id
                        or launch_id in used_launch_points
                        or launch_id in protected_launches
                    ):
                        if launch_id:
                            saw_launch_conflict = True
                        continue
                    base_points = self._normalise_external_path_points(path)
                    if not base_points:
                        continue
                    base_last_time = max(float(p.get("time", 0.0) or 0.0) for p in base_points)
                    delay = 0.0
                    delay_limit = self._external_conflict_delay_limit(path, base_points)
                    while delay <= delay_limit + 1e-6:
                        delay_attempts += 1
                        shifted = self._delay_before_wait_preserving_arrival(path, base_points, delay)
                        if shifted is None:
                            break
                        last_time = max(float(p.get("time", 0.0) or 0.0) for p in shifted)
                        if abs(last_time - base_last_time) > 1.0:
                            break
                        conflict_checks += 1
                        hide_conflict_detail = self._external_hide_wait_conflict_detail(
                            path,
                            shifted,
                            hide_wait_reservations,
                            delay,
                        )
                        path_conflict_detail = self._external_path_conflict_detail(shifted, reserved_slots)
                        hide_conflict = hide_conflict_detail is not None
                        path_conflict = path_conflict_detail is not None
                        has_conflict = hide_conflict or path_conflict
                        if not has_conflict:
                            key = (delay, self._external_candidate_path_sort_key(path))
                            if best is None or key < best[0]:
                                best = (key, path, shifted, launch_id, delay, source)
                            break
                        if hide_conflict:
                            hide_wait_conflict_hits += 1
                            saw_hide_conflict = True
                            last_hide_conflict_detail = hide_conflict_detail
                        if path_conflict:
                            saw_path_conflict = True
                            last_path_conflict_detail = path_conflict_detail
                        conflict_hits += 1
                        if (
                            path_conflict_detail is not None
                            and self._path_conflict_fixed_after_wait(path, base_points, path_conflict_detail)
                        ):
                            fixed_suffix_pruned += 1
                            break
                        delay = self._next_external_conflict_delay(delay, hide_conflict_detail)
                if best is None:
                    return None
                _key, path, shifted, launch_id, delay, selected_source = best
                return path, shifted, launch_id, delay, selected_source

            primary_paths = (
                [path for path in paths if self._candidate_path_launch_id(path) == assigned_launch]
                if assigned_launch
                else list(paths)
            )
            primary_result = evaluate_candidate_paths(primary_paths, source="conflict_free")
            if primary_result is not None:
                chosen, chosen_points, chosen_launch, chosen_delay, chosen_source = primary_result

            if chosen is None and paths:
                # The global launch matching is a good first guess, but it can
                # over-constrain a late vehicle if its assigned launch has hide
                # or path timing conflicts. Re-try against launches that are not
                # actually used yet, while still preserving final launch
                # uniqueness.
                retry_paths = sorted(
                    paths,
                    key=self._external_candidate_path_sort_key,
                )
                retry_result = evaluate_candidate_paths(retry_paths, source="conflict_free_reassigned")
                if retry_result is not None:
                    chosen, chosen_points, chosen_launch, chosen_delay, chosen_source = retry_result
                    self.event_log.log(
                        "external_dispatch_reassigned_candidate_path",
                        task_id=task_id,
                        subtask_id=sid,
                        vehicle_id=vehicle_id,
                        original_launch=assigned_launch,
                        launch_node=chosen_launch,
                        rank=chosen.get("rank"),
                        timing_strategy=chosen.get("timing_strategy"),
                        hide_selected=bool(chosen.get("hide_selected", False)),
                        delay_sec=round(chosen_delay, 3),
                    )
                    debug_append_log(
                        f"[external-dispatch] reassigned_candidate task={task_id} sid={sid} "
                        f"vehicle={vehicle_id} original={assigned_launch} launch={chosen_launch} "
                        f"rank={chosen.get('rank')} strategy={chosen.get('timing_strategy')} "
                        f"delay_sec={chosen_delay:.3f}"
                    )

            if chosen is None and paths:
                repair = self._try_external_launch_swap_repair(
                    task_id=task_id,
                    subtask_id=sid,
                    vehicle_id=vehicle_id,
                    paths=paths,
                    accepted_entries=accepted_entries,
                    assigned_launch_points=assigned_launch_points,
                    assigned_launch=assigned_launch,
                )
                if repair is not None:
                    chosen, chosen_points, chosen_launch, chosen_delay, chosen_source, _repair_detail = repair
                    used_launch_points, reserved_slots, hide_wait_reservations = self._external_reservations_from_entries(
                        accepted_entries
                    )

            if chosen is None and paths:
                repair = self._try_external_conflict_component_repair(
                    task_id=task_id,
                    subtask_id=sid,
                    vehicle_id=vehicle_id,
                    paths=paths,
                    accepted_entries=accepted_entries,
                    assigned_launch_points=assigned_launch_points,
                    assigned_launch=assigned_launch,
                    hide_conflict=last_hide_conflict_detail,
                    path_conflict=last_path_conflict_detail,
                )
                if repair is not None:
                    chosen, chosen_points, chosen_launch, chosen_delay, chosen_source, _repair_detail = repair
                    used_launch_points, reserved_slots, hide_wait_reservations = self._external_reservations_from_entries(
                        accepted_entries
                    )

            if chosen is None and self.external_conflict_fallback_to_rank1:
                protected_launches = assigned_launch_points - ({assigned_launch} if assigned_launch else set())
                fallback_path, fallback_points, fallback_launch = self._fallback_external_candidate_path(
                    raw_paths,
                    used_launch_points | protected_launches,
                    hide_wait_reservations,
                    reserved_slots,
                )
                if fallback_path is not None and fallback_points and fallback_launch:
                    chosen = fallback_path
                    chosen_points = fallback_points
                    chosen_launch = fallback_launch
                    chosen_delay = 0.0
                    chosen_source = "conflict_free_direct_fallback"
                    fallback_count += 1
                    self.event_log.log(
                        "external_dispatch_fallback_candidate_path",
                        task_id=task_id,
                        subtask_id=sid,
                        vehicle_id=vehicle_id,
                        launch_node=chosen_launch,
                        rank=chosen.get("rank"),
                        timing_strategy=chosen.get("timing_strategy"),
                        hide_selected=bool(chosen.get("hide_selected", False)),
                    )
                    debug_append_log(
                        f"[external-dispatch] fallback_candidate task={task_id} sid={sid} "
                        f"vehicle={vehicle_id} launch={chosen_launch} rank={chosen.get('rank')} "
                        f"strategy={chosen.get('timing_strategy')}"
                    )

            if chosen is None and self.external_force_emit_bundle_when_unresolved:
                protected_launches = assigned_launch_points - ({assigned_launch} if assigned_launch else set())
                fallback_path, fallback_points, fallback_launch = self._fallback_external_candidate_path(
                    raw_paths,
                    used_launch_points | protected_launches,
                    hide_wait_reservations,
                    reserved_slots,
                )
                if fallback_path is not None and fallback_points and fallback_launch:
                    chosen = fallback_path
                    chosen_points = fallback_points
                    chosen_launch = fallback_launch
                    chosen_delay = 0.0
                    chosen_source = "conflict_free_force_fallback"
                    fallback_count += 1
                    self.event_log.log(
                        "external_dispatch_force_continue_rank1",
                        task_id=task_id,
                        subtask_id=sid,
                        vehicle_id=vehicle_id,
                        launch_node=chosen_launch,
                        rank=chosen.get("rank"),
                        path_points=len(chosen_points),
                    )
                    debug_append_log(
                        f"[external-dispatch] force_continue_rank1 task={task_id} sid={sid} "
                        f"vehicle={vehicle_id} launch={chosen_launch} rank={chosen.get('rank')}"
                    )

            if chosen is None:
                if not raw_paths:
                    unresolved_reason = "no_candidate_launch"
                elif not paths:
                    reject_counts: Dict[str, int] = {}
                    for raw_path in raw_paths:
                        reason = self._external_candidate_reject_reason(raw_path)
                        reject_counts[reason] = reject_counts.get(reason, 0) + 1
                    unresolved_reason = max(
                        reject_counts,
                        key=lambda reason: (reject_counts[reason], reason),
                        default="no_reachable_launch",
                    )
                elif saw_hide_conflict:
                    unresolved_reason = "hide_time_conflict"
                elif saw_path_conflict:
                    unresolved_reason = "path_space_time_conflict"
                elif saw_launch_conflict or sid not in launch_assignment:
                    unresolved_reason = "launch_conflict"
                else:
                    unresolved_reason = "no_unique_launch_or_conflict_free_path"
                self.event_log.log(
                    "external_dispatch_unresolved",
                    task_id=task_id,
                    subtask_id=sid,
                    vehicle_id=vehicle_id,
                    reason=unresolved_reason,
                    path_conflict=last_path_conflict_detail,
                    hide_conflict=last_hide_conflict_detail,
                )
                debug_append_log(
                    f"[external-dispatch] unresolved task={task_id} sid={sid} "
                    f"vehicle={vehicle_id} reason={unresolved_reason} "
                    f"path_conflict={last_path_conflict_detail} "
                    f"hide_conflict={last_hide_conflict_detail}"
                )
                unresolved_vehicle_ids.append(str(vehicle_id))
                unresolved_reasons[str(vehicle_id)] = unresolved_reason
                continue

            if not chosen_source.startswith("conflict_free"):
                self.event_log.log(
                    "external_dispatch_conflict_failed",
                    task_id=task_id,
                    subtask_id=sid,
                    vehicle_id=vehicle_id,
                    launch_node=chosen_launch,
                    selection_source=chosen_source,
                    assigned_launch=assigned_launch,
                    launch_conflict=saw_launch_conflict,
                    path_conflict=last_path_conflict_detail,
                    hide_conflict=last_hide_conflict_detail,
                )
                debug_append_log(
                    f"[external-dispatch] conflict_failed task={task_id} sid={sid} "
                    f"vehicle={vehicle_id} launch={chosen_launch} source={chosen_source} "
                    f"assigned_launch={assigned_launch} launch_conflict={saw_launch_conflict} "
                    f"path_conflict={last_path_conflict_detail} "
                    f"hide_conflict={last_hide_conflict_detail}"
                )

            used_launch_points.add(chosen_launch)
            if assigned_launch and chosen_launch != assigned_launch:
                assigned_launch_points.discard(assigned_launch)
            self._reserve_external_path(chosen_points, reserved_slots, vehicle_id)
            self._reserve_external_hide_wait(
                chosen,
                chosen_points,
                hide_wait_reservations,
                vehicle_id,
                chosen_delay,
            )
            row = {
                "vehicle_id": str(vehicle_id),
                "fire_point_id": self._external_launch_public_id(chosen_launch),
                "hide_point": self._candidate_path_hide_id(chosen),
                "depot_id": chosen.get("depot_id"),
                "depot_port": chosen.get("depot_port"),
                "path_points": chosen_points,
                "dispatch_selection_source": chosen_source,
                "conflict_resolved": chosen_source.startswith("conflict_free"),
            }
            for key in (
                "timing_strategy",
                "hide_selected",
                "wait_seconds",
                "fire_time_error_sec",
                "timing_total_until_fire_sec",
                "launch_startup_mode",
                "conflict_retime_advance_sec",
                "depot_arrival_time",
                "reload_end_time",
                "reload_duration_sec",
                "post_fire_segments",
                "depot_queue_wait_sec",
            ):
                if key in chosen:
                    row[key] = chosen.get(key)
            try:
                row["port"] = int(float(vehicle_id))
            except Exception:
                pass
            trajectories.append(row)
            accepted_entries.append(
                {
                    "subtask_id": sid,
                    "vehicle_id": str(vehicle_id),
                    "path": chosen,
                    "points": chosen_points,
                    "launch": chosen_launch,
                    "assigned_launch": assigned_launch,
                    "paths": list(paths),
                    "trajectory_row": row,
                    "delay": chosen_delay,
                    "source": chosen_source,
                }
            )
            self.event_log.log(
                "external_dispatch_path_selected",
                task_id=task_id,
                subtask_id=sid,
                vehicle_id=vehicle_id,
                launch_node=chosen_launch,
                rank=chosen.get("rank"),
                score_total=chosen.get("score_total"),
                hide_selected=bool(chosen.get("hide_selected", False)),
                timing_strategy=chosen.get("timing_strategy"),
                selection_source=chosen_source,
                delay_sec=round(chosen_delay, 3),
                path_points=len(chosen_points),
            )
            debug_append_log(
                f"[external-dispatch] path_selected task={task_id} sid={sid} vehicle={vehicle_id} "
                f"launch={chosen_launch} rank={chosen.get('rank')} score={chosen.get('score_total')} "
                f"hide_selected={bool(chosen.get('hide_selected', False))} "
                f"strategy={chosen.get('timing_strategy')} source={chosen_source} "
                f"delay_sec={chosen_delay:.3f}"
            )

        if self.two_stage_depot_enabled:
            resolution_sec = time.perf_counter() - resolution_started
            conflict_audit = self._audit_external_dispatch_entries(accepted_entries)
            if not conflict_audit["ok"]:
                self.event_log.log(
                    "external_dispatch_conflict_audit_failed",
                    task_id=task_id,
                    phase="pre_launch",
                    **conflict_audit,
                )
                return False
            self.prelaunch_dispatch_states[task_id] = {
                "task_id": task_id,
                "selected": list(selected),
                "trajectories": trajectories,
                "accepted_entries": accepted_entries,
                "unresolved_vehicle_ids": unresolved_vehicle_ids,
                "unresolved_reasons": unresolved_reasons,
                "prelaunch_conflict_audit": conflict_audit,
                "status": "prelaunch_resolved",
            }
            self.event_log.log(
                "external_prelaunch_dispatch_resolved",
                task_id=task_id,
                selected_count=len(selected),
                trajectory_count=len(trajectories),
                unresolved_count=len(unresolved_vehicle_ids),
                elapsed_sec=round(resolution_sec, 6),
            )
            self._dispatch_depot_vehicle_score_contexts(task_id)
            return True

        depot_suffix_summary = self._resolve_selected_depot_suffixes(task_id, accepted_entries)
        resolution_sec = time.perf_counter() - resolution_started
        unresolved_reason_summary: Dict[str, int] = {}
        for reason in unresolved_reasons.values():
            unresolved_reason_summary[reason] = unresolved_reason_summary.get(reason, 0) + 1
        conflict_audit = self._audit_external_dispatch_entries(accepted_entries)
        if not conflict_audit["ok"]:
            self.event_log.log(
                "external_dispatch_conflict_audit_failed",
                task_id=task_id,
                **conflict_audit,
            )
            debug_append_log(
                f"[external-dispatch] audit_failed task={task_id} "
                f"conflicts={conflict_audit['conflict_count']}"
            )
            return False
        #保存并发送最终轨迹
        bundle = {
            "task_id": task_id,
            "trajectories": trajectories,
            "resolved_count": len(trajectories),
            "unresolved_count": len(unresolved_vehicle_ids),
            "unresolved_vehicle_ids": unresolved_vehicle_ids,
            "unresolved_reasons": unresolved_reasons,
            "unresolved_reason_summary": unresolved_reason_summary,
            "depot_suffix_summary": depot_suffix_summary,
            "conflict_audit": conflict_audit,
        }
        self.resolved_dispatch_trajectory_bundles[task_id] = bundle
        timer = self.selected_depot_wait_timers.pop(task_id, None)
        if timer is not None:
            timer.cancel()
        self.selected_depot_wait_deadlines.pop(task_id, None)
        self.event_log.log(
            "external_dispatch_resolved",
            task_id=task_id,
            trajectory_count=len(trajectories),
            selected_count=len(selected),
            resolved_count=len(trajectories),
            unresolved_count=len(unresolved_vehicle_ids),
            unresolved_reason_summary=unresolved_reason_summary,
            depot_suffix_summary=depot_suffix_summary,
        )
        self.event_log.log(
            "external_conflict_resolution_timing",
            task_id=task_id,
            selected_count=len(selected),
            trajectory_count=len(trajectories),
            conflict_checks=conflict_checks,
            conflict_hits=conflict_hits,
            hide_wait_conflict_hits=hide_wait_conflict_hits,
            delay_attempts=delay_attempts,
            fixed_suffix_pruned=fixed_suffix_pruned,
            fallback_count=fallback_count,
            elapsed_sec=round(resolution_sec, 6),
        )
        self._timing_log(
            "external_dispatch_resolved",
            elapsed_sec=resolution_sec,
            task_id=task_id,
            selected_count=len(selected),
            trajectory_count=len(trajectories),
            unresolved_count=len(unresolved_vehicle_ids),
            conflict_checks=conflict_checks,
            conflict_hits=conflict_hits,
            hide_wait_conflict_hits=hide_wait_conflict_hits,
            delay_attempts=delay_attempts,
            fixed_suffix_pruned=fixed_suffix_pruned,
            fallback_count=fallback_count,
        )
        debug_append_log(
            f"[external-dispatch] resolved task={task_id} trajectories={len(trajectories)} "
            f"conflict_checks={conflict_checks} conflict_hits={conflict_hits} "
            f"hide_wait_conflict_hits={hide_wait_conflict_hits} "
            f"delay_attempts={delay_attempts} fixed_suffix_pruned={fixed_suffix_pruned} "
            f"fallback_count={fallback_count} "
            f"resolution_sec={resolution_sec:.6f}"
        )
        self._maybe_send_dispatch_trajectory_bundle(task_id)
        return True

    def _dispatch_depot_vehicle_score_contexts(self, task_id: str) -> None:
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state:
            return
        entries = list(state.get("accepted_entries") or [])
        depots: List[dict] = []
        for row in self._compact_depot_points():
            if int(row.get("capacity", 0) or 0) <= 0:
                continue
            try:
                int(float(row.get("depot_port")))
            except (TypeError, ValueError):
                continue
            depots.append(row)
        if not depots:
            self.event_log.log(
                "depot_assignment_skipped",
                task_id=task_id,
                reason="no_addressable_depot_points_with_capacity",
            )
            self._finalize_prelaunch_only(task_id, "no_addressable_depot_points_with_capacity")
            return
        assignments: List[dict] = []
        for entry in entries:
            launch_node = str(entry.get("launch") or "")
            launch_row = dict(self.external_launch_point_rows_by_node.get(launch_node, {}))
            points = list(entry.get("points") or [])
            subtask = self.subtasks.get(str(entry.get("subtask_id") or ""))
            assignment_fire_time = (
                subtask.sim_fire_time
                if subtask is not None and subtask.sim_fire_time is not None
                else self._task_sim_fire_time(task_id)
            )
            if assignment_fire_time is None:
                assignment_fire_time = points[-1].get("time") if points else None
            assignments.append(
                {
                    "subtask_id": entry.get("subtask_id"),
                    "vehicle_id": entry.get("vehicle_id"),
                    "launch_node": launch_node,
                    "fire_point_id": self._external_launch_public_id(launch_node),
                    "launch_name": launch_row.get("name"),
                    # 任务书中的车型/弹种资源匹配：调度把任务所需弹种一并发给贮备库。
                    # 只有贮备库声明 supported_ammo_types 时才过滤，不声明则保持当前结果不变。
                    "required_ammo_type": subtask.ammo_type if subtask is not None else None,
                    "lon": launch_row.get("lon"),
                    "lat": launch_row.get("lat"),
                    "fire_time": assignment_fire_time,
                }
            )
        state["depot_rows"] = depots
        state["expected_depot_ids"] = [str(row.get("depot_id")) for row in depots]
        state["status"] = "waiting_depot_scores"
        for depot in depots:
            try:
                depot_port = int(float(depot.get("depot_port")))
            except (TypeError, ValueError):
                self.event_log.log(
                    "depot_vehicle_score_context_send_failed",
                    task_id=task_id,
                    depot_id=depot.get("depot_id"),
                    error="missing_depot_port",
                )
                continue
            payload = {
                "msg_type": MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
                "data": {
                    # 调度把射前已消解的“车辆-发射点”关系发给每个贮备库。
                    # 贮备库只计算本库到各发射点的分数，不在库端决定最终分配。
                    "task_id": task_id,
                    "scheduler_id": self.node_id,
                    "reply_host": self.advertise_host,
                    "reply_port": self.listen_port,
                    **depot,
                    "score_weights": {
                        "distance_weight_a": self.depot_distance_weight_a,
                        "distance_rank_weight_b": self.depot_distance_rank_weight_b,
                        "load_ratio_weight_c": self.depot_load_ratio_weight_c,
                        "distance_metric": self.depot_distance_metric,
                    },
                    "assignments": assignments,
                },
            }
            self._send_raw_json_callback(
                (self.depot_direct_host, depot_port),
                payload,
                "depot_vehicle_score_context_sent",
                task_id=task_id,
                depot_id=depot.get("depot_id"),
                vehicle_count=len(assignments),
                capacity=depot.get("capacity"),
            )

    def _on_depot_vehicle_score_result(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        depot_id = str(payload.get("depot_id") or payload.get("depot_port") or "")
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state or not depot_id:
            return
        self.depot_vehicle_score_results[task_id][depot_id] = payload
        expected = set(state.get("expected_depot_ids") or [])
        received = set(self.depot_vehicle_score_results[task_id])
        self.event_log.log(
            "depot_vehicle_score_result_received",
            task_id=task_id,
            depot_id=depot_id,
            score_count=len(payload.get("scores") or []),
            received_count=len(received & expected),
            expected_count=len(expected),
        )
        if expected and not expected.issubset(received):
            return
        self._assign_depots_and_request_post_fire_paths(task_id)

    def _assign_depots_and_request_post_fire_paths(self, task_id: str) -> None:
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state or state.get("status") not in {"waiting_depot_scores", "prelaunch_resolved"}:
            return
        entries = list(state.get("accepted_entries") or [])
        vehicle_ids = [str(entry.get("vehicle_id")) for entry in entries]
        # 对应贮备库全局分配规则：汇总所有库返回的分数矩阵。
        # 每轮选择当前最小分，删除该车辆列并扣减库容量，直到 48 辆车全部分配。
        result = greedy_capacity_assignment(
            self.depot_vehicle_score_results.get(task_id, {}).values(),
            vehicle_ids,
            load_ratio_weight=self.depot_load_ratio_weight_c,
        )
        unassigned = list(result.get("unassigned_vehicle_ids") or [])
        if unassigned:
            state["status"] = "depot_capacity_or_score_shortage"
            state["unassigned_depot_vehicle_ids"] = unassigned
            self.event_log.log(
                "depot_global_assignment_incomplete",
                task_id=task_id,
                selected_count=len(vehicle_ids),
                assigned_count=result.get("assigned_count"),
                unassigned_vehicle_ids=unassigned,
                reason="capacity_sum_or_score_matrix_incomplete",
            )
            return
        entry_by_vehicle = {str(entry.get("vehicle_id")): entry for entry in entries}
        depot_by_id = {
            str(row.get("depot_id")): dict(row)
            for row in (state.get("depot_rows") or [])
        }
        state["status"] = "waiting_post_fire_paths"
        for assignment in result.get("assignments") or []:
            vehicle_id = str(assignment.get("vehicle_id"))
            entry = entry_by_vehicle[vehicle_id]
            depot = depot_by_id.get(str(assignment.get("depot_id")), {})
            merged_assignment = {**dict(assignment), **depot}
            self.depot_vehicle_assignments[task_id][vehicle_id] = merged_assignment
            vehicle = self.vehicles.get(vehicle_id)
            if vehicle is None or vehicle.endpoint is None:
                state["status"] = "vehicle_endpoint_missing"
                self.event_log.log(
                    "vehicle_depot_assignment_send_failed",
                    task_id=task_id,
                    vehicle_id=vehicle_id,
                    reason="vehicle_endpoint_missing",
                )
                return
            points = list(entry.get("points") or [])
            subtask = self.subtasks.get(str(entry.get("subtask_id") or ""))
            assignment_fire_time = (
                subtask.sim_fire_time
                if subtask is not None and subtask.sim_fire_time is not None
                else self._task_sim_fire_time(task_id)
            )
            if assignment_fire_time is None:
                assignment_fire_time = points[-1].get("time") if points else None
            payload = {
                "msg_type": MSG_VEHICLE_DEPOT_ASSIGNMENT,
                "data": {
                    "task_id": task_id,
                    "subtask_id": entry.get("subtask_id"),
                    "vehicle_id": vehicle_id,
                    "launch_node": entry.get("launch"),
                    "fire_point_id": self._external_launch_public_id(str(entry.get("launch") or "")),
                    "fire_time": assignment_fire_time,
                    "reply_host": self.advertise_host,
                    "reply_port": self.listen_port,
                    "depot": merged_assignment,
                },
            }
            self._send_raw_json_callback(
                vehicle.endpoint,
                payload,
                "vehicle_depot_assignment_sent",
                task_id=task_id,
                vehicle_id=vehicle_id,
                depot_id=merged_assignment.get("depot_id"),
            )
        self.event_log.log(
            "depot_global_assignment_completed",
            task_id=task_id,
            assigned_count=len(result.get("assignments") or []),
            depot_count=len(self.depot_vehicle_score_results.get(task_id, {})),
        )

    def _on_vehicle_post_fire_path_result(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        vehicle_id = str(payload.get("vehicle_id") or payload.get("port") or "")
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state or not vehicle_id:
            return
        if vehicle_id not in self.depot_vehicle_assignments.get(task_id, {}):
            self.event_log.log(
                "post_fire_path_result_ignored",
                task_id=task_id,
                vehicle_id=vehicle_id,
                reason="vehicle_has_no_depot_assignment",
            )
            return
        self.post_fire_path_results[task_id][vehicle_id] = payload
        expected = set(self.depot_vehicle_assignments.get(task_id, {}))
        received = set(self.post_fire_path_results.get(task_id, {}))
        self.event_log.log(
            "vehicle_post_fire_path_result_received",
            task_id=task_id,
            vehicle_id=vehicle_id,
            path_points=len(payload.get("path_points") or []),
            received_count=len(received & expected),
            expected_count=len(expected),
        )
        if expected.issubset(received):
            self._resolve_post_fire_paths_and_finalize(task_id)

    def _resolve_post_fire_paths_and_finalize(self, task_id: str) -> None:
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state or state.get("status") != "waiting_post_fire_paths":
            return
        entries = list(state.get("accepted_entries") or [])
        reserved_slots: Dict[int, List[Tuple[float, float, str]]] = {}
        # 射后消解以前先把射前轨迹写入占用表。
        # 这样“发射后去贮备库/回起点”的轨迹不会穿过仍在执行射前任务的车辆。
        for entry in entries:
            self._reserve_external_path(
                list(entry.get("points") or []),
                reserved_slots,
                str(entry.get("vehicle_id") or ""),
            )
        omitted: List[str] = []
        for entry in sorted(entries, key=lambda row: str(row.get("vehicle_id") or "")):
            vehicle_id = str(entry.get("vehicle_id") or "")
            post = self.post_fire_path_results.get(task_id, {}).get(vehicle_id, {})
            post_points = self._normalise_external_path_points(post)
            pre_points = [dict(point) for point in (entry.get("points") or [])]
            if not pre_points or len(post_points) < 2:
                omitted.append(vehicle_id)
                continue
            suffix = [dict(point) for point in post_points[1:]]
            accepted_suffix: Optional[List[dict]] = None
            delay = 0.0
            # 任务书“贮备库任务冲突消解”：射后段只通过等待延迟做低扰动消解。
            # 不改车辆已规划路径形状，只调整出发时间，避免破坏射前已经验证的无冲突集合。
            while delay <= self.external_post_fire_max_delay_sec + 1e-6:
                shifted = [dict(point) for point in suffix]
                if delay > 0.0:
                    for point in shifted:
                        point["time"] = round(float(point.get("time", 0.0) or 0.0) + delay, 3)
                if self._external_path_conflict_detail(shifted, reserved_slots) is None:
                    accepted_suffix = shifted
                    break
                delay += self.external_conflict_delay_step_sec
            if accepted_suffix is None:
                omitted.append(vehicle_id)
                continue
            combined = list(pre_points)
            if delay > 0.0:
                wait_point = dict(pre_points[-1])
                wait_point["time"] = round(float(wait_point.get("time", 0.0) or 0.0) + delay, 3)
                combined.append(wait_point)
            combined.extend(accepted_suffix)
            self._reserve_external_path(accepted_suffix, reserved_slots, vehicle_id)
            entry["points"] = combined
            entry["path"] = {**dict(entry.get("path") or {}), "path_points": combined}
            row = entry.get("trajectory_row")
            assignment = self.depot_vehicle_assignments[task_id][vehicle_id]
            if isinstance(row, dict):
                row["path_points"] = combined
                row["depot_id"] = assignment.get("depot_id")
                row["depot_node"] = assignment.get("depot_node")
                row["depot_port"] = assignment.get("depot_port")
                row["depot_capacity"] = assignment.get("capacity")
                row["depot_assignment_score"] = assignment.get("score_total")
                row["post_fire_delay_sec"] = round(delay, 3)
                for key in (
                    "depot_arrival_time",
                    "reload_end_time",
                    "reload_duration_sec",
                    "finish_time",
                    "segments",
                ):
                    if key in post:
                        row["post_fire_segments" if key == "segments" else key] = post.get(key)
        if omitted:
            state["status"] = "post_fire_conflict_unresolved"
            self.event_log.log(
                "post_fire_conflict_resolution_incomplete",
                task_id=task_id,
                omitted_vehicle_ids=omitted,
            )
            return
        conflict_audit = self._audit_external_dispatch_entries(entries)
        if not conflict_audit["ok"]:
            state["status"] = "post_fire_conflict_audit_failed"
            self.event_log.log(
                "post_fire_conflict_audit_failed",
                task_id=task_id,
                **conflict_audit,
            )
            return
        trajectories = list(state.get("trajectories") or [])
        bundle = {
            "task_id": task_id,
            "trajectories": trajectories,
            "resolved_count": len(trajectories),
            "unresolved_count": len(state.get("unresolved_vehicle_ids") or []),
            "unresolved_vehicle_ids": list(state.get("unresolved_vehicle_ids") or []),
            "unresolved_reasons": dict(state.get("unresolved_reasons") or {}),
            "depot_assignment_count": len(self.depot_vehicle_assignments.get(task_id, {})),
            "prelaunch_conflict_audit": state.get("prelaunch_conflict_audit"),
            "conflict_audit": conflict_audit,
        }
        state["status"] = "completed"
        self.resolved_dispatch_trajectory_bundles[task_id] = bundle
        self.event_log.log(
            "two_stage_dispatch_completed",
            task_id=task_id,
            trajectory_count=len(trajectories),
            depot_assignment_count=bundle["depot_assignment_count"],
        )
        self._maybe_send_dispatch_trajectory_bundle(task_id)

    def _finalize_prelaunch_only(self, task_id: str, reason: str) -> None:
        state = self.prelaunch_dispatch_states.get(task_id)
        if not state:
            return
        trajectories = list(state.get("trajectories") or [])
        self.resolved_dispatch_trajectory_bundles[task_id] = {
            "task_id": task_id,
            "trajectories": trajectories,
            "resolved_count": len(trajectories),
            "unresolved_count": len(state.get("unresolved_vehicle_ids") or []),
            "unresolved_vehicle_ids": list(state.get("unresolved_vehicle_ids") or []),
            "unresolved_reasons": dict(state.get("unresolved_reasons") or {}),
            "depot_assignment_count": 0,
            "depot_omitted_reason": reason,
            "conflict_audit": state.get("prelaunch_conflict_audit"),
        }
        state["status"] = "completed_prelaunch_only"
        self._maybe_send_dispatch_trajectory_bundle(task_id)

    # 把调度后的最终轨迹逐车拆开发给模型，同时在本地保留总 bundle。
    def _try_resolve_external_dispatches(self) -> None:
        task_ids = {
            self.subtasks[sid].task_id
            for sid in self.external_vehicle_selection
            if sid in self.subtasks
        }
        for task_id in sorted(task_ids):
            self._try_resolve_external_dispatch_for_task(task_id)

    def _retry_external_dispatch_after_depot_wait(self, task_id: str) -> None:
        with self._lock:
            self.selected_depot_wait_timers.pop(task_id, None)
            if task_id in self.dispatched_trajectory_bundle_tasks:
                return
            self._try_resolve_external_dispatch_for_task(task_id)

    def _send_dispatch_trajectory_bundle_to_model(self, task_id: str) -> None:
        if not self.model_callback_enabled:
            return
        started_at = time.perf_counter()
        bundle = self._build_dispatch_trajectory_bundle(task_id)
        built_at = time.perf_counter()
        try:
            out_path = Path("result") / "latest_dispatch_trajectory_bundle.json"
            out_path.write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.event_log.log(
                "dispatch_trajectory_bundle_save_failed",
                task_id=task_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        trajectories = bundle.get("trajectories") or []
        for row in trajectories:
            single = {
                "task_id": task_id,
                "vehicle_id": row.get("vehicle_id"),
                "port": row.get("port"),
                "trajectories": [row],
                "msg_type": MSG_DISPATCH_TRAJECTORY_BUNDLE,
            }
            try:
                self._send_raw_json_callback(
                    (self.model_callback_host, self.model_callback_port),
                    single,
                    "dispatch_trajectory_bundle_sent",
                    task_id=task_id,
                    trajectory_count=1,
                    vehicle_id=row.get("vehicle_id"),
                )
            except Exception as exc:
                self.event_log.log(
                    "dispatch_trajectory_bundle_send_failed",
                    task_id=task_id,
                    vehicle_id=row.get("vehicle_id"),
                    target_addr=f"{self.model_callback_host}:{self.model_callback_port}",
                    error=f"{type(exc).__name__}: {exc}",
                )
        self._timing_log(
            "dispatch_trajectory_bundle_to_model",
            elapsed_sec=time.perf_counter() - started_at,
            task_id=task_id,
            trajectory_count=len(trajectories),
            build_sec=round(built_at - started_at, 6),
            send_sec=round(time.perf_counter() - built_at, 6),
        )

    def _maybe_send_dispatch_trajectory_bundle(self, task_id: str) -> None:
        if not self.model_callback_enabled or not task_id or task_id in self.dispatched_trajectory_bundle_tasks:
            return
        if task_id in self.resolved_dispatch_trajectory_bundles:
            self.dispatched_trajectory_bundle_tasks.add(task_id)
            self._send_dispatch_trajectory_bundle_to_model(task_id)
            return
        self.event_log.log("dispatch_trajectory_bundle_waiting", task_id=task_id, reason="not_resolved")

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
            name="scheduler-lane-prewarm",
        )
        self._lane_graph_prewarm_thread.start()

    def _prewarm_lane_graph(self, prewarm_cfg: dict) -> None:
        include_hide = bool(prewarm_cfg.get("include_hide_points", False))
        include_return_home = bool(prewarm_cfg.get("include_return_home", True))
        stop_on_work = bool(prewarm_cfg.get("stop_on_work", True))
        max_pairs = int(prewarm_cfg.get("max_pairs", 2000) or 0)
        yield_every = max(1, int(prewarm_cfg.get("yield_every", 25) or 25))
        start_nodes = {v.home_node for v in self.vehicles.values()}
        start_nodes.update(self.points.depots)
        if include_hide:
            start_nodes.update(self.points.hide_points)
        goal_nodes = set(self.points.launch_points)
        goal_nodes.update(self.points.depots)
        if include_return_home:
            goal_nodes.update(v.home_node for v in self.vehicles.values())
        if include_hide:
            goal_nodes.update(self.points.hide_points)
        speed_caps = sorted(
            {
                round(float(v.speed_mps), 3)
                for v in self.vehicles.values()
                if float(v.speed_mps) > 0
            }
        ) or [None]
        started = time.perf_counter()
        unique_starts = [str(x) for x in dict.fromkeys(sorted(start_nodes)) if str(x)]
        unique_goals = [str(x) for x in dict.fromkeys(sorted(goal_nodes)) if str(x)]
        attempted = 0
        before_queries = self.lane_graph.route_queries
        before_hits = self.lane_graph.cache_hits
        before_misses = self.lane_graph.cache_misses
        stopped_reason = ""
        for speed_cap in speed_caps:
            for start_node in unique_starts:
                for goal_node in unique_goals:
                    if start_node == goal_node:
                        continue
                    if stop_on_work and (self.queued_subtasks or self.pending_subtasks):
                        stopped_reason = "task_work_available"
                        break
                    if max_pairs > 0 and attempted >= max_pairs:
                        stopped_reason = "max_pairs"
                        break
                    attempted += 1
                    self.lane_graph.route_between_nodes(start_node, goal_node, speed_cap_mps=speed_cap)
                    if attempted % yield_every == 0:
                        time.sleep(0.001)
                if stopped_reason:
                    break
            if stopped_reason:
                break
        stats = {
            "start_nodes": len(unique_starts),
            "goal_nodes": len(unique_goals),
            "speed_caps": len(speed_caps),
            "attempted_pairs": attempted,
            "route_queries_added": self.lane_graph.route_queries - before_queries,
            "cache_hits_added": self.lane_graph.cache_hits - before_hits,
            "cache_misses_added": self.lane_graph.cache_misses - before_misses,
        }
        if stopped_reason:
            stats["stopped_reason"] = stopped_reason
        stats["wall_sec"] = round(time.perf_counter() - started, 3)
        self.event_log.log("lane_graph_prewarm", **stats)
        self._push_recent_event("lane_graph_prewarm", **stats)










    def _on_vehicle_score_result(self, env: Envelope) -> None:
        payload = env.payload or {}
        rows = payload.get("vehicle_scores")
        if not isinstance(rows, list):
            score = payload.get("vehicle_score")
            rows = [score] if isinstance(score, dict) else []
        simplified_score = payload.get("score_total")
        score_count = len([score for score in rows if isinstance(score, dict)])
        self.metrics["vehicle_score_result"] = self.metrics.get("vehicle_score_result", 0) + score_count
        self.event_log.log(
            "vehicle_score_result_received",
            vehicle_id=payload.get("vehicle_id") or env.sender,
            task_id=payload.get("task_id"),
            subtask_id=payload.get("subtask_id"),
            score_count=score_count,
            simplified_score=simplified_score,
        )
#接收选车结果
    def _on_selected_vehicle_result(self, env: Envelope) -> None:
        payload = env.payload or {}
        if isinstance(payload.get("data"), list):
            rows = payload.get("data")
        elif isinstance(payload.get("data"), dict):
            row = dict(payload.get("data") or {})
            for key in ("task_id", "subtask_id", "selection_source"):
                if row.get(key) is None and payload.get(key) is not None:
                    row[key] = payload.get(key)
            self._apply_selected_vehicle_payload(row, payload)
            self._try_resolve_external_dispatches()
            return
        elif isinstance(payload.get("data"), (str, int, float)):
            sid_queue = [sid for sid in self._selected_vehicle_default_subtasks() if sid not in self.external_vehicle_selection]
            vehicle_id = str(payload.get("data") or "")
            row = {"vehicle_id": vehicle_id, "port": vehicle_id}
            if sid_queue:
                row["subtask_id"] = sid_queue[0]
            self._apply_selected_vehicle_payload(row, payload)
            self._try_resolve_external_dispatches()
            return
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = payload.get("selections") or payload.get("vehicles")
        if isinstance(rows, list):
            sid_queue = self._selected_vehicle_default_subtasks()
            for idx, row in enumerate(rows):
                if isinstance(row, dict):
                    if not row.get("subtask_id") and idx < len(sid_queue):
                        row = dict(row)
                        row["subtask_id"] = sid_queue[idx]
                    self._apply_selected_vehicle_payload(row, payload)
            self._try_resolve_external_dispatches()
            return

        self._apply_selected_vehicle_payload(payload, payload)
        self._try_resolve_external_dispatches()

    def _on_selected_depot_result(self, env: Envelope) -> None:
        payload = env.payload or {}
        data = payload.get("data")
        if isinstance(data, str):
            text = data.strip()
            if text:
                try:
                    data = json.loads(text)
                except Exception:
                    data = {
                        "depot_id": text,
                        "subtask_id": payload.get("subtask_id"),
                        "vehicle_id": payload.get("vehicle_id") or payload.get("port"),
                        "task_id": payload.get("task_id"),
                    }
                if not isinstance(data, (dict, list)):
                    data = {
                        "depot_id": text,
                        "subtask_id": payload.get("subtask_id"),
                        "vehicle_id": payload.get("vehicle_id") or payload.get("port"),
                        "task_id": payload.get("task_id"),
                    }
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = [data]
        elif isinstance(payload.get("selections"), list):
            rows = payload["selections"]
        else:
            rows = [payload]
        vehicle_to_subtask = {
            str(candidates[0]): subtask_id
            for subtask_id, candidates in self.external_vehicle_selection.items()
            if candidates
        }
        applied = 0
        pool_applied = 0
        for row in rows:
            if not isinstance(row, dict):
                self.event_log.log(
                    "selected_depot_result_ignored",
                    reason="row_not_object",
                    row_type=type(row).__name__,
                )
                continue
            subtask_id = str(row.get("subtask_id") or "")
            vehicle_id = str(row.get("vehicle_id") or row.get("port") or "")
            if not subtask_id and vehicle_id:
                subtask_id = vehicle_to_subtask.get(vehicle_id, "")
            depot_id = str(
                row.get("depot_id")
                or row.get("selected_depot")
                or row.get("depot_node")
                or row.get("depot_port")
                or ""
            )
            depot_id = self._canonical_depot_selection_id(depot_id)
            if depot_id and not subtask_id:
                task_ids = self._task_ids_for_depot_selection(payload, row)
                for task_id in task_ids:
                    self.external_depot_selection_by_task[task_id].add(depot_id)
                    pool_applied += 1
                    self.event_log.log(
                        "selected_depot_pool_result_received",
                        task_id=task_id,
                        depot_id=depot_id,
                        selection_scope="task_pool",
                    )
                if task_ids:
                    continue
            if not subtask_id or not depot_id:
                self.event_log.log(
                    "selected_depot_result_ignored",
                    reason="missing_subtask_or_depot",
                    subtask_id=subtask_id,
                    vehicle_id=vehicle_id,
                    depot_id=depot_id,
                    payload_keys=sorted(row.keys()),
                )
                continue
            self.external_depot_selection[subtask_id] = depot_id
            applied += 1
            self.event_log.log(
                "selected_depot_result_received",
                task_id=row.get("task_id") or payload.get("task_id"),
                subtask_id=subtask_id,
                vehicle_id=vehicle_id,
                depot_id=depot_id,
            )
        if applied or pool_applied:
            self._try_resolve_external_dispatches()
        else:
            self.event_log.log(
                "selected_depot_result_no_rows_applied",
                payload_keys=sorted(payload.keys()),
                row_count=len(rows),
            )

    def _selected_vehicle_default_subtasks(self) -> List[str]:
        candidates = [
            sid
            for sid in list(self.pending_subtasks) + list(self.queued_subtasks)
            if sid in self.subtasks
            and self.subtasks[sid].status in {"PENDING", "ASSIGNED", "PLANNING"}
            and not self.subtasks[sid].is_redundant
        ]
        if not candidates:
            candidates = [
                sid
                for sid, st in self.subtasks.items()
                if st.status not in {"DONE", "FAILED"} and not st.is_redundant
            ]
        return sorted(candidates, key=lambda sid: (self.subtasks[sid].fire_time, sid))
#选车结果和车辆绑定
    def _apply_selected_vehicle_payload(self, payload: dict, root_payload: dict) -> None:
        subtask_id = str(payload.get("subtask_id") or "")
        if not subtask_id:
            self.event_log.log(
                "selected_vehicle_result_ignored",
                reason="missing_subtask_id",
                task_id=payload.get("task_id") or root_payload.get("task_id"),
                vehicle_id=payload.get("vehicle_id"),
            )
            return
        candidates: List[str] = []
        primary = str(payload.get("vehicle_id") or "")
        if primary:
            candidates.append(primary)
        for vid in payload.get("candidate_vehicle_ids") or []:
            text = str(vid or "")
            if text and text not in candidates:
                candidates.append(text)
        for row in payload.get("candidate_scores") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("vehicle_id") or "")
            if text and text not in candidates:
                candidates.append(text)
        if not candidates:
            self.event_log.log(
                "selected_vehicle_result_ignored",
                reason="empty_candidates",
                task_id=payload.get("task_id") or root_payload.get("task_id"),
                subtask_id=subtask_id,
            )
            return
        self.external_vehicle_selection[subtask_id] = candidates
        st = self.subtasks.get(subtask_id)
        launch_node = payload.get("launch_node") or payload.get("fire_point_id") or payload.get("launch_point")
        if st and launch_node:
            st.preferred_launch_point = str(launch_node)
        self.metrics["selected_vehicle_result"] = self.metrics.get("selected_vehicle_result", 0) + 1
        self.event_log.log(
            "selected_vehicle_result_received",
            task_id=payload.get("task_id") or root_payload.get("task_id"),
            subtask_id=subtask_id,
            vehicle_id=primary,
            launch_node=launch_node,
            candidate_count=len(candidates),
            selection_source=payload.get("selection_source"),
        )
        debug_append_log(
            f"[selected-vehicle] sid={subtask_id} primary={primary or '-'} "
            f"launch={launch_node or '-'} candidates={len(candidates)}"
        )






    def start(self) -> None:
        self.transport.start()
        self.dashboard.start()
        self._running = True
        self._scheduler_thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._scheduler_thread.start()
        self._heartbeat_thread.start()
        self._start_lane_graph_prewarm()

    def stop(self) -> None:
        self._running = False
        self.dashboard.stop()
        self.transport.stop()
#调度消息入口，接收任务包，选车结果，发射点，隐蔽点，车辆点信息，候选点路径
    def on_message(self, env: Envelope, addr: MessageAddress) -> Optional[dict]:
        self._capture_message("recv", env.msg_type, env.payload or {}, addr=addr, sender=env.sender, target=env.target)
        if env.msg_type == "HEARTBEAT":
            self._queue_heartbeat(env, addr)
            return None
        with self._lock:
            if env.msg_type == "TASK_PACKAGE":
                return self._on_task_package(env, addr)
            elif env.msg_type == MSG_SELECTED_VEHICLE_RESULT:
                self._on_selected_vehicle_result(env)
            elif env.msg_type == MSG_SELECTED_DEPOT_RESULT:
                self._on_selected_depot_result(env)
            elif env.msg_type in {MSG_FA_SHE_DIAN, MSG_YIN_BI_DIAN, MSG_VEHICLE_DIAN, MSG_DEPOT_DIAN, MSG_ZHU_BEI_DIAN, MSG_ZHU_BEI_KU_DIAN}:
                self._on_dian_context(env)
            elif env.msg_type == MSG_VEHICLE_CANDIDATE_PATH_RESULT:
                self._on_vehicle_candidate_path_result(env)
            elif env.msg_type == MSG_VEHICLE_SCORE_RESULT:
                self._on_vehicle_score_result(env)
            elif env.msg_type == MSG_DEPOT_VEHICLE_SCORE_RESULT:
                self._on_depot_vehicle_score_result(env)
            elif env.msg_type == MSG_VEHICLE_POST_FIRE_PATH_RESULT:
                self._on_vehicle_post_fire_path_result(env)
        return None

    def _queue_heartbeat(self, env: Envelope, addr: MessageAddress) -> None:
        payload = dict(env.payload or {})
        pose = payload.get("realtime_pose") if isinstance(payload.get("realtime_pose"), dict) else {}
        if payload.get("schema") == "realtime_pose_v1":
            pose = payload
        if pose:
            payload.setdefault("vehicle_id", pose.get("vehicle_id"))
            payload.setdefault("active_task_id", pose.get("task_id"))
            payload.setdefault("active_subtask_id", pose.get("subtask_id"))
            payload.setdefault("active_phase", pose.get("phase"))
            payload.setdefault("speed_mps", pose.get("speed_mps"))
        vid = str(payload.get("vehicle_id") or env.sender or "")
        if not vid:
            return
        with self._heartbeat_queue_lock:
            self._pending_heartbeats[vid] = (payload, addr)
        self.metrics["heartbeats_queued"] = self.metrics.get("heartbeats_queued", 0) + 1

    def _drain_heartbeats_locked(self) -> None:
        with self._heartbeat_queue_lock:
            pending = list(self._pending_heartbeats.values())
            self._pending_heartbeats.clear()
        if not pending:
            return
        for payload, addr in pending:
            self._apply_heartbeat_payload_locked(payload, addr)
        self.metrics["heartbeats_applied"] = self.metrics.get("heartbeats_applied", 0) + len(pending)

    def _apply_heartbeat_payload_locked(self, payload: dict, addr: MessageAddress) -> None:
        vid = payload["vehicle_id"]
        vehicle = self.vehicles.get(vid)
        if vehicle is None and not self.allow_dynamic_vehicle_registration:
            self.event_log.log("heartbeat_ignored_unknown_vehicle", vehicle_id=vid)
            return
        advertised_host = payload.get("advertise_host") or payload.get("listen_host")
        advertised_port = payload.get("advertise_port") or payload.get("listen_port")
        resolved_endpoint: Optional[Tuple[str, int]] = vehicle.endpoint if vehicle else None
        if advertised_host and advertised_port:
            host = str(advertised_host)
            if host in {"0.0.0.0", ""}:
                host = addr[0] if addr else "127.0.0.1"
            resolved_endpoint = (host, int(advertised_port))
        elif addr is not None and self.transport_type == "udp":
            resolved_endpoint = addr
        if vehicle is None:
            vehicle = VehicleRuntime(
                vehicle_id=vid,
                endpoint=resolved_endpoint,
                home_node=payload.get("home_node", payload.get("current_node", "")),
                ammo_types=set(payload.get("ammo_types", [])),
            )
            self.vehicles[vid] = vehicle
        if resolved_endpoint is not None:
            vehicle.endpoint = resolved_endpoint
        vehicle.current_node = payload.get("current_node", vehicle.current_node)
        reported_status = payload.get("status", vehicle.status)
        vehicle.speed_mps = float(payload.get("speed_mps", vehicle.speed_mps))
        vehicle.realtime_scale = float(payload.get("realtime_scale", vehicle.realtime_scale))
        if "kinematics" in payload and isinstance(payload["kinematics"], dict):
            vehicle.kinematics = dict(payload["kinematics"])
        reported_active_sid = payload.get("active_subtask_id")
        local_active_sid = vehicle.active_subtask_id
        local_active_st = self.subtasks.get(local_active_sid) if local_active_sid else None
        keep_local_active = bool(
            local_active_sid
            and local_active_st
            and local_active_st.status not in {"DONE", "FAILED"}
            and reported_active_sid
            and reported_active_sid != local_active_sid
        )
        if keep_local_active:
            self.event_log.log(
                "heartbeat_active_subtask_ignored",
                vehicle_id=vid,
                local_active_subtask_id=local_active_sid,
                reported_active_subtask_id=reported_active_sid,
                reported_status=reported_status,
            )
        elif reported_active_sid:
            vehicle.active_subtask_id = reported_active_sid
        elif not vehicle.active_subtask_id:
            vehicle.active_subtask_id = None
        if not (
            keep_local_active
            and str(reported_status) in {"RELOADING", "WAIT_DEPOT_QUEUE", "TO_DEPOT", "ENROUTE_DEPOT"}
        ):
            vehicle.status = reported_status
        vehicle.last_seen = utc_now_iso()
        
#调度接收任务包
    def _on_task_package(self, env: Envelope, addr: MessageAddress) -> Optional[dict]:
        started_at = time.perf_counter()
        #检验任务包时间
        self._normalize_task_package_times(env)
        validation = self.task_validator.validate(env.payload, now_utc().timestamp())
        for warning in validation.warnings:
            self.event_log.log("task_validation_warning", task_id=env.payload.get("task_id"), detail=warning)
        if not validation.ok:
            for error in validation.errors:
                self.event_log.log("task_validation_error", task_id=env.payload.get("task_id"), detail=error)
            print(
                f"[Scheduler] reject task={env.payload.get('task_id')} "
                f"errors={len(validation.errors)}"
            )
            return self._send_task_package_receipt(
                env,
                addr,
                accepted=False,
                task_id=str((env.payload or {}).get("task_id") or ""),
                launches=0,
                redundant_launches=0,
                subtasks_created=0,
                errors=validation.errors,
                warnings=validation.warnings,
            )
#把外部json转为内部TaskPackage
        task = TaskPackage.from_dict(env.payload)
        self.metrics["tasks_received"] += 1
        launches = list(task.launches)
        real_count = len(launches)
        redundant_count = int(math.ceil(real_count * self.redundancy_ratio)) if self.redundancy_enabled and real_count > 0 else 0
        created_count = 0
        if redundant_count > 0:
            for i in range(redundant_count):
                base = dict(launches[i % real_count])
                base["redundant"] = True
                base["redundant_for_index"] = i % real_count
                launches.append(base)
        for idx, launch in enumerate(launches):
            #按launches数量生成子任务
            sid = f"{task.task_id}_s{idx:03d}"
            if sid in self.subtasks:
                continue
            is_redundant = bool(launch.get("redundant", False))
            redundant_for = None
            if is_redundant:
                source_idx = int(launch.get("redundant_for_index", 0) or 0)
                redundant_for = f"{task.task_id}_s{source_idx:03d}"
            st = SubTask(
                subtask_id=sid,
                task_id=task.task_id,
                ammo_type=launch["ammo_type"],
                fire_time=launch["fire_time"],
                sim_fire_time=launch.get("sim_fire_time"),
                is_redundant=is_redundant,
                redundant_for_subtask_id=redundant_for,
            )
            self.subtasks[sid] = st
            self.queued_subtasks.append(sid)
            self.metrics["subtasks_created"] += 1
            created_count += 1
        print(
            f"[Scheduler] received task={task.task_id}, launches={real_count}, "
            f"redundant={redundant_count}"
        )
        self.event_log.log(
            "task_received",
            task_id=task.task_id,
            launches=real_count,
            redundant_launches=redundant_count,
        )
        self._push_recent_event(
            "task_received",
            task_id=task.task_id,
            launches=real_count,
            redundant_launches=redundant_count,
        )
        #收到任务后向模型发送点位请求
        self._request_dian_context_from_model(task.task_id)
        self._timing_log(
            "task_package_processed",
            started_at=started_at,
            task_id=task.task_id,
            launches=real_count,
            redundant_launches=redundant_count,
            subtasks_created=created_count,
        )
        return self._send_task_package_receipt(
            env,
            addr,
            accepted=True,
            task_id=task.task_id,
            launches=real_count,
            redundant_launches=redundant_count,
            subtasks_created=created_count,
            errors=[],
            warnings=validation.warnings,
        )

    @staticmethod
    def _iso_to_sim_seconds(value: Any) -> Optional[float]:
        if value in {None, ""}:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return round((parse_iso_time(str(value)) - SIM_TIME_EPOCH).total_seconds(), 3)
        except Exception:
            return None

    @staticmethod
    def _sim_seconds_to_iso(value: Any) -> str:
        try:
            seconds = float(value)
        except Exception:
            seconds = 0.0
        return (SIM_TIME_EPOCH + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    #任务包内容规范
    def _normalize_task_package_times(env: Envelope) -> None:
        payload = env.payload or {}
        if "launches" not in payload:
            task_count = payload.get("task_count", payload.get("task_num", payload.get("num", payload.get("count", payload.get("launch_count")))))
            fire_time = payload.get("fire_time")
            sim_time = payload.get("sim_time", payload.get("current_time"))
            if task_count is not None and fire_time is not None and sim_time is not None:
                count = max(0, int(task_count))
                payload.setdefault("task_id", f"task_{int(float(sim_time))}_{int(float(fire_time))}")
                payload["sim_time"] = float(sim_time)
                payload["dispatch_time"] = SchedulerApp._sim_seconds_to_iso(sim_time)
                payload["launches"] = [
                    {
                        "ammo_type": str(payload.get("ammo_type") or "HE"),
                        "fire_time": SchedulerApp._sim_seconds_to_iso(fire_time),
                        "sim_fire_time": float(fire_time),
                    }
                    for _ in range(count)
                ]
        launches = payload.get("launches")
        if not isinstance(launches, list):
            return
        sim_time = payload.get("sim_time", payload.get("current_time"))
        if sim_time is not None:
            try:
                payload["sim_time"] = float(sim_time)
                payload["dispatch_time"] = SchedulerApp._sim_seconds_to_iso(payload["sim_time"])
            except Exception:
                pass
        dispatch_time = str(payload.get("dispatch_time") or payload.get("created_at") or env.created_at or SchedulerApp._sim_seconds_to_iso(0.0))
        try:
            dispatch_dt = parse_iso_time(dispatch_time)
        except Exception:
            dispatch_time = SchedulerApp._sim_seconds_to_iso(0.0)
            dispatch_dt = parse_iso_time(dispatch_time)
        payload.setdefault("dispatch_time", dispatch_time)
        for launch in launches:
            if not isinstance(launch, dict):
                continue
            if isinstance(launch.get("fire_time"), (int, float)):
                launch["sim_fire_time"] = float(launch["fire_time"])
                launch["fire_time"] = SchedulerApp._sim_seconds_to_iso(launch["sim_fire_time"])
            if launch.get("fire_time") is None and launch.get("fire_after_sec") is not None:
                try:
                    launch["fire_time"] = (
                        dispatch_dt + timedelta(seconds=float(launch["fire_after_sec"]))
                    ).isoformat()
                    launch["sim_fire_time"] = SchedulerApp._iso_to_sim_seconds(launch["fire_time"])
                except Exception:
                    continue
            elif launch.get("fire_time") is not None and launch.get("sim_fire_time") is None:
                launch["sim_fire_time"] = SchedulerApp._iso_to_sim_seconds(launch.get("fire_time"))
#发送任务回执
    def _send_task_package_receipt(
        self,
        request_env: Envelope,
        addr: MessageAddress,
        *,
        accepted: bool,
        task_id: str,
        launches: int,
        redundant_launches: int,
        subtasks_created: int,
        errors: List[str],
        warnings: List[str],
    ) -> Optional[dict]:
        payload = dict(correlation_fields(request_env.payload or {}))
        payload.update(
            {
                "schema": "task_package_receipt_v1",
                "task_id": task_id,
                "accepted": accepted,
                "status": "ACCEPTED" if accepted else "REJECTED",
                "launches": launches,
                "redundant_launches": redundant_launches,
                "subtasks_created": subtasks_created,
                "errors": list(errors),
                "warnings": list(warnings),
            }
        )
        sim_time = (request_env.payload or {}).get("sim_time", (request_env.payload or {}).get("current_time"))
        if sim_time is not None:
            payload["sim_time"] = float(sim_time)
        if (request_env.payload or {}).get("_raw_tcp_json"):
            payload.setdefault("msg_type", "TASK_PACKAGE_RECEIPT")
            payload.setdefault("request_type", request_env.msg_type)
            reply_addr = self._raw_json_reply_addr(request_env.payload or {}, addr)
            reply_host = reply_addr[0] if reply_addr else ""
            reply_port = reply_addr[1] if reply_addr else 0 #打印ip和端口是否对上
            if reply_host and reply_port:
                try:
                    self._send_raw_json_callback(
                        (str(reply_host), int(reply_port)),
                        payload,
                        "raw_task_package_receipt_callback_sent",
                        task_id=task_id,
                        accepted=accepted,
                    )
                except Exception as exc:
                    self.event_log.log(
                        "raw_task_package_receipt_callback_prepare_failed",
                        task_id=task_id,
                        accepted=accepted,
                        reply_host=str(reply_host),
                        reply_port=str(reply_port),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            self.event_log.log( #能否输出
                "raw_task_package_receipt_sent",
                task_id=task_id,
                accepted=accepted,
                errors=len(errors),
            )
            return None

        response_addr = reply_address(request_env.payload or {}, addr)
        if response_addr is None:
            self.event_log.log(
                "task_package_receipt_no_reply_address",
                task_id=task_id,
                accepted=accepted,
            )
            return None
        try:
            self._capture_message(
                "send",
                "TASK_PACKAGE_RECEIPT",
                payload,
                addr=response_addr,
                sender=self.node_id,
                target=request_env.sender or "model",
            )
            self.transport.send_message(
                msg_type="TASK_PACKAGE_RECEIPT",
                target=request_env.sender or "model",
                payload=payload,
                addr=response_addr,
                require_ack=bool((request_env.payload or {}).get("response_require_ack", False)),
            )
            self.event_log.log(
                "task_package_receipt_sent",
                task_id=task_id,
                accepted=accepted,
                addr=f"{response_addr[0]}:{response_addr[1]}",
            )
        except Exception as exc:
            self.event_log.log(
                "task_package_receipt_failed",
                task_id=task_id,
                accepted=accepted,
                error=f"{type(exc).__name__}: {exc}",
            )
        return None

    def _request_dian_context_from_model(self, task_id: str) -> None:
        if not self.model_callback_enabled:
            self.event_log.log("dian_context_request_skipped", task_id=task_id, reason="model_callback_disabled")
            return
        started_at = time.perf_counter()
        payload = {
            "msg_type": MSG_REQUEST_DIAN,
            "data": {},
        }
        self._send_raw_json_callback(
            (self.model_callback_host, self.model_callback_port),
            payload,
            "dian_context_requested",
            task_id=task_id,
            target_addr=f"{self.model_callback_host}:{self.model_callback_port}",
        )
        debug_append_log(
            f"[dian-context-requested] task_id={task_id} target={self.model_callback_host}:{self.model_callback_port}"
        )
        self._timing_log(
            "dian_context_requested",
            started_at=started_at,
            task_id=task_id,
            target_addr=f"{self.model_callback_host}:{self.model_callback_port}",
        )
#接收三类点位，并投影到路往上
    def _on_dian_context(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        rows = self._normalize_dian_rows(env.payload or {})
        normalized_at = time.perf_counter()
        theater = self._scheduler_theater()
        if theater:
            rows = [row for row in rows if self._row_in_theater(row, theater)]
        filtered_at = time.perf_counter()
        if env.msg_type == MSG_FA_SHE_DIAN:
            self.external_fa_she_dian = rows
            self.points.launch_points = self._dian_rows_to_graph_nodes(rows, "launch")
        elif env.msg_type == MSG_YIN_BI_DIAN:
            self.external_yin_bi_dian = rows
            self.points.hide_points = self._dian_rows_to_graph_nodes(rows, "hide")
        elif env.msg_type in {MSG_DEPOT_DIAN, MSG_ZHU_BEI_DIAN, MSG_ZHU_BEI_KU_DIAN}:
            self.external_depot_dian = rows
            self.points.depots = self._dian_rows_to_graph_nodes(rows, "depot")
        elif env.msg_type == MSG_VEHICLE_DIAN:
            self._merge_dian_rows(self.external_vehicle_dian, rows)
            self._apply_vehicle_dian_rows(rows)
        self.event_log.log(
            "dian_context_received",
            msg_type=env.msg_type,
            count=len(rows),
            total=self._dian_total_count(env.msg_type),
        )
        debug_append_log(
            f"[dian-context-received] msg_type={env.msg_type} count={len(rows)} "
            f"total={self._dian_total_count(env.msg_type)} rows={rows[:3]}"
        )
        applied_at = time.perf_counter()
        self._timing_log(
            "dian_context_received",
            elapsed_sec=applied_at - started_at,
            msg_type=env.msg_type,
            rows=len(rows),
            total=self._dian_total_count(env.msg_type),
            normalize_sec=round(normalized_at - started_at, 6),
            filter_sec=round(filtered_at - normalized_at, 6),
            apply_sec=round(applied_at - filtered_at, 6),
        )
        
        self._maybe_send_vehicle_scoring_context_to_model()
#时间约束和倒排结果
    def _maybe_send_vehicle_scoring_context_to_model(self) -> None:
        task_id = self._latest_active_task_id()
        if not task_id or task_id in self.vehicle_scoring_context_sent_tasks:
            return
        expected_vehicles = len(self.vehicles)
        if (
            len(self.external_fa_she_dian) < expected_vehicles
            or not self.external_yin_bi_dian
            or len(self.external_vehicle_dian) < expected_vehicles
            or not self.external_depot_dian
            or not self.points.depots
        ):
            debug_append_log(
                f"[score-context-waiting] task_id={task_id} "
                f"fa_she={len(self.external_fa_she_dian)}/{expected_vehicles} "
                f"yin_bi={len(self.external_yin_bi_dian)} "
                f"vehicle={len(self.external_vehicle_dian)}/{expected_vehicles} "
                f"depot={len(self.external_depot_dian)} projected_depot={len(self.points.depots)}"
            )
            self.event_log.log(
                "vehicle_scoring_context_waiting_dian",
                task_id=task_id,
                fa_she_count=len(self.external_fa_she_dian),
                fa_she_expected=expected_vehicles,
                yin_bi_count=len(self.external_yin_bi_dian),
                vehicle_count=len(self.external_vehicle_dian),
                vehicle_expected=expected_vehicles,
                depot_count=len(self.external_depot_dian),
                projected_depot_count=len(self.points.depots),
            )
            return
        self._send_vehicle_scoring_context_to_model(task_id)
        self.vehicle_scoring_context_sent_tasks.add(task_id)

    @staticmethod
    def _dian_row_key(row: dict, fallback: int) -> str:
        for key in ("id", "point_id", "vehicle_id", "port", "name", "index"):
            value = row.get(key)
            if value not in {None, ""}:
                return str(value)
        return f"idx:{fallback}"

    def _merge_dian_rows(self, target: List[dict], rows: List[dict]) -> None:
        index = {self._dian_row_key(row, idx): idx for idx, row in enumerate(target)}
        for row in rows:
            key = self._dian_row_key(row, len(target))
            if key in index:
                target[index[key]] = dict(row)
            else:
                index[key] = len(target)
                target.append(dict(row))

    def _dian_total_count(self, msg_type: str) -> int:
        if msg_type == MSG_FA_SHE_DIAN:
            return len(self.external_fa_she_dian)
        if msg_type == MSG_YIN_BI_DIAN:
            return len(self.external_yin_bi_dian)
        if msg_type == MSG_VEHICLE_DIAN:
            return len(self.external_vehicle_dian)
        if msg_type in {MSG_DEPOT_DIAN, MSG_ZHU_BEI_DIAN, MSG_ZHU_BEI_KU_DIAN}:
            return len(self.external_depot_dian)
        return 0
#投影函数，吧一批原始的点位批量转化为路网中的节点ID，调用nearest_graph_node
    def _dian_rows_to_graph_nodes(self, rows: List[dict], prefix: str = "dian") -> List[str]:
        out: List[str] = []
        if prefix == "launch":
            self.external_launch_point_rows_by_node = {}
        elif prefix == "hide":
            self.external_hide_point_rows_by_node = {}
        elif prefix == "depot":
            self.external_depot_point_rows_by_node = {}
        for idx, row in enumerate(rows):
            node_id = self._nearest_graph_node_for_dian(row, f"{prefix}_{idx:03d}")
            if not node_id:
                continue
            if prefix == "launch":
                self.external_launch_point_rows_by_node[node_id] = dict(row)
            elif prefix == "hide":
                self.external_hide_point_rows_by_node[node_id] = dict(row)
            elif prefix == "depot":
                self.external_depot_point_rows_by_node[node_id] = dict(row)
            if node_id not in out:
                out.append(node_id)
        return out

    def _apply_vehicle_dian_rows(self, rows: List[dict]) -> None:
        applied = 0
        skipped = 0
        for row in rows:
            vehicle_id = str(row.get("vehicle_id") or row.get("port") or row.get("vehicle_port") or "")
            if not vehicle_id or vehicle_id not in self.vehicles:
                skipped += 1
                continue
            node_id = self._nearest_graph_node_for_dian(row, f"vehicle_{vehicle_id}")
            if node_id:
                old_node = self.vehicles[vehicle_id].current_node
                self.vehicles[vehicle_id].current_node = node_id
                applied += 1
                self.event_log.log(
                    "vehicle_dian_row_applied",
                    vehicle_id=vehicle_id,
                    old_node=old_node,
                    current_node=node_id,
                    lon=row.get("lon") or row.get("longitude") or row.get("platform_LocationLLA_Lon"),
                    lat=row.get("lat") or row.get("latitude") or row.get("platform_LocationLLA_Lat"),
                )
            else:
                skipped += 1
            seen_time = row.get("time", row.get("sim_time", row.get("simTime")))
            if seen_time is not None:
                self.vehicles[vehicle_id].last_seen = str(seen_time)
        self.event_log.log("vehicle_dian_apply_summary", rows=len(rows), applied=applied, skipped=skipped)
#单点投影入口，先把原始点位转化成图坐标系下面的xy，在路网里找最近的边投影，
    def _nearest_graph_node_for_dian(self, row: dict, prefix: str = "dian") -> Optional[str]:
        node_id = row.get("node_id") or row.get("launch_node") or row.get("current_node")
        if node_id and str(node_id) in self.graph.nodes:
            return str(node_id)
        xy = self._dian_xy(row)
        if xy is None:
            return None
        x_f, y_f = xy
        nearest_edge = MapLoader._nearest_edge_projection(self.graph, x_f, y_f)
        if nearest_edge is not None:
            edge, ratio = nearest_edge
            point_id = self._dian_graph_point_id(row, prefix)
            if point_id in self.graph.nodes:
                row["node_id"] = point_id
                return point_id
            projected_xy = self._point_on_edge_geometry(edge, ratio)
            oriented_ratio = float(ratio)
            if edge.geometry and not (edge.geom_from == edge.src and edge.geom_to == edge.dst):
                oriented_ratio = 1.0 - oriented_ratio
            projected_x, projected_y = projected_xy if projected_xy is not None else (x_f, y_f)
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
                row["node_id"] = projected
                row["projection_from_node"] = edge.src
                row["projection_to_node"] = edge.dst
                row["projection_ratio"] = oriented_ratio
                row["projected_x"] = projected_x
                row["projected_y"] = projected_y
                if projected_lonlat is not None:
                    row["mapped_lon"], row["mapped_lat"] = projected_lonlat
                return projected
            except Exception as exc:
                debug_append_log(
                    f"[dian-edge-project-failed] point_id={point_id} edge={edge.src}->{edge.dst} "
                    f"error={type(exc).__name__}: {exc}"
                )
        return min(
            self.graph.nodes,
            key=lambda nid: (self.graph.nodes[nid].x - x_f) ** 2 + (self.graph.nodes[nid].y - y_f) ** 2,
            default=None,
        )

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

    def _dian_graph_point_id(self, row: dict, prefix: str) -> str:
        if prefix in {"launch", "hide", "depot"}:
            raw = ""
            for key in ("id", "point_id", "fire_point_id", "launch_node", "node_id", "name", "index"):
                value = row.get(key)
                if value not in {None, ""}:
                    raw = str(value)
                    break
            if not raw:
                raw = self._dian_row_key(row, 0)
        else:
            raw = self._dian_row_key(row, 0)
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(raw))
        return f"{prefix}_{safe}"

    def _dian_xy(self, row: dict) -> Optional[Tuple[float, float]]:
        lon = row.get("lon", row.get("lng", row.get("longitude", row.get("platform_LocationLLA_Lon"))))
        lat = row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat")))
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
        for node in self.graph.nodes.values():
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
        xs = [node.x for node in self.graph.nodes.values()]
        ys = [node.y for node in self.graph.nodes.values()]
        return min(xs) >= -180.0 and max(xs) <= 180.0 and min(ys) >= -90.0 and max(ys) <= 90.0

    @staticmethod
    def _normalize_dian_rows(payload: dict) -> List[dict]:
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in (
                "points",
                "items",
                "vehicles",
                "fa_she_dian",
                "yin_bi_dian",
                "vehicle_dian",
                "depot_dian",
                "zhu_bei_dian",
                "zhu_bei_ku_dian",
                "depots",
            ):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
            else:
                rows = [data]
        else:
            rows = []
        return [dict(row) for row in rows if isinstance(row, dict)]
    # 接收车辆回传的候选路径；一旦某辆车路径到齐，就尝试推进整批任务的调度求解。
    def _on_vehicle_candidate_path_result(self, env: Envelope) -> None:
        started_at = time.perf_counter()
        payload = env.payload or {}
        vehicle_id = str(payload.get("vehicle_id") or payload.get("port") or env.sender or "")
        if not vehicle_id:
            self.event_log.log("vehicle_candidate_path_result_ignored", reason="missing_vehicle_id")
            return
        paths = payload.get("paths") or []
        if not isinstance(paths, list):
            paths = []
        # 保存该车的全部候选路径，供后续全局分配和冲突消解使用。
        self.external_vehicle_candidate_paths[vehicle_id] = dict(payload)
        self.event_log.log(
            "vehicle_candidate_path_result_received",
            task_id=payload.get("task_id"),
            vehicle_id=vehicle_id,
            path_count=len(paths),
        )
        debug_append_log(
            f"[candidate-path-result] vehicle={vehicle_id} task_id={payload.get('task_id')} paths={len(paths)}"
        )
        # 每收到一辆车的候选路径都尝试推进，直到这一批任务满足求解条件。
        self._try_resolve_external_dispatches()
        self._timing_log(
            "vehicle_candidate_path_result_received",
            started_at=started_at,
            task_id=payload.get("task_id"),
            vehicle_id=vehicle_id,
            path_count=len(paths),
        )

    # 生成并发给模型两类上下文：
    # 1. 时间倒排规则
    # 2. 每辆车对应的候选发射点列表
    def _send_vehicle_scoring_context_to_model(self, task_id: str) -> None:
        if not self.vehicle_scoring_context_enabled or not self.model_callback_enabled:
            return
        started_at = time.perf_counter()
        ctx = self._build_vehicle_scoring_context({"task_id": task_id})
        built_at = time.perf_counter()
        backplan_payload = {
            "msg_type": MSG_TIME_BACKPLAN_CONTEXT,
            "data": self._build_compact_time_backplan_rules(task_id),
        }
        # 候选点消息里包含“每辆车 -> 候选发射点”的映射，车辆后续据此规划真实路径。
        candidates_payload = {
            "msg_type": MSG_VEHICLE_CANDIDATE_CONTEXT,
            "data": {
                "schema": "vehicle_candidate_context_v1",
                "task_id": task_id,
                "vehicles": self._compact_vehicle_candidate_launch_points(
                    ctx.get("vehicle_candidate_launch_points", [])
                ),
            },
        }
        try:
            self._send_raw_json_callback(
                (self.model_callback_host, self.model_callback_port),
                backplan_payload,
                "time_backplan_context_sent",
                task_id=task_id,
            )
            self._send_raw_json_callback(
                (self.model_callback_host, self.model_callback_port),
                candidates_payload,
                "vehicle_candidate_context_sent",
                task_id=task_id,
                vehicle_count=len(ctx.get("vehicle_candidate_launch_points") or []),
            )
            debug_append_log(
                f"[score-context-sent] task_id={task_id} "
                f"vehicles={len(ctx.get('vehicle_candidate_launch_points') or [])} "
                f"target={self.model_callback_host}:{self.model_callback_port} "
                f"build_sec={built_at - started_at:.3f} send_sec={time.perf_counter() - built_at:.3f}"
            )
            self._timing_log(
                "vehicle_scoring_context_sent",
                elapsed_sec=time.perf_counter() - started_at,
                task_id=task_id,
                vehicle_count=len(ctx.get("vehicle_candidate_launch_points") or []),
                build_sec=round(built_at - started_at, 6),
                send_sec=round(time.perf_counter() - built_at, 6),
            )
        except Exception as exc:
            self.event_log.log(
                "vehicle_scoring_context_send_failed",
                task_id=task_id,
                target_addr=f"{self.model_callback_host}:{self.model_callback_port}",
                error=f"{type(exc).__name__}: {exc}",
            )
            debug_append_log(f"[score-context-failed] task_id={task_id} error={type(exc).__name__}: {exc}")
            self._timing_log(
                "vehicle_scoring_context_send_failed",
                elapsed_sec=time.perf_counter() - started_at,
                task_id=task_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _raw_json_reply_addr(self, payload: dict, addr: MessageAddress) -> Optional[Tuple[str, int]]:
        if self.model_callback_enabled and self.model_callback_host and self.model_callback_port > 0:
            return (self.model_callback_host, self.model_callback_port)
        reply_host = (
            payload.get("reply_host")
            or payload.get("response_host")
            or (addr[0] if addr else "")
        )
        reply_port = payload.get("reply_port") or payload.get("response_port")
        if reply_host and reply_port:
            return (str(reply_host), int(reply_port))
        return None

    def _send_raw_json_callback(self, addr: Tuple[str, int], payload: dict, event_name: str, **fields: Any) -> None:
        try:
            wrapped = self._wrap_raw_json_payload(payload)
            if (
                self.model_message_type_tag
                and addr[0] == self.model_callback_host
                and int(addr[1]) == int(self.model_callback_port)
            ):
                wrapped["group"] = self.model_message_type_tag
            data = json.dumps(wrapped, ensure_ascii=False).encode("utf-8") + b"\n"
            self._capture_message("send", str(wrapped.get("msg_type") or "UNKNOWN"), wrapped.get("data") or {}, addr=addr)
            timeout = float(self.transport_cfg.get("tcp", {}).get("connect_timeout_sec", 1.5))
            with socket.create_connection(addr, timeout=timeout) as sock:
                sock.sendall(data)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            self.event_log.log(event_name, addr=f"{addr[0]}:{addr[1]}", bytes=len(data), **fields)
        except Exception as exc:
            self.event_log.log(
                f"{event_name}_failed",
                addr=f"{addr[0]}:{addr[1]}",
                error=f"{type(exc).__name__}: {exc}",
                **fields,
            )

    @staticmethod
    def _wrap_raw_json_payload(payload: dict) -> dict:
        if "msg_type" in payload and isinstance(payload.get("data"), dict):
            return payload
        if "msgtype" in payload and isinstance(payload.get("data"), dict):
            return {
                "msg_type": str(payload.get("msgtype") or "UNKNOWN"),
                "data": dict(payload.get("data") or {}),
            }
        msg_type = str(payload.get("msg_type") or payload.get("msgtype") or payload.get("type") or "UNKNOWN")
        if "data" in payload:
            raw_data = payload.get("data")
            data = dict(raw_data) if isinstance(raw_data, dict) else {"items": raw_data}
            for key, value in payload.items():
                if key not in {"msg_type", "msgtype", "message_type", "type", "data"}:
                    data[key] = value
            return {"msg_type": msg_type, "data": data}
        data = {
            key: value
            for key, value in payload.items()
            if key not in {"msg_type", "msgtype", "message_type", "type", "data"}
        }
        return {"msg_type": msg_type, "data": data}

    def _capture_message(
        self,
        direction: str,
        msg_type: str,
        payload: dict,
        *,
        addr: MessageAddress = None,
        sender: Optional[str] = None,
        target: Optional[str] = None,
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
                "node_type": "scheduler",
                "node_id": self.node_id,
                "direction": direction,
                "msg_type": msg_type,
                "sender": sender,
                "target": target,
                "addr": f"{addr[0]}:{addr[1]}" if addr else None,
                "payload": payload,
            }
            out_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


    def _task_sim_fire_time(self, task_id: Optional[str]) -> Optional[float]:
        if not task_id:
            return None
        for st in self.subtasks.values():
            if st.task_id == task_id and st.sim_fire_time is not None:
                return float(st.sim_fire_time)
        for st in self.subtasks.values():
            if st.task_id == task_id:
                return self._iso_to_sim_seconds(st.fire_time)
        return None
#生成并发送倒拍结果
    def _build_time_backplan_rules(self, task_id: Optional[str] = None, fire_time: Optional[Any] = None) -> dict:
        sim_fire_time = None
        if fire_time not in {None, ""}:
            try:
                sim_fire_time = float(fire_time)
            except Exception:
                sim_fire_time = self._iso_to_sim_seconds(fire_time)
        if sim_fire_time is None:
            sim_fire_time = self._task_sim_fire_time(task_id)
        launch_prepare_time = None
        launch_standby_time = None
        if sim_fire_time is not None:
            launch_prepare_time = sim_fire_time - self.launch_prepare_sec
            launch_standby_time = launch_prepare_time - self.cold_standby_sec
        time_constraints: List[dict] = []
        for st in sorted(
            (item for item in self.subtasks.values() if task_id is None or item.task_id == task_id),
            key=lambda item: (item.fire_time, item.subtask_id),
        ):
            subtask_fire_time = st.sim_fire_time if st.sim_fire_time is not None else sim_fire_time
            if subtask_fire_time is None:
                continue
            latest_arrive_launch_time = subtask_fire_time - self.launch_prepare_sec
            latest_arrive_hide_time = latest_arrive_launch_time - self.cold_standby_sec
            time_constraints.append(
                {
                    "subtask_id": st.subtask_id,
                    "task_id": st.task_id,
                    "latest_arrive_launch_time": round(latest_arrive_launch_time, 3),
                    "latest_arrive_hide_time": round(latest_arrive_hide_time, 3),
                    "latest_depart_hide_time": round(latest_arrive_hide_time, 3),
                    "launch_prepare_duration_sec": round(self.launch_prepare_sec, 3),
                    "launch_standby_duration_sec": round(self.cold_standby_sec, 3),
                    "latest_finish_time": round(subtask_fire_time, 3),
                    "redundancy_ratio": 1.0 + float(1 if st.is_redundant else 0),
                }
            )
        return {
            "schema": "time_backplan_points_v1",
            "task_id": task_id,
            "fire_time": sim_fire_time,
            "launch_prepare_time": launch_prepare_time,
            "launch_standby_time": launch_standby_time,
            "standby_mode": "cold_default",
            "task_book_time_constraints": time_constraints,
            "subtasks": time_constraints,
        }

    def _latest_active_task_id(self) -> Optional[str]:
        task_ids = sorted({
            st.task_id
            for st in self.subtasks.values()
            if st.status not in {"DONE", "FAILED"}
        })
        return task_ids[-1] if task_ids else None

    def _build_vehicle_scoring_context(self, request: dict) -> dict:
        task_id_filter = request.get("task_id")
        time_rules = self._build_time_backplan_rules(task_id_filter)
        return {
            "task_id": task_id_filter,
            "fire_time_grace_sec": float(self.fire_window_grace_sec),
            "score_submit_host": self.score_submit_host,
            "score_port_base": self.score_port_base,
            "score_port_count": self.score_port_count,
            "depot_points": self._compact_depot_points(),
            "vehicle_candidate_launch_points": self._build_vehicle_candidate_launch_points(),
        }

    def _build_compact_time_backplan_rules(self, task_id: str) -> dict:
        rules = self._build_time_backplan_rules(task_id)
        constraints = list(rules.get("task_book_time_constraints") or rules.get("subtasks") or [])
        return {
            "schema": rules.get("schema", "time_backplan_points_v1"),
            "task_id": rules.get("task_id"),
            "fire_time": rules.get("fire_time"),
            "launch_prepare_time": rules.get("launch_prepare_time"),
            "launch_standby_time": rules.get("launch_standby_time"),
            "standby_mode": rules.get("standby_mode"),
            "subtasks": constraints,
        }

    @staticmethod
    def _compact_vehicle_candidate_launch_points(rows: List[dict]) -> List[dict]:
        compact_rows: List[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            compact_candidates: List[dict] = []
            for candidate in row.get("candidate_launch_points") or []:
                if not isinstance(candidate, dict):
                    continue
                compact_candidates.append(
                    {
                        "rank": candidate.get("rank"),
                        "launch_node": candidate.get("launch_node"),
                        "node_id": candidate.get("node_id") or candidate.get("launch_node"),
                        "fire_point_id": candidate.get("fire_point_id"),
                        "name": candidate.get("name"),
                        "index": candidate.get("index"),
                        "lon": candidate.get("lon"),
                        "lat": candidate.get("lat"),
                        "alt": candidate.get("alt"),
                        "mapped_lon": candidate.get("mapped_lon"),
                        "mapped_lat": candidate.get("mapped_lat"),
                        "projection_from_node": candidate.get("projection_from_node"),
                        "projection_to_node": candidate.get("projection_to_node"),
                        "projection_ratio": candidate.get("projection_ratio"),
                        "projected_x": candidate.get("projected_x"),
                        "projected_y": candidate.get("projected_y"),
                        "hide_candidates": list(candidate.get("hide_candidates") or []),
                        "depot_candidates": list(candidate.get("depot_candidates") or []),
                        "allow_direct": bool(candidate.get("allow_direct", True)),
                    }
                )
            compact_rows.append(
                {
                    "vehicle_id": row.get("vehicle_id"),
                    "vehicle_port": row.get("vehicle_port"),
                    "candidate_launch_points": compact_candidates,
                }
            )
        return compact_rows

    def _compact_depot_points(self) -> List[dict]:
        rows: List[dict] = []
        for rank, depot_node in enumerate(self.points.depots, start=1):
            row = dict(self.external_depot_point_rows_by_node.get(depot_node, {}))
            node = self.graph.nodes.get(depot_node)
            raw_capacity = row.get("capacity")
            capacity = (
                self.depot_capacity
                if raw_capacity in {None, ""}
                else max(0, int(raw_capacity))
            )
            rows.append(
                {
                    "rank": rank,
                    "depot_node": depot_node,
                    "node_id": depot_node,
                    "depot_id": row.get("id") or row.get("point_id") or row.get("name") or depot_node,
                    "depot_port": row.get("port") or row.get("vehicle_id"),
                    "name": row.get("name") or row.get("depot_name") or row.get("id") or depot_node,
                    "capacity": capacity,
                    "lon": row.get("lon", row.get("longitude", row.get("platform_LocationLLA_Lon", node.lon if node else None))),
                    "lat": row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat", node.lat if node else None))),
                    "mapped_lon": node.lon if node else None,
                    "mapped_lat": node.lat if node else None,
                    "projection_from_node": row.get("projection_from_node"),
                    "projection_to_node": row.get("projection_to_node"),
                    "projection_ratio": row.get("projection_ratio"),
                    "projected_x": row.get("projected_x"),
                    "projected_y": row.get("projected_y"),
                }
            )
        return rows

    def _candidate_depots_for_launch(self, launch_node: str, limit: int = 3) -> List[dict]:
        launch = self.graph.nodes.get(launch_node)
        if launch is None or not self.points.depots:
            return []
        ranked: List[Tuple[float, str, dict]] = []
        for row in self._compact_depot_points():
            depot_node = str(row.get("depot_node") or "")
            depot = self.graph.nodes.get(depot_node)
            if depot is None:
                continue
            distance_sq = (launch.x - depot.x) ** 2 + (launch.y - depot.y) ** 2
            ranked.append((distance_sq, str(row.get("depot_id") or depot_node), row))
        out: List[dict] = []
        for rank, (_distance, _depot_id, row) in enumerate(
            sorted(ranked, key=lambda item: (item[0], item[1]))[: max(1, int(limit))],
            start=1,
        ):
            item = dict(row)
            item["rank"] = rank
            out.append(item)
        return out
#生成候选点结果
    def _build_vehicle_candidate_launch_points(self, candidate_count: int = 6) -> List[dict]:
        started_at = time.perf_counter()
        # 拿到所有发射点。后续真正给每辆车分候选时，会再按战区过滤。
        raw_launch_points = list(self.points.launch_points)
        # 同一个真实发射点可能同时保留本地节点和运行时投影节点。先按真实
        # fire point ID 去重，并优先使用运行时点位对应的投影节点。
        launch_by_public_id: Dict[str, str] = {}
        for launch_node in raw_launch_points:
            public_id = self._external_launch_public_id(launch_node)
            current = launch_by_public_id.get(public_id)
            if current is None or (
                launch_node in self.external_launch_point_rows_by_node
                and current not in self.external_launch_point_rows_by_node
            ):
                launch_by_public_id[public_id] = launch_node
        launch_points_all = list(launch_by_public_id.values())
        if not launch_points_all:
            return []
        #拿到所有车辆
        vehicles = sorted(self.vehicles.values(), key=lambda item: item.vehicle_id)
        rows: List[dict] = []
        launch_coords = {
            lp: (self.graph.nodes[lp].x, self.graph.nodes[lp].y)
            for lp in launch_points_all
            if lp in self.graph.nodes
        }
        vehicle_coords: Dict[str, Tuple[float, float]] = {}
        for vehicle in vehicles:
            start_node = vehicle.current_node or vehicle.home_node
            start = self.graph.nodes.get(start_node)
            if start is not None:
                vehicle_coords[vehicle.vehicle_id] = (start.x, start.y)

        distance_cache: Dict[Tuple[str, str], float] = {}
        score_cache: Dict[Tuple[str, str], float] = {}
        launch_points_by_vehicle: Dict[str, List[str]] = {}
        for vehicle in vehicles:
            launch_points = launch_points_by_vehicle.setdefault(vehicle.vehicle_id, self._launch_nodes_for_vehicle(vehicle))
            start_xy = vehicle_coords.get(vehicle.vehicle_id)
            if start_xy is None:
                for lp in launch_points:
                    distance_cache[(vehicle.vehicle_id, lp)] = float("inf")
                    score_cache[(vehicle.vehicle_id, lp)] = float("-inf")
                continue
            sx, sy = start_xy
            for lp in launch_points:
                launch_xy = launch_coords.get(lp)
                if launch_xy is None:
                    distance_cache[(vehicle.vehicle_id, lp)] = float("inf")
                    score_cache[(vehicle.vehicle_id, lp)] = float("-inf")
                    continue
                lx, ly = launch_xy
                # 对应公式(1)的候选发射点排序准备阶段。
                # 这里先缓存车辆到发射点的距离，并调用 score_launch_point_for_vehicle 生成可解释评分。
                distance_cache[(vehicle.vehicle_id, lp)] = (sx - lx) ** 2 + (sy - ly) ** 2
                launch_score = score_launch_point_for_vehicle(
                    vehicle_id=vehicle.vehicle_id,
                    current_node=vehicle.current_node or vehicle.home_node,
                    launch_node=lp,
                    ammo_types=vehicle.ammo_types,
                    required_ammo_type=None,
                    speed_mps=vehicle.speed_mps,
                    graph=self.graph,
                    lane_graph=None,
                    request_time=0.0,
                    desired_arrival_time=(
                        self._task_sim_fire_time(self._latest_active_task_id()) - self.launch_prepare_sec
                        if self._task_sim_fire_time(self._latest_active_task_id()) is not None
                        else None
                    ),
                    fire_time_grace_sec=float(self.fire_window_grace_sec),
                    hide_candidate_count=0,
                    launch_priority_score=self._launch_depot_priority_score(lp),
                    redundancy_ratio=1.0,
                    ignore_speed_limits=True,
                )
                score_cache[(vehicle.vehicle_id, lp)] = float(launch_score.get("score_total", 0.0) or 0.0)

        ranked_by_vehicle: Dict[str, List[str]] = {}
        distance_ranked_by_vehicle: Dict[str, List[str]] = {}
        for vehicle in vehicles:
            launch_points = launch_points_by_vehicle.get(vehicle.vehicle_id, [])
            ranked_by_vehicle[vehicle.vehicle_id] = sorted(
                launch_points,
                key=lambda lp: (
                    -score_cache.get((vehicle.vehicle_id, lp), float("-inf")),
                    distance_cache.get((vehicle.vehicle_id, lp), float("inf")),
                    lp,
                ),
            )
            distance_ranked_by_vehicle[vehicle.vehicle_id] = sorted(
                launch_points,
                key=lambda lp: (
                    distance_cache.get((vehicle.vehicle_id, lp), float("inf")),
                    -score_cache.get((vehicle.vehicle_id, lp), float("-inf")),
                    lp,
                ),
            )

        # rank1 是真实占用的第一发射点，必须唯一。这里不用车辆 ID 顺序
        # 贪心抢点，而是在每辆车的局部近邻发射点里做全局唯一匹配；
        # 距离是主目标，评分只作为近距离内的辅助项，避免明显远点进入 rank1。
        rank1_by_vehicle, rank1_guard_stats = self._distance_bounded_unique_rank1_match(
            vehicles=vehicles,
            launch_points=launch_points_all,
            distance_ranked_by_vehicle=distance_ranked_by_vehicle,
            distance_cache=distance_cache,
            score_cache=score_cache,
        )
        rank1_unassigned = [
            vehicle.vehicle_id for vehicle in vehicles if vehicle.vehicle_id not in rank1_by_vehicle
        ]
        rank1_strategy = "distance_bounded_global_unique"
        hide_candidates_per_launch = 2
        rank1_hide_by_vehicle, rank1_hide_guard_stats = self._global_unique_rank1_hide_assignment(
            vehicles=vehicles,
            rank1_by_vehicle=rank1_by_vehicle,
        )

        for vehicle in vehicles:
            launch_points = launch_points_by_vehicle.get(vehicle.vehicle_id, [])
            max_candidates = min(candidate_count, len(launch_points)) if launch_points else 0
            ordered: List[str] = []
            rank1 = rank1_by_vehicle.get(vehicle.vehicle_id)
            if rank1:
                ordered.append(rank1)
            # rank1 由全局匹配保证唯一；rank2～rank6 只是车辆自己的后备集合，
            # 不代表占用，也不因被其他车辆列入候选或选为 rank1 而增加惩罚。
            backup_ranked = ranked_by_vehicle.get(vehicle.vehicle_id, [])
            for lp in backup_ranked:
                if len(ordered) >= max_candidates:
                    break
                if lp not in ordered:
                    ordered.append(lp)
            candidates = []
            #每个发射点附带候选隐蔽点
            for idx, launch_node in enumerate(ordered):
                candidate = self._candidate_launch_point_payload(launch_node, idx + 1)
                preferred_hide = rank1_hide_by_vehicle.get(vehicle.vehicle_id, "") if launch_node == rank1 else ""
                hide_candidates = self._candidate_hide_points_for_route(
                    vehicle.current_node or vehicle.home_node,
                    launch_node,
                    candidate_count=hide_candidates_per_launch,
                    preferred_hide=preferred_hide,
                    allowed_hide_points=self._hide_nodes_for_vehicle(vehicle),
                )
                candidate["hide_candidates"] = hide_candidates
                candidate["candidate_hide_points"] = hide_candidates
                candidate["depot_candidates"] = self._candidate_depots_for_launch(launch_node)
                candidate["allow_direct"] = True
                launch_score = score_launch_point_for_vehicle(
                    vehicle_id=vehicle.vehicle_id,
                    current_node=vehicle.current_node or vehicle.home_node,
                    launch_node=launch_node,
                    ammo_types=vehicle.ammo_types,
                    required_ammo_type=None,
                    speed_mps=vehicle.speed_mps,
                    graph=self.graph,
                    lane_graph=self.lane_graph,
                    request_time=0.0,
                    desired_arrival_time=(
                        self._task_sim_fire_time(self._latest_active_task_id()) - self.launch_prepare_sec
                        if self._task_sim_fire_time(self._latest_active_task_id()) is not None
                        else None
                    ),
                    fire_time_grace_sec=float(self.fire_window_grace_sec),
                    hide_candidate_count=len(hide_candidates),
                    launch_priority_score=max(0.0, self._launch_depot_priority_score(launch_node) - idx * 8.0),
                    redundancy_ratio=1.0,
                    ignore_speed_limits=True,
                )
                candidate["launch_score_breakdown"] = launch_score
                candidate["launch_point_priority_score"] = launch_score.get("score_total", 0.0)
                # 对应公式(5)：把任务截止时间、车辆类型权重和到目标距离转成优先级拆分项。
                # 该字段随候选上下文发给模型，便于解释车辆-发射点候选排序。
                # 公式(5) compute_dispatch_priority 实际计算点：当前仅输出 priority_* 解释字段。
                # 如需让公式直接控制冲突消解顺序，应把 priority_total 接入路径选择排序键。
                priority = compute_dispatch_priority(
                    task_deadline_sec=self._task_sim_fire_time(self._latest_active_task_id()),
                    current_time_sec=0.0,
                    vehicle_type_weight=1.0,
                    distance_to_target_m=launch_score.get("distance_m"),
                )
                candidate["dispatch_priority_breakdown"] = priority
                candidates.append(candidate)
            candidate_distance_rows = []
            for idx, launch_node in enumerate(ordered):
                distance_sq = distance_cache.get((vehicle.vehicle_id, launch_node), float("inf"))
                candidate_distance_rows.append(
                    {
                        "rank": idx + 1,
                        "launch_node": launch_node,
                        "fire_point_id": self._external_launch_public_id(launch_node),
                        "score_total": round(score_cache.get((vehicle.vehicle_id, launch_node), 0.0), 3),
                        "distance_m": (
                            round(math.sqrt(max(0.0, distance_sq)), 3)
                            if math.isfinite(distance_sq)
                            else None
                        ),
                    }
                )
            self.event_log.log(
                "vehicle_candidate_launch_points_vehicle",
                vehicle_id=vehicle.vehicle_id,
                theater_id=str((self._theater_for_vehicle(vehicle) or {}).get("id") or ""),
                start_node=vehicle.current_node or vehicle.home_node,
                rank1=rank1,
                rank1_hide=rank1_hide_by_vehicle.get(vehicle.vehicle_id),
                candidates=candidate_distance_rows,
                hide_candidate_policy="rank1_global_unique_first_plus_nearest_backup",
                backup_candidate_policy="nearest_without_cross_vehicle_penalty",
            )
            rows.append(
                {
                    "vehicle_id": vehicle.vehicle_id,
                    "vehicle_port": vehicle.endpoint[1] if vehicle.endpoint else None,
                    "candidate_launch_points": candidates,
                }
            )
        self.event_log.log(
            "vehicle_candidate_launch_points_built",
            vehicle_count=len(rows),
            launch_point_count=len(launch_points_all),
            raw_launch_point_count=len(raw_launch_points),
            duplicate_launch_point_count=max(0, len(raw_launch_points) - len(launch_points_all)),
            candidate_count=candidate_count,
            rank1_assigned_count=len(rank1_by_vehicle),
            rank1_unique_count=len({self._external_launch_public_id(value) for value in rank1_by_vehicle.values()}),
            rank1_pool_count=len(launch_points_all),
            rank1_locality_guard=rank1_guard_stats,
            rank1_unassigned_vehicle_ids=rank1_unassigned,
            rank1_hide_assigned_count=len(rank1_hide_by_vehicle),
            rank1_hide_unique_count=len({self._external_hide_public_id(value) for value in rank1_hide_by_vehicle.values()}),
            rank1_hide_guard=rank1_hide_guard_stats,
            hide_candidate_mode="rank1_global_unique_first_plus_vehicle_launch_bound",
            hide_candidates_per_launch=hide_candidates_per_launch,
            backup_candidate_policy="nearest_without_cross_vehicle_penalty",
            build_sec=round(time.perf_counter() - started_at, 6),
            strategy=f"{rank1_strategy}_plus_score_ranked_backups",
        )
        return rows

    def _global_unique_rank1_hide_assignment(
        self,
        vehicles: List[VehicleRuntime],
        rank1_by_vehicle: Dict[str, str],
    ) -> Tuple[Dict[str, str], dict]:
        """为每辆车的 rank1 发射点分配全局唯一的第一隐蔽点。"""
        active_vehicles = [vehicle for vehicle in vehicles if rank1_by_vehicle.get(vehicle.vehicle_id)]
        if not active_vehicles:
            return {}, {"reason": "no_rank1_launches"}

        hide_nodes: List[str] = []
        cost_cache: Dict[Tuple[str, str], float] = {}
        allowed_counts: Dict[str, int] = {}
        for vehicle in active_vehicles:
            vehicle_id = vehicle.vehicle_id
            start_node = vehicle.current_node or vehicle.home_node
            launch_node = rank1_by_vehicle.get(vehicle_id, "")
            start = self.graph.nodes.get(start_node)
            launch = self.graph.nodes.get(launch_node)
            if start is None or launch is None:
                allowed_counts[vehicle_id] = 0
                continue
            direct_dist = max(1.0, math.hypot(start.x - launch.x, start.y - launch.y))
            allowed = self._hide_nodes_for_vehicle(vehicle)
            allowed_counts[vehicle_id] = len(allowed)
            for hide_node in allowed:
                hide = self.graph.nodes.get(hide_node)
                if hide is None:
                    continue
                to_hide = math.hypot(start.x - hide.x, start.y - hide.y)
                to_launch = math.hypot(hide.x - launch.x, hide.y - launch.y)
                total_dist = to_hide + to_launch
                detour_ratio = total_dist / direct_dist
                corridor_dist = self._point_segment_distance(
                    hide.x,
                    hide.y,
                    start.x,
                    start.y,
                    launch.x,
                    launch.y,
                )
                # 保持和局部隐蔽点排序一致：绕行率优先，其次贴近主路径，再看离发射点距离。
                cost_cache[(vehicle_id, hide_node)] = detour_ratio * 1_000_000.0 + corridor_dist + to_launch / 1000.0
                if hide_node not in hide_nodes:
                    hide_nodes.append(hide_node)

        if len(hide_nodes) < len(active_vehicles):
            return {}, {
                "reason": "insufficient_hide_points",
                "vehicle_count": len(active_vehicles),
                "hide_point_count": len(hide_nodes),
                "allowed_counts_min": min(allowed_counts.values()) if allowed_counts else 0,
            }
        assignment = self._hungarian_unique_rank1_match(active_vehicles, hide_nodes, cost_cache)
        unassigned = [vehicle.vehicle_id for vehicle in active_vehicles if vehicle.vehicle_id not in assignment]
        return assignment, {
            "vehicle_count": len(active_vehicles),
            "hide_point_count": len(hide_nodes),
            "assigned_count": len(assignment),
            "unique_count": len(set(assignment.values())),
            "unassigned_count": len(unassigned),
            "unassigned_vehicle_ids": unassigned[:20],
        }

    def _launch_depot_priority_score(self, launch_node: str) -> float:
        if launch_node not in self.graph.nodes or not self.points.depots:
            return 100.0
        launch = self.graph.nodes[launch_node]
        best_sq: Optional[float] = None
        for depot_node in self.points.depots:
            depot = self.graph.nodes.get(depot_node)
            if depot is None:
                continue
            dist_sq = (launch.x - depot.x) ** 2 + (launch.y - depot.y) ** 2
            best_sq = dist_sq if best_sq is None else min(best_sq, dist_sq)
        if best_sq is None:
            return 100.0
        dist_m = math.sqrt(max(0.0, best_sq))
        return max(0.0, min(100.0, 100.0 - dist_m / 1000.0))

    def _distance_bounded_unique_rank1_match(
        self,
        vehicles: List[VehicleRuntime],
        launch_points: List[str],
        distance_ranked_by_vehicle: Dict[str, List[str]],
        distance_cache: Dict[Tuple[str, str], float],
        score_cache: Dict[Tuple[str, str], float],
    ) -> Tuple[Dict[str, str], dict]:
        """在局部近邻范围内为 rank1 做全局唯一匹配。

        rank1 代表最终占用的第一候选发射点，所以必须全局不重复。
        但如果按车辆顺序抢点，后面的车辆会被挤到很远的点。这里先
        给每辆车保留本车附近的候选边，再用匈牙利算法做最小代价匹配。
        """
        if not vehicles or len(launch_points) < len(vehicles):
            return {}, {"reason": "insufficient_launch_points"}
        initial_pool = max(1, int(self.assignment_scoring_cfg.get("rank1_match_initial_pool", 8)))
        ratio_limit = max(1.0, float(self.assignment_scoring_cfg.get("rank1_locality_ratio_limit", 2.5)))
        max_distance_m = float(self.assignment_scoring_cfg.get("rank1_max_distance_m", 30000.0))
        score_penalty_per_point_m = float(
            self.assignment_scoring_cfg.get("rank1_score_penalty_per_point_m", 100.0)
        )
        pool_counts: List[int] = []
        for value in (initial_pool, 12, 16, 24, 32, len(launch_points)):
            value = max(1, min(int(value), len(launch_points)))
            if value not in pool_counts:
                pool_counts.append(value)

        best_assignment: Dict[str, str] = {}
        best_pool = 0
        for pool_count in pool_counts:
            allowed = self._rank1_allowed_launch_edges(
                vehicles=vehicles,
                distance_ranked_by_vehicle=distance_ranked_by_vehicle,
                distance_cache=distance_cache,
                pool_count=pool_count,
                ratio_limit=ratio_limit,
                max_distance_m=max_distance_m,
            )
            cost_cache: Dict[Tuple[str, str], float] = {}
            for vehicle_id, allowed_launches in allowed.items():
                for launch_node in allowed_launches:
                    dist_sq = distance_cache.get((vehicle_id, launch_node), float("inf"))
                    if not math.isfinite(dist_sq):
                        continue
                    dist_m = math.sqrt(max(0.0, dist_sq))
                    score = score_cache.get((vehicle_id, launch_node), 0.0)
                    score_penalty = max(0.0, 100.0 - score) * score_penalty_per_point_m
                    cost_cache[(vehicle_id, launch_node)] = dist_m + score_penalty
            assignment = self._hungarian_unique_rank1_match(vehicles, launch_points, cost_cache)
            if len(assignment) > len(best_assignment):
                best_assignment = assignment
                best_pool = pool_count
            if len(assignment) == len(vehicles):
                far_count = self._rank1_far_assignment_count(assignment, distance_cache, max_distance_m)
                return assignment, {
                    "pool_count": pool_count,
                    "expanded": pool_count != pool_counts[0],
                    "far_count": far_count,
                    "max_distance_m": max_distance_m,
                    "ratio_limit": ratio_limit,
                }

        far_count = self._rank1_far_assignment_count(best_assignment, distance_cache, max_distance_m)
        return best_assignment, {
            "pool_count": best_pool,
            "expanded": True,
            "far_count": far_count,
            "max_distance_m": max_distance_m,
            "ratio_limit": ratio_limit,
            "unassigned": len(vehicles) - len(best_assignment),
        }

    def _rank1_allowed_launch_edges(
        self,
        vehicles: List[VehicleRuntime],
        distance_ranked_by_vehicle: Dict[str, List[str]],
        distance_cache: Dict[Tuple[str, str], float],
        pool_count: int,
        ratio_limit: float,
        max_distance_m: float,
    ) -> Dict[str, List[str]]:
        allowed: Dict[str, List[str]] = {}
        for vehicle in vehicles:
            vehicle_id = vehicle.vehicle_id
            ranked = [
                lp
                for lp in distance_ranked_by_vehicle.get(vehicle_id, [])
                if math.isfinite(distance_cache.get((vehicle_id, lp), float("inf")))
            ]
            if not ranked:
                allowed[vehicle_id] = []
                continue
            nearest_m = math.sqrt(max(0.0, distance_cache[(vehicle_id, ranked[0])]))
            distance_cap = max(nearest_m * ratio_limit, nearest_m + 1.0)
            if max_distance_m > 0:
                distance_cap = min(distance_cap, max_distance_m)
            local: List[str] = []
            for idx, launch_node in enumerate(ranked):
                dist_m = math.sqrt(max(0.0, distance_cache[(vehicle_id, launch_node)]))
                # pool_count 控制最多展开多少个近邻，但不能绕过距离上限；
                # 否则局部池扩大时，仍可能把几十公里外的点纳入 rank1 匹配。
                if idx < pool_count and dist_m <= distance_cap:
                    local.append(launch_node)
            allowed[vehicle_id] = local
        return allowed

    @staticmethod
    def _rank1_far_assignment_count(
        assignment: Dict[str, str],
        distance_cache: Dict[Tuple[str, str], float],
        max_distance_m: float,
    ) -> int:
        if max_distance_m <= 0:
            return 0
        count = 0
        for vehicle_id, launch_node in assignment.items():
            dist_sq = distance_cache.get((vehicle_id, launch_node), float("inf"))
            if math.isfinite(dist_sq) and math.sqrt(max(0.0, dist_sq)) > max_distance_m:
                count += 1
        return count

    def _repair_rank1_assignment_locality(
        self,
        assignment: Dict[str, str],
        vehicles: List[VehicleRuntime],
        ranked_by_vehicle: Dict[str, List[str]],
        distance_cache: Dict[Tuple[str, str], float],
        local_rank_limit: int,
    ) -> Tuple[Dict[str, str], dict]:
        """避免全局唯一匹配把车辆 rank1 推到本车局部排序很靠后的远点。"""
        if not assignment:
            return assignment, {"replaced": 0, "duplicate_fallback": 0}
        ratio_limit = float(self.assignment_scoring_cfg.get("rank1_locality_ratio_limit", 2.5))
        rank_limit = max(1, int(self.assignment_scoring_cfg.get("rank1_locality_rank_limit", local_rank_limit)))
        repaired = dict(assignment)
        replaced = 0
        duplicate_fallback = 0
        for vehicle in vehicles:
            vehicle_id = vehicle.vehicle_id
            ranked = ranked_by_vehicle.get(vehicle_id) or []
            assigned = repaired.get(vehicle_id)
            if not assigned or assigned not in ranked or not ranked:
                continue
            assigned_rank = ranked.index(assigned) + 1
            nearest = ranked[0]
            assigned_d = distance_cache.get((vehicle_id, assigned), float("inf"))
            nearest_d = distance_cache.get((vehicle_id, nearest), float("inf"))
            if (
                assigned_rank <= rank_limit
                or not math.isfinite(assigned_d)
                or not math.isfinite(nearest_d)
                or assigned_d <= max(nearest_d, 1e-12) * ratio_limit * ratio_limit
            ):
                continue
            used_by_others = {node for vid, node in repaired.items() if vid != vehicle_id}
            replacement = next((node for node in ranked[:rank_limit] if node not in used_by_others), None)
            if replacement is None:
                replacement = nearest
                duplicate_fallback += 1
            repaired[vehicle_id] = replacement
            replaced += 1
            self.event_log.log(
                "rank1_locality_repaired",
                vehicle_id=vehicle_id,
                old_launch=assigned,
                old_rank=assigned_rank,
                new_launch=replacement,
                old_distance=round(math.sqrt(max(0.0, assigned_d)), 3),
                nearest_distance=round(math.sqrt(max(0.0, nearest_d)), 3),
                rank_limit=rank_limit,
                ratio_limit=ratio_limit,
            )
        return repaired, {"replaced": replaced, "duplicate_fallback": duplicate_fallback}

    def _hungarian_unique_rank1_match(
        self,
        vehicles: List[VehicleRuntime],
        launch_points: List[str],
        distance_cache: Dict[Tuple[str, str], float],
    ) -> Dict[str, str]:
        if not vehicles or len(launch_points) < len(vehicles):
            return {}
        n = len(vehicles)
        m = len(launch_points)
        vehicle_ids = [vehicle.vehicle_id for vehicle in vehicles]
        max_finite = 1.0
        for vehicle_id in vehicle_ids:
            for launch_node in launch_points:
                value = distance_cache.get((vehicle_id, launch_node), float("inf"))
                if math.isfinite(value):
                    max_finite = max(max_finite, abs(value))
        penalty = max_finite * 1_000_000.0

        u = [0.0] * (n + 1)
        v = [0.0] * (m + 1)
        p = [0] * (m + 1)
        way = [0] * (m + 1)

        for i in range(1, n + 1):
            p[0] = i
            j0 = 0
            minv = [float("inf")] * (m + 1)
            used = [False] * (m + 1)
            while True:
                used[j0] = True
                i0 = p[j0]
                delta = float("inf")
                j1 = 0
                vehicle_id = vehicle_ids[i0 - 1]
                for j in range(1, m + 1):
                    if used[j]:
                        continue
                    launch_node = launch_points[j - 1]
                    cost = distance_cache.get((vehicle_id, launch_node), float("inf"))
                    if not math.isfinite(cost):
                        cost = penalty
                    cur = cost - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
                if not math.isfinite(delta) or j1 == 0:
                    return {}
                for j in range(0, m + 1):
                    if used[j]:
                        u[p[j]] += delta
                        v[j] -= delta
                    else:
                        minv[j] -= delta
                j0 = j1
                if p[j0] == 0:
                    break
            while True:
                j1 = way[j0]
                p[j0] = p[j1]
                j0 = j1
                if j0 == 0:
                    break

        assignment: Dict[str, str] = {}
        for j in range(1, m + 1):
            if p[j] <= 0:
                continue
            vehicle_id = vehicle_ids[p[j] - 1]
            launch_node = launch_points[j - 1]
            if math.isfinite(distance_cache.get((vehicle_id, launch_node), float("inf"))):
                assignment[vehicle_id] = launch_node
        return assignment

    def _global_rank1_launch_assignment(
        self,
        vehicles: List[VehicleRuntime],
        launch_points: List[str],
        ranked_by_vehicle: Dict[str, List[str]],
        distance_cache: Dict[Tuple[str, str], float],
        initial_pool_count: int,
    ) -> Dict[str, str]:
        if not vehicles or not launch_points or len(launch_points) < len(vehicles):
            return {}

        pool_counts = [max(1, min(initial_pool_count, len(launch_points)))]
        while pool_counts[-1] < len(launch_points):
            nxt = min(len(launch_points), pool_counts[-1] * 2)
            if nxt == pool_counts[-1]:
                break
            pool_counts.append(nxt)

        for pool_count in pool_counts:
            assignment = self._fast_unique_rank1_match(
                vehicles=vehicles,
                ranked_by_vehicle=ranked_by_vehicle,
                distance_cache=distance_cache,
                pool_count=pool_count,
            )
            if len(assignment) == len(vehicles):
                return assignment

        assigned_launches: Set[str] = set()
        fallback: Dict[str, str] = {}
        for vehicle in vehicles:
            for launch_node in ranked_by_vehicle.get(vehicle.vehicle_id, []):
                if launch_node in assigned_launches:
                    continue
                fallback[vehicle.vehicle_id] = launch_node
                assigned_launches.add(launch_node)
                break
        return fallback

    def _fast_unique_rank1_match(
        self,
        vehicles: List[VehicleRuntime],
        ranked_by_vehicle: Dict[str, List[str]],
        distance_cache: Dict[Tuple[str, str], float],
        pool_count: int,
    ) -> Dict[str, str]:
        vehicle_ids = [vehicle.vehicle_id for vehicle in vehicles]
        candidates_by_vehicle: Dict[str, List[str]] = {}
        launch_nodes: Set[str] = set()
        pair_rows: List[Tuple[float, str, str]] = []
        for vehicle_id in vehicle_ids:
            candidates = [
                launch_node
                for launch_node in ranked_by_vehicle.get(vehicle_id, [])[:pool_count]
                if math.isfinite(distance_cache.get((vehicle_id, launch_node), float("inf")))
            ]
            candidates_by_vehicle[vehicle_id] = candidates
            for launch_node in candidates:
                launch_nodes.add(launch_node)
                pair_rows.append((distance_cache[(vehicle_id, launch_node)], vehicle_id, launch_node))
        if len(launch_nodes) < len(vehicle_ids):
            return {}

        assignment: Dict[str, str] = {}
        owner_by_launch: Dict[str, str] = {}
        for _, vehicle_id, launch_node in sorted(pair_rows, key=lambda item: (item[0], item[1], item[2])):
            if vehicle_id in assignment or launch_node in owner_by_launch:
                continue
            assignment[vehicle_id] = launch_node
            owner_by_launch[launch_node] = vehicle_id

        def augment(vehicle_id: str, seen_launches: Set[str]) -> bool:
            for launch_node in candidates_by_vehicle.get(vehicle_id, []):
                if launch_node in seen_launches:
                    continue
                seen_launches.add(launch_node)
                owner = owner_by_launch.get(launch_node)
                if owner is None or augment(owner, seen_launches):
                    if owner is not None:
                        assignment.pop(owner, None)
                    assignment[vehicle_id] = launch_node
                    owner_by_launch[launch_node] = vehicle_id
                    return True
            return False

        for vehicle_id in vehicle_ids:
            if vehicle_id not in assignment:
                augment(vehicle_id, set())
        return assignment if len(assignment) == len(vehicle_ids) else {}
#隐蔽点选择
    def _candidate_hide_points_for_route(
        self,
        start_node: str,
        launch_node: str,
        candidate_count: int = 4,
        preferred_hide: str = "",
        reserved_hide_points: Optional[Set[str]] = None,
        allowed_hide_points: Optional[List[str]] = None,
    ) -> List[dict]:
        start = self.graph.nodes.get(start_node)
        launch = self.graph.nodes.get(launch_node)
        if start is None or launch is None:
            return []
        direct_dist = max(1.0, math.hypot(start.x - launch.x, start.y - launch.y))
        ranked: List[Tuple[float, float, float, str]] = []
        hide_pool = allowed_hide_points if allowed_hide_points is not None else self.points.hide_points
        for hide_node in hide_pool:
            hide = self.graph.nodes.get(hide_node)
            if hide is None:
                continue
            to_hide = math.hypot(start.x - hide.x, start.y - hide.y)
            to_launch = math.hypot(hide.x - launch.x, hide.y - launch.y)
            total_dist = to_hide + to_launch
            detour_ratio = total_dist / direct_dist
            corridor_dist = self._point_segment_distance(
                hide.x,
                hide.y,
                start.x,
                start.y,
                launch.x,
                launch.y,
            )
            ranked.append((detour_ratio, corridor_dist, to_launch, hide_node))
        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        reserved_hide_points = reserved_hide_points or set()
        ordered = []
        if preferred_hide and preferred_hide in hide_pool:
            ordered.append(preferred_hide)
        for _ratio, _corridor, _to_launch, hide_node in ranked:
            if hide_node not in ordered and hide_node not in reserved_hide_points:
                ordered.append(hide_node)
        for _ratio, _corridor, _to_launch, hide_node in ranked:
            if hide_node not in ordered:
                ordered.append(hide_node)
        out: List[dict] = []
        for hide_node in ordered[: max(1, candidate_count)]:
            row = self.external_hide_point_rows_by_node.get(hide_node, {})
            node = self.graph.nodes.get(hide_node)
            public_id = row.get("id") or row.get("point_id") or row.get("name") or row.get("index") or hide_node
            item = {
                "hide_node": hide_node,
                "node_id": hide_node,
                "hide_point_id": str(public_id),
                "point_id": str(public_id),
                "name": row.get("name") or str(public_id),
                "index": row.get("index"),
                "lon": row.get("lon", row.get("longitude", row.get("platform_LocationLLA_Lon"))),
                "lat": row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat"))),
                "mapped_lon": node.lon if node else None,
                "mapped_lat": node.lat if node else None,
                "projection_from_node": row.get("projection_from_node"),
                "projection_to_node": row.get("projection_to_node"),
                "projection_ratio": row.get("projection_ratio"),
                "projected_x": row.get("projected_x"),
                "projected_y": row.get("projected_y"),
            }
            out.append({key: value for key, value in item.items() if value is not None})
        return out

    @staticmethod
    def _point_segment_distance(
        px: float,
        py: float,
        ax: float,
        ay: float,
        bx: float,
        by: float,
    ) -> float:
        dx = bx - ax
        dy = by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            return math.hypot(px - ax, py - ay)
        ratio = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        return math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy))

    def _external_hide_public_id(self, hide_node: str) -> str:
        if not hide_node:
            return ""
        row = self.external_hide_point_rows_by_node.get(hide_node, {})
        return str(row.get("id") or row.get("point_id") or row.get("name") or row.get("index") or hide_node)

    def _candidate_launch_point_payload(self, launch_node: str, rank: int) -> dict:
        payload = {
            "rank": rank,
            "launch_node": launch_node,
            "node_id": launch_node,
            "fire_point_id": self._external_launch_public_id(launch_node),
        }
        row = self.external_launch_point_rows_by_node.get(launch_node, {})
        for key in ("name", "index", "lon", "lat", "alt", "type", "side"):
            if row.get(key) is not None:
                payload[key] = row.get(key)
        for key in ("projection_from_node", "projection_to_node", "projection_ratio", "projected_x", "projected_y"):
            if row.get(key) is not None:
                payload[key] = row.get(key)
        node = self.graph.nodes.get(launch_node)
        if node is not None:
            if payload.get("lon") is None and node.lon is not None:
                payload["lon"] = node.lon
            if payload.get("lat") is None and node.lat is not None:
                payload["lat"] = node.lat
            if payload.get("alt") is None:
                payload["alt"] = node.alt
            payload["mapped_lon"] = node.lon
            payload["mapped_lat"] = node.lat
        return payload

    def _schedule_loop(self) -> None:
        while self._running:
            with self._lock:
                try:
                    self._drain_heartbeats_locked()
                except Exception as exc:
                    self.event_log.log(
                        "schedule_loop_error",
                        error=str(exc),
                        traceback=traceback.format_exc(limit=8),
                    )
                    print(f"[Scheduler] schedule loop error: {exc}")
            time.sleep(self.tick_sec)

    def _status_loop(self) -> None:
        while self._running:
            time.sleep(2.0)
            with self._lock:
                self._drain_heartbeats_locked()
                pending = len([s for s in self.pending_subtasks if self.subtasks[s].status == "PENDING"])
                queued = len([s for s in self.queued_subtasks if self.subtasks.get(s) and self.subtasks[s].status == "PENDING"])
                running = len([s for s in self.subtasks.values() if s.status not in {"PENDING", "DONE", "FAILED"}])
                online = len([v for v in self.vehicles.values() if self._is_vehicle_online(v)])
                lane_stats = self.lane_graph.stats()
                conflict_stats = self._conflict_stats()
                print(
                    f"[Scheduler] vehicles={len(self.vehicles)} online={online} "
                    f"pending={pending} queued={queued} running={running} "
                    f"cache_hit={lane_stats['cache_hit_rate']:.2f} deadlock={self.metrics['deadlock_risk']}"
                )
                now_ts = time.time()
                if now_ts - self._last_metrics_log_ts >= 10.0:
                    self._last_metrics_log_ts = now_ts
                    self.event_log.log(
                        "scheduler_metrics",
                        vehicles_total=len(self.vehicles),
                        vehicles_online=online,
                        subtasks_pending=pending,
                        subtasks_queued=queued,
                        subtasks_running=running,
                        metrics=dict(self.metrics),
                        lane_graph=lane_stats,
                        conflicts=conflict_stats,
                    )

    def _is_vehicle_online(self, v: VehicleRuntime) -> bool:
        if not v.last_seen:
            return False
        try:
            dt = parse_iso_time(v.last_seen)
        except Exception:
            return False
        return (now_utc() - dt).total_seconds() <= self.heartbeat_timeout_sec























    def get_dashboard_state(self) -> dict:
        with self._lock:
            planning_settled_statuses = {"DONE", "FAILED", "ENROUTE_FIRE", "ENROUTE_DEPOT", "ENROUTE_RETURN", "RELOADING", "WAIT_DEPOT_QUEUE"}
            nodes = [
                {"id": node.node_id, "x": node.x, "y": node.y, "kind": node.kind}
                for node in self.graph.nodes.values()
            ]
            edges = []
            seen = set()
            for meta in self.graph.edge_meta.values():
                if meta.edge_id in seen:
                    continue
                seen.add(meta.edge_id)
                edges.append(
                    {
                        "id": meta.edge_id,
                        "from": meta.src,
                        "to": meta.dst,
                        "width": meta.width,
                        "lanes_forward": meta.lanes_forward,
                        "lanes_backward": meta.lanes_backward,
                        "speed_limit_mps": meta.speed_limit_mps,
                        "curvature": meta.curvature,
                        "geometry": meta.geometry,
                    }
                )

            vehicles = []
            for v in sorted(self.vehicles.values(), key=lambda x: x.vehicle_id):
                vehicles.append(
                    {
                        "vehicle_id": v.vehicle_id,
                        "status": v.status,
                        "current_node": v.current_node,
                        "busy": v.busy,
                        "online": self._is_vehicle_online(v),
                        "last_seen": v.last_seen,
                        "endpoint": f"{v.endpoint[0]}:{v.endpoint[1]}" if v.endpoint else "n/a",
                        "home_node": v.home_node,
                        "ammo_types": sorted(v.ammo_types),
                        "speed_mps": v.speed_mps,
                        "realtime_scale": v.realtime_scale,
                        "kinematics": dict(v.kinematics or {}),
                        "active_subtask_id": v.active_subtask_id,
                        "active_plan": self.active_plans.get(v.vehicle_id),
                    }
                )

            subtasks = []
            for st in sorted(self.subtasks.values(), key=lambda x: x.subtask_id):
                subtasks.append(
                    {
                        "subtask_id": st.subtask_id,
                        "task_id": st.task_id,
                        "ammo_type": st.ammo_type,
                        "fire_time": st.fire_time,
                        "status": st.status,
                        "phase": st.phase,
                        "assigned_vehicle": st.assigned_vehicle,
                        "assigned_launch_point": st.assigned_launch_point,
                        "depot_node": st.depot_node,
                        "assignment_mode": st.assignment_mode,
                        "redundant": st.is_redundant,
                        "redundant_for_subtask_id": st.redundant_for_subtask_id,
                        "score_breakdown": dict(st.score_breakdown or {}),
                    }
                )
            mission_subtasks = [st for st in self.subtasks.values() if not st.is_redundant]
            redundant_subtasks = [st for st in self.subtasks.values() if st.is_redundant]
            dispatch_pending_statuses = {"PENDING", "WAIT_VEHICLE_READY"}

            return {
                "server_time": utc_now_iso(),
                "map": {"nodes": nodes, "edges": edges},
                "points": {
                    "hide_points": list(self.points.hide_points),
                    "launch_points": list(self.points.launch_points),
                    "depots": list(self.points.depots),
                },
                "vehicles": vehicles,
                "subtasks": subtasks,
                "recent_events": list(self.recent_events[-20:]),
                "metrics": {
                    "vehicles_total": len(self.vehicles),
                    "vehicles_online": len([v for v in self.vehicles.values() if self._is_vehicle_online(v)]),
                    "subtasks_total": len(self.subtasks),
                    "mission_tasks_total": len(mission_subtasks),
                    "redundant_tasks_total": len(redundant_subtasks),
                    "subtasks_pending": len([st for st in self.subtasks.values() if st.status == "PENDING"]),
                    "subtasks_pending_active": len([s for s in self.pending_subtasks if self.subtasks[s].status == "PENDING"]),
                    "subtasks_queued": len([s for s in self.queued_subtasks if self.subtasks.get(s) and self.subtasks[s].status == "PENDING"]),
                    "subtasks_running": len([s for s in self.subtasks.values() if s.status not in {"PENDING", "DONE", "FAILED"}]),
                    "mission_tasks_pending": len([st for st in mission_subtasks if st.status == "PENDING"]),
                    "mission_tasks_pending_active": len([s for s in self.pending_subtasks if self.subtasks[s].status == "PENDING" and not self.subtasks[s].is_redundant]),
                    "mission_tasks_queued": len([s for s in self.queued_subtasks if self.subtasks.get(s) and self.subtasks[s].status == "PENDING" and not self.subtasks[s].is_redundant]),
                    "mission_tasks_running": len([st for st in mission_subtasks if st.status not in {"PENDING", "DONE", "FAILED"}]),
                    "mission_tasks_planning_pending": len([st for st in mission_subtasks if st.status not in planning_settled_statuses]),
                    "mission_tasks_dispatch_pending": len([st for st in mission_subtasks if st.status in dispatch_pending_statuses]),
                    "redundant_tasks_pending": len([st for st in redundant_subtasks if st.status == "PENDING"]),
                    "redundant_tasks_pending_active": len([s for s in self.pending_subtasks if self.subtasks[s].status == "PENDING" and self.subtasks[s].is_redundant]),
                    "redundant_tasks_queued": len([s for s in self.queued_subtasks if self.subtasks.get(s) and self.subtasks[s].status == "PENDING" and self.subtasks[s].is_redundant]),
                    "redundant_tasks_running": len([st for st in redundant_subtasks if st.status not in {"PENDING", "DONE", "FAILED"}]),
                    "redundant_tasks_planning_pending": len([st for st in redundant_subtasks if st.status not in planning_settled_statuses]),
                    "redundant_tasks_dispatch_pending": len([st for st in redundant_subtasks if st.status in dispatch_pending_statuses]),
                    "deadlock_risk": self.metrics["deadlock_risk"],
                    "path_rejected": self.metrics["path_rejected"],
                    "proposal_retries": self.metrics["proposal_retries"],
                    "deferred_assignments": self.metrics["deferred_assignments"],
                    "direct_reassign_after_reload": self.metrics["direct_reassign_after_reload"],
                },
                "fire_zone": dict(self.fire_zone_cfg),
                "lane_graph": self.lane_graph.stats(),
                "conflicts": self._conflict_stats(),
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="MVS Scheduler")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    app = SchedulerApp(args.config)
    app.start()
    print(f"[Scheduler] started at {app.listen_host}:{app.listen_port}")
    print(f"[Scheduler] dashboard: http://{app.dashboard_host}:{app.dashboard_port}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == "__main__":
    main()

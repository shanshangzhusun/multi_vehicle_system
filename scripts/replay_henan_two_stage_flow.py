#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mvs.common.models import Envelope, new_msg_id, utc_now_iso
from mvs.common.platform_interfaces import (
    MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
    MSG_DEPOT_VEHICLE_SCORE_RESULT,
    MSG_VEHICLE_DEPOT_ASSIGNMENT,
    MSG_VEHICLE_POST_FIRE_PATH_RESULT,
)
from mvs.depot import depot_app as depot_module
from mvs.depot.depot_app import DepotApp
from mvs.scheduler import scheduler_app as scheduler_module
from mvs.vehicle import vehicle_app as vehicle_module
from mvs.vehicle.vehicle_app import VehicleApp
from scripts.replay_conflict_resolution import (
    OfflineTransport,
    prepare_offline_scheduler,
    read_json,
    selected_rows,
    task_id_from_event_log,
)


def envelope(msg_type: str, payload: Dict[str, Any], sender: str = "offline_replay") -> Envelope:
    return Envelope(
        msg_id=new_msg_id(),
        msg_type=msg_type,
        sender=sender,
        target="",
        created_at=utc_now_iso(),
        require_ack=False,
        ack_for=None,
        payload=dict(payload),
    )


def latest_candidate_dir(root: Path) -> Path:
    candidates = sorted(path.parent for path in root.glob("*/replay_summary.json"))
    if not candidates:
        raise FileNotFoundError(f"no replay_summary.json below {root}")
    return candidates[-1]


def load_flat_candidates(candidate_dir: Path, task_id: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for path in sorted(candidate_dir.glob("*_VEHICLE_CANDIDATE_PATH_RESULT.json")):
        capture = read_json(path)
        payload = capture.get("payload")
        if not isinstance(payload, dict) or str(payload.get("task_id") or "") != task_id:
            continue
        vehicle_id = str(payload.get("vehicle_id") or payload.get("port") or "")
        if vehicle_id:
            found[vehicle_id] = dict(payload)
    return found


def point_captures(recv_dir: Path) -> List[Dict[str, Any]]:
    wanted = {"FA_SHE_DIAN", "YIN_BI_DIAN", "VEHICLE_DIAN", "ZHU_BEI_DIAN", "DEPOT_DIAN"}
    rows: List[Dict[str, Any]] = []
    for path in sorted(recv_dir.glob("*.json")):
        row = read_json(path)
        if str(row.get("msg_type") or "") in wanted:
            rows.append(row)
    return rows


def offline_config(source: Path, out_dir: Path, label: str) -> Path:
    cfg = read_json(source)
    cfg["message_capture"] = {"enabled": False}
    cfg["received_dian_capture"] = {"enabled": False}
    cfg["manual_debug"] = {"enabled": False}
    cfg["event_log_path"] = str(out_dir / f"{label}_events.jsonl")
    cfg["timing_log_path"] = str(out_dir / f"{label}_timing.jsonl")
    path = out_dir / f"{label}_config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def capture_address(row: Dict[str, Any]) -> Tuple[str, int]:
    text = str(row.get("addr") or "127.0.0.1:0")
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port or 0)


def haversine_m(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    lon1, lat1 = math.radians(float(a["lon"])), math.radians(float(a["lat"]))
    lon2, lat2 = math.radians(float(b["lon"])), math.radians(float(b["lat"]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 6371000.0 * 2.0 * math.asin(min(1.0, math.sqrt(value)))


def apply_scheduler_points(scheduler: Any, captures: Iterable[Dict[str, Any]]) -> None:
    for row in captures:
        scheduler._on_dian_context(
            envelope(str(row.get("msg_type") or ""), dict(row.get("payload") or {}))
        )


def run_depot_scores(
    contexts: List[Dict[str, Any]],
    depot_config_dir: Path,
    point_rows: List[Dict[str, Any]],
    output_dir: Path,
    shared_graph: Any,
) -> List[Dict[str, Any]]:
    depot_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    depot_module.MapLoader.load_graph_from_config = lambda _cfg: shared_graph
    captures_by_port = {
        int(item["addr"][1]): item
        for item in contexts
        if item.get("addr") and int(item["addr"][1]) > 0
    }
    results: List[Dict[str, Any]] = []
    for depot_port in sorted(captures_by_port):
        source = depot_config_dir / f"{depot_port}.json"
        app = DepotApp(str(offline_config(source, output_dir, f"depot_{depot_port}")))
        sent: List[Tuple[str, Dict[str, Any]]] = []
        app._send_raw_json = lambda msg_type, payload, _addr: sent.append((str(msg_type), dict(payload)))
        for row in point_rows:
            if str(row.get("msg_type") or "") in {"FA_SHE_DIAN", "ZHU_BEI_DIAN", "DEPOT_DIAN"}:
                app.on_message(
                    envelope(str(row.get("msg_type") or ""), dict(row.get("payload") or {})),
                    capture_address(row),
                )
        context = captures_by_port[depot_port]["payload"]["data"]
        app.on_message(envelope(MSG_DEPOT_VEHICLE_SCORE_CONTEXT, context), ("127.0.0.1", 9120))
        matches = [payload for msg_type, payload in sent if msg_type == MSG_DEPOT_VEHICLE_SCORE_RESULT]
        if not matches:
            raise RuntimeError(f"depot {depot_port} produced no vehicle score result")
        results.append(matches[-1])
        print(f"[henan-replay] depot={depot_port} scores={len(matches[-1].get('scores') or [])}", flush=True)
    return results


def run_post_fire_paths(
    assignments: List[Dict[str, Any]],
    vehicle_config_dir: Path,
    vehicle_capture_root: Path,
    output_dir: Path,
    shared_graph: Any,
) -> List[Dict[str, Any]]:
    vehicle_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    vehicle_module.MapLoader.load_graph_from_config = lambda _cfg: shared_graph
    results: List[Dict[str, Any]] = []
    for index, item in enumerate(sorted(assignments, key=lambda row: str(row["payload"]["data"].get("vehicle_id")))):
        assignment = dict(item["payload"]["data"])
        # Scheduler and vehicle processes project the same public depot into
        # process-local node IDs.  A shared graph is used only to keep this
        # offline replay memory-bounded, so do not leak the scheduler's local
        # projection ID into the vehicle-side resolver.
        assignment["depot"] = dict(assignment.get("depot") or {})
        assignment["depot"].pop("depot_node", None)
        assignment.pop("depot_node", None)
        vehicle_id = str(assignment.get("vehicle_id") or "")
        app = VehicleApp(
            str(offline_config(vehicle_config_dir / f"{vehicle_id}.json", output_dir, f"vehicle_{vehicle_id}"))
        )
        sent: List[Tuple[str, Dict[str, Any]]] = []
        app._send_raw_json = lambda msg_type, payload, _addr: sent.append((str(msg_type), dict(payload)))
        recv_dir = vehicle_capture_root / vehicle_id / "recv"
        for path in sorted(recv_dir.glob("*.json")):
            row = read_json(path)
            if str(row.get("msg_type") or "") not in {
                "FA_SHE_DIAN",
                "YIN_BI_DIAN",
                "ZHU_BEI_DIAN",
                "DEPOT_DIAN",
                "VEHICLE_CONTEXT",
                "VEHICLE_STATE_RESULT",
            }:
                continue
            app.on_message(
                envelope(str(row.get("msg_type") or ""), dict(row.get("payload") or {})),
                capture_address(row),
            )
        app.on_message(envelope(MSG_VEHICLE_DEPOT_ASSIGNMENT, assignment), ("127.0.0.1", 9120))
        matches = [payload for msg_type, payload in sent if msg_type == MSG_VEHICLE_POST_FIRE_PATH_RESULT]
        if not matches:
            raise RuntimeError(f"vehicle {vehicle_id} produced no post-fire path")
        results.append(matches[-1])
        if (index + 1) % 8 == 0 or index + 1 == len(assignments):
            print(f"[henan-replay] post-fire planned={index + 1}/{len(assignments)}", flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay the full two-stage depot flow for the Henan theater.")
    parser.add_argument("--scheduler-config", type=Path, default=Path("result/theaters/henan/configs/scheduler_debug.json"))
    parser.add_argument("--vehicle-config-dir", type=Path, default=Path("result/theaters/henan/configs/vehicles"))
    parser.add_argument("--depot-config-dir", type=Path, default=Path("result/configs/depots"))
    parser.add_argument("--event-log", type=Path, default=Path("backups/pre_prepare_20260720_214900/logs/scheduler_001_events.jsonl"))
    parser.add_argument("--scheduler-recv-dir", type=Path, default=Path("backups/pre_prepare_20260720_214900/result/message_capture/scheduler/scheduler_001/recv"))
    parser.add_argument("--vehicle-capture-root", type=Path, default=Path("backups/pre_prepare_20260720_214900/result/message_capture/vehicle"))
    parser.add_argument("--candidate-root", type=Path, default=Path("backups/sequential_replay_20260720_223542/sequential_vehicle_replay/henan"))
    parser.add_argument("--task-id", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("result/henan_two_stage_replay"))
    args = parser.parse_args()

    task_id = args.task_id or task_id_from_event_log(args.event_log)
    selected = selected_rows(args.event_log, task_id)
    candidate_dir = latest_candidate_dir(args.candidate_root)
    candidates = load_flat_candidates(candidate_dir, task_id)
    missing = sorted({vehicle_id for _sid, vehicle_id in selected} - set(candidates))
    if missing:
        raise SystemExit(f"missing selected candidates: {', '.join(missing)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.out_dir / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    scheduler_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    scheduler = prepare_offline_scheduler(
        args.scheduler_config,
        args.event_log,
        output_dir,
        task_id,
        selected,
        candidates,
        [],
    )
    points = point_captures(args.scheduler_recv_dir)
    apply_scheduler_points(scheduler, points)

    outgoing: List[Dict[str, Any]] = []
    scheduler._send_raw_json_callback = lambda addr, payload, event_name, **fields: outgoing.append(
        {"addr": addr, "payload": dict(payload), "event": event_name, "fields": fields}
    )
    started = time.perf_counter()
    if not scheduler._try_resolve_external_dispatch_for_task(task_id):
        raise RuntimeError("pre-launch conflict resolution failed")
    prelaunch_sec = time.perf_counter() - started
    contexts = [row for row in outgoing if row["payload"].get("msg_type") == MSG_DEPOT_VEHICLE_SCORE_CONTEXT]
    if not contexts:
        raise RuntimeError("scheduler produced no depot score contexts")

    depot_started = time.perf_counter()
    depot_scores = run_depot_scores(contexts, args.depot_config_dir, points, output_dir, scheduler.graph)
    (output_dir / "depot_score_results.json").write_text(
        json.dumps(depot_scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    outgoing.clear()
    for result in depot_scores:
        scheduler._on_depot_vehicle_score_result(envelope(MSG_DEPOT_VEHICLE_SCORE_RESULT, result, "offline_depot"))
    depot_sec = time.perf_counter() - depot_started
    assignments = [row for row in outgoing if row["payload"].get("msg_type") == MSG_VEHICLE_DEPOT_ASSIGNMENT]
    if len(assignments) != len(selected):
        raise RuntimeError(f"scheduler produced {len(assignments)} assignments for {len(selected)} selected vehicles")
    (output_dir / "depot_assignments.json").write_text(
        json.dumps([row["payload"]["data"] for row in assignments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    post_plan_started = time.perf_counter()
    post_paths = run_post_fire_paths(
        assignments,
        args.vehicle_config_dir,
        args.vehicle_capture_root,
        output_dir,
        scheduler.graph,
    )
    post_plan_sec = time.perf_counter() - post_plan_started
    post_resolve_started = time.perf_counter()
    for result in post_paths:
        scheduler._on_vehicle_post_fire_path_result(
            envelope(MSG_VEHICLE_POST_FIRE_PATH_RESULT, result, str(result.get("vehicle_id") or ""))
        )
    post_resolve_sec = time.perf_counter() - post_resolve_started

    bundle = scheduler.resolved_dispatch_trajectory_bundles.get(task_id)
    if not bundle:
        state = scheduler.prelaunch_dispatch_states.get(task_id, {})
        raise RuntimeError(f"two-stage flow did not finalize; state={state.get('status')}")
    trajectories = list(bundle.get("trajectories") or [])
    depot_counts = Counter(str(row.get("depot_port") or row.get("depot_id") or "") for row in trajectories)
    return_errors = []
    for row in trajectories:
        path = list(row.get("path_points") or [])
        if len(path) < 2:
            return_errors.append({"vehicle_id": row.get("vehicle_id"), "reason": "path_too_short"})
            continue
        error_m = haversine_m(path[0], path[-1])
        if error_m > 50.0:
            return_errors.append({"vehicle_id": row.get("vehicle_id"), "return_error_m": round(error_m, 3)})

    report = {
        "generated_at": utc_now_iso(),
        "task_id": task_id,
        "candidate_dir": str(candidate_dir),
        "candidate_vehicle_count": len(candidates),
        "selected_vehicle_count": len(selected),
        "depot_context_count": len(contexts),
        "depot_score_result_count": len(depot_scores),
        "assignment_count": len(assignments),
        "post_fire_path_count": len(post_paths),
        "trajectory_count": len(trajectories),
        "depot_counts": dict(sorted(depot_counts.items())),
        "return_error_over_50m": return_errors,
        "prelaunch_conflict_audit": bundle.get("prelaunch_conflict_audit"),
        "final_conflict_audit": bundle.get("conflict_audit"),
        "timing_sec": {
            "prelaunch_conflict_resolution": round(prelaunch_sec, 6),
            "depot_scoring_and_assignment": round(depot_sec, 6),
            "vehicle_post_fire_planning": round(post_plan_sec, 6),
            "post_fire_conflict_resolution": round(post_resolve_sec, 6),
            "total": round(time.perf_counter() - started, 6),
        },
        "ok": (
            len(trajectories) == len(selected)
            and len(post_paths) == len(selected)
            and not return_errors
            and bool((bundle.get("conflict_audit") or {}).get("ok"))
        ),
    }
    (output_dir / f"{task_id}_dispatch_trajectory_bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "post_fire_path_results.json").write_text(
        json.dumps(post_paths, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = output_dir / "validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[henan-replay] output={output_dir}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mvs.common.event_log import EventLogger
from mvs.scheduler import scheduler_app as scheduler_module
from mvs.scheduler.scheduler_app import SchedulerApp, SubTask


class OfflineTransport:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("offline conflict replay cannot send messages")


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[replay] skip invalid JSONL {path}:{line_no}: {exc}", file=sys.stderr)
                continue
            if isinstance(value, dict):
                yield value


def task_id_from_event_log(path: Path) -> str:
    resolved = [
        str(row.get("task_id") or "")
        for row in read_jsonl(path)
        if row.get("event") == "external_dispatch_resolved" and row.get("task_id")
    ]
    if resolved:
        return resolved[-1]
    for row in read_jsonl(path):
        subtask_id = str(row.get("subtask_id") or "")
        if row.get("event") == "selected_vehicle_result_received" and "_s" in subtask_id:
            return subtask_id.rsplit("_s", 1)[0]
    raise ValueError(f"cannot determine task_id from {path}")


def selected_rows(path: Path, task_id: str) -> List[Tuple[str, str]]:
    latest: Dict[str, str] = {}
    prefix = f"{task_id}_s"
    for row in read_jsonl(path):
        if row.get("event") != "selected_vehicle_result_received":
            continue
        subtask_id = str(row.get("subtask_id") or "")
        vehicle_id = str(row.get("vehicle_id") or "")
        if subtask_id.startswith(prefix) and vehicle_id:
            latest[subtask_id] = vehicle_id
    return sorted(latest.items(), key=lambda item: item[0])


def candidate_payloads(capture_root: Path, task_id: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Tuple[Tuple[float, str], Dict[str, Any]]] = {}
    pattern = "vehicle/*/send/*_VEHICLE_CANDIDATE_PATH_RESULT.json"
    for path in capture_root.glob(pattern):
        try:
            capture = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[replay] skip invalid candidate {path}: {exc}", file=sys.stderr)
            continue
        payload = capture.get("payload")
        if not isinstance(payload, dict) or str(payload.get("task_id") or "") != task_id:
            continue
        vehicle_id = str(payload.get("vehicle_id") or payload.get("port") or capture.get("node_id") or "")
        if not vehicle_id:
            continue
        order_key = (path.stat().st_mtime, path.name)
        previous = found.get(vehicle_id)
        if previous is None or order_key >= previous[0]:
            found[vehicle_id] = (order_key, dict(payload))
    return {vehicle_id: payload for vehicle_id, (_order_key, payload) in found.items()}


def selected_depot_ids(capture_root: Path, scheduler_id: str) -> List[str]:
    selected: List[str] = []
    pattern = f"scheduler/{scheduler_id}/recv/*_SELECTED_DEPOT_RESULT.json"
    for path in sorted(capture_root.glob(pattern)):
        try:
            capture = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[replay] skip invalid depot selection {path}: {exc}", file=sys.stderr)
            continue
        payload = capture.get("payload")
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        values: List[Any]
        if isinstance(data, list):
            values = data
        else:
            values = [data if data is not None and data != "" else payload]
        for value in values:
            if isinstance(value, dict):
                depot_id = str(
                    value.get("depot_id")
                    or value.get("selected_depot")
                    or value.get("depot_node")
                    or value.get("depot_port")
                    or ""
                )
            else:
                depot_id = str(value or "").strip()
            if depot_id and depot_id not in selected:
                selected.append(depot_id)
    return selected


def prepare_offline_scheduler(
    config_path: Path,
    event_log_path: Path,
    output_dir: Path,
    task_id: str,
    selected: List[Tuple[str, str]],
    candidates: Dict[str, Dict[str, Any]],
    selected_depots: List[str],
) -> SchedulerApp:
    scheduler_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    scheduler = SchedulerApp(str(config_path))
    scheduler.model_callback_enabled = False
    scheduler.message_capture_enabled = False
    scheduler.event_log = EventLogger(
        path=str(output_dir / "conflict_replay_events.jsonl"),
        node_id=f"{scheduler.node_id}_offline_replay",
    )
    scheduler.timing_log_path = output_dir / "conflict_replay_timing.jsonl"

    task_rows = [row for row in read_jsonl(event_log_path) if row.get("event") == "task_received"]
    task_count = next(
        (
            int(row.get("launches") or 0) + int(row.get("redundant_launches") or 0)
            for row in reversed(task_rows)
            if str(row.get("task_id") or "") == task_id
        ),
        len(selected),
    )
    fire_time = next(
        (
            float(payload.get("paths", [{}])[0].get("path_points", [{}])[-1].get("time"))
            for payload in candidates.values()
            if payload.get("paths")
            and payload["paths"][0].get("path_points")
        ),
        0.0,
    )
    for index in range(task_count):
        subtask_id = f"{task_id}_s{index:03d}"
        scheduler.subtasks[subtask_id] = SubTask(
            subtask_id=subtask_id,
            task_id=task_id,
            ammo_type="HE",
            fire_time=SchedulerApp._sim_seconds_to_iso(fire_time),
            sim_fire_time=fire_time,
        )
        scheduler.queued_subtasks.append(subtask_id)

    scheduler.external_vehicle_selection = {
        subtask_id: [vehicle_id] for subtask_id, vehicle_id in selected
    }
    scheduler.external_vehicle_candidate_paths = dict(candidates)
    scheduler.external_depot_selection_by_task[task_id].update(selected_depots)
    return scheduler


def render_bundle(bundle: Path, config: Path, output: Path) -> None:
    command = [
        sys.executable,
        "scripts/render_latest_dispatch_routes.py",
        "--bundle",
        str(bundle),
        "--scheduler-config",
        str(config),
        "--received-dir",
        "result/received_dian",
        "--dian-dir",
        "result/extracted_dian",
        "--out",
        str(output),
        "--focus-routes-only",
    ]
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay scheduler conflict resolution from saved vehicle candidate paths."
    )
    parser.add_argument("--scheduler-config", required=True, type=Path)
    parser.add_argument("--event-log", required=True, type=Path)
    parser.add_argument("--capture-root", type=Path, default=Path("result/message_capture"))
    parser.add_argument("--task-id", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("result/conflict_replay"))
    parser.add_argument("--prioritize-vehicle", action="append", default=[])
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()

    task_id = args.task_id or task_id_from_event_log(args.event_log)
    selected = selected_rows(args.event_log, task_id)
    priority = {str(vehicle_id): index for index, vehicle_id in enumerate(args.prioritize_vehicle)}
    if priority:
        selected.sort(key=lambda item: (priority.get(item[1], len(priority)), item[0]))
    candidates = candidate_payloads(args.capture_root, task_id)
    selected_depots = selected_depot_ids(args.capture_root, Path(args.scheduler_config).stem)
    selected_vehicle_ids = {vehicle_id for _subtask_id, vehicle_id in selected}
    missing = sorted(selected_vehicle_ids - set(candidates))
    if missing:
        raise SystemExit(
            f"missing candidate paths for {len(missing)} selected vehicles: {', '.join(missing)}"
        )
    if not selected:
        raise SystemExit(f"no selected vehicles found for task {task_id}")

    output_dir = args.out_dir / Path(args.scheduler_config).stem
    output_dir.mkdir(parents=True, exist_ok=True)
    scheduler = prepare_offline_scheduler(
        args.scheduler_config,
        args.event_log,
        output_dir,
        task_id,
        selected,
        candidates,
        selected_depots,
    )
    for order, vehicle_id in enumerate(args.prioritize_vehicle):
        for subtask_id, selected_vehicle_id in selected:
            if selected_vehicle_id == str(vehicle_id):
                scheduler.subtasks[subtask_id].fire_time = SchedulerApp._sim_seconds_to_iso(-1000 + order)
    resolved = scheduler._try_resolve_external_dispatch_for_task(task_id)
    if not resolved and task_id in scheduler.selected_depot_wait_deadlines:
        # Offline replay should reproduce the scheduler's delayed retry without
        # sleeping for the real configured timeout.
        timer = scheduler.selected_depot_wait_timers.pop(task_id, None)
        if timer is not None:
            timer.cancel()
        scheduler.selected_depot_wait_deadlines[task_id] = time.monotonic() - 0.001
        resolved = scheduler._try_resolve_external_dispatch_for_task(task_id)
    if not resolved:
        raise SystemExit(f"conflict resolution did not complete for task {task_id}")

    bundle = scheduler.resolved_dispatch_trajectory_bundles[task_id]
    bundle_path = output_dir / f"{task_id}_dispatch_trajectory_bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[replay] task={task_id} selected={len(selected)} candidates={len(candidates)} "
        f"selected_depots={len(selected_depots)} resolved={bundle.get('resolved_count')} "
        f"unresolved={bundle.get('unresolved_count')}"
    )
    print(f"[replay] depot_pool={','.join(selected_depots) or '-'}")
    print(f"[replay] bundle={bundle_path}")

    if args.render:
        image_path = output_dir / f"{task_id}_dispatch_routes.png"
        render_bundle(bundle_path, args.scheduler_config, image_path)
        print(f"[replay] image={image_path}")


if __name__ == "__main__":
    main()

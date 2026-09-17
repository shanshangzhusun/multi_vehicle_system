#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mvs.common.models import Envelope, new_msg_id, utc_now_iso
from mvs.common.platform_interfaces import (
    MSG_FA_SHE_DIAN,
    MSG_VEHICLE_CANDIDATE_CONTEXT,
    MSG_VEHICLE_DIAN,
    MSG_YIN_BI_DIAN,
    MSG_ZHU_BEI_DIAN,
    MSG_ZHU_BEI_KU_DIAN,
)
from mvs.scheduler import scheduler_app as scheduler_module
from mvs.scheduler.scheduler_app import SchedulerApp
from scripts.replay_conflict_resolution import task_id_from_event_log


class OfflineTransport:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def capture_addr(value: Any) -> Tuple[str, int]:
    text = str(value or "127.0.0.1:0")
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port or 0)


def envelope_from_capture(row: Dict[str, Any]) -> Envelope:
    return Envelope(
        msg_id=new_msg_id(),
        msg_type=str(row.get("msg_type") or ""),
        sender=str(row.get("sender") or "offline_replay"),
        target=str(row.get("target") or ""),
        created_at=str(row.get("ts") or utc_now_iso()),
        require_ack=False,
        ack_for=None,
        payload=dict(row.get("payload") or {}),
    )


def iter_input_captures(recv_dir: Path) -> Iterable[Path]:
    allowed = {
        "TASK_PACKAGE",
        MSG_FA_SHE_DIAN,
        MSG_YIN_BI_DIAN,
        MSG_VEHICLE_DIAN,
        MSG_ZHU_BEI_DIAN,
        MSG_ZHU_BEI_KU_DIAN,
    }
    for path in sorted(recv_dir.glob("*.json")):
        if any(path.name.endswith(f"_{msg_type}.json") for msg_type in allowed):
            yield path


def latest_matching(paths: Iterable[Path], task_id: str = "") -> Optional[Path]:
    latest: Optional[Path] = None
    for path in sorted(paths):
        if not task_id:
            latest = path
            continue
        try:
            row = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if str(payload.get("task_id") or "") == task_id:
            latest = path
    return latest


def replace_with_backup(path: Path, value: Dict[str, Any], result_dir: Path, backup_dir: Path) -> None:
    relative = path.relative_to(result_dir)
    backup_path = backup_dir / relative
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    write_json(path, value)


def build_context(scheduler_config: Path, scheduler_recv_dir: Path, task_id: str) -> Dict[str, Any]:
    scheduler_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    scheduler = SchedulerApp(str(scheduler_config))
    scheduler.message_capture_enabled = False
    scheduler.model_callback_enabled = False
    for path in iter_input_captures(scheduler_recv_dir):
        row = read_json(path)
        scheduler.on_message(envelope_from_capture(row), capture_addr(row.get("addr")))
    ctx = scheduler._build_vehicle_scoring_context({"task_id": task_id})
    return {
        "schema": "vehicle_candidate_context_v1",
        "task_id": task_id,
        "vehicles": scheduler._compact_vehicle_candidate_launch_points(
            ctx.get("vehicle_candidate_launch_points", [])
        ),
    }


def context_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    vehicles = [row for row in payload.get("vehicles") or [] if isinstance(row, dict)]
    first_hides: List[str] = []
    hide_counts: List[int] = []
    path_options = 0
    for row in vehicles:
        candidates = [item for item in row.get("candidate_launch_points") or [] if isinstance(item, dict)]
        for candidate in candidates:
            hides = list(candidate.get("hide_candidates") or [])
            hide_counts.append(len(hides))
            path_options += len(hides) + (1 if bool(candidate.get("allow_direct", True)) else 0)
        if candidates:
            hides = list(candidates[0].get("hide_candidates") or [])
            if hides:
                first_hides.append(str(hides[0]))
    return {
        "vehicle_count": len(vehicles),
        "candidate_count_min": min((len(row.get("candidate_launch_points") or []) for row in vehicles), default=0),
        "candidate_count_max": max((len(row.get("candidate_launch_points") or []) for row in vehicles), default=0),
        "hide_count_min": min(hide_counts, default=0),
        "hide_count_max": max(hide_counts, default=0),
        "rank1_first_hide_count": len(first_hides),
        "rank1_first_hide_unique_count": len(set(first_hides)),
        "estimated_path_options": path_options,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild VEHICLE_CANDIDATE_CONTEXT from saved scheduler inputs and optionally replace captures."
    )
    parser.add_argument("--scheduler-config", required=True, type=Path)
    parser.add_argument("--scheduler-id", default="")
    parser.add_argument("--event-log", required=True, type=Path)
    parser.add_argument("--result-dir", type=Path, default=Path("result"))
    parser.add_argument("--capture-root", type=Path, default=Path("result/message_capture"))
    parser.add_argument("--task-id", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("result/rebuilt_candidate_context"))
    parser.add_argument("--replace-capture", action="store_true")
    args = parser.parse_args()

    scheduler_id = args.scheduler_id or args.scheduler_config.stem
    task_id = args.task_id or task_id_from_event_log(args.event_log)
    scheduler_recv_dir = args.capture_root / "scheduler" / scheduler_id / "recv"
    scheduler_send_dir = args.capture_root / "scheduler" / scheduler_id / "send"
    if not scheduler_recv_dir.exists():
        raise FileNotFoundError(f"scheduler recv capture dir not found: {scheduler_recv_dir}")

    payload = build_context(args.scheduler_config, scheduler_recv_dir, task_id)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.out_dir / scheduler_id / run_stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_path = output_dir / f"{task_id}_VEHICLE_CANDIDATE_CONTEXT.json"
    write_json(payload_path, payload)

    backup_dir: Optional[Path] = None
    replaced_vehicle_count = 0
    if args.replace_capture:
        backup_dir = args.result_dir / "capture_backups" / f"before_candidate_context_rebuild_{scheduler_id}_{run_stamp}"
        scheduler_send_path = latest_matching(
            scheduler_send_dir.glob("*_VEHICLE_CANDIDATE_CONTEXT.json"),
            task_id=task_id,
        )
        if scheduler_send_path is not None:
            row = read_json(scheduler_send_path)
            row["ts"] = utc_now_iso()
            row["payload"] = payload
            replace_with_backup(scheduler_send_path, row, args.result_dir, backup_dir)

        wrapped = {"msg_type": MSG_VEHICLE_CANDIDATE_CONTEXT, "data": payload}
        for vehicle in payload.get("vehicles") or []:
            if not isinstance(vehicle, dict):
                continue
            vehicle_id = str(vehicle.get("vehicle_id") or vehicle.get("vehicle_port") or "")
            if not vehicle_id:
                continue
            recv_path = latest_matching(
                (args.capture_root / "vehicle" / vehicle_id / "recv").glob(
                    "*_VEHICLE_CANDIDATE_CONTEXT.json"
                ),
                task_id=task_id,
            )
            if recv_path is None:
                continue
            row = read_json(recv_path)
            vehicle_payload = dict(payload)
            vehicle_payload["_raw_tcp_json"] = wrapped
            row["ts"] = utc_now_iso()
            row["payload"] = vehicle_payload
            replace_with_backup(recv_path, row, args.result_dir, backup_dir)
            replaced_vehicle_count += 1

    summary = {
        "generated_at": utc_now_iso(),
        "scheduler_id": scheduler_id,
        "task_id": task_id,
        "payload": str(payload_path),
        "replaced": bool(args.replace_capture),
        "replaced_vehicle_count": replaced_vehicle_count,
        "backup_dir": str(backup_dir) if backup_dir else None,
        **context_summary(payload),
    }
    summary_path = output_dir / "summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

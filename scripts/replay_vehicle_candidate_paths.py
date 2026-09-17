#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mvs.common.models import Envelope, new_msg_id, utc_now_iso
from mvs.vehicle import vehicle_app as vehicle_module
from mvs.vehicle.vehicle_app import VehicleApp


class OfflineTransport:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


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


def offline_config(source: Path, output_dir: Path, vehicle_id: str) -> Path:
    cfg = read_json(source)
    cfg["message_capture"] = {"enabled": False}
    cfg["received_dian_capture"] = {"enabled": False}
    cfg["manual_debug"] = {"enabled": False}
    cfg["event_log_path"] = str(output_dir / f"{vehicle_id}_events.jsonl")
    cfg["timing_log_path"] = str(output_dir / f"{vehicle_id}_timing.jsonl")
    path = output_dir / f"{vehicle_id}_config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def replay_vehicle(
    result_dir: Path,
    config_dir: Path,
    output_dir: Path,
    vehicle_id: str,
) -> Tuple[Dict[str, Any], Path]:
    recv_dir = result_dir / "message_capture" / "vehicle" / vehicle_id / "recv"
    recv_paths = sorted(recv_dir.glob("*.json"))
    if not recv_paths:
        raise FileNotFoundError(f"no received captures found for vehicle {vehicle_id}")

    config_path = config_dir / f"{vehicle_id}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"vehicle config not found: {config_path}")
    app = VehicleApp(str(offline_config(config_path, output_dir, vehicle_id)))
    sent: List[Tuple[str, Dict[str, Any], Tuple[str, int]]] = []

    def capture_send(msg_type: str, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        sent.append((str(msg_type), dict(payload), addr))

    app._send_raw_json = capture_send  # type: ignore[method-assign]
    for path in recv_paths:
        row = read_json(path)
        app.on_message(envelope_from_capture(row), capture_addr(row.get("addr")))

    candidates = [item for item in sent if item[0] == "VEHICLE_CANDIDATE_PATH_RESULT"]
    if not candidates:
        raise RuntimeError(f"vehicle {vehicle_id} replay did not produce candidate paths")
    payload = candidates[-1][1]
    if str(payload.get("vehicle_id") or payload.get("port") or "") != vehicle_id:
        raise RuntimeError(f"vehicle {vehicle_id} replay produced mismatched payload identity")
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise RuntimeError(f"vehicle {vehicle_id} replay produced no paths")

    old_paths = sorted(
        (result_dir / "message_capture" / "vehicle" / vehicle_id / "send").glob(
            "*_VEHICLE_CANDIDATE_PATH_RESULT.json"
        )
    )
    if not old_paths:
        raise FileNotFoundError(f"vehicle {vehicle_id} has no candidate capture to replace")
    old_path = old_paths[-1]
    old_capture = read_json(old_path)
    new_capture = dict(old_capture)
    new_capture.update(
        {
            "ts": utc_now_iso(),
            "node_type": "vehicle",
            "node_id": vehicle_id,
            "direction": "send",
            "msg_type": "VEHICLE_CANDIDATE_PATH_RESULT",
            "sender": vehicle_id,
            "payload": payload,
        }
    )
    generated_path = output_dir / f"{vehicle_id}_VEHICLE_CANDIDATE_PATH_RESULT.json"
    generated_path.write_text(json.dumps(new_capture, ensure_ascii=False, indent=2), encoding="utf-8")
    return new_capture, old_path


def rank_one_summary(capture: Dict[str, Any]) -> Dict[str, Any]:
    payload = capture.get("payload") or {}
    rank_one = next((row for row in payload.get("paths", []) if int(row.get("rank", -1)) == 1), None)
    return {
        "vehicle_id": str(payload.get("vehicle_id") or payload.get("port") or ""),
        "path_count": len(payload.get("paths") or []),
        "usable_path_count": int(payload.get("usable_path_count") or 0),
        "rejected_path_count": int(payload.get("rejected_path_count") or 0),
        "rank1_launch_node": rank_one.get("launch_node") if rank_one else None,
        "rank1_travel_seconds": rank_one.get("travel_seconds") if rank_one else None,
        "rank1_wait_seconds": rank_one.get("wait_seconds") if rank_one else None,
        "rank1_fire_time_error_sec": rank_one.get("fire_time_error_sec") if rank_one else None,
        "rank1_feasible": rank_one.get("feasible") if rank_one else None,
        "rank1_timing_strategy": rank_one.get("timing_strategy") if rank_one else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay vehicle candidate planning from saved receive captures.")
    parser.add_argument("--vehicles", nargs="+", default=["8564", "8565", "8566"])
    parser.add_argument("--result-dir", type=Path, default=Path("result"))
    parser.add_argument("--config-dir", type=Path, default=Path("result/configs/vehicles"))
    parser.add_argument("--out-dir", type=Path, default=Path("result/vehicle_candidate_replay"))
    parser.add_argument("--replace-capture", action="store_true")
    parser.add_argument(
        "--reuse-graph",
        action="store_true",
        help="Load one shared graph for this sequential replay batch.",
    )
    args = parser.parse_args()

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.out_dir / run_stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    vehicle_module.create_transport_node = lambda **_kwargs: OfflineTransport()
    if args.reuse_graph:
        original_loader = vehicle_module.MapLoader.load_graph_from_config
        shared_graph: Dict[str, Any] = {}

        def load_shared_graph(map_cfg: Dict[str, Any]):
            if "graph" not in shared_graph:
                shared_graph["graph"] = original_loader(map_cfg)
            return shared_graph["graph"]

        vehicle_module.MapLoader.load_graph_from_config = load_shared_graph

    generated: List[Tuple[str, Dict[str, Any], Path]] = []
    for vehicle_id in [str(value) for value in args.vehicles]:
        capture, old_path = replay_vehicle(args.result_dir, args.config_dir, output_dir, vehicle_id)
        generated.append((vehicle_id, capture, old_path))

    backup_dir: Optional[Path] = None
    if args.replace_capture:
        backup_dir = args.result_dir / "capture_backups" / f"before_speed80_replay_{run_stamp}"
        for vehicle_id, capture, old_path in generated:
            relative = old_path.relative_to(args.result_dir)
            backup_path = backup_dir / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, backup_path)
            old_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "generated_at": utc_now_iso(),
        "replaced": bool(args.replace_capture),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "vehicles": [rank_one_summary(capture) for _vehicle_id, capture, _old_path in generated],
    }
    summary_path = output_dir / "replay_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

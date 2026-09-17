#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIAN_DIR = ROOT / "result" / "extracted_dian"


def send_json(host: str, port: int, obj: Dict[str, Any], timeout: float = 2.0) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(data)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise SystemExit(f"JSON root must be an object: {path}")
    return obj


def command_task(args: argparse.Namespace) -> None:
    payload = {
        "msg_type": "TASK_PACKAGE",
        "data": {
            "task_id": args.task_id,
            "sim_time": float(args.sim_time),
            "fire_time": float(args.fire_time),
            "task_count": int(args.task_count),
        },
    }
    send_json(args.scheduler_host, args.scheduler_port, payload, args.timeout)
    print(
        f"sent TASK_PACKAGE task_id={args.task_id} task_count={args.task_count} "
        f"sim_time={args.sim_time} fire_time={args.fire_time} "
        f"to={args.scheduler_host}:{args.scheduler_port}"
    )


def command_dian(args: argparse.Namespace) -> None:
    paths = [
        Path(args.fa_she or DEFAULT_DIAN_DIR / "FA_SHE_DIAN.json"),
        Path(args.yin_bi or DEFAULT_DIAN_DIR / "YIN_BI_DIAN.json"),
        Path(args.depot or DEFAULT_DIAN_DIR / "DEPOT_DIAN.json"),
        Path(args.vehicle or DEFAULT_DIAN_DIR / "VEHICLE_DIAN.json"),
    ]
    for path in paths:
        if not path.exists():
            continue
        obj = load_json(path)
        send_json(args.scheduler_host, args.scheduler_port, obj, args.timeout)
        rows = obj.get("data")
        count = len(rows) if isinstance(rows, list) else (1 if isinstance(rows, dict) else 0)
        print(f"sent {obj.get('msg_type')} count={count} from={path}")


def _literal_obj_after(text: str, marker: str) -> Dict[str, Any] | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    raw = text[idx + len(marker) :].strip()
    try:
        value = ast.literal_eval(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _score_rows_from_json_line(line: str) -> Iterable[Tuple[str, float]]:
    try:
        obj = json.loads(line)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    if obj.get("event") == "vehicle_score_result_received":
        vid = obj.get("vehicle_id") or obj.get("port")
        score = obj.get("simplified_score") or obj.get("score_total")
        if vid is not None and score is not None:
            return [(str(vid), float(score))]
    if obj.get("msg_type") == "VEHICLE_SCORE_RESULT":
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        vid = data.get("port") or data.get("vehicle_id")
        score = data.get("score_total")
        if vid is not None and score is not None:
            return [(str(vid), float(score))]
    return []


def _score_rows_from_text_line(line: str) -> Iterable[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    obj = _literal_obj_after(line, "obj=") or _literal_obj_after(line, "payload=")
    if obj:
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        if isinstance(data, dict):
            vid = data.get("port") or data.get("vehicle_id")
            score = data.get("score_total")
            if vid is not None and score is not None:
                rows.append((str(vid), float(score)))
    score_match = re.search(r"score_total['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)", line)
    if score_match:
        score = float(score_match.group(1))
        vid_match = (
            re.search(r"\[Vehicle\s+(\d+)\]", line)
            or re.search(r"vehicle[_ ]id['\"]?\s*[:=]\s*['\"]?(\d+)", line)
            or re.search(r"port['\"]?\s*[:=]\s*['\"]?(\d+)", line)
        )
        if vid_match:
            rows.append((str(vid_match.group(1)), score))
    return rows


def read_scores(log_paths: List[Path]) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for path in log_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "score_total" not in line and "vehicle_score_result_received" not in line:
                    continue
                for vid, score in list(_score_rows_from_json_line(line)) + list(_score_rows_from_text_line(line)):
                    if not vid:
                        continue
                    scores[vid] = max(score, scores.get(vid, float("-inf")))
    return scores


def build_selection(task_id: str, scores: Dict[str, float], count: int) -> Dict[str, Any]:
    ranked = sorted(scores.items(), key=lambda item: (-item[1], int(float(item[0])) if item[0].isdigit() else item[0]))
    selected = ranked[:count]
    if len(selected) < count:
        raise SystemExit(f"not enough score rows: need={count} found={len(selected)}")
    rows = [
        {
            "subtask_id": f"{task_id}_s{idx:03d}",
            "vehicle_id": vehicle_id,
            "score_total": round(float(score), 3),
        }
        for idx, (vehicle_id, score) in enumerate(selected)
    ]
    return {"msg_type": "SELECTED_VEHICLE_RESULT", "data": rows}


def command_select(args: argparse.Namespace) -> None:
    log_paths = [Path(p) for p in args.logs]
    scores = read_scores(log_paths)
    payload = build_selection(args.task_id, scores, args.task_count)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote selected vehicles to={out}")
    if not args.no_send:
        send_json(args.scheduler_host, args.scheduler_port, payload, args.timeout)
        print(
            f"sent SELECTED_VEHICLE_RESULT count={len(payload['data'])} "
            f"to={args.scheduler_host}:{args.scheduler_port}"
        )
    top = payload["data"][:5]
    print(f"top5={top}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual helper for external-model scheduling flow tests.")
    parser.add_argument("--scheduler-host", default="127.0.0.8")
    parser.add_argument("--scheduler-port", type=int, default=9120)
    parser.add_argument("--timeout", type=float, default=2.0)
    sub = parser.add_subparsers(dest="cmd", required=True)

    task = sub.add_parser("task", help="Send a simple TASK_PACKAGE to scheduler.")
    task.add_argument("--task-id", default="task_0_1800")
    task.add_argument("--task-count", type=int, default=56)
    task.add_argument("--sim-time", type=float, default=0.0)
    task.add_argument("--fire-time", type=float, default=1800.0)
    task.set_defaults(func=command_task)

    dian = sub.add_parser("dian", help="Send FA_SHE_DIAN/YIN_BI_DIAN/DEPOT_DIAN/VEHICLE_DIAN files to scheduler.")
    dian.add_argument("--fa-she", default="")
    dian.add_argument("--yin-bi", default="")
    dian.add_argument("--depot", default="")
    dian.add_argument("--vehicle", default="")
    dian.set_defaults(func=command_dian)

    select = sub.add_parser("select", help="Select top-score vehicles from logs and send SELECTED_VEHICLE_RESULT.")
    select.add_argument("--task-id", default="task_0_1800")
    select.add_argument("--task-count", type=int, default=56)
    select.add_argument(
        "--logs",
        nargs="+",
        default=[
            ".run/logs/manual_debug.log",
            "manual_debug.log",
            "logs/scheduler_001_events.jsonl",
            ".run/logs/scheduler_001.log",
        ],
    )
    select.add_argument("--out", default="result/manual_selected_vehicle_result.json")
    select.add_argument("--no-send", action="store_true")
    select.set_defaults(func=command_select)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

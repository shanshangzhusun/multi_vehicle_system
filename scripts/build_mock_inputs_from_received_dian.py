#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_files(received_dir: Path) -> Iterable[Path]:
    return sorted(received_dir.glob("*.json"))


def _vehicle_id_from_name(path: Path) -> Optional[str]:
    name = path.stem
    if "_" not in name:
        return None
    prefix, _ = name.split("_", 1)
    return prefix if prefix.isdigit() else None


def _msg_suffix(path: Path) -> str:
    name = path.stem
    return name.split("_", 1)[1] if "_" in name else name


def _norm_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return dict(row)


def _row_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        row.get("index"),
        row.get("name"),
        row.get("type"),
        row.get("lon"),
        row.get("lat"),
        row.get("platform_id"),
        row.get("platform_name"),
        row.get("vehicle_id"),
        row.get("port"),
    )


def _pick_rows(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = obj.get("raw_rows")
    if not isinstance(rows, list) or not rows:
        rows = obj.get("mapped_rows")
    if not isinstance(rows, list):
        return []
    return [_norm_row(row) for row in rows if isinstance(row, dict)]


def _write_rows(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"data": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build mock_local_model_node aggregate inputs from split result/received_dian files."
    )
    parser.add_argument("--received-dir", default="result/received_dian")
    parser.add_argument("--out-dir", default="result/mock_inputs_from_received")
    parser.add_argument("--vehicle-start-port", type=int, default=None)
    parser.add_argument("--vehicle-count", type=int, default=None)
    args = parser.parse_args()

    received_dir = Path(args.received_dir)
    out_dir = Path(args.out_dir)
    include_ids = None
    if args.vehicle_start_port is not None and args.vehicle_count is not None:
        include_ids = {
            str(port)
            for port in range(args.vehicle_start_port, args.vehicle_start_port + args.vehicle_count)
        }

    fire_rows: List[Dict[str, Any]] = []
    hide_rows: List[Dict[str, Any]] = []
    vehicle_rows: List[Dict[str, Any]] = []
    fire_seen = set()
    hide_seen = set()
    vehicle_seen = set()

    for path in _iter_files(received_dir):
        vehicle_id = _vehicle_id_from_name(path)
        if include_ids is not None and vehicle_id not in include_ids:
            continue
        suffix = _msg_suffix(path)
        obj = _load_json(path)
        rows = _pick_rows(obj)
        if not rows:
            continue
        if suffix == "FA_SHE_DIAN":
            for row in rows:
                key = _row_key(row)
                if key not in fire_seen:
                    fire_seen.add(key)
                    fire_rows.append(row)
        elif suffix == "YIN_BI_DIAN":
            for row in rows:
                key = _row_key(row)
                if key not in hide_seen:
                    hide_seen.add(key)
                    hide_rows.append(row)
        elif suffix == "VEHICLE_CONTEXT":
            for row in rows:
                key = _row_key(row)
                if key not in vehicle_seen:
                    vehicle_seen.add(key)
                    vehicle_rows.append(row)

    _write_rows(out_dir / "FA_SHE_DIAN.json", fire_rows)
    _write_rows(out_dir / "YIN_BI_DIAN.json", hide_rows)
    _write_rows(out_dir / "VEHICLE_DIAN.json", vehicle_rows)

    print(
        f"wrote {out_dir} "
        f"fire={len(fire_rows)} hide={len(hide_rows)} vehicle={len(vehicle_rows)}"
    )


if __name__ == "__main__":
    main()

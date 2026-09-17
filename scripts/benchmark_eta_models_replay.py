#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.common.models import Envelope, utc_now_iso
import mvs.vehicle.vehicle_app as vehicle_module
from scripts.apply_deployment_config import resolve_eta_model_path


class _DummyTransport:
    def register_handler(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _dummy_transport_factory(*_args: Any, **_kwargs: Any) -> _DummyTransport:
    return _DummyTransport()


def _load_capture_envelope(path: Path) -> Optional[Envelope]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    msg_type = obj.get("msg_type")
    payload = obj.get("payload")
    if not msg_type or not isinstance(payload, dict):
        return None
    return Envelope(
        msg_id=obj.get("msg_id") or path.stem,
        msg_type=msg_type,
        sender=str(obj.get("sender") or "replay"),
        target=str(obj.get("target") or ""),
        created_at=obj.get("created_at") or obj.get("ts") or utc_now_iso(),
        require_ack=False,
        ack_for=None,
        payload=payload,
    )


def _vehicle_dirs(capture_root: Path) -> Iterable[Path]:
    if not capture_root.exists():
        return []
    return sorted([p for p in capture_root.iterdir() if p.is_dir()], key=lambda p: p.name)


def _has_required_context(vehicle_dir: Path) -> bool:
    recv = vehicle_dir / "recv"
    if not recv.exists():
        return False
    msg_types = set()
    for path in recv.glob("*.json"):
        try:
            msg_types.add(json.loads(path.read_text(encoding="utf-8")).get("msg_type"))
        except Exception:
            continue
    required = {"FA_SHE_DIAN", "YIN_BI_DIAN", "TIME_BACKPLAN_CONTEXT", "VEHICLE_CANDIDATE_CONTEXT", "VEHICLE_CONTEXT"}
    return required.issubset(msg_types)


def _write_temp_config(config_path: Path, model_path: str, out_dir: Path, label: str) -> Path:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["time_predict_model_path"] = model_path
    cfg["message_capture"] = {"enabled": False}
    cfg["event_log_path"] = str(out_dir / f"{config_path.stem}_{label}_events.jsonl")
    cfg["timing_log_path"] = str(out_dir / f"{config_path.stem}_{label}_timing.jsonl")
    out_path = out_dir / f"{config_path.stem}_{label}.json"
    out_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _replay_vehicle(
    *,
    vehicle_id: str,
    model_path: str,
    label: str,
    config_dir: Path,
    capture_root: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    config_path = config_dir / f"{vehicle_id}.json"
    if not config_path.exists():
        return {"vehicle_id": vehicle_id, "label": label, "ok": False, "error": "missing_config"}
    temp_config = _write_temp_config(config_path, model_path, out_dir, label)

    app = vehicle_module.VehicleApp(str(temp_config))
    sent: List[Tuple[str, Dict[str, Any], Tuple[str, int]]] = []

    def fake_send(msg_type: str, payload: Dict[str, Any], addr: Tuple[str, int]) -> None:
        sent.append((msg_type, deepcopy(payload), addr))

    real_submit = app._submit_candidate_paths_to_model
    app._send_raw_json = fake_send  # type: ignore[method-assign]
    app._submit_candidate_paths_to_model = lambda: None  # type: ignore[method-assign]
    app._submit_vehicle_context_score = lambda: None  # type: ignore[method-assign]
    app._capture_message = lambda *args, **kwargs: None  # type: ignore[method-assign]

    allowed = {
        "FA_SHE_DIAN",
        "YIN_BI_DIAN",
        "ZHU_BEI_DIAN",
        "TIME_BACKPLAN_CONTEXT",
        "VEHICLE_CANDIDATE_CONTEXT",
        "VEHICLE_CONTEXT",
    }
    recv_dir = capture_root / vehicle_id / "recv"
    for path in sorted(recv_dir.glob("*.json")):
        env = _load_capture_envelope(path)
        if env is None or env.msg_type not in allowed:
            continue
        app.on_message(env, ("127.0.0.1", 0))

    app._submit_candidate_paths_to_model = real_submit  # type: ignore[method-assign]
    before_predict = app.time_predictor.predict_count
    before_network = app.time_predictor.network_predict_count
    before_hits = app.time_predictor.cache_hit_count
    started = time.perf_counter()
    real_submit()
    elapsed = time.perf_counter() - started

    candidate_payloads = [payload for msg_type, payload, _addr in sent if msg_type == "VEHICLE_CANDIDATE_PATH_RESULT"]
    last_payload = candidate_payloads[-1] if candidate_payloads else {}
    paths = last_payload.get("paths") if isinstance(last_payload, dict) else None
    return {
        "vehicle_id": vehicle_id,
        "label": label,
        "ok": bool(candidate_payloads),
        "elapsed_sec": round(elapsed, 6),
        "path_count": len(paths) if isinstance(paths, list) else 0,
        "predict_calls": app.time_predictor.predict_count - before_predict,
        "network_calls": app.time_predictor.network_predict_count - before_network,
        "cache_hits": app.time_predictor.cache_hit_count - before_hits,
        "cache_size": len(getattr(app.time_predictor, "_cache", {})),
        "model_summary": app.time_predictor.model_summary(),
        "error": "" if candidate_payloads else "no_candidate_path_result",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay captured vehicle contexts and compare ETA model runtime.")
    parser.add_argument("--capture-root", default="result/message_capture/vehicle")
    parser.add_argument("--config-dir", default="result/configs/vehicles")
    parser.add_argument("--student", default="models/eta_mlp_runtime_32x16.json")
    parser.add_argument("--teacher", default="models/eta_mlp_teacher_400x300.json")
    parser.add_argument("--deployment-config", default="", help="optional deployment config; when set, benchmark its eta_time_predictor.mode")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--vehicles", nargs="*", default=[])
    parser.add_argument("--out", default="result/eta_model_benchmark.json")
    args = parser.parse_args()

    vehicle_module.create_transport_node = _dummy_transport_factory  # type: ignore[assignment]
    capture_root = Path(args.capture_root)
    config_dir = Path(args.config_dir)
    out_path = Path(args.out)
    out_dir = out_path.parent / "eta_replay_tmp"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.vehicles:
        vehicles = [str(v) for v in args.vehicles]
    else:
        vehicles = [p.name for p in _vehicle_dirs(capture_root) if _has_required_context(p)][: args.limit]

    if args.deployment_config:
        deployment = json.loads(Path(args.deployment_config).read_text(encoding="utf-8"))
        mode = str((deployment.get("eta_time_predictor") or {}).get("mode") or "deployment")
        model_pairs = [(mode, resolve_eta_model_path(deployment))]
    else:
        model_pairs = [("student", args.student), ("teacher_cached", args.teacher)]

    results: List[Dict[str, Any]] = []
    for vehicle_id in vehicles:
        for label, model_path in model_pairs:
            results.append(
                _replay_vehicle(
                    vehicle_id=vehicle_id,
                    model_path=model_path,
                    label=label,
                    config_dir=config_dir,
                    capture_root=capture_root,
                    out_dir=out_dir,
                )
            )

    summary: Dict[str, Any] = {"vehicles": vehicles, "models": dict(model_pairs), "results": results}
    for label, _model_path in model_pairs:
        rows = [r for r in results if r.get("label") == label and r.get("ok")]
        summary[label] = {
            "ok_count": len(rows),
            "total_elapsed_sec": round(sum(float(r.get("elapsed_sec", 0.0)) for r in rows), 6),
            "avg_elapsed_sec": round(sum(float(r.get("elapsed_sec", 0.0)) for r in rows) / max(1, len(rows)), 6),
            "predict_calls": sum(int(r.get("predict_calls", 0)) for r in rows),
            "network_calls": sum(int(r.get("network_calls", 0)) for r in rows),
            "cache_hits": sum(int(r.get("cache_hits", 0)) for r in rows),
        }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

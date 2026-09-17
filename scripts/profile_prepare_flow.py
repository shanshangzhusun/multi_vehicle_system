#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import apply_deployment_config as adc  # noqa: E402


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


class Timer:
    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def time_call(self, name: str, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        print(f"[profile] start {name}", flush=True)
        try:
            result = fn()
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.steps.append({"step": name, "elapsed_sec": elapsed, "ok": False, "error": repr(exc)})
            raise
        elapsed = time.perf_counter() - started
        self.steps.append({"step": name, "elapsed_sec": elapsed, "ok": True})
        print(f"[profile] done  {name}: {elapsed:.3f}s", flush=True)
        return result

    def time_cmd(self, name: str, cmd: List[str]) -> subprocess.CompletedProcess[str]:
        started = time.perf_counter()
        print(f"[profile] start {name}", flush=True)
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        elapsed = time.perf_counter() - started
        row = {
            "step": name,
            "elapsed_sec": elapsed,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": "\n".join(proc.stdout.splitlines()[-20:]),
            "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
        }
        self.steps.append(row)
        if proc.returncode != 0:
            raise RuntimeError(f"{name} failed with code {proc.returncode}\n{proc.stderr}")
        print(f"[profile] done  {name}: {elapsed:.3f}s", flush=True)
        return proc


def resolve_run_root(config_path: Path, deployment_path: Optional[Path]) -> Path:
    if deployment_path:
        deployment = load_json(deployment_path)
        runtime = deployment.get("runtime") if isinstance(deployment.get("runtime"), dict) else {}
        if runtime.get("out_root"):
            return REPO_ROOT / str(runtime["out_root"])
    cfg = load_json(config_path)
    return REPO_ROOT / str(cfg.get("out_root", "result"))


def prune_task_files_for_wave_mode(tasks_dir: Path) -> Dict[str, int]:
    if not tasks_dir.is_dir():
        return {"wave_tasks": 0, "removed_non_wave_tasks": 0}
    wave_tasks = list(tasks_dir.glob("task_wave_*.json"))
    non_wave_tasks = [path for path in tasks_dir.glob("task_*.json") if not path.name.startswith("task_wave_")]
    removed = 0
    if wave_tasks and non_wave_tasks:
        for path in non_wave_tasks:
            path.unlink()
            removed += 1
    return {"wave_tasks": len(wave_tasks), "removed_non_wave_tasks": removed}


def summarize_build_meta(run_root: Path) -> Dict[str, Any]:
    metas: Dict[str, Any] = {}
    for meta_path in [run_root / "build_meta.json", *sorted((run_root / "theaters").glob("*/build_meta.json"))]:
        if not meta_path.exists():
            continue
        try:
            meta = load_json(meta_path)
        except Exception:
            continue
        key = "root" if meta_path == run_root / "build_meta.json" else meta_path.parent.name
        metas[key] = {
            "cache_hit": bool(meta.get("cache_hit")),
            "graph_nodes": meta.get("graph_nodes"),
            "graph_edges": meta.get("graph_edges"),
            "bbox_lonlat": meta.get("bbox_lonlat"),
            "roads_shp": meta.get("roads_shp"),
        }
    return metas


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile the prepare steps from run_three_platform_flow.sh")
    parser.add_argument("--config", default="configs/shp_four_wave_demo.json")
    parser.add_argument("--deployment-config", default="")
    parser.add_argument("--simulation-mode", default="")
    parser.add_argument("--vehicle-realtime-scale", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args()

    config_path = REPO_ROOT / args.config
    deployment_path = REPO_ROOT / args.deployment_config if args.deployment_config else None
    run_root = resolve_run_root(config_path, deployment_path)
    timer = Timer()

    timer.time_cmd(
        "reset_mission_runtime",
        [
            "bash",
            "scripts/reset_mission_runtime.sh",
            "--config",
            str(config_path.relative_to(REPO_ROOT)),
            "--run-root",
            str(run_root.relative_to(REPO_ROOT)),
        ],
    )

    effective_config = config_path
    if deployment_path:
        effective_config = run_root / "configs" / "scenario.from_deployment.json"
        timer.time_call(
            "apply_deployment_config:scenario",
            lambda: adc.make_scenario(config_path, deployment_path, effective_config),
        )

    prepare_cmd = [
        "bash",
        "scripts/run_mission_demo.sh",
        "--config",
        str(effective_config.relative_to(REPO_ROOT)),
        "--out-root",
        str(run_root.relative_to(REPO_ROOT)),
        "--prepare-only",
    ]
    if args.simulation_mode:
        prepare_cmd.extend(["--simulation-mode", args.simulation_mode])
    if args.vehicle_realtime_scale:
        prepare_cmd.extend(["--vehicle-realtime-scale", args.vehicle_realtime_scale])
    timer.time_cmd("run_mission_demo:prepare_only", prepare_cmd)

    timer.time_call(
        "prune_task_files_for_wave_mode",
        lambda: prune_task_files_for_wave_mode(run_root / "tasks"),
    )

    if deployment_path:
        deployment = load_json(deployment_path)
        applied: Dict[str, Any] = {
            "deployment_config": str(deployment_path.relative_to(REPO_ROOT)),
            "run_root": str(run_root.relative_to(REPO_ROOT)),
        }
        applied.update(
            timer.time_call(
                "runtime:ensure_theater_map_overrides",
                lambda: adc.ensure_theater_map_overrides(run_root, deployment),
            )
        )
        applied.update(
            timer.time_call(
                "runtime:apply_special_points",
                lambda: adc.apply_special_points(run_root, deployment),
            )
        )
        applied.update(
            timer.time_call(
                "runtime:apply_vehicle_starts",
                lambda: adc.apply_vehicle_starts(run_root, deployment),
            )
        )
        timer.time_call("runtime:sync_scheduler_vehicle_rows", lambda: adc.sync_scheduler_vehicle_rows(run_root, deployment))
        platforms_path = timer.time_call(
            "runtime:write_scheduler_platforms",
            lambda: adc.write_scheduler_platforms(run_root, deployment),
        )
        applied["scheduler_platforms_config"] = str(platforms_path.relative_to(REPO_ROOT))
        timer.time_call(
            "runtime:write_deployment_applied",
            lambda: write_json(run_root / "configs" / "deployment_applied.json", applied),
        )
        timer.time_cmd(
            "generate_depot_configs",
            [
                "python3",
                "scripts/generate_depot_configs.py",
                "--deployment-config",
                str(deployment_path.relative_to(REPO_ROOT)),
                "--run-root",
                str(run_root.relative_to(REPO_ROOT)),
            ],
        )

    total = sum(float(row["elapsed_sec"]) for row in timer.steps)
    report = {
        "command_equivalent": [
            "bash",
            "scripts/run_three_platform_flow.sh",
            "prepare",
            "--deployment-config",
            str(deployment_path.relative_to(REPO_ROOT)) if deployment_path else "",
        ],
        "run_root": str(run_root.relative_to(REPO_ROOT)),
        "total_profiled_sec": total,
        "steps": timer.steps,
        "build_meta": summarize_build_meta(run_root),
    }
    out_json = REPO_ROOT / (args.out_json or str(run_root / "prepare_timing_profile.json"))
    write_json(out_json, report)
    txt_path = out_json.with_suffix(".txt")
    lines = [
        f"prepare profile total: {total:.3f}s",
        f"run_root: {report['run_root']}",
        "",
        "steps:",
    ]
    for row in timer.steps:
        lines.append(f"- {row['step']}: {float(row['elapsed_sec']):.3f}s")
    lines.append("")
    lines.append("build_meta:")
    for name, meta in report["build_meta"].items():
        lines.append(
            f"- {name}: cache_hit={meta['cache_hit']} nodes={meta['graph_nodes']} edges={meta['graph_edges']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[profile] wrote {out_json.relative_to(REPO_ROOT)}")
    print(f"[profile] wrote {txt_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

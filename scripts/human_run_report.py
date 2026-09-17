#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def render_kv(label: str, value: Any) -> str:
    return f"- {label}: {value}"


def latest_by(rows: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        k = row.get(key)
        if not k:
            continue
        out[str(k)] = row
    return out


def phase_rank(phase: str) -> int:
    return {"to_fire": 0, "to_depot": 1, "return_home": 2}.get(phase, 9)


def build_task_file_summary(tasks_dir: Path, relevant_task_ids: set[str]) -> tuple[List[str], Dict[str, dict]]:
    lines: List[str] = []
    task_files = sorted(tasks_dir.glob("*.json"))
    task_meta: Dict[str, dict] = {}
    if not task_files:
        return ["- 没有找到任务文件"], task_meta

    for path in task_files:
        obj = load_json(path)
        task_id = obj.get("task_id", path.stem)
        if relevant_task_ids and task_id not in relevant_task_ids:
            continue
        launches = list(obj.get("launches", []))
        ammo_counts = Counter(x.get("ammo_type", "UNKNOWN") for x in launches)
        fire_after = [float(x.get("fire_after_sec", 0.0)) for x in launches if "fire_after_sec" in x]
        task_meta[task_id] = {
            "task_id": task_id,
            "launch_count": len(launches),
            "ammo_counts": dict(ammo_counts),
            "fire_after_min": min(fire_after) if fire_after else None,
            "fire_after_max": max(fire_after) if fire_after else None,
            "file": str(path),
        }
        lines.append(f"- {task_id}")
        lines.append(f"  文件: {path}")
        lines.append(f"  发射子项数: {len(launches)}")
        lines.append(f"  弹种构成: {dict(ammo_counts)}")
        if fire_after:
            lines.append(f"  相对发射时间范围: {min(fire_after):.0f}s ~ {max(fire_after):.0f}s")
    return lines, task_meta


def build_subtask_index(
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
) -> tuple[Dict[str, dict], List[Dict[str, Any]]]:
    subtasks: Dict[str, dict] = defaultdict(
        lambda: {
            "task_id": "",
            "assigned_vehicle": None,
            "launch_point": None,
            "fire_time": None,
            "phases": set(),
            "plans": {},
            "vehicle_events": [],
            "vehicle_event_times": {},
            "done": False,
            "reset": None,
            "deadlock_count": 0,
            "retry_count": 0,
            "reject_count": 0,
            "latest_ts": "",
        }
    )
    risk_rows: List[Dict[str, Any]] = []

    for row in scheduler_rows:
        sid = row.get("subtask_id")
        if not sid:
            continue
        task_id = row.get("task_id")
        if not task_id and "_s" in sid:
            task_id = sid.rsplit("_s", 1)[0]
        info = subtasks[sid]
        info["task_id"] = task_id or info["task_id"]
        info["latest_ts"] = max(info["latest_ts"], row.get("ts", ""))
        evt = row.get("event")
        if evt == "subtask_assigned":
            info["assigned_vehicle"] = row.get("vehicle_id")
            info["launch_point"] = row.get("launch_point")
            info["fire_time"] = row.get("fire_time")
        elif evt == "plan_execute":
            info["phases"].add(row.get("phase"))
            info["plans"][row.get("phase")] = row
        elif evt == "subtask_done":
            info["done"] = True
        elif evt == "subtask_reset":
            info["reset"] = row
            risk_rows.append(row)
        elif evt == "reservation_deadlock_risk":
            info["deadlock_count"] += 1
            risk_rows.append(row)
        elif evt == "path_request_retry":
            info["retry_count"] += 1
            risk_rows.append(row)
        elif evt == "path_rejected":
            info["reject_count"] += 1
            risk_rows.append(row)

    for row in vehicle_rows:
        sid = row.get("subtask_id")
        if not sid:
            continue
        info = subtasks[sid]
        info["latest_ts"] = max(info["latest_ts"], row.get("ts", ""))
        if row.get("event") == "vehicle_event":
            info["vehicle_events"].append(row.get("event_name"))
            info["vehicle_event_times"][row.get("event_name")] = row.get("ts")

    return subtasks, risk_rows


def summarize_task_progress(
    task_meta: Dict[str, dict],
    subtasks: Dict[str, dict],
    scheduler_rows: List[Dict[str, Any]],
) -> List[str]:
    lines: List[str] = []
    received_rows = latest_by([r for r in scheduler_rows if r.get("event") == "task_received"], "task_id")
    task_ids = sorted(set(task_meta) | {x["task_id"] for x in subtasks.values() if x["task_id"]})
    if not task_ids:
        return ["- 没有读到任务执行信息"]

    for task_id in task_ids:
        related = [info for info in subtasks.values() if info["task_id"] == task_id]
        done = sum(1 for x in related if x["done"])
        failed = sum(1 for x in related if x["reset"] and x["reset"].get("status") == "FAILED")
        in_progress = sum(1 for x in related if not x["done"] and not x["reset"] and x["phases"])
        waiting = sum(1 for x in related if not x["done"] and not x["reset"] and not x["phases"])
        deadlock = sum(int(x["deadlock_count"]) for x in related)
        retries = sum(int(x["retry_count"]) for x in related)
        launch_count = task_meta.get(task_id, {}).get("launch_count", len(related))
        lines.append(f"- {task_id}")
        lines.append(f"  收到时间: {received_rows.get(task_id, {}).get('ts', '-')}")
        lines.append(f"  计划子任务数: {launch_count}")
        lines.append(f"  已完成: {done}")
        lines.append(f"  执行中: {in_progress}")
        lines.append(f"  等待中: {waiting}")
        lines.append(f"  失败: {failed}")
        lines.append(f"  死锁退避次数: {deadlock}")
        lines.append(f"  路径请求重试次数: {retries}")
    return lines


def summarize_subtasks(subtasks: Dict[str, dict], subtask_limit: int) -> List[str]:
    lines: List[str] = []
    if not subtasks:
        return ["- 没有子任务明细"]

    ordered = sorted(subtasks.items(), key=lambda item: item[0])
    for sid, info in ordered[:subtask_limit]:
        vehicle_events = list(dict.fromkeys(info["vehicle_events"]))
        if info["done"]:
            state = "已完成"
        elif info["reset"] and info["reset"].get("status") == "FAILED":
            state = "失败"
        elif info["phases"]:
            state = "执行中"
        else:
            state = "等待中"
        lines.append(f"- {sid}")
        lines.append(f"  所属任务: {info['task_id']}")
        lines.append(f"  当前结论: {state}")
        lines.append(f"  执行车辆: {info['assigned_vehicle'] or '-'}")
        lines.append(f"  发射点: {info['launch_point'] or '-'}")
        lines.append(f"  要求发射时刻: {info['fire_time'] or '-'}")
        lines.append(f"  已走阶段: {', '.join(sorted(info['phases'], key=phase_rank)) or '-'}")
        lines.append(f"  车辆事件: {', '.join(vehicle_events) or '-'}")
        lines.append(f"  死锁退避: {info['deadlock_count']}")
        lines.append(f"  路径重试: {info['retry_count']}")
        lines.append(f"  路径被拒: {info['reject_count']}")
        if info["reset"]:
            lines.append(
                f"  失败原因: {info['reset'].get('reason')} / 状态={info['reset'].get('status')}"
            )
    if len(ordered) > subtask_limit:
        lines.append(f"- 其余子任务: 还有 {len(ordered) - subtask_limit} 个未展开")
    return lines


def summarize_vehicles(
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
    max_show: int,
) -> List[str]:
    lines: List[str] = []
    last_heartbeat = latest_by([r for r in scheduler_rows if r.get("event") == "heartbeat"], "vehicle_id")
    assignments: Dict[str, List[str]] = defaultdict(list)
    done_map: Dict[str, List[str]] = defaultdict(list)
    for row in scheduler_rows:
        evt = row.get("event")
        if evt == "subtask_assigned":
            assignments[row.get("vehicle_id")].append(row.get("subtask_id"))
        elif evt == "subtask_done":
            done_map[row.get("vehicle_id")].append(row.get("subtask_id"))

    vehicle_events = defaultdict(Counter)
    latest_exec = {}
    for row in vehicle_rows:
        vid = row.get("node_id")
        if not vid:
            continue
        if row.get("event") == "vehicle_event":
            vehicle_events[vid][row.get("event_name", "")] += 1
        elif row.get("event") == "execute_plan":
            current = latest_exec.get(vid)
            if current is None or row.get("ts", "") >= current.get("ts", ""):
                latest_exec[vid] = row

    vehicle_ids = sorted(set(last_heartbeat) | set(assignments) | set(vehicle_events))
    if not vehicle_ids:
        return ["- 没有车辆明细"]
    for vid in vehicle_ids[:max_show]:
        hb = last_heartbeat.get(vid, {})
        evt = vehicle_events.get(vid, Counter())
        assigned_list = assignments.get(vid, [])
        done_list = done_map.get(vid, [])
        lines.append(f"- {vid}")
        lines.append(f"  当前状态: {hb.get('status', '-')}")
        lines.append(f"  当前位置: {hb.get('current_node', '-')}")
        lines.append(f"  最后心跳时间: {hb.get('ts', '-')}")
        lines.append(f"  被分配子任务数: {len(assigned_list)}")
        lines.append(f"  已完成子任务数: {len(done_list)}")
        lines.append(f"  已发射次数: {evt.get('FIRED', 0)}")
        lines.append(f"  已补给次数: {evt.get('RELOADED', 0)}")
        lines.append(f"  已回待命次数: {evt.get('IDLE_READY', 0)}")
        lines.append(f"  分配过的子任务: {', '.join(assigned_list[:6]) or '-'}")
        if len(assigned_list) > 6:
            lines.append(f"  分配过的子任务(未展开): 还有 {len(assigned_list) - 6} 个")
        if vid in latest_exec:
            row = latest_exec[vid]
            lines.append(
                f"  最近一次执行: {row.get('subtask_id')} / {row.get('phase')} "
                f"开始={row.get('start_at')} 结束={row.get('end_at')}"
            )
    if len(vehicle_ids) > max_show:
        lines.append(f"- 其余车辆: 还有 {len(vehicle_ids) - max_show} 辆未展开")
    return lines


def summarize_risks(risk_rows: List[Dict[str, Any]], max_show: int) -> List[str]:
    if not risk_rows:
        return ["- 本轮没有看到明显异常事件"]
    lines: List[str] = []
    counts = Counter(r.get("event", "unknown") for r in risk_rows)
    for event, count in sorted(counts.items()):
        lines.append(f"- {event}: {count}")
    lines.append("- 详细样例:")
    for row in sorted(risk_rows, key=lambda x: x.get("ts", ""))[:max_show]:
        evt = row.get("event")
        if evt == "reservation_deadlock_risk":
            lines.append(
                f"  {row.get('ts')} {row.get('subtask_id')} 车辆={row.get('vehicle_id')} "
                f"阶段={row.get('phase')} 出现预约冲突重试"
            )
        elif evt == "subtask_reset":
            lines.append(
                f"  {row.get('ts')} {row.get('subtask_id')} 被回收 原因={row.get('reason')} "
                f"状态={row.get('status')}"
            )
        elif evt == "path_request_retry":
            lines.append(
                f"  {row.get('ts')} {row.get('subtask_id')} 车辆={row.get('vehicle_id')} "
                f"路径请求重试 第{row.get('retry')}次"
            )
        elif evt == "path_rejected":
            lines.append(
                f"  {row.get('ts')} {row.get('subtask_id')} 车辆={row.get('vehicle_id')} "
                f"路径被拒 原因={row.get('reason')}"
            )
        else:
            lines.append(f"  {row.get('ts')} {evt} {json.dumps(row, ensure_ascii=False)}")
    return lines


def build_audit_summary(
    summary: dict,
    subtasks: Dict[str, dict],
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
) -> tuple[List[str], List[str], List[str]]:
    passes: List[str] = []
    warns: List[str] = []
    fails: List[str] = []

    expected = int(summary.get("expected_subtasks", 0) or 0)
    completed = int(summary.get("completed_subtasks", 0) or 0)
    if expected and completed == expected:
        passes.append(f"子任务完成数匹配: {completed}/{expected}")
    elif expected:
        fails.append(f"子任务未全部完成: {completed}/{expected}")

    for sid, info in sorted(subtasks.items()):
        if info["done"]:
            missing_phases = [p for p in ["to_fire", "to_depot", "return_home"] if p not in info["phases"]]
            if missing_phases:
                fails.append(f"{sid} 已完成，但缺少阶段记录: {', '.join(missing_phases)}")
            missing_events = [e for e in ["FIRED", "RELOADED", "IDLE_READY"] if e not in info["vehicle_events"]]
            if missing_events:
                fails.append(f"{sid} 已完成，但缺少车辆事件: {', '.join(missing_events)}")
        if info["deadlock_count"] > 0:
            warns.append(f"{sid} 出现 {info['deadlock_count']} 次预约冲突退避")
        if info["retry_count"] > 0:
            warns.append(f"{sid} 出现 {info['retry_count']} 次路径请求重试")
        if info["reject_count"] > 0:
            warns.append(f"{sid} 出现 {info['reject_count']} 次路径拒绝")
        if info["reset"] and info["reset"].get("status") == "FAILED":
            fails.append(f"{sid} 最终失败，原因={info['reset'].get('reason')}")

        to_fire_plan = info["plans"].get("to_fire")
        if to_fire_plan:
            fire_delay = float(to_fire_plan.get("delay_sec", 0.0) or 0.0)
            if fire_delay > 30.0:
                warns.append(f"{sid} 发射前冲突延迟较大: {fire_delay:.1f}s")
            fire_time = parse_ts(info.get("fire_time"))
            fire_event = parse_ts(info["vehicle_event_times"].get("FIRED"))
            if fire_time and fire_event:
                lateness = (fire_event - fire_time).total_seconds()
                if lateness > 20.0:
                    warns.append(f"{sid} 实际发射比要求时间晚 {lateness:.1f}s")
                elif lateness < -60.0:
                    warns.append(f"{sid} 实际发射比要求时间早 {-lateness:.1f}s")

        return_plan = info["plans"].get("return_home")
        if return_plan:
            return_delay = float(return_plan.get("delay_sec", 0.0) or 0.0)
            if return_delay > 60.0:
                warns.append(f"{sid} 回程冲突延迟较大: {return_delay:.1f}s")

    last_heartbeat = latest_by([r for r in scheduler_rows if r.get("event") == "heartbeat"], "vehicle_id")
    assignments = Counter()
    vehicle_events = defaultdict(Counter)
    for row in scheduler_rows:
        if row.get("event") == "subtask_assigned":
            assignments[row.get("vehicle_id")] += 1
    for row in vehicle_rows:
        if row.get("event") == "vehicle_event":
            vehicle_events[row.get("node_id")][row.get("event_name", "")] += 1

    for vid in sorted(set(assignments) | set(vehicle_events) | set(last_heartbeat)):
        fired = vehicle_events[vid].get("FIRED", 0)
        reloaded = vehicle_events[vid].get("RELOADED", 0)
        idle_ready = vehicle_events[vid].get("IDLE_READY", 0)
        assigned = assignments.get(vid, 0)
        if assigned != fired:
            warns.append(f"{vid} 分配次数={assigned}，但发射次数={fired}")
        if fired != reloaded:
            warns.append(f"{vid} 发射次数={fired}，补给次数={reloaded} 不一致")
        if reloaded != idle_ready:
            warns.append(f"{vid} 补给次数={reloaded}，回待命次数={idle_ready} 不一致")
        final_status = last_heartbeat.get(vid, {}).get("status")
        if expected and completed == expected and final_status not in {None, "IDLE"}:
            warns.append(f"{vid} 在任务全部完成后仍不是 IDLE，而是 {final_status}")

    if not fails:
        passes.append("没有发现硬错误型问题，例如完成后缺阶段、缺关键车辆事件、最终失败子任务")

    def dedupe(items: List[str]) -> List[str]:
        return list(dict.fromkeys(items))

    return dedupe(passes), dedupe(warns), dedupe(fails)


def build_report(
    run_root: Path,
    vehicle_limit: int,
    risk_limit: int,
    subtask_limit: int,
) -> str:
    summary = load_json(run_root / "stress_summary.json")
    scheduler_cfg = load_json(run_root / "configs" / "scheduler_debug.json")
    scheduler_rows = load_jsonl(run_root / "logs" / "scheduler_events.jsonl")
    vehicle_rows: List[Dict[str, Any]] = []
    for path in sorted((run_root / "logs").glob("vehicle_*_events.jsonl")):
        vehicle_rows.extend(load_jsonl(path))

    relevant_task_ids = {
        row.get("task_id")
        for row in scheduler_rows
        if row.get("event") == "task_received" and row.get("task_id")
    }
    task_file_lines, task_meta = build_task_file_summary(run_root / "tasks", relevant_task_ids)
    subtasks, risk_rows = build_subtask_index(scheduler_rows, vehicle_rows)
    event_counts = Counter(row.get("event", "") for row in scheduler_rows)
    passes, warns, fails = build_audit_summary(summary, subtasks, scheduler_rows, vehicle_rows)

    lines: List[str] = []
    lines.append("运行报告")
    lines.append("")
    lines.append("零、审计结论")
    if fails:
        lines.append(f"- 总结: 本轮不是“完全没问题”，至少有 {len(fails)} 条失败级问题。")
    elif warns:
        lines.append(f"- 总结: 本轮虽然跑通了，但有 {len(warns)} 条需要盯的警告。")
    else:
        lines.append("- 总结: 本轮没有发现明显的硬错误或警告。")
    lines.append(f"- 通过项: {len(passes)}")
    lines.append(f"- 警告项: {len(warns)}")
    lines.append(f"- 失败项: {len(fails)}")
    if passes:
        lines.append("- 关键通过项:")
        for item in passes[:8]:
            lines.append(f"  {item}")
    if warns:
        lines.append("- 关键警告项:")
        for item in warns[:12]:
            lines.append(f"  {item}")
    if fails:
        lines.append("- 关键失败项:")
        for item in fails[:12]:
            lines.append(f"  {item}")

    lines.append("")
    lines.append("一、运行总览")
    lines.append(render_kv("运行目录", run_root))
    lines.append(render_kv("调度端口", scheduler_cfg.get("listen_port", "-")))
    lines.append(render_kv("车辆数量", len(scheduler_cfg.get("vehicles", []))))
    lines.append(render_kv("地图文件", scheduler_cfg.get("map", {}).get("graph_json", "-")))
    lines.append(render_kv("特殊点文件", scheduler_cfg.get("map", {}).get("points_json", "-")))
    if summary:
        lines.append(render_kv("预期子任务", summary.get("expected_subtasks", "-")))
        lines.append(render_kv("已完成子任务", summary.get("completed_subtasks", "-")))
        lines.append(render_kv("完成率", fmt_pct(float(summary.get("completion_rate", 0.0)))))
        lines.append(render_kv("在线车辆峰值", summary.get("max_online_vehicles", "-")))
    lines.append(render_kv("调度日志事件数", len(scheduler_rows)))
    lines.append(render_kv("车辆日志事件数", len(vehicle_rows)))

    lines.append("")
    lines.append("二、本轮配置清单")
    lines.append(render_kv("调度 tick_sec", scheduler_cfg.get("tick_sec", "-")))
    lines.append(render_kv("心跳超时", scheduler_cfg.get("heartbeat_timeout_sec", "-")))
    lines.append(render_kv("路径提案超时", scheduler_cfg.get("proposal_timeout_sec", "-")))
    lines.append(render_kv("预约时间槽", scheduler_cfg.get("reservation_slot_sec", "-")))
    lines.append(render_kv("预约延迟步长", scheduler_cfg.get("reservation_delay_step_sec", "-")))
    lines.append(render_kv("预约最大延迟", scheduler_cfg.get("reservation_max_delay_sec", "-")))
    lines.append(render_kv("预约保留时间", scheduler_cfg.get("reservation_retention_sec", "-")))
    lines.append(render_kv("死锁退避时间", scheduler_cfg.get("reservation_deadlock_backoff_sec", "-")))

    lines.append("")
    lines.append("三、任务包清单")
    lines.extend(task_file_lines)

    lines.append("")
    lines.append("四、任务执行进度")
    lines.extend(summarize_task_progress(task_meta, subtasks, scheduler_rows))

    lines.append("")
    lines.append("五、子任务清单")
    lines.extend(summarize_subtasks(subtasks, subtask_limit=subtask_limit))

    lines.append("")
    lines.append("六、每辆车进度")
    lines.extend(summarize_vehicles(scheduler_rows, vehicle_rows, max_show=vehicle_limit))

    lines.append("")
    lines.append("七、异常和风险")
    lines.extend(summarize_risks(risk_rows, max_show=risk_limit))

    lines.append("")
    lines.append("八、调度事件统计")
    for event, count in sorted(event_counts.items()):
        lines.append(f"- {event}: {count}")
    if summary:
        last_metrics = summary.get("last_scheduler_metrics") or {}
        lane_stats = last_metrics.get("lane_graph", {})
        conflict_stats = last_metrics.get("conflicts", {})
        lines.append("- 说明:")
        if lane_stats:
            lines.append(
                f"  路由缓存命中率={fmt_pct(float(lane_stats.get('cache_hit_rate', 0.0)))} "
                f"查询次数={lane_stats.get('route_queries', '-')}"
            )
        if conflict_stats:
            lines.append(
                f"  预约成功={conflict_stats.get('reserve_success', '-')} "
                f"预约拒绝={conflict_stats.get('reserve_reject', '-')} "
                f"当前保留时隙={conflict_stats.get('reserved_slots', '-')}"
            )

    lines.append("")
    lines.append("九、补充说明")
    lines.append("- 任务进度以调度日志为准，车辆进度以各车日志为准。")
    lines.append("- 本脚本默认只写文本文件，不在终端输出；如需终端查看，可额外加 --print。")
    lines.append("- stress_test 现在会先清空同名运行目录，避免旧日志混入。")
    lines.append("- 审计结论比“完成率”更严格。完成率只说明最后收尾了，审计结论会额外检查顺序、缺失事件、重试和延迟。")
    lines.append("- 如果还想继续追某个任务或某辆车，可以再用 explain_logs.py 单独做时间线分析。")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a detailed human-readable report file for one run directory.")
    parser.add_argument("--run-root", required=True, help="stress_test 运行目录，例如 .stress_runs/medium_test")
    parser.add_argument("--vehicle-limit", type=int, default=12, help="展开显示多少辆车")
    parser.add_argument("--risk-limit", type=int, default=12, help="展开显示多少条异常样例")
    parser.add_argument("--subtask-limit", type=int, default=60, help="展开显示多少个子任务")
    parser.add_argument("--out", default="", help="输出文本文件路径；默认保存到 run_root/human_report.txt")
    parser.add_argument("--print", action="store_true", help="额外打印到终端")
    args = parser.parse_args()

    run_root = Path(args.run_root)
    out_path = Path(args.out) if args.out else (run_root / "human_report.txt")
    report = build_report(
        run_root=run_root,
        vehicle_limit=args.vehicle_limit,
        risk_limit=args.risk_limit,
        subtask_limit=args.subtask_limit,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    if args.print:
        print(report, end="")


if __name__ == "__main__":
    main()

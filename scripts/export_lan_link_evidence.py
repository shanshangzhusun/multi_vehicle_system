#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_expected_links(lan_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    scheduler_model = lan_cfg.get("scheduler_model") if isinstance(lan_cfg.get("scheduler_model"), dict) else {}
    fire_node = lan_cfg.get("fire_platform_node") if isinstance(lan_cfg.get("fire_platform_node"), dict) else {}
    prelaunch_only = bool(scheduler_model.get("prelaunch_only", False))
    external_vehicle_selection = bool(
        scheduler_model.get("external_vehicle_selection_enabled", False)
        or fire_node.get("external_vehicle_selection_enabled", False)
    )

    links: List[Dict[str, Any]] = [
        {
            "link": "task_source->scheduler_model",
            "events": {"scheduler_model_task_received"},
            "purpose": "unified model injects prepared task packages into scheduler model",
        },
        {
            "link": "scheduler_model->scheduler_software",
            "events": {
                "task_forwarded_to_scheduler",
                "scheduler_context_requested",
                "vehicle_message_forwarded_to_scheduler",
                "heartbeat_summary",
                "selected_vehicle_forwarded_to_scheduler",
            },
            "purpose": "scheduler model forwards task/context requests, selected vehicle results, and vehicle messages to scheduler software",
        },
        {
            "link": "scheduler_software->scheduler_model",
            "events": {"scheduler_context_received"},
            "purpose": "scheduler software returns scheduling context to unified scheduler model",
        },
        {
            "link": "scheduler_software->scheduler_model->vehicle_software",
            "events": {"scheduler_command_fanout_to_vehicles"},
            "purpose": "scheduler software sends path request/execute plan through unified scheduler model to vehicle software",
        },
        {
            "link": "fire_platform_node->scheduler_model",
            "events": {"fire_node_context_requested", "fire_node_selected_vehicle_sent"},
            "purpose": "fire node requests fire context and sends selected vehicle results back to unified scheduler model",
        },
        {
            "link": "scheduler_model->fire_platform_node",
            "events": {"fire_node_context_received", "model_context_served"},
            "purpose": "unified scheduler model returns fire context to fire node",
        },
    ]

    if external_vehicle_selection:
        links.append(
            {
                "link": "vehicle_software->fire_platform_node",
                "events": {"fire_node_vehicle_score_received"},
                "purpose": "vehicle software proactively pushes self-scored results to fire node",
            }
        )
    else:
        links.extend(
            [
                {
                    "link": "fire_platform_node->vehicle_software",
                    "events": {"fire_node_vehicle_score_requested"},
                    "purpose": "fire node requests vehicle scores from vehicle software",
                },
                {
                    "link": "vehicle_software->fire_platform_node",
                    "events": {"fire_node_vehicle_score_received"},
                    "purpose": "vehicle software returns vehicle scores to fire node",
                },
            ]
        )

    if not prelaunch_only:
        links.extend(
            [
                {
                    "link": "depot_node->depot_software",
                    "events": {"depot_node_score_requested"},
                    "purpose": "depot node requests depot scores from depot software",
                },
                {
                    "link": "depot_software->depot_node",
                    "events": {"depot_node_score_received"},
                    "purpose": "depot software returns depot scores to depot node",
                },
                {
                    "link": "depot_node->scheduler_model",
                    "events": {"depot_node_context_requested"},
                    "purpose": "depot node requests depot assignment context from unified scheduler model",
                },
                {
                    "link": "scheduler_model->depot_node",
                    "events": {"depot_node_context_received", "model_context_served"},
                    "purpose": "unified scheduler model returns depot assignment context to depot node",
                },
            ]
        )
    return links


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def counter_to_list(counter: Counter) -> List[Dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def normalize_link_name(link: str) -> str:
    if link.startswith("scheduler_model->vehicle_"):
        return "scheduler_software->scheduler_model->vehicle_software"
    if link == "fire_platform_node->scheduler_model->scheduler_software":
        return "scheduler_model->scheduler_software"
    return link


def summarize(rows: Iterable[Dict[str, Any]], expected_links: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    by_link: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_event = Counter()
    by_msg_type = Counter()
    heartbeat_counts = Counter()
    for row in rows:
        event = str(row.get("event") or "")
        link = normalize_link_name(str(row.get("link") or ""))
        msg_type = str(row.get("msg_type") or "")
        if link:
            normalized = dict(row)
            normalized["link"] = link
            by_link[link].append(normalized)
        if event:
            by_event[event] += 1
        if msg_type:
            by_msg_type[msg_type] += 1
        if event == "heartbeat_summary":
            heartbeat_counts[link] += int(row.get("count") or 0)

    link_summaries = []
    expected_names = set()
    for spec in expected_links:
        link = spec["link"]
        aliases = set(spec.get("aliases") or set())
        expected_names.add(link)
        expected_names.update(aliases)
        link_rows = []
        for name in [link, *sorted(aliases)]:
            link_rows.extend(by_link.get(name, []))
        events = Counter(str(row.get("event") or "") for row in link_rows)
        msg_types = Counter(str(row.get("msg_type") or "") for row in link_rows if row.get("msg_type"))
        observed_links = sorted({str(row.get("link") or "") for row in link_rows if row.get("link")})
        task_ids = sorted({str(row.get("task_id") or "") for row in link_rows if row.get("task_id")})
        failures = sum(1 for row in link_rows if row.get("ok") is False)
        expected_events = spec["events"]
        observed_expected = sum(count for event, count in events.items() if event in expected_events)
        link_summaries.append(
            {
                "link": link,
                "purpose": spec["purpose"],
                "observed": observed_expected > 0,
                "event_count": len(link_rows),
                "expected_event_count": observed_expected,
                "failure_count": failures,
                "heartbeat_forwarded_count": heartbeat_counts.get(link, 0),
                "observed_links": observed_links,
                "events": dict(events),
                "msg_types": dict(msg_types),
                "task_ids_sample": task_ids[:8],
            }
        )

    unknown_links = sorted(set(by_link) - expected_names)
    return {
        "source_rows": len(rows),
        "events": counter_to_list(by_event),
        "msg_types": counter_to_list(by_msg_type),
        "links": link_summaries,
        "unknown_links": unknown_links,
    }


def write_text(summary: Dict[str, Any], out: Path, lan_log: Path) -> None:
    lines = [
        "LAN Link Evidence",
        f"source_log={lan_log}",
        f"source_rows={summary.get('source_rows', 0)}",
        "",
    ]
    if not lan_log.exists():
        lines.extend(
            [
                "Status: LAN evidence log is missing.",
                "Run the LAN/mock-platform flow again after this update to collect link-level evidence.",
            ]
        )
    elif int(summary.get("source_rows") or 0) == 0:
        lines.append("Status: LAN evidence log exists but contains no valid JSONL rows.")
    else:
        for item in summary.get("links", []):
            status = "OK" if item.get("observed") else "MISSING"
            lines.append(f"[{status}] {item['link']}")
            lines.append(f"  purpose: {item['purpose']}")
            lines.append(
                "  counts: "
                f"events={item.get('event_count', 0)}, "
                f"expected_events={item.get('expected_event_count', 0)}, "
                f"heartbeat_forwarded={item.get('heartbeat_forwarded_count', 0)}, "
                f"failures={item.get('failure_count', 0)}"
            )
            if item.get("events"):
                lines.append(
                    "  events: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(item["events"].items()))
                )
            if item.get("observed_links") and item.get("observed_links") != [item["link"]]:
                lines.append("  observed_links: " + ", ".join(item["observed_links"]))
            if item.get("msg_types"):
                lines.append(
                    "  msg_types: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(item["msg_types"].items()))
                )
            if item.get("task_ids_sample"):
                lines.append("  task_ids_sample: " + ", ".join(item["task_ids_sample"]))
            lines.append("")
        if summary.get("unknown_links"):
            lines.append(f"Unknown links (showing {min(12, len(summary['unknown_links']))}/{len(summary['unknown_links'])}):")
            for link in summary["unknown_links"][:12]:
                lines.append(f"  - {link}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export LAN module-to-module link evidence.")
    parser.add_argument("--lan-log", default="logs/lan_events.jsonl")
    parser.add_argument("--lan-config", default="result/lan/config.generated.json")
    parser.add_argument("--out-json", default="result/results/lan_link_evidence.json")
    parser.add_argument("--out-txt", default="result/results/leader_package/10_lan_link_evidence.txt")
    args = parser.parse_args()

    lan_log = Path(args.lan_log)
    lan_cfg = read_json(Path(args.lan_config))
    expected_links = build_expected_links(lan_cfg)
    summary = summarize(load_jsonl(lan_log), expected_links)
    summary["source_log"] = lan_log.as_posix()
    summary["source_config"] = str(Path(args.lan_config))
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_text(summary, Path(args.out_txt), lan_log)


if __name__ == "__main__":
    main()

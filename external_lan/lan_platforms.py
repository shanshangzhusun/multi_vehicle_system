from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mvs.common.models import Envelope, make_envelope, utc_now_iso
from mvs.common.transport import _recv_tcp_payload, _try_parse_raw_json_request
from mvs.common.platform_interfaces import (
    MSG_DISPATCH_TRAJECTORY_BUNDLE,
    MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
    MSG_DEPOT_SCORE_RESPONSE,
    MSG_FIRE_PLATFORM_CONTEXT_RESULT,
    MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT,
    MSG_REQUEST_DEPOT_SCORE,
    MSG_REQUEST_FIRE_PLATFORM_CONTEXT,
    MSG_REQUEST_TIME_BACKPLAN,
    MSG_REQUEST_VEHICLE_ASSIGNMENT,
    MSG_REQUEST_VEHICLE_SCORE,
    MSG_SELECTED_VEHICLE_RESULT,
    MSG_TIME_BACKPLAN_RESULT,
    MSG_VEHICLE_ASSIGNMENT_RESULT,
    MSG_VEHICLE_PLANNING_CONTEXT_RESULT,
    MSG_VEHICLE_SCORE_RESULT,
)


Address = Tuple[str, int]


class RuntimeControl:
    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.stop_reason = ""

    def request_stop(self, reason: str) -> None:
        if not self.stop_requested.is_set():
            self.stop_reason = str(reason or "")
            self.stop_requested.set()


def recv_exact(sock: socket.socket, size: int, timeout_sec: float) -> bytes:
    sock.settimeout(timeout_sec)
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise ConnectionError("socket closed while receiving data")
        chunks.extend(part)
    return bytes(chunks)


def recv_frame(sock: socket.socket, timeout_sec: float, max_payload_bytes: int) -> bytes:
    header = recv_exact(sock, 4, timeout_sec)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > max_payload_bytes:
        raise ValueError(f"invalid payload size: {size}")
    return recv_exact(sock, size, timeout_sec)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def make_ack(sender: str, target: str, ack_for: str) -> Envelope:
    return make_envelope("__ack__", sender=sender, target=target, payload={}, require_ack=False, ack_for=ack_for)


class TcpServer:
    def __init__(
        self,
        node_id: str,
        host: str,
        port: int,
        handler,
        read_timeout_sec: float,
        max_payload_bytes: int,
        listen_backlog: int,
    ) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
        self.handler = handler
        self.read_timeout_sec = read_timeout_sec
        self.max_payload_bytes = max_payload_bytes
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(listen_backlog)
        self._server.settimeout(0.2)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        print(f"[{self.node_id}] listening on {self.host}:{self.port}", flush=True)

    def stop(self) -> None:
        self._running = False
        try:
            self._server.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn: socket.socket, addr: Address) -> None:
        with conn:
            raw_json = False
            try:
                payload, raw_json = _recv_tcp_payload(conn, self.read_timeout_sec, self.max_payload_bytes)
                if raw_json:
                    raw_obj = json.loads(payload.decode("utf-8"))
                    if not isinstance(raw_obj, dict):
                        raise ValueError("raw JSON request must be an object")
                    env = _try_parse_raw_json_request(raw_obj)
                else:
                    env = Envelope.from_bytes(payload)
                if env.require_ack and not raw_json:
                    send_frame(conn, make_ack(self.node_id, env.sender, env.msg_id).to_bytes())
                if env.msg_type != "__ack__":
                    response = self.handler(env, addr)
                    if raw_json and isinstance(response, dict):
                        conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            except Exception as exc:
                if raw_json:
                    try:
                        conn.sendall(
                            json.dumps(
                                {
                                    "msg_type": "ERROR",
                                    "accepted": False,
                                    "status": "ERROR",
                                    "error": f"{type(exc).__name__}: {exc}",
                                },
                                ensure_ascii=False,
                            ).encode("utf-8")
                            + b"\n"
                        )
                    except Exception:
                        pass
                print(f"[{self.node_id}] receive error from {addr}: {exc}", flush=True)


class LanNode:
    def __init__(self, cfg: dict, section: str) -> None:
        self.cfg = cfg
        self.section = section
        self.runtime_control: Optional[RuntimeControl] = cfg.get("__runtime_control__")
        self.node_cfg = cfg[section]
        self.node_id = str(self.node_cfg.get("node_id", section))
        self.host = str(self.node_cfg.get("host", "0.0.0.0"))
        self.port = int(self.node_cfg["port"])
        tcp = cfg.get("tcp", {})
        self.connect_timeout_sec = float(tcp.get("connect_timeout_sec", 1.5))
        self.read_timeout_sec = float(tcp.get("read_timeout_sec", 2.0))
        self.max_retries = int(tcp.get("max_retries", 1))
        self.retry_backoff_sec = float(tcp.get("retry_backoff_sec", 0.1))
        self.max_payload_bytes = int(tcp.get("max_payload_bytes", 4 * 1024 * 1024))
        self.listen_backlog = int(tcp.get("listen_backlog", 512))
        self._server = TcpServer(
            self.node_id,
            self.host,
            self.port,
            self.on_message,
            self.read_timeout_sec,
            self.max_payload_bytes,
            self.listen_backlog,
        )
        self._running = False
        self.event_log_path = Path(str(cfg.get("lan_event_log", "logs/lan_events.jsonl")))
        self._event_log_lock = threading.Lock()
        self._heartbeat_summary_interval_sec = float(
            cfg.get("lan_heartbeat_summary_interval_sec", self.node_cfg.get("heartbeat_summary_interval_sec", 5.0))
        )
        self.quiet_logs = bool(cfg.get("quiet_model_logs", self.node_cfg.get("quiet_logs", True)))
        self._heartbeat_summary: Dict[str, Dict[str, Any]] = {}

    def start(self) -> None:
        self._running = True
        self._server.start()

    def stop(self) -> None:
        self._running = False
        self._server.stop()

    def on_message(self, env: Envelope, addr: Address) -> None:
        del env, addr

    def send_env(self, env: Envelope, addr: Address, require_ack: bool = False) -> bool:
        env.require_ack = require_ack
        data = env.to_bytes()
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                with socket.create_connection(addr, timeout=self.connect_timeout_sec) as sock:
                    send_frame(sock, data)
                    if require_ack:
                        ack = Envelope.from_bytes(recv_frame(sock, self.read_timeout_sec, self.max_payload_bytes))
                        if ack.msg_type != "__ack__" or ack.ack_for != env.msg_id:
                            raise RuntimeError("invalid ack")
                return True
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_sec)
        print(f"[{self.node_id}] send failed target={addr[0]}:{addr[1]} type={env.msg_type}: {last_error}", flush=True)
        return False

    def send_message(self, msg_type: str, target: str, payload: dict, addr: Address, require_ack: bool = False) -> bool:
        env = make_envelope(msg_type, sender=self.node_id, target=target, payload=payload, require_ack=require_ack)
        return self.send_env(env, addr, require_ack=require_ack)

    def public_host(self) -> str:
        host = str(self.node_cfg.get("advertise_host") or self.node_cfg.get("host") or "127.0.0.1")
        return "127.0.0.1" if host in {"0.0.0.0", ""} else host

    def log_event(self, event: str, **fields: Any) -> None:
        row = {
            "ts": utc_now_iso(),
            "event": event,
            "node_id": self.node_id,
            "section": self.section,
        }
        row.update(fields)
        try:
            self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log_lock:
                with self.event_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass

    def log_heartbeat_summary(self, link: str, msg_type: str = "HEARTBEAT") -> None:
        now = time.monotonic()
        summary = self._heartbeat_summary.setdefault(link, {"count": 0, "last": now})
        summary["count"] = int(summary.get("count", 0)) + 1
        last = float(summary.get("last", now))
        if now - last >= self._heartbeat_summary_interval_sec:
            count = int(summary.get("count", 0))
            self.log_event("heartbeat_summary", link=link, msg_type=msg_type, count=count)
            summary["count"] = 0
            summary["last"] = now


class SchedulerModel(LanNode):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg, "scheduler_model")
        scheduler_rows = cfg.get("scheduler_softwares") or [cfg["scheduler_software"]]
        self.schedulers = [
            {
                "node_id": str(row.get("node_id", f"scheduler_{idx + 1:03d}")),
                "addr": (str(row.get("host", "127.0.0.1")), int(row["port"])),
                "dashboard_port": int(row.get("dashboard_port", 0) or 0),
            }
            for idx, row in enumerate(scheduler_rows)
        ]
        self.scheduler_addr = self.schedulers[0]["addr"]
        self.scheduler_dashboard_port = int(self.schedulers[0].get("dashboard_port", 0) or 0)
        self.fire_model_addr = address_from(cfg["fire_platform_model"])
        self.depot_model_addr = address_from(cfg["depot_model"])
        self.contexts: Dict[str, Dict[str, Any]] = {}
        self._task_scheduler: Dict[str, Address] = {}
        self._next_scheduler_idx = 0
        self.scheduler_routing = str(self.node_cfg.get("scheduler_routing", "primary"))
        self._lock = threading.Lock()
        self._task_source_started = False
        self._task_source_finished = False
        self._fire_context_published: set[str] = set()
        self._depot_context_published: set[str] = set()
        self.unified_model_enabled = bool(self.node_cfg.get("unified_model_enabled", True))
        self.prelaunch_only = bool(self.node_cfg.get("prelaunch_only", False))
        self.require_nodes_for_dispatch = bool(self.node_cfg.get("require_nodes_for_dispatch", self.unified_model_enabled))
        self.required_node_timeout_sec = float(self.node_cfg.get("required_node_timeout_sec", 30.0) or 30.0)
        self.required_node_poll_sec = float(self.node_cfg.get("required_node_poll_sec", 0.5) or 0.5)
        self._required_node_ids = {"fire_platform_node"}
        if not self.prelaunch_only:
            self._required_node_ids.add("depot_node")
        self._node_last_seen: Dict[str, float] = {}
        self.vehicles = load_vehicles(cfg)

    def start(self) -> None:
        super().start()
        task_source = self.node_cfg.get("task_source") or {}
        if task_source.get("enabled") and not self._task_source_started:
            self._task_source_started = True
            threading.Thread(target=self._run_task_source, daemon=True).start()

    def on_message(self, env: Envelope, addr: Address) -> Optional[dict]:
        self._note_node_activity(env.sender)
        if env.msg_type == "TASK_PACKAGE":
            payload = dict(env.payload or {})
            task_id, scheduler_addr, ok = self._forward_task_package(env, payload, f"{addr[0]}:{addr[1]}")
            if (env.payload or {}).get("_raw_tcp_json"):
                return {
                    "msg_type": "SCHEDULER_MODEL_TASK_RECEIPT",
                    "schema": "scheduler_model_task_receipt_v1",
                    "accepted": bool(ok),
                    "status": "FORWARDED" if ok else "FORWARD_FAILED",
                    "task_id": task_id,
                    "launches": len((env.payload or {}).get("launches") or []),
                    "target_scheduler": f"{scheduler_addr[0]}:{scheduler_addr[1]}",
                    "issued_at": utc_now_iso(),
                }
        elif env.msg_type == "TASK_PACKAGE_RECEIPT":
            payload = dict(env.payload or {})
            task_id = str(payload.get("task_id") or "")
            self.log_event(
                "scheduler_task_receipt_received",
                link="scheduler_software->scheduler_model",
                msg_type=env.msg_type,
                task_id=task_id,
                accepted=bool(payload.get("accepted", False)),
                status=str(payload.get("status") or ""),
                launches=int(payload.get("launches") or 0),
                redundant_launches=int(payload.get("redundant_launches") or 0),
                subtasks_created=int(payload.get("subtasks_created") or 0),
            )
            print(
                f"[{self.node_id}] task receipt task={task_id or '-'} "
                f"status={payload.get('status', '-')}",
                flush=True,
            )
        elif env.msg_type in {MSG_TIME_BACKPLAN_RESULT, MSG_VEHICLE_ASSIGNMENT_RESULT, MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT}:
            self._store_scheduler_response(env)
        elif env.msg_type == MSG_SELECTED_VEHICLE_RESULT:
            self._forward_selected_vehicle_to_scheduler(env)
        elif env.msg_type == MSG_DISPATCH_TRAJECTORY_BUNDLE:
            payload = dict(env.payload or {})
            plan_count = int(payload.get("plan_count") or len(payload.get("plans") or []))
            task_id = str(payload.get("task_id") or "")
            self.log_event(
                "dispatch_trajectory_bundle_received",
                link="scheduler_software->scheduler_model",
                msg_type=env.msg_type,
                task_id=task_id,
                plan_count=plan_count,
                bundle_name=str(payload.get("bundle_name") or ""),
            )
            print(
                f"[{self.node_id}] dispatch trajectories received task={task_id or '-'} plans={plan_count}",
                flush=True,
            )
        elif env.msg_type == MSG_REQUEST_FIRE_PLATFORM_CONTEXT:
            return self._serve_cached_context_request(env, addr, MSG_FIRE_PLATFORM_CONTEXT_RESULT, "fire_platform_context")
        elif env.msg_type == MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT:
            return self._serve_cached_context_request(env, addr, MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT, "depot_assignment_context")
        elif env.msg_type in {"HEARTBEAT", "PATH_PROPOSAL", "VEHICLE_EVENT"}:
            self._forward_vehicle_message_to_scheduler(env)
        elif env.msg_type in {
            MSG_FIRE_PLATFORM_CONTEXT_RESULT,
            "PATH_REQUEST",
            "EXECUTE_PLAN",
            MSG_VEHICLE_PLANNING_CONTEXT_RESULT,
        }:
            self._forward_scheduler_command(env)
        return None

    def _forward_selected_vehicle_to_scheduler(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        scheduler_addr = self._assign_scheduler(task_id)
        ok = self.send_env(env, scheduler_addr, require_ack=False)
        self.log_event(
            "selected_vehicle_forwarded_to_scheduler",
            link="fire_platform_node->scheduler_model->scheduler_software",
            msg_type=env.msg_type,
            task_id=task_id,
            subtask_id=str(payload.get("subtask_id") or ""),
            vehicle_id=str(payload.get("vehicle_id") or ""),
            target_addr=f"{scheduler_addr[0]}:{scheduler_addr[1]}",
            ok=ok,
        )

    def _forward_task_package(self, env: Envelope, payload: dict, source_addr: str) -> Tuple[str, Address, bool]:
        task_id = str(payload.get("task_id") or "")
        print(f"[{self.node_id}] task received task={task_id} from={source_addr}", flush=True)
        self.log_event(
            "scheduler_model_task_received",
            link="task_source->scheduler_model",
            msg_type=env.msg_type,
            task_id=task_id,
            source_addr=source_addr,
        )
        if not self._required_nodes_ready():
            self.log_event(
                "task_forward_blocked_required_nodes_offline",
                link="scheduler_model->scheduler_software",
                msg_type=env.msg_type,
                task_id=task_id,
                required_nodes=sorted(self._required_node_ids),
            )
            print(f"[{self.node_id}] block task forward task={task_id or '-'} reason=required_nodes_offline", flush=True)
            return task_id, self.scheduler_addr, False
        scheduler_addr = self._assign_scheduler(task_id)
        forwarded_payload = dict(payload)
        forwarded_payload.setdefault("reply_host", self.public_host())
        forwarded_payload.setdefault("reply_port", self.port)
        forwarded_payload.setdefault("response_require_ack", False)
        forwarded = Envelope(
            msg_id=env.msg_id,
            msg_type=env.msg_type,
            sender=env.sender,
            target="scheduler",
            created_at=env.created_at,
            require_ack=False,
            ack_for=env.ack_for,
            payload=forwarded_payload,
        )
        ok = self.send_env(forwarded, scheduler_addr, require_ack=False)
        self.log_event(
            "task_forwarded_to_scheduler",
            link="scheduler_model->scheduler_software",
            msg_type=env.msg_type,
            task_id=task_id,
            target_addr=f"{scheduler_addr[0]}:{scheduler_addr[1]}",
            ok=ok,
        )
        if task_id:
            threading.Thread(target=self._refresh_context_until_ready, args=(task_id,), daemon=True).start()
        return task_id, scheduler_addr, ok

    def _run_task_source(self) -> None:
        source = dict(self.node_cfg.get("task_source") or {})
        start_delay_sec = float(source.get("start_delay_sec", 3.0))
        gap_scale = float(source.get("gap_scale", 1.0))
        task_files = [str(p) for p in source.get("task_files") or []]
        if not task_files:
            print(f"[{self.node_id}] task source enabled but no task files configured", flush=True)
            return
        time.sleep(max(0.0, start_delay_sec))
        self._wait_for_required_nodes()
        self._wait_for_vehicle_readiness(source)
        for idx, task_file in enumerate(task_files):
            path = Path(task_file)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[{self.node_id}] task source cannot read {task_file}: {exc}", flush=True)
                continue
            payload.setdefault("dispatch_time", datetime.now(timezone.utc).isoformat())
            env = make_envelope(
                "TASK_PACKAGE",
                sender=self.node_id,
                target="scheduler",
                payload=payload,
                require_ack=False,
            )
            task_id, _scheduler_addr, ok = self._forward_task_package(env, payload, "scheduler_model_internal_task_source")
            print(
                f"[{self.node_id}] issued task wave={idx + 1} task={task_id or path.stem} ok={ok}",
                flush=True,
            )
            if idx < len(task_files) - 1:
                gaps = source.get("gaps_sec") or []
                gap = float(gaps[idx] if idx < len(gaps) else 0.0)
                if gap > 0:
                    time.sleep(max(0.0, gap * gap_scale))
        self._task_source_finished = True
        if bool(source.get("auto_stop_on_completion", True)):
            threading.Thread(
                target=self._wait_for_mission_completion,
                args=(
                    len(task_files),
                    float(source.get("completion_poll_sec", 2.0) or 2.0),
                    int(source.get("completion_stable_rounds", 3) or 3),
                    float(source.get("completion_timeout_sec", 7200.0) or 7200.0),
                ),
                daemon=True,
            ).start()

    def _note_node_activity(self, sender: str) -> None:
        sender_id = str(sender or "")
        if sender_id in self._required_node_ids:
            with self._lock:
                self._node_last_seen[sender_id] = time.time()

    def _required_nodes_ready(self) -> bool:
        if not self.require_nodes_for_dispatch:
            return True
        now = time.time()
        with self._lock:
            for node_id in self._required_node_ids:
                seen = float(self._node_last_seen.get(node_id, 0.0) or 0.0)
                if seen <= 0.0 or (now - seen) > max(2.0, self.required_node_timeout_sec):
                    return False
        return True

    def _wait_for_required_nodes(self) -> None:
        if not self.require_nodes_for_dispatch:
            return
        deadline = time.time() + max(5.0, self.required_node_timeout_sec)
        while time.time() < deadline:
            if self._required_nodes_ready():
                print(f"[{self.node_id}] required nodes ready: {','.join(sorted(self._required_node_ids))}", flush=True)
                return
            time.sleep(max(0.2, self.required_node_poll_sec))
        missing: List[str] = []
        now = time.time()
        with self._lock:
            for node_id in sorted(self._required_node_ids):
                seen = float(self._node_last_seen.get(node_id, 0.0) or 0.0)
                if seen <= 0.0 or (now - seen) > max(2.0, self.required_node_timeout_sec):
                    missing.append(node_id)
        print(f"[{self.node_id}] required node wait timeout: missing={','.join(missing) or '-'}", flush=True)

    def _wait_for_vehicle_readiness(self, source: dict) -> None:
        if self.scheduler_dashboard_port <= 0:
            return
        min_online = int(source.get("vehicle_ready_min_online", 0) or 0)
        min_ratio = float(source.get("vehicle_ready_min_online_ratio", 0.0) or 0.0)
        timeout_sec = float(source.get("vehicle_ready_timeout_sec", 0.0) or 0.0)
        if min_online <= 0 and min_ratio <= 0.0:
            return
        if timeout_sec <= 0.0:
            timeout_sec = 30.0
        poll_sec = float(source.get("vehicle_ready_poll_sec", 0.5) or 0.5)
        stable_rounds = max(1, int(source.get("vehicle_ready_stable_rounds", 1) or 1))
        deadline = time.time() + timeout_sec
        stable = 0
        url = f"http://{self.scheduler_addr[0]}:{self.scheduler_dashboard_port}/api/state"
        last_summary = "waiting_for_vehicle_readiness"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=min(5.0, max(1.0, poll_sec))) as resp:
                    data = json.load(resp)
                metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
                total = int(metrics.get("vehicles_total", 0) or 0)
                online = int(metrics.get("vehicles_online", 0) or 0)
                ratio_required = int(total * min_ratio + 0.999999) if total > 0 and min_ratio > 0.0 else 0
                required = max(min_online, ratio_required)
                last_summary = f"online={online}/{total} required={required}"
                if required > 0 and online >= required:
                    stable += 1
                    if stable >= stable_rounds:
                        print(f"[{self.node_id}] vehicle readiness reached: {last_summary}", flush=True)
                        return
                else:
                    stable = 0
            except Exception as exc:
                last_summary = f"readiness_poll_error={type(exc).__name__}: {exc}"
                stable = 0
            time.sleep(max(0.2, poll_sec))
        print(f"[{self.node_id}] vehicle readiness wait timeout: {last_summary}", flush=True)

    def _wait_for_mission_completion(
        self,
        expected_task_count: int,
        poll_sec: float,
        stable_rounds: int,
        timeout_sec: float,
    ) -> None:
        if not self.runtime_control or self.scheduler_dashboard_port <= 0:
            return
        deadline = time.time() + max(30.0, timeout_sec)
        stable = 0
        url = f"http://{self.scheduler_addr[0]}:{self.scheduler_dashboard_port}/api/state"
        last_summary = "waiting_for_scheduler_state"
        while not self.runtime_control.stop_requested.is_set() and time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=min(10.0, max(1.0, poll_sec))) as resp:
                    data = json.load(resp)
                metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
                tasks_received = int(metrics.get("tasks_received", 0) or 0)
                pending = int(metrics.get("mission_tasks_pending", metrics.get("subtasks_pending", 0)) or 0)
                queued = int(metrics.get("mission_tasks_queued", metrics.get("subtasks_queued", 0)) or 0)
                running = int(metrics.get("mission_tasks_running", metrics.get("subtasks_running", 0)) or 0)
                subtasks = data.get("subtasks", []) if isinstance(data, dict) else []
                settled = sum(
                    1 for item in subtasks
                    if isinstance(item, dict) and str(item.get("status") or "") in {"DONE", "FAILED"}
                )
                total = len(subtasks)
                last_summary = (
                    f"tasks_received={tasks_received}/{expected_task_count} "
                    f"pending={pending} queued={queued} running={running} "
                    f"settled={settled}/{total}"
                )
                ready = (
                    tasks_received >= expected_task_count
                    and pending == 0
                    and queued == 0
                    and running == 0
                    and (total == 0 or settled >= total)
                )
                stable = stable + 1 if ready else 0
                if stable >= max(1, stable_rounds):
                    print(f"[{self.node_id}] mission completed, model exiting", flush=True)
                    self.runtime_control.request_stop("mission_complete")
                    return
            except Exception as exc:
                last_summary = f"completion_poll_error={type(exc).__name__}: {exc}"
                stable = 0
            time.sleep(max(0.2, poll_sec))
        print(f"[{self.node_id}] completion wait timeout: {last_summary}", flush=True)
        self.runtime_control.request_stop("completion_timeout")

    def _forward_vehicle_message_to_scheduler(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        if env.msg_type == "HEARTBEAT":
            payload.setdefault("origin_advertise_host", payload.get("origin_advertise_host") or payload.get("advertise_host"))
            payload.setdefault("origin_advertise_port", payload.get("origin_advertise_port") or payload.get("advertise_port"))
            payload["advertise_host"] = self.public_host()
            payload["advertise_port"] = self.port
        task_id = str(payload.get("task_id") or "")
        if not task_id and env.msg_type == "VEHICLE_EVENT":
            subtask_id = str(payload.get("subtask_id") or "")
            if "_s" in subtask_id:
                task_id = subtask_id.rsplit("_s", 1)[0]
        forwarded = Envelope(
            msg_id=env.msg_id,
            msg_type=env.msg_type,
            sender=env.sender,
            target="scheduler",
            created_at=env.created_at,
            require_ack=False,
            ack_for=env.ack_for,
            payload=payload,
        )
        target_addr = self._scheduler_for_task(task_id)
        ok = self.send_env(forwarded, target_addr, require_ack=False)
        if env.msg_type == "HEARTBEAT":
            self.log_heartbeat_summary("scheduler_model->scheduler_software")
        else:
            self.log_event(
                "vehicle_message_forwarded_to_scheduler",
                link="scheduler_model->scheduler_software",
                msg_type=env.msg_type,
                task_id=task_id,
                vehicle_id=str(payload.get("vehicle_id") or ""),
                subtask_id=str(payload.get("subtask_id") or ""),
                target_addr=f"{target_addr[0]}:{target_addr[1]}",
                ok=ok,
            )

    def _forward_scheduler_command(self, env: Envelope) -> None:
        if self.unified_model_enabled:
            self._fanout_scheduler_command_to_vehicles(env)
            return
        self._forward_scheduler_command_to_fire_model(env)

    def _forward_scheduler_command_to_fire_model(self, env: Envelope) -> None:
        forwarded = Envelope(
            msg_id=env.msg_id,
            msg_type=env.msg_type,
            sender=env.sender,
            target="fire_platform_model",
            created_at=env.created_at,
            require_ack=False,
            ack_for=env.ack_for,
            payload=dict(env.payload or {}),
        )
        ok = self.send_env(forwarded, self.fire_model_addr, require_ack=False)
        assigned_vehicle = str((env.payload or {}).get("assigned_vehicle_id") or (env.payload or {}).get("vehicle_id") or "")
        payload = env.payload or {}
        self.log_event(
            "scheduler_command_forwarded_to_fire_model",
            link="scheduler_software->scheduler_model->fire_platform_model",
            msg_type=env.msg_type,
            task_id=str(payload.get("task_id") or ""),
            subtask_id=str(payload.get("subtask_id") or ""),
            vehicle_id=assigned_vehicle,
            target_addr=f"{self.fire_model_addr[0]}:{self.fire_model_addr[1]}",
            ok=ok,
        )
        if not self.quiet_logs:
            print(
                f"[{self.node_id}] scheduler command forwarded type={env.msg_type} assigned={assigned_vehicle or '-'}",
                flush=True,
            )

    def _fanout_scheduler_command_to_vehicles(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        assigned_vehicle = str(payload.get("assigned_vehicle_id") or payload.get("vehicle_id") or "")
        sent = 0
        recipients = self.vehicles
        if assigned_vehicle:
            recipients = [vehicle for vehicle in self.vehicles if str(vehicle.get("vehicle_id") or "") == assigned_vehicle]
        for vehicle in recipients:
            forwarded = Envelope(
                msg_id=str(uuid.uuid4()),
                msg_type=env.msg_type,
                sender=env.sender,
                target=vehicle["vehicle_id"],
                created_at=env.created_at,
                require_ack=False,
                ack_for=None,
                payload=payload,
            )
            require_ack = bool(assigned_vehicle and vehicle["vehicle_id"] == assigned_vehicle)
            if self.send_env(forwarded, vehicle["addr"], require_ack=require_ack):
                sent += 1
        self.log_event(
            "scheduler_command_fanout_to_vehicles",
            link="scheduler_software->scheduler_model->vehicle_software",
            msg_type=env.msg_type,
            task_id=str(payload.get("task_id") or ""),
            subtask_id=str(payload.get("subtask_id") or ""),
            vehicle_id=assigned_vehicle,
            vehicles_sent=sent,
            vehicles_total=len(recipients),
        )
        if not self.quiet_logs:
            print(
                f"[{self.node_id}] command fanout type={env.msg_type} assigned={assigned_vehicle or '-'} vehicles={sent}/{len(recipients)}",
                flush=True,
            )

    def _store_scheduler_response(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            rows = payload.get("point_assignment") or payload.get("vehicle_assignment_result") or payload.get("stage_timeline") or []
            if rows and isinstance(rows, list):
                task_id = str((rows[0] or {}).get("task_id") or "")
        if not task_id:
            return
        with self._lock:
            ctx = self.contexts.setdefault(task_id, {"task_id": task_id})
            if env.msg_type == MSG_TIME_BACKPLAN_RESULT:
                ctx["stage_timeline"] = payload.get("stage_timeline") or payload.get("time_backplan_result") or []
            elif env.msg_type == MSG_VEHICLE_ASSIGNMENT_RESULT:
                ctx["point_assignment"] = payload.get("point_assignment") or payload.get("vehicle_assignment_result") or []
                ctx["scoring_subtasks"] = payload.get("scoring_subtasks") or []
                ctx["candidate_launch_points"] = payload.get("candidate_launch_points") or []
            elif env.msg_type == MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT:
                ctx["depot_context"] = payload
            ready = bool(ctx.get("stage_timeline") and ctx.get("point_assignment"))
            depot_ready = bool(ctx.get("depot_context"))
        self.log_event(
            "scheduler_context_received",
            link="scheduler_software->scheduler_model",
            msg_type=env.msg_type,
            task_id=task_id,
            stage_rows=len((payload.get("stage_timeline") or payload.get("time_backplan_result") or []))
            if env.msg_type == MSG_TIME_BACKPLAN_RESULT
            else None,
            assignment_rows=len((payload.get("point_assignment") or payload.get("vehicle_assignment_result") or []))
            if env.msg_type == MSG_VEHICLE_ASSIGNMENT_RESULT
            else None,
            depot_context=env.msg_type == MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
        )
        if ready and task_id not in self._fire_context_published and not self.unified_model_enabled:
            self._fire_context_published.add(task_id)
            self._publish_fire_context(task_id)
        if depot_ready and task_id not in self._depot_context_published and not self.unified_model_enabled:
            self._depot_context_published.add(task_id)
            self._publish_depot_context(task_id)

    def _assign_scheduler(self, task_id: str) -> Address:
        if not self.schedulers:
            return self.scheduler_addr
        with self._lock:
            if task_id and task_id in self._task_scheduler:
                return self._task_scheduler[task_id]
            if self.scheduler_routing == "round_robin":
                row = self.schedulers[self._next_scheduler_idx % len(self.schedulers)]
                self._next_scheduler_idx += 1
            else:
                row = self.schedulers[0]
            addr = row["addr"]
            if task_id:
                self._task_scheduler[task_id] = addr
        print(f"[{self.node_id}] route task={task_id or 'unknown'} scheduler={addr[0]}:{addr[1]}", flush=True)
        return addr

    def _serve_cached_context_request(
        self,
        env: Envelope,
        addr: Address,
        response_type: str,
        context_key: str,
    ) -> Optional[dict]:
        if self.require_nodes_for_dispatch:
            allowed_sender = "fire_platform_node" if env.msg_type == MSG_REQUEST_FIRE_PLATFORM_CONTEXT else "depot_node"
            if str(env.sender or "") != allowed_sender:
                allow_vehicle_sender = bool(
                    env.msg_type == MSG_REQUEST_FIRE_PLATFORM_CONTEXT
                    and self.node_cfg.get("external_vehicle_selection_enabled", False)
                    and str(env.sender or "").startswith("vehicle_")
                )
                if not allow_vehicle_sender:
                    self.log_event(
                        "model_context_request_blocked",
                        link=f"{self.node_id}->{env.sender or 'unknown'}",
                        msg_type=env.msg_type,
                        response_type=response_type,
                        requester=str(env.sender or ""),
                        allowed_sender=allowed_sender,
                    )
                    if not self.quiet_logs:
                        print(
                            f"[{self.node_id}] block context request from={env.sender or '-'} "
                            f"allowed={allowed_sender}",
                            flush=True,
                        )
                    return None
        payload = dict(env.payload or {})
        task_id = str(payload.get("task_id") or "")
        with self._lock:
            context = dict(self.contexts.get(task_id) or self.contexts.get(sorted(self.contexts.keys())[-1], {}) if self.contexts else {})
        response = {
            "request_id": payload.get("request_id"),
            "task_id": context.get("task_id") or task_id,
            "wave_id": context.get("wave_id"),
            context_key: context,
        }
        response.update(context)
        if response_type == MSG_FIRE_PLATFORM_CONTEXT_RESULT:
            fire_node_cfg = self.cfg.get("fire_platform_node") or {}
            response.setdefault(
                "score_submit_host",
                str(fire_node_cfg.get("advertise_host") or self.public_host()),
            )
            response.setdefault("score_port_base", int(fire_node_cfg.get("score_port_base", 8000) or 8000))
            response.setdefault("score_port_count", int(fire_node_cfg.get("score_port_count", len(self.vehicles)) or len(self.vehicles)))
        response_addr = reply_address(payload, addr)
        if response_addr:
            ok = self.send_message(response_type, env.sender, response, response_addr, require_ack=False)
            self.log_event(
                "model_context_served",
                link=f"{self.node_id}->{env.sender}",
                msg_type=response_type,
                request_msg_type=env.msg_type,
                task_id=str(response.get("task_id") or ""),
                requester=env.sender,
                target_addr=f"{response_addr[0]}:{response_addr[1]}",
                ok=ok,
            )
        if not self.quiet_logs:
            print(f"[{self.node_id}] context served to={env.sender} task={response.get('task_id') or '-'}", flush=True)
        return None

    def _scheduler_for_task(self, task_id: str) -> Address:
        with self._lock:
            return self._task_scheduler.get(task_id, self.scheduler_addr)

    def _refresh_context_until_ready(self, task_id: str) -> None:
        max_attempts = int(self.node_cfg.get("context_refresh_attempts", 20))
        interval_sec = float(self.node_cfg.get("context_refresh_interval_sec", 0.8))
        for _ in range(max_attempts):
            self._request_scheduler_context(task_id)
            with self._lock:
                ctx = self.contexts.get(task_id, {})
                if ctx.get("stage_timeline") and ctx.get("point_assignment") and ctx.get("depot_context"):
                    return
            time.sleep(interval_sec)

    def _request_scheduler_context(self, task_id: str) -> None:
        base = {
            "task_id": task_id,
            "request_id": str(uuid.uuid4()),
            "reply_host": self.public_host(),
            "reply_port": self.port,
            "response_require_ack": False,
        }
        for msg_type in (MSG_REQUEST_TIME_BACKPLAN, MSG_REQUEST_VEHICLE_ASSIGNMENT, MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT):
            payload = dict(base)
            payload["request_id"] = str(uuid.uuid4())
            target_addr = self._scheduler_for_task(task_id)
            ok = self.send_message(msg_type, "scheduler", payload, target_addr, require_ack=False)
            self.log_event(
                "scheduler_context_requested",
                link="scheduler_model->scheduler_software",
                msg_type=msg_type,
                task_id=task_id,
                target_addr=f"{target_addr[0]}:{target_addr[1]}",
                ok=ok,
            )

    def _publish_fire_context(self, task_id: str) -> None:
        with self._lock:
            ctx = dict(self.contexts.get(task_id, {}))
        payload = {
            "task_id": task_id,
            "wave_id": task_id.rsplit("_", 1)[-1] if "_" in task_id else "",
            "stage_timeline": ctx.get("stage_timeline") or [],
            "time_backplan_result": ctx.get("stage_timeline") or [],
            "point_assignment": ctx.get("point_assignment") or [],
            "vehicle_assignment_result": ctx.get("point_assignment") or [],
            "issued_at": utc_now_iso(),
        }
        ok = self.send_message(MSG_FIRE_PLATFORM_CONTEXT_RESULT, "fire_platform_model", payload, self.fire_model_addr)
        self.log_event(
            "fire_context_published",
            link="scheduler_model->fire_platform_model",
            msg_type=MSG_FIRE_PLATFORM_CONTEXT_RESULT,
            task_id=task_id,
            assignment_rows=len(payload["point_assignment"]),
            stage_rows=len(payload["stage_timeline"]),
            target_addr=f"{self.fire_model_addr[0]}:{self.fire_model_addr[1]}",
            ok=ok,
        )

    def _publish_depot_context(self, task_id: str) -> None:
        with self._lock:
            ctx = dict(self.contexts.get(task_id, {}))
        payload = dict(ctx.get("depot_context") or {})
        payload.setdefault("task_id", task_id)
        payload.setdefault("wave_id", task_id.rsplit("_", 1)[-1] if "_" in task_id else "")
        payload.setdefault("issued_at", utc_now_iso())
        ok = self.send_message(MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT, "depot_model", payload, self.depot_model_addr)
        self.log_event(
            "depot_context_published",
            link="scheduler_model->depot_model",
            msg_type=MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT,
            task_id=task_id,
            assignment_rows=len(payload.get("point_assignment") or payload.get("vehicle_assignment_result") or []),
            target_addr=f"{self.depot_model_addr[0]}:{self.depot_model_addr[1]}",
            ok=ok,
        )


class ContextModel(LanNode):
    def __init__(self, cfg: dict, section: str, result_msg: str, context_key: str) -> None:
        super().__init__(cfg, section)
        self.result_msg = result_msg
        self.context_key = context_key
        self.contexts: Dict[str, Dict[str, Any]] = {}
        self.latest_task_id = ""
        self._lock = threading.Lock()

    def on_message(self, env: Envelope, addr: Address) -> None:
        if env.msg_type == self.result_msg:
            payload = dict(env.payload or {})
            task_id = str(payload.get("task_id") or "")
            if task_id:
                with self._lock:
                    self.contexts[task_id] = payload
                    self.latest_task_id = task_id
            self.log_event(
                "model_context_updated",
                link=f"{env.sender}->{self.node_id}",
                msg_type=env.msg_type,
                task_id=task_id,
                source=env.sender,
            )
        elif env.msg_type in {MSG_REQUEST_FIRE_PLATFORM_CONTEXT, MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT}:
            payload = dict(env.payload or {})
            task_id = str(payload.get("task_id") or "")
            with self._lock:
                context = dict(self.contexts.get(task_id) or self.contexts.get(self.latest_task_id, {}))
            response = {
                "request_id": payload.get("request_id"),
                "task_id": context.get("task_id") or task_id,
                "wave_id": context.get("wave_id"),
                self.context_key: context,
            }
            response.update(context)
            response_addr = reply_address(payload, addr)
            if response_addr:
                ok = self.send_message(self.result_msg, env.sender, response, response_addr, require_ack=False)
                self.log_event(
                    "model_context_served",
                    link=f"{self.node_id}->{env.sender}",
                    msg_type=self.result_msg,
                    request_msg_type=env.msg_type,
                    task_id=str(response.get("task_id") or ""),
                    requester=env.sender,
                    target_addr=f"{response_addr[0]}:{response_addr[1]}",
                    ok=ok,
                )


class FirePlatformModel(ContextModel):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg, "fire_platform_model", MSG_FIRE_PLATFORM_CONTEXT_RESULT, "fire_platform_context")
        self.scheduler_model_addr = address_from(cfg["scheduler_model"])
        self.vehicles = load_vehicles(cfg)

    def on_message(self, env: Envelope, addr: Address) -> None:
        if env.msg_type == MSG_FIRE_PLATFORM_CONTEXT_RESULT:
            super().on_message(env, addr)
            self._fanout_scheduler_command_to_vehicles(env)
            return
        if env.msg_type == MSG_REQUEST_FIRE_PLATFORM_CONTEXT:
            super().on_message(env, addr)
            return
        if env.msg_type in {"HEARTBEAT", "PATH_PROPOSAL", "VEHICLE_EVENT"}:
            self._forward_vehicle_message_to_scheduler_model(env)
            return
        if env.msg_type in {"PATH_REQUEST", "EXECUTE_PLAN", MSG_VEHICLE_PLANNING_CONTEXT_RESULT}:
            self._fanout_scheduler_command_to_vehicles(env)
            return
        super().on_message(env, addr)

    def _forward_vehicle_message_to_scheduler_model(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        if env.msg_type == "HEARTBEAT":
            payload.setdefault("origin_advertise_host", payload.get("advertise_host"))
            payload.setdefault("origin_advertise_port", payload.get("advertise_port"))
            payload["advertise_host"] = self.public_host()
            payload["advertise_port"] = self.port
        forwarded = Envelope(
            msg_id=env.msg_id,
            msg_type=env.msg_type,
            sender=env.sender,
            target="scheduler_model",
            created_at=env.created_at,
            require_ack=False,
            ack_for=env.ack_for,
            payload=payload,
        )
        ok = self.send_env(forwarded, self.scheduler_model_addr, require_ack=False)
        if env.msg_type == "HEARTBEAT":
            self.log_heartbeat_summary("fire_platform_model->scheduler_model")
        else:
            payload = env.payload or {}
            self.log_event(
                "vehicle_message_forwarded_to_scheduler_model",
                link="vehicle_software->fire_platform_model->scheduler_model",
                msg_type=env.msg_type,
                task_id=str(payload.get("task_id") or ""),
                subtask_id=str(payload.get("subtask_id") or ""),
                vehicle_id=str(payload.get("vehicle_id") or ""),
                target_addr=f"{self.scheduler_model_addr[0]}:{self.scheduler_model_addr[1]}",
                ok=ok,
            )

    def _fanout_scheduler_command_to_vehicles(self, env: Envelope) -> None:
        payload = dict(env.payload or {})
        assigned_vehicle = str(payload.get("assigned_vehicle_id") or payload.get("vehicle_id") or "")
        sent = 0
        recipients = self.vehicles
        if assigned_vehicle:
            recipients = [vehicle for vehicle in self.vehicles if str(vehicle.get("vehicle_id") or "") == assigned_vehicle]
        for vehicle in recipients:
            forwarded = Envelope(
                msg_id=str(uuid.uuid4()),
                msg_type=env.msg_type,
                sender=env.sender,
                target="all_vehicles",
                created_at=env.created_at,
                require_ack=False,
                ack_for=None,
                payload=payload,
            )
            require_ack = bool(assigned_vehicle and vehicle["vehicle_id"] == assigned_vehicle)
            if self.send_env(forwarded, vehicle["addr"], require_ack=require_ack):
                sent += 1
        self.log_event(
            "scheduler_command_fanout_to_vehicles",
            link="fire_platform_model->vehicle_software",
            msg_type=env.msg_type,
            task_id=str(payload.get("task_id") or ""),
            subtask_id=str(payload.get("subtask_id") or ""),
            vehicle_id=assigned_vehicle,
            vehicles_sent=sent,
            vehicles_total=len(recipients),
        )
        if not self.quiet_logs:
            print(
                f"[{self.node_id}] command fanout type={env.msg_type} "
                f"assigned={assigned_vehicle or '-'} vehicles={sent}/{len(recipients)}",
                flush=True,
            )


class FirePlatformNode(LanNode):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg, "fire_platform_node")
        self.vehicles = load_vehicles(cfg)
        self.fire_model_addr = address_from(cfg["scheduler_model"])
        self._context: Dict[str, Any] = {}
        self.external_vehicle_selection_enabled = bool(self.node_cfg.get("external_vehicle_selection_enabled", False))
        self.score_port_base = int(self.node_cfg.get("score_port_base", 8000) or 8000)
        self.score_port_count = int(self.node_cfg.get("score_port_count", len(self.vehicles)) or len(self.vehicles))
        self.score_select_settle_sec = float(self.node_cfg.get("score_select_settle_sec", 1.0) or 1.0)
        self.selection_candidate_limit = max(1, int(self.node_cfg.get("selection_candidate_limit", 32) or 32))
        self._score_state_lock = threading.Lock()
        self._scores_by_subtask: Dict[str, Dict[str, dict]] = {}
        self._score_first_seen_ts: Dict[str, float] = {}
        self._selection_sent: set[str] = set()
        self._score_listener_sockets: List[socket.socket] = []

    def start(self) -> None:
        super().start()
        if self.external_vehicle_selection_enabled:
            self._start_score_receivers()
        threading.Thread(target=self._loop, daemon=True).start()

    def on_message(self, env: Envelope, addr: Address) -> None:
        if env.msg_type == MSG_FIRE_PLATFORM_CONTEXT_RESULT:
            self._context = dict(env.payload or {})
            self.log_event(
                "fire_node_context_received",
                link="scheduler_model->fire_platform_node",
                msg_type=env.msg_type,
                task_id=str(self._context.get("task_id") or ""),
            )
            print(f"[{self.node_id}] fire context received task={self._context.get('task_id')}", flush=True)
        elif env.msg_type == MSG_VEHICLE_SCORE_RESULT:
            self._record_vehicle_score(dict(env.payload or {}), "vehicle_software->fire_platform_node", env.msg_type)

    def _loop(self) -> None:
        interval_sec = float(self.node_cfg.get("request_interval_sec", 10.0))
        while self._running:
            self._request_context()
            if self.external_vehicle_selection_enabled:
                self._flush_selection_results()
            time.sleep(interval_sec)

    def _request_context(self) -> None:
        payload = {
            "request_id": str(uuid.uuid4()),
            "reply_host": self.public_host(),
            "reply_port": self.port,
            "response_require_ack": False,
        }
        ok = self.send_message(MSG_REQUEST_FIRE_PLATFORM_CONTEXT, "scheduler_model", payload, self.fire_model_addr)
        self.log_event(
            "fire_node_context_requested",
            link="fire_platform_node->scheduler_model",
            msg_type=MSG_REQUEST_FIRE_PLATFORM_CONTEXT,
            target_addr=f"{self.fire_model_addr[0]}:{self.fire_model_addr[1]}",
            ok=ok,
        )

    @staticmethod
    def _vehicle_index(vehicle_id: str) -> Optional[int]:
        digits = "".join(ch for ch in str(vehicle_id or "") if ch.isdigit())
        if not digits:
            return None
        try:
            return max(0, int(digits) - 1)
        except ValueError:
            return None

    def _score_port_for_vehicle(self, vehicle_id: str) -> Optional[int]:
        idx = self._vehicle_index(vehicle_id)
        if idx is None or idx >= self.score_port_count:
            return None
        return self.score_port_base + idx

    def _start_score_receivers(self) -> None:
        for vehicle in self.vehicles[: self.score_port_count]:
            port = self._score_port_for_vehicle(str(vehicle.get("vehicle_id") or ""))
            if port is None:
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(8)
            sock.settimeout(0.5)
            self._score_listener_sockets.append(sock)
            threading.Thread(target=self._score_accept_loop, args=(sock, port), daemon=True).start()

    def _score_accept_loop(self, sock: socket.socket, port: int) -> None:
        while self._running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_score_conn, args=(conn, addr, port), daemon=True).start()

    def _handle_score_conn(self, conn: socket.socket, addr: Address, port: int) -> None:
        with conn:
            try:
                conn.settimeout(2.0)
                chunks = bytearray()
                while True:
                    part = conn.recv(65536)
                    if not part:
                        break
                    chunks.extend(part)
                if not chunks:
                    return
                payload = json.loads(bytes(chunks).decode("utf-8-sig"))
                if not isinstance(payload, dict):
                    return
                payload.setdefault("score_port", port)
                self._record_vehicle_score(payload, "vehicle_software->fire_platform_node", "RAW_VEHICLE_SCORE")
            except Exception as exc:
                self.log_event(
                    "fire_node_vehicle_score_parse_failed",
                    link="vehicle_software->fire_platform_node",
                    target_port=port,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _record_vehicle_score(self, payload: dict, link: str, msg_type: str) -> None:
        vehicle_id = str(payload.get("vehicle_id") or "")
        subtask_id = str(payload.get("subtask_id") or "")
        score_total = payload.get("score_total")
        if not vehicle_id or not subtask_id:
            return
        with self._score_state_lock:
            self._scores_by_subtask.setdefault(subtask_id, {})[vehicle_id] = dict(payload)
            self._score_first_seen_ts.setdefault(subtask_id, time.time())
        self.log_event(
            "fire_node_vehicle_score_received",
            link=link,
            msg_type=msg_type,
            vehicle_id=vehicle_id,
            subtask_id=subtask_id,
            score_total=score_total,
        )
        print(
            f"[{self.node_id}] vehicle score vehicle={vehicle_id or '-'} subtask={subtask_id or '-'} total={score_total}",
            flush=True,
        )

    def _flush_selection_results(self) -> None:
        subtasks = self._context.get("scoring_subtasks") or []
        if not isinstance(subtasks, list):
            return
        indexed = {
            str(row.get("subtask_id") or ""): row
            for row in subtasks
            if isinstance(row, dict) and row.get("subtask_id")
        }
        now_ts = time.time()
        ready_rows: List[Tuple[str, dict, Dict[str, dict]]] = []
        for subtask_id, row in indexed.items():
            with self._score_state_lock:
                score_rows = dict(self._scores_by_subtask.get(subtask_id) or {})
                first_seen = float(self._score_first_seen_ts.get(subtask_id, 0.0) or 0.0)
            if not score_rows or not first_seen or (now_ts - first_seen) < self.score_select_settle_sec:
                continue
            if subtask_id in self._selection_sent:
                continue
            ready_rows.append((subtask_id, row, score_rows))

        used_vehicle_ids: set[str] = set()
        ready_rows.sort(key=lambda item: item[0])
        for subtask_id, row, score_rows in ready_rows:
            ranked = sorted(
                score_rows.values(),
                key=lambda item: float(item.get("score_total", 0.0) or 0.0),
                reverse=True,
            )
            best = None
            for candidate in ranked:
                vehicle_id = str(candidate.get("vehicle_id") or "")
                if not vehicle_id or vehicle_id in used_vehicle_ids:
                    continue
                best = candidate
                break
            if best is None and ranked:
                best = ranked[0]
            if best is None:
                continue
            candidate_rows = []
            seen_candidate_ids: set[str] = set()
            for candidate in ranked:
                vehicle_id = str(candidate.get("vehicle_id") or "")
                if not vehicle_id or vehicle_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(vehicle_id)
                candidate_rows.append(
                    {
                        "vehicle_id": vehicle_id,
                        "score_total": float(candidate.get("score_total", 0.0) or 0.0),
                    }
                )
                if len(candidate_rows) >= self.selection_candidate_limit:
                    break
            payload = {
                "task_id": str(best.get("task_id") or row.get("task_id") or self._context.get("task_id") or ""),
                "subtask_id": subtask_id,
                "vehicle_id": str(best.get("vehicle_id") or ""),
                "score_total": float(best.get("score_total", 0.0) or 0.0),
                "candidate_vehicle_ids": [str(item["vehicle_id"]) for item in candidate_rows],
                "candidate_scores": candidate_rows,
                "selection_source": "fire_platform_node",
            }
            if payload["vehicle_id"]:
                used_vehicle_ids.add(payload["vehicle_id"])
            ok = self.send_message(MSG_SELECTED_VEHICLE_RESULT, "scheduler_model", payload, self.fire_model_addr)
            self.log_event(
                "fire_node_selected_vehicle_sent",
                link="fire_platform_node->scheduler_model",
                msg_type=MSG_SELECTED_VEHICLE_RESULT,
                task_id=payload["task_id"],
                subtask_id=subtask_id,
                vehicle_id=payload["vehicle_id"],
                target_addr=f"{self.fire_model_addr[0]}:{self.fire_model_addr[1]}",
                ok=ok,
            )
            if ok:
                self._selection_sent.add(subtask_id)
                print(
                    f"[{self.node_id}] selected vehicle subtask={subtask_id} vehicle={payload['vehicle_id']}",
                    flush=True,
                )


class DepotNode(LanNode):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg, "depot_node")
        depot = cfg["depot_software"]
        self.depot_addr = (str(depot["host"]), int(depot["port"]))
        self.model_addr = address_from(cfg["scheduler_model"])
        self._context: Dict[str, Any] = {}

    def start(self) -> None:
        super().start()
        threading.Thread(target=self._loop, daemon=True).start()

    def on_message(self, env: Envelope, addr: Address) -> None:
        if env.msg_type == MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT:
            payload = dict(env.payload or {})
            self._context = payload.get("depot_assignment_context") if isinstance(payload.get("depot_assignment_context"), dict) else payload
            self.log_event(
                "depot_node_context_received",
                link="scheduler_model->depot_node",
                msg_type=env.msg_type,
                task_id=str(self._context.get("task_id") or ""),
            )
            print(f"[{self.node_id}] depot context received task={self._context.get('task_id')}", flush=True)
        elif env.msg_type == MSG_DEPOT_SCORE_RESPONSE:
            result = (env.payload or {}).get("depot_score_result") or {}
            self.log_event(
                "depot_node_score_received",
                link="depot_software->depot_node",
                msg_type=env.msg_type,
                task_id=str(result.get("task_id") or ""),
                subtask_id=str(result.get("subtask_id") or ""),
                vehicle_id=str(result.get("vehicle_id") or ""),
                selected_depot=str(result.get("selected_depot") or ""),
            )
            print(f"[{self.node_id}] depot score selected={result.get('selected_depot')}", flush=True)

    def _loop(self) -> None:
        interval_sec = float(self.node_cfg.get("request_interval_sec", 10.0))
        while self._running:
            self._request_context()
            time.sleep(0.3)
            self._request_score()
            time.sleep(interval_sec)

    def _request_context(self) -> None:
        payload = {
            "request_id": str(uuid.uuid4()),
            "reply_host": self.public_host(),
            "reply_port": self.port,
            "response_require_ack": False,
        }
        ok = self.send_message(MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT, "scheduler_model", payload, self.model_addr)
        self.log_event(
            "depot_node_context_requested",
            link="depot_node->scheduler_model",
            msg_type=MSG_REQUEST_DEPOT_ASSIGNMENT_CONTEXT,
            target_addr=f"{self.model_addr[0]}:{self.model_addr[1]}",
            ok=ok,
        )

    def _request_score(self) -> None:
        rows = self._context.get("point_assignment") or self._context.get("vehicle_assignment_result") or []
        if not rows:
            return
        row = dict(rows[0] or {})
        payload = {
            "request_id": str(uuid.uuid4()),
            "task_id": row.get("task_id"),
            "subtask_id": row.get("subtask_id"),
            "vehicle_id": row.get("vehicle_id"),
            "launch_node": row.get("fire_point_id") or row.get("launch_node"),
            "ammo_type": row.get("ammo_type"),
            "reply_host": self.public_host(),
            "reply_port": self.port,
            "response_require_ack": False,
        }
        ok = self.send_message(MSG_REQUEST_DEPOT_SCORE, "depot_platform", payload, self.depot_addr)
        self.log_event(
            "depot_node_score_requested",
            link="depot_node->depot_software",
            msg_type=MSG_REQUEST_DEPOT_SCORE,
            task_id=str(row.get("task_id") or ""),
            subtask_id=str(row.get("subtask_id") or ""),
            vehicle_id=str(row.get("vehicle_id") or ""),
            launch_node=str(payload.get("launch_node") or ""),
            target_addr=f"{self.depot_addr[0]}:{self.depot_addr[1]}",
            ok=ok,
        )


def reply_address(payload: Dict[str, Any], fallback: Optional[Address]) -> Optional[Address]:
    host = payload.get("reply_host") or payload.get("response_host")
    port = payload.get("reply_port") or payload.get("response_port")
    if host and port:
        return str(host), int(port)
    return fallback


def address_from(cfg: dict) -> Address:
    host = str(cfg.get("advertise_host") or cfg.get("host") or "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return host, int(cfg["port"])


def load_vehicles(cfg: dict) -> List[Dict[str, Any]]:
    vehicles = []
    vehicles_dir = Path(str(cfg.get("vehicles_dir", "result/configs/vehicles")))
    for path in sorted(vehicles_dir.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        vehicles.append(
            {
                "vehicle_id": str(row["vehicle_id"]),
                "addr": (str(row.get("advertise_host", "127.0.0.1")), int(row.get("advertise_port", row["listen_port"]))),
            }
        )
    return vehicles


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_nodes(cfg: dict, role: str) -> List[LanNode]:
    if role == "model":
        return [SchedulerModel(cfg)]
    if role == "scheduler_model":
        return [SchedulerModel(cfg)]
    if role == "fire_platform_model":
        return [FirePlatformModel(cfg)]
    if role == "depot_model":
        return [ContextModel(cfg, "depot_model", MSG_DEPOT_ASSIGNMENT_CONTEXT_RESULT, "depot_assignment_context")]
    if role == "fire_platform_node":
        return [FirePlatformNode(cfg)]
    if role == "depot_node":
        return [DepotNode(cfg)]
    if role == "all":
        return [
            SchedulerModel(cfg),
            FirePlatformNode(cfg),
            DepotNode(cfg),
        ]
    raise ValueError(f"unknown role: {role}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock external LAN models and nodes for MVS integration.")
    parser.add_argument(
        "role",
        choices=["model", "scheduler_model", "fire_platform_model", "fire_platform_node", "depot_model", "depot_node", "all"],
    )
    parser.add_argument("--config", default="external_lan/config.example.json")
    args = parser.parse_args()

    runtime_control = RuntimeControl()
    cfg = load_config(Path(args.config))
    cfg["__runtime_control__"] = runtime_control
    nodes = build_nodes(cfg, args.role)
    for node in nodes:
        node.start()
    try:
        while not runtime_control.stop_requested.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        for node in nodes:
            node.stop()


if __name__ == "__main__":
    main()

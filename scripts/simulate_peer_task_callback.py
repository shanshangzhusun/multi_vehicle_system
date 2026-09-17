#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise SystemExit(f"payload must be a JSON object: {path}")
    return obj


def recv_all(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    chunks = bytearray()
    while True:
        try:
            part = sock.recv(65536)
        except socket.timeout:
            break
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


def refresh_task_times(payload: dict[str, Any], fire_after_sec: float) -> None:
    now = datetime.now(timezone.utc)
    fire_time = (now + timedelta(seconds=fire_after_sec)).isoformat()
    payload["dispatch_time"] = now.isoformat()
    launches = payload.get("launches")
    if not isinstance(launches, list):
        return
    for idx, launch in enumerate(launches):
        if not isinstance(launch, dict):
            continue
        launch.setdefault("subtask_id", f"{payload.get('task_id', 'task')}_s{idx:03d}")
        launch.setdefault("ammo_type", "HE")
        launch["fire_after_sec"] = fire_after_sec
        launch["fire_time"] = fire_time


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate a peer scheduler model: send raw JSON and listen for reply_host/reply_port callback."
    )
    parser.add_argument("--json", default="configs/raw_task_package_example.json")
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=9120)
    parser.add_argument("--reply-host", default="127.0.0.1")
    parser.add_argument("--reply-port", type=int, default=8888)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--unique-task-id", action="store_true")
    parser.add_argument("--fire-after-sec", type=float, default=2400.0)
    parser.add_argument("--keep-json-times", action="store_true")
    args = parser.parse_args()

    payload = load_json(Path(args.json))
    if args.unique_task_id:
        task_id = str(payload.get("task_id") or "task")
        payload["task_id"] = f"{task_id}_{int(time.time())}"
    if not args.keep_json_times:
        refresh_task_times(payload, args.fire_after_sec)
    payload["reply_host"] = args.reply_host
    payload["reply_port"] = args.reply_port

    callback_data = bytearray()
    callback_error: list[str] = []
    listener_ready = threading.Event()

    def listen_callback() -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((args.reply_host, args.reply_port))
                server.listen(1)
                server.settimeout(args.timeout)
                listener_ready.set()
                conn, _addr = server.accept()
                with conn:
                    callback_data.extend(recv_all(conn, args.timeout))
        except Exception as exc:
            callback_error.append(f"{type(exc).__name__}: {exc}")
            listener_ready.set()

    thread = threading.Thread(target=listen_callback, daemon=True)
    thread.start()
    if not listener_ready.wait(timeout=args.timeout):
        raise SystemExit(f"callback listener did not start on {args.reply_host}:{args.reply_port}")
    if callback_error:
        raise SystemExit(f"callback listener failed: {callback_error[-1]}")

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with socket.create_connection((args.target_host, args.target_port), timeout=args.timeout) as sock:
        sock.settimeout(args.timeout)
        sock.sendall(raw)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        same_conn = recv_all(sock, args.timeout)

    thread.join(timeout=args.timeout + 0.5)

    print("===== SAME_CONNECTION_RESPONSE =====")
    print(same_conn.decode("utf-8", errors="replace").strip() or "<EMPTY>")
    print("===== CALLBACK_RESPONSE =====")
    if callback_data:
        print(bytes(callback_data).decode("utf-8", errors="replace").strip())
    elif callback_error:
        print(f"<ERROR {callback_error[-1]}>")
    else:
        print("<EMPTY>")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.common.models import parse_iso_time, utc_now_iso


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise SystemExit(f"raw JSON task must be an object: {path}")
    return obj


def _fill_fire_time(payload: dict[str, Any], dispatch_time: str) -> None:
    payload["dispatch_time"] = dispatch_time
    dispatch_dt = parse_iso_time(dispatch_time)
    launches = payload.get("launches")
    if not isinstance(launches, list):
        return
    for idx, launch in enumerate(launches):
        if not isinstance(launch, dict):
            raise SystemExit(f"launches[{idx}] must be an object/dict")
        if "fire_time" not in launch and "fire_after_sec" in launch:
            launch["fire_time"] = (dispatch_dt + timedelta(seconds=float(launch["fire_after_sec"]))).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one raw JSON object over TCP and print the response.")
    parser.add_argument("--json", required=True, help="JSON file to send as raw TCP JSON.")
    parser.add_argument("--host", default="127.0.0.1", help="Target host, default 127.0.0.1.")
    parser.add_argument("--port", type=int, default=9120, help="Target TCP port, default 9120.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Connect/read timeout seconds.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic send/listen information to stderr.")
    parser.add_argument("--reply-host", default="", help="Override reply_host in the JSON payload.")
    parser.add_argument("--reply-port", type=int, default=0, help="Override reply_port in the JSON payload.")
    parser.add_argument(
        "--callback",
        action="store_true",
        help="Ask the target to reply by opening a new TCP connection to reply_host:reply_port, then listen for it.",
    )
    parser.add_argument(
        "--unique-task-id",
        action="store_true",
        help="Append current timestamp to task_id before sending, useful for repeated local tests.",
    )
    parser.add_argument(
        "--auto-fire-time",
        action="store_true",
        help="Fill missing launch.fire_time from dispatch_time + fire_after_sec before sending.",
    )
    parser.add_argument(
        "--dispatch-time",
        default="",
        help="Override dispatch_time when --auto-fire-time is used; default is current UTC time.",
    )
    args = parser.parse_args()

    payload = _load_json(Path(args.json))
    if args.unique_task_id:
        base_task_id = str(payload.get("task_id") or "task")
        payload["task_id"] = f"{base_task_id}_{int(time.time())}"
    reply_host = args.reply_host
    reply_port = args.reply_port
    json_response_mode = str(payload.get("response_mode") or payload.get("reply_mode") or "").lower()
    json_callback = json_response_mode in {"callback", "reply_port", "port", "async"}
    if args.callback or json_callback:
        reply_host = reply_host or "127.0.0.1"
        reply_port = reply_port or 8888
        payload["response_mode"] = "callback"
    if reply_host:
        payload["reply_host"] = reply_host
    if reply_port:
        payload["reply_port"] = reply_port
    if args.auto_fire_time:
        _fill_fire_time(payload, args.dispatch_time or str(payload.get("dispatch_time") or utc_now_iso()))

    callback_chunks = bytearray()
    callback_error: list[str] = []
    callback_ready = threading.Event()

    def listen_callback() -> None:
        bind_host = reply_host or "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((bind_host, int(reply_port)))
                server.listen(1)
                server.settimeout(args.timeout)
                callback_ready.set()
                conn, addr = server.accept()
                if args.verbose:
                    print(f"[raw-json] callback connected from {addr[0]}:{addr[1]}", file=sys.stderr)
                with conn:
                    conn.settimeout(args.timeout)
                    while True:
                        part = conn.recv(65536)
                        if not part:
                            break
                        callback_chunks.extend(part)
        except Exception as exc:
            callback_error.append(f"{type(exc).__name__}: {exc}")
            callback_ready.set()

    callback_thread: Optional[threading.Thread] = None
    use_callback = args.callback or json_callback
    if use_callback:
        callback_thread = threading.Thread(target=listen_callback, daemon=True)
        callback_thread.start()
        if not callback_ready.wait(timeout=args.timeout):
            raise SystemExit(f"callback listener did not start on {reply_host}:{reply_port}")
        if callback_error:
            raise SystemExit(f"callback listener failed on {reply_host}:{reply_port}: {callback_error[-1]}")

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if args.verbose:
        reply = ""
        if payload.get("reply_host") or payload.get("reply_port"):
            reply = f" reply={payload.get('reply_host', '-')}:{payload.get('reply_port', '-')}"
        print(f"[raw-json] target={args.host}:{args.port} bytes={len(data)} task_id={payload.get('task_id', '-')}{reply}", file=sys.stderr)
    with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
        sock.settimeout(args.timeout)
        sock.sendall(data)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = bytearray()
        while True:
            try:
                part = sock.recv(65536)
            except socket.timeout:
                break
            if not part:
                break
            chunks.extend(part)

    if use_callback:
        if callback_thread:
            callback_thread.join(timeout=args.timeout + 0.5)
        if callback_chunks:
            text = bytes(callback_chunks).decode("utf-8", errors="replace")
            print(text.rstrip())
        elif callback_error:
            print(f"callback error: {callback_error[-1]}", file=sys.stderr)
        else:
            print("no callback response received", file=sys.stderr)
    elif chunks:
        text = bytes(chunks).decode("utf-8", errors="replace")
        print(text.rstrip())
    else:
        print("no response received", file=sys.stderr)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import re
import socket
import threading
from pathlib import Path
from typing import Any, Dict, List

from mvs.common.event_log import EventLogger
from mvs.common.models import utc_now_iso


class DepotGateway:
    """Collect node score requests and forward them to the model by theater."""

    def __init__(self, config_path: str) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.listen_host = str(self.cfg.get("listen_host", "0.0.0.0"))
        self.listen_port = int(self.cfg.get("listen_port", 9191))
        self.model_host = str(self.cfg.get("model_host", "127.0.0.1"))
        self.model_port = int(self.cfg.get("model_port", 8888))
        self.node_host = str(self.cfg.get("node_host", "192.168.2.12"))
        self.event_log = EventLogger(
            path=str(self.cfg.get("event_log_path", "logs/depot_gateway_events.jsonl")),
            node_id=str(self.cfg.get("node_id", "depot_gateway")),
        )
        self.node_id = str(self.cfg.get("node_id", "depot_gateway"))
        self.message_capture_cfg = dict(self.cfg.get("message_capture", {}))
        self.message_capture_enabled = bool(self.message_capture_cfg.get("enabled", False))
        self.message_capture_dir = Path(
            self.message_capture_cfg.get("dir", "result/message_capture/depot/gateway")
        )
        self._message_capture_seq = 0
        self._message_capture_lock = threading.Lock()
        self.port_groups = [
            {
                "start": int(row["start"]),
                "end": int(row["end"]),
                "group": str(row["group"]),
            }
            for row in self.cfg.get("port_groups", [])
        ]
        self._server: socket.socket | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.listen_host, self.listen_port))
        self._server.listen(64)
        self._server.settimeout(1.0)
        print(
            f"[DepotGateway] listening on {self.listen_host}:{self.listen_port} "
            f"-> model {self.model_host}:{self.model_port}",
            flush=True,
        )
        self.event_log.log(
            "depot_gateway_started",
            listen_host=self.listen_host,
            listen_port=self.listen_port,
            model_host=self.model_host,
            model_port=self.model_port,
            groups=list(self.port_groups),
        )
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _capture_message(
        self,
        direction: str,
        msg_type: str,
        payload: Dict[str, Any],
        *,
        addr: tuple[str, int] | None = None,
        sender: str | None = None,
        target: str | None = None,
        group: str | None = None,
    ) -> None:
        if not self.message_capture_enabled:
            return
        try:
            with self._message_capture_lock:
                self._message_capture_seq += 1
                safe_msg_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(msg_type or "UNKNOWN"))
                out_dir = self.message_capture_dir / direction
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{self._message_capture_seq:06d}_{safe_msg_type}.json"
                row = {
                    "ts": utc_now_iso(),
                    "node_type": "depot_gateway",
                    "node_id": self.node_id,
                    "direction": direction,
                    "msg_type": msg_type,
                    "sender": sender,
                    "target": target,
                    "group": group,
                    "addr": f"{addr[0]}:{addr[1]}" if addr else None,
                    "payload": payload,
                }
                out_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.close()

    @staticmethod
    def _recv_json(conn: socket.socket) -> Dict[str, Any]:
        conn.settimeout(2.0)
        chunks = bytearray()
        while True:
            part = conn.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if b"\n" in part:
                break
        return json.loads(bytes(chunks).decode("utf-8-sig").strip())

    @staticmethod
    def _rows(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("depots"), list):
            rows = data["depots"]
        elif isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = [data]
        else:
            rows = [obj]
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _group_for_port(self, port: int) -> str:
        for row in self.port_groups:
            if row["start"] <= port <= row["end"]:
                return row["group"]
        return ""

    @staticmethod
    def _reply_host(obj: Dict[str, Any], row: Dict[str, Any], fallback: str) -> str:
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        return str(
            row.get("msg_ip")
            or data.get("msg_ip")
            or obj.get("msg_ip")
            or fallback
        )

    def _handle_conn(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            try:
                obj = self._recv_json(conn)
                rows = self._rows(obj)
                self._capture_message(
                    "recv",
                    str(obj.get("msg_type") or "UNKNOWN"),
                    obj,
                    addr=addr,
                    sender=str(obj.get("sender") or ""),
                    target="depot_gateway",
                    group=str(obj.get("group")) if obj.get("group") is not None else None,
                )
                self.event_log.log(
                    "depot_gateway_request_received",
                    from_addr=f"{addr[0]}:{addr[1]}",
                    msg_type=obj.get("msg_type"),
                    rows=len(rows),
                    ports=[
                        row.get("port") or row.get("depot_port") or row.get("depot_id")
                        for row in rows
                    ],
                )
                grouped: Dict[str, List[Dict[str, Any]]] = {}
                for row in rows:
                    port = int(row.get("port") or row.get("depot_port") or row.get("depot_id") or 0)
                    group = self._group_for_port(port)
                    if not group:
                        raise ValueError(f"depot port {port} is outside configured theater ranges")
                    item = dict(row)
                    item["depot_id"] = str(row.get("depot_id") or port)
                    item["port"] = port
                    item["msg_ip"] = self._reply_host(obj, row, self.node_host)
                    item.setdefault("reply_port", port)
                    grouped.setdefault(group, []).append(item)

                for group, depot_rows in grouped.items():
                    request = {
                        "msg_type": "REQUEST_ZHU_BEI",
                        "group": group,
                        "data": {"depots": depot_rows},
                    }
                    raw = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
                    with socket.create_connection((self.model_host, self.model_port), timeout=2.0) as upstream:
                        upstream.sendall(raw)
                    self._capture_message(
                        "send",
                        "REQUEST_ZHU_BEI",
                        request.get("data") or {},
                        addr=(self.model_host, self.model_port),
                        sender=self.node_id,
                        target="model",
                        group=group,
                    )
                    self.event_log.log(
                        "depot_gateway_request_forwarded",
                        from_addr=f"{addr[0]}:{addr[1]}",
                        model_addr=f"{self.model_host}:{self.model_port}",
                        group=group,
                        depots=len(depot_rows),
                        ports=[row.get("port") for row in depot_rows],
                        bytes=len(raw),
                    )
                    print(
                        f"[DepotGateway] forwarded group={group} depots={len(depot_rows)} "
                        f"from={addr[0]}:{addr[1]}",
                        flush=True,
                    )

                ack = {
                    "msg_type": "REQUEST_DEPOT_ACCEPTED",
                    "data": {"count": len(rows)},
                }
                conn.sendall(json.dumps(ack, ensure_ascii=False).encode("utf-8") + b"\n")
                self._capture_message(
                    "send",
                    "REQUEST_DEPOT_ACCEPTED",
                    ack.get("data") or {},
                    addr=addr,
                    sender=self.node_id,
                    target=str(obj.get("sender") or ""),
                    group=str(obj.get("group")) if obj.get("group") is not None else None,
                )
                self.event_log.log(
                    "depot_gateway_request_accepted",
                    from_addr=f"{addr[0]}:{addr[1]}",
                    rows=len(rows),
                )
            except Exception as exc:
                self.event_log.log(
                    "depot_gateway_request_failed",
                    from_addr=f"{addr[0]}:{addr[1]}",
                    error=f"{type(exc).__name__}: {exc}",
                )
                print(
                    f"[DepotGateway] request failed from {addr[0]}:{addr[1]}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Depot score-request gateway")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    app = DepotGateway(args.config)
    try:
        app.start()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == "__main__":
    main()

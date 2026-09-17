from __future__ import annotations

import argparse
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from mvs.common.debug_append_log import configure_manual_debug, debug_append_log, manual_debug_enabled
from mvs.common.event_log import EventLogger
from mvs.common.models import utc_now_iso

'''
bash scripts/send_vehicle_score_requests_mapped.sh 192.168.2.8 9190 0.1 \
  8414-8430=192.168.2.12 \
  8431-8477=192.168.2.14 \
  8519-8582=192.168.2.15
'''

class VehicleGateway:
    FIXED_SCORE_PORT_MIN = 8408
    FIXED_SCORE_PORT_MAX = 8413
    FIXED_SCORE_PORT_MIN_EXT = 8513
    FIXED_SCORE_PORT_MAX_EXT = 8518

    def __init__(self, config_path: str) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        manual_debug_cfg = self.cfg.get("manual_debug") if isinstance(self.cfg.get("manual_debug"), dict) else {}
        configure_manual_debug(bool(manual_debug_cfg.get("enabled", self.cfg.get("manual_debug_enabled", manual_debug_enabled()))))
        self.listen_host = str(self.cfg.get("listen_host", "0.0.0.0"))
        self.listen_port = int(self.cfg.get("listen_port", 9190))
        self.model_host = str(self.cfg.get("model_host", "127.0.0.1"))
        self.model_port = int(self.cfg.get("model_port", 8888))
        self.vehicle_host = str(self.cfg.get("vehicle_host", "192.168.2.8"))
        self.node_host = str(self.cfg.get("node_host", "192.168.2.12"))
        self.vehicle_start_port = int(self.cfg.get("vehicle_start_port", 8414))
        self.vehicle_count = int(self.cfg.get("vehicle_count", 64))
        configured_ports = self.cfg.get("vehicle_ports") or []
        self.vehicle_ports = [int(port) for port in configured_ports if str(port).strip()]
        self.fixed_score_enabled = bool(self.cfg.get("fixed_score_enabled", True))
        self.node_id = str(self.cfg.get("node_id", "vehicle_gateway"))
        self.event_log = EventLogger(
            path=str(self.cfg.get("event_log_path", "logs/vehicle_gateway_events.jsonl")),
            node_id=self.node_id,
        )
        self.message_capture_cfg = dict(self.cfg.get("message_capture", {}))
        self.message_capture_enabled = bool(self.message_capture_cfg.get("enabled", False))
        self.message_capture_dir = Path(
            self.message_capture_cfg.get("dir", "result/message_capture/vehicle/gateway")
        )
        self._message_capture_seq = 0
        self._message_capture_lock = threading.Lock()
        self._running = False
        self._server: socket.socket | None = None
        self._request_total = 0

    def start(self) -> None:
        self._running = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.listen_host, self.listen_port))
        self._server.listen(64)
        self._server.settimeout(1.0)
        print(
            f"[VehicleGateway] listening on {self.listen_host}:{self.listen_port} "
            f"-> model {self.model_host}:{self.model_port}",
            flush=True,
        )
        debug_append_log(
            f"[VehicleGateway] listening host={self.listen_host} port={self.listen_port} "
            f"vehicle_host={self.vehicle_host} node_host={self.node_host} "
            f"vehicle_start_port={self.vehicle_start_port} vehicle_count={self.vehicle_count} "
            f"vehicle_ports={self.vehicle_ports[:5]}...{len(self.vehicle_ports)}"
        )
        self.event_log.log(
            "vehicle_gateway_started",
            listen_host=self.listen_host,
            listen_port=self.listen_port,
            model_host=self.model_host,
            model_port=self.model_port,
            vehicle_host=self.vehicle_host,
            node_host=self.node_host,
            vehicle_start_port=self.vehicle_start_port,
            vehicle_count=self.vehicle_count,
            vehicle_ports=list(self.vehicle_ports),
            fixed_score_enabled=self.fixed_score_enabled,
        )
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

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
                    "node_type": "vehicle_gateway",
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

    def _handle_conn(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            try:
                raw = self._recv_all(conn)
                if not raw:
                    return
                self._request_total += 1
                print(f"GW9190_COUNT n={self._request_total}", flush=True)
                debug_append_log(f"GW9190_COUNT n={self._request_total}")
                obj = json.loads(raw.decode("utf-8-sig"))
                vehicle_rows = self._extract_vehicle_rows(obj)
                vehicle_ports = self._vehicle_ports_from_rows(vehicle_rows)
                msg_type = str(obj.get("msg_type") or obj.get("msgtype") or obj.get("msg-type") or "")
                self._capture_message(
                    "recv",
                    msg_type or "UNKNOWN",
                    obj,
                    addr=addr,
                    sender=f"{addr[0]}:{addr[1]}",
                    target="vehicle_gateway",
                )
                self.event_log.log(
                    "vehicle_gateway_request_received",
                    from_addr=f"{addr[0]}:{addr[1]}",
                    msg_type=msg_type,
                    rows=len(vehicle_rows),
                    vehicle_ports=vehicle_ports,
                    requested_vehicle_ids=[str(port) for port in vehicle_ports],
                    reply_host=self._reply_host(obj),
                    bytes=len(raw),
                    request_total=self._request_total,
                )
                print(
                    f"[VehicleGateway] request from {addr[0]}:{addr[1]} msg_type={msg_type} "
                    f"vehicles={len(vehicle_rows)}",
                    flush=True,
                )
                debug_append_log(
                    f"[VehicleGateway] request from {addr[0]}:{addr[1]} msg_type={msg_type} "
                    f"vehicles={len(vehicle_rows)} msg_ip={self._reply_host(obj)}"
                )
                #大车与节点走测试流程
                if msg_type == "REQUEST_FALSE_SCORE":

                    if self.fixed_score_enabled and self._send_fixed_scores_if_needed(obj, vehicle_rows):
                        return
  
                    self._forward_score_requests(obj, vehicle_rows, addr)
                    print(
                        f"[VehicleGateway] forwarded score requests for {len(vehicle_rows)} vehicles from "
                        f"{addr[0]}:{addr[1]} to local vehicles",
                        flush=True,
                    )
                    debug_append_log(
                        f"[VehicleGateway] forwarded score requests count={len(vehicle_rows)} from={addr}"
                    )
                #节点向大车要分数信息
                elif msg_type == "REQUEST_VEHICLE_SCORE":
                    if self.fixed_score_enabled and self._send_fixed_scores_if_needed(obj, vehicle_rows):
                        return
                    reply_host = self._reply_host(obj)
                    model_vehicle_rows: List[Dict[str, Any]] = []
                    for row in vehicle_rows:
                        forward_row = dict(row)
                        forward_row["msg_ip"] = reply_host
                        forward_row.setdefault("reply_port", int(forward_row.get("port") or forward_row.get("vehicle_id") or 0))
                        model_vehicle_rows.append(forward_row)
                    forward = {
                        "msg_type": "REQUEST_VEHICLE",
                        "data": {
                            "vehicles": model_vehicle_rows,
                        },
                    }
                    payload = json.dumps(forward, ensure_ascii=False).encode("utf-8") + b"\n"
                    debug_append_log(
                        f"[VehicleGateway] forward-to-model host={self.model_host} port={self.model_port} "
                        f"msg_ip={reply_host} vehicles={len(model_vehicle_rows)}"
                    )
                    with socket.create_connection((self.model_host, self.model_port), timeout=2.0) as upstream:
                        upstream.sendall(payload)
                    self._capture_message(
                        "send",
                        "REQUEST_VEHICLE",
                        forward,
                        addr=(self.model_host, self.model_port),
                        sender="vehicle_gateway",
                        target="model",
                    )
                    self.event_log.log(
                        "vehicle_gateway_request_forwarded",
                        from_addr=f"{addr[0]}:{addr[1]}",
                        model_addr=f"{self.model_host}:{self.model_port}",
                        msg_type=msg_type,
                        forwarded_msg_type="REQUEST_VEHICLE",
                        rows=len(model_vehicle_rows),
                        vehicle_ports=self._vehicle_ports_from_rows(model_vehicle_rows),
                        bytes=len(payload),
                    )
                    print(
                        f"[VehicleGateway] forwarded {len(vehicle_rows)} vehicles from "
                        f"{addr[0]}:{addr[1]} to {self.model_host}:{self.model_port}",
                        flush=True,
                    )
                else:
                    debug_append_log(
                        f"[VehicleGateway] ignored msg_type={msg_type} vehicles={len(vehicle_rows)}"
                    )
                    print(
                        f"[VehicleGateway] ignored msg_type={msg_type} from {addr[0]}:{addr[1]}",
                        flush=True,
                    )
                try:
                    ack = json.dumps(
                        {"msg_type": "REQUEST_VEHICLE_ACCEPTED", "data": {"count": len(vehicle_rows)}},
                        ensure_ascii=False,
                    ).encode("utf-8") + b"\n"
                    conn.sendall(ack)
                    self._capture_message(
                        "send",
                        "REQUEST_VEHICLE_ACCEPTED",
                        {"count": len(vehicle_rows)},
                        addr=addr,
                        sender="vehicle_gateway",
                    )
                    self.event_log.log(
                        "vehicle_gateway_request_accepted",
                        to_addr=f"{addr[0]}:{addr[1]}",
                        msg_type=msg_type,
                        rows=len(vehicle_rows),
                        vehicle_ports=vehicle_ports,
                        bytes=len(ack),
                    )
                    debug_append_log(
                        f"[VehicleGateway] ack sent to={addr} count={len(vehicle_rows)}"
                    )
                except OSError:
                    pass
            except Exception as exc:
                debug_append_log(
                    f"[VehicleGateway] request failed from {addr[0]}:{addr[1]} "
                    f"{type(exc).__name__}: {exc}"
                )
                self.event_log.log(
                    "vehicle_gateway_request_failed",
                    from_addr=f"{addr[0]}:{addr[1]}",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                print(f"[VehicleGateway] request failed from {addr[0]}:{addr[1]}: {type(exc).__name__}: {exc}", flush=True)

    @staticmethod
    def _recv_all(conn: socket.socket) -> bytes:
        conn.settimeout(2.0)
        chunks = bytearray()
        while True:
            part = conn.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if b"\n" in part:
                break
        return bytes(chunks).strip()

    def _expand_vehicle_rows(self, start_port: int) -> List[Dict[str, Any]]:
        return [{"vehicle_id": start_port, "port": start_port}]

    @staticmethod
    def _vehicle_ports_from_rows(vehicle_rows: List[Dict[str, Any]]) -> List[int]:
        ports: List[int] = []
        for row in vehicle_rows:
            if not isinstance(row, dict):
                continue
            value = row.get("port")
            if value in {None, ""}:
                value = row.get("vehicle_id") or row.get("vehicle_port") or row.get("reply_port")
            if value in {None, ""}:
                continue
            try:
                ports.append(int(value))
            except (TypeError, ValueError):
                continue
        return ports

    @staticmethod
    def _request_context(obj: Dict[str, Any]) -> Dict[str, Any]:
        data = obj.get("data")
        context: Dict[str, Any] = {}
        if isinstance(data, dict):
            for key in ("port",):
                if key in data:
                    context[key] = data.get(key)
        for key in ("port",):
            if key in obj and key not in context:
                context[key] = obj.get(key)
        return context

    def _extract_vehicle_rows(self, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = obj.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            if isinstance(data.get("vehicles"), list):
                rows = data["vehicles"]
            elif isinstance(data.get("vehicle_id"), list):
                rows = [{"vehicle_id": item} for item in data.get("vehicle_id", [])]
            elif "vehicle_id" in data:
                rows = [data]
            elif data.get("port") is not None:
                rows = self._expand_vehicle_rows(int(data.get("port")))
            else:
                rows = [value for value in data.values() if isinstance(value, dict) and value.get("vehicle_id") is not None]
        elif data is None:
            rows = []
        else:
            rows = [{"vehicle_id": data}]
        if not rows:
            fallback_port = obj.get("port") or obj.get("msg_port")
            if fallback_port is None and isinstance(data, dict):
                fallback_port = data.get("port") or data.get("msg_port")
            if fallback_port is not None:
                rows = self._expand_vehicle_rows(int(fallback_port))
        out: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict) and row.get("vehicle_id") is not None:
                out.append(dict(row))
            elif isinstance(row, (int, str)):
                out.append({"vehicle_id": row})
        if not out:
            ports = self.vehicle_ports or list(
                range(self.vehicle_start_port, self.vehicle_start_port + self.vehicle_count)
            )
            out = [
                {"vehicle_id": port}
                for port in ports
            ]
        context = self._request_context(obj)
        if context:
            for row in out:
                row.update({key: value for key, value in context.items() if key not in row})
        return out

    def _forward_score_requests(self, obj: Dict[str, Any], vehicle_rows: List[Dict[str, Any]], addr: tuple[str, int]) -> None:
        base_payload: Dict[str, Any] = {}
        data = obj.get("data")
        if isinstance(data, dict):
            for key, value in data.items():
                if key not in {"vehicles", "vehicle_id", "msg_ip"}:
                    base_payload[key] = value
        for key in ("ammo_type", "required_ammo_type", "fire_time", "desired_fire_time", "request_time", "issued_at"):
            if key in obj:
                base_payload[key] = obj.get(key)
        for row in vehicle_rows:
            vehicle_port = int(row["vehicle_id"])
            payload = dict(base_payload)
            payload.update(row)
            payload["vehicle_id"] = vehicle_port
            payload["port"] = vehicle_port
            payload["reply_host"] = self._reply_host(obj)
            payload["reply_port"] = vehicle_port
            print(
                f"[VehicleGateway] score request vehicle_port={vehicle_port} "
                f"reply={payload.get('reply_host')}:{payload.get('reply_port')}",
                flush=True,
            )
            debug_append_log(
                f"[VehicleGateway] score request vehicle_port={vehicle_port} "
                f"send_to={self.vehicle_host}:{vehicle_port} reply={payload.get('reply_host')}:{payload.get('reply_port')} "
                f"payload_keys={sorted(payload.keys())}"
            )
            raw = json.dumps({"msg_type": "REQUEST_VEHICLE_SCORE", "data": payload}, ensure_ascii=False).encode("utf-8") + b"\n"
            with socket.create_connection((self.vehicle_host, vehicle_port), timeout=2.0) as downstream:
                downstream.sendall(raw)
            self._capture_message(
                "send",
                "REQUEST_VEHICLE_SCORE",
                payload,
                addr=(self.vehicle_host, vehicle_port),
                sender="vehicle_gateway",
                target=str(vehicle_port),
            )
            self.event_log.log(
                "vehicle_gateway_score_request_delivered",
                vehicle_port=vehicle_port,
                vehicle_addr=f"{self.vehicle_host}:{vehicle_port}",
                reply_host=payload.get("reply_host"),
                reply_port=payload.get("reply_port"),
                bytes=len(raw),
            )
            debug_append_log(
                f"[VehicleGateway] score request delivered vehicle_port={vehicle_port}"
            )

    def _reply_host(self, obj: Dict[str, Any]) -> str:
        data = obj.get("data")
        if isinstance(data, dict) and data.get("msg_ip"):
            return str(data.get("msg_ip"))
        if obj.get("msg_ip"):
            return str(obj.get("msg_ip"))
        return self.node_host

    def _send_fixed_scores_if_needed(self, obj: Dict[str, Any], vehicle_rows: List[Dict[str, Any]]) -> bool:
        ports = [int(row["vehicle_id"]) for row in vehicle_rows if row.get("vehicle_id") is not None]
        if not ports or not all(
            (self.FIXED_SCORE_PORT_MIN <= port <= self.FIXED_SCORE_PORT_MAX)
            or (self.FIXED_SCORE_PORT_MIN_EXT <= port <= self.FIXED_SCORE_PORT_MAX_EXT)
            for port in ports
        ):
            return False
        reply_host = self._reply_host(obj)
        for port in ports:
            base_port = (
                self.FIXED_SCORE_PORT_MIN
                if self.FIXED_SCORE_PORT_MIN <= port <= self.FIXED_SCORE_PORT_MAX
                else self.FIXED_SCORE_PORT_MIN_EXT
            )
            score = round(90.0 + (port - base_port) * 0.37, 3)
            payload = {
                "port": port,
                "vehicle_id": port,
                "score_total": score,
            }
            raw = json.dumps(
                {"msg_type": "VEHICLE_SCORE_RESULT", "data": payload},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            debug_append_log(
                f"[VehicleGateway] fixed_score_send to={reply_host}:{port} score_total={score}"
            )
            with socket.create_connection((reply_host, port), timeout=1.5) as downstream:
                downstream.sendall(raw)
            self._capture_message(
                "send",
                "VEHICLE_SCORE_RESULT",
                payload,
                addr=(reply_host, port),
                sender="vehicle_gateway",
                target=str(port),
            )
            self.event_log.log(
                "vehicle_gateway_fixed_score_sent",
                to_addr=f"{reply_host}:{port}",
                vehicle_port=port,
                score_total=score,
                bytes=len(raw),
            )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Vehicle gateway")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    app = VehicleGateway(args.config)
    try:
        app.start()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        time.sleep(0.1)


if __name__ == "__main__":
    main()

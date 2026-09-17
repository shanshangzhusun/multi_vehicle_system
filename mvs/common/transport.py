from __future__ import annotations

import socket
import struct
import threading
import time
import uuid
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set, Tuple

from mvs.common.debug_append_log import debug_append_log
from mvs.common.models import Envelope, make_envelope
from mvs.common.reliable_udp import ReliableUDPNode


MessageAddress = Optional[Tuple[str, int]]
OnMessage = Callable[[Envelope, MessageAddress], Any]


def _transport_defaults() -> dict:
    # 任务书描述的是 UDP 网口传输；工程实现同时保留 UDP 和 TCP。
    # 当前服务器流程默认 TCP，是为了大 JSON 分数/轨迹包有长度帧、ACK 和重试；需要 UDP 时只改配置 type。
    return {
        "type": "tcp",
        "udp": {
            "ack_timeout_sec": 0.6,
            "max_retries": 8,
        },
        "tcp": {
            "connect_timeout_sec": 1.5,
            "read_timeout_sec": 2.0,
            "max_retries": 3,
            "retry_backoff_sec": 0.15,
            "max_payload_bytes": 4 * 1024 * 1024,
        },
        "kafka": {
            "bootstrap_servers": ["127.0.0.1:9092"],
            "topic_task_package": "mvs.task.package",
            "topic_vehicle_telemetry": "mvs.vehicle.telemetry",
            "topic_command_prefix": "mvs.scheduler.command",
            "consumer_group": "mvs.default",
            "client_id": "mvs-node",
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def normalize_transport_config(cfg: Optional[dict]) -> dict:
    return _deep_merge(_transport_defaults(), cfg or {})


def _recv_exact(sock: socket.socket, size: int, timeout_sec: float) -> bytes:
    sock.settimeout(timeout_sec)
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise ConnectionError("socket closed while receiving data")
        chunks.extend(part)
    return bytes(chunks)


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _recv_frame(sock: socket.socket, timeout_sec: float, max_payload_bytes: int) -> bytes:
    header = _recv_exact(sock, 4, timeout_sec)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > max_payload_bytes:
        raise ValueError(f"invalid payload size: {size}")
    return _recv_exact(sock, size, timeout_sec)


def _try_parse_raw_json_request(obj: Dict[str, Any]) -> Envelope:
    # 对接模型/节点时允许直接发 JSON，不强制使用内部 Envelope 帧。
    # 这里把外部字段别名归一成系统消息类型，减少双方协议细节差异造成的丢包。
    msg_type = str(
        obj.get("msg_type")
        or obj.get("msgtype")
        or obj.get("msg-type")
        or obj.get("message_type")
        or obj.get("request_type")
        or obj.get("type")
        or obj.get("schema")
        or obj.get("data_type")
        or ""
    )
    aliases = {
        "time_backplan": "REQUEST_TIME_BACKPLAN",
        "request_timeback_plan": "REQUEST_TIME_BACKPLAN",
        "request_time_backplan": "REQUEST_TIME_BACKPLAN",
        "backplan": "REQUEST_TIME_BACKPLAN",
        "stage_timeline": "REQUEST_TIME_BACKPLAN",
        "stage_timeline_v1": "REQUEST_TIME_BACKPLAN",
        "time_backplan_result": "REQUEST_TIME_BACKPLAN",
        "timeback_plan_context": "TIME_BACKPLAN_CONTEXT",
        "time_backplan_context": "TIME_BACKPLAN_CONTEXT",
        "timeback_plan": "TIME_BACKPLAN_CONTEXT",
        "倒排结果请求": "REQUEST_TIME_BACKPLAN",
        "倒排结果": "REQUEST_TIME_BACKPLAN",
        "vehicle_assignment": "REQUEST_VEHICLE_ASSIGNMENT",
        "vehicle_candidate": "REQUEST_VEHICLE_CANDIDATE",
        "request_vehicle_candidate": "REQUEST_VEHICLE_CANDIDATE",
        "request_vehicle_candidate_context": "REQUEST_VEHICLE_CANDIDATE",
        "vehicle_candidate_context_request": "REQUEST_VEHICLE_CANDIDATE",
        "point_assignment": "REQUEST_VEHICLE_ASSIGNMENT",
        "point_assignment_v1": "REQUEST_VEHICLE_ASSIGNMENT",
        "assignment": "REQUEST_VEHICLE_ASSIGNMENT",
        "vehicle_assignment_result": "REQUEST_VEHICLE_ASSIGNMENT",
        "车辆分配结果请求": "REQUEST_VEHICLE_ASSIGNMENT",
        "车辆分配结果": "REQUEST_VEHICLE_ASSIGNMENT",
        "depot_assignment_context": "REQUEST_DEPOT_ASSIGNMENT_CONTEXT",
        "depot_context": "REQUEST_DEPOT_ASSIGNMENT_CONTEXT",
        "depot_assignment_context_v1": "REQUEST_DEPOT_ASSIGNMENT_CONTEXT",
        "贮备库分配上下文请求": "REQUEST_DEPOT_ASSIGNMENT_CONTEXT",
        "贮备库分配上下文": "REQUEST_DEPOT_ASSIGNMENT_CONTEXT",
    }
    msg_type = aliases.get(msg_type.lower(), msg_type)
    if not msg_type and obj.get("task_id"):
        msg_type = "TASK_PACKAGE"
    if not msg_type:
        raise ValueError("raw JSON request is missing msg_type/request_type/type")
    payload = obj.get("payload")
    data_value = obj.get("data")
    if not isinstance(payload, dict) and isinstance(data_value, dict):
        payload = data_value
    if not isinstance(payload, dict):
        payload = {
            k: v
            for k, v in obj.items()
            if k not in {"msg_type", "msgtype", "msg-type", "message_type", "request_type", "type", "schema", "data_type", "data"}
        }
        if "data" in obj:
            payload["data"] = data_value
    if obj.get("group") not in {None, ""}:
        payload["_group"] = str(obj.get("group"))
    payload["_raw_tcp_json"] = True
    payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    debug_append_log(
        f"[transport] parsed_raw_json msg_type={msg_type} sender={obj.get('sender')} "
        f"target={obj.get('target')} payload_keys={payload_keys}"
    )
    return Envelope(
        msg_id=str(obj.get("msg_id") or uuid.uuid4()),
        msg_type=msg_type,
        sender=str(obj.get("sender") or "raw_json_peer"),
        target=str(obj.get("target") or ""),
        created_at=str(obj.get("created_at") or ""),
        require_ack=False,
        ack_for=None,
        payload=payload,
    )


def _recv_tcp_payload(sock: socket.socket, timeout_sec: float, max_payload_bytes: int) -> Tuple[bytes, bool]:
    first = _recv_exact(sock, 4, timeout_sec)
    if first.lstrip()[:1] not in {b"{", b"[", b"\xef"}:
        size = struct.unpack("!I", first)[0]
        if size <= 0 or size > max_payload_bytes:
            # Be tolerant of raw JSON peers that may prepend UTF-8 BOM or other
            # non-whitespace text bytes; fall back to reading the stream as raw JSON.
            chunks = bytearray(first)
            sock.settimeout(timeout_sec)
            while len(chunks) <= max_payload_bytes:
                try:
                    json.loads(bytes(chunks).decode("utf-8-sig"))
                    return bytes(chunks), True
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                part = sock.recv(65536)
                if not part:
                    break
                chunks.extend(part)
            json.loads(bytes(chunks).decode("utf-8-sig"))
            return bytes(chunks), True
        return _recv_exact(sock, size, timeout_sec), False

    chunks = bytearray(first)
    sock.settimeout(timeout_sec)
    while len(chunks) <= max_payload_bytes:
        try:
            json.loads(bytes(chunks).decode("utf-8-sig"))
            return bytes(chunks), True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        part = sock.recv(65536)
        if not part:
            break
        chunks.extend(part)
    json.loads(bytes(chunks).decode("utf-8-sig"))
    return bytes(chunks), True


class TransportNode:
    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def send_message(
        self,
        msg_type: str,
        target: str,
        payload: dict,
        addr: MessageAddress = None,
        require_ack: bool = True,
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class UDPTransportNode(TransportNode):
    def __init__(self, node_id: str, host: str, port: int, on_message: OnMessage, cfg: dict) -> None:
        udp_cfg = cfg.get("udp", {})
        self._node = ReliableUDPNode(
            node_id=node_id,
            host=host,
            port=port,
            on_message=on_message,
            ack_timeout_sec=float(udp_cfg.get("ack_timeout_sec", 0.6)),
            max_retries=int(udp_cfg.get("max_retries", 8)),
        )

    def start(self) -> None:
        self._node.start()

    def stop(self) -> None:
        self._node.stop()

    def send_message(
        self,
        msg_type: str,
        target: str,
        payload: dict,
        addr: MessageAddress = None,
        require_ack: bool = True,
    ) -> None:
        if addr is None:
            raise ValueError("UDP transport requires addr=(host, port)")
        self._node.send_message(msg_type=msg_type, target=target, payload=payload, addr=addr, require_ack=require_ack)


class TCPTransportNode(TransportNode):
    def __init__(self, node_id: str, host: str, port: int, on_message: OnMessage, cfg: dict) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
        self.on_message = on_message
        tcp_cfg = cfg.get("tcp", {})
        self.connect_timeout_sec = float(tcp_cfg.get("connect_timeout_sec", 1.5))
        self.read_timeout_sec = float(tcp_cfg.get("read_timeout_sec", 2.0))
        self.max_retries = int(tcp_cfg.get("max_retries", 3))
        self.retry_backoff_sec = float(tcp_cfg.get("retry_backoff_sec", 0.15))
        self.max_payload_bytes = int(tcp_cfg.get("max_payload_bytes", 4 * 1024 * 1024))
        self.listen_backlog = int(tcp_cfg.get("listen_backlog", 512))

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(self.listen_backlog)
        self._server.settimeout(0.2)

        self._running = False
        self._accept_thread: Optional[threading.Thread] = None
        self._seen_lock = threading.Lock()
        self._seen_msg_ids: Set[str] = set()

    def start(self) -> None:
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._server.close()
        except OSError:
            pass
        if self._accept_thread:
            self._accept_thread.join(timeout=1.0)

    def send_message(
        self,
        msg_type: str,
        target: str,
        payload: dict,
        addr: MessageAddress = None,
        require_ack: bool = True,
    ) -> None:
        if addr is None:
            raise ValueError("TCP transport requires addr=(host, port)")
        env = make_envelope(
            msg_type=msg_type,
            sender=self.node_id,
            target=target,
            payload=payload,
            require_ack=require_ack,
        )
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
                debug_append_log(
                    f"[transport] send_message node={self.node_id} msg_type={msg_type} target={target} "
                    f"addr={addr} require_ack={require_ack} payload_keys={payload_keys}"
                )
                with socket.create_connection(addr, timeout=self.connect_timeout_sec) as sock:
                    _send_frame(sock, env.to_bytes())
                    if require_ack:
                        ack_data = _recv_frame(sock, self.read_timeout_sec, self.max_payload_bytes)
                        ack = Envelope.from_bytes(ack_data)
                        if ack.msg_type != "__ack__" or ack.ack_for != env.msg_id:
                            raise RuntimeError("invalid TCP ack received")
                return
            except Exception as exc:
                debug_append_log(
                    f"[transport] send_failed node={self.node_id} msg_type={msg_type} addr={addr} "
                    f"attempt={attempt} error={type(exc).__name__}: {exc}"
                )
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff_sec)
        raise RuntimeError(f"TCP send failed to {addr}: {last_error}")

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        with conn:
            raw_json = False
            try:
                payload, raw_json = _recv_tcp_payload(conn, self.read_timeout_sec, self.max_payload_bytes)
                debug_append_log(
                    f"[transport] recv node={self.node_id} addr={addr} raw_json={raw_json} "
                    f"bytes={len(payload)}"
                )
                if raw_json:
                    raw_obj = json.loads(payload.decode("utf-8-sig"))
                    if not isinstance(raw_obj, dict):
                        raise ValueError("raw JSON request must be an object")
                    env = _try_parse_raw_json_request(raw_obj)
                else:
                    env = Envelope.from_bytes(payload)
                payload_keys = sorted(env.payload.keys()) if isinstance(env.payload, dict) else []
                debug_append_log(
                    f"[transport] dispatch node={self.node_id} addr={addr} msg_type={env.msg_type} "
                    f"target={env.target} payload_keys={payload_keys}"
                )
                if env.require_ack and not raw_json:
                    ack = make_envelope(
                        msg_type="__ack__",
                        sender=self.node_id,
                        target=env.sender,
                        payload={},
                        require_ack=False,
                        ack_for=env.msg_id,
                    )
                    _send_frame(conn, ack.to_bytes())
                with self._seen_lock:
                    if env.msg_id in self._seen_msg_ids:
                        return
                    self._seen_msg_ids.add(env.msg_id)
                    if len(self._seen_msg_ids) > 20000:
                        self._seen_msg_ids = set(list(self._seen_msg_ids)[-10000:])
                response = self.on_message(env, addr)
                if raw_json and isinstance(response, dict):
                    conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                    response_keys = sorted(response.keys())
                    debug_append_log(
                        f"[transport] raw_json_response node={self.node_id} addr={addr} response_keys={response_keys}"
                    )
            except Exception as exc:
                debug_append_log(
                    f"[transport] handle_conn_error node={self.node_id} addr={addr} raw_json={raw_json} "
                    f"error={type(exc).__name__}: {exc}"
                )
                if raw_json:
                    try:
                        conn.sendall(
                            json.dumps(
                                {
                                    "msg_type": "ERROR",
                                    "data": {
                                        "accepted": False,
                                        "status": "ERROR",
                                        "error": f"{type(exc).__name__}: {exc}",
                                    },
                                },
                                ensure_ascii=False,
                            ).encode("utf-8")
                            + b"\n"
                        )
                    except Exception:
                        pass
                return


class KafkaTransportNode(TransportNode):
    def __init__(self, node_id: str, host: str, port: int, on_message: OnMessage, cfg: dict) -> None:
        del host, port
        self.node_id = node_id
        self.on_message = on_message
        self.cfg = cfg.get("kafka", {})
        try:
            from kafka import KafkaConsumer, KafkaProducer  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError(
                "Kafka transport selected but dependency is missing. "
                "Install kafka-python or switch transport.type to tcp."
            ) from exc
        self._KafkaConsumer = KafkaConsumer
        self._KafkaProducer = KafkaProducer
        self._producer = None
        self._consumer = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        bootstrap = self.cfg.get("bootstrap_servers", ["127.0.0.1:9092"])
        self._producer = self._KafkaProducer(bootstrap_servers=bootstrap)
        topics = []
        if self.node_id.startswith("scheduler"):
            topics.append(self.cfg.get("topic_task_package", "mvs.task.package"))
            topics.append(self.cfg.get("topic_vehicle_telemetry", "mvs.vehicle.telemetry"))
        topics.append(f"{self.cfg.get('topic_command_prefix', 'mvs.scheduler.command')}.{self.node_id}")
        self._consumer = self._KafkaConsumer(
            *topics,
            bootstrap_servers=bootstrap,
            group_id=self.cfg.get("consumer_group", "mvs.default"),
            client_id=f"{self.cfg.get('client_id', 'mvs-node')}-{self.node_id}",
            auto_offset_reset="latest",
            enable_auto_commit=True,
            value_deserializer=lambda b: b,
        )
        self._running = True
        self._consumer_thread = threading.Thread(target=self._consume_loop, daemon=True)
        self._consumer_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._consumer:
            self._consumer.close()
            self._consumer = None
        if self._producer:
            self._producer.flush(timeout=2.0)
            self._producer.close()
            self._producer = None
        if self._consumer_thread:
            self._consumer_thread.join(timeout=1.0)

    def send_message(
        self,
        msg_type: str,
        target: str,
        payload: dict,
        addr: MessageAddress = None,
        require_ack: bool = True,
    ) -> None:
        del addr, require_ack
        if self._producer is None:
            raise RuntimeError("Kafka producer not started")
        env = make_envelope(
            msg_type=msg_type,
            sender=self.node_id,
            target=target,
            payload=payload,
            require_ack=False,
        )
        topic = self._topic_for(env.msg_type, target)
        self._producer.send(topic, env.to_bytes())
        self._producer.flush(timeout=2.0)

    def _consume_loop(self) -> None:
        if self._consumer is None:
            return
        while self._running:
            for record in self._consumer.poll(timeout_ms=200, max_records=100).values():
                for item in record:
                    try:
                        env = Envelope.from_bytes(item.value)
                        self.on_message(env, None)
                    except Exception:
                        continue

    def _topic_for(self, msg_type: str, target: str) -> str:
        if msg_type == "TASK_PACKAGE":
            return self.cfg.get("topic_task_package", "mvs.task.package")
        if msg_type in {"HEARTBEAT", "PATH_PROPOSAL", "VEHICLE_EVENT"}:
            return self.cfg.get("topic_vehicle_telemetry", "mvs.vehicle.telemetry")
        return f"{self.cfg.get('topic_command_prefix', 'mvs.scheduler.command')}.{target}"


def create_transport_node(
    node_id: str,
    host: str,
    port: int,
    on_message: OnMessage,
    cfg: Optional[dict] = None,
) -> TransportNode:
    merged = normalize_transport_config(cfg)
    t = str(merged.get("type", "tcp")).lower()
    if t == "udp":
        return UDPTransportNode(node_id=node_id, host=host, port=port, on_message=on_message, cfg=merged)
    if t == "tcp":
        return TCPTransportNode(node_id=node_id, host=host, port=port, on_message=on_message, cfg=merged)
    if t == "kafka":
        return KafkaTransportNode(node_id=node_id, host=host, port=port, on_message=on_message, cfg=merged)
    raise ValueError(f"unsupported transport type: {t}")


def send_message_once(
    *,
    sender: str,
    msg_type: str,
    target: str,
    payload: Dict[str, Any],
    transport_cfg: Optional[dict] = None,
    addr: MessageAddress = None,
    require_ack: bool = False,
) -> None:
    cfg = normalize_transport_config(transport_cfg)
    env = make_envelope(
        msg_type=msg_type,
        sender=sender,
        target=target,
        payload=payload,
        require_ack=require_ack and str(cfg.get("type", "tcp")).lower() in {"udp", "tcp"},
    )
    t = str(cfg.get("type", "tcp")).lower()
    if t == "udp":
        if addr is None:
            raise ValueError("UDP send requires addr=(host, port)")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(env.to_bytes(), addr)
        finally:
            sock.close()
        return
    if t == "tcp":
        if addr is None:
            raise ValueError("TCP send requires addr=(host, port)")
        tcp_cfg = cfg.get("tcp", {})
        connect_timeout_sec = float(tcp_cfg.get("connect_timeout_sec", 1.5))
        read_timeout_sec = float(tcp_cfg.get("read_timeout_sec", 2.0))
        max_payload_bytes = int(tcp_cfg.get("max_payload_bytes", 4 * 1024 * 1024))
        with socket.create_connection(addr, timeout=connect_timeout_sec) as sock:
            _send_frame(sock, env.to_bytes())
            if env.require_ack:
                ack = Envelope.from_bytes(_recv_frame(sock, read_timeout_sec, max_payload_bytes))
                if ack.msg_type != "__ack__" or ack.ack_for != env.msg_id:
                    raise RuntimeError("invalid TCP ack received")
        return
    if t == "kafka":
        try:
            from kafka import KafkaProducer  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError(
                "Kafka transport selected but dependency is missing. "
                "Install kafka-python or switch transport.type to tcp."
            ) from exc
        kafka_cfg = cfg.get("kafka", {})
        producer = KafkaProducer(bootstrap_servers=kafka_cfg.get("bootstrap_servers", ["127.0.0.1:9092"]))
        if msg_type == "TASK_PACKAGE":
            topic = kafka_cfg.get("topic_task_package", "mvs.task.package")
        elif msg_type in {"HEARTBEAT", "PATH_PROPOSAL", "VEHICLE_EVENT"}:
            topic = kafka_cfg.get("topic_vehicle_telemetry", "mvs.vehicle.telemetry")
        else:
            topic = f"{kafka_cfg.get('topic_command_prefix', 'mvs.scheduler.command')}.{target}"
        producer.send(topic, env.to_bytes())
        producer.flush(timeout=2.0)
        producer.close()
        return
    raise ValueError(f"unsupported transport type: {t}")

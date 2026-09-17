from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set, Tuple

from mvs.common.models import Envelope, make_envelope


@dataclass
class PendingMessage:
    envelope: Envelope
    addr: Tuple[str, int]
    last_sent_ts: float
    retry_count: int


class ReliableUDPNode:
    def __init__(
        self,
        node_id: str,
        host: str,
        port: int,
        on_message: Callable[[Envelope, Tuple[str, int]], None],
        ack_timeout_sec: float = 0.6,
        max_retries: int = 8,
    ) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
        self.on_message = on_message
        self.ack_timeout_sec = ack_timeout_sec
        self.max_retries = max_retries

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.sock.settimeout(0.2)

        self._running = False
        self._recv_thread: Optional[threading.Thread] = None
        self._retry_thread: Optional[threading.Thread] = None

        self._pending_lock = threading.Lock()
        self._pending: Dict[str, PendingMessage] = {}
        self._seen_lock = threading.Lock()
        self._seen_msg_ids: Set[str] = set()

    def start(self) -> None:
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._retry_thread = threading.Thread(target=self._retry_loop, daemon=True)
        self._recv_thread.start()
        self._retry_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=1.0)
        if self._retry_thread:
            self._retry_thread.join(timeout=1.0)
        self.sock.close()

    def send(self, env: Envelope, addr: Tuple[str, int]) -> None:
        data = env.to_bytes()
        self.sock.sendto(data, addr)
        if env.require_ack and env.msg_type != "__ack__":
            with self._pending_lock:
                self._pending[env.msg_id] = PendingMessage(
                    envelope=env,
                    addr=addr,
                    last_sent_ts=time.time(),
                    retry_count=0,
                )

    def send_message(
        self,
        msg_type: str,
        target: str,
        payload: dict,
        addr: Tuple[str, int],
        require_ack: bool = True,
    ) -> None:
        env = make_envelope(
            msg_type=msg_type,
            sender=self.node_id,
            target=target,
            payload=payload,
            require_ack=require_ack,
        )
        self.send(env, addr)

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, addr = self.sock.recvfrom(1024 * 1024)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                env = Envelope.from_bytes(data)
            except Exception:
                continue

            if env.msg_type == "__ack__" and env.ack_for:
                with self._pending_lock:
                    self._pending.pop(env.ack_for, None)
                continue

            if env.require_ack:
                ack = make_envelope(
                    msg_type="__ack__",
                    sender=self.node_id,
                    target=env.sender,
                    payload={},
                    require_ack=False,
                    ack_for=env.msg_id,
                )
                self.sock.sendto(ack.to_bytes(), addr)

            with self._seen_lock:
                if env.msg_id in self._seen_msg_ids:
                    continue
                self._seen_msg_ids.add(env.msg_id)
                if len(self._seen_msg_ids) > 20000:
                    # Keep memory bounded in long runs.
                    self._seen_msg_ids = set(list(self._seen_msg_ids)[-10000:])

            self.on_message(env, addr)

    def _retry_loop(self) -> None:
        while self._running:
            now = time.time()
            to_retry = []
            to_drop = []
            with self._pending_lock:
                for msg_id, item in self._pending.items():
                    if now - item.last_sent_ts < self.ack_timeout_sec:
                        continue
                    if item.retry_count >= self.max_retries:
                        to_drop.append(msg_id)
                        continue
                    to_retry.append(msg_id)

                for msg_id in to_drop:
                    self._pending.pop(msg_id, None)

                for msg_id in to_retry:
                    item = self._pending.get(msg_id)
                    if not item:
                        continue
                    self.sock.sendto(item.envelope.to_bytes(), item.addr)
                    item.retry_count += 1
                    item.last_sent_ts = now

            time.sleep(0.05)

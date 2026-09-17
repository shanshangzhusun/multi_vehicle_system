#!/usr/bin/env python3
from __future__ import annotations

import socket
import threading
from datetime import datetime


HOST = "0.0.0.0"
PORT = 9110
REPLY = b"yes\n"
MAX_BYTES = 1024 * 1024


def handle_client(conn: socket.socket, addr: tuple[str, int]) -> None:
    with conn:
        conn.settimeout(3.0)
        chunks = bytearray()
        while len(chunks) < MAX_BYTES:
            try:
                part = conn.recv(65536)
            except socket.timeout:
                break
            if not part:
                break
            chunks.extend(part)
            if len(part) < 65536:
                break
        conn.sendall(REPLY)
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"from={addr[0]}:{addr[1]} bytes={len(chunks)} reply=yes",
            flush=True,
        )


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(128)
        print(f"[tcp-yes] listening on {HOST}:{PORT}", flush=True)
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()

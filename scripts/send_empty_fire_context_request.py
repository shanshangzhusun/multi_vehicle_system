#!/usr/bin/env python3
import json
import socket


TARGET_HOST = "192.168.2.10"
TARGET_PORT = 8414


def main() -> None:
    payload = {
        "msg_type": "REQUEST_VEHICLE",
        "data": {
            "vehicles": [
                {"vehicle_id": 8414, "port": 8414},
                {"vehicle_id": 8415, "port": 8415},
            ],
        },
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"

    print(f"sending to {TARGET_HOST}:{TARGET_PORT} {json.dumps(payload, ensure_ascii=False)}", flush=True)
    with socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=3.0) as sock:
        sock.sendall(raw)


if __name__ == "__main__":
    main()

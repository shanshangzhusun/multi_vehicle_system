#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def recv_json(conn: socket.socket, timeout: float = 3.0) -> Dict[str, Any]:
    conn.settimeout(timeout)
    chunks = bytearray()
    while True:
        part = conn.recv(65536)
        if not part:
            break
        chunks.extend(part)
        if b"\n" in part:
            break
    if not chunks:
        return {}
    return json.loads(bytes(chunks).decode("utf-8-sig").strip())


def send_json(host: str, port: int, obj: Dict[str, Any], timeout: float = 2.0) -> bool:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(data)
        return True
    except Exception as exc:
        print(f"[mock] send failed to {host}:{port} msg_type={obj.get('msg_type')} {type(exc).__name__}: {exc}", flush=True)
        return False


def data_of(obj: Dict[str, Any]) -> Any:
    return obj.get("data") if "data" in obj else obj


def load_rows_from_file(path_text: str) -> List[Dict[str, Any]]:
    if not path_text:
        return []
    path = Path(path_text)
    if not path.exists():
        return []
    obj = json.loads(path.read_text(encoding="utf-8"))
    data = obj.get("data") if isinstance(obj, dict) else obj
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [dict(row) for row in rows if isinstance(row, dict)]


def rows_from_request(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = data_of(obj)
    if isinstance(data, dict) and isinstance(data.get("vehicles"), list):
        rows = data["vehicles"]
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = [data]
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            row = {"vehicle_id": row, "port": row}
        port = row.get("port") or row.get("vehicle_id")
        if port is None:
            continue
        item = {"vehicle_id": int(port), "port": int(port)}
        if row.get("msg_ip") not in {None, ""}:
            item["msg_ip"] = row.get("msg_ip")
        if row.get("reply_host") not in {None, ""}:
            item["reply_host"] = row.get("reply_host")
        if row.get("reply_port") not in {None, ""}:
            item["reply_port"] = row.get("reply_port")
        out.append(item)
    return out


def parse_ports(value: str, start_port: int, count: int) -> List[int]:
    if value:
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    return list(range(int(start_port), int(start_port) + int(count)))


def grid_points(
    *,
    count: int,
    lon0: float,
    lat0: float,
    lon1: float,
    lat1: float,
    name_prefix: str,
    type_name: str,
    start_index: int = 1,
) -> List[Dict[str, Any]]:
    cols = 12
    rows = max(1, (count + cols - 1) // cols)
    points = []
    for idx in range(count):
        r = idx // cols
        c = idx % cols
        lon = lon0 + (lon1 - lon0) * (c + 0.5) / cols
        lat = lat0 + (lat1 - lat0) * (r + 0.5) / rows
        points.append(
            {
                "name": f"{name_prefix}_{idx + 1}",
                "index": start_index + idx,
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "alt": 0,
                "type": type_name,
            }
        )
    return points


class MockModel:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.vehicle_ports = parse_ports(args.vehicle_ports, args.vehicle_start_port, args.vehicle_count)
        self.fa_she = load_rows_from_file(args.fa_she_file) or grid_points(
            count=args.launch_count,
            lon0=args.lon0,
            lat0=args.lat0,
            lon1=args.lon1,
            lat1=args.lat1,
            name_prefix="fa_she",
            type_name="DF_FIRING_POSITION",
            start_index=1,
        )
        self.yin_bi = load_rows_from_file(args.yin_bi_file) or grid_points(
            count=args.hide_count,
            lon0=args.lon0 + 0.03,
            lat0=args.lat0 + 0.03,
            lon1=args.lon1 - 0.03,
            lat1=args.lat1 - 0.03,
            name_prefix="yin_bi",
            type_name="DF_HIDDEN_POSITION",
            start_index=1,
        )
        self.depots = load_rows_from_file(args.depot_file) or grid_points(
            count=args.depot_count,
            lon0=args.lon0 + 0.08,
            lat0=args.lat0 + 0.08,
            lon1=args.lon1 - 0.08,
            lat1=args.lat1 - 0.08,
            name_prefix="depot",
            type_name="DF_FRONT_STORAGE",
            start_index=1,
        )
        for idx, row in enumerate(self.depots):
            port = int(row.get("depot_port") or row.get("port") or row.get("vehicle_id") or args.depot_start_port + idx)
            row["depot_id"] = row.get("depot_id") or row.get("name") or str(port)
            row["depot_port"] = port
            row["vehicle_id"] = port
            row["port"] = port
            row["capacity"] = int(row.get("capacity", args.depot_capacity) or args.depot_capacity)
        loaded_vehicles = load_rows_from_file(args.vehicle_file)
        if loaded_vehicles:
            wanted = set(self.vehicle_ports)
            selected = []
            for row in loaded_vehicles:
                try:
                    port = int(row.get("port") or row.get("vehicle_id"))
                except Exception:
                    port = 0
                if port in wanted:
                    selected.append(row)
            self.vehicles = selected[: max(0, len(self.vehicle_ports))]
        else:
            self.vehicles = grid_points(
                count=len(self.vehicle_ports),
                lon0=args.lon0 + 0.06,
                lat0=args.lat0 + 0.06,
                lon1=args.lon1 - 0.06,
                lat1=args.lat1 - 0.06,
                name_prefix="vehicle",
                type_name="DF26DLaunchCar",
                start_index=self.vehicle_ports[0] if self.vehicle_ports else args.vehicle_start_port,
            )
        for idx, row in enumerate(self.vehicles):
            port = int(row.get("port") or row.get("vehicle_id") or self.vehicle_ports[idx])
            row["vehicle_id"] = port
            row["port"] = port
            if row.get("time") is None:
                row["time"] = args.sim_time
        self.task_launches: List[Dict[str, Any]] = []
        self.candidate_results_by_task: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.selected_results_sent: set[str] = set()
        self.selected_depot_results_sent: set[str] = set()
        self._server: Optional[socket.socket] = None
        self._running = False

    def serve(self) -> None:
        self._running = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.args.model_host, self.args.model_port))
        self._server.listen(128)
        self._server.settimeout(1.0)
        print(f"[mock-model] listening {self.args.model_host}:{self.args.model_port}", flush=True)
        while self._running:
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            try:
                obj = recv_json(conn)
            except Exception as exc:
                print(f"[mock-model] parse failed from {addr}: {type(exc).__name__}: {exc}", flush=True)
                return
        msg_type = str(obj.get("msg_type") or obj.get("msgtype") or "")
        print(f"[mock-model] recv {msg_type} from {addr[0]}:{addr[1]}", flush=True)
        if msg_type == "TASK_PACKAGE":
            launches = obj.get("launches")
            if isinstance(launches, list):
                self.task_launches = [dict(row) for row in launches if isinstance(row, dict)]
            send_json(self.args.scheduler_host, self.args.scheduler_port, obj)
        elif msg_type == "REQUEST_DIAN":
            self.send_dian_context()
        elif msg_type in {"TIMEBACK_PLAN_CONTEXT", "TIME_BACKPLAN_CONTEXT", "VEHICLE_CANDIDATE_CONTEXT", "VEHICLE_CONTEXT", "FA_SHE_DIAN", "YIN_BI_DIAN"}:
            self.forward_to_all_vehicles(obj)
        elif msg_type == "REQUEST_VEHICLE":
            self.send_vehicle_contexts(rows_from_request(obj))
        elif msg_type == "VEHICLE_CANDIDATE_PATH_RESULT":
            self._record_candidate_result(obj)
            send_json(self.args.scheduler_host, self.args.scheduler_port, obj)
            self._maybe_send_selected_vehicle_result(obj)
        elif msg_type == "DISPATCH_TRAJECTORY_BUNDLE":
            send_json(self.args.scheduler_host, self.args.scheduler_port, obj)
        else:
            print(f"[mock-model] logged only msg_type={msg_type} obj={obj}", flush=True)

    def send_task_package(self) -> None:
        launches = [
            {
                "subtask_id": f"{self.args.task_id}_s{i:03d}",
                "ammo_type": "HE",
                "fire_time": float(self.args.fire_time),
            }
            for i in range(self.args.task_count)
        ]
        obj = {
            "msg_type": "TASK_PACKAGE",
            "task_id": self.args.task_id,
            "sim_time": float(self.args.sim_time),
            "fire_time": float(self.args.fire_time),
            "task_count": int(self.args.task_count),
            "launches": launches,
        }
        send_json(self.args.scheduler_host, self.args.scheduler_port, obj)
        print(f"[mock-model] sent TASK_PACKAGE task_count={self.args.task_count}", flush=True)

    def _record_candidate_result(self, obj: Dict[str, Any]) -> None:
        payload = data_of(obj)
        if not isinstance(payload, dict):
            return
        task_id = str(payload.get("task_id") or self.args.task_id)
        vehicle_id = str(payload.get("vehicle_id") or payload.get("port") or "")
        if not vehicle_id:
            return
        self.candidate_results_by_task.setdefault(task_id, {})[vehicle_id] = dict(payload)

    def _candidate_best_score(self, payload: Dict[str, Any]) -> float:
        paths = payload.get("paths")
        if not isinstance(paths, list):
            return float("-inf")
        best = float("-inf")
        for path in paths:
            if not isinstance(path, dict):
                continue
            feasible_bonus = 1000000.0 if path.get("feasible") is True else 0.0
            try:
                score = float(path.get("score_total", 0.0) or 0.0)
            except Exception:
                score = 0.0
            best = max(best, feasible_bonus + score)
        return best

    def _maybe_send_selected_vehicle_result(self, obj: Dict[str, Any]) -> None:
        payload = data_of(obj)
        if not isinstance(payload, dict):
            return
        task_id = str(payload.get("task_id") or self.args.task_id)
        if task_id in self.selected_results_sent:
            return
        rows = self.candidate_results_by_task.get(task_id) or {}
        expected = min(int(self.args.request_count), int(self.args.vehicle_count))
        if len(rows) < expected:
            return

        ranked = sorted(
            rows.items(),
            key=lambda item: (-self._candidate_best_score(item[1]), item[0]),
        )
        selected_vehicle_ids = [vehicle_id for vehicle_id, _ in ranked[: int(self.args.task_count)]]
        if not selected_vehicle_ids:
            return

        launches = self.task_launches or [
            {"subtask_id": f"{task_id}_s{i:03d}"}
            for i in range(int(self.args.task_count))
        ]
        data_rows: List[Dict[str, Any]] = []
        for idx, vehicle_id in enumerate(selected_vehicle_ids):
            if idx >= len(launches):
                break
            subtask_id = str(launches[idx].get("subtask_id") or f"{task_id}_s{idx:03d}")
            data_rows.append(
                {
                    "subtask_id": subtask_id,
                    "vehicle_id": vehicle_id,
                }
            )
        if not data_rows:
            return

        selected_obj = {
            "msg_type": "SELECTED_VEHICLE_RESULT",
            "task_id": task_id,
            "data": data_rows,
        }
        if send_json(self.args.scheduler_host, self.args.scheduler_port, selected_obj):
            self.selected_results_sent.add(task_id)
            print(
                f"[mock-model] sent SELECTED_VEHICLE_RESULT task_id={task_id} selected={len(data_rows)}",
                flush=True,
            )
            depot_rows: List[Dict[str, Any]] = []
            for row in data_rows:
                vehicle_id = str(row["vehicle_id"])
                payload = rows.get(vehicle_id) or {}
                depot_options: List[Dict[str, Any]] = []
                for path in payload.get("paths") or []:
                    if not isinstance(path, dict) or path.get("feasible") is False:
                        continue
                    depot_options.extend(
                        depot for depot in path.get("depot_candidates") or [] if isinstance(depot, dict)
                    )
                if not depot_options:
                    continue
                selected_depot = min(
                    depot_options,
                    key=lambda depot: (
                        float(depot.get("total_post_fire_sec", float("inf")) or float("inf")),
                        str(depot.get("depot_id") or ""),
                    ),
                )
                depot_rows.append(
                    {
                        "task_id": task_id,
                        "subtask_id": row["subtask_id"],
                        "vehicle_id": vehicle_id,
                        "depot_id": selected_depot.get("depot_id"),
                        "depot_node": selected_depot.get("depot_node"),
                    }
                )
            if depot_rows:
                depot_obj = {
                    "msg_type": "SELECTED_DEPOT_RESULT",
                    "task_id": task_id,
                    "data": depot_rows,
                }
                if send_json(self.args.scheduler_host, self.args.scheduler_port, depot_obj):
                    self.selected_depot_results_sent.add(task_id)
                    print(
                        f"[mock-model] sent SELECTED_DEPOT_RESULT task_id={task_id} "
                        f"selected={len(depot_rows)}",
                        flush=True,
                    )

    def send_dian_context(self) -> None:
        packets = [
            {"msg_type": "FA_SHE_DIAN", "data": self.fa_she},
            {"msg_type": "YIN_BI_DIAN", "data": self.yin_bi},
        ]
        if self.depots:
            packets.append({"msg_type": "DEPOT_DIAN", "data": self.depots})
        for vehicle in self.vehicles:
            packets.append({"msg_type": "VEHICLE_DIAN", "data": vehicle})
        ok = 0
        for packet in packets:
            if send_json(self.args.scheduler_host, self.args.scheduler_port, packet):
                ok += 1
            if packet.get("msg_type") in {"FA_SHE_DIAN", "YIN_BI_DIAN", "DEPOT_DIAN"}:
                self.forward_to_all_vehicles(
                    packet,
                    retries=max(1, int(self.args.dian_forward_retries)),
                    interval_sec=max(0.0, float(self.args.dian_forward_interval_sec)),
                )
                if packet.get("msg_type") == "DEPOT_DIAN":
                    self.forward_to_all_depots(
                        packet,
                        retries=max(1, int(self.args.dian_forward_retries)),
                        interval_sec=max(0.0, float(self.args.dian_forward_interval_sec)),
                    )
            elif packet.get("msg_type") == "VEHICLE_DIAN":
                vehicle = packet.get("data") or {}
                port = int(vehicle.get("port") or vehicle.get("vehicle_id") or 0)
                if port > 0:
                    send_json(self.args.vehicle_host, port, packet)
        print(f"[mock-model] sent dian packets={ok}/{len(packets)}", flush=True)

    def send_vehicle_contexts(self, vehicles: List[Dict[str, Any]]) -> None:
        if not vehicles:
            vehicles = [{"vehicle_id": self.args.vehicle_start_port, "port": self.args.vehicle_start_port}]
        for row in vehicles:
            port = int(row.get("port") or row["vehicle_id"])
            ctx = dict(self._vehicle_by_port(port))
            ctx["vehicle_id"] = port
            ctx["port"] = port
            if row.get("msg_ip") not in {None, ""}:
                ctx["msg_ip"] = row.get("msg_ip")
            if row.get("reply_host") not in {None, ""}:
                ctx["reply_host"] = row.get("reply_host")
            if row.get("reply_port") not in {None, ""}:
                ctx["reply_port"] = row.get("reply_port")
            elif row.get("port") not in {None, ""}:
                ctx["reply_port"] = int(row.get("port"))
            if ctx.get("time") is None:
                ctx["time"] = float(self.args.sim_time)
            send_json(self.args.vehicle_host, port, {"msg_type": "VEHICLE_CONTEXT", "data": ctx})
        print(f"[mock-model] sent VEHICLE_CONTEXT count={len(vehicles)}", flush=True)

    def _vehicle_by_port(self, port: int) -> Dict[str, Any]:
        for row in self.vehicles:
            try:
                if int(row.get("port") or row.get("vehicle_id")) == int(port):
                    return row
            except Exception:
                continue
        if int(port) in self.vehicle_ports:
            idx = self.vehicle_ports.index(int(port))
        else:
            idx = max(0, min(int(port) - self.args.vehicle_start_port, len(self.vehicles) - 1))
        return self.vehicles[idx]

    def forward_to_all_vehicles(self, obj: Dict[str, Any], retries: int = 1, interval_sec: float = 0.0) -> None:
        best_count = 0
        for attempt in range(max(1, retries)):
            count = 0
            for port in self.vehicle_ports:
                if send_json(self.args.vehicle_host, port, obj):
                    count += 1
            best_count = max(best_count, count)
            if attempt + 1 < max(1, retries) and interval_sec > 0.0:
                time.sleep(interval_sec)
        print(
            f"[mock-model] forwarded {obj.get('msg_type')} to vehicles count={best_count}"
            f" retries={max(1, retries)}",
            flush=True,
        )

    def forward_to_all_depots(self, obj: Dict[str, Any], retries: int = 1, interval_sec: float = 0.0) -> None:
        depot_ports = [int(row["depot_port"]) for row in self.depots]
        best_count = 0
        for attempt in range(max(1, retries)):
            count = 0
            for port in depot_ports:
                if send_json(self.args.depot_host, port, obj):
                    count += 1
            best_count = max(best_count, count)
            if attempt + 1 < max(1, retries) and interval_sec > 0.0:
                time.sleep(interval_sec)
        print(
            f"[mock-model] forwarded {obj.get('msg_type')} to depots count={best_count}"
            f" retries={max(1, retries)}",
            flush=True,
        )


class MockNode:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.vehicle_ports = parse_ports(args.vehicle_ports, args.vehicle_start_port, args.vehicle_count)
        self.received = 0
        self._running = False

    def start_score_listeners(self) -> None:
        self._running = True
        for port in self.vehicle_ports:
            threading.Thread(target=self._listen_score_port, args=(port,), daemon=True).start()
        label = f"{self.vehicle_ports[0]}-{self.vehicle_ports[-1]}" if self.vehicle_ports else "none"
        print(
            f"[mock-node] listening scores {self.args.node_host}:{label}",
            flush=True,
        )

    def _listen_score_port(self, port: int) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.args.node_host, port))
            server.listen(32)
            server.settimeout(1.0)
            while self._running:
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    try:
                        obj = recv_json(conn)
                    except Exception as exc:
                        print(f"[mock-node] score parse failed port={port} {type(exc).__name__}: {exc}", flush=True)
                        continue
                self.received += 1
                print(f"[mock-node] score recv port={port} from={addr[0]}:{addr[1]} obj={obj}", flush=True)
        finally:
            server.close()

    def send_score_requests(self) -> None:
        for port in self.vehicle_ports[: self.args.request_count]:
            obj = {
                "msg_type": "REQUEST_VEHICLE_SCORE",
                "msg_ip": self.args.node_host,
                "port": port,
            }
            send_json(self.args.gateway_host, self.args.gateway_port, obj)
            time.sleep(self.args.request_gap_sec)
        print(f"[mock-node] sent score requests count={self.args.request_count}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local raw-JSON mock model/node for vehicle scoring flow")
    parser.add_argument("role", choices=["model", "node", "all"])
    parser.add_argument("--model-host", default="127.0.0.10")
    parser.add_argument("--model-port", type=int, default=8888)
    parser.add_argument("--scheduler-host", default="127.0.0.8")
    parser.add_argument("--scheduler-port", type=int, default=9120)
    parser.add_argument("--gateway-host", default="127.0.0.8")
    parser.add_argument("--gateway-port", type=int, default=9190)
    parser.add_argument("--vehicle-host", default="127.0.0.8")
    parser.add_argument("--depot-host", default="127.0.0.8")
    parser.add_argument("--node-host", default="127.0.0.12")
    parser.add_argument("--vehicle-start-port", type=int, default=8414)
    parser.add_argument("--vehicle-count", type=int, default=64)
    parser.add_argument("--depot-start-port", type=int, default=8601)
    parser.add_argument("--depot-count", type=int, default=24)
    parser.add_argument("--depot-capacity", type=int, default=16)
    parser.add_argument("--vehicle-ports", default="")
    parser.add_argument("--request-count", type=int, default=64)
    parser.add_argument("--request-gap-sec", type=float, default=0.02)
    parser.add_argument("--task-count", type=int, default=56)
    parser.add_argument("--task-id", default="local_mock_task")
    parser.add_argument("--launch-count", type=int, default=96)
    parser.add_argument("--hide-count", type=int, default=96)
    parser.add_argument("--sim-time", type=float, default=10.0)
    parser.add_argument("--fire-time", type=float, default=1000.0)
    parser.add_argument("--send-task", action="store_true")
    parser.add_argument("--score-delay-sec", type=float, default=8.0)
    parser.add_argument("--listen-sec", type=float, default=120.0)
    parser.add_argument("--lon0", type=float, default=112.650921)
    parser.add_argument("--lat0", type=float, default=31.174717)
    parser.add_argument("--lon1", type=float, default=115.744501)
    parser.add_argument("--lat1", type=float, default=33.111033)
    parser.add_argument("--fa-she-file", default="")
    parser.add_argument("--yin-bi-file", default="")
    parser.add_argument("--depot-file", default="")
    parser.add_argument("--vehicle-file", default="")
    parser.add_argument("--dian-forward-retries", type=int, default=5)
    parser.add_argument("--dian-forward-interval-sec", type=float, default=0.2)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    threads: List[threading.Thread] = []
    model = None
    node = None
    if args.role in {"model", "all"}:
        model = MockModel(args)
        thread = threading.Thread(target=model.serve, daemon=True)
        thread.start()
        threads.append(thread)
        time.sleep(0.3)
    if args.role in {"node", "all"}:
        node = MockNode(args)
        node.start_score_listeners()
        time.sleep(0.3)
    if args.send_task:
        if model is None:
            send_json(
                args.model_host,
                args.model_port,
                {
                    "msg_type": "TASK_PACKAGE",
                    "task_id": args.task_id,
                    "sim_time": args.sim_time,
                    "fire_time": args.fire_time,
                    "task_count": args.task_count,
                    "launches": [
                        {"subtask_id": f"{args.task_id}_s{i:03d}", "ammo_type": "HE", "fire_time": args.fire_time}
                        for i in range(args.task_count)
                    ],
                },
            )
        else:
            model.send_task_package()
    if node is not None:
        time.sleep(args.score_delay_sec)
        node.send_score_requests()
    end_at = time.time() + args.listen_sec
    try:
        while time.time() < end_at:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

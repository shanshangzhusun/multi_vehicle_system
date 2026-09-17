from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class Envelope:
    msg_id: str
    msg_type: str
    sender: str
    target: str
    created_at: str
    require_ack: bool
    ack_for: Optional[str]
    payload: Dict[str, Any]

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=True).encode("utf-8")

    @staticmethod
    def from_bytes(data: bytes) -> "Envelope":
        obj = json.loads(data.decode("utf-8"))
        return Envelope(
            msg_id=obj["msg_id"],
            msg_type=obj["msg_type"],
            sender=obj["sender"],
            target=obj["target"],
            created_at=obj["created_at"],
            require_ack=bool(obj.get("require_ack", False)),
            ack_for=obj.get("ack_for"),
            payload=obj.get("payload", {}),
        )


def new_msg_id() -> str:
    return str(uuid.uuid4())


def make_envelope(
    msg_type: str,
    sender: str,
    target: str,
    payload: Dict[str, Any],
    require_ack: bool = True,
    ack_for: Optional[str] = None,
) -> Envelope:
    return Envelope(
        msg_id=new_msg_id(),
        msg_type=msg_type,
        sender=sender,
        target=target,
        created_at=utc_now_iso(),
        require_ack=require_ack,
        ack_for=ack_for,
        payload=payload,
    )


@dataclass
class TaskPackage:
    task_id: str
    created_at: str
    dispatch_time: Optional[str]
    launches: List[Dict[str, Any]]

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TaskPackage":
        return TaskPackage(
            task_id=d["task_id"],
            created_at=d.get("created_at", utc_now_iso()),
            dispatch_time=d.get("dispatch_time"),
            launches=list(d.get("launches", [])),
        )


@dataclass
class PathSegment:
    kind: str
    node_path: List[str]
    start_at: str
    end_at: str
    wait_seconds: float = 0.0


@dataclass
class VehiclePlan:
    plan_id: str
    task_id: str
    subtask_id: str
    vehicle_id: str
    segments: List[PathSegment] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "vehicle_id": self.vehicle_id,
            "segments": [asdict(s) for s in self.segments],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "VehiclePlan":
        return VehiclePlan(
            plan_id=d["plan_id"],
            task_id=d["task_id"],
            subtask_id=d["subtask_id"],
            vehicle_id=d["vehicle_id"],
            segments=[PathSegment(**x) for x in d.get("segments", [])],
        )

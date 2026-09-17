from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from mvs.common.models import parse_iso_time


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class TaskPackageValidator:
    def __init__(
        self,
        allowed_ammo_types: Sequence[str],
        max_launches: int = 256,
        reject_past_fire_time: bool = False,
        past_time_grace_sec: float = 30.0,
    ) -> None:
        self.allowed_ammo_types = set(allowed_ammo_types)
        self.max_launches = max_launches
        self.reject_past_fire_time = reject_past_fire_time
        self.past_time_grace_sec = past_time_grace_sec

    def validate(self, payload: Dict[str, Any], now_ts: float) -> ValidationResult:
        result = ValidationResult(ok=True)

        task_id = payload.get("task_id")
        launches = payload.get("launches")

        if not isinstance(task_id, str) or not task_id.strip():
            result.errors.append("task_id 缺失或不是非空字符串")
        if not isinstance(launches, list):
            result.errors.append("launches 缺失或不是数组")
            return self._finalize(result)
        if not launches:
            result.errors.append("launches 为空")
        if len(launches) > self.max_launches:
            result.errors.append(f"launches 数量超过上限 {self.max_launches}")

        seen = set()
        for idx, launch in enumerate(launches):
            prefix = f"launch[{idx}]"
            if not isinstance(launch, dict):
                result.errors.append(f"{prefix} 不是对象")
                continue

            ammo_type = launch.get("ammo_type")
            fire_time = launch.get("fire_time")
            fire_after_sec = launch.get("fire_after_sec")
            launch_id = launch.get("launch_id") or launch.get("subtask_id")
            launch_key = ("id", launch_id) if launch_id else ("profile", ammo_type, fire_time)

            if ammo_type not in self.allowed_ammo_types:
                result.errors.append(f"{prefix}.ammo_type 非法: {ammo_type}")
            if fire_time is None and fire_after_sec is None:
                result.errors.append(f"{prefix}.fire_time 和 fire_after_sec 不能同时缺失")
                continue
            if fire_after_sec is not None and not isinstance(fire_after_sec, (int, float)):
                result.errors.append(f"{prefix}.fire_after_sec 不是数值")
            if fire_time is None:
                continue
            if not isinstance(fire_time, str):
                result.errors.append(f"{prefix}.fire_time 不是字符串")
                continue

            try:
                fire_dt = parse_iso_time(fire_time)
            except Exception:
                result.errors.append(f"{prefix}.fire_time 不是合法 ISO 时间: {fire_time}")
                continue

            fire_ts = fire_dt.timestamp()
            lag = now_ts - fire_ts
            if lag > self.past_time_grace_sec:
                msg = f"{prefix}.fire_time 已早于当前时间 {lag:.1f}s"
                if self.reject_past_fire_time:
                    result.errors.append(msg)
                else:
                    result.warnings.append(msg)

            if launch_key in seen:
                result.warnings.append(f"{prefix} 与前面子项重复")
            seen.add(launch_key)

        return self._finalize(result)

    @staticmethod
    def _finalize(result: ValidationResult) -> ValidationResult:
        result.ok = len(result.errors) == 0
        return result

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union


class MLPTimePredictor:
    """
    Small configurable MLP inference engine.
    Model file is JSON so it can run without third-party ML dependencies.


    """

    def __init__(self, model_path: Optional[str] = None, fallback_bias: float = 1.05) -> None:
        self.model_path = model_path
        self.fallback_bias = fallback_bias
        self.model = self._load_model(model_path) if model_path else None
        self.predict_count = 0
        self.network_predict_count = 0
        cache_cfg = self.model.get("cache") if isinstance(self.model, dict) else {}
        self.cache_enabled = bool(cache_cfg.get("enabled", False)) if isinstance(cache_cfg, dict) else False
        self.cache_length_bin_m = float(cache_cfg.get("length_bin_m", 5.0) or 5.0) if isinstance(cache_cfg, dict) else 5.0
        self.cache_curvature_bin = float(cache_cfg.get("curvature_bin", 0.0005) or 0.0005) if isinstance(cache_cfg, dict) else 0.0005
        self.cache_speed_bin_mps = float(cache_cfg.get("speed_bin_mps", 0.5) or 0.5) if isinstance(cache_cfg, dict) else 0.5
        self._cache: Dict[Tuple[object, ...], Tuple[float, float]] = {}
        self.cache_hit_count = 0

    def predict_seconds(self, distance_m: float, speed_mps: float, phase: str) -> float:
        return self.predict_with_context(
            {
                "distance_m": distance_m,
                "path_length_m": distance_m,
                "speed_mps": speed_mps,
                "phase": phase,
                "edge_count": 1.0,
                "curvature": 0.0,
            }
        )

    def predict_with_context(self, context: Dict[str, Union[float, str]]) -> float:
        return self.predict_segment(context)[0]

    def predict_segment(self, context: Dict[str, Union[float, str]]) -> Tuple[float, float]:
        # 对应公式(8)~(11)的运行入口：有 JSON MLP 模型时执行真实前向推理。
        # 没有模型时按 distance/speed 乘 fallback_bias 降级，保证离线服务器不因缺库中断。
        self.predict_count += 1
        distance_m = float(context.get("distance_m", context.get("path_length_m", 0.0)) or 0.0)
        speed_mps = float(context.get("enter_speed_mps", context.get("speed_mps", 0.0)) or 0.0)
        if distance_m <= 0:
            return 0.0, max(0.0, speed_mps)
        if speed_mps <= 0:
            return distance_m, 0.1
        baseline = distance_m / speed_mps
        if not self.model:
            return baseline * self.fallback_bias, speed_mps

        cache_key = self._cache_key(context, distance_m, speed_mps) if self.cache_enabled else None
        if cache_key is not None and cache_key in self._cache:
            self.cache_hit_count += 1
            seconds, speed_out = self._cache[cache_key]
            return seconds, speed_out

        features = self._build_features(context)
        self.network_predict_count += 1
        if self.model.get("output_type") == "exit_speed_mps":
            speed_out = self._clip_exit_speed(
                self._forward(features),
                speed_in=speed_mps,
                allowed_speed=float(context.get("allowed_speed_mps", context.get("speed_limit_mps", speed_mps)) or speed_mps),
                max_accel=float(context.get("max_accel_mps2", context.get("max_accel", 1.0)) or 1.0),
                length_m=distance_m,
            )
            seconds = 2.0 * distance_m / max(1e-6, speed_mps + speed_out)
            if cache_key is not None:
                self._cache[cache_key] = (seconds, speed_out)
            return seconds, speed_out

        factor = self._forward(features)
        min_time = baseline * 0.7
        max_time = baseline * 2.0
        seconds = min(max(min_time, baseline * factor), max_time)
        if cache_key is not None:
            self._cache[cache_key] = (seconds, speed_mps)
        return seconds, speed_mps

    @property
    def network_enabled(self) -> bool:
        return bool(self.model)

    def model_summary(self) -> Dict[str, Union[str, int, bool]]:
        layers = self.model.get("layers", []) if self.model else []
        feature_names = self.model.get("feature_names", []) if self.model else []
        return {
            "network_enabled": self.network_enabled,
            "model_path": self.model_path or "",
            "feature_count": len(feature_names),
            "layer_count": len(layers),
            "cache_enabled": self.cache_enabled,
            "cache_size": len(self._cache),
        }

    def _cache_key(
        self,
        context: Dict[str, Union[float, str]],
        distance_m: float,
        speed_mps: float,
    ) -> Tuple[object, ...]:
        def q(value: float, step: float) -> int:
            return int(round(float(value) / max(1e-9, step)))

        curvature = float(context.get("curvature", 0.0) or 0.0)
        allowed_speed = float(context.get("allowed_speed_mps", context.get("speed_limit_mps", speed_mps)) or speed_mps)
        enter_speed = float(context.get("enter_speed_mps", context.get("speed_mps", speed_mps)) or speed_mps)
        return (
            q(distance_m, self.cache_length_bin_m),
            q(curvature, self.cache_curvature_bin),
            q(speed_mps, self.cache_speed_bin_mps),
            q(allowed_speed, self.cache_speed_bin_mps),
            q(enter_speed, self.cache_speed_bin_mps),
            str(context.get("phase", "")),
            self.model.get("output_type", "time_factor") if self.model else "",
        )

    def _load_model(self, path: str) -> Optional[Dict]:
        model_path = Path(path)
        if not model_path.exists():
            return None
        return json.loads(model_path.read_text(encoding="utf-8"))

    def _build_features(self, context: Dict[str, Union[float, str]]) -> List[float]:
        # 栅格化特征构造：优先按模型文件里的 feature_names 取值并标准化。
        # 这样离线部署时只需要替换 JSON 模型，不需要改代码或安装 torch。
        if self.model and self.model.get("feature_names"):
            raw = [self._feature_value(name, context) for name in self.model["feature_names"]]
            mean = self.model.get("input_mean", [0.0] * len(raw))
            std = self.model.get("input_std", [1.0] * len(raw))
            return [(x - m) / max(1e-6, s) for x, m, s in zip(raw, mean, std)]

        distance_m = float(context.get("distance_m", context.get("path_length_m", 0.0)) or 0.0)
        speed_mps = float(context.get("speed_mps", 0.0) or 0.0)
        phase = str(context.get("phase", ""))
        phase_map = {
            "to_fire": [1.0, 0.0, 0.0],
            "to_depot": [0.0, 1.0, 0.0],
            "return_home": [0.0, 0.0, 1.0],
        }
        phase_feat = phase_map.get(phase, [0.0, 0.0, 0.0])
        base = [distance_m, speed_mps] + phase_feat
        mean = self.model.get("input_mean", [0.0] * len(base))
        std = self.model.get("input_std", [1.0] * len(base))
        return [(x - m) / max(1e-6, s) for x, m, s in zip(base, mean, std)]

    @staticmethod
    def _feature_value(name: str, context: Dict[str, Union[float, str]]) -> float:
        phase = str(context.get("phase", ""))
        if name == "phase_to_fire":
            return 1.0 if phase == "to_fire" else 0.0
        if name == "phase_to_depot":
            return 1.0 if phase == "to_depot" else 0.0
        if name in {"phase_return_home", "phase_to_return_home"}:
            return 1.0 if phase == "return_home" else 0.0
        aliases = {
            "length_m": "distance_m",
            "allowed_speed_mps": "speed_limit_mps",
            "enter_speed_mps": "speed_mps",
            "max_accel_mps2": "max_accel",
        }
        if name in aliases and name not in context:
            return float(context.get(aliases[name], 0.0) or 0.0)
        return float(context.get(name, 0.0) or 0.0)

    @staticmethod
    def _clip_exit_speed(
        raw_speed: float,
        *,
        speed_in: float,
        allowed_speed: float,
        max_accel: float,
        length_m: float,
    ) -> float:
        # 对神经网络输出的出口速度做物理约束裁剪。
        # 同时受道路允许速度和最大加速度限制，避免网络输出不合理速度。
        speed_in = max(0.1, float(speed_in))
        allowed_speed = max(0.1, float(allowed_speed))
        max_accel = max(0.1, float(max_accel))
        length_m = max(0.1, float(length_m))
        accel_limited = math.sqrt(max(0.0, speed_in * speed_in + 2.0 * max_accel * length_m))
        decel_limited = math.sqrt(max(0.01, speed_in * speed_in - 2.0 * max_accel * length_m))
        upper = min(allowed_speed, accel_limited)
        lower = min(upper, decel_limited)
        return min(upper, max(lower, float(raw_speed)))

    def _forward(self, features: Sequence[float]) -> float:
        # 纯 Python MLP 前向传播：逐层执行 weights*x+b 和激活函数。
        # 这个实现避免第三方深度学习依赖，适合 openEuler 离线环境直接运行。
        x = list(features)
        for layer in self.model.get("layers", []):
            weights = layer["weights"]
            bias = layer["bias"]
            activation = layer.get("activation", "linear")
            out: List[float] = []
            for row, b in zip(weights, bias):
                val = sum(w * xi for w, xi in zip(row, x)) + b
                out.append(self._activate(val, activation))
            x = out
        return max(0.0, x[0] if x else 0.0)

    @staticmethod
    def _activate(value: float, activation: str) -> float:
        if activation == "relu":
            return max(0.0, value)
        if activation == "tanh":
            return math.tanh(value)
        if activation == "softplus":
            if value > 20:
                return value
            return math.log1p(math.exp(value))
        return value

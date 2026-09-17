#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np


FEATURE_NAMES = [
    "length_m",
    "curvature",
    "grade",
    "allowed_speed_mps",
    "enter_speed_mps",
    "max_accel_mps2",
]

PHASES = ["to_fire", "to_depot", "return_home"]


def iter_edges(graph_paths: Sequence[Path]) -> Iterable[Dict]:
    seen = set()
    for path in graph_paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for edge in data.get("edges", []):
            if not isinstance(edge, dict):
                continue
            key = (path.as_posix(), edge.get("id"), edge.get("from"), edge.get("to"))
            if key in seen:
                continue
            seen.add(key)
            yield edge


def edge_length(edge: Dict) -> float:
    value = edge.get("cost", edge.get("length_m", edge.get("length")))
    try:
        length = float(value)
    except Exception:
        length = 0.0
    if length > 0.0:
        return length
    geom = edge.get("geometry") or []
    total = 0.0
    for a, b in zip(geom, geom[1:]):
        try:
            total += math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"]))
        except Exception:
            pass
    return max(0.0, total)


def target_exit_speed(
    length_m: float,
    curvature: float,
    allowed_speed: float,
    enter_speed: float,
    max_accel: float,
) -> float:
    # 任务书公式(8)：网络输出栅格/路段出口速度 V_out。
    # 训练标签以当前路网几何和 80km/h 配置速度生成，不虚构阶段惩罚；
    # 曲率来自路网元数据，影响很弱，只用于模拟弯道轻微减速。
    curvature_factor = 1.0 - min(0.03, abs(curvature) * 6.0)
    desired = max(0.1, allowed_speed * curvature_factor)
    accel_limited = math.sqrt(max(0.0, enter_speed * enter_speed + 2.0 * max_accel * max(0.1, length_m)))
    return min(allowed_speed, desired, accel_limited)


def build_dataset(edges: Sequence[Dict], samples_per_edge: int, max_speed_mps: float, seed: int) -> Tuple[np.ndarray, np.ndarray]:
    rng = random.Random(seed)
    xs: List[List[float]] = []
    ys: List[float] = []
    for edge in edges:
        length = edge_length(edge)
        if length <= 0.5:
            continue
        try:
            curvature = float(edge.get("curvature", 0.0) or 0.0)
        except Exception:
            curvature = 0.0
        for _ in range(max(1, samples_per_edge)):
            # 速度统一按车辆最大速度 80km/h，避免神经网络改变任务时间尺度。
            allowed_speed = max_speed_mps
            enter_speed = rng.uniform(max_speed_mps * 0.75, max_speed_mps)
            max_accel = rng.uniform(0.8, 1.4)
            xs.append(
                [
                    length,
                    curvature,
                    0.0,
                    allowed_speed,
                    enter_speed,
                    max_accel,
                ]
            )
            ys.append(target_exit_speed(length, curvature, allowed_speed, enter_speed, max_accel))
    return np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64).reshape(-1, 1)


def relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(x, 0.0)


def train_mlp(
    x: np.ndarray,
    y: np.ndarray,
    *,
    hidden1: int,
    hidden2: int,
    epochs: int,
    lr: float,
    seed: int,
) -> Tuple[Dict, Dict[str, float]]:
    rng = np.random.default_rng(seed)
    mean = x.mean(axis=0)
    std = np.maximum(x.std(axis=0), 1e-6)
    xn = (x - mean) / std
    n, input_dim = xn.shape
    idx = np.arange(n)
    rng.shuffle(idx)
    split = max(1, int(n * 0.85))
    train_idx, val_idx = idx[:split], idx[split:]
    xt, yt = xn[train_idx], y[train_idx]
    xv, yv = xn[val_idx], y[val_idx]

    w1 = rng.normal(0.0, math.sqrt(2.0 / input_dim), size=(hidden1, input_dim))
    b1 = np.zeros((1, hidden1))
    w2 = rng.normal(0.0, math.sqrt(2.0 / hidden1), size=(hidden2, hidden1))
    b2 = np.zeros((1, hidden2))
    w3 = rng.normal(0.0, math.sqrt(1.0 / hidden2), size=(1, hidden2))
    b3 = np.zeros((1, 1))

    batch_size = min(4096, max(128, len(xt) // 8))
    for _epoch in range(max(1, epochs)):
        order = np.arange(len(xt))
        rng.shuffle(order)
        for start in range(0, len(order), batch_size):
            batch = order[start : start + batch_size]
            xb = xt[batch]
            yb = yt[batch]
            z1 = xb @ w1.T + b1
            a1 = relu(z1)
            z2 = a1 @ w2.T + b2
            a2 = relu(z2)
            pred = a2 @ w3.T + b3
            grad = (2.0 / len(xb)) * (pred - yb)
            gw3 = grad.T @ a2
            gb3 = grad.sum(axis=0, keepdims=True)
            ga2 = grad @ w3
            gz2 = ga2 * (z2 > 0.0)
            gw2 = gz2.T @ a1
            gb2 = gz2.sum(axis=0, keepdims=True)
            ga1 = gz2 @ w2
            gz1 = ga1 * (z1 > 0.0)
            gw1 = gz1.T @ xb
            gb1 = gz1.sum(axis=0, keepdims=True)
            for param, grad_param in ((w1, gw1), (b1, gb1), (w2, gw2), (b2, gb2), (w3, gw3), (b3, gb3)):
                param -= lr * np.clip(grad_param, -5.0, 5.0)

    def predict(xn_: np.ndarray) -> np.ndarray:
        return relu(relu(xn_ @ w1.T + b1) @ w2.T + b2) @ w3.T + b3

    pred_train = predict(xt)
    pred_val = predict(xv) if len(xv) else pred_train
    metrics = {
        "train_samples": int(len(xt)),
        "validation_samples": int(len(xv)),
        "train_mae_exit_speed_mps": float(np.mean(np.abs(pred_train - yt))),
        "validation_mae_exit_speed_mps": float(np.mean(np.abs(pred_val - yv))) if len(xv) else 0.0,
        "validation_rmse_exit_speed_mps": float(np.sqrt(np.mean((pred_val - yv) ** 2))) if len(xv) else 0.0,
    }
    model = {
        "model_type": "eta_exit_speed_mlp",
        "output_type": "exit_speed_mps",
        "description": "Taskbook-style JSON MLP ETA model: input road-grid features and enter speed, output exit speed.",
        "feature_names": FEATURE_NAMES,
        "input_mean": [float(v) for v in mean],
        "input_std": [float(v) for v in std],
        "layers": [
            {"weights": w1.tolist(), "bias": b1.ravel().tolist(), "activation": "relu"},
            {"weights": w2.tolist(), "bias": b2.ravel().tolist(), "activation": "relu"},
            {"weights": w3.tolist(), "bias": b3.ravel().tolist(), "activation": "linear"},
        ],
        "metadata": metrics,
    }
    return model, metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a pure-JSON MLP ETA model from current road graph data.")
    parser.add_argument("--graph", action="append", default=[], help="road graph JSON path; can be repeated")
    parser.add_argument("--out", default="models/eta_mlp_model.json", help="output JSON model path")
    parser.add_argument("--samples-per-edge", type=int, default=1)
    parser.add_argument("--max-edges", type=int, default=120000)
    parser.add_argument("--hidden1", type=int, default=400)
    parser.add_argument("--hidden2", type=int, default=300)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--lr", type=float, default=0.003)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--max-speed-mps", type=float, default=22.222)
    args = parser.parse_args()

    graph_paths = [Path(p) for p in args.graph]
    if not graph_paths:
        graph_paths = [
            Path("result/theaters/henan/data/road_graph_shp_demo.json"),
            Path("result/theaters/guangdong/data/road_graph_shp_demo.json"),
            Path("result/data/road_graph_shp_demo.json"),
        ]
    edges = list(iter_edges(graph_paths))
    random.Random(args.seed).shuffle(edges)
    if args.max_edges > 0:
        edges = edges[: args.max_edges]
    x, y = build_dataset(edges, args.samples_per_edge, args.max_speed_mps, args.seed)
    if len(x) < 10:
        raise SystemExit("not enough road graph samples to train ETA MLP")
    model, metrics = train_mlp(
        x,
        y,
        hidden1=args.hidden1,
        hidden2=args.hidden2,
        epochs=args.epochs,
        lr=args.lr,
        seed=args.seed,
    )
    baseline_sec = x[:, 0] / np.maximum(x[:, 3], 1e-6)
    target_sec = 2.0 * x[:, 0] / np.maximum(1e-6, x[:, 4] + y.ravel())
    metrics.update(
        {
            "graph_paths": [p.as_posix() for p in graph_paths if p.exists()],
            "edge_samples": int(len(x)),
            "max_speed_mps": float(args.max_speed_mps),
            "mean_baseline_seconds": float(np.mean(baseline_sec)),
            "mean_target_seconds": float(np.mean(target_sec)),
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    model["metadata"] = metrics
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": out.as_posix(), **metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

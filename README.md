# 多车协同调度本地测试与服务器流程

- 本地测试：在一台机器上启动调度、车辆、mock 模型/节点，跑完整任务、车辆规划、贮备库选择、冲突消解和结果出图。
- 服务器流程：在服务器或局域网环境启动真实联调进程，按平台拆分运行。

## 一、本地测试流程

### 1. 清理旧进程和旧结果

```bash
bash scripts/stack_down.sh
rm -rf .run
```

### 2. 生成本地配置

16 车快速测试：

```bash
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config configs/deployment_topology.local_16.json
```

64/128 车联调数据测试：

```bash
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config configs/deployment_topology.local_192.json
```

需要调整车辆数量时，改部署文件里的：

- `vehicle_count`
- `score_port_count`
- `enable_extended_128`

是否保存收发消息，改部署文件里的：

```json
"message_capture": {
  "enabled": true
}
```

### 3. 启动调度

```bash
bash scripts/run_three_platform_flow.sh scheduler \
  --deployment-config configs/deployment_topology.local_16.json
```

或直接启动生成后的调度配置：

```bash
python3 -u -m mvs.scheduler.scheduler_app \
  --config result/configs/scheduler_debug.json
```

### 4. 启动车辆

```bash
bash scripts/run_three_platform_flow.sh vehicles \
  --deployment-config configs/deployment_topology.local_16.json
```

也可以用底层启动脚本：

```bash
bash scripts/stack_up.sh \
  --scheduler-config result/configs/scheduler_debug.json \
  --no-scheduler \
  --vehicle-gateway-config result/configs/vehicle_gateway.json \
  --vehicles-dir result/configs/vehicles
```

### 5. 启动本地 mock 模型/节点

mock 会发送任务、点位、车辆评分请求，并在车辆返回候选路径后模拟模型选择车辆和贮备库。

```bash
python3 scripts/mock_local_model_node.py all \
  --model-host 127.0.0.10 \
  --model-port 8888 \
  --scheduler-host 127.0.0.8 \
  --scheduler-port 9120 \
  --gateway-host 127.0.0.8 \
  --gateway-port 9190 \
  --vehicle-host 127.0.0.8 \
  --node-host 127.0.0.12 \
  --vehicle-start-port 8414 \
  --vehicle-count 64 \
  --request-count 64 \
  --task-count 56 \
  --fire-time 1800 \
  --listen-sec 240 \
  --send-task \
  --fa-she-file result/extracted_dian/FA_SHE_DIAN.json \
  --yin-bi-file result/extracted_dian/YIN_BI_DIAN.json \
  --depot-file result/extracted_dian/DEPOT_DIAN.json \
  --vehicle-file result/extracted_dian/VEHICLE_DIAN.json
```

### 6. 验证是否生成最终无冲突轨迹

```bash
grep -nE 'external_dispatch_resolved|dispatch_trajectory_bundle_sent|DISPATCH_TRAJECTORY_BUNDLE|conflict_audit' \
  .run/logs/manual_debug.log
```

重点看：

- `resolved_count` 是否等于任务需求车辆数。
- `unresolved_count` 是否为 `0`。
- `conflict_audit.ok` 是否为 `true`。
- 最终是否发送 `DISPATCH_TRAJECTORY_BUNDLE`。

### 7. 导出最终轨迹

```bash
python3 scripts/extract_latest_dispatch_bundle.py \
  --log .run/logs/manual_debug.log \
  --out result/latest_dispatch_trajectory_bundle.json
```

### 8. 绘制最终路径图

```bash
python3 scripts/render_latest_dispatch_routes.py \
  --bundle result/latest_dispatch_trajectory_bundle.json \
  --scheduler-config result/configs/scheduler_debug.json \
  --received-dir result/received_dian \
  --dian-dir result/extracted_dian \
  --out result/visuals/latest_dispatch_routes.png \
  --focus-routes-only
```

### 9. 绘制点位图

```bash
python3 scripts/render_dian_separate.py \
  --scheduler-config result/configs/scheduler_debug.json \
  --dian-dir result/extracted_dian \
  --out-dir result/visuals/dian_separate
```

主要输出：

- `result/latest_dispatch_trajectory_bundle.json`
- `result/visuals/latest_dispatch_routes.png`
- `result/visuals/dian_separate/fire_points.png`
- `result/visuals/dian_separate/hide_points.png`
- `result/visuals/dian_separate/vehicle_points.png`

## 二、只复现调度冲突消解

车辆端候选路径已经保存时，可以不重新启动车辆，只重放调度里的冲突消解。

```bash
python3 scripts/replay_conflict_resolution.py \
  --scheduler-config result/configs/schedulers/scheduler_002.json \
  --event-log logs/scheduler_002_events.jsonl \
  --out-dir result/conflict_replay \
  --render
```

默认读取：

```text
result/message_capture/vehicle/*/send/*_VEHICLE_CANDIDATE_PATH_RESULT.json
```

验收标准仍然是：

- `resolved_count` 等于任务需求车辆数。
- `unresolved_count` 为 `0`。
- `conflict_audit.ok` 为 `true`。

## 三、服务器流程

服务器实际流程使用 `deployment_topology.local_192.json`，主流程启动仍然是 `scheduler` 和 `vehicles`，不是 `*-server`。

### 1. 修改代码后重新生成配置

```bash
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config configs/deployment_topology.local_192.json
```

`prepare` 会清理并重新生成 `result/configs/` 下的调度、车辆、gateway 等运行配置。

### 1.1 路段时间预测模式

当前服务器联调默认保持“无神经网络权重文件”的旧模式：

```json
"eta_time_predictor": {
  "mode": "fallback",
  "model_path": ""
}
```

该模式下车辆配置里的 `time_predict_model_path` 为空，车辆不会加载 MLP 权重文件；路段时间按车辆配置速度和距离估算，保持之前的运行逻辑。

如果后续需要对比神经网络模式，只改 `mode` 后重新执行 `prepare`：

```json
"eta_time_predictor": {
  "mode": "student",
  "student_model_path": "models/eta_mlp_runtime_32x16_exit_speed.json",
  "teacher_cached_model_path": "models/eta_mlp_teacher_400x300_exit_speed.json",
  "teacher_direct_model_path": "models/eta_mlp_teacher_400x300_exit_speed.json",
  "teacher_direct_variant_path": "models/eta_mlp_teacher_400x300_exit_speed_direct.json",
  "model_path": ""
}
```

可选值：

- `fallback`：不使用权重文件，按距离/速度估算时间，当前默认。
- `student`：使用小型出口速度 MLP，速度快，适合 128 车在线对比。
- `teacher_cached`：使用 400/300 教师 MLP，并启用查表缓存。
- `teacher_direct`：使用 400/300 教师 MLP 直接预测，不启用查表，主要用于耗时对比。

服务器切换流程：

1. 修改 `configs/deployment_topology.local_192.json` 中的 `eta_time_predictor.mode`。

```json
"mode": "fallback"
```

2. 重新生成运行配置。

```bash
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config configs/deployment_topology.local_192.json
```

3. 确认车辆配置已经生效。

```bash
python3 - <<'PY'
import json
from pathlib import Path
for vid in ["8414", "8519"]:
    p = Path("result/configs/vehicles") / f"{vid}.json"
    if p.exists():
        cfg = json.loads(p.read_text())
        print(vid, repr(cfg.get("time_predict_model_path", "")))
PY
```

期望结果：

- `fallback`：输出 `''`，表示不加载权重文件。
- `student`：输出 `models/eta_mlp_runtime_32x16_exit_speed.json`。
- `teacher_cached`：输出 `models/eta_mlp_teacher_400x300_exit_speed.json`。
- `teacher_direct`：输出 `models/eta_mlp_teacher_400x300_exit_speed_direct.json`。

### 2. 启动调度

```bash
bash scripts/run_three_platform_flow.sh scheduler \
  --deployment-config configs/deployment_topology.local_192.json
```

### 3. 启动车辆

```bash
bash scripts/run_three_platform_flow.sh vehicles \
  --deployment-config configs/deployment_topology.local_192.json
```

### 4. 端口检查

常用端口：

- `9190`：车辆 gateway。
- `9120`、`9121`：调度。
- `8414-8477`、`8519-8572`：车辆。

检查示例：

```bash
ss -ltnp | grep 9190
ss -ltnp | grep 9120
ss -ltnp | grep 8414
```

### 5. 模拟节点发送车辆评分请求

按车辆端口和机器 IP 映射发送：

```bash
bash scripts/send_vehicle_score_requests_mapped.sh 192.168.2.8 9190 0.1 \
  8414-8430=192.168.2.12 \
  8431-8477=192.168.2.14 \
  8519-8582=192.168.2.15
```

### 6. 收发文件保存开关

在 `configs/deployment_topology.local_192.json` 中找到：

```json
"message_capture": {
  "enabled": true
}
```

需要关闭收发文件保存时，把 `true` 改为 `false`。

### 7. 车辆数量和 128 车开关

改车辆数量需要同步修改 `configs/deployment_topology.local_192.json` 中的：

- `vehicle_count`
- `score_port_count`

开启 128 辆车：

```json
"enable_extended_128": true
```

关闭 128 辆车：

```json
"enable_extended_128": false
```

### 8. 画最终路径图

```bash
python3 scripts/render_latest_dispatch_routes.py \
  --bundle result/latest_dispatch_trajectory_bundle.json \
  --scheduler-config result/configs/scheduler_debug.json \
  --received-dir result/received_dian \
  --dian-dir result/extracted_dian \
  --out result/visuals/latest_dispatch_routes.png \
  --focus-routes-only
```

### 9. 日志位置

主要运行日志在：

```text
.run/logs/manual_debug.log
```

如果只想从日志提取最新 bundle：

```bash
python3 scripts/extract_latest_dispatch_bundle.py \
  --log .run/logs/manual_debug.log \
  --out result/latest_dispatch_trajectory_bundle.json
```

## 四、当前消息推进格式

主流程消息顺序：

```text
TASK_PACKAGE
-> TASK_PACKAGE_RECEIPT
-> REQUEST_DIAN
-> FA_SHE_DIAN / YIN_BI_DIAN / DEPOT_DIAN / VEHICLE_DIAN
-> TIME_BACKPLAN_CONTEXT
-> VEHICLE_CANDIDATE_CONTEXT
-> VEHICLE_CANDIDATE_PATH_RESULT
-> SELECTED_VEHICLE_RESULT
-> 调度完成射前冲突消解
-> DEPOT_VEHICLE_SCORE_CONTEXT
-> DEPOT_VEHICLE_SCORE_RESULT
-> 调度按分数矩阵和 capacity 全局分配贮备库
-> VEHICLE_DEPOT_ASSIGNMENT
-> VEHICLE_POST_FIRE_PATH_RESULT
-> 调度完成射后冲突消解并与射前轨迹拼接
-> DISPATCH_TRAJECTORY_BUNDLE
```

车辆第一次规划只生成“起点/隐蔽点 -> 发射点”的射前候选路径，不携带贮备库后缀。模型只转发车辆候选路径和选车结果，不再选择或分配贮备库。

射前冲突消解后，调度将最终的车辆与发射点对应关系分别发给本战区每个贮备库。贮备库按道路距离和距离排名返回分数数组；调度每次取矩阵全局最小分数，删除该车辆列并扣减对应库的 `capacity`，容量为 0 后删除该库行。

车辆只有收到 `VEHICLE_DEPOT_ASSIGNMENT` 后才规划“发射点 -> 指定贮备库 -> 补充 -> 本车起点”，并用 `VEHICLE_POST_FIRE_PATH_RESULT` 直接返回调度。如果容量总和不足、评分矩阵不完整、车辆射后规划失败或射后冲突未解决，调度记录失败状态且不发送伪完整结果。

最终调度输出 `DISPATCH_TRAJECTORY_BUNDLE` 时，应包含冲突消解后的整条轨迹，以及贮备库相关字段：

- `depot_id`
- `depot_port`
- `depot_arrival_time`
- `reload_end_time`
- `reload_duration_sec`
- `post_fire_segments`
- `post_fire_delay_sec`

## 五、清理和状态

查看状态：



停止进程：

```bash
bash scripts/run_three_platform_flow.sh down \
  --deployment-config configs/deployment_topology.local_192.json
```

彻底重置：

```bash
bash scripts/run_three_platform_flow.sh reset \
  --deployment-config configs/deployment_topology.local_192.json
```

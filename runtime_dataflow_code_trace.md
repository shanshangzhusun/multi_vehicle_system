# 全流程运行与数据流代码对应说明

生成时间：2026-08-12

本文按一次真实服务器链路从任务包下发到调度输出最终轨迹的顺序整理。每一步都包含：输入消息、代码入口、核心处理、输出消息或状态、可核对日志/抓包位置。行号基于当前工作区代码快照。

## 0. 通讯层如何把外部 JSON 送到业务入口

输入消息：外部模型、车辆、贮备库或脚本发来的 TCP JSON/Envelope。

代码对应：
- `mvs/common/transport.py:254-272`：`TCPTransportNode.__init__` 建立监听 socket，读取 TCP 配置中的超时、重试、最大包大小。
- `mvs/common/transport.py:337-345`：`_accept_loop` 持续接收连接，每个连接交给独立线程处理。
- `mvs/common/transport.py:347-384`：`_handle_conn` 接收 TCP 内容，识别 raw JSON 或内部 Envelope，解析出 `env.msg_type` 后调用节点自己的 `on_message`。
- `mvs/common/transport.py:385-414`：raw JSON 请求如果业务函数返回 dict，则直接写回 JSON；异常时返回 `ERROR`。
- `mvs/common/platform_interfaces.py:16-50`：定义全流程使用的消息类型常量，例如 `REQUEST_DIAN`、`FA_SHE_DIAN`、`VEHICLE_CANDIDATE_PATH_RESULT`、`DEPOT_VEHICLE_SCORE_CONTEXT`、`VEHICLE_POST_FIRE_PATH_RESULT`、`DISPATCH_TRAJECTORY_BUNDLE`。
- `mvs/common/platform_interfaces.py:53-64`：`reply_address` 从 `reply_host/reply_port`、`response_host/response_port`、`msg_ip/port` 等字段解析回传地址。

运行证据：
- `logs/*_events.jsonl` 中可以看到各节点业务事件。
- `result/message_capture/<node_type>/<node_id>/recv|send/*.json` 中可以看到每条收发消息的原始 payload。

## 1. 任务包下发到调度

输入消息：`TASK_PACKAGE`，由模型/节点发到调度端口。

代码对应：
- `mvs/scheduler/scheduler_app.py:3634-3656`：调度统一消息入口 `SchedulerApp.on_message`。所有消息先写入 message capture，然后根据 `env.msg_type` 分发。`TASK_PACKAGE` 分发给 `_on_task_package`。
- `mvs/scheduler/scheduler_app.py:3748-3772`：`_on_task_package` 首先归一化任务时间、执行任务包校验，校验失败时生成拒绝回执。
- `mvs/scheduler/scheduler_app.py:3773-3808`：校验成功后用 `TaskPackage.from_dict` 转成内部任务对象，并按 `launches` 生成 `SubTask`，写入 `self.subtasks` 和 `self.queued_subtasks`。
- `mvs/scheduler/scheduler_app.py:3813-3835`：记录 `task_received` 和 `task_package_processed`，随后请求外部点位。
- `mvs/scheduler/scheduler_app.py:3921-4020`：`_send_task_package_receipt` 生成并发送 `TASK_PACKAGE_RECEIPT`。

状态变化：
- `self.subtasks`：保存每个发射任务子任务。
- `self.queued_subtasks`：保存等待调度的子任务队列。
- `self.metrics["tasks_received"]`、`self.metrics["subtasks_created"]`：更新运行统计。

运行证据：
- `logs/scheduler_001_timing.jsonl:2` 示例：`task_package_processed`，`launches=48`，`subtasks_created=48`。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/recv/*TASK_PACKAGE.json` 和 `send/*TASK_PACKAGE_RECEIPT.json`。

## 2. 调度向模型请求点位

输出消息：`REQUEST_DIAN`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3825-3826`：任务包处理完成后立即调用 `_request_dian_context_from_model(task.task_id)`。
- `mvs/scheduler/scheduler_app.py:4022-4047`：组装 `{ "msg_type": "REQUEST_DIAN", "data": {} }`，通过 `_send_raw_json_callback` 发到 `model_callback_host:model_callback_port`，并记录 `dian_context_requested`。

运行证据：
- `logs/scheduler_001_timing.jsonl:1` 示例：`dian_context_requested`，目标为 `192.168.2.10:8888`。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/send/*REQUEST_DIAN.json`。

## 3. 模型返回发射点、隐蔽点、车辆点、贮备库点

输入消息：`FA_SHE_DIAN`、`YIN_BI_DIAN`、`VEHICLE_DIAN`、`ZHU_BEI_DIAN`/`DEPOT_DIAN`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3646-3647`：调度入口把上述点位消息统一分发到 `_on_dian_context`。
- `mvs/scheduler/scheduler_app.py:4048-4067`：`_on_dian_context` 解析 rows，按战区过滤，然后按消息类型写入 `external_fa_she_dian`、`external_yin_bi_dian`、`external_depot_dian`、`external_vehicle_dian`。
- `mvs/scheduler/scheduler_app.py:4056-4064`：发射点、隐蔽点、贮备库点通过 `_dian_rows_to_graph_nodes` 投影为路网节点，分别写入 `self.points.launch_points`、`self.points.hide_points`、`self.points.depots`。
- `mvs/scheduler/scheduler_app.py:4065-4067`：车辆点调用 `_apply_vehicle_dian_rows` 更新每辆车当前路网节点和位置。
- `mvs/scheduler/scheduler_app.py:4079-4088`：记录 `dian_context_received` 计时，其中 `apply_sec` 是点位应用/投影耗时。

状态变化：
- 原始点位：`external_fa_she_dian`、`external_yin_bi_dian`、`external_depot_dian`、`external_vehicle_dian`。
- 投影节点：`self.points.launch_points`、`self.points.hide_points`、`self.points.depots`。
- 车辆起点：每个 `VehicleRuntime.current_node`。

运行证据：
- `logs/scheduler_001_timing.jsonl:3-6` 示例：依次收到 96 个发射点、64 个车辆点、96 个隐蔽点、12 个贮备库点。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/recv/*FA_SHE_DIAN.json`、`*YIN_BI_DIAN.json`、`*VEHICLE_DIAN.json`、`*ZHU_BEI_DIAN.json`。

## 4. 调度生成车辆评分上下文并发给模型

输出消息：`TIME_BACKPLAN_CONTEXT`、`VEHICLE_CANDIDATE_CONTEXT`。

触发条件：
- `mvs/scheduler/scheduler_app.py:4092-4124`：`_maybe_send_vehicle_scoring_context_to_model` 检查任务存在、未发送过，并且发射点、隐蔽点、车辆点、贮备库点都已经收到且完成投影。缺少任何一类会记录 `vehicle_scoring_context_waiting_dian`。

代码对应：
- `mvs/scheduler/scheduler_app.py:4433-4481`：`_send_vehicle_scoring_context_to_model` 构建上下文并发给模型。
- `mvs/scheduler/scheduler_app.py:4439-4442`：`TIME_BACKPLAN_CONTEXT` 携带时间倒排规则。
- `mvs/scheduler/scheduler_app.py:4443-4452`：`VEHICLE_CANDIDATE_CONTEXT` 携带每辆车的候选发射点和绑定候选隐蔽点。
- `mvs/scheduler/scheduler_app.py:4455-4467`：两个上下文发往模型回调地址。
- `mvs/scheduler/scheduler_app.py:4474-4481`：记录 `vehicle_scoring_context_sent`，含 `build_sec` 和 `send_sec`。

运行证据：
- `logs/scheduler_001_timing.jsonl:7` 示例：`vehicle_scoring_context_sent`，`vehicle_count=64`。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/send/*TIME_BACKPLAN_CONTEXT.json` 和 `*VEHICLE_CANDIDATE_CONTEXT.json`。

## 5. 车辆接收点位、时间上下文和候选上下文

输入消息：车辆会收到 `FA_SHE_DIAN`、`YIN_BI_DIAN`、`ZHU_BEI_DIAN`、`TIME_BACKPLAN_CONTEXT`、`VEHICLE_CANDIDATE_CONTEXT`、`VEHICLE_CONTEXT`。

代码对应：
- `mvs/vehicle/vehicle_app.py:259-291`：车辆统一入口 `VehicleApp.on_message`，先写入 message capture，再按消息类型分发。
- `mvs/vehicle/vehicle_app.py:280-283`：发射点/隐蔽点/贮备库点进入车辆端点位处理。
- `mvs/vehicle/vehicle_app.py:284-287`：时间倒排和候选上下文分别进入 `_on_time_backplan_context`、`_on_vehicle_candidate_context`。
- `mvs/vehicle/vehicle_app.py:1208-1225`：`_on_time_backplan_context` 保存时间规则，若车辆状态已齐则尝试评分和候选路径规划。
- `mvs/vehicle/vehicle_app.py:1229-1263`：`_on_vehicle_candidate_context` 保存本车候选发射点，解析候选发射点和候选隐蔽点，必要时刷新 LaneGraph，然后尝试评分和候选路径规划。

状态变化：
- `external_time_backplan_context`：时间倒排规则。
- `external_candidate_context`：本车候选发射点上下文。
- `points.launch_points`、`points.hide_points`、`points.depots`：车辆本地可规划点。
- `external_vehicle_state`：模型/节点下发的本车当前状态和回传地址。

运行证据：
- 车辆日志示例：`logs/8546_events.jsonl` 中可看到 `dian_context_received`、`depot_dian_context_received`、`vehicle_candidate_context_received`。
- 抓包示例：`result/message_capture/vehicle/8423/recv/000004_TIME_BACKPLAN_CONTEXT.json`、`000005_VEHICLE_CANDIDATE_CONTEXT.json`。

## 6. 车辆计算评分并规划射前候选路径

输出消息：`VEHICLE_SCORE_RESULT`、`VEHICLE_CANDIDATE_PATH_RESULT`。

评分代码对应：
- `mvs/vehicle/vehicle_app.py:1331-1373`：`_submit_vehicle_context_score` 检查车辆位置、时间上下文、候选上下文是否齐全，提取回传地址和候选发射点。
- `mvs/vehicle/vehicle_app.py:1373-1382`：对候选发射点计算评分，取最佳 `score_total`。
- `mvs/vehicle/vehicle_app.py:1395-1430`：通过 `_send_raw_json(MSG_VEHICLE_SCORE_RESULT, ...)` 回传评分；失败会记录 `vehicle_context_score_send_failed`。

候选路径代码对应：
- `mvs/vehicle/vehicle_app.py:1998-2042`：`_submit_candidate_paths_to_model` 检查车辆位置、时间上下文、候选上下文和候选发射点。
- `mvs/vehicle/vehicle_app.py:2056-2068`：确定当前可用隐蔽点数量并记录 `submit_candidate_hide_points`。
- `mvs/vehicle/vehicle_app.py:2069-2075`：读取 `launch_prepare_time`、`fire_time`、`current_time`，准备路径规划。
- `mvs/vehicle/vehicle_app.py:2076-2142`：逐个候选发射点、逐个绑定隐蔽点调用 `_plan_candidate_path_option` 生成路径。
- `mvs/vehicle/vehicle_app.py:2151-2163`：组装 `VEHICLE_CANDIDATE_PATH_RESULT`，字段包括 `vehicle_id`、`port`、`task_id`、`paths`、`candidate_count`、`usable_path_count`、`rejected_path_count`。
- `mvs/vehicle/vehicle_app.py:2167-2193`：发送候选路径结果并记录 `candidate_path_planning_timing`。
- `mvs/vehicle/vehicle_app.py:349-360`：`_send_raw_json` 把业务 payload 包成 `{msg_type, data}` 后通过 TCP 发出，并写入 message capture。

运行证据：
- 抓包示例：`result/message_capture/vehicle/8423/send/000007_VEHICLE_SCORE_RESULT.json`、`000008_VEHICLE_CANDIDATE_PATH_RESULT.json`。
- 调度日志示例：`logs/scheduler_001_events.jsonl` 中 `vehicle_candidate_path_result_received` 说明调度已收到车辆候选路径。

## 7. 调度接收车辆候选路径结果

输入消息：`VEHICLE_CANDIDATE_PATH_RESULT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3648-3649`：调度入口把该消息分发给 `_on_vehicle_candidate_path_result`。
- `mvs/scheduler/scheduler_app.py:4399-4410`：解析 `vehicle_id` 和 `paths`，保存到 `self.external_vehicle_candidate_paths[vehicle_id]`。
- `mvs/scheduler/scheduler_app.py:4411-4428`：记录 `vehicle_candidate_path_result_received`，然后调用 `_try_resolve_external_dispatches` 尝试推进整批调度。

状态变化：
- `external_vehicle_candidate_paths`：保存所有已回传车辆的候选路径集合。

运行证据：
- `logs/scheduler_001_events.jsonl` 可看到多条 `vehicle_candidate_path_result_received`，字段含 `vehicle_id` 和 `path_count`。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/recv/*VEHICLE_CANDIDATE_PATH_RESULT.json`。

## 8. 调度接收模型选车结果

输入消息：`SELECTED_VEHICLE_RESULT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3642-3643`：调度入口把选车结果分发给 `_on_selected_vehicle_result`。
- `mvs/scheduler/scheduler_app.py:3409-3450`：选车结果解析为 `subtask_id -> vehicle_id`，写入 `self.external_vehicle_selection`，并触发 `_try_resolve_external_dispatches`。

状态变化：
- `external_vehicle_selection`：调度端最终采用的车辆选择结果。

运行证据：
- `logs/scheduler_001_events.jsonl` 可看到 `selected_vehicle_result_received`。
- 在候选路径未齐时，会反复出现 `external_dispatch_waiting_candidate_paths`；在选车未齐时，会出现 `external_dispatch_waiting_selected_vehicles`。

## 9. 调度执行射前全局分配和冲突消解

触发条件：
- `SELECTED_VEHICLE_RESULT` 已达到任务期望数量。
- 所有被选车辆都有 `VEHICLE_CANDIDATE_PATH_RESULT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3221-3228`：`_try_resolve_external_dispatches` 找到待求解 task，逐个调用 `_try_resolve_external_dispatch_for_task`。
- `mvs/scheduler/scheduler_app.py:2205-2254`：`_try_resolve_external_dispatch_for_task` 等待选车结果和车辆候选路径，未齐时记录等待日志。
- `mvs/scheduler/scheduler_app.py:2313-2331`：提取每个子任务可用候选路径。
- `mvs/scheduler/scheduler_app.py:2332-2344`：先做全局发射点分配，尽量保证一车一发射点。
- `mvs/scheduler/scheduler_app.py:2399-2474`：核心候选路径评估函数，检查发射点唯一性、隐蔽点等待冲突、路径时空冲突，并尝试延迟。
- `mvs/scheduler/scheduler_app.py:2481-2550`：先尝试全局分配的候选路径，再尝试重分配、交换修复、冲突连通分量修复。
- `mvs/scheduler/scheduler_app.py:2551-2609`：如果配置允许，使用冲突安全兜底路径。
- `mvs/scheduler/scheduler_app.py:2672-2727`：接受一条路径后，把发射点、隐蔽点、路径点写入轨迹行，并加入占用表。
- `mvs/scheduler/scheduler_app.py:2728-2748`：记录 `external_dispatch_path_selected`。

结果分支：
- 两阶段贮备库启用：进入第 10 步。
- 两阶段贮备库未启用：`mvs/scheduler/scheduler_app.py:2782-2862` 直接生成 `resolved_dispatch_trajectory_bundles` 并发送最终轨迹。

运行证据：
- `logs/scheduler_001_events.jsonl` 可看到 `external_dispatch_global_launch_assignment`、`external_dispatch_path_selected`。
- 两阶段启用时，`logs/scheduler_001_events.jsonl:625` 示例：`two_stage_dispatch_completed` 前会先出现 `external_prelaunch_dispatch_resolved`。

## 10. 调度把射前结果发给贮备库评分

输出消息：`DEPOT_VEHICLE_SCORE_CONTEXT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:2750-2779`：两阶段贮备库启用时，射前冲突审计通过后保存 `prelaunch_dispatch_states[task_id]`，状态设为 `prelaunch_resolved`，再调用 `_dispatch_depot_vehicle_score_contexts`。
- `mvs/scheduler/scheduler_app.py:2864-2885`：筛选有端口、有容量的贮备库；没有可寻址贮备库时走射前-only 兜底。
- `mvs/scheduler/scheduler_app.py:2886-2913`：把射前已消解出的车辆-发射点关系整理为 `assignments`，含 `vehicle_id`、`launch_node`、`fire_point_id`、`fire_time`。
- `mvs/scheduler/scheduler_app.py:2928-2945`：构造 `DEPOT_VEHICLE_SCORE_CONTEXT`，包含贮备库自身信息、容量、评分权重、所有已选车辆发射点关系。
- `mvs/scheduler/scheduler_app.py:2947-2955`：逐库发送评分上下文到 `depot_direct_host:depot_port`。

状态变化：
- `prelaunch_dispatch_states[task_id]`：保存射前轨迹、被选车辆、已接受路径、贮备库 rows、等待状态。

运行证据：
- `logs/scheduler_002_events.jsonl:524` 示例：`external_prelaunch_dispatch_resolved`，48 条射前轨迹完成。
- `logs/scheduler_002_events.jsonl:525-536` 示例：连续向 12 个贮备库发送 `depot_vehicle_score_context_sent`。

## 11. 贮备库接收评分上下文并返回分数数组

输入消息：`DEPOT_VEHICLE_SCORE_CONTEXT`。

输出消息：`DEPOT_VEHICLE_SCORE_RESULT`。

代码对应：
- `mvs/depot/depot_app.py:139-185`：贮备库统一入口 `DepotApp.on_message`，写入 message capture，并把 `DEPOT_VEHICLE_SCORE_CONTEXT` 分发给 `_on_vehicle_score_context`。
- `mvs/depot/depot_app.py:397-425`：校验这条消息是否属于本库，解析本库路网节点和距离计算方式。
- `mvs/depot/depot_app.py:426-467`：遍历所有已选车辆-发射点关系，计算发射点到本库距离；配置为 `road_network` 时走最短路，否则默认欧式距离。
- `mvs/depot/depot_app.py:468-478`：调用 `score_depot_vehicles` 计算本库对所有车辆的分数。
- `mvs/common/depot_assignment.py:6-36`：`score_depot_vehicles` 实际公式：`score_total = a * distance_m + b * distance_rank_score`。分数越小越优。
- `mvs/depot/depot_app.py:493-508`：组装 `DEPOT_VEHICLE_SCORE_RESULT`，携带 `capacity`、`scores`、不可达车辆和资源不匹配车辆列表。
- `mvs/depot/depot_app.py:508-519`：发送评分结果并记录 `depot_vehicle_score_result_sent`。

运行证据：
- 贮备库日志：`logs/8601_events.jsonl` 等文件中应出现 `depot_message_received`、`depot_vehicle_score_result_sent`。
- 贮备库抓包：`result/message_capture/depot/depot_8601/recv/*DEPOT_VEHICLE_SCORE_CONTEXT.json` 和 `send/*DEPOT_VEHICLE_SCORE_RESULT.json`。

## 12. 调度汇总贮备库分数矩阵并全局分配

输入消息：`DEPOT_VEHICLE_SCORE_RESULT`。

输出消息：`VEHICLE_DEPOT_ASSIGNMENT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3652-3653`：调度入口把贮备库评分结果分发给 `_on_depot_vehicle_score_result`。
- `mvs/scheduler/scheduler_app.py:2957-2977`：保存每个库的分数结果，等待所有期望贮备库都返回后进入全局分配。
- `mvs/scheduler/scheduler_app.py:2979-3004`：`_assign_depots_and_request_post_fire_paths` 收集车辆列表，调用 `greedy_capacity_assignment`。如果容量或分数矩阵不足，则记录 `depot_global_assignment_incomplete`。
- `mvs/common/depot_assignment.py:39-115`：`greedy_capacity_assignment` 构建“库-车”分数矩阵。每轮取当前全局最低分，把该车辆列删除，并扣减对应库容量；库容量为 0 后不再参与。
- `mvs/scheduler/scheduler_app.py:3010-3057`：把分配结果按车辆整理成 `VEHICLE_DEPOT_ASSIGNMENT`，发给对应车辆端口。
- `mvs/scheduler/scheduler_app.py:3058-3063`：记录 `depot_global_assignment_completed`。

状态变化：
- `depot_vehicle_score_results[task_id][depot_id]`：每个库返回的分数数组。
- `depot_vehicle_assignments[task_id][vehicle_id]`：最终每辆车分配到哪个贮备库。

运行证据：
- `logs/scheduler_001_events.jsonl` 中应出现 `depot_vehicle_score_result_received`、`depot_global_assignment_completed`、`vehicle_depot_assignment_sent`。
- 抓包位置：`result/message_capture/scheduler/scheduler_001/send/*VEHICLE_DEPOT_ASSIGNMENT.json`。

## 13. 车辆收到贮备库分配后规划射后路径

输入消息：`VEHICLE_DEPOT_ASSIGNMENT`。

输出消息：`VEHICLE_POST_FIRE_PATH_RESULT`。

代码对应：
- `mvs/vehicle/vehicle_app.py:288-289`：车辆入口把贮备库分配消息分发给 `_on_vehicle_depot_assignment`。
- `mvs/vehicle/vehicle_app.py:1938-1963`：校验车辆 ID、解析发射点节点、解析贮备库、解析回传地址，然后调用 `_plan_post_fire_depot_candidates`。
- `mvs/vehicle/vehicle_app.py:1812-1828`：`_plan_post_fire_depot_candidates` 只在收到正式分配的贮备库后执行；如果点位、贮备库、发射点、时间不齐则返回空。
- `mvs/vehicle/vehicle_app.py:1829-1834`：射后返回节点优先使用 `external_mission_origin_node`，否则使用车辆 home node，再否则使用 current node。
- `mvs/vehicle/vehicle_app.py:1854-1880`：分别规划“发射点到贮备库”和“贮备库回起点”两段路，并计算到库时间、补给结束时间和最终回到起点时间。
- `mvs/vehicle/vehicle_app.py:1881-1897`：生成射后轨迹点，包含到库、补给等待、返程。
- `mvs/vehicle/vehicle_app.py:1905-1926`：组装射后结果，字段含 `depot_id`、`depot_node`、`capacity`、`reload_duration_sec`、`finish_time`、`path_points`、`segments`。
- `mvs/vehicle/vehicle_app.py:1975-1986`：把射后路径结果补充 `task_id/subtask_id/vehicle_id/launch_node/fire_point_id` 后发送回调度。
- `mvs/vehicle/vehicle_app.py:1987-1995`：记录 `post_fire_path_result_sent`。

运行证据：
- 车辆抓包示例：`result/message_capture/vehicle/8423/recv/000009_VEHICLE_DEPOT_ASSIGNMENT.json`、`send/000010_VEHICLE_POST_FIRE_PATH_RESULT.json`。
- 车辆日志示例：`logs/8546_events.jsonl` 中 `post_fire_path_result_sent`，字段含 `depot_id`、`path_points`、`finish_time`、`addr`。

## 14. 调度接收射后路径并做最终拼接、射后冲突消解

输入消息：`VEHICLE_POST_FIRE_PATH_RESULT`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3654-3655`：调度入口把射后路径结果分发给 `_on_vehicle_post_fire_path_result`。
- `mvs/scheduler/scheduler_app.py:3065-3092`：保存每辆车射后路径，统计 `received_count/expected_count`；全部收到后调用 `_resolve_post_fire_paths_and_finalize`。
- `mvs/scheduler/scheduler_app.py:3094-3107`：先把射前轨迹写入占用表，保证射后消解不穿越射前车辆占用。
- `mvs/scheduler/scheduler_app.py:3108-3142`：逐车取射后路径后缀，尝试用等待延迟消解冲突；成功后把射前轨迹和射后后缀合并。
- `mvs/scheduler/scheduler_app.py:3143-3161`：把贮备库分配、射后延迟、到库时间、补给完成时间、最终完成时间写回最终轨迹行。
- `mvs/scheduler/scheduler_app.py:3162-3178`：如果有车辆射后无法消解或最终冲突审计失败，则记录错误并停止最终发送。
- `mvs/scheduler/scheduler_app.py:3179-3199`：全部通过后生成 `bundle`，记录 `two_stage_dispatch_completed`，并触发最终发送。

状态变化：
- `post_fire_path_results[task_id][vehicle_id]`：所有车辆回传的射后路径。
- `resolved_dispatch_trajectory_bundles[task_id]`：最终可发送的轨迹 bundle。

运行证据：
- `logs/scheduler_002_events.jsonl:598-645` 示例：48 条 `vehicle_post_fire_path_result_received` 收齐。
- `logs/scheduler_002_events.jsonl:646` 示例：`two_stage_dispatch_completed`，`trajectory_count=48`，`depot_assignment_count=48`。

## 15. 调度输出最终轨迹给模型并落盘

输出消息：`DISPATCH_TRAJECTORY_BUNDLE`。

代码对应：
- `mvs/scheduler/scheduler_app.py:3290-3297`：`_maybe_send_dispatch_trajectory_bundle` 检查最终 bundle 是否已生成，避免重复发送。
- `mvs/scheduler/scheduler_app.py:3237-3248`：`_send_dispatch_trajectory_bundle_to_model` 构建最终 bundle，并写入 `result/latest_dispatch_trajectory_bundle.json`。
- `mvs/scheduler/scheduler_app.py:3255-3272`：逐车拆成单条轨迹发送给模型，每条消息仍是 `DISPATCH_TRAJECTORY_BUNDLE`，`trajectories` 中只放一辆车。
- `mvs/scheduler/scheduler_app.py:3273-3288`：发送失败记录 `dispatch_trajectory_bundle_send_failed`，完成后记录 `dispatch_trajectory_bundle_to_model` 计时。

最终文件：
- `result/latest_dispatch_trajectory_bundle.json`：本地最新完整最终轨迹包。
- `result/message_capture/scheduler/scheduler_001/send/*DISPATCH_TRAJECTORY_BUNDLE.json`：发送给模型的逐车轨迹抓包。

运行证据：
- `logs/scheduler_001_events.jsonl:626-673` 示例：一区 48 条逐车 `dispatch_trajectory_bundle_sent`。
- `logs/scheduler_002_events.jsonl:647` 起：二区 48 条逐车 `dispatch_trajectory_bundle_sent`。
- `result/server_timing_summary.txt:11` 示例：当前拷贝结果中最终轨迹发送共 96 条，发送跨度约 6.063 秒。

## 16. message_capture 如何生成

调度：
- `mvs/scheduler/scheduler_app.py:3634-3635`：调度每次收到消息先调用 `_capture_message("recv", ...)`。
- `mvs/scheduler/scheduler_app.py:4561-4590`：调度端 `_capture_message` 将收发内容写成 JSON 文件。

车辆：
- `mvs/vehicle/vehicle_app.py:259-263`：车辆每次收到消息先写 capture。
- `mvs/vehicle/vehicle_app.py:349-355`：车辆每次 `_send_raw_json` 前写 send capture。

贮备库：
- `mvs/depot/depot_app.py:139-150`：贮备库每次收到消息先写 capture。
- `mvs/depot/depot_app.py:208-242`：贮备库 `_capture_message` 生成 `result/message_capture/depot/<node_id>/recv|send/*.json`。

用途：
- `logs/*.jsonl` 适合看事件和耗时。
- `result/message_capture/**` 适合看真实收发 payload、字段是否完整、地址是否正确。

## 17. 当前链路关键事件顺序

一次完整成功链路应该能在日志中看到如下顺序：

1. 调度收到 `TASK_PACKAGE`，生成子任务。
2. 调度发送 `REQUEST_DIAN`。
3. 调度收到 `FA_SHE_DIAN`、`VEHICLE_DIAN`、`YIN_BI_DIAN`、`ZHU_BEI_DIAN`。
4. 调度发送 `TIME_BACKPLAN_CONTEXT` 和 `VEHICLE_CANDIDATE_CONTEXT`。
5. 车辆收到上下文，发送 `VEHICLE_SCORE_RESULT` 和 `VEHICLE_CANDIDATE_PATH_RESULT`。
6. 调度收到模型 `SELECTED_VEHICLE_RESULT`。
7. 调度等待被选车辆候选路径齐全。
8. 调度完成射前全局发射点分配和冲突消解，记录 `external_prelaunch_dispatch_resolved`。
9. 调度发送 `DEPOT_VEHICLE_SCORE_CONTEXT` 给每个贮备库。
10. 贮备库返回 `DEPOT_VEHICLE_SCORE_RESULT`。
11. 调度完成贮备库全局容量分配，发送 `VEHICLE_DEPOT_ASSIGNMENT` 给车辆。
12. 车辆规划射后路径并返回 `VEHICLE_POST_FIRE_PATH_RESULT`。
13. 调度收齐射后路径，完成射后冲突消解和拼接，记录 `two_stage_dispatch_completed`。
14. 调度逐车发送 `DISPATCH_TRAJECTORY_BUNDLE` 给模型，并写入 `result/latest_dispatch_trajectory_bundle.json`。

## 18. 出问题时优先看哪里

任务包没进入：
- 看 `result/message_capture/scheduler/*/recv/*TASK_PACKAGE.json` 是否存在。
- 看 `logs/scheduler_*_events.jsonl` 是否有 `task_received` 或 `task_validation_error`。

点位阶段卡住：
- 看 `logs/scheduler_*_timing.jsonl` 中 `dian_context_received` 四类点位数量是否齐。
- 看 `vehicle_scoring_context_waiting_dian` 缺哪一类。

车辆路径没回：
- 看车辆 `logs/<vehicle>_events.jsonl` 是否有 `candidate_path_planning_timing`。
- 看车辆 capture send 是否有 `VEHICLE_CANDIDATE_PATH_RESULT`。
- 看调度 capture recv 是否收到对应车辆的 `VEHICLE_CANDIDATE_PATH_RESULT`。

贮备库链路没走：
- 看调度是否有 `external_prelaunch_dispatch_resolved`。
- 看调度是否有 `depot_vehicle_score_context_sent`。
- 看贮备库是否有 `depot_message_received` 和 `depot_vehicle_score_result_sent`。
- 看调度是否有 `depot_vehicle_score_result_received` 和 `depot_global_assignment_completed`。

最终轨迹没发：
- 看是否有 `two_stage_dispatch_completed` 或 `external_dispatch_resolved`。
- 看是否有 `dispatch_trajectory_bundle_sent`。
- 看 `result/latest_dispatch_trajectory_bundle.json` 是否生成。


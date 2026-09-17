# 任务书逐段代码对应讲解稿

生成时间：2026-08-12

说明：本文按《任务书对照.docx》中有实际工程含义的段落逐段核对当前代码。每一节都包含：任务书要求、代码位置、函数逻辑、公式/流程落地情况、当前偏差或未实现点。文件和行号按当前工作区 `/home/shanshangzhusun/multi_vehicle_system` 的代码快照记录。

## 一、总体结论

- 当前代码已经实现了调度、车辆、贮备库、模型/节点通讯的主体链路：点位接收与投影、时间倒排、车辆候选路径生成、候选路径回传、模型选车结果接收、射前冲突消解、两阶段贮备库评分与容量分配、射后去库并返回起点路径规划、最终轨迹打包发送。
- 任务书公式已经集中落在 `mvs/common/formula_utils.py`，并在 `mvs/common/scoring.py`、`mvs/scheduler/scheduler_app.py`、`mvs/vehicle/vehicle_app.py`、`mvs/depot/depot_app.py`、`mvs/common/depot_assignment.py` 中有实际调用或解释性输出。
- 需要如实说明的偏差：任务书写的是 UDP 通讯，但当前默认主链路是 TCP；任务书写 ETA 批量推理和 ONNX/OM/NPU 部署，当前是纯 Python JSON MLP 单段推理和无模型降级；任务书写独立步进执行软件，当前主要由脚本、日志、可视化和消息回放替代；任务书写 12 个指挥平台，当前代码可多调度器配置，但默认服务器测试主要是两个区域调度。

## 二、4.1 软件总体架构

### 任务书段落 0004：系统由区域层级式多车调度、发射平台行动时序规划、贮备库模拟、步进执行和网络传输组成

任务书要求：全流程拆成多个软件/模块，调度软件负责任务分配和多车冲突，车辆软件负责单车时序规划，贮备库软件负责补给资源评价与过程，步进执行软件驱动仿真并维护状态，各软件通过网络消息交换。

代码对应：
- `mvs/scheduler/scheduler_app.py:110-380`：`SchedulerApp.__init__` 加载调度配置、地图、车辆、通讯、消息捕获和冲突/贮备库参数，是区域调度软件主对象。
- `mvs/vehicle/vehicle_app.py:55-217`：`VehicleApp.__init__` 加载单车配置、路网、点位、时间预测器、通讯和状态，是发射平台行动时序规划软件主对象。
- `mvs/depot/depot_app.py:119-185`：`DepotApp.start/on_message` 启动贮备库监听，并按消息类型分发到评分、点位、两阶段评分等处理函数。
- `mvs/common/platform_interfaces.py:6-50`：统一定义跨软件消息类型，如 `TIME_BACKPLAN_CONTEXT`、`VEHICLE_CANDIDATE_CONTEXT`、`VEHICLE_CANDIDATE_PATH_RESULT`、`DEPOT_VEHICLE_SCORE_CONTEXT`、`VEHICLE_DEPOT_ASSIGNMENT`、`DISPATCH_TRAJECTORY_BUNDLE`。
- `mvs/common/transport.py:21-45`：通讯配置默认值，当前默认 `transport.type` 是 `tcp`。
- `scripts/run_three_platform_flow.sh:21-40`：统一启动入口，包含 `prepare`、`scheduler`、`vehicles`、`depot`、`model`、`reset`、`down` 等命令。

函数逻辑讲解：调度、车辆、贮备库都通过 `TcpNode/UdpNode` 收发 `Envelope`；收到消息后进入各自 `on_message` 分发；发送消息时通过 `_send_raw_json_callback` 或 `_send_raw_json` 包装为 `{"msg_type": ..., "data": ...}`。`message_capture` 在各端记录收发 JSON，供服务器真实链路复盘。

落实情况：已实现主体架构。偏差是“步进执行软件”不是独立常驻业务进程，当前由启动脚本、模拟模型/节点、日志和可视化工具承担一部分作用。

### 任务书段落 0005：12 指挥平台、128 车、12 贮备库，10 秒内完成；路径搜索按车辆并行，ETA 批量推理，冲突消解用时间窗+容量预约表增量调整

任务书要求：系统规模为多平台、多车辆、多库；候选路径和时序计算要高效；冲突消解不能全量反复重算，要基于时间窗和容量预约表做增量处理。

代码对应：
- `scripts/run_three_platform_flow.sh:500-539`：`prepare` 生成运行配置、车辆配置和贮备库配置。
- `scripts/run_three_platform_flow.sh:541-587`：启动调度和贮备库。
- `scripts/run_three_platform_flow.sh:640-803`：启动车辆并按部署配置修正车辆回传地址和 gateway。
- `mvs/scheduler/scheduler_app.py:2205-2359`：`_try_resolve_external_dispatch_for_task` 等选车结果、等候选路径、做全局发射点分配、初始化预约表。
- `mvs/scheduler/scheduler_app.py:2399-2474`：内部 `evaluate_candidate_paths` 对每条候选路径做隐蔽点容量冲突和路径时空冲突检查，并用延迟尝试修复。
- `mvs/scheduler/scheduler_app.py:2672-2682`：接受一条轨迹后立即写入 `reserved_slots` 和 `hide_wait_reservations`，后续车辆只和已有占用比较，属于增量预约表。
- `mvs/scheduler/scheduler_app.py:1489-1579`：`_audit_external_dispatch_entries` 最终审计发射点唯一性、隐蔽点容量、路径时空冲突、贮备库容量。
- `mvs/scheduler/scheduler_app.py:2864-3063`：两阶段贮备库分配，调度把射前已消解结果发给每个库，收齐库分数后做全局容量分配。

函数逻辑讲解：`_try_resolve_external_dispatch_for_task` 不是一次性把所有路径全排列暴力搜索，而是先做全局发射点唯一匹配，再按车辆逐条验冲突。每接受一辆车，就把轨迹点按时间离散写入预约表。后面的车只检查与当前预约表是否冲突，冲突时优先压缩等待或延迟出发，失败才换候选/兜底。

落实情况：时间窗+容量预约表已经实现；按车辆并行主要体现在车辆软件是多进程并行启动，单车各自规划；ETA 批量推理没有实现，当前是每条边逐次调用预测器；10 秒是目标能力，不是代码里的硬保证，需要用服务器日志证明。

### 任务书段落 0006：双机/多机部署，主控机和辅助计算机交换小规模时间占用与确认信息

任务书要求：支持分布式部署，主控调度与其它电脑上的车辆/节点/模型交换精简消息。

代码对应：
- `scripts/run_three_platform_flow.sh:64-68`：说明 server/LAN split。
- `scripts/run_three_platform_flow.sh:103-126`：`load_deployment_advertise_host` 从部署配置读取对外 IP。
- `scripts/run_three_platform_flow.sh:640-803`：`vehicles-server` 根据部署配置给车辆写入模型/节点回传地址和 gateway 地址。
- `mvs/common/platform_interfaces.py:53-64`：`reply_address` 从 `reply_host/reply_port/msg_ip/host/port` 中解析回传地址。
- `mvs/depot/depot_app.py:856-866`：`ZHU_BEI_CONTEXT` 特殊回传逻辑，使用 `msg_ip` 作为回传 host，`depot_id` 作为回传端口。

函数逻辑讲解：服务器运行时，配置文件决定本进程监听地址、对外宣告地址、模型地址、车辆 gateway 地址、贮备库 gateway 地址。车辆和贮备库不需要知道所有其它进程，只需要根据收到消息里的回传地址和本地配置发送结果。

落实情况：已实现可配置 LAN 部署。需要注意真实服务器上车辆本机 IP、模型回传 IP、节点 IP 要分别按部署文件配置，不能混为一个地址。

### 任务书段落 0012：仿真平台先给步进软件配置，步进软件启动其它软件，最终状态返回步进软件

任务书要求：有一个步进执行软件作为总控，负责启动其它软件、接收仿真状态、维护状态流。

代码对应：
- `scripts/run_three_platform_flow.sh:21-40`：当前由脚本统一准备、启动和停止各软件。
- `scripts/run_three_platform_flow.sh:500-539`：`prepare` 清理输出、生成地图和运行配置。
- `mvs/scheduler/dashboard_server.py`：提供调度状态/可视化服务。
- `scripts/build_trajectory_player.py:226`、`scripts/package_client_route_player.py:514`：根据最终轨迹生成可播放结果。

函数逻辑讲解：当前没有一个名为“step execution”的独立 Python 服务。脚本承担进程编排，调度/车辆/贮备库日志承担状态记录，可视化播放器承担轨迹回放。

落实情况：部分实现。若甲方严格要求独立“步进执行软件”，需要把脚本编排、状态汇聚、路径回放和仿真状态更新封装成一个常驻进程。

## 三、4.2 区域层级式多车调度平台

### 任务书段落 0014：调度平台包括信息读取、通讯、层级指挥业务、仿真接口、可视化交互

代码对应：
- 信息读取：`mvs/scheduler/scheduler_app.py:110-380` 初始化配置、地图、车辆、任务和外部点位存储。
- 通讯：`mvs/scheduler/scheduler_app.py:3634` 附近 `on_message` 分发外部消息；`mvs/scheduler/scheduler_app.py:4510-4528` 发送原始 JSON。
- 层级业务：`mvs/scheduler/scheduler_app.py:2205-2815` 外部选车、候选路径和射前冲突消解主流程；`mvs/scheduler/scheduler_app.py:2864-3135` 两阶段贮备库和射后路径流程。
- 仿真/可视化：`mvs/scheduler/dashboard_server.py`、`scripts/build_trajectory_player.py`、`scripts/package_client_route_player.py`。

落实情况：调度平台主功能已实现。可视化和仿真接口偏工程工具化，不是任务书中独立 UI 软件形态。

### 任务书段落 0018：读取道路、发射点、隐蔽点、贮备库位置和容量、时间窗、车辆参数

代码对应：
- `mvs/scheduler/map_model.py:401-422`：`MapLoader.load_graph_from_config` 根据配置加载 SHP/OpenDRIVE/JSON 路网。
- `mvs/scheduler/map_model.py:437-477`：`MapLoader.load_graph` 从已生成 `road_graph_shp_demo.json` 载入节点和边。
- `mvs/scheduler/map_model.py:503-509`：`MapLoader.load_points` 读取特殊点位。
- `mvs/scheduler/map_model.py:177-237`：`RoadGraph.add_point_on_edge` 把车辆/发射点/隐蔽点/贮备库投影并插入路网边。
- `mvs/scheduler/scheduler_app.py:127`：默认贮备库容量 `depot_capacity=16`。
- `mvs/scheduler/scheduler_app.py:323-356`：读取冲突消解、选库等待、两阶段贮备库等配置。
- `scripts/apply_deployment_config.py:491-510`：从部署文件把贮备库容量和两阶段配置写入调度配置。

函数逻辑讲解：调度先加载基础路网，再接收外部 `FA_SHE_DIAN/YIN_BI_DIAN/VEHICLE_DIAN/ZHU_BEI_DIAN` 点位。点位不是简单保存经纬度，而是会投影到最近道路边，生成可用于路径搜索的节点引用。后续车辆上下文尽量携带已投影 `node_id`，避免车辆端重复投影。

落实情况：道路、点位、容量、车辆参数、时间约束都已进入流程。卫星过顶 CSV 的专门读取和约束未形成独立主流程文件，当前主要通过倒排时间窗和隐蔽等待逻辑表达。

### 任务书段落 0087、0130-0133：调度通讯模块，与发射平台、贮备库、仿真/步进软件通信；UDP 通讯库

代码对应：
- `mvs/common/platform_interfaces.py:6-50`：消息类型定义。
- `mvs/common/platform_interfaces.py:53-72`：回传地址和关联字段。
- `mvs/common/transport.py:21-45`：TCP/UDP 默认配置。
- `mvs/common/transport.py:223-251`：`UdpNode`。
- `mvs/common/transport.py:254-360`：`TcpNode`。
- `mvs/scheduler/scheduler_app.py:4561-4590`：调度 message_capture 写入收发文件。

函数逻辑讲解：`transport` 把网络字节解析成 `Envelope`，业务层只根据 `env.msg_type` 分发。调度发送到模型、车辆、贮备库时都走统一 JSON 包装，方便服务器上通过 `message_capture` 对照。

落实情况：通讯库支持 UDP，但当前主链路默认 TCP。讲解时应说“代码支持 UDP 节点，但现阶段服务器联调默认 TCP，为了大 JSON 和连接可靠性更稳”。

### 任务书段落 0136-0137：层级指挥、多波次、多类型、跨区域和冗余车辆

代码对应：
- `mvs/scheduler/scheduler_app.py:82-108`：`SubTask` 保存子任务、区域、发射时间、弹种、是否冗余。
- `mvs/scheduler/scheduler_app.py:235-238`：读取冗余配置，默认可关闭。
- `mvs/scheduler/scheduler_app.py:3778-3803`：根据 `redundancy_ratio` 复制冗余子任务，并记录其对应原任务。
- `mvs/scheduler/scheduler_app.py:3552-3558`：部分调度统计和任务推进时区分真实任务与冗余任务。
- `mvs/scheduler/scheduler_app.py:5705-5746`：运行指标里输出冗余任务数量和状态。

函数逻辑讲解：`SubTask` 是调度内部的任务粒度。冗余任务不是另外一种车，而是按比例复制额外发射点/子任务，标记 `is_redundant` 和 `redundant_for_subtask_id`，供调度和指标区分。

落实情况：冗余车辆/任务逻辑存在，但默认配置经常关闭；多区域通过多个 scheduler 和 theater 配置实现，不是完整 12 级指挥树。

### 任务书段落 0185-0195：时间倒排、发射点/贮备库多目标评分、冗余车辆

代码对应：
- `mvs/scheduler/scheduler_app.py:4606-4652`：`_build_time_backplan_rules` 由发射时间倒推出到达发射点、到达隐蔽点、发射准备和冷待机时间。
- `mvs/scheduler/scheduler_app.py:4430-4481`：`_send_vehicle_scoring_context_to_model` 向模型发送 `TIME_BACKPLAN_CONTEXT` 和 `VEHICLE_CANDIDATE_CONTEXT`。
- `mvs/common/scoring.py:205-287`：`score_launch_point_for_vehicle` 计算发射点候选评分和公式(1)解释字段。
- `mvs/common/scoring.py:421-476`：`score_depot_task_execution` 输出任务书贮备库评分解释字段。
- `mvs/common/depot_assignment.py:6-36`、`mvs/common/depot_assignment.py:39-115`：当前真实两阶段贮备库矩阵评分与容量分配。

函数逻辑讲解：时间倒排产生的是车辆规划的约束，发给模型后模型再触发车辆规划。发射点评分当前用于候选上下文解释和排序字段；贮备库真实分配采用“库端打分、调度汇总矩阵”的两阶段流程。

落实情况：已实现。需要解释清楚“任务书公式评分”和“当前两阶段贮备库分配评分”是两个口径，后者是用户后续需求加入的主流程。

### 任务书段落 0196-0215：发射车-发射点多车调度、路径时空冲突、发射点容量、隐蔽点容量、车辆优先级、换点/换隐蔽点冲突消解

代码对应：
- `mvs/scheduler/scheduler_app.py:2205-2254`：等待模型选车和车辆候选路径。
- `mvs/scheduler/scheduler_app.py:2332-2344`：先做全局发射点唯一分配。
- `mvs/scheduler/scheduler_app.py:2399-2474`：对候选路径进行时间窗、隐蔽点容量、路径空间时间冲突检查。
- `mvs/scheduler/scheduler_app.py:2485-2550`：当首选发射点冲突时，尝试重新选择候选发射点或做局部修复。
- `mvs/scheduler/scheduler_app.py:2672-2682`：把已接受路径登记到发射点、路径和隐蔽等待预约表。
- `mvs/scheduler/scheduler_app.py:2750-2779`：两阶段启用时，射前路径先单独审计并固化，再进入贮备库流程。
- `mvs/scheduler/scheduler_app.py:1489-1579`：最终冲突审计。
- `mvs/common/formula_utils.py:34-91`：公式(2)-(6) 时间窗、容量差、车辆优先级、库槽位容量差。
- `mvs/common/scoring.py:158-202`：`compute_dispatch_priority` 计算车辆优先级解释字段。

函数逻辑讲解：调度不是盲目使用模型选车结果，而是在收到车辆候选路径后重新检查每辆车实际路径。核心约束包括：发射点唯一、同隐蔽点同时等待不超过容量、路径轨迹点在同一时间槽/空间距离内不冲突。若冲突，先延迟并压缩等待，再换候选发射点，再做局部修复，最后才判定未解决。

落实情况：已实现。发射点容量当前主要按“唯一发射点”处理，即容量等价于 1；公式(3)存在，但主流程更直接地用 `used_launch_points` 和全局匹配保证唯一性。

### 任务书段落 0216-0231：发射后车辆-贮备库调度、库容量/资源/通道/路径冲突、实时状态监测和重规划

代码对应：
- `mvs/scheduler/scheduler_app.py:2864-2955`：调度把射前已选车辆-发射点结果发给每个贮备库。
- `mvs/depot/depot_app.py:397-484`：每个库收到 `DEPOT_VEHICLE_SCORE_CONTEXT` 后，对所有已选车辆计算本库分数。
- `mvs/common/depot_assignment.py:6-36`：单库评分 `score = a*distance + b*distance_rank`。
- `mvs/common/depot_assignment.py:39-115`：调度汇总所有库分数矩阵，按容量贪心分配。
- `mvs/scheduler/scheduler_app.py:2979-3063`：调度把最终库分配发给车辆。
- `mvs/vehicle/vehicle_app.py:1812-1936`：车辆收到正式库后，规划 `发射点 -> 贮备库 -> 起点/任务起点`。
- `mvs/vehicle/vehicle_app.py:1938-1995`：车辆把射后路径回调调度。
- `mvs/scheduler/scheduler_app.py:3094-3135`：调度把射后路径接到射前路径后，并只用等待延迟做低扰动冲突消解。
- `mvs/scheduler/scheduler_app.py:718-749`：同库排队等待时间按容量计算。

函数逻辑讲解：当前实现已经把贮备库选择从模型中独立出来。模型只参与选车，射前冲突消解完成后，调度向每个库下发“已选车辆及其发射点”。库只返回分数，不直接决定哪个车归哪个库。调度把所有库返回的行拼成矩阵，每轮取最小分、删除车辆列、库容量减一。分配完成后车辆才规划射后段。

落实情况：新两阶段流程已实现。资源类型匹配是可选硬过滤：只有库或消息带 `supported_ammo_types` 时才生效；否则不会改变现有结果。连续状态监测/动态重规划只是轻量日志和状态口径，没有完整闭环仿真重规划进程。

### 任务书段落 0232-0275：调度仿真接口、地图展示、动态状态、回放和报告

代码对应：
- `mvs/scheduler/dashboard_server.py`：调度可视化/状态服务。
- `scripts/build_trajectory_player.py:226`：根据最终轨迹生成 HTML 播放器。
- `scripts/package_client_route_player.py:514`：打包给外部使用的播放器和数据。
- `mvs/scheduler/scheduler_app.py:4561-4590`：收发消息捕获。

函数逻辑讲解：调度运行时会记录事件日志和 message_capture；任务结束后，播放器脚本读取最终轨迹 bundle、路网、特殊点，生成可拖动时间轴的动态展示。

落实情况：可视化和回放已实现；完整报告生成只做到导出 JSON/播放器，未实现任务书所述完整统计报告 UI。

## 四、4.3 发射平台行动时序规划软件

### 任务书段落 0277-0279：车辆软件包含信息读取、通讯、行动规划、目标更新、仿真接口，并支持训练/更新接口

代码对应：
- `mvs/vehicle/vehicle_app.py:55-217`：车辆初始化，加载配置、路网、特殊点、MLP 时间预测器、通讯和 message_capture。
- `mvs/vehicle/vehicle_app.py:259` 附近：`on_message` 根据消息类型调用车辆评分、点位、倒排、候选上下文、最终轨迹、贮备库分配等处理函数。
- `mvs/vehicle/time_predictor.py:9-160`：纯 Python MLP 推理器。

函数逻辑讲解：车辆端是单车自治规划器。它不会一启动就规划，而是等车辆位置、发射/隐蔽/贮备点位、时间倒排和候选上下文齐备后，才生成候选路径并回传。

落实情况：信息读取、通讯、行动规划已实现；目标更新和仿真接口是轻量实现；ONNX/OM 训练更新接口没有完整工程化实现。

### 任务书段落 0281-0335：车辆信息读取和通讯

代码对应：
- `mvs/vehicle/vehicle_app.py:465-555`：接收发射点/隐蔽点，优先复用调度端已投影节点，缺少投影引用时延迟投影，避免每辆车重复重投影。
- `mvs/vehicle/vehicle_app.py:557-610`：接收贮备库点位。
- `mvs/vehicle/vehicle_app.py:1208-1225`：接收时间倒排上下文。
- `mvs/vehicle/vehicle_app.py:1229-1263`：接收本车候选发射点上下文。
- `mvs/vehicle/vehicle_app.py:1265-1295`：接收最终轨迹 bundle 并筛选本车计划。

函数逻辑讲解：车辆端点位处理的关键不是“每辆车独立找最近路网边”，而是尽量让调度端统一投影后把节点引用带下来。这样既保证车辆信息独立接收，又避免 128 辆车重复扫描细路网造成百秒级延迟。

落实情况：已实现。若外部只发原始经纬度、不带调度投影节点，车辆会走降级路径并可能变慢。

### 任务书段落 0371-0416：车辆行动规划业务，ETA、多波次单车时序规划、任务执行评分

代码对应：
- `mvs/vehicle/vehicle_app.py:2538-2585`：`_travel_time_seconds` 和 `_edge_seconds_from_path` 逐边计算 ETA。
- `mvs/vehicle/vehicle_app.py:2607-2655`：`_propose_path` 单条路径规划入口，选择直达、隐蔽点等待或起点等待。
- `mvs/common/scoring.py:290-418`：`score_vehicle_task_execution` 发射平台任务执行评分主入口。
- `mvs/vehicle/vehicle_app.py:397-463`：收到车辆评分请求时调用 `score_vehicle_task_execution` 并回传最佳分数。

函数逻辑讲解：车辆路径规划先求直达发射点的路线，然后结合发射时间、准备时间、冷热待机时间，判断是否需要隐蔽点等待或起点等待。每条候选路径会输出 `timing_strategy`、`hide_selected`、`wait_seconds`、`fire_time_error_sec`、`score_total` 和轨迹点。

落实情况：已实现。注意当前 `_travel_time_seconds` 调用的是 `predict_with_conntext`，而 `MLPTimePredictor` 定义的是 `predict_with_context`，这处命名如果没有兼容别名会导致运行错误；如果当前服务器能跑，说明本地代码可能已有缓存/热修或该分支未走到，需要单独确认。

### 任务书段落 0417-0439：神经网络 ETA、栅格化、输入特征、两类车辆模型、隐藏层 400/300、逐格累计时间

代码对应：
- `mvs/common/formula_utils.py:101-185`：`grid_features_from_polyline` 把路径折线按固定距离切成等距栅格特征。
- `mvs/common/formula_utils.py:188-243`：曲率、坡度、栅格耗时、逐格 ETA 汇总公式。
- `mvs/vehicle/time_predictor.py:36-65`：`predict_with_context` 有模型时执行 MLP 前向推理，无模型时降级为 `distance/speed*fallback_bias`。
- `mvs/vehicle/time_predictor.py:87-148`：按模型 `feature_names` 构造特征并裁剪出口速度。
- `mvs/vehicle/time_predictor.py:150-163`：纯 Python MLP 前向传播。
- `mvs/vehicle/vehicle_app.py:2538-2585`：车辆主流程 ETA 调用点。

函数逻辑讲解：任务书要求的“栅格化预测”在公式工具函数里具备完整形式：折线按距离切段，提取长度、曲率、坡度、入口速度等特征，模型预测出口速度，再通过 `2*length/(vin+vout)` 累加时间。当前车辆主流程为了稳定和离线可运行，是按路网边逐段调用 MLPTimePredictor；如果模型 JSON 不存在，就自动降级为统一最大速度下的时间估计。

落实情况：部分实现。真实 MLP 前向实现存在，但没有 ONNX/OM/CANN/NPU；两类车辆模型和 400/300 隐层结构依赖外部 JSON 模型文件内容，不是代码硬编码；ETA 批量推理没有实现。

### 任务书段落 0441-0467：拓扑搜索、OpenDRIVE 路口/道路、Dijkstra、A*/Hybrid A*、隐蔽点时序搜索

代码对应：
- `mvs/scheduler/map_model.py:317-389`：`RoadGraph.shortest_path/fastest_path` 用路网图求最短/最快路径。
- `mvs/common/lane_graph.py:36-93`：`LaneGraph` 构造车道级图。
- `mvs/common/lane_graph.py:93-180`：`route_between_nodes` 执行车道图 Dijkstra，并用公式(12)合成距离/曲率/转向代价。
- `mvs/vehicle/path_constraints.py:89-96`、`mvs/vehicle/path_constraints.py:220-227`：运动学约束搜索中使用公式(12)作为边代价。
- `mvs/vehicle/hybrid_astar.py:128-147`：Hybrid A* 用动作集合生成邻居。
- `mvs/vehicle/hybrid_astar.py:187-194`：Hybrid A* 单步代价调用公式(12)。
- `mvs/vehicle/vehicle_app.py:2607-2655`：车辆路径规划入口，后续会枚举隐蔽点候选并生成等待策略。
- `mvs/common/formula_utils.py:279-296`：公式(14)(15) 隐蔽点搜索范围函数。

函数逻辑讲解：主流程主要使用路网图搜索；车道图和 Hybrid A* 是更细粒度的走廊/运动学工具。隐蔽点时序规划不是单独一个公式函数完成，而是在 `_propose_path` 中把直达、隐蔽等待和起点等待比较后输出候选。

落实情况：拓扑搜索和隐蔽点时序规划已实现；OpenDRIVE 路口语义不是主流程唯一数据源，当前更多依赖 SHP/JSON 路网。

### 任务书段落 0468-0495：发射平台任务执行评分，时间、安全、冗余、Sigmoid、总评分

代码对应：
- `mvs/common/formula_utils.py:299-384`：公式(16)-(21) 的纯函数实现。
- `mvs/common/scoring.py:290-418`：`score_vehicle_task_execution` 是主入口，实际调用公式(16)-(21)。
- `mvs/vehicle/vehicle_app.py:397-463`：车辆收到评分请求时执行该评分并回传。

函数逻辑讲解：车辆评分先用 `vehicle_score` 得到路网距离、ETA 和时间窗可行性，再计算时间评分、安全评分、冗余评分，经过 Sigmoid 归一化后按权重合成最终 `score_total`。如果缺少任务时间窗，则退回旧工程分，避免接口缺字段时整车不回分。

落实情况：已实现并进入车辆评分主流程。需要说明公式(16)(19) 默认使用“时间越短越优”的工程修正版，不是盲目使用文档里可能导致越慢越高的原式方向。

### 任务书段落 0496-0498：发射动作控制、冷热待机、发射准备时间

代码对应：
- `mvs/vehicle/vehicle_app.py:82-111`：车辆读取速度、冷热待机阈值、发射准备时间等参数。
- `mvs/vehicle/vehicle_app.py:2506-2536`：`_standby_mode_for_path` 根据等待点到发射点距离判断 hot/cold。
- `mvs/scheduler/scheduler_app.py:4606-4652`：调度倒排发射准备和冷待机时间窗口。

函数逻辑讲解：调度先规定最晚到达发射点和隐蔽点时间；车辆规划时根据等待点到发射点距离判断冷热待机，并在候选路径中写入 `launch_startup_mode`、`wait_seconds` 等字段。

落实情况：已实现时间逻辑；真实发射动作控制模板和手动/自动 UI 没有完整实现。

### 任务书段落 0499-0519：目标更新模块

代码对应：
- `mvs/vehicle/vehicle_app.py:1265-1295`：车辆接收最终调度轨迹后更新本车计划。
- `mvs/vehicle/vehicle_app.py:1938-1995`：车辆收到库分配后更新射后目标并规划。

函数逻辑讲解：当前目标更新主要通过消息驱动：调度最终 bundle 或贮备库分配消息到达后，车辆保存/规划新的目标轨迹。

落实情况：部分实现。没有独立“目标更新训练接口”或复杂目标跟踪模块。

## 五、4.4 贮备库模拟软件

### 任务书段落 0567-0731：贮备库信息读取、通讯、任务评价、调度执行、仿真和可视化

代码对应：
- `mvs/depot/depot_app.py:119-185`：贮备库启动和消息分发。
- `mvs/depot/depot_app.py:208-230`：贮备库 message_capture 写收发文件。
- `mvs/depot/depot_app.py:345-383`：贮备库接收发射点/隐蔽点/车辆点/贮备库点，且从外部点位读取 `capacity` 覆盖默认容量。
- `mvs/depot/depot_app.py:397-484`：新两阶段流程中，单库对已选车辆集合打分。
- `mvs/depot/depot_app.py:597-650`：旧式评分和分配请求接口。
- `mvs/depot/depot_app.py:674-727`：无具体发射任务时的库自身可服务能力评分。
- `mvs/depot/depot_app.py:811-844`：把旧式库评分包装成任务书公式解释结果。
- `mvs/depot/depot_gateway.py:48`、`mvs/depot/depot_gateway.py:77`、`mvs/depot/depot_gateway.py:160`：贮备库 gateway 启动、message_capture、请求转发。

函数逻辑讲解：贮备库软件有两条接口：旧接口可直接根据请求算库评分并返回；新接口是调度射前消解后，把每个已选车辆的发射点发给每个库，库计算到本库的距离和排名分，返回给调度做全局分配。

落实情况：信息读取、通讯、库评分、容量字段已实现。贮备库动作执行仿真、实时队列状态变化和图形化 UI 是轻量实现。

### 任务书段落 0702-0721：贮备库任务执行评分，繁忙度、等待准备、平均到达时间、总评分

代码对应：
- `mvs/common/formula_utils.py:387-432`：公式(22)-(26) 的纯函数实现。
- `mvs/common/scoring.py:421-476`：`score_depot_task_execution` 对旧式库评分结果增加 `formula_depot_*` 字段。
- `mvs/depot/depot_app.py:811-844`：`_build_task_book_depot_result` 把每个库评分包装成任务书输出。

函数逻辑讲解：任务书库评分公式当前主要作为解释字段输出，不是新两阶段主分配口径。真实全流程中的库分配采用 `a*发射点到库距离 + b*距离排名 + c*当前负载率`，因为这是后续需求明确提出的全局矩阵容量分配方式。

落实情况：公式函数和旧式输出已实现；主流程改为两阶段矩阵后，公式(22)-(26)不是最终分配的直接排序依据。这一点需要对甲方如实说明，或者后续把公式(22)-(26)合并进矩阵分数。

## 六、4.5 步进执行软件

### 任务书段落 0780-0922：步进执行软件读取信息、通讯、业务模块、路径离散、二维车辆运动、不可行回退、可视化

代码对应：
- `scripts/run_three_platform_flow.sh:21-40`：统一命令入口。
- `scripts/run_three_platform_flow.sh:500-539`：清理和生成运行配置。
- `mvs/common/platform_interfaces.py:6-50`：步进/模型/节点可复用消息类型。
- `scripts/build_trajectory_player.py`、`scripts/package_client_route_player.py`：最终轨迹回放和打包。
- `mvs/vehicle/hybrid_astar.py:120-210`：二维运动模型搜索能力。

函数逻辑讲解：目前“步进执行”的一部分能力分散在启动脚本、模拟模型/节点、车辆运动学搜索和播放器中。路径点本身带 `time/lon/lat`，播放器按时间推进车辆位置。

落实情况：部分实现。没有独立步进执行服务统一维护所有装备状态、逐步推进仿真、触发不可行回退。

## 七、4.6 硬件与部署要求

### 任务书段落 0974-0977：openEuler、Atlas/NPU、12平台、128车、12库、10秒指标

代码对应：
- `scripts/run_three_platform_flow.sh:64-68`：服务器/LAN 运行说明。
- `configs/deployment_topology.local_192.json:124`：`selected_depot_expected_count` 示例配置。
- `mvs/scheduler/scheduler_app.py:127`：默认库容量 16。
- `mvs/depot/depot_app.py:75`：贮备库默认容量 16。
- `mvs/vehicle/time_predictor.py:9-160`：纯 Python MLP，不依赖 PyTorch/ONNX。

函数逻辑讲解：当前代码更偏“离线服务器可直接运行”，用 JSON MLP 避免安装复杂推理库。真实 Atlas/NPU 部署没有接入。

落实情况：openEuler 友好性较好；NPU/ONNX/OM 未实现；规模能力依赖部署配置和机器资源，10秒指标需要每次运行日志证明。

## 八、公式逐项对应表

### 公式(1)：发射点综合代价/发射点评分

代码位置：
- `mvs/common/formula_utils.py:16-31`：`launch_point_cost`，计算 `w1*车辆到发射点距离 + w2*发射点到隐蔽点距离 - w3*保障支撑分数`。
- `mvs/common/scoring.py:205-287`：`score_launch_point_for_vehicle`，把距离、时间、隐蔽支撑、优先级、冗余合成为 `score_total`，并输出 `formula_launch_point_cost`。
- `mvs/scheduler/scheduler_app.py:4838-4858`、`mvs/scheduler/scheduler_app.py:4935-4953`：调度构建车辆候选发射点上下文时调用。

主流程作用：用于调度发给模型的候选发射点解释和排序字段。当前最终选车/选点仍以模型返回和调度冲突消解为准。

### 公式(2)：时间窗重叠

代码位置：
- `mvs/common/formula_utils.py:34-42`：`time_window_overlap`。
- `mvs/scheduler/scheduler_app.py:736-742`：贮备库补给服务时间窗排队。
- `mvs/scheduler/scheduler_app.py:1246-1248`：隐蔽点等待区间重叠。
- `mvs/scheduler/scheduler_app.py:1523-1528`：最终轨迹贮备库容量审计。

主流程作用：这是冲突消解的核心基础公式，用于判断两个时间段是否同时占用同一资源。

### 公式(3)：发射点容量差

代码位置：
- `mvs/common/formula_utils.py:53-57`：`launch_point_capacity_delta`。
- `mvs/scheduler/scheduler_app.py:2332-2347`、`mvs/scheduler/scheduler_app.py:2417-2425`：实际主流程用全局发射点匹配和 `used_launch_points/protected_launches` 保证唯一。

主流程作用：公式函数存在，但当前发射点容量按“一个发射点最终只给一辆车”处理，主流程没有直接调用 `launch_point_capacity_delta`。

### 公式(4)：隐蔽点容量差

代码位置：
- `mvs/common/formula_utils.py:60-64`：`hide_point_capacity_delta`。
- `mvs/scheduler/scheduler_app.py:1218-1261`：`_external_hide_wait_conflict_detail` 统计重叠等待车辆并判断是否超过容量。

主流程作用：真实用于射前冲突消解，避免多个车在同一隐蔽点同一时间超过容量。

### 公式(5)：车辆优先级

代码位置：
- `mvs/common/formula_utils.py:67-84`：`vehicle_priority`。
- `mvs/common/scoring.py:158-202`：`compute_dispatch_priority`。
- `mvs/scheduler/scheduler_app.py:4960-4968`：候选上下文中输出 priority 字段。

主流程作用：当前主要作为候选上下文解释字段，不直接替代模型选车结果。

### 公式(6)：贮备库槽位容量差

代码位置：
- `mvs/common/formula_utils.py:87-91`：`depot_slot_delta`。
- `mvs/scheduler/scheduler_app.py:1546-1548`：最终轨迹审计同库补给容量是否超限。
- `mvs/common/depot_assignment.py:39-115`：真实分配中用容量递减控制库满后移除。

主流程作用：容量约束真实生效。两阶段分配先用 `capacity` 控制分配数量，最终审计再次检查。

### 公式(7)：车辆-贮备库资源类型不匹配

代码位置：
- `mvs/common/formula_utils.py:94-98`：`vehicle_depot_type_mismatch`。
- `mvs/scheduler/scheduler_app.py:2906-2908`：调度把 `required_ammo_type` 发给贮备库。
- `mvs/depot/depot_app.py:430-447`：库端如有 `supported_ammo_types`，则过滤资源不匹配车辆。

主流程作用：资源匹配作为可选硬约束实现。没有资源类型字段时不影响现有流程。

### 公式(8)-(11)：路段曲率、坡度、栅格耗时、神经网络 ETA 汇总

代码位置：
- `mvs/common/formula_utils.py:101-185`：路径等距栅格化。
- `mvs/common/formula_utils.py:188-243`：曲率、坡度、单格耗时和 ETA 累计。
- `mvs/vehicle/time_predictor.py:36-65`：MLP/降级 ETA 入口。
- `mvs/vehicle/time_predictor.py:87-163`：特征构造、出口速度裁剪、MLP 前向传播。
- `mvs/vehicle/vehicle_app.py:2538-2585`：车辆主流程逐边 ETA 调用。

主流程作用：有模型 JSON 时真实用 MLP 推理；无模型时降级为速度估计。等距栅格化函数已补充，但主流程目前按路网边逐段预测，不是严格固定长度栅格批量推理。

### 公式(12)：拓扑路径搜索综合代价

代码位置：
- `mvs/common/formula_utils.py:246-264`：`topology_total_cost`。
- `mvs/common/lane_graph.py:148-155`、`mvs/common/lane_graph.py:290-292`：车道图 Dijkstra 边代价和转向惩罚。
- `mvs/vehicle/path_constraints.py:89-96`、`mvs/vehicle/path_constraints.py:220-227`：运动学约束搜索代价。
- `mvs/vehicle/hybrid_astar.py:187-194`：Hybrid A* 单步动作代价。

主流程作用：用于路径搜索代价封装，尤其在车道图和 Hybrid A* 中体现距离、曲率/转向代价。

### 公式(13)：动作模型邻居生成

代码位置：
- `mvs/common/formula_utils.py:267-276`：`generate_neighbors`。
- `mvs/vehicle/hybrid_astar.py:128-147`：Hybrid A* 每次扩展状态时调用。

主流程作用：用于走廊内可行轨迹搜索。当前主流程通常以图路径为主，Hybrid A* 是细化能力。

### 公式(14)-(15)：隐蔽点搜索范围

代码位置：
- `mvs/common/formula_utils.py:279-296`：隐蔽点搜索距离函数。
- `mvs/vehicle/vehicle_app.py:2607-2655`：实际单车时序规划入口，枚举候选隐蔽点并比较等待策略。

主流程作用：隐蔽点时序规划已经在 `_propose_path` 中实现；公式(14)(15)函数作为可解释接口保留，当前不是唯一筛选入口。

### 公式(16)-(21)：发射平台任务执行评分

代码位置：
- `mvs/common/formula_utils.py:299-384`：时间评分、安全评分、冗余评分、Sigmoid、总评分。
- `mvs/common/scoring.py:290-418`：`score_vehicle_task_execution` 主入口。
- `mvs/vehicle/vehicle_app.py:397-463`：车辆评分请求实际调用点。

主流程作用：这些公式已经进入车辆评分主流程。分数最终通过 `VEHICLE_SCORE_RESULT` 返回给请求方。

### 公式(22)-(26)：贮备库任务执行评分

代码位置：
- `mvs/common/formula_utils.py:387-432`：繁忙、等待准备、平均到达时间、区位时间、总评分。
- `mvs/common/scoring.py:421-476`：`score_depot_task_execution`。
- `mvs/depot/depot_app.py:811-844`：旧式库评分包装输出。

主流程作用：当前作为任务书解释输出。真实两阶段分配使用 `mvs/common/depot_assignment.py:6-115` 的矩阵分数。如果甲方要求公式(22)-(26)直接控制最终选库，需要把它们并入 `score_depot_vehicles` 或 `greedy_capacity_assignment` 的 `score_total`。

## 九、当前真实全链路讲解顺序

1. `prepare`：`scripts/run_three_platform_flow.sh:500-539` 清理旧运行输出，生成路网、点位、调度配置、车辆配置、贮备库配置。
2. 启动调度：`scripts/run_three_platform_flow.sh:541-573` 启动 scheduler。
3. 启动车辆：`scripts/run_three_platform_flow.sh:640-803` 启动车辆和 vehicle gateway。
4. 启动贮备库：`scripts/run_three_platform_flow.sh:575-587` 生成 depot 配置并启动 depot gateway 和库进程。
5. 调度接收任务和点位：`scheduler_app.py` 接收任务包、发射点、隐蔽点、车辆点、贮备库点，投影到路网。
6. 调度发时间倒排和候选上下文：`mvs/scheduler/scheduler_app.py:4430-4481`。
7. 车辆收到上下文后规划射前候选路径：`mvs/vehicle/vehicle_app.py:1208-1263`、`mvs/vehicle/vehicle_app.py:2607-2655`。
8. 车辆向模型/调度回传候选路径：调度在 `mvs/scheduler/scheduler_app.py:4398-4428` 接收。
9. 模型返回选车结果后，调度射前冲突消解：`mvs/scheduler/scheduler_app.py:2205-2815`。
10. 两阶段贮备库评分：调度发 `DEPOT_VEHICLE_SCORE_CONTEXT`，库端在 `mvs/depot/depot_app.py:397-484` 打分，调度在 `mvs/scheduler/scheduler_app.py:2957-3063` 全局分配。
11. 车辆收到正式库分配后规划射后路径：`mvs/vehicle/vehicle_app.py:1812-1995`。
12. 调度接收射后路径并冲突消解，组装最终轨迹：`mvs/scheduler/scheduler_app.py:3065-3135`。
13. 调度发送最终轨迹并写 message_capture：`mvs/scheduler/scheduler_app.py:4510-4590`。

## 十、需要对甲方如实说明的未实现或偏差

- UDP：代码支持 UDP，但主流程默认 TCP，和任务书“UDP 通讯库”表述不完全一致。
- ETA 批量推理：未实现批量推理；当前是逐边调用纯 Python MLP/降级模型。
- ONNX/OM/CANN/Atlas：未实现，当前为了离线 openEuler 简化为 JSON MLP。
- 400/300 隐藏层：代码支持任意 JSON MLP 层结构，但没有硬编码两个车型 400/300 模型。
- 步进执行软件：没有独立常驻进程，当前由脚本、模拟节点、日志和播放器承担。
- 贮备库公式(22)-(26)：已实现解释输出，但当前最终选库主流程采用后续需求确定的两阶段矩阵评分。
- 发射点容量公式(3)：公式函数存在，但主流程以发射点唯一分配实现容量约束。
- 实时状态监测与连续重规划：有轻量日志/状态/失败兜底，没有完整闭环仿真重规划模块。

## 十一、讲解时建议使用的证据文件

- 运行流程证据：`logs/scheduler_001_events.jsonl`、`logs/*_events.jsonl`、`logs/*_timing.jsonl`。
- 收发消息证据：`result/message_capture/scheduler/scheduler_001/send`、`result/message_capture/scheduler/scheduler_001/recv`、`result/message_capture/vehicle/*/send`、`result/message_capture/depot/*/send`。
- 最终轨迹证据：`result/latest_dispatch_trajectory_bundle.json`。
- 可视化证据：`result/visuals/` 或 `deliverables/client_route_player/`。
- 配置证据：`configs/deployment_topology.local_192.json`、`result/configs/scheduler_debug.json`、`result/configs/vehicles/*.json`、`result/configs/depots/*.json`。

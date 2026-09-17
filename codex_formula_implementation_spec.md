# 项目公式与评分函数实现说明（给 Codex）

本文档根据《集群智能控制软件原理样机研制技术方案》整理，目标是把方案中的公式转化为可落地的代码接口。公式主要覆盖：区域层级调度评分、冲突判定、车辆优先级、神经网络 ETA 时间预测、拓扑/详细路径规划代价、隐蔽点时序规划、发射平台任务评分、贮备库任务评分。

> 工程约定：
> - 除特别说明外，`score` 越大表示越优，`cost` 越小表示越优。
> - 对文档中明显“方向反”的对数评分，代码中建议提供两种实现：`doc_formula=True` 严格按文档；默认采用工程修正版。
> - 所有除法使用 `eps=1e-6` 防止除零。
> - 所有函数应写入 `formula_utils.py` 或当前项目中已有的 `scoring.py / score_utils.py`。

---

## 1. 区域调度：发射点/任务点多目标代价函数

文档公式：

```text
f(X) = w1 * f_d(X) + w2 * f_p(X) - w3 * f_n(X, R)        # (1)
```

变量含义：
- `X`：候选任务点/发射点位置。
- `f_d(X)`：任务点到车辆的距离代价，越大越差。
- `f_p(X)`：任务点到最近隐蔽点距离，越大越差。
- `f_n(X, R)`：以任务点为圆心、半径 R 范围内的贮备库支撑适配性，越大越好。
- `w1,w2,w3`：权重。

代码接口：

```python
def launch_point_cost(distance_to_vehicle: float,
                      distance_to_hide: float,
                      support_score: float,
                      w_distance: float = 0.4,
                      w_hide: float = 0.3,
                      w_support: float = 0.3) -> float:
    """公式(1)：发射点/任务点综合代价，越小越优。"""
```

支撑适配性可用下式近似实现：

```text
support_score = a * high_score_depot_count
              + b * average_depot_score
              + c * road_match_score
```

---

## 2. 发射车—发射点冲突判定公式

### 2.1 路线时空冲突指数

文档公式：

```text
C = max(0, min(t_i,e_end, t_j,e_end) - max(t_i,e_start, t_j,e_start))      # (2)
```

判定：`C > 0` 表示两车在同一路段上的占用时间窗重叠。双车道场景中，路段容量 `cap=2`，同一时间窗占用数量超过 `cap` 才需要触发容量冲突。

代码接口：

```python
def time_window_overlap(start_i: float, end_i: float,
                        start_j: float, end_j: float) -> float:
    """公式(2)：两个时间窗的重叠时长，>0 表示时间冲突。"""

def has_route_time_conflict(start_i: float, end_i: float,
                            start_j: float, end_j: float) -> bool:
    """基于公式(2)判断两车是否有路线时空冲突。"""

def has_segment_capacity_conflict(occupancy_count: int, capacity: int = 2) -> bool:
    """双车道容量约束，occupancy_count > capacity 判定为容量冲突。"""
```

### 2.2 发射点承载量冲突

文档公式：

```text
ΔG_k = N_k - G_k        # (3)
```

判定：`ΔG_k > 0` 表示发射点超载。

代码接口：

```python
def launch_point_capacity_delta(selected_vehicle_count: int, capacity: int) -> int:
    """公式(3)：发射点承载量冲突量，>0 表示超载。"""
```

### 2.3 卫星过境隐蔽点容量冲突

文档公式：

```text
ΔC_h = N_h - C_h        # (4)
```

判定：`ΔC_h > 0` 表示隐蔽点容量冲突。

代码接口：

```python
def hide_point_capacity_delta(selected_vehicle_count: int, capacity: int) -> int:
    """公式(4)：隐蔽点容量冲突量，>0 表示冲突。"""
```

### 2.4 车辆优先级函数

文档公式：

```text
P(i) = η1 / (t_i_deadline - t_i_current)
     + η2 * type(i)
     + η3 / d(i, target)                                      # (5)
```

判定：`P(i)` 越大，车辆优先级越高；冲突消解时优先保留高优先级车辆的发射点/路径。

代码接口：

```python
def vehicle_priority(deadline_time: float,
                     current_time: float,
                     vehicle_type_weight: float,
                     distance_to_target: float,
                     eta_time: float = 1.0,
                     eta_type: float = 1.0,
                     eta_distance: float = 1.0,
                     eps: float = 1e-6) -> float:
    """公式(5)：车辆优先级，越大越优先。"""
```

---

## 3. 发射车—贮备库冲突判定公式

### 3.1 贮备库补充位冲突

文档公式：

```text
ΔB_mM = N_mM - B_mM       # (6)
```

判定：`ΔB_mM > 0` 表示某贮备库对某车型的补充位不足。

代码接口：

```python
def depot_slot_delta(selected_vehicle_count: int, slot_capacity: int) -> int:
    """公式(6)：贮备库补充位冲突量，>0 表示补充位不足。"""
```

### 3.2 车型—贮备库资源适配冲突

文档公式：

```text
δ_mM = 1, if R_M != R_m
δ_mM = 0, if R_M == R_m       # (7)
```

代码接口：

```python
def vehicle_depot_type_mismatch(vehicle_resource_type: str,
                                depot_resource_type: str) -> int:
    """公式(7)：不匹配返回1，匹配返回0。"""
```

---

## 4. 神经网络 ETA 时间预测公式

文档明确采用“路径栅格化 + 神经网络预测栅格离开速度 + 逐栅格累加时间”的方法。网络输入 6 维：栅格长度、道路曲率、道路坡度、允许车速、进入速度、最大允许加/减速度；输出 1 维：离开栅格速度。

### 4.1 曲率与坡度特征

```text
κ = (θ' - θ) / L
g = (h' - h) / L
```

代码接口：

```python
def road_curvature(theta_next: float, theta: float, length: float, eps: float = 1e-6) -> float:
    """道路曲率 κ。"""

def road_grade(height_next: float, height: float, length: float, eps: float = 1e-6) -> float:
    """道路坡度 g。"""
```

### 4.2 神经网络速度预测

文档公式：

```text
V_out = f_NN(L, κ, g, V_ex, V_in, a_max)        # (8)
```

规则约束：

```text
V_in > V_out, if V_in > V_ex
V_in < V_out, otherwise                         # (9)
```

工程建议：神经网络输出后做物理约束裁剪，避免负速度、超过允许速度、超过最大加减速度。

代码接口：

```python
@dataclass
class GridFeature:
    length: float
    curvature: float
    grade: float
    allowed_speed: float
    enter_speed: float
    max_accel: float

VelocityModel = Callable[[GridFeature], float]

def clip_grid_exit_speed(raw_v_out: float,
                         v_in: float,
                         allowed_speed: float,
                         max_accel: float,
                         length: float,
                         eps: float = 1e-6) -> float:
    """对神经网络输出速度做物理约束裁剪。"""
```

### 4.3 单栅格耗时与整段路径 ETA

文档公式：

```text
t_i = t_{i-1} + 2L / (V_in + V_out)             # (10)
T_est = Σ_{i=1}^{n} t_i                         # (11)
```

注意：公式(11)按严格数学理解更合理应为 `T_est = t_n` 或 `T_est = Σ Δt_i`，其中 `Δt_i = 2L/(V_in+V_out)`。代码中建议实现为累加每段 `Δt_i`，避免把累计时间 `t_i` 再次求和导致重复累计。

代码接口：

```python
def grid_delta_time(length: float, v_in: float, v_out: float, eps: float = 1e-6) -> float:
    """公式(10)的单栅格耗时增量 Δt = 2L/(V_in+V_out)。"""

def estimate_eta_by_grid_model(grids: Sequence[GridFeature],
                               velocity_model: VelocityModel,
                               initial_speed: float | None = None,
                               eps: float = 1e-6) -> float:
    """公式(8)(10)(11)：逐栅格预测 V_out 并累加 ETA。"""
```

### 4.4 神经网络结构建议

文档说明：两层全连接隐藏层，第一层 400 个神经元，第二层 300 个神经元，输出层 1 维。建议 Codex 新增一个可选 PyTorch 模型：

```python
class ETAMLP(nn.Module):
    def __init__(self, input_dim: int = 6, hidden1: int = 400, hidden2: int = 300): ...
    def forward(self, x): ...
```

该模型不是必须接入主流程；可以先用 `VelocityModel` 协议做抽象，后续再替换为 ONNX/OM 推理。

---

## 5. 拓扑路径搜索与详细路径规划公式

### 5.1 多维融合拓扑代价

文档公式：

```text
totalCost = w_s * C_s + w_d * C_d + w_l * C_l + w_k * C_k       # (12)
```

其中：
- `C_s = Σ |h_parent - h_current|`：累计爬坡代价。
- `C_d = Σ ||x_parent - x_current||`：距离代价。
- `C_l = Σ n_landmark`：途径隐蔽点代价。
- `C_k = Σ k_bar`：累计转向代价。

代码接口：

```python
def topology_total_cost(climb_cost: float,
                        distance_cost: float,
                        landmark_cost: float,
                        curvature_cost: float,
                        w_climb: float = 1.0,
                        w_distance: float = 1.0,
                        w_landmark: float = 1.0,
                        w_curvature: float = 1.0) -> float:
    """公式(12)：拓扑搜索综合代价，越小越优。"""
```

### 5.2 A* 动作模型邻居生成

文档公式：

```text
neighbors(n) = { simulate(n, u, Δt) | u ∈ U }       # (13)
```

代码接口：

```python
def generate_neighbors(state: Any,
                       actions: Iterable[Any],
                       dt: float,
                       simulate: Callable[[Any, Any, float], Any]) -> list[Any]:
    """公式(13)：通过车辆运动学动作模型生成邻居节点。"""
```

---

## 6. 隐蔽点时序搜索范围公式

文档公式：

```text
d_before = max(d_min, (t_max - t_driving - t_waiting) * 0.8 * V_max / n), if d <= d_start
d_after  = d_end - d_start, if d > d_start                                      # (14)
```

代码接口：

```python
def hide_search_distance_before(d_min: float,
                                t_max: float,
                                t_driving: float,
                                t_waiting: float,
                                v_max: float,
                                satellite_pass_count: int,
                                eps: float = 1e-6) -> float:
    """公式(14)上半式：卫星周期开始前的隐蔽点搜索范围。"""

def hide_search_distance_after(d_start: float, d_end: float) -> float:
    """公式(14)下半式：卫星周期内可搜索/可行驶距离。"""
```

---

## 7. 发射平台任务执行评分公式

### 7.1 多维评分向量

文档公式：

```text
S = [S_time, S_safe, S_red, S_total]       # (15)
```

### 7.2 时间评分

文档公式：

```text
S_time = ln(T_est / T_max)                 # (16)
```

工程注意：文档文字说“若 T_est > T_max 则迅速下降”，但 `ln(T_est/T_max)` 在超时时会变大，方向不一致。工程上建议默认使用：

```text
S_time = ln(T_max / T_est)
```

代码接口：

```python
def time_score(estimated_time: float, max_time: float,
               doc_formula: bool = False, eps: float = 1e-6) -> float:
    """公式(16)：时间评分。默认工程修正版，doc_formula=True 时按文档原式。"""
```

### 7.3 安全评分 / 暴露概率

文档公式：

```text
η = min(t_estimate - t_max, 0) / t_hide * τ       # (17)
S_safe = 1 - η
```

工程注意：按概率含义，建议默认用 `max(t_estimate - t_max, 0)`，并将结果裁剪到 `[0,1]`。

代码接口：

```python
def exposure_probability(estimated_time: float,
                         max_time: float,
                         hide_time: float,
                         tau: float = 1.0,
                         doc_formula: bool = False,
                         eps: float = 1e-6) -> float:
    """公式(17)：暴露概率。默认工程修正版，doc_formula=True 时按文档原式。"""

def safe_score(estimated_time: float,
               max_time: float,
               hide_time: float,
               tau: float = 1.0,
               doc_formula: bool = False,
               eps: float = 1e-6) -> float:
    """安全评分 S_safe = 1 - η，返回 [0,1]。"""
```

### 7.4 路线冗余评分

文档公式：

```text
T_red = Σ_{i=1}^{4} γ_i * T_est_i        # (18)
S_red = ln(T_red / T_max)                # (19)
```

工程注意：与时间评分类似，若希望“越大越好”，建议默认使用 `ln(T_max/T_red)`。

代码接口：

```python
def weighted_redundant_time(estimated_times: Sequence[float],
                            weights: Sequence[float]) -> float:
    """公式(18)：备选发射点加权 ETA。"""

def redundancy_score(estimated_times: Sequence[float],
                     weights: Sequence[float],
                     max_time: float,
                     doc_formula: bool = False,
                     eps: float = 1e-6) -> float:
    """公式(19)：路线冗余评分。默认工程修正版。"""
```

### 7.5 Sigmoid 归一化与总评分

文档公式：

```text
S_tilde = 1 / (1 + exp(-k * (s - s0)))      # (20)
S_total = δ1 * S_time_tilde + δ2 * S_red_tilde + δ3 * S_safe_tilde     # (21)
```

代码接口：

```python
def sigmoid_normalize(score: float, k: float = 1.0, s0: float = 0.0) -> float:
    """公式(20)：Sigmoid 归一化到 (0,1)。"""

def launcher_total_score(time_score_norm: float,
                         red_score_norm: float,
                         safe_score_norm: float,
                         delta_time: float = 0.4,
                         delta_red: float = 0.2,
                         delta_safe: float = 0.4) -> float:
    """公式(21)：发射平台综合评分，越大越好。"""
```

---

## 8. 贮备库任务执行评分公式

### 8.1 繁忙程度评分

文档公式：

```text
S_busy^ω = -α * max(0, (n_w - c_w) / c_w)       # (22)
```

代码接口：

```python
def depot_busy_score(selected_count: int,
                     capacity: int,
                     alpha: float = 1.0,
                     eps: float = 1e-6) -> float:
    """公式(22)：贮备库繁忙程度惩罚，越接近0越好，超载后为负。"""
```

### 8.2 等待/准备时间评分

文档公式：

```text
S_prep^ω = -β * T_prep^ω / T_max       # (23)
```

代码接口：

```python
def depot_prep_score(prep_time: float,
                     max_time: float,
                     beta: float = 1.0,
                     eps: float = 1e-6) -> float:
    """公式(23)：贮备库等待/准备时间惩罚。"""
```

### 8.3 贮备库区位时间评分

文档公式：

```text
T_p→ω = (Σ_{i=1}^{3} T_{p_i→ω}) / 3       # (24)
S_time^ω = ln(T_est^ω / T_max)            # (25)
```

工程注意：若希望“越大越好”，建议默认使用 `ln(T_max/T_est^ω)`。

代码接口：

```python
def depot_average_arrival_time(arrival_times: Sequence[float], top_k: int = 3) -> float:
    """公式(24)：最近 top_k 个平台到达贮备库的平均 ETA。"""

def depot_time_score(arrival_times: Sequence[float],
                     max_time: float,
                     top_k: int = 3,
                     doc_formula: bool = False,
                     eps: float = 1e-6) -> float:
    """公式(25)：贮备库区位时间评分。默认工程修正版。"""
```

### 8.4 贮备库综合评分

文档公式：

```text
S_w = S_busy^ω + S_prep^ω + S_time_tilde^ω       # (26)
```

代码接口：

```python
def depot_total_score(busy_score: float,
                      prep_score: float,
                      time_score_norm: float) -> float:
    """公式(26)：贮备库综合评分。"""
```

---

## 9. Codex 实施任务清单

请 Codex 在项目中新增或更新以下文件：

1. `formula_utils.py`
   - 实现本文档全部函数。
   - 所有函数添加类型注解和 docstring。
   - 保留“公式编号”注释，便于验收时对照方案文档。
   - 对除零情况使用 `eps=1e-6`。
   - 对文档原式与工程修正版不一致的公式，使用 `doc_formula` 参数兼容。

2. `test_formula_utils.py`
   - 至少覆盖以下基本测试：
     - 时间窗重叠：重叠时返回正数，不重叠时返回 0。
     - 容量冲突：数量超过容量时 delta > 0。
     - ETA：给定常速模型时，结果等于各栅格耗时之和。
     - Sigmoid：输入 0、k=1、s0=0 时等于 0.5。
     - 发射平台总评分：三个归一化评分加权求和正确。
     - 贮备库繁忙评分：未超载为 0，超载为负。

3. 可选：`eta_mlp.py`
   - 如果项目中使用 PyTorch，则增加两层 MLP：6 → 400 → 300 → 1。
   - 如果项目走 ONNX/OM 推理，则只保留 `VelocityModel` 抽象接口，不强依赖 PyTorch。

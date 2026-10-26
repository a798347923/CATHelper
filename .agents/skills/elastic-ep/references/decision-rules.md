# Agent 侧决策表（纯逻辑，无网络依赖）

> 本表是 Agent 做"能力判定"与"retry/scale_down 指令决策"的**唯一逻辑来源**，与 `scripts/eep.py` 的实现一一对应。
> 服务端自有其校验（fault-tolerance-api.md §3），Agent 侧决策门是为了**在服务端拒绝之前就拦住**，并给出正确引导。

## 1. 能力判定

| 证据（GET /fault_tolerance/status 结果） | 结论 | 后续动作 |
| --- | --- | --- |
| HTTP `200` | `FT_ENABLED` 已启用 | 继续看 engines 状态 |
| HTTP `200` 但响应体缺 status 载荷（`capability=enabled` 却无 engines） | `STATUS_MISSING` 数据异常 | 如实报告，不猜；排查工具层/服务响应 |
| HTTP `200` 但 `engines` 与 `total_engines` 不一致、编号重复/越界、状态值非法、**engines 结构非法（非 list / 条目非对象）**、**`total_engines`/`id` 为 bool**（JSON `true`/`false` 不当作 0/1） | `STATUS_INCONSISTENT` 数据异常 | 如实报告，不猜；status 裁决 exit 1，apply 门控直接拒绝（exit 3）。**任何非法结构都不崩溃**，一律转裁决/拒绝 |
| HTTP `200` 且 `total_engines=0`（空引擎列表） | `NO_FAULT` 但数据可疑 | 提示"0 engines，先核实 status 数据"；apply 门控直接拒绝（exit 3） |
| HTTP `404` | `FT_DISABLED` 未启用 | 停止：无法下发指令；如实告知，可给启动指引（launch-parameters.md） |
| 连接失败 / 超时 | `FT_UNKNOWN` 未知 | 停止：**不可达 ≠ 未启用**；排查 host/port/工具层连通性后重试 |
| 工具层返回的 `capability` 值非法（非 `enabled`/`disabled`/`unknown`） | 数据异常 | status → `FT_UNKNOWN`（exit 5）；apply → `REJECTED`（exit 5），绝不穿透门控下发 |

> `eep.py status` 首行裁决即此三值；`FT_ENABLED` 时再给 `READY`/`WAIT_PAUSE`（见 §2）。

## 2. rank 状态判定（指令前置）

输入：engines 列表。划分：

- `H` = 状态为 `healthy` 的 rank 集合
- `D` = 状态为 `dead` 的 rank 集合
- `P` = 其余（`paused`/`unhealthy`）集合

| 条件 | 结论 | 动作 |
| --- | --- | --- |
| `H` 非空 且 `D∪P` 非空 | `WAIT_PAUSE` 暂停未完成 | **提示用户等待**：容错框架会自动下发 `pause`；轮询 `status`，直到 `H` 为空。**Agent 不代发 pause**。轮询间隔建议 5–10s；提醒用户剩余恢复窗口（`engine_recovery_timeout_sec`，默认 120s，超时引擎抛异常退出） |
| `H` = 全部 rank（全 healthy） | 无故障迹象 | 不下发任何指令；向用户核实意图（服务本来就健康） |
| `H` 空 且 `D` 非空 | 有死 rank | **只能 scale_down**（retry 必被拒："Engine dead"） |
| `H` 空 且 `D` 空 且 `P` 非空 | 全部暂停/异常 | retry 与 scale_down 均可，按 §3 选 |
| `H` 空 且 `D` 空 且 `P` 空 | 无故障迹象（引擎列表为空/异常） | 不下发任何指令，先核实 status 数据本身 |

## 3. 指令选择（`H` 为空之后）

| 故障性质 | 判据 | 指令 | 参数 |
| --- | --- | --- | --- |
| 瞬时故障（网络抖动、通信超时、可恢复异常） | 无 `dead` rank；故障类型为网络/超时类 | `retry` | `params: {"timeout": N}` |
| 永久故障（卡掉线、HBM UCE、进程死亡） | 存在 `dead` rank，或故障为硬件类 | `scale_down` | `params: {"timeout": N, "exclude_dp_ranks": [...]}` |

`timeout` 取值：默认 30，引擎多/缩容阶段长时可放大；服务端 500 "Timed out after Ns" 时增大后重试。

## 4. scale_down 的 exclude_dp_ranks 规则

| # | 规则 |
| --- | --- |
| R1 | **非空**（空列表无意义，服务端参数校验要求 list） |
| R2 | **必须包含全部 `dead` rank**（漏掉会致状态不一致） |
| R3 | 必须包含全部**故障 rank**（含仅 `unhealthy` 未死的——那是被隔离对象） |
| R4 | 不得包含健康 rank（正常 rank 不在排除之列） |
| R5 | 下发前**必须与用户确认**排除清单（scale_down 不可逆） |
| R6 | 排除后剩余 rank 数 ≥ 1（全故障时服务无法缩容，如实告知） |
| R7 | 每个编号必须在 `[0, total_engines-1]` 范围内（以**最新** status 为准——缩容后 rank 会重编号，沿用旧编号会越界被拒） |

## 5. 下发后复验（apply 200 ≠ 恢复）

| 指令 | 复验 `status` 期望 |
| --- | --- |
| `retry` | 全部引擎回到 `healthy`，`total_engines` 不变 |
| `scale_down` | `total_engines = 原值 - len(exclude_dp_ranks)`，剩余全部 `healthy`；rank 已重编号 0..N-1 |

| apply 返回 | 处理 |
| --- | --- |
| 400 | 参数非法：按 detail 修参数重发 |
| 500 "Some engines/retained engines are still healthy..." | 前置校验拒绝 → 回到 §2 等待自动 pause |
| 500 "Engine dead" | retry 遇 dead → 改 scale_down（带全 dead rank） |
| 500 "Timed out after Ns..." | 增大 `params.timeout` 重发（确认引擎仍活着：先查 status） |
| 连接失败 | `FT_UNKNOWN`，排查连通性，不重试轰炸 |

## 6. 缩容后 rank 重编号

- `scale_down` 成功后 rank 重编号为 `0..total_engines-1`（连续）。
- **后续所有指令必须使用新编号**；旧编号失效。
- 如需旧→新映射：ClientSentinel 经 ZMQ PUB 广播 `original_to_new`（或从前后两次 `status` 对照推导）。

## 7. 决策门流程图

```text
用户: 帮我 retry / 缩容
   │
   ▼
[1] 能力判定 ── 404 ──▶ 告知未启用，停止
   │            │
   │       不可达 ──▶ FT_UNKNOWN，排查连通性
   ▼ 200
[2] rank 状态
   ├─ H 非空 ──▶ 提示等待（框架自动 pause）→ 轮询 status → 回到 [2]
   │             （提醒恢复窗口剩余时间）
   └─ H 空
       ├─ D 非空 ──▶ 只能 scale_down（exclude 含全 D + 故障 rank，R1–R6）
       └─ D 空
           ├─ P 非空 ──▶ 按故障性质：瞬时→retry；永久→scale_down
           └─ P 空 ──▶ 无故障迹象，核实意图
   ▼
[3] 下发 apply（vllm_ft_apply / eep.py apply）
   ▼
[4] 复验 status（§5 期望）→ 出证据（命令 + 状态码 + JSON 原文）
```

## 8. 与 eep.py 退出码的对应

| 退出码 | 含义 |
| --- | --- |
| 0 | 正向裁决：`FT_ENABLED` / `SENT` / launch 校验通过 / `PROBE_OK` / `KILLED` |
| 1 | 传输/工具层/数据异常错误（含 `STATUS_MISSING`、`STATUS_INCONSISTENT`） |
| 2 | 用法错误（参数缺失/非法） |
| 3 | 决策门拒绝（前置条件不满足：有 healthy、dead+retry、0 engines、exclude 越界、数据不一致、硬约束不过、kill 未确认/目标不可杀） |
| 4 | 能力不存在（404，`FT_DISABLED`） |
| 5 | 状态未知（不可达，`FT_UNKNOWN`、`PROBE_UNREACHABLE`） |
| 6 | 服务端指令执行失败（apply 非 200、`PROBE_FAILED`、kill 执行失败） |

## 9. 测试辅助（probe / kill-rank）

### 9.1 probe 裁决（验证服务可推理）

| 证据 | 裁决 | 退出码 |
| --- | --- | --- |
| `POST /v1/chat/completions` → 200（含 choices） | `PROBE_OK`（附回复文本与耗时） | 0 |
| 非 200 | `PROBE_FAILED`（附状态码与 body 原文） | 6 |
| 连接失败/超时 | `PROBE_UNREACHABLE`（不可达 ≠ 服务坏，先排查连通性） | 5 |

> 用法：`retry`/`scale_down` 复验 healthy 后，用 probe 确认**推理请求真的通了**——这是"已恢复"的最强证据。

### 9.2 kill-rank 决策门（故障注入演练，顺序固定）

| # | 条件 | 结论 |
| --- | --- | --- |
| K1 | 传输：工具层（`--tool-url`，默认）或 SSH 应急通道（`--ssh`，需状态源 `--direct`/`--json`）；两者互斥；都没有 → 拒绝 | 无传输不得杀进程；SSH 是**例外**（Agent 直连节点，需用户同意），且状态仍要经 `--direct`/`--json` 读取 |
| K2 | `FT_DISABLED` 且无 `--force` | 拒绝：杀 rank 会直接崩掉整个实例；`--force` 仅限非容错实验，且必须显式 `--rank` |
| K3 | 状态未知/数据不一致/0 engines | 拒绝：无法核实目标 |
| K4 | 指定 `--rank` 越界或已 dead | 拒绝：目标不可杀 |
| K5 | 随机目标但全部 rank 已 dead | 拒绝：无活口可杀 |
| K6 | 无 `--yes` | 拒绝：破坏性操作需确认（提示目标 rank，重跑带 `--yes`） |
| K7 | 全部通过 | 工具层：`vllm_kill_rank`（`confirmed=true`，工具层二次校验 M8–M10）；SSH：远程 `pgrep -f 'EngineCore_DP{rank}([[:space:]]\|$)'` + `kill -9`（`--pattern` 可覆盖；找不到时输出 `ps` 候选） |

**kill 之后的预期链路**：被杀 rank 上报故障 → 框架自动 `pause` 存活 rank → `status` 观察（被杀 rank `dead`、其余 `paused`/`unhealthy`）→ `apply scale_down --exclude-dp-ranks <被杀rank>` 演练缩容 → 复验 healthy + probe。

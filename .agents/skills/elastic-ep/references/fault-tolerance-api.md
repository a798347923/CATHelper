# 容错 REST API 契约与状态语义

> 唯一事实来源：打补丁后的 vLLM 源码（`elastic-ep/vllm/patches/`）。本文件把契约、状态语义、服务端校验规则收敛于此，文末附源码行号索引。
> Agent 不直接调这些 REST 接口——统一经 CAT 工具层（见 tool-catalog.md）；本契约是工具层与 Agent 双方对**结果语义**的共同理解。

## 1. 术语

- **DP rank / engine**：一个数据并行 rank（一个 EngineCore 进程）。
- **rank 状态**（`EngineStatusType`）：`healthy`(0) / `dead`(1) / `unhealthy`(2) / `paused`(3)，HTTP 中以小写字符串呈现。

## 2. GET /fault_tolerance/status — 状态查询（能力判定的唯一依据）

```http
GET http://<host>:<port>/fault_tolerance/status
```

### 响应

**200 — 容错已启用：**

```json
{
  "total_engines": 4,
  "engines": [
    {"id": 0, "status": "healthy"},
    {"id": 1, "status": "paused"},
    {"id": 2, "status": "unhealthy"},
    {"id": 3, "status": "dead"}
  ]
}
```

**404 — 容错未启用**（`--enable-fault-tolerance` 未开时 router 不注册，该路径不存在）。

**连接失败 / 超时 — 未知**：既不是已启用也不是未启用。**不可达 ≠ 未启用。**

### 状态语义

| status | 含义 | 触发 |
| --- | --- | --- |
| `healthy` | 引擎正常运行 | 启动时；retry / scale_down 成功后重置 |
| `unhealthy` | 引擎遇到异常，已上报 | EngineCore 捕获非 pause 类异常，经 ZMQ 上报 ClientSentinel |
| `paused` | 引擎已暂停（busy loop 停，等待恢复指令） | 自动 pause 执行成功；`EngineLoopPausedError` |
| `dead` | 引擎进程已退出 | EngineCore 进程死亡（`EngineDeadError` 上报） |

### 自动 pause（Agent 必须等待的动作）

任一 rank 上报非 `healthy` 状态后，ClientSentinel：

1. 置 `is_faulted`；
2. 向所有**非 dead** 引擎自动下发 `pause` 指令，timeout = `max(DEEP_EP_KERNEL_TIMEOUT=100, gloo_timeout_seconds) + 5` 秒；
3. 经 ZMQ PUB 广播最新状态（外部故障管理中心 / 工具层可订阅 `external_fault_notify_port`）。

→ 这就是"用户要求 retry/缩容但还有 healthy rank 时，提示等待"的机制来源：**pause 由框架自动完成，Agent 不代发**。

### 缩容后的状态广播

`scale_down` 成功后 ClientSentinel 移除被排除引擎、**rank 重编号为 0..N-1**、剩余引擎重置 `healthy`，并经 ZMQ PUB 广播带 `type: "scale_down"`、`exclude_dp_ranks`、`original_to_new`（旧编号 → 新编号映射）的消息。

## 3. POST /fault_tolerance/apply — 容错指令下发

```http
POST http://<host>:<port>/fault_tolerance/apply
Content-Type: application/json

{"instruction": "<pause|retry|scale_down>", "params": {...}}
```

### 参数表（服务端按此校验，类型不符 → 400）

| instruction | 必填 params | 可选 params | 说明 |
| --- | --- | --- | --- |
| `pause` | `timeout`（int/float） | `exclude_engine_index`（list） | 暂停所有健康 rank（Agent 不代发） |
| `retry` | `timeout`（int/float） | — | 清理 worker 状态、重建 Gloo 通信组，恢复 busy loop |
| `scale_down` | `timeout`（int/float）、`exclude_dp_ranks`（list） | — | 移除故障 DP rank，7 阶段缩容后恢复服务 |

### 响应

| 状态码 | body | 含义 |
| --- | --- | --- |
| 200 | `{"message": "Instruction executed successfully."}` | **框架侧执行成功——不代表服务已恢复**，必须复验 status |
| 400 | detail 描述 | 请求体非法：缺 `instruction`/`params`、非法指令名、参数类型不符（`Missing or invalid timeout value. Expected: int | float.`） |
| 500 | `"Instruction execution failed. Reason: <reason>"` | 前置校验拒绝或引擎执行失败（reason 见下） |

### 服务端前置校验（`validate_ft_request`，500 的 reason 来源）

| instruction | 前置条件 | 拒绝时 reason |
| --- | --- | --- |
| `pause` | 无（永远放行——pause 本身就是把 healthy 转成 paused，拒绝会死锁） | — |
| `retry` | 无 `healthy` 引擎 | `"Some engines are still healthy — may have hung during pause: [...]"` |
| `scale_down` | **被保留**（不在 `exclude_dp_ranks` 内）的引擎中无 `healthy` | `"Some retained engines are still healthy — may have hung during pause: [...]"` |

另有框架层硬校验：`retry` 时若存在 `dead` 引擎 → `"Engine dead"`（500）——**有 dead 只能 scale_down**。

### 执行语义

- **retry**：清理状态（暂停事件、设备停止/重启、进程组重置、worker 状态清理）→ 重建 DP Gloo 通信组 → 恢复 busy loop → 全部状态重置 `healthy`。适用于瞬时故障（网络抖动、通信超时）。
- **scale_down**：Worker 侧 7 阶段——① 专家分布重算 ② 专家权重重载 ③ 专家路由重建 ④ 并行参数更新 ⑤ CPU Gloo 通信组重建 ⑥ MC2 Mask 更新 ⑦ MoE 配置更新 → 终止被排除引擎 → **rank 重编号** → 剩余引擎重置 `healthy`。适用于永久故障（卡掉线、HBM UCE、进程死亡）。
- **指令超时**：`params.timeout` 内引擎未全部响应 → 500 `"Timed out after Ns waiting for engine responses."`
- **重试窗口**：`engine_recovery_timeout_sec`（默认 120s）内引擎未收到任何有效指令 → 重新抛出原始异常并退出（`fault_tolerant_wrapper`）。

## 4. 状态流转全景

```text
healthy ──故障上报──▶ unhealthy ─┐
                                 ├─▶ 自动 pause ──▶ paused ──retry──▶ healthy（全部）
healthy ──EngineLoopPausedError─▶ paused ──┘                    └──scale_down──▶ healthy（剩余，重编号 0..N-1）
任意状态 ──进程退出──▶ dead
paused/unhealthy ──超时（engine_recovery_timeout_sec）──▶ 引擎抛原始异常并退出
```

## 5. 源码索引

> 行号以 `elastic-ep/vllm/patches/vllm_scale_down.patch`（含 `+` 行）为准，随补丁版本可能漂移，核对时以内容为准。

| 主题 | 位置 |
| --- | --- |
| `api_router.py`：路由注册、payload 校验、状态码 | 666–808 |
| `FaultToleranceConfig` 字段定义 | 433–475 |
| `EngineStatusType` 枚举 | 823–827 |
| ClientSentinel 初始化 + **恢复超时硬校验 ValueError** | 1820–1932（校验逻辑 1860–1871） |
| `ClientSentinel.pause` / `retry` / `scale_down` | 1975–2133 |
| ClientSentinel 故障接收循环 + 自动 pause | 2186–2220 |
| `MPClient.handle_fault`（服务端前置校验入口） | 1375–1390 |
| EngineCoreSentinel 故障上报（paused/unhealthy 判定） | 2387–2408 |
| `fault_tolerant_wrapper`（恢复窗口、超时抛异常） | 2742–2830 |
| `validate_ft_request`（retry/scale_down 拒绝规则） | 3096–3159 |

`vllm_ascend_scale_down.patch`：

| 主题 | 位置 |
| --- | --- |
| `EngineLoopPausedError`（Worker 暂停异常） | 1342 |
| Worker 侧 scale_down 助手（EPLB/权重重载等） | 文件内 `ScaleDownHelper` 相关段 |

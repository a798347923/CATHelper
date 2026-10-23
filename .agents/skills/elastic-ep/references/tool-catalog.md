# CAT 工具层目录（Agent ↔ 工具层契约）

> **状态：本目录是 skill 规定的契约（to-be-implemented），不是现成接口。**
> 工具层负责：接收 Agent 请求 → 解析任务 → 提交给分布式管理工具（Ansible / Slurm 等）→ 监控执行状态 → 回传结果。
> Agent 只与工具层通信，**不直连 NPU 计算节点**（大脑在集群外，手脚在集群内）。

## 1. 任务信封（Task Envelope）

### 提交任务

```http
POST {CAT_TOOL_URL}/api/tasks
Content-Type: application/json

{
  "tool": "<工具名>",
  "args": { ... }
}
```

| 响应 | 含义 |
| --- | --- |
| `202` `{"task_id": "<uuid>"}` | 已受理，进入异步执行 |
| `400` `{"error": {"code": "INVALID_ARGS", "message": "..."}}` | 参数非法（按各工具 args 表校验） |

### 轮询结果

```http
GET {CAT_TOOL_URL}/api/tasks/{task_id}
```

```json
{
  "task_id": "<uuid>",
  "state": "PENDING | RUNNING | SUCCESS | FAILED | REJECTED",
  "result": { ... } | null,
  "error": {"code": "...", "message": "..."} | null
}
```

| state | 含义 |
| --- | --- |
| `PENDING` / `RUNNING` | 继续轮询 |
| `SUCCESS` | 完成，`result` 为工具返回值 |
| `FAILED` | 执行失败，`error` 含原因（含执行节点原始输出） |
| `REJECTED` | 预检/门控拒绝（如 launch 硬约束不满足），`error.code` 说明拒绝项 |

## 2. 三个 CAT 工具

### 工具一：`vllm_ft_status` — 容错能力与 rank 状态查询

底层动作：向目标 vLLM API 服务发 `GET /fault_tolerance/status`（在集群内执行）。

**args：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `host` | string | ✅ | vLLM API 服务地址 |
| `port` | int | ✅ | vLLM API 端口 |
| `timeout_sec` | int | ❌ | HTTP 超时秒数，默认 5 |

**result（SUCCESS）：**

```json
{
  "capability": "enabled | disabled",
  "status": {
    "total_engines": 4,
    "engines": [{"id": 0, "status": "healthy"}]
  }
}
```

**失败模式：**

| 情形 | 表现 |
| --- | --- |
| vLLM 返回 404 | `capability: "disabled"`（SUCCESS，不是错误） |
| 连接失败 | `FAILED`，`error.code: "TRANSPORT_ERROR"`（**绝不映射成 disabled**） |
| HTTP 超时 | `FAILED`，`error.code: "TIMEOUT"` |

### 工具二：`vllm_ft_apply` — 容错指令下发

底层动作：向目标 vLLM API 服务发 `POST /fault_tolerance/apply`（在集群内执行）。

**args：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `host` | string | ✅ | vLLM API 服务地址 |
| `port` | int | ✅ | vLLM API 端口 |
| `instruction` | string | ✅ | `retry` \| `scale_down`（**Agent 不下发 `pause`**，见 MUST 条款） |
| `params` | object | ✅ | 指令参数，见下表 |
| `timeout_sec` | int | ❌ | 任务总超时（含 vLLM 执行），默认 60 |

**params（与 REST 契约一致，见 fault-tolerance-api.md）：**

| instruction | 必填 | 可选 |
| --- | --- | --- |
| `retry` | `timeout`（int/float） | — |
| `scale_down` | `timeout`、`exclude_dp_ranks`（int 列表，非空） | — |

**result（SUCCESS）：**

```json
{"http_status": 200, "body": {"message": "Instruction executed successfully."}}
```

**失败模式：**

| 情形 | 表现 |
| --- | --- |
| vLLM 返回 400 | `FAILED`，`error` 携带 vLLM detail（参数非法） |
| vLLM 返回 500 | `FAILED`，`error` 携带 vLLM detail（如 "Some engines are still healthy..."、"Engine dead"、"Timed out after Ns..."） |
| 连接失败 | `FAILED`，`TRANSPORT_ERROR` |

> ⚠️ `http_status: 200` **只代表框架侧指令执行成功**，不代表服务已恢复——Agent 必须再用 `vllm_ft_status` 复验。

### 工具三：`vllm_ft_launch` — 带容错 vLLM 服务拉起

底层动作：在指定节点执行带容错参数的 `vllm serve`（可经 Ansible/Slurm 分发）。

**args：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `model` | string | ✅ | 本地模型路径 |
| `served_model_name` | string | ✅ | 对外模型名 |
| `dp_size` | int | ✅ | DP rank 数 |
| `redundant_experts` | int | ✅ | 每 rank 冗余专家数（缩容可行性约束，见 launch-parameters.md） |
| `port` | int | ✅ | API 端口 |
| `fault_port` | int | ✅ | 外部故障通知端口（ZMQ PUB） |
| `internal_fault_port` | int | ❌ | 引擎故障上报端口（内部 ZMQ），默认 22866 |
| `recovery_timeout` | int | ✅ | `engine_recovery_timeout_sec` |
| `gloo_timeout_seconds` | int | ✅ | DP 域 Gloo 超时 |
| `host` | string | ❌ | 绑定地址，默认 `0.0.0.0` |
| `dp_size_local` | int | ❌ | 本节点本地 DP 数（多节点），默认 = `dp_size` |
| `npu_ids` | int 列表 | ❌ | 本节点使用的 NPU 设备 ID（多节点），默认按序 |
| `node_rank` / `num_nodes` | int | ❌ | 多节点编号（要求 `dp_size % num_nodes == 0`） |
| `extra_args` | object | ❌ | 透传其余 vLLM 参数 |

**result（SUCCESS）：**

```json
{"service_id": "...", "api_host": "...", "api_port": 8006, "fault_notify_port": 22867}
```

**失败模式：**

| 情形 | 表现 |
| --- | --- |
| 硬约束校验不过（见 launch-parameters.md §2） | `REJECTED`，`error.code: "HARD_CONSTRAINT"`，启动前即拒 |
| 启动失败 | `FAILED`，携带节点原始输出 |

## 3. MUST 条款（工具层实现必须满足）

| # | 条款 |
| --- | --- |
| M1 | `vllm_ft_status` 必须**原样区分** 200 / 404 / 传输失败——不可达必须报 `TRANSPORT_ERROR`，**绝不映射成 `disabled`** |
| M2 | `vllm_ft_apply` 必须原样回传 vLLM 的 HTTP 状态码与 body（含 500 detail 的原因串） |
| M3 | 任务必须异步执行且可轮询；`SUCCESS`/`FAILED`/`REJECTED` 为终态 |
| M4 | **不代发 `pause`**：工具层拒绝 `instruction: "pause"`（框架自动下发）；对同一服务的 `retry`/`scale_down` 重复并发下发必须拒绝（返回 `REJECTED`） |
| M5 | `vllm_ft_launch` 在提交集群前先在工具层做硬约束校验（`recovery_timeout > max(gloo_timeout, 100)` 等） |
| M6 | 每个工具支持任务超时（默认 60s，可配），超时置 `FAILED` |
| M7 | `result`/`error` 必须携带执行节点的**原始输出**（供 Agent 出证据） |

## 4. Agent 侧约定

- 选择工具：能力探测 → `vllm_ft_status`；下发恢复指令 → `vllm_ft_apply`；拉起服务 → `vllm_ft_launch`。
- 决策门（能力 → rank 状态 → 指令选择）在 **Agent 侧**完成（见 decision-rules.md），工具层只做机械执行。
- 取证脚本 `scripts/eep.py` 已实现本信封的客户端（提交 + 轮询 + verdict）；工具层未就绪时可用 `--json` 纯本地模式。

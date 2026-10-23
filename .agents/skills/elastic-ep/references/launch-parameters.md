# 带容错 vLLM 启动入参全表与硬约束

> 依据：`elastic-ep/vllm/SPEC.md §5`、`examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh`、补丁源码。
> 给启动建议时**先过 §2 硬约束**——不满足约束的参数组合会导致启动即失败或缩容不可用。

## 1. 入参全表

### 1.1 vLLM serve 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--enable-fault-tolerance` | `False` | 启用容错框架 |
| `--enable-expert-parallel` | `False` | 启用专家并行（**容错必需**） |
| `--fault-tolerance-config` | `None` | 容错配置 JSON 字典（**传入即自动启用容错**） |
| `--gloo-timeout-seconds` | `None`（回退 600） | DP 域 Gloo CPU 通信组超时（秒） |

### 1.2 FaultToleranceConfig（`--fault-tolerance-config` JSON 字段）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `engine_recovery_timeout_sec` | `120` | 故障暂停后等待恢复指令的秒数，超时抛原始异常退出 |
| `enable_fault_tolerance_rebalance` | `False` | 故障后重新调用 EPLB 做专家负载均衡 |
| `internal_fault_report_port` | `22866` | 引擎 → ClientSentinel 故障上报端口（内部 ZMQ） |
| `external_fault_notify_port` | `22867` | ClientSentinel 对外发布状态端口（ZMQ PUB） |

```json
{
  "engine_recovery_timeout_sec": 120,
  "enable_fault_tolerance_rebalance": false,
  "internal_fault_report_port": 22866,
  "external_fault_notify_port": 22867
}
```

### 1.3 ft_vllm_serve_qwen.sh 脚本参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dp-size` | `4` | 数据并行大小（DP rank 数量） |
| `--redundant-experts` | `0` | 每 rank 冗余专家数（缩容前提，见 §2 约束 C3） |
| `--host` | `0.0.0.0` | 服务器绑定地址 |
| `--port` | `8006` | API 服务端口 |
| `--fault-port` | `22867` | 外部故障通知端口 |
| `--model-name` | `/qwen-ai/Qwen3-30B-A3B-W8A8` | 模型名/路径 |
| `--local-model` | `nytopop/Qwen3-30B-A3B.w8a8` | 本地模型路径 |
| `--recovery-timeout` | `120` | = `engine_recovery_timeout_sec` |
| `--gloo-timeout-seconds` | `30` | DP 域 Gloo 超时（须 < recovery-timeout，见 C1） |

脚本固定开启：`--enable-expert-parallel`、`--enable-fault-tolerance`、EPLB dynamic（冗余专家 >0 时）、`--quantization ascend`。

### 1.4 scale_down_demo.py（模拟外部故障管理中心，可选组件）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--npu-ids` | `0-15` | 参与推理的 NPU 设备 ID 列表 |
| `--interval-time` | `3` | DCMI 硬件状态轮询间隔（秒） |
| `--external-fault-notify-port` | `22867` | ZMQ SUB 订阅端口（与 `--fault-port` 一致） |
| `--port` | `8006` | vLLM API 端口（下发 pause/scale_down 的目标） |
| `--host` | `localhost` | vLLM API 主机 |
| `--recovery-timeout` | `120` | 容错指令超时等待（秒） |

## 2. 硬约束（启动前必查，顺序即检查顺序）

| # | 约束 | 违反后果 | 依据 |
| --- | --- | --- | --- |
| C1 | `engine_recovery_timeout_sec > max(gloo_timeout_seconds, 100)`（DeepEP/nixl-ep 内核超时固定 100s） | **启动即 ValueError**：`"Engine recovery timeout must be greater than both Gloo timeout and all2all kernel timeout..."` | ClientSentinel 初始化校验（fault-tolerance-api.md 源码索引） |
| C2 | `gloo_timeout_seconds < engine_recovery_timeout_sec`（脚本默认 30 < 120） | 故障 rank 同步阻塞时健康 rank 等待超过恢复窗口，容错指令来不及执行 | SPEC §5.1.3 |
| C3 | 冗余专家：**全部健康卡上的冗余专家总数 > 故障卡上的逻辑专家数量** | scale_down 时专家放不下，缩容失败 | SPEC §5.1.3、Release_Notes |
| C4 | Expert Parallel 必须开启 | 容错框架以 EP 为前提 | SPEC §6.2 |
| C5 | TP=1（不支持 tensor parallel >1）；不支持 Pipeline Parallel | 未适配 | Release_Notes 兼容性 |
| C6 | 仅华为昇腾 A3 服务器 | 未适配其他机型 | README 前置条件 |
| C7 | 量化仅 W8A8（ModelSlim 格式；W4A8/W4A16 不支持） | 未适配 | Release_Notes |
| C8 | FULL Graph 模式不支持（Eager / PIECEWISE ACL 可） | 未适配 | Release_Notes |
| C9 | 不支持外部 LB（`local_engines_only`） | 启动即 `NotImplementedError` | ClientSentinel 初始化 |
| C10 | 端口不冲突：`internal_fault_report_port`(22866) 与 `external_fault_notify_port`(22867) 与 API `port` 三者互异 | 通信错乱/绑定失败 | SPEC §5.1.2 |

## 3. 单机 vs 多节点

### 单机（一个节点一个 vLLM 实例）

```bash
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 4 --redundant-experts 48 --fault-port 22867 \
    --recovery-timeout 120 --port 8006
```

### 多节点（多台服务器组成一个 vLLM 服务）

| 要点 | 说明 |
| --- | --- |
| 主节点 | 暴露 API 端口（对外接收 status/apply）；从节点 `--port 0` 不暴露 |
| DP 划分 | `dp_size % num_nodes == 0`；每节点 `dp-size-local = dp_size / num_nodes`；全局 DP rank = 节点偏移 + 本地编号 |
| 前置 | 各节点 SSH 免密、同一网络段、均已打补丁 |
| 故障检测 | 每节点本地检测（DCMI/CATMonitor），缩容指令统一发主节点 API |
| 缩容后 | rank 重编号 + `original_to_new` 广播，各节点 DP 窗口随之 rebase |
| 并发 | 同一时刻仅支持一个缩容；并发不同 DIE 故障会抛 `RuntimeError` |

配置驱动参考：`docs/superpowers/plans/2026-07-25-multi-server-fault-tolerance-scripts.md`（`cluster_config.json` 结构：servers/npu_mapping/coordinator/vllm_config）。

## 4. 兼容性矩阵（Release_Notes v0.1.0）

| 特性 | 状态 | 说明 |
| --- | --- | --- |
| 动态 EPLB | ✅ 已兼容 | 故障后可重新平衡专家放置 |
| 量化模型 | ⚠️ 部分 | 仅 W8A8（ModelSlim） |
| MTP（多 Token 预测） | ✅ 已兼容 | GLM-5.1 上验证 |
| Eager 模式 | ✅ | 逐算子执行，禁用图捕获 |
| PIECEWISE ACL Graph | ✅ | 分块图捕获 |
| FULL Graph | ❌ | 整图捕获不支持 |
| Pipeline Parallel | ❌ | 不支持 |
| Tensor Parallel | ⚠️ 仅 TP=1 | |
| Expert Parallel | ✅ 必须开启 | |
| NPU | ⚠️ 仅昇腾 A3 | |

## 5. 已知问题（Release_Notes v0.1.0）

- **缩容后再次缩容**存在偶现问题，会导致缩容不成功（连续缩容谨慎，且并发缩容不支持）。
- triton-ascend 3.2.1 仅适用 vllm-ascend v0.20.0+；v0.18.0 需降级到 3.2.0（README 安装说明）。

## 6. 已验证模型

| 模型 | 量化 |
| --- | --- |
| DeepSeek-V3 | W8A8 |
| Qwen3-235B-A22B | W8A8 |
| GLM-5.1 | W8A8 |

# Elastic EP Release Notes

> 本文档按时间倒序记录每次发布的版本信息。每次发布在顶部追加，不删除历史记录。

---

## v0.2.0

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.0 |
| 发布时间 | 2026-08-26 |
| 发布人 | w30024811 |
| 框架支持 | vLLM + vLLM-Ascend |
| 许可证 | Apache License 2.0 |

### 特性能力摘要

- **多节点容错**：scale_down_demo.py 新增 `--node-rank`、`--num-nodes`、`--master-host`、`--master-port` 参数，支持 hub-and-spoke 模式：各节点独立检测本地 NPU 故障，通过 dp_rank_offset 映射到全局 DP rank，所有缩容指令统一发送至主节点
- **多节点 vLLM 拉起**：ft_vllm_serve_qwen.sh 新增 `--master-ip`、`--data-parallel-rpc-port`、`--num-nodes`、`--headless`、`--data-parallel-start-rank` 参数，主/从节点分别运行脚本拉起分布式 DP 部署，脚本自动均分本地 DP 数并附加 vLLM 多节点参数
- **并发故障保护**：同一 DIE 有缩容在进行时，后续故障事件（除 recovered 外）直接 raise RuntimeError 拒绝，避免重复缩容
- **DP rank 重排检测**：通过轮询 vLLM `/fault_tolerance/status` 检测 total_engines 变化，自动重建 NPU→DP 映射

### 兼容性与限制

| 特性 | 状态 | 说明 |
|------|------|------|
| Tensor Parallel | 已支持 | 支持 TP>=1，TP>1 时每个 DP rank 占用 tp_size 张物理卡 |
| 多节点部署 | 已支持 | dp_size 须能被 num_nodes 整除，各节点 local_dp_size = dp_size / num_nodes |
| 主节点故障 | 有限 | 缩容指令超时（recovery-timeout）后报错，无自动切换 |

---

## v0.1.0

| 项目 | 说明 |
|------|------|
| 版本号 | v0.1.0 |
| 发布时间 | 2026-07-24 |
| 发布人 | sunnytao-blue |
| 框架支持 | vLLM + vLLM-Ascend |
| 许可证 | Apache License 2.0 |

### 特性能力摘要

- **外部协同**：通过vLLM内新增的容错框架，支持通过 REST API 与外部（如推理服务化框架）故障管理中心协同容错，REST API支持故障上报、弹性容错命令
- **故障检测**：支持主动通告（ZMQ）和被动查询（外部故障管理中心REST API查询）2种方式，将容错框架检查到故障上报至外部，由外部决策容错方式
- **弹性容错**：支持接收外部故障管理中心决策的弹性容错命令，当前版本支持请求重推、缩容恢复两种命令，分别对应网络瞬时故障、卡/节点故障
- **外部故障管理中心**：scale_down_demo.py 模拟外部故障管理中心，双路径故障检测（DCMI 轮询 NPU 硬件状态 + ZMQ 订阅引擎健康状态），检测到故障后下发容错命令（缩容）

### 兼容性与限制

| 特性 | 状态 | 说明 |
|------|------|------|
| 动态 EPLB | 已兼容 | 支持故障后通过 EPLB 框架重新平衡专家放置 |
| 量化模型 | 部分兼容 | 仅兼容 W8A8（ModelSlim 格式），W4A8、W4A16 等暂不支持 |
| MTP（多 Token 预测） | 已兼容 | 已完成适配，在 GLM5.1 上完成测试 |
| Eager 模式 | 已兼容 | 逐算子执行，禁用图捕获 |
| PIECEWISE ACL Graph 模式 | 已兼容 | 支持大模型分块图捕获 |
| FULL Graph 模式 | 暂未兼容 | 不支持大模型整图捕获 |
| Pipeline Parallel | 不支持 | 不支持流水线并行 |
| Tensor Parallel | 已支持 | 支持 TP>=1，TP>1 时每个 DP rank 占用 tp_size 张物理卡 |
| Expert Parallel | 必须开启 | 容错特性必须开启 Expert Parallel |
| 冗余专家数 | 有约束 | 健康卡上的冗余专家总数必须大于故障卡上的逻辑专家数量 |
| NPU支持 | 仅支持华为昇腾 A3 服务器 | 当前版本仅支持华为昇腾 A3 服务器 |

### 已知问题

缩容后，再次缩容存在一些偶现的问题，会导致缩容不成功。

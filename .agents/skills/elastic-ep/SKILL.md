---
name: elastic-ep
description: >-
  Use whenever the user asks about Elastic-EP fault tolerance on Ascend A3 vLLM
  deployments — probe whether a running vLLM service has fault tolerance enabled,
  produce launch parameters for a fault-tolerant vLLM service, or recover from a
  fault via retry / scale_down. Triggers on "容错", "fault tolerance", "vllm 容错",
  "这个 vllm 开了容错吗", "拉起带容错的 vllm", "retry", "重试", "scale_down", "缩容",
  "卡挂了", "rank 故障", "弹性容错", "elastic ep".
license: Apache-2.0
compatibility: >-
  Huawei Ascend A3 servers running vLLM v0.18.0 + vllm-ascend v0.18.0 with the
  CATHelper Elastic-EP patches applied (elastic-ep/vllm/patches/). The agent never
  touches NPU compute nodes directly; all cluster operations go through the CAT
  tool layer (task envelope, see references/tool-catalog.md).
metadata:
  author: CATHelper
  role: fault-tolerance-orchestration
---

# Elastic-EP 容错管理

## 概述

Elastic EP 是 **DP(data parallel)+EP(expert parallel)** 部署模式下的**卡级弹性容错**：卡故障后推理实例**不退出**，而是隔离故障卡所在的 DP 域、EPLB 重排专家、剩余 DP 继续服务；网络闪断类瞬时故障支持 retry 恢复。

核心心智模型：**探测能力（status）→ 判断状态 → 下发指令（apply）→ 复验（status）**。

> 架构铁律：**大脑在集群外，手脚在集群内**。Agent 只与集群内的 CAT 工具层通信（任务信封），**不直连 NPU 计算节点**；工具层负责解析任务、提交给 Ansible/Slurm 等分布式管理工具、监控执行并回传结果。`pause` 指令**不是** Agent 的业务——它由容错框架在检测到故障后**自动下发**。

> `description` 里的关键词只用于**触发本 skill**；接口能力一律以 [references/tool-catalog.md](references/tool-catalog.md) 与 [references/fault-tolerance-api.md](references/fault-tolerance-api.md) 为准，绝不凭记忆编造参数。

## 何时用 / 何时不用

**用**：用户想（a）确认某个运行中的 vLLM 服务是否开了容错；（b）知道拉起带容错的 vLLM 服务要传哪些参数；（c）在故障发生后执行重试/缩容——"帮我 retry"、"3 号卡挂了，帮我 scale_down"。

**不用**：用户想定位**为什么**出了故障（那是诊断，不是容错操作）；或对非昇腾 A3 / 未打补丁的环境。

## ⚠️ 铁律：没有验证证据，不得声称"成功"

容错操作极易产生错觉——模型可能报告"已恢复"，实际指令被拒、服务仍暂停、甚至能力根本不存在。唯一可信的成功判据是**可观测证据**：

1. **能力判定唯一依据**是 `GET /fault_tolerance/status` 的 **HTTP 状态码**：`200` = 已启用，`404` = 未启用。**不可达 ≠ 未启用**（连接失败/超时 = 未知，如实说"未知"，去排查连通性）。`200` 但缺 `engines` 载荷（`STATUS_MISSING`）、`engines` 与 `total_engines` 不一致/编号重复或越界/状态值非法/**结构非法**（`STATUS_INCONSISTENT`）、工具层 `capability` 值非法，都是**数据异常**——如实报告、不猜、不下发任何指令；`total_engines=0`（空引擎列表）裁决为无故障迹象（`NO_FAULT`）但同样**不下发指令**。eep.py 对任何非法结构**不崩溃**，一律转裁决/拒绝。
2. **Agent 只下发 `retry` / `scale_down`**；`pause` 由容错框架故障后自动下发，Agent 不代发。rank 还没全部 `paused`/`unhealthy`/`dead` 时**轮询等待**，不催、不代发。
3. **有 `healthy` rank 就不下发** `retry`/`scale_down`（服务端会拒，见 API 前置校验）——等框架自动 pause 完成。
4. **有 `dead` rank 就不能 `retry`**（框架层必然返回 "Engine dead"），只能 `scale_down`，且 `exclude_dp_ranks` 必须包含全部 dead rank。
5. **`apply` 返回 200 ≠ 服务已恢复**：它只代表框架侧指令执行成功。**必须复验 `status`**：retry 后全部回到 `healthy`（engine 数不变）；scale_down 后 `total_engines` 减少、剩余全部 `healthy`。
6. **`scale_down` 不可逆**（没有 scale_up），会 EPLB 重排 + 权重重载 + **rank 重编号**（`0..N-1`）——后续指令必须用**新编号**。
7. **恢复窗口有限**：`engine_recovery_timeout_sec`（默认 120s）内引擎没收到有效指令，会**重新抛出原始异常并退出**——决策要快，别无限期"再等等"。
8. **报告必须附证据**：每条结论后给出 ① 确切命令 ② 关键输出原文（HTTP 状态码 + JSON 原文）。不得只写"已成功"。

### 取证脚本（优先用它出证据）

```bash
# 用 python 调脚本 + 给 skill 目录下的路径：不依赖当前目录，Windows/Linux 通用
# 经工具层（默认）：
python <本 SKILL.md 所在目录>/scripts/eep.py status --tool-url http://<tool-layer>:<port> --host <vllm-host> --port 8006
python <本 SKILL.md 所在目录>/scripts/eep.py apply  --tool-url http://<tool-layer>:<port> --host <vllm-host> --port 8006 --instruction retry --timeout 30 --verify
python <本 SKILL.md 所在目录>/scripts/eep.py launch --model <model-path> --model-name <served-name> --dp-size 4 --redundant-experts 48 --tool-url http://<tool-layer>:<port>
# 纯本地取证（对已保存的 status JSON 出裁决，不联网）：
python <本 SKILL.md 所在目录>/scripts/eep.py status --json - < status.json
```

首行是裁决（其后是引擎状态原文，一并贴出）。`apply` 走完"能力探测 → 决策门 → 下发 → （--verify 复验）"全链路。退出码语义见 [references/decision-rules.md](references/decision-rules.md) 与脚本 `--help`。

### Red flags（出现即停，去核实）

| 你正想说/想做 | 实际必须做 |
| --- | --- |
| "服务连不上，说明没开容错" | 连不上 = **未知**，先排查连通性，别下结论 |
| "status 返回 200 但 engines 空/缺失" | 缺载荷 = STATUS_MISSING、数据不一致/结构非法 = STATUS_INCONSISTENT；如实报告、不猜，不下发指令（脚本不崩溃） |
| "apply 返回 200，所以恢复好了" | 复验 `GET /fault_tolerance/status`，贴原文 |
| "帮它发个 pause" | **不代发**——pause 是框架自动下发的 |
| "有卡还 healthy，直接 retry 吧" | 服务端会拒；等待自动 pause 完成再下发 |
| "有卡 dead 了，retry 一下试试" | retry 必然 "Engine dead"；只能 scale_down |
| "缩容后 rank 还是原来的编号吧" | rank 会重编号；用新编号，或查 ZMQ 广播的 original_to_new |
| "exclude 编号差不多就行" | 必须在 `[0, total_engines-1]` 内，以最新 status 为准，越界直接拒 |
| "差不多传个参数就行" | 参数以 tool-catalog / API 契约为准，硬约束先过 [launch](references/launch-parameters.md) |

## 三个能力

### 能力① 容错能力探测

问"这个 vllm 服务开了容错吗"：

1. 经工具层执行 `vllm_ft_status`（或 `eep.py status`）拿到 `GET /fault_tolerance/status` 的 HTTP 状态码；
2. `200` → 已启用（顺带给出引擎状态）；`200` 但缺 `engines` 载荷 → `STATUS_MISSING`、`engines` 与 `total_engines` 不一致/编号重复越界/结构非法 → `STATUS_INCONSISTENT`，**数据异常**，如实报告、不猜；`200` 且空引擎列表 → 无故障迹象（`NO_FAULT`）但数据可疑，不下发指令；`404` → 未启用（如实告知，可提示能力②的启动方式）；连接失败/超时 → **未知**（排查 host/port/工具层连通性，不猜）。
3. 证据：贴状态码 + 响应原文。

### 能力② 容错启动入参

问"拉起带容错的 vLLM 要传哪些参数"：

1. 按 [references/launch-parameters.md](references/launch-parameters.md) 列出完整入参（vLLM serve 参数、FaultToleranceConfig JSON、脚本包装参数）；
2. **先校验硬约束**（顺序不可颠倒）：
   - `engine_recovery_timeout_sec > max(gloo_timeout_seconds, 100)`，否则**启动即 ValueError**；
   - 冗余专家：全部健康卡冗余专家总数 > 故障卡逻辑专家数量（否则 scale_down 无法执行）；
   - EP 必须开启、TP=1、无 PP、仅昇腾 A3、仅 W8A8、FULL Graph 不支持；
3. 给出可直接执行的命令（单机模板 / 多节点要点），并把校验结论明确告诉用户。
4. 建议参数不满足约束时**明确指出**并给出修正值，不给"启动会挂"的参数组合。

### 能力③ 故障后恢复指令（retry / scale_down）

用户要求"重试/缩容"时，**决策门**（顺序固定，任何一步不过就停）：

1. **先判容错能力**（能力①）：未启用 → 如实告知，无法下发；未知 → 排查连通性；status 数据缺失/空引擎列表 → 数据异常，不下发，先核实。
2. **再判 rank 状态**（`status` 的 engines 列表）：
   - 存在 `healthy` rank → **提示用户等待**：当前容错框架会自动下发 `pause` 指令，等指令执行成功（所有 rank 进入 `paused`/`unhealthy`/`dead`）后再下发恢复指令。**Agent 不代发 pause**，轮询 `status` 观察即可（最长等到 `engine_recovery_timeout_sec`，超时引擎会退出——提醒用户）。
   - 全部处于 `paused`/`unhealthy`/`dead` → 进入第 3 步。
   - 全部 `healthy` → 无故障迹象，不下发，与用户核实意图。
3. **选指令**（依据故障性质 + rank 状态，完整决策表见 [references/decision-rules.md](references/decision-rules.md)）：
   - 有 `dead` rank → **只能 scale_down**，`exclude_dp_ranks` 必须含全部 dead + 故障 rank，且每个编号在 `[0, total_engines-1]` 内（以最新 status 为准）；
   - 无 dead → 瞬时故障（网络抖动/超时）用 `retry`；永久故障（卡掉线/HBM UCE）用 `scale_down`；
   - 有 healthy → 回到第 2 步等待（服务端会拒）。
4. **下发 + 复验**：`vllm_ft_apply`（或 `eep.py apply --verify`）→ `200` 只代表框架侧成功，**必须复验 status**（retry 后全 healthy；scale_down 后 total_engines 减少且全 healthy）。
5. **出证据**：贴「指令 + HTTP 状态码 + status 复验原文」。

## 安全红线（必须遵守）

- **不代发 `pause`**：未就绪时轮询等待；框架自动 pause 完成后 rank 会进入 paused/unhealthy/dead。
- **有 healthy 不下发 retry/scale_down**：服务端拒绝只是最轻的后果，真正的风险是状态不一致导致恢复失败或级联故障。
- **dead 不 retry**：框架层必然 "Engine dead"。
- **scale_down 不可逆**：下发前**必须与用户确认**要排除的 rank 清单（这是不可恢复操作，无 scale_up）；确认后 `exclude_dp_ranks` 一字不差地传。
- **连续缩容受限**：已知问题——缩容后再缩容偶现失败；并发缩容不支持。若用户要连续缩容，明确告知风险，且必须等前一次复验 healthy 后再做下一次。
- **恢复窗口**：`engine_recovery_timeout_sec`（默认 120s）超时引擎抛原始异常退出——决策拖延等于让服务退出。等待阶段要主动提醒用户剩余窗口。
- **不直连集群**：Agent 不 SSH/直连 NPU 计算节点，一律走 CAT 工具层。`eep.py --direct` 仅限调试且需用户明确同意。

## 标准工作流

```text
- [ ] 1. 确认意图属于能力①/②/③；确认服务 host:port 与工具层地址
- [ ] 2. 能力①：status 探测（eep.py status）→ FT_ENABLED / FT_DISABLED / FT_UNKNOWN / STATUS_MISSING / STATUS_INCONSISTENT
- [ ] 3. 能力②：按 launch-parameters.md 列参 + 先过硬约束，给出可执行命令
- [ ] 4. 能力③：决策门——能力 → rank 状态 → 指令选择 → 下发 → 复验
- [ ] 5. 出证据：贴「命令 + 状态码 + JSON 原文」
- [ ] 6. 等待场景：轮询 status，直到无 healthy rank；提醒用户恢复窗口剩余时间
```

## 常见错误

- **把"连不上"当"未启用"** → 不可达 = 未知，排查 host/port/工具层。
- **status 200 但 engines 空/缺失就继续决策** → 缺载荷 = STATUS_MISSING、不一致/结构非法 = STATUS_INCONSISTENT，数据异常不猜；先核实工具层/服务响应。
- **capability 值异常还继续走门控** → eep.py 直接 REJECTED（exit 5），不穿透下发；如实报告工具层响应异常。
- **代发 pause** → 违反铁律；pause 是框架自动行为，Agent 只等。
- **有 healthy rank 就下发** → 服务端 500 "Some engines are still healthy — may have hung during pause"；回去等待。
- **有 dead rank 还 retry** → 500 "Engine dead"；改 scale_down 并带上 dead rank。
- **apply 200 就宣称"已恢复"** → 必须复验 status。
- **缩容后还用旧 rank 编号** → 缩容会重编号（0..N-1）；查最新 status / original_to_new。
- **exclude_dp_ranks 漏掉 dead rank** → 服务端校验/框架行为异常；dead 必须全部在 exclude 里。
- **exclude_dp_ranks 编号越界** → 必须在 `[0, total_engines-1]` 内（最新 status 为准）；越界直接拒。
- **timeout 太小** → 500 "Timed out after Ns waiting for engine responses."；放大 params.timeout。
- **recovery_timeout ≤ max(gloo_timeout, 100)** → 启动即 ValueError；参看 launch-parameters.md。
- **端口冲突（C10）** → `internal_fault_report_port`(22866)/`external_fault_notify_port`(22867)/API `port` 三者必须互异。

## 参考

- CAT 工具层目录（3 个工具 + 任务信封 + MUST 条款，**契约，工具层 to-be-implemented**）：[references/tool-catalog.md](references/tool-catalog.md)
- REST 契约 / 状态语义 / 服务端校验 / 源码行号索引：[references/fault-tolerance-api.md](references/fault-tolerance-api.md)
- 启动入参全表 / 硬约束 / 单机与多节点差异 / 兼容性矩阵：[references/launch-parameters.md](references/launch-parameters.md)
- 能力判定与 retry/scale_down 完整决策表（Agent 侧纯逻辑）：[references/decision-rules.md](references/decision-rules.md)
- 取证与执行脚本：[scripts/eep.py](scripts/eep.py)（stdlib-only，带 verdict 与退出码）
- 上游项目文档（若可得）：`elastic-ep/vllm/` 下 `SPEC.md`、`DESIGN.md`、`README.md`、`Release_Notes.md`、`test_report.md`

# CATHelper skills

本目录存放 CATHelper 的 **Agent Skill**。

> **如何使用**：`.agents/skills` 是工具中立的存放处——各 agent 运行时默认不会扫描它。把它下面的技能**拷贝或软链**到你所用运行时的目录即可：
>
> | 运行时 | 目标目录 |
> | --- | --- |
> | opencode | 项目 `.opencode/skill/`，或全局 `~/.config/opencode/skill/` |
> | Claude Code | 项目 `.claude/skills/`，或全局 `~/.claude/skills/` |
> | 通用别名 | `~/.agents/skills/` |
>
> ```bash
> # 软链示例（随仓库更新）
> ln -s "$(pwd)/.agents/skills/elastic-ep" ~/.config/opencode/skill/elastic-ep
> ```

## 目录

- `skills/` — 技能本体。每个子目录是一个**自洽子技能**（`SKILL.md` + `references/` + `scripts/`）。

| 技能 | 状态 | 领域 |
| --- | --- | --- |
| [`elastic-ep`](skills/elastic-ep/) | ✅ 已实现 | 昇腾 A3 上带容错的 Elastic-EP vLLM 服务：容错能力探测、容错启动入参、故障后 `retry` / `scale_down` 决策 |
| `accuracy-monitoring` | 🚧 占位 | 精度监控（`SKILL.md` 为空，待补） |
| `straggler-detection` | 🚧 占位 | 慢卡/掉队节点检测（`SKILL.md` 为空，待补） |

---

## `elastic-ep` — Elastic EP 容错管理

把"这个 vllm 开了容错吗"、"拉起带容错的 vllm 要传哪些参数"、"帮我 retry / scale_down"这类自然语言意图，
翻译成**可下发、可验证**的容错操作。

### 做什么

- **能力① 容错能力探测**：只认 `GET /fault_tolerance/status` 的 HTTP 状态码（`200` = 已启用，`404` = 未启用，不可达 ≠ 未启用）；
- **能力② 容错启动入参**：给出可直接执行的启动命令，并**先**校验硬约束（恢复超时、TP/EP、冗余专家、兼容性）；
- **能力③ 故障后恢复指令**：先判容错能力 → 再判 rank 是否全部处于 `paused`/`unhealthy`/`dead` → 是则给指令，否则**提示等待**（`pause` 由框架自动下发，Agent 不代发）；
- **能力④ 测试请求探测**：向服务发送最小化推理请求，验证推理链路（`PROBE_OK` / `PROBE_FAILED` / `PROBE_UNREACHABLE`）；
- **能力⑤ 故障注入演练**：`kill-rank` 随机杀死一个 DP rank 触发容错链路（仅测试；未开容错默认拒绝，需 `--yes` 确认；默认走工具层，无工具层时可用 `--ssh` 应急通道）；
- **防错觉**：`apply` 返回 `200` 只代表框架侧指令执行成功，**不等于**服务已恢复——必须复验 `status`；
- 架构遵循"**大脑在集群外，手脚在集群内**"：Agent 只与集群内的工具层通信，不直连 NPU 计算节点。

### 文件布局

| 文件 | 作用 |
| --- | --- |
| `SKILL.md` | 技能正文（**稳定**：铁律、决策门、安全红线、工作流） |
| `references/tool-catalog.md` | 五个 CAT 工具的参数/返回/失败模式；工具层任务信封与 MUST 条款 |
| `references/fault-tolerance-api.md` | `/fault_tolerance/*` REST 契约、状态语义、服务端校验规则、源码行号索引 |
| `references/launch-parameters.md` | 容错启动入参全表、硬约束、单机/多节点差异、兼容性矩阵 |
| `references/decision-rules.md` | 能力判定、`retry`/`scale_down` 与 probe/kill-rank 的完整决策表（Agent 侧纯逻辑） |
| `scripts/eep.py` | 取证与执行脚本：`status` / `apply` / `launch` / `probe` / `kill-rank` 子命令（stdlib-only，带 verdict 与退出码） |
| `scripts/test_eep.py` | eep.py 回归测试（36 用例，含本地 mock 工具层/模拟 vLLM，stdlib-only） |

### 触发

直接说需求即可，例如："这个 vllm 服务开了容错吗"、"拉起带容错的 vllm 要传哪些参数"、
"3 号卡挂了，帮我 scale_down"、"能不能 retry 一下"、"发个测试请求看看服务还通不通"、
"kill 一个 rank 演练一下缩容"。

### 关键约束

- **能力判定唯一依据**是 `GET /fault_tolerance/status` 的 HTTP 状态码；不可达**不等于**未启用。
- **Agent 只下发 `retry` / `scale_down`**；`pause` 由容错框架自动下发，Agent 不代发——未就绪时**轮询等待**。
- **有 `healthy` rank 就不下发** `retry` / `scale_down`（服务端会拒），等框架自动 `pause` 完成。
- **有 `dead` rank 就不能 `retry`**（框架层必然 `Engine dead`），只能 `scale_down` 且必须把 `dead` rank 放进 `exclude_dp_ranks`。
- **`scale_down` 不可逆**（无 `scale_up`）、会 EPLB 重排 + 权重重载 + **rank 重编号**——后续指令必须用新编号。
- **恢复窗口有限**：`engine_recovery_timeout_sec`（默认 120s）内没收到有效指令，引擎会抛出原始异常并退出。
- **启动硬约束**：`engine_recovery_timeout_sec >= max(gloo_timeout_seconds, 100)`，否则启动即 `ValueError`。
- **工具层尚未实现**：`references/tool-catalog.md` 里的任务信封是**本 skill 规定的契约**（to-be-implemented），不是现成接口。

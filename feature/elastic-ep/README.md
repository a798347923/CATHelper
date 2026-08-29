# Elastic EP

> **Elastic EP** — 推理大EP卡级弹性容错

Elastic EP是CATHelper系列特性之一，实现推理大EP部署的卡级弹性容错，目前仅支持vLLM，后续计划也支持SGLang。 Elastic EP特性实现DP(data parallel)+EP(expert parallel)的部署模式下，卡故障之后，推理实例不退出，而是将故障卡所在的DP域隔离掉，重排专家后剩余DP继续提供推理服务，也支持网络闪断故障后请求重推恢复。

## 版本信息

> 详见 [Release_Notes.md](Release_Notes.md#v010)。

## 功能特性

> 详见 [Release_Notes.md §特性能力摘要](Release_Notes.md#特性能力摘要)。

## 技术栈

> 详见 [SPEC.md §1.2 技术栈](SPEC.md#12-技术栈)。

## 快速上手

### 前置条件

- 华为昇腾A3服务器：当前版本仅支持A3

### 安装

#### Step 1：拉取官方 vLLM Ascend Docker 镜像

```bash
docker pull quay.io/ascend/vllm-ascend:v0.18.0-a3
docker run -it --net=host --ipc=host --privileged=true \
    -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    quay.io/ascend/vllm-ascend:v0.18.0-a3 bash
```

#### Step 2：打容错框架补丁

```bash
cd /vllm-workspace/vllm
git fetch --all && git checkout v0.18.0 && git reset --hard bcf2be96
git apply /path/to/patches/vllm_scale_down.patch

cd /vllm-workspace/vllm-ascend
git fetch --all && git checkout v0.18.0 && git reset --hard 4a533861
git apply /path/to/patches/vllm_ascend_scale_down.patch
```

#### Step 3：安装vllm和vllm-ascend

```bash
cd /vllm-workspace/vllm
VLLM_TARGET_DEVICE=empty pip install -e .

cd /vllm-workspace/vllm-ascend
git submodule update --init --recursive
pip uninstall triton-ascend
pip uninstall triton
pip install triton-ascend==3.2.1 --extra-index-url=https://mirrors.huaweicloud.com/ascend/repos/pypi
pip install --no-build-isolation -e . --no-cache-dir
```

### 使用

`examples/fault_tolerance_scale/` 目录下提供一个演示demo,可以启动支持容错的vLLM服务、以及一个外部故障管理中心；

| 脚本 | 说明 |
|------|------|
| `ft_vllm_serve_qwen.sh` | 启动支持弹性容错功能的 vLLM 服务，模型Qwen3-30B-A3-W8A8；支持单节点与多节点（主/从）拉起，多节点时自动附加 `--data-parallel-address`/`--data-parallel-rpc-port`，从节点加 `--headless`/`--data-parallel-start-rank` |
| `scale_down_demo.py` | 外部故障管理中心，双路径故障检测：① 通过 `catmonitor_fault_sub.py` 订阅 **CATMonitor** 推送的 NPU 故障事件（HTTP webhook，替代原 DCMI 轮询）；② ZMQ 订阅引擎健康状态，接收引擎异常上报。检测到故障后自动通过 REST API 发送 pause/scale_down/retry 指令；支持多节点 hub-and-spoke 部署（`--node-rank`/`--num-nodes`/`--master-host`/`--master-port`），各节点独立检测并把缩容指令统一发往主节点 |
| `catmonitor_fault_sub.py` | CATMonitor 故障订阅器：向 CATMonitor REST 注册 webhook，接收 `FaultEvent`，NPU→DP 映射后下发 vLLM 容错指令；支持 TP>1（`--tp-size`）与多节点 DP rank 偏移（`dp_rank_offset`），并轮询 `/fault_tolerance/status` 检测缩容后的 DP rank 重排自动重建映射 |

**ft_vllm_serve_qwen.sh 参数：**

> 详见 [SPEC.md §5.1.3](SPEC.md#513-serve_qwensh-脚本参数)。

**scale_down_demo.py 参数：**

> 详见 [SPEC.md §5.1.4](SPEC.md#514-scale_downpy-脚本参数)。

> **注意：** 运行前需根据实际环境修改脚本中的模型权重路径（`LOCAL_MODEL_PATH`、`MODEL_NAME` 等参数），或通过命令行参数覆盖。

> **前置依赖（新增）：** 故障管理中心需依赖 **CATMonitor daemon**（启用 `faultsub` 特性）提供 NPU 故障事件。先构建并启动 CATMonitor：
> ```bash
> cd CATHelper/CATMonitor && GOFLAGS=-tags=dcmi make build   # NPU 环境需加 dcmi 构建标签
> export PATH=$PATH:./bin
> mkdir -p /etc/catmonitor/
> cp configs/catmonitor.yaml /etc/catmonitor/catmonitor.yaml
> # 编辑 catmonitor.yaml，设 faultsub.enabled: true
> catmonitor daemon                               # 采集 + /metrics:9100 + faultsub REST:9101
> ```
> 详见 [CATMonitor/README.md](../../CATMonitor/README.md) 与 [整合设计](EEP_combination_DESIGN.md)。

---

#### 场景一：启动模拟的外部故障管理中心（自动响应故障）

模拟的外部故障管理中心通过双路径检测（CATMonitor webhook 订阅 NPU 故障 + ZMQ 订阅引擎健康状态），检测到故障后自动发送缩容指令（暂停由引擎异常自动触发），全程无需人工干预。

**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 4 --redundant-experts 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：启动外部故障管理中心demo(可选)**
不启动demo时，vLLM内的容错框架仍会拦截异常，并等待容错命令，用户也可以通过REST API手动发送'retry(重试)'或'scale_down(缩容)'
```bash
python examples/fault_tolerance_scale/scale_down_demo.py \
    --dp-size 4 \
    --catmonitor-host localhost --catmonitor-rest-port 9101 \
    --callback-port 9102 --advertise-url http://localhost:9102/fault_event \
    --external-fault-notify-port 22867 --port 8006
```
`--dp-size` 须与 vLLM 一致（默认取全部可见卡）；`--npu-ids` 缺省时按 `ASCEND_RT_VISIBLE_DEVICES`（默认 `0-15`）与 `--npu-per-die 2` 自动推导 A3 的 8 个 DIE。

**TP>1 示例（TP2/DP2，占用 4 张物理卡）：** 每个 DP rank 占用 `tp_size` 张连续物理卡，`scale_down_demo.py` 与 `ft_vllm_serve_qwen.sh` 均传 `--tp-size`（vLLM 侧为 `--tensor-parallel-size`），DP rank `r` 绑定 `visible_devices[r*tp_size:(r+1)*tp_size]`：

```bash
# Step 1：启动 vLLM（--tp-size 与 --dp-size 配合，总卡数 = dp-size * tp-size）
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 2 --tp-size 2 --redundant-experts 48 --fault-port 22867 \
    --recovery-timeout 120 --port 8006

# Step 2：启动外部故障管理中心（--tp-size 须与 vLLM 一致）
python examples/fault_tolerance_scale/scale_down_demo.py \
    --dp-size 2 --tp-size 2 \
    --catmonitor-host localhost --catmonitor-rest-port 9101 \
    --callback-port 9102 --advertise-url http://localhost:9102/fault_event \
    --external-fault-notify-port 22867 --port 8006
```
故障 DIE 时整个 DP rank（全部 `tp_size` 张卡）一并缩容，保证剩余卡的 DP rank 仍保持 `tp_size` 连续分组。

**多节点（hub-and-spoke）部署示例：** vLLM 与故障管理中心分别在主/从节点拉起，`--dp-size` 传全局 DP 总数（`dp_size` 须能被 `num_nodes` 整除），各节点本地 DP 数为 `dp_size / num_nodes`：

```bash
# Step 1a：主节点拉起 vLLM（本地 rank 0-3，起 API server）
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 8 --num-nodes 2 --master-ip <master-ip>

# Step 1b：从节点拉起 vLLM（本地 rank 4-7，无 API server；
# --data-parallel-start-rank 为从节点第一个 local_rank 的全局 world_rank）
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 8 --num-nodes 2 --master-ip <master-ip> \
    --headless --data-parallel-start-rank 4

# Step 2a：主节点运行外部故障管理中心（node-rank 0）
python examples/fault_tolerance_scale/scale_down_demo.py \
    --dp-size 8 --num-nodes 2 --node-rank 0 \
    --catmonitor-host localhost --callback-host 0.0.0.0 --callback-port 9102 \
    --advertise-url http://<master-ip>:9102/fault_event \
    --external-fault-notify-port 22867 --host <master-ip> --port 8006

# Step 2b：从节点运行外部故障管理中心（node-rank 1）
python examples/fault_tolerance_scale/scale_down_demo.py \
    --dp-size 8 --num-nodes 2 --node-rank 1 \
    --catmonitor-host localhost --callback-host 0.0.0.0 --callback-port 9103 \
    --advertise-url http://<node1-ip>:9103/fault_event \
    --external-fault-notify-port 22867 --master-host <master-ip> --master-port 8006
```
主/从节点的启动命令完全一致（仅 `--node-rank` / 回调端口 / master 地址不同）。每个节点运行外部故障管理中心时自动按 `dp_rank_offset` 计算**自己拥有的 DP rank 窗口**（如 node-rank 0 拥有 0-3，node-rank 1 拥有 4-7），只在窗口内响应引擎死亡事件，避免多个节点对同一个故障 rank 并发发出重复缩容指令；缩容成功后接收广播中的 `original_to_new` 重编号映射，自动 rebase 本地窗口。
其他节点缩容导致 DP rank 重排时，各节点通过轮询 `GET /fault_tolerance/status` 的 `total_engines` 自动检测并重建 NPU→DP 映射。多节点 + TP>1 同样支持：各节点再传 `--tp-size`（vLLM 与 demo 同步），本地卡数 = `dp_size / num_nodes * tp_size`；TP 分组不跨节点。

**步骤 3：发送推理请求**

服务就绪后，正常发送推理请求。

**步骤 4：注入故障**

模拟 NPU 故障，例如 kill 掉某个 Worker 进程。

**步骤 5：等待自动恢复**

模拟的外部故障管理中心检测到故障后自动执行：
1. CATMonitor 采集 NPU 指标并判定故障（卡掉线/HBM UCE/RoCE 链路等），经 HTTP webhook 推送 `FaultEvent` 给故障管理中心
2. 故障管理中心把故障 DIE（如 DIE 5 = 物理卡 10,11）映射为对应 DP rank 列表（rank 10,11），通过查询容错状态确认暂停完成
3. 发送缩容指令，一次性移除故障 DIE 的全部 DP rank
4. 服务在剩余健康 NPU 上恢复，推理继续

---

#### 场景二：不启动模拟的外部故障管理中心（手动响应故障）


**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
    --dp-size 4 --redundant-experts 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：发送推理请求**

服务就绪后，正常发送推理请求。


**步骤 3：注入故障**

模拟 NPU 故障，例如 kill 掉某个 Worker 进程。

**步骤 4：手动查询容错状态**

```bash
curl http://localhost:8006/fault_tolerance/status
```

返回结果中健康 DP rank 状态应为 `paused`，确认暂停成功。

**步骤 5：手动发送恢复指令**

根据故障类型选择重试或缩容：

```bash
# 重试（重启所有 DP rank）
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry","params":{"timeout":30}}'

# 缩容（排除指定 DP rank）
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```

## 特性兼容与限制

> 详见 [Release_Notes.md §兼容性与限制](Release_Notes.md#兼容性与限制)。

## 已测试模型

本特性已在以下模型上完成验证：

| 模型 | 量化 |
|------|------|
| DeepSeek-V3 | W8A8 |
| Qwen3-235B-A22B | W8A8 |
| GLM-5.1 | W8A8 |

## 已知问题

> 详见 [Release_Notes.md §已知问题](Release_Notes.md#已知问题)。

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 技术规格与需求 |
| [DESIGN.md](DESIGN.md) | 架构与模块设计 |
| [Release_Notes.md](Release_Notes.md) | 版本发布记录 |
| [test_report.md](test_report.md) | 测试报告 |

### 项目结构

> 详见 [DESIGN.md §2.1 目录结构](DESIGN.md#21-目录结构)。

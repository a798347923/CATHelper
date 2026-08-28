#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the CATHelper project
set -e

HOST="0.0.0.0"
MASTER_IP=""
LOCAL_IP="51.62.10.87"
NIC_NAME="enp196s0f0"
PORT=8006
DATA_PARALLEL_SIZE=4
TENSOR_PARALLEL_SIZE=1
REDUNDANT_EXPERTS=0
FAULT_PORT=22867
LOCAL_MODEL_PATH="nytopop/Qwen3-30B-A3B.w8a8"
MODEL_NAME="/qwen-ai/Qwen3-30B-A3B-W8A8"
GLOO_TIMEOUT=30
RECOVERY_TIMEOUT=120
NUM_NODES=1
DATA_PARALLEL_RPC_PORT=13389
HEADLESS=0
DATA_PARALLEL_START_RANK=0

export HCCL_IF_IP=$LOCAL_IP
export GLOO_SOCKET_IFNAME=$NIC_NAME
export TP_SOCKET_IFNAME=$NIC_NAME
export HCCL_SOCKET_IFNAME=$NIC_NAME
export TASK_QUEUE_ENABLE=0

while [[ $# -gt 0 ]]; do
    case $1 in
        --dp-size)
            DATA_PARALLEL_SIZE="$2"
            shift 2
            ;;
        --tp-size)
            TENSOR_PARALLEL_SIZE="$2"
            shift 2
            ;;
        --redundant-experts)
            REDUNDANT_EXPERTS="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --fault-port)
            FAULT_PORT="$2"
            shift 2
            ;;
        --model-name)
            MODEL_NAME="$2"
            shift 2
            ;;
        --local-model)
            LOCAL_MODEL_PATH="$2"
            shift 2
            ;;
        --gloo-timeout-seconds)
            GLOO_TIMEOUT="$2"
            shift 2
            ;;
        --recovery-timeout)
            RECOVERY_TIMEOUT="$2"
            shift 2
            ;;
        --master-ip)
            MASTER_IP="$2"
            shift 2
            ;;
        --data-parallel-rpc-port)
            DATA_PARALLEL_RPC_PORT="$2"
            shift 2
            ;;
        --num-nodes)
            NUM_NODES="$2"
            shift 2
            ;;
        --headless)
            HEADLESS=1
            shift 1
            ;;
        --data-parallel-start-rank)
            DATA_PARALLEL_START_RANK="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --dp-size SIZE                 Set data parallel size, i.e. number of DP ranks to launch (default: 4)"
            echo "  --tp-size SIZE                 Set tensor parallel size (default: 1)"
            echo "  --redundant-experts SIZE       Set number of redundant experts per rank for scale-down redistribution (default: 0)"
            echo "  --host HOST                    Set host address (default: 0.0.0.0)"
            echo "  --port PORT                    Set port number (default: 8006)"
            echo "  --fault-port FAULT_PORT        Set external fault notify port (default: 22867)"
            echo "  --gloo-timeout-seconds GLOO_TIMEOUT    gloo communication group timeout (seconds)"
            echo "  --recovery-timeout RECOVERY_TIMEOUT    engine recovery timeout (seconds, default: 120)"
            echo "  --master-ip MASTER_IP                  data parallel master IP (multi-node only; master and workers both set it)"
            echo "  --data-parallel-rpc-port PORT          data parallel master RPC port (multi-node only, default: 13389)"
            echo "  --num-nodes NUM_NODES                  total number of nodes (default: 1; dp-size must be divisible by num-nodes)"
            echo "  --headless                             worker node: start engine core only, no API server (requires --master-ip)"
            echo "  --data-parallel-start-rank RANK        worker node: world_rank of its first local rank (requires --headless)"
            echo "  --model-name MODEL_NAME                Set model name or path"
            echo "  --local-model LOCAL_MODEL_PATH Use local model at $LOCAL_MODEL_PATH"
            echo "  -h, --help                     Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

echo "Starting vLLM server for $MODEL_NAME with data parallel size: $DATA_PARALLEL_SIZE, tensor parallel size: $TENSOR_PARALLEL_SIZE, and redundant experts: $REDUNDANT_EXPERTS"

LOCAL_DP_SIZE=$((DATA_PARALLEL_SIZE / NUM_NODES))
if (( DATA_PARALLEL_SIZE % NUM_NODES != 0 )); then
    echo "ERROR: --dp-size $DATA_PARALLEL_SIZE must be divisible by --num-nodes $NUM_NODES" >&2
    exit 1
fi
if [[ -n "$MASTER_IP" ]] && (( NUM_NODES == 1 )); then
    echo "WARNING: --master-ip given but --num-nodes is 1; ignoring multi-node args" >&2
fi
if (( NUM_NODES > 1 )) && [[ -z "$MASTER_IP" ]]; then
    echo "ERROR: multi-node (--num-nodes $NUM_NODES) requires --master-ip" >&2
    exit 1
fi
if (( HEADLESS == 1 )) && [[ -z "$MASTER_IP" ]]; then
    echo "ERROR: --headless (worker node) requires --master-ip" >&2
    exit 1
fi

DP_MULTI_NODE_ARGS=""
if [[ -n "$MASTER_IP" ]] && (( NUM_NODES > 1 )); then
    DP_MULTI_NODE_ARGS="--data-parallel-address $MASTER_IP --data-parallel-rpc-port $DATA_PARALLEL_RPC_PORT"
    if (( HEADLESS == 1 )); then
        DP_MULTI_NODE_ARGS="$DP_MULTI_NODE_ARGS --headless --data-parallel-start-rank $DATA_PARALLEL_START_RANK"
    fi
fi

export HCCL_BUFFSIZE=2048

if [[ "$REDUNDANT_EXPERTS" -gt 0 ]]; then
    export DYNAMIC_EPLB="true"
fi

vllm serve "$LOCAL_MODEL_PATH" \
    --served-model-name "$MODEL_NAME" \
    --data-parallel-size "$DATA_PARALLEL_SIZE" \
    --data-parallel-size-local "$LOCAL_DP_SIZE" \
    --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
    --enable-expert-parallel \
    --enable-fault-tolerance \
    --fault-tolerance-config '{"external_fault_notify_port":'$FAULT_PORT',"engine_recovery_timeout_sec":'$RECOVERY_TIMEOUT'}' \
    --api-server-count 1 \
    --trust-remote-code \
    --gloo-timeout-seconds "$GLOO_TIMEOUT" \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --additional-config '{"eplb_config":{"dynamic_eplb": false, "num_redundant_experts":'${REDUNDANT_EXPERTS}'}}' \
    --quantization ascend \
    --host "$HOST" \
    --port "$PORT" \
    --max-cudagraph-capture-size 512 \
    --max-num-batched-tokens 512 \
    $DP_MULTI_NODE_ARGS

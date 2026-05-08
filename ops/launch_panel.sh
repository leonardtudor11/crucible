#!/usr/bin/env bash
# Bring up the Crucible 7-container model panel from scratch on the
# MI300X droplet (6 vLLMs + LiteLLM proxy). Idempotent — safe to re-run.
#
# Run from your laptop:  bash ops/launch_panel.sh [<droplet-ip>]
set -e

DROPLET_IP=${1:-129.212.181.126}

ssh_run() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$DROPLET_IP" "$@"
}

# Heredoc on the remote — each container launched with bounded GPU mem,
# port bound to 127.0.0.1 only.
ssh_run 'bash -s' <<'REMOTE_EOF'
set -e

echo "=== ensure /shared-docker/litellm/config.yaml exists ==="
mkdir -p /shared-docker/litellm /shared-docker/hf-cache

if [ ! -f /shared-docker/litellm/config.yaml ]; then
  cat > /shared-docker/litellm/config.yaml <<'CONFIG'
model_list:
  - model_name: Qwen/Qwen2.5-7B-Instruct
    litellm_params: { model: openai/Qwen/Qwen2.5-7B-Instruct, api_base: http://127.0.0.1:8001/v1, api_key: dummy }
  - model_name: microsoft/Phi-3.5-mini-instruct
    litellm_params: { model: openai/microsoft/Phi-3.5-mini-instruct, api_base: http://127.0.0.1:8002/v1, api_key: dummy }
  - model_name: tiiuae/Falcon3-7B-Instruct
    litellm_params: { model: openai/tiiuae/Falcon3-7B-Instruct, api_base: http://127.0.0.1:8003/v1, api_key: dummy }
  - model_name: NousResearch/Hermes-3-Llama-3.1-8B
    litellm_params: { model: openai/NousResearch/Hermes-3-Llama-3.1-8B, api_base: http://127.0.0.1:8004/v1, api_key: dummy }
  - model_name: internlm/internlm2_5-7b-chat
    litellm_params: { model: openai/internlm/internlm2_5-7b-chat, api_base: http://127.0.0.1:8005/v1, api_key: dummy }
  - model_name: 01-ai/Yi-1.5-9B-Chat
    litellm_params: { model: openai/01-ai/Yi-1.5-9B-Chat, api_base: http://127.0.0.1:8006/v1, api_key: dummy }
litellm_settings:
  drop_params: true
  request_timeout: 120
general_settings:
  master_key: sk-crucible-local
CONFIG
fi

echo "=== removing any stale containers ==="
for n in vllm_qwen vllm_phi vllm_falcon vllm_hermes vllm_internlm vllm_yi litellm; do
  docker stop "$n" 2>/dev/null || true
  docker rm "$n" 2>/dev/null || true
done

VLLM_FLAGS="--gpu-memory-utilization 0.13 --dtype float16 --enforce-eager --host 0.0.0.0 --port 8000 --max-num-seqs 8"

launch_vllm() {
  local name=$1 port=$2 model=$3 ctx=$4 extra=$5
  echo "  -> $name :$port  $model"
  docker run -d \
    --name "vllm_$name" --restart unless-stopped \
    --device=/dev/kfd --device=/dev/dri --group-add video --ipc=host --shm-size=8g \
    -p 127.0.0.1:$port:8000 \
    -v /shared-docker:/shared-docker \
    -v /shared-docker/hf-cache:/root/.cache/huggingface \
    rocm \
    bash -c "vllm serve $model --max-model-len $ctx $VLLM_FLAGS $extra" > /dev/null
}

echo "=== launching 6 vLLM containers ==="
launch_vllm qwen     8001 "Qwen/Qwen2.5-7B-Instruct"            32768 ""
launch_vllm phi      8002 "microsoft/Phi-3.5-mini-instruct"     32768 "--trust-remote-code"
launch_vllm falcon   8003 "tiiuae/Falcon3-7B-Instruct"          32768 ""
launch_vllm hermes   8004 "NousResearch/Hermes-3-Llama-3.1-8B"  32768 ""
launch_vllm internlm 8005 "internlm/internlm2_5-7b-chat"        32768 "--trust-remote-code"
launch_vllm yi       8006 "01-ai/Yi-1.5-9B-Chat"                4096 ""

echo "=== launching LiteLLM proxy ==="
docker run -d \
  --name litellm --restart unless-stopped --network host \
  -v /shared-docker/litellm:/app/config \
  ghcr.io/berriai/litellm:main-stable \
  --config /app/config/config.yaml --port 8000 --host 127.0.0.1 > /dev/null

echo
echo "=== polling endpoints (up to 5 min) ==="
declare -A SVCS=( [qwen]=8001 [phi]=8002 [falcon]=8003 [hermes]=8004 [internlm]=8005 [yi]=8006 )
declare -A READY
START=$(date +%s)
for round in $(seq 1 60); do
  ALL=1
  for n in "${!SVCS[@]}"; do
    [ "${READY[$n]:-0}" = "1" ] && continue
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:${SVCS[$n]}/v1/models 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
      READY[$n]=1
      echo "  +$(($(date +%s)-START))s  $n ready"
    else
      ALL=0
    fi
  done
  [ "$ALL" = "1" ] && break
  sleep 5
done

echo
echo "=== final status ==="
for n in "${!SVCS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:${SVCS[$n]}/v1/models 2>/dev/null || echo 000)
  printf "  %-12s :%s  http=%s\n" "$n" "${SVCS[$n]}" "$code"
done
LITELLM_CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-crucible-local")
echo "  litellm     :8000 http=$LITELLM_CODE"
REMOTE_EOF

echo
echo "=== done ==="
echo "next: from your laptop run \`ssh -L 8000:localhost:8000 -N root@$DROPLET_IP &\` to (re)open the tunnel"

#!/usr/bin/env bash
# Verify the Crucible model panel on the MI300X droplet is healthy.
# Run from your laptop:  bash ops/check_panel.sh [<droplet-ip>]
set -e

DROPLET_IP=${1:-129.212.181.126}

ssh_run() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$DROPLET_IP" "$@"
}

echo "=== ssh ==="
if ! ssh_run "echo ok" >/dev/null 2>&1; then
  echo "  FAIL: cannot ssh to root@$DROPLET_IP"
  exit 1
fi
echo "  ok"

echo
echo "=== docker containers ==="
ssh_run "docker ps --format '{{.Names}}\t{{.Status}}' | sort" || true

echo
echo "=== vllm endpoints (each model loaded) ==="
ssh_run '
SVCS=( vllm_qwen:8001 vllm_phi:8002 vllm_falcon:8003 vllm_hermes:8004 vllm_internlm:8005 vllm_yi:8006 )
for s in "${SVCS[@]}"; do
  name=${s%%:*}
  port=${s##*:}
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 4 http://127.0.0.1:$port/v1/models 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then
    model=$(curl -s -m 3 http://127.0.0.1:$port/v1/models | python3 -c "import json,sys; print(json.load(sys.stdin)[\"data\"][0][\"id\"])" 2>/dev/null || echo "?")
    echo "  ok  $name :$port -> $model"
  else
    echo "  DOWN $name :$port (http=$code)"
  fi
done'

echo
echo "=== litellm proxy (port 8000) ==="
LITELLM_OUT=$(ssh_run 'curl -s -m 5 http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-crucible-local"' 2>/dev/null || echo "")
if echo "$LITELLM_OUT" | grep -q '"data"'; then
  echo "$LITELLM_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  ok  routing {len(d[\"data\"])} models'); [print(f'      - {m[\"id\"]}') for m in d['data']]"
else
  echo "  DOWN litellm not responding"
fi

echo
echo "=== gpu memory ==="
ssh_run 'rocm-smi --showmeminfo vram --json 2>/dev/null' | python3 -c "
import json, sys
d = json.load(sys.stdin)
g = next(iter(d.values()))
used = int(g.get('VRAM Total Used Memory (B)', 0))
total = int(g.get('VRAM Total Memory (B)', 1))
print(f'  {used/1e9:.1f}/{total/1e9:.1f} GB ({used/total*100:.0f}%)')
" 2>/dev/null || echo "  (snapshot failed)"

echo
echo "=== mac → droplet tunnel (8000) ==="
TUNNEL_CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 5 http://localhost:8000/v1/models -H "Authorization: Bearer sk-crucible-local" 2>/dev/null || echo "000")
if [ "$TUNNEL_CODE" = "200" ]; then
  echo "  ok  tunnel up (Mac:8000 → droplet:8000)"
else
  echo "  DOWN tunnel returned http=$TUNNEL_CODE"
  echo "  (re-establish: ssh -L 8000:localhost:8000 -N root@$DROPLET_IP &)"
fi

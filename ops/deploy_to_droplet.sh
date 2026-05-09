#!/usr/bin/env bash
# Deploy Streamlit + Cloudflare Tunnel on a fresh MI300X droplet so the
# dashboard is publicly reachable as a demo URL. Run AFTER
# ops/launch_panel.sh has the 6 vLLMs + LiteLLM proxy up.
#
# This captures what we did manually for the hackathon submission so the
# whole stack can be rebuilt from this repo without a DO snapshot.
#
# Run from your laptop:  bash ops/deploy_to_droplet.sh [<droplet-ip>]
# After it finishes, the public Cloudflare URL is logged on the droplet
# at: journalctl -u crucible-tunnel.service | grep trycloudflare

set -e

DROPLET_IP=${1:-129.212.181.126}

ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$DROPLET_IP" 'bash -s' <<'REMOTE_EOF'
set -e

echo "=== installing python3-venv + cloudflared ==="
apt-get update -qq 2>&1 | tail -1
apt-get install -y -qq python3-venv 2>&1 | tail -2
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi
cloudflared --version 2>&1 | head -1

echo
echo "=== cloning crucible to /opt/crucible ==="
if [ ! -d /opt/crucible/.git ]; then
  rm -rf /opt/crucible
  git clone --quiet https://github.com/leonardtudor11/crucible /opt/crucible
else
  cd /opt/crucible && git pull --quiet
fi
cd /opt/crucible

echo
echo "=== creating venv + installing crucible ==="
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi
.venv/bin/pip install --quiet -e . 2>&1 | tail -3

echo
echo "=== writing .env (LiteLLM is local on this droplet) ==="
cat > /opt/crucible/.env <<EOF
CRUCIBLE_API_BASE=http://127.0.0.1:8000/v1
CRUCIBLE_API_KEY=sk-crucible-local
CRUCIBLE_DEFAULT_MODEL=Qwen/Qwen2.5-7B-Instruct
CRUCIBLE_SYNTHESIZER_MODEL=Qwen/Qwen2.5-7B-Instruct
CRUCIBLE_TIMEOUT_SECONDS=120
CRUCIBLE_MAX_RETRIES=2
EOF

echo
echo "=== writing systemd units ==="
cat > /etc/systemd/system/crucible-streamlit.service <<UNIT
[Unit]
Description=Crucible Streamlit dashboard
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=/opt/crucible
ExecStart=/opt/crucible/.venv/bin/streamlit run src/crucible/ui/app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/crucible-tunnel.service <<UNIT
[Unit]
Description=Cloudflare quick tunnel for Crucible (public HTTPS demo URL)
After=network.target crucible-streamlit.service

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8501
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

echo
echo "=== enabling + starting both services ==="
systemctl daemon-reload
systemctl enable --now crucible-streamlit.service
sleep 5
systemctl enable --now crucible-tunnel.service
sleep 8

echo
echo "=== final status ==="
systemctl is-active crucible-streamlit.service && echo "  streamlit: active"
systemctl is-active crucible-tunnel.service && echo "  tunnel: active"

echo
echo "=== public URL (paste into hackathon form) ==="
journalctl -u crucible-tunnel.service --no-pager -n 30 --output=cat 2>&1 \
  | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1 \
  || echo "  (URL not yet logged — wait 10s and re-run: journalctl -u crucible-tunnel.service | grep trycloudflare)"
REMOTE_EOF

echo
echo "=== done ==="
echo "Full rebuild from scratch on a fresh droplet:"
echo "  1. ops/launch_panel.sh <new-droplet-ip>     # 6 vLLMs + LiteLLM"
echo "  2. ops/deploy_to_droplet.sh <new-droplet-ip># Streamlit + tunnel + systemd"
echo "  Total time: ~30 min (most of it is HF model downloads)"

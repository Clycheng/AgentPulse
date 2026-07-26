#!/usr/bin/env bash
# AgentPulse 一键启动脚本
# 启动: PostgreSQL → API → Desktop
# 用法: npm run dev

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$ROOT_DIR/services/api"
LOG_DIR="$ROOT_DIR/.dev-logs"

# The one-command development stack is PostgreSQL + real Hermes by contract.
# Explicit caller-provided values still win for isolated or fail-closed runs.
export AGENTPULSE_DATABASE_URL="${AGENTPULSE_DATABASE_URL:-postgresql://agentpulse:agentpulse@127.0.0.1:55432/agentpulse}"
export AGENTPULSE_HERMES_PROVISIONING="${AGENTPULSE_HERMES_PROVISIONING:-true}"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${CYAN}[dev]${NC} $*"; }
ok()  { echo -e "${GREEN}[ok]${NC} $*"; }
warn(){ echo -e "${YELLOW}[warn]${NC} $*"; }

cleanup() {
    log "正在停止所有服务..."
    jobs -p | xargs -r kill 2>/dev/null || true
    wait 2>/dev/null || true
    log "已停止"
}
trap cleanup EXIT INT TERM

mkdir -p "$LOG_DIR"

# ── 1. PostgreSQL ──
log "启动 PostgreSQL..."
cd "$ROOT_DIR"
if docker compose ps -q postgres 2>/dev/null | head -1 | grep -q .; then
    ok "PostgreSQL 已在运行"
else
    docker compose up -d
    log "等待 PostgreSQL 就绪..."
    for i in $(seq 1 30); do
        if docker compose exec -T postgres pg_isready -U agentpulse -d agentpulse &>/dev/null; then
            ok "PostgreSQL 就绪"
            break
        fi
        sleep 1
    done
fi

# ── 2. 后端 API ──
log "启动后端 API (http://localhost:8000)..."
cd "$API_DIR"
if [ -f ".venv/bin/uvicorn" ]; then
    .venv/bin/uvicorn app.main:app --reload --port 8000 &> "$LOG_DIR/api.log" &
else
    uvicorn app.main:app --reload --port 8000 &> "$LOG_DIR/api.log" &
fi
API_PID=$!

# 等待 API 就绪
log "等待 API 就绪..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/docs &>/dev/null; then
        ok "API 就绪 (http://localhost:8000)"
        break
    fi
    if [ $i -eq 30 ]; then
        warn "API 30s 内未就绪，继续启动前端（可能需要检查 $LOG_DIR/api.log）"
    fi
    sleep 1
done

# ── 3. 前端 Desktop ──
log "启动 Desktop 前端 (http://localhost:5174)..."
cd "$ROOT_DIR"
npm run dev:desktop &> "$LOG_DIR/desktop.log" &
DESKTOP_PID=$!

echo ""
ok "==========================================="
ok "  AgentPulse 开发环境已启动！"
ok "==========================================="
ok "  API:      http://localhost:8000"
ok "  API Docs: http://localhost:8000/docs"
ok "  Desktop:  http://localhost:5174"
ok "  日志目录:  $LOG_DIR/"
ok "==========================================="
echo ""
log "按 Ctrl+C 停止所有服务"

wait

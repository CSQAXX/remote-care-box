#!/usr/bin/env bash
# =============================================================================
# 养老盒子 · AI 网关 —— 服务器部署脚本 (幂等, 可重复执行)
#
# 【隔离红线】本脚本只碰 /kanhu-ai/ 一个目录:
#   - 不动 /jiuzuo/ 下任何文件
#   - 不动 jiuzuo-backend / jiuzuo-mongo / zhigou-* 任何容器
#   - 不动宝塔 Nginx 任何配置
#   - 不动宿主机已有 27017 / 3306 端口
#
# 用法 (在服务器上):
#   cd /kanhu-ai && bash deploy.sh
#
# 密钥从 .env 读, 不写进镜像, 不进 git。
# =============================================================================
set -euo pipefail

ROOT=/kanhu-ai
cd "$ROOT"

echo "============================================================"
echo "  养老盒子 AI 网关部署"
echo "  目录 $ROOT  (与 /jiuzuo 完全隔离)"
echo "============================================================"

# --- 0) 前置检查 ------------------------------------------------------------
echo "[0/6] 前置检查"
command -v docker >/dev/null || { echo "  [错误] 没装 docker"; exit 1; }
docker compose version >/dev/null || { echo "  [错误] 没有 compose v2"; exit 1; }
if [ ! -f .env ]; then
    echo "  [错误] 缺少 .env (至少要有 KANHU_DEVICES 和 LLM key)"
    echo "         可从 .env.example 复制:  cp .env.example .env && vi .env"
    exit 1
fi
echo "  docker $(docker --version | cut -d' ' -f3)  ok"

# --- 1) 备份现有配置 (只备份自己) -------------------------------------------
echo "[1/6] 备份本项目配置"
mkdir -p "$ROOT/backups"
TS=$(date +%Y%m%d_%H%M%S)
if [ -f .env ] || [ -f docker-compose.yml ]; then
    tar -czf "$ROOT/backups/config_$TS.tar.gz" \
        -C "$ROOT" .env docker-compose.yml Dockerfile app.py requirements.txt 2>/dev/null || true
    ls -t "$ROOT"/backups/config_*.tar.gz | tail -n +8 | xargs -r rm --
    echo "  备份 -> backups/config_$TS.tar.gz"
fi

# --- 2) 拉起 ----------------------------------------------------------------
echo "[2/6] 构建并启动"
# 显式指定项目名, 绝不依赖目录名推导, 避免和 jiuzuo 撞项目名
docker compose -p kanhu-ai up -d --build

# --- 3) 等健康 --------------------------------------------------------------
echo "[3/6] 等待健康检查"
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:18000/v1/health >/dev/null 2>&1; then
        echo "  网关已就绪 (第 ${i}0 秒内)"
        break
    fi
    [ "$i" = "30" ] && { echo "  [错误] 60 秒还没就绪, 看日志:"; docker compose -p kanhu-ai logs --tail=40; exit 1; }
    sleep 2
done

# --- 4) 验证隔离 ------------------------------------------------------------
echo "[4/6] 验证没有碰到现有服务"
echo "  本项目容器:"
docker compose -p kanhu-ai ps --format '    {{.Name}}\t{{.Status}}\t{{.Ports}}'
echo "  现有容器 (应全部保持 Up, 无重建):"
docker ps --format '    {{.Names}}\t{{.Status}}' | grep -v '^    kanhu-ai' || true

# --- 5) 端口确认 ------------------------------------------------------------
echo "[5/6] 端口占用确认"
ss -lntp 2>/dev/null | grep -E ':(18000|27018)' || echo "  (还没监听, 稍等)"

# --- 6) 健康详情 ------------------------------------------------------------
echo "[6/6] 健康详情"
curl -fsS http://127.0.0.1:18000/v1/health | python -m json.tool 2>/dev/null || \
    curl -fsS http://127.0.0.1:18000/v1/health

echo ""
echo "============================================================"
echo "  部署完成"
echo "  对外地址: http://175.24.204.75:18000"
echo "  自测:     curl http://127.0.0.1:18000/v1/health"
echo "  日志:     cd $ROOT && docker compose -p kanhu-ai logs -f gateway"
echo "============================================================"

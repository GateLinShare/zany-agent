#!/usr/bin/env bash
# gateway 启停: ./gateway/run.sh start|stop|restart|status
# pid 存 gateway/.gateway.pid; 后台运行, 日志追加 gateway/.gateway.log
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE="gateway/.gateway.pid"
LOG_FILE="gateway/.gateway.log"

# 身份只认本仓库 .env: 从 agent 会话里启动别的 checkout 的 gateway, 会继承父 gateway 灌进环境的
# FEISHU_*(agent.py 是 setdefault 载入, 环境变量优先), 后果是本网关顶着别家的 app 身份连飞书
unset FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_ALLOWED_USERS FEISHU_BOT_OPEN_ID FEISHU_HOME_CHANNEL GATEWAY_WORKDIR

case "${1:-}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行: pid $(cat "$PID_FILE")"; exit 0
    fi
    setsid nohup uv run python gateway/main.py >>"$LOG_FILE" 2>&1 &  # 自成进程组, stop 才能杀干净(uv→python 子进程树)
    echo $! > "$PID_FILE"
    echo "started: pid $(cat "$PID_FILE"), 日志 $LOG_FILE"
    ;;
  stop)
    [ -f "$PID_FILE" ] || { echo "没在运行"; exit 0; }
    PGID=$(cat "$PID_FILE")
    kill -- -"$PGID" 2>/dev/null || kill "$PGID" 2>/dev/null || true
    for _ in $(seq 1 35); do  # 等真死(优雅停最久 30s); 垂死进程的 flock 会拦死紧跟着的 start
      kill -0 -"$PGID" 2>/dev/null || break
      sleep 1
    done
    kill -0 -"$PGID" 2>/dev/null && kill -9 -- -"$PGID" 2>/dev/null  # 优雅停超时: 强杀兜底
    rm -f "$PID_FILE"
    echo "stopped"
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running: pid $(cat "$PID_FILE")"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "用法: $0 start|stop|restart|status"; exit 1
    ;;
esac

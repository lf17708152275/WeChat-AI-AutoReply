#!/usr/bin/env bash
# 在终端里实时看 bot 日志，不用开 GUI。
#   bash scripts/watch_log.sh            # 跟随最新日志（彩色）
#   bash scripts/watch_log.sh -e         # 只看告警/错误
#   bash scripts/watch_log.sh -n 300     # 从最后 300 行开始
#   Ctrl+C 退出
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="logs/wechat_bot_dev.log"
TAIL_N=60
MODE="all"
while [ $# -gt 0 ]; do
  case "$1" in
    -e|--errors) MODE="errors"; shift ;;
    -n) TAIL_N="${2:-100}"; shift 2 ;;
    -f|--file) LOG="${2:-$LOG}"; shift 2 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（-h 看帮助）" >&2; exit 2 ;;
  esac
done

[ -f "$LOG" ] || { echo "日志文件不存在: $LOG（bot 还没启动过？）" >&2; exit 1; }

echo "== 跟随日志: $LOG （Ctrl+C 退出） =="

if [ "$MODE" = "errors" ]; then
  tail -n "$TAIL_N" -F "$LOG" 2>/dev/null \
    | grep --line-buffered -E "⚠️|🚫|❌|错误|失败|异常|熔断|脱靶|降级|超时"
else
  tail -n "$TAIL_N" -F "$LOG" 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      *⚠️*|*🚫*|*❌*|*错误*|*失败*|*异常*|*熔断*|*脱靶*) printf '\033[31m%s\033[0m\n' "$line" ;;
      *✅*|*成功*)                                     printf '\033[32m%s\033[0m\n' "$line" ;;
      *🎤*)                                            printf '\033[33m%s\033[0m\n' "$line" ;;
      *💬*|*🧠*|*📩*|*Agent*)                          printf '\033[36m%s\033[0m\n' "$line" ;;
      *🔍*|*⏭️*|*♻️*)                                   printf '\033[35m%s\033[0m\n' "$line" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done
fi

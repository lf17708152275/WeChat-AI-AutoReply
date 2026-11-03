#!/usr/bin/env bash
# Linux 启动脚本（X11）。用法：
#   ./start_linux.sh            # 启动 AI 守护进程（后台回复）
#   ./start_linux.sh gui        # 启动 Tk 控制中心
#
# 说明：
#   DISPLAY 指向 xrdp 那块真实 Xorg 屏（本机是 :10），不是 xrdp 自己起的 :0。
set -euo pipefail
cd "$(dirname "$0")"

export DISPLAY="${DISPLAY:-:10}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# 自动挑一个能连上的 X 显示：
# 本机 :0 是 lightdm 本地 seat（没有 cookie 连不上），微信实际跑在 xrdp 的 :10。
pick_display() {
    local cand
    for cand in "${DISPLAY:-}" ":10" ":0"; do
        [ -n "$cand" ] || continue
        if DISPLAY="$cand" XAUTHORITY="$XAUTHORITY" \
           python3 -c "from Xlib import display; display.Display()" >/dev/null 2>&1; then
            printf '%s' "$cand"
            return 0
        fi
    done
    printf '%s' ":10"
}

# pip 装到用户目录（本机没有系统 pip / 没有 sudo）
export PYTHONPATH="$HOME/.local/lib/python3.13/site-packages:${PYTHONPATH:-}"

# tkinter 兜底：系统没装 python3-tk 时，用解包出来的 deb 目录
TKROOT="$HOME/.local/tkroot"
if [ -d "$TKROOT/usr/lib/python3.13/tkinter" ]; then
    export PYTHONPATH="$TKROOT/usr/lib/python3.13:$TKROOT/usr/lib/python3.13/lib-dynload:$PYTHONPATH"
    export LD_LIBRARY_PATH="$TKROOT/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
    export TCL_LIBRARY="$TKROOT/usr/share/tcltk/tcl8.6"
    export TK_LIBRARY="$TKROOT/usr/share/tcltk/tk8.6"
fi

# 运行期目录（dispatcher 需要 DISPLAY 才能抓图）
export DISPLAY="$(pick_display)"
echo "▶ DISPLAY=$DISPLAY" >&2

mode="${1:-bot}"
case "$mode" in
  gui)
    exec python3 src/wechat_bot_gui_dev.py
    ;;
  bot|*)
    exec python3 src/wechat_ai_bot_dev.py
    ;;
esac

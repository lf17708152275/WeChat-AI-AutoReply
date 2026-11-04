#!/usr/bin/env bash
# 启动 Linux 微信：强制软件渲染，避免在 xrdp / 无 GPU 的 Xorg 上
# Qt 初始化 GPU 失败后主窗口“mapped 但不重绘”（截图全是别的窗口/旧像素）。
#
# 用法：
#   ./scripts/launch_wechat_softgl.sh [微信额外参数]
# 也可以把 Exec= 改成这个脚本，让桌面快捷方式/自启动都用软件渲染。
set -euo pipefail

export LIBGL_ALWAYS_SOFTWARE=1
export QT_OPENGL=software
export QT_QUICK_BACKEND=software
export QSG_RENDER_LOOP=basic
export QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --disable-gpu-compositing"

exec /usr/bin/wechat --ozone-platform=x11 "$@"

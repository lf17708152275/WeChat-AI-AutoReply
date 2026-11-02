#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Linux（X11）后端：把 wechat_ai_bot_dev.py 里那 12 个 Win32 函数换成 X11/XTest 实现。

设计原则
--------
1. 只做 I/O，不含业务判断 —— 业务逻辑（OCR 解析、名字匹配、AI 回复）留在主程序里。
2. 与 Windows 侧保持同样的"契约"：
   - 窗口句柄 = X11 window id（int）
   - rect 有 .left/.top/.right/.bottom（整窗坐标，含边框）
   - 抓图返回 (PIL.Image, rect, scale)，图像坐标与窗口坐标 1:1（本机 96dpi 实测）
3. 依赖：python-xlib、Pillow、python3-gi（GTK3 剪贴板）、RapidOCR（主程序自带）。

踩坑记录（都是实机踩出来的）
--------------------------
- `xwd -id` / `window.get_image()` 在 Qt+xrdp 下会拿到过期画面，必须抓 root 再裁剪。
- XTest 逐键打 ASCII 会被 fcitx5 吞成候选词，中文一律走剪贴板 + Ctrl+V。
- 一次性 GTK 进程持有剪贴板时要等它退出（8s+），改成常驻守护后 0.2s。
- 窗口会被用户/WM 拖动，任何点击前都必须重新取几何。
"""
import os
import re
import subprocess
import sys
import threading
import time

from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest

# ----------------------------------------------------------------------------
# X11 连接（懒加载单例：Display 对象不是线程安全的，统一加锁）
# ----------------------------------------------------------------------------
_LOCK = threading.RLock()
_DISPLAY = None

CLIP_FILE = "/tmp/wx_bot_clip.txt"

CLIPD_SNIPPET = r"""
import os, sys, gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk, Gdk, GLib
path = sys.argv[1]
state = {"last": None}
cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

def tick():
    # 主程序退出后（被 init 收养）自动收摊，避免守护进程越积越多
    if os.getppid() == 1:
        Gtk.main_quit()
        return False
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        txt = state["last"] or ""
    if txt != state["last"]:
        state["last"] = txt
        cb.set_text(txt, -1)
        cb.store()
    return True

tick()
GLib.timeout_add(120, tick)
Gtk.main()
"""


def dpy():
    """取全局 Display 连接。"""
    global _DISPLAY
    with _LOCK:
        if _DISPLAY is None:
            _DISPLAY = display.Display()
        return _DISPLAY


def screen():
    return dpy().screen()


class Rect:
    """与 wintypes.RECT 同形状。"""

    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left, top, right, bottom):
        self.left = int(left)
        self.top = int(top)
        self.right = int(right)
        self.bottom = int(bottom)

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def __repr__(self):
        return f"Rect({self.left},{self.top},{self.right},{self.bottom})"


def _walk(w, depth=0, max_depth=4):
    """递归遍历窗树（微信主窗被 WM 重父化，不一定在 root 直接子窗里）。"""
    if depth > max_depth:
        return
    try:
        children = w.query_tree().children
    except Exception:
        return
    for c in children:
        yield c
        yield from _walk(c, depth + 1, max_depth)


def _wm_class(w):
    try:
        cls = w.get_wm_class()
    except Exception:
        return ()
    return cls or ()


def _is_viewable(w):
    try:
        return w.get_attributes().map_state == X.IsViewable
    except Exception:
        return False


def window_rect(wid):
    """窗口在屏幕上的绝对矩形（含窗口管理器边框的话按客户端区域算）。"""
    d = dpy()
    w = d.create_resource_object("window", int(wid))
    t = w.translate_coords(d.screen().root, 0, 0)
    g = w.get_geometry()
    x, y = -t.x, -t.y
    return Rect(x, y, x + g.width, y + g.height)


def _wechat_windows():
    """所有 class 里带 wechat 的可见窗（主窗 + 搜索弹出窗 + 1x1 隐藏辅助窗）。"""
    out = []
    for w in _walk(dpy().screen().root):
        cls = _wm_class(w)
        if not cls:
            continue
        if not any("wechat" in str(c).lower() for c in cls if c):
            continue
        try:
            g = w.get_geometry()
        except Exception:
            continue
        out.append((w, g.width, g.height, _is_viewable(w)))
    return out


def find_wechat_window():
    """找微信主窗，返回 (wid, ver)。ver=4 表示 4.x（Qt）。"""
    best = None
    for w, width, height, vis in _wechat_windows():
        if not vis or width <= 1 or height <= 1:
            continue
        area = width * height
        if best is None or area > best[0]:
            best = (area, w.id)
    if best is None:
        return None, None
    return best[1], 4


def find_popup(main_wid, timeout=1.2, min_w=200, min_h=80):
    """找搜索结果的独立弹出窗（Linux 4.1.26 实测确实是独立顶层窗）。

    与主窗同 class=wechat，但面积更小；排除 1x1 辅助窗。
    """
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        best = None
        for w, width, height, vis in _wechat_windows():
            if w.id == int(main_wid) or not vis:
                continue
            if width < min_w or height < min_h:
                continue
            area = width * height
            if best is None or area > best[0]:
                best = (area, w.id)
        if best:
            return best[1]
        if time.time() >= deadline:
            return None
        time.sleep(0.10)


def is_viewable(wid):
    d = dpy()
    return _is_viewable(d.create_resource_object("window", int(wid)))


# ----------------------------------------------------------------------------
# 抓图：root 抓一次再裁（window.get_image 在 Qt+xrdp 下会拿到过期内容）
# ----------------------------------------------------------------------------
def stacked_above(wid):
    """返回压在 wid 之上的可见窗口 [(wid, Rect, class)]，用于遮挡判定。

    依据 _NET_CLIENT_LIST_STACKING（自底向上），取排在 wid 之后的部分。
    """
    d = dpy()
    root = d.screen().root
    stack = []
    try:
        prop = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST_STACKING"),
                                      X.AnyPropertyType)
        if prop and prop.value:
            stack = [int(v) for v in prop.value]
    except Exception:
        stack = []
    if not stack:
        return []
    try:
        idx = stack.index(int(wid))
    except ValueError:
        return []

    out = []
    for w in stack[idx + 1:]:
        try:
            win = d.create_resource_object("window", w)
            attrs = win.get_attributes()
            if attrs.map_state != X.IsViewable:
                continue
            try:
                cls = (win.get_wm_class() or ("", ""))[1] or ""
            except Exception:
                cls = ""
            # 微信自己的弹出层（同一 class）不算遮挡
            if cls == "wechat":
                continue
            r = window_rect(w)
            if r.width < 60 or r.height < 60:
                continue
            out.append((w, r, cls))
        except Exception:
            continue
    return out


def _overlap_ratio(rect, other):
    x0 = max(rect.left, other.left)
    y0 = max(rect.top, other.top)
    x1 = min(rect.right, other.right)
    y1 = min(rect.bottom, other.bottom)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area = max(1, rect.width * rect.height)
    return inter / float(area)


def occlusion_ratio(wid, rect=None, threshold=0.02):
    """被别的窗口挡住了多少面积（0~1）。"""
    rect = rect or window_rect(wid)
    total = 0.0
    for _w, r, _cls in stacked_above(wid):
        total += _overlap_ratio(rect, r)
    return min(1.0, total)


def grab_window(wid, retries=2):
    """返回 (PIL.Image, Rect, scale)。scale 恒为 1.0（Linux 下图像即物理像素）。"""
    d = dpy()
    scr = d.screen()
    wid = int(wid)
    if not is_viewable(wid):
        ensure_visible(wid)

    rect = window_rect(wid)
    if rect.width <= 0 or rect.height <= 0:
        return None, rect, 1.0

    # 遮挡判定：其他窗口压在上面时，直接抓图会把别人的界面 OCR 成微信消息
    # （实测被 GUI 挡住时，把人设 Prompt 当成好友发来的消息提交给了 Agent）
    for attempt in range(retries + 1):
        try:
            occl = occlusion_ratio(wid, rect)
            if occl > _OCCL_TOLERATE:
                # 大面积被压住：先把微信抬上来再抓，不然会把别人的界面当成微信内容
                activate(wid)
                time.sleep(0.3)
                rect = window_rect(wid)
                occl = occlusion_ratio(wid, rect)
                if occl > _OCCL_TOLERATE:
                    log_occlusion(wid, occl)
                    return None, rect, 1.0
            raw = scr.root.get_image(0, 0, scr.width_in_pixels, scr.height_in_pixels,
                                     X.ZPixmap, 0xffffffff)
            full = Image.frombytes("RGB", (scr.width_in_pixels, scr.height_in_pixels),
                                   raw.data, "raw", "BGRX")
            x0, y0 = max(0, rect.left), max(0, rect.top)
            x1, y1 = min(full.width, rect.right), min(full.height, rect.bottom)
            if x1 - x0 < 50 or y1 - y0 < 50:
                return None, rect, 1.0
            img = full.crop((x0, y0, x1, y1))
            # 全黑/全白判定：窗口被遮挡或还没画出来
            if attempt < retries and _looks_blank(img):
                time.sleep(0.25)
                activate(wid)
                continue
            return img, rect, 1.0
        except Exception:
            if attempt >= retries:
                return None, rect, 1.0
            time.sleep(0.2)
    return None, rect, 1.0


_LAST_OCCL_LOG = {"v": -1.0}

# 被压住多少算"没法用"：超过就先抬窗口再抓，抬完还压着就本轮跳过。
# 必须卡得很死（0.5%）：只被压住几个百分点时，抓图会把压在上面的终端/编辑器文字
# 一起 OCR 进聊天区，被误判成"我发出的"气泡 -> bot 以为没有待回复消息，永久静默。
_OCCL_TOLERATE = 0.005


def log_occlusion(wid, ratio):
    """遮挡日志：只在比例明显变化时打一条，防止主循环刷屏。"""
    last = _LAST_OCCL_LOG["v"]
    if abs(ratio - last) < 0.15:
        return
    _LAST_OCCL_LOG["v"] = ratio
    try:
        names = ", ".join(sorted({c or "?" for _w, _r, c in stacked_above(wid)}))[:120]
    except Exception:
        names = "?"
    print(f"[遮挡] 微信窗口被遮挡 {ratio*100:.0f}%（{names}），本轮跳过，避免误读别人的界面",
          flush=True)


def _looks_blank(img):
    """粗判空白：抽样像素几乎全同色（被遮挡/未绘制）。"""
    try:
        small = img.resize((32, 24))
        colors = small.getcolors(maxcolors=1024) or []
        if not colors:
            return False
        top = max(colors)[0]
        return top >= 32 * 24 * 0.995
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 窗口激活 / 显示
# ----------------------------------------------------------------------------
def _send_net_active(w):
    d = dpy()
    try:
        from Xlib import protocol
        root = d.screen().root
        atom = d.intern_atom("_NET_ACTIVE_WINDOW")
        ev = protocol.event.ClientMessage(
            window=w, client_type=atom,
            data=(32, [1, X.CurrentTime, 0, 0, 0]),
        )
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.flush()
    except Exception:
        pass


def activate(wid, timeout=0.8):
    """把微信顶到前台并拿到键盘焦点。返回是否成功。"""
    d = dpy()
    wid = int(wid)
    w = d.create_resource_object("window", wid)
    with _LOCK:
        try:
            w.map()
            _send_net_active(w)
            w.configure(stack_mode=X.Above)
            d.set_input_focus(w, X.RevertToParent, X.CurrentTime)
            d.sync()
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if d.get_input_focus().focus.id == wid:
                return True
        except Exception:
            pass
        time.sleep(0.05)
    try:
        return d.get_input_focus().focus.id == wid
    except Exception:
        return False


def ensure_visible(wid, timeout=3.0):
    """最小化/隐藏时把窗口拉回可见（对应 Win32 的 ShowWindow）。"""
    d = dpy()
    wid = int(wid)
    if is_viewable(wid):
        return True
    w = d.create_resource_object("window", wid)
    try:
        w.map()
        _send_net_active(w)
        w.configure(stack_mode=X.Above)
        d.sync()
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_viewable(wid):
            time.sleep(0.15)
            return True
        time.sleep(0.1)
    return is_viewable(wid)


# ----------------------------------------------------------------------------
# 输入注入：XTest（真实输入队列；WM 消息注入在 Qt 上不可靠）
# ----------------------------------------------------------------------------
def click_client(wid, x, y, button=1):
    """点击窗口内坐标（抓图像素坐标 == 窗口内坐标，无边框偏移）。"""
    rect = window_rect(wid)
    ax, ay = rect.left + int(x), rect.top + int(y)
    d = dpy()
    with _LOCK:
        xtest.fake_input(d, X.MotionNotify, x=ax, y=ay)
        d.sync()
        time.sleep(0.05)
        xtest.fake_input(d, X.ButtonPress, button)
        d.sync()
        time.sleep(0.06)
        xtest.fake_input(d, X.ButtonRelease, button)
        d.sync()
        time.sleep(0.05)


# Windows VK → X11 keysym 名
_VK_MAP = {
    0x08: "BackSpace", 0x09: "Tab", 0x0D: "Return", 0x1B: "Escape",
    0x11: "Control_L", 0x10: "Shift_L", 0x12: "Alt_L", 0x2E: "Delete",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x24: "Home", 0x23: "End", 0x21: "Page_Up", 0x22: "Page_Down",
}
for _c in range(0x30, 0x3A):        # 0-9
    _VK_MAP[_c] = chr(_c)
for _c in range(0x41, 0x5B):        # A-Z
    _VK_MAP[_c] = chr(_c).lower()
_VK_MAP[0x20] = "space"


def _keycode(name):
    d = dpy()
    ks = XK.string_to_keysym(name)
    if not ks:
        return None
    return d.keysym_to_keycode(ks) or None


def key(vk, ctrl=False, ctrl_name="Control_L"):
    """按一次键（vk 用 Windows 虚拟键码，方便和主程序原样对接）。"""
    name = _VK_MAP.get(int(vk))
    if not name:
        return False
    d = dpy()
    kc = _keycode(name)
    ctrl_kc = _keycode(ctrl_name)
    if not kc:
        return False
    with _LOCK:
        if ctrl and ctrl_kc:
            xtest.fake_input(d, X.KeyPress, ctrl_kc)
            d.sync()
            time.sleep(0.01)
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        time.sleep(0.02)
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()
        if ctrl and ctrl_kc:
            time.sleep(0.01)
            xtest.fake_input(d, X.KeyRelease, ctrl_kc)
            d.sync()
        time.sleep(0.02)
    return True


def type_text(text):
    """逐键打 ASCII（有输入法时会被 fcitx 吞掉，中文请用剪贴板）。"""
    d = dpy()
    ok = True
    for ch in str(text):
        name = _VK_MAP.get(ord(ch.upper())) if ch.isalnum() else None
        if ch == " ":
            name = "space"
        if not name:
            ok = False
            continue
        kc = _keycode(name)
        if not kc:
            ok = False
            continue
        with _LOCK:
            xtest.fake_input(d, X.KeyPress, kc)
            xtest.fake_input(d, X.KeyRelease, kc)
            d.sync()
            time.sleep(0.02)
    return ok


# ----------------------------------------------------------------------------
# 剪贴板：常驻 GTK 守护（本机没有 xclip/xsel，pyperclip 也用不了）
# ----------------------------------------------------------------------------
_CLIPD = None


def _ensure_clipd():
    global _CLIPD
    if _CLIPD and _CLIPD.poll() is None:
        return _CLIPD
    if not os.path.exists(CLIP_FILE):
        with open(CLIP_FILE, "w", encoding="utf-8") as f:
            f.write("")
    _CLIPD = subprocess.Popen(
        [sys.executable, "-c", CLIPD_SNIPPET, CLIP_FILE],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.9)          # 等 GTK 起来并抢到 CLIPBOARD 选择权
    return _CLIPD


def clipboard_copy(text):
    """写系统剪贴板。返回是否写入（守护进程存活即视为成功）。"""
    try:
        _ensure_clipd()
        with open(CLIP_FILE, "w", encoding="utf-8") as f:
            f.write(str(text))
        time.sleep(0.22)     # 等守护进程轮询到变化并 set_text
        return True
    except Exception:
        return False


def clipboard_paste():
    """读回剪贴板文本（尽力而为，失败返回空串）。"""
    try:
        with open(CLIP_FILE, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def probe():
    wid, ver = find_wechat_window()
    info = {"wid": hex(wid) if wid else None, "ver": ver}
    if wid:
        info["rect"] = tuple(window_rect(wid).__dict__.values()) if False else (
            window_rect(wid).left, window_rect(wid).top,
            window_rect(wid).width, window_rect(wid).height)
        info["viewable"] = is_viewable(wid)
        info["popup"] = hex(find_popup(wid, timeout=0.0) or 0)
    return info


if __name__ == "__main__":
    import json
    print(json.dumps(probe(), ensure_ascii=False, indent=2))

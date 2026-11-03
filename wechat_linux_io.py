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
import contextlib
import fcntl
import json
import os
import re
import signal
import socket
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

CLIP_IMAGE_FILE = "/tmp/wx_bot_clip_img.json"
CLIPD_SNIPPET = r"""
import os, sys, json, gi
gi.require_version('Gtk','3.0')
gi.require_version('GdkPixbuf','2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
path = sys.argv[1]
img_path = sys.argv[2] if len(sys.argv) > 2 else None
state = {"last": None, "img_nonce": None}
cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

def tick():
    # 主程序退出后（被 init 收养）自动收摊，避免守护进程越积越多
    if os.getppid() == 1:
        Gtk.main_quit()
        return False
    # 图片优先：微信里 Ctrl+V 贴图靠 image 目标
    if img_path:
        try:
            with open(img_path, encoding="utf-8") as f:
                req = json.load(f)
            nonce = req.get("nonce")
            if nonce != state["img_nonce"]:
                state["img_nonce"] = nonce
                pix = GdkPixbuf.Pixbuf.new_from_file(req["path"])
                cb.set_image(pix)
                cb.store()
                return True
        except Exception:
            pass
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
    """窗口在屏幕上的绝对矩形；窗口已销毁/不可探时返回 None。

    弹出窗（搜索框）关闭时机和我们的调用存在竞态：拿到 id 后窗口可能已经没了，
    TranslateCoords 会抛 BadWindow。这里统一吞成 None，让调用方按"窗口没了"处理，
    而不是把异常抛进主循环。
    """
    try:
        d = dpy()
        w = d.create_resource_object("window", int(wid))
        t = w.translate_coords(d.screen().root, 0, 0)
        g = w.get_geometry()
        x, y = -t.x, -t.y
        return Rect(x, y, x + g.width, y + g.height)
    except Exception:
        return None


def is_alive(wid):
    """窗口是否还活着且 mapped（用于搜索弹窗等短命窗口的安全判定）。"""
    try:
        w = dpy().create_resource_object("window", int(wid))
        return w.get_attributes().map_state == X.IsViewable
    except Exception:
        return False


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
            if r is None or r.width < 60 or r.height < 60:
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
    if rect is None:
        return 0.0
    total = 0.0
    for _w, r, _cls in stacked_above(wid):
        total += _overlap_ratio(rect, r)
    return min(1.0, total)


# ----------------------------------------------------------------------------
# 离屏隐藏（XComposite）：把微信窗口转到 off-screen storage
# ----------------------------------------------------------------------------
# 原理：Composite 的 RedirectAutomatic 让窗口内容改画到服务器端的离屏 pixmap，
# 桌面上就看不见它；窗口仍 mapped，所以键盘输入照常，pixmap 也能抓到完整画面。
#
# 硬规则（全是踩出来的）：
#  1) python-xlib 的 Composite 错误路径会抛字段不全的异常并把 Display 连接弄脏，
#     之后任何 sync() 都可能永久阻塞 —— 所以 Composite 调用全部放进独立助手进程。
#  2) redirect / unredirect 必须由**同一个 X 客户端**完成，所以 hide 时启动常驻
#     守护进程（x11_offscreen.py daemon），抓图与召回都通过 Unix socket 回到那个
#     客户端执行。
#  3) GUI 和 bot 是两个进程，可能同时点隐藏/召回，bot 还会在隐藏态临时召回窗口切
#     会话。所有操作都用 /tmp/wx_bot_offscreen.lock 做跨进程 flock 串行化；守护
#     进程失联时一律走「硬恢复」：SIGKILL 掉它（X 服务器随客户端断开自动撤销自动
#     重定向）→ 新连接把窗口 configure 回状态文件里保存的几何位置，绝不把窗口卡在
#     屏幕外。
#  4) 隐藏状态写在 /tmp/wx_bot_offscreen.json，GUI 与 bot 两个进程共享；文件里的
#     pid 用来判断守护进程是否还活着。
_OFFSCREEN_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x11_offscreen.py")
_OFFSCREEN_STATE = os.path.join("/tmp", "wx_bot_offscreen.json")
_OFFSCREEN_SOCKET = os.path.join("/tmp", "wx_bot_offscreen.sock")
_OFFSCREEN_LOG = os.path.join("/tmp", "wx_bot_offscreen.log")
_OFFSCREEN_LOCK = os.path.join("/tmp", "wx_bot_offscreen.lock")
_OFFSCREEN_TIMEOUT = 8.0
_STALE_RECOVER = {}
_STALE_LOCK = threading.Lock()


def _offscreen_state():
    try:
        with open(_OFFSCREEN_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid):
    """进程是否还活着；僵尸（已死未回收）算死亡，否则 SIGKILL 后会白等 2 秒。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            fields = fh.read().split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _send_offscreen(payload, timeout=6.0):
    """给离屏守护进程发一条 JSON 命令：返回 (ok, dict)。"""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(float(timeout))
        try:
            sock.connect(_OFFSCREEN_SOCKET)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        line = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        data = json.loads(line) if line else {}
        if not isinstance(data, dict):
            return False, {"detail": "bad reply"}
        return bool(data.get("ok")), data
    except Exception as exc:
        return False, {"detail": repr(exc)}


def _run_offscreen(*args, timeout=_OFFSCREEN_TIMEOUT):
    """跑一次一次性助手（兜底/硬恢复用）：返回 (ok, detail)。"""
    cmd = [sys.executable, _OFFSCREEN_HELPER, *[str(a) for a in args]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return False, f"helper failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return False, detail
    return True, (proc.stdout or "").strip()


@contextlib.contextmanager
def _offscreen_lock(timeout=12.0):
    """跨进程互斥：GUI 与 bot 可能同时 hide/show，同一时刻只允许一个操作。"""
    fh = None
    acquired = False
    try:
        fh = open(_OFFSCREEN_LOCK, "a+")
        deadline = time.time() + max(1.0, float(timeout))
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if fh is not None:
            try:
                if acquired:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _probe_offscreen(wid):
    """问窗口真实状态（一次性助手，只读、跨客户端安全）：{'viewable','offscreen','rect'}。"""
    ok, detail = _run_offscreen("probe", int(wid), timeout=5.0)
    if not ok:
        return {}
    try:
        data = json.loads(detail or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _visible_again(wid):
    """窗口是否真的回到桌面：可见、且不再停在屏幕外。"""
    info = _probe_offscreen(wid)
    if not info:
        return False
    return bool(info.get("viewable")) and not info.get("offscreen")


def _geom_from_state(wid):
    state = _offscreen_state()
    if int(state.get("wid", -1)) != int(wid):
        return None
    geom = state.get("geom")
    try:
        if geom and len(geom) == 4:
            return [int(v) for v in geom]
    except Exception:
        return None
    return None


def _write_offscreen_state(wid, hidden, geom=None):
    try:
        old = _offscreen_state()
        same = int(old.get("wid", -1)) == int(wid)
        state = {"wid": int(wid), "hidden": bool(hidden)}
        if geom is not None:
            state["geom"] = [int(v) for v in geom]
        elif same and old.get("geom"):
            state["geom"] = old["geom"]
        if hidden:
            # 保留守护进程自己的 pid/socket，is_hidden 才能判断它是否还活着
            for key in ("pid", "socket"):
                if same and old.get(key):
                    state[key] = old[key]
        with open(_OFFSCREEN_STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception:
        pass


def _remove_offscreen_files():
    for path in (_OFFSCREEN_SOCKET, _OFFSCREEN_STATE):
        try:
            os.remove(path)
        except OSError:
            pass


def _kill_daemon(pid):
    """SIGTERM（让它自己还原窗口）等不到就 SIGKILL（X 随客户端断开会撤销重定向）。"""
    try:
        pid = int(pid)
    except Exception:
        return
    if pid == os.getpid() or not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return
    deadline = time.time() + 0.5
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)


def _force_recover_locked(wid, rect=None):
    """硬恢复（调用方必须已持有 _offscreen_lock）：返回恢复后的几何 [l,t,w,h] 或 None。"""
    wid = int(wid)
    state = _offscreen_state()
    if state.get("pid"):
        _kill_daemon(state.get("pid"))
    geom = [int(v) for v in rect] if rect else _geom_from_state(wid)
    _remove_offscreen_files()
    info = _probe_offscreen(wid)
    r = info.get("rect")
    if not geom:
        if r and not info.get("offscreen"):
            geom = [int(v) for v in r]
        elif r:
            geom = [100, 100, int(r[2]), int(r[3])]
        else:
            geom = [100, 100, 1008, 756]
    ok, detail = _run_offscreen("restore", wid, *geom, timeout=8.0)
    _write_offscreen_state(wid, False, geom)
    if not ok:
        print(f"⚠️ [离屏] 硬恢复失败: {detail}", file=sys.stderr)
        return None
    info = _probe_offscreen(wid)
    if not info or not info.get("viewable"):
        print("⚠️ [离屏] 硬恢复后窗口仍不可见", file=sys.stderr)
        return None
    return geom


def force_recover(wid, rect=None):
    """硬恢复窗口（GUI 超时兜底 / 外部修复入口）：返回恢复后的几何或 None。"""
    with _offscreen_lock(12.0) as got:
        if not got:
            return None
        return _force_recover_locked(wid, rect=rect)


def _recover_in_background(wid, reason=""):
    """守护进程失联时后台拉一次硬恢复（节流 3 秒，不阻塞调用方）。"""
    wid = int(wid)
    now = time.time()
    with _STALE_LOCK:
        if now - _STALE_RECOVER.get(wid, 0) < 3.0:
            return
        _STALE_RECOVER[wid] = now

    def _run():
        try:
            if force_recover(wid) is not None and reason:
                print(f"⚠️ [离屏] {reason}，已强制恢复窗口", file=sys.stderr)
        except Exception as exc:
            print(f"⚠️ [离屏] 后台硬恢复失败: {exc!r}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True).start()


def is_hidden(wid):
    """是否处于离屏隐藏（跨进程共享状态文件）。"""
    state = _offscreen_state()
    try:
        if not (state.get("hidden") and int(state.get("wid", -1)) == int(wid)):
            return False
    except Exception:
        return False
    pid = state.get("pid")
    if pid is None or not _pid_alive(pid):
        # 状态说隐藏、但守护进程已经不在了：窗口大概率还停在屏外。
        # 先按隐藏处理（抓图走 pixmap 兜底），同时后台把它拉回桌面。
        _recover_in_background(wid, "守护进程已不在")
        return True
    return True


def hide_window(wid):
    """把窗口转入离屏：桌面看不见、不抢焦点，但 bot 仍能抓图/发消息。"""
    wid = int(wid)
    with _offscreen_lock(12.0) as got:
        if not got:
            print("⚠️ [离屏] 等锁超时，放弃本次隐藏", file=sys.stderr)
            return bool(_offscreen_state().get("hidden"))
        state = _offscreen_state()
        if state.get("hidden") and int(state.get("wid", -1)) == wid:
            ok, _payload = _send_offscreen({"cmd": "ping"}, timeout=2.0)
            if ok:
                return True
            # 状态说隐藏、守护进程却没了：先把窗口救回桌面，再重新隐藏
            _force_recover_locked(wid)
            state = _offscreen_state()
        pid = state.get("pid")
        if pid and _pid_alive(pid):
            _kill_daemon(pid)
        _remove_offscreen_files()
        logf = None
        try:
            logf = open(_OFFSCREEN_LOG, "a", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, _OFFSCREEN_HELPER, "daemon", str(wid)],
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True,
            )
        except Exception as exc:
            print(f"⚠️ [离屏] 启动守护进程失败: {exc!r}", file=sys.stderr)
            return False
        finally:
            if logf is not None:
                try:
                    logf.close()
                except Exception:
                    pass
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if os.path.exists(_OFFSCREEN_SOCKET):
                ok, _payload = _send_offscreen({"cmd": "ping"}, timeout=2.0)
                if ok:
                    return True
            time.sleep(0.1)
        print("⚠️ [离屏] 守护进程启动失败（细节见 /tmp/wx_bot_offscreen.log）",
              file=sys.stderr)
        info = _probe_offscreen(wid)
        return bool(info.get("offscreen"))


def show_window(wid, rect=None):
    """把离屏窗口召回桌面（只信守护进程；失败一律走硬恢复，不做跨客户端 unredirect）。"""
    wid = int(wid)
    with _offscreen_lock(12.0) as got:
        if not got:
            print("⚠️ [离屏] 等锁超时，放弃本次召回", file=sys.stderr)
            return False
        state = _offscreen_state()
        hidden = bool(state.get("hidden")) and int(state.get("wid", -1)) == wid
        if not hidden and _visible_again(wid):
            _write_offscreen_state(wid, False)
            return True
        pid = state.get("pid")
        if hidden and (pid is None or not _pid_alive(pid)):
            print("⚠️ [离屏] 守护进程失联，走硬恢复", file=sys.stderr)
            return bool(_force_recover_locked(wid, rect=rect))
        ok, payload = _send_offscreen({"cmd": "show"}, timeout=5.0)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if _visible_again(wid):
                _write_offscreen_state(wid, False)
                return True
            time.sleep(0.1)
        if ok and not payload.get("detail"):
            _write_offscreen_state(wid, False)
            return True
        print(f"⚠️ [离屏] show 失败，改走硬恢复: {payload.get('detail') or 'daemon 无响应'}",
              file=sys.stderr)
        return bool(_force_recover_locked(wid, rect=rect))


def _grab_hidden(wid):
    """离屏窗口抓图：守护进程读 Composite pixmap 存 PNG；失败时一次性兜底。"""
    wid = int(wid)
    path = os.path.join("/tmp", f"wx_hidden_grab_{wid}.png")
    ok = False
    with _offscreen_lock(8.0) as got:
        if got:
            ok, _payload = _send_offscreen({"cmd": "grab", "path": path}, timeout=6.0)
    if not ok:
        ok, _detail = _run_offscreen("grab", wid, path, timeout=6.0)
    if not ok:
        return None
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGB")
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def set_window_geometry(wid, x, y, width, height):
    """把窗口移动/缩放到指定几何（离屏召回时用）。"""
    try:
        with _LOCK:
            d = dpy()
            w = d.create_resource_object("window", int(wid))
            w.configure(x=int(x), y=int(y), width=int(width), height=int(height))
            d.sync()
        return True
    except Exception:
        return False


def focus_window(wid):
    """不抬窗、不抢 Z-Order，直接把键盘焦点给窗口（离屏发送用）。"""
    try:
        with _LOCK:
            d = dpy()
            w = d.create_resource_object("window", int(wid))
            w.set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()
        return True
    except Exception:
        return False


def grab_window(wid, retries=2):
    """返回 (PIL.Image, Rect, scale)。scale 恒为 1.0（Linux 下图像即物理像素）。"""
    d = dpy()
    scr = d.screen()
    wid = int(wid)
    if is_hidden(wid):
        # 离屏模式：看不见但抓得到，完全不碰窗口层级/焦点。
        # 抓不到说明状态文件过期（窗口其实已经召回）→ 落到普通抓图路径。
        img = _grab_hidden(wid)
        if img is not None:
            return img, window_rect(wid), 1.0
        _write_offscreen_state(wid, False)
    if not is_viewable(wid):
        ensure_visible(wid)

    rect = window_rect(wid)
    if rect is None or rect.width <= 0 or rect.height <= 0:
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
                if rect is None:
                    return None, None, 1.0
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
    """把微信顶到前台并拿到键盘焦点。返回是否成功。

    离屏隐藏时不能抬窗/映射（会把窗口拉回桌面），只把键盘焦点给它。
    """
    if is_hidden(wid):
        return focus_window(wid)
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
    if rect is None:
        return False
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
    return True


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
        [sys.executable, "-c", CLIPD_SNIPPET, CLIP_FILE, CLIP_IMAGE_FILE],
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


def clipboard_copy_image(path):
    """把图片写系统剪贴板（GIF/PNG/JPG，微信里 Ctrl+V 可当图片粘贴）。"""
    try:
        _ensure_clipd()
        with open(CLIP_IMAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"path": os.path.abspath(str(path)), "nonce": time.time()}, f)
        time.sleep(0.35)     # 等守护进程轮询到变化并 set_image
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
        _r = window_rect(wid)
        info["rect"] = None if _r is None else (_r.left, _r.top, _r.width, _r.height)
        info["viewable"] = is_viewable(wid)
        info["popup"] = hex(find_popup(wid, timeout=0.0) or 0)
    return info


if __name__ == "__main__":
    import json
    print(json.dumps(probe(), ensure_ascii=False, indent=2))

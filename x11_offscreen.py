#!/usr/bin/env python3
"""X11 离屏隐藏 / 召唤 / 抓图的常驻助手进程。

为什么要独立进程：python-xlib 的 Composite 请求在异常路径上会抛一个字段不全的
异常对象（`BadRRModeError` 没有 sequence_number），并在共享的 Display 连接上
留下未读的错误包，之后任何 sync() 都会永久阻塞 —— 表现就是 GUI 点「召唤微信回
桌面」直接卡死、bot 隐藏后抓图挂住。所以 Composite 相关的调用只允许发生在助手
进程里，主进程（GUI / bot）只通过 socket 发命令。

为什么必须常驻：Composite 的重定向是"客户端级"的承诺 —— 同一个 X 客户端做完
`RedirectWindow` 之后，必须由**同一个客户端** `UnredirectWindow` 才可靠。之前
hide / show 各起一个一次性子进程，结果 show 永远卡在 unredirect 上（实测 8s 超时
且窗口停留在离屏状态）。所以 hide 时就启动本守护进程，之后抓图、召唤都回到这个
进程里执行。

用法：
  python3 x11_offscreen.py daemon <window-id>   # hide 时由 wechat_linux_io 启动
  python3 x11_offscreen.py show   <window-id>   # 无 daemon 时的一次性兜底（不推荐）
  python3 x11_offscreen.py grab   <window-id> <out.png>
  python3 x11_offscreen.py state

socket 协议（/tmp/wx_bot_offscreen.sock，JSON 行）：
  {"cmd": "grab", "path": "/tmp/x.png"} -> {"ok": true, "path": ...}
  {"cmd": "show"}                       -> {"ok": true} 然后 daemon 退出
  {"cmd": "probe"}                      -> {"ok": true, "wid":..., "redirected":...}
状态文件：/tmp/wx_bot_offscreen.json
  {"wid":..., "hidden":true, "geom":[l,t,w,h], "pid":..., "socket":"..."}
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time

from Xlib import X
from Xlib import display as _display
from Xlib.ext import composite

STATE_FILE = os.path.join('/tmp', 'wx_bot_offscreen.json')
SOCKET_PATH = os.path.join('/tmp', 'wx_bot_offscreen.sock')
LOG_FILE = os.path.join('/tmp', 'wx_bot_offscreen.log')


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except Exception:
        pass


def read_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def write_state(data):
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # pragma: no cover - 状态文件失败不该影响主流程
        log(f'state write failed: {exc!r}')


def open_display():
    """打开带"吞错"处理器的连接：协议错误只记录，不让 python-xlib 抛半成品异常。"""
    d = _display.Display()
    d.set_error_handler(lambda *a, **k: None)
    return d


def window_rect(d, wid):
    w = d.create_resource_object('window', int(wid))
    t = w.translate_coords(d.screen().root, 0, 0)
    g = w.get_geometry()
    return (-t.x, -t.y, g.width, g.height)


def repaint_desktop(d, x, y, width, height):
    """桌面窗不会自己重画离开的区域，主动 clear_area 一次。"""
    try:
        root = d.screen().root
        for child in root.query_tree().children:
            try:
                cls = child.get_wm_class() or ()
                name = (cls[0] if isinstance(cls, (tuple, list)) and cls else '').lower()
                if 'desktop' in name:
                    child.clear_area(int(x), int(y), int(width), int(height), True)
                    d.sync()
                    return
            except Exception:
                continue
    except Exception:
        pass


class Offscreen:
    """持有 Composite 重定向的助手：hide / grab / show 都在这个对象里完成。"""

    def __init__(self, wid):
        self.wid = int(wid)
        self.d = open_display()
        self.w = self.d.create_resource_object('window', self.wid)
        self.geom = None
        self.redirected = False
        self.sock = None

    # ---------- 基础动作 ----------

    def hide(self):
        state = read_state()
        same_window = state.get('hidden') and int(state.get('wid', -1)) == self.wid
        if not same_window:
            left, top, width, height = window_rect(self.d, self.wid)
            if width <= 1 or height <= 1:
                return False, 'bad geometry'
            self.geom = [left, top, width, height]
        else:  # 幂等：重复 hide 时沿用最初的几何
            self.geom = state.get('geom') or list(window_rect(self.d, self.wid))
        left, top, width, height = [int(v) for v in self.geom]
        try:
            self.w.composite_redirect_window(composite.RedirectAutomatic)
            self.d.sync()
            self.redirected = True
        except Exception as exc:
            return False, f'redirect failed: {exc!r}'
        # 移到屏幕外：xfwm4 会留一条 20~30px 的边在屏幕里，可以接受
        scr = self.d.screen()
        try:
            self.w.configure(x=scr.width_in_pixels + 400, y=top, width=width, height=height)
            self.d.sync()
        except Exception:
            pass
        repaint_desktop(self.d, left, top, width, height)
        write_state({'wid': self.wid, 'hidden': True, 'geom': self.geom,
                     'pid': os.getpid(), 'socket': SOCKET_PATH})
        return True, 'hidden'

    def grab(self, out_path):
        g = self.w.get_geometry()
        if g.width <= 1 or g.height <= 1:
            return False, 'bad geometry'
        try:
            pix = self.w.composite_name_window_pixmap()
            raw = pix.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff).data
        except Exception as exc:
            return False, f'pixmap grab failed: {exc!r}'
        if len(raw) != g.width * g.height * 4:
            return False, 'unexpected pixmap size'
        from PIL import Image
        Image.frombytes('RGB', (g.width, g.height), raw, 'raw', 'BGRX').save(out_path)
        return True, out_path

    def show(self):
        left, top, width, height = [int(v) for v in (self.geom or [100, 100, 1008, 756])]
        # 先回原位置再取消重定向：顺序反了会看到窗口在屏幕外闪一下
        try:
            self.w.configure(x=left, y=top, width=width, height=height, stack_mode=X.Above)
            self.d.sync()
        except Exception:
            pass
        write_state({'wid': self.wid, 'hidden': False, 'geom': [left, top, width, height]})
        if self.redirected:
            try:
                self.w.composite_unredirect_window(composite.RedirectAutomatic)
                self.d.sync()
                self.redirected = False
            except Exception as exc:
                log(f'unredirect note: {exc!r}')
        try:
            self.w.map()
            self.w.configure(stack_mode=X.Above)
            self.d.sync()
        except Exception:
            pass
        repaint_desktop(self.d, left, top, width, height)
        write_state({'wid': self.wid, 'hidden': False, 'geom': [left, top, width, height]})
        return True, 'shown'

    # ---------- 命令循环 ----------

    def _handle(self, req):
        cmd = req.get('cmd')
        if cmd == 'grab':
            out = req.get('path') or '/tmp/wx_offscreen_grab.png'
            ok, detail = self.grab(out)
            return {'ok': ok, 'path': detail if ok else '', 'detail': '' if ok else detail}
        if cmd == 'probe':
            return {'ok': True, 'wid': self.wid, 'redirected': self.redirected,
                    'hidden': self.redirected, 'geom': self.geom}
        if cmd == 'show':
            ok, detail = self.show()
            return {'ok': ok, 'detail': '' if ok else detail, 'closing': True}
        return {'ok': False, 'detail': f'unknown command: {cmd}'}

    def _send(self, conn, payload):
        try:
            conn.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        except Exception:
            pass

    def serve(self):
        ok, detail = self.hide()
        if not ok:
            log(f'hide failed: {detail}')
            return 1
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
        except Exception:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(SOCKET_PATH)
            os.chmod(SOCKET_PATH, 0o600)
            srv.listen(8)
        except Exception as exc:
            log(f'socket setup failed: {exc!r}')
            return 1
        self.sock = srv
        srv.settimeout(0.5)
        log('daemon ready')

        stopping = {'flag': False}

        def _term(*_a):
            stopping['flag'] = True

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)

        while not stopping['flag']:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except Exception as exc:
                log(f'accept failed: {exc!r}')
                break
            conn.settimeout(15.0)
            closing = False
            try:
                buf = b''
                while b'\n' not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b'\n', 1)[0].decode('utf-8', 'replace').strip()
                req = json.loads(line) if line else {}
                reply = self._handle(req)
                closing = bool(reply.pop('closing', False))
                self._send(conn, reply)
            except Exception as exc:
                log(f'command failed: {exc!r}')
                self._send(conn, {'ok': False, 'detail': repr(exc)})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            if closing:
                break

        try:
            srv.close()
        except Exception:
            pass
        try:
            os.remove(SOCKET_PATH)
        except Exception:
            pass
        # 退出前一定尽量把窗口还回去：daemon 没了但窗口还在离屏是最坏情况
        try:
            if self.redirected:
                self.show()
        except Exception:
            pass
        return 0


def cmd_oneshot_show(wid):
    """兜底：没有 daemon（比如 daemon 被 kill）时的一次性 show。

    注意：跨客户端 unredirect 在 python-xlib 上并不可靠，这里只做尽力而为，
    成功后把状态文件写成 hidden:false。
    """
    state = read_state()
    geom = state.get('geom') or [100, 100, 1008, 756]
    left, top, width, height = [int(v) for v in geom]
    d = open_display()
    w = d.create_resource_object('window', int(wid))
    try:
        w.configure(x=left, y=top, width=width, height=height, stack_mode=X.Above)
        d.sync()
    except Exception as exc:
        log(f'oneshot configure failed: {exc!r}')
    try:
        w.composite_unredirect_window(composite.RedirectAutomatic)
        d.sync()
    except Exception as exc:
        log(f'oneshot unredirect note: {exc!r}')
    write_state({'wid': int(wid), 'hidden': False, 'geom': [left, top, width, height]})
    print(json.dumps({'hidden': False, 'geom': [left, top, width, height]}))
    return 0


def cmd_oneshot_grab(wid, out_path):
    """兜底抓图：pixmap 读取不依赖重定向是哪个客户端做的，跨进程可用。"""
    d = open_display()
    w = d.create_resource_object('window', int(wid))
    g = w.get_geometry()
    if g.width <= 1 or g.height <= 1:
        print('bad geometry', file=sys.stderr)
        return 2
    try:
        pix = w.composite_name_window_pixmap()
        raw = pix.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff).data
    except Exception as exc:
        print(f'pixmap grab failed: {exc!r}', file=sys.stderr)
        return 3
    if len(raw) != g.width * g.height * 4:
        print('unexpected pixmap size', file=sys.stderr)
        return 4
    from PIL import Image
    Image.frombytes('RGB', (g.width, g.height), raw, 'raw', 'BGRX').save(out_path)
    print(out_path)
    return 0


def cmd_probe(wid):
    """报告窗口可见性与位置。

    注意：**不能用 name_window_pixmap 判断是否被重定向** —— python-xlib 在未重定向
    的窗口上也会返回一个 pixmap 对象而不抛 BadMatch（实测 Thunar、终端全部误报
    redirected=True）。所以这里只报 viewable / rect / offscreen（是否被挪到屏幕外），
    真正"是否处于重定向"只由守护进程内部状态回答。
    """
    d = open_display()
    w = d.create_resource_object('window', int(wid))
    scr = d.screen()
    info = {'wid': int(wid), 'viewable': False, 'offscreen': False, 'rect': None}
    try:
        info['viewable'] = w.get_attributes().map_state == X.IsViewable
    except Exception:
        pass
    try:
        rect = list(window_rect(d, wid))
        info['rect'] = rect
        # 窗口被挪到屏幕右侧外：xfwm4 会留约 29px 在屏内，所以用"几乎整窗在外"判断
        info['offscreen'] = bool(rect and rect[0] > scr.width_in_pixels - 120)
    except Exception:
        pass
    print(json.dumps(info))
    sys.stdout.flush()
    return 0


def cmd_state():
    print(json.dumps(read_state()))
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 64
    action = argv[1]
    if action == 'state':
        return cmd_state()
    if len(argv) < 3:
        print('missing window id', file=sys.stderr)
        return 64
    wid = int(argv[2], 0)
    if action == 'daemon':
        return Offscreen(wid).serve()
    if action == 'probe':
        return cmd_probe(wid)
    if action == 'show':
        return cmd_oneshot_show(wid)
    if action == 'grab':
        if len(argv) < 4:
            print('missing output path', file=sys.stderr)
            return 64
        return cmd_oneshot_grab(wid, argv[3])
    if action == 'hide':
        # 兼容旧调用：等价于 daemon（前台运行，通常由 io 后台 Popen）
        return Offscreen(wid).serve()
    print(f'unknown action: {action}', file=sys.stderr)
    return 64


if __name__ == '__main__':
    _code = main(sys.argv)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # 直接 _exit：python-xlib 的 Display 析构/关闭在异常路径上会永久阻塞
    os._exit(_code)

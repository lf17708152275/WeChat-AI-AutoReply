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

为什么连接要"分工"：一旦某个连接上发生过 Composite 报错，它就被弄脏了，后续任何
需要读回复的请求（sync / 读属性 / 抓图）都可能永久阻塞。所以本进程只用一个专属
连接持有 redirect，其它所有 X 操作（取几何、移动、map、重绘、抓 pixmap）都另开
一次性连接完成；show() 里 unredirect 必须是**最后一个** X 调用，之后就只写状态
文件、回复 socket、退出，不再碰这个连接。

用法：
  python3 x11_offscreen.py daemon  <window-id>   # hide 时由 wechat_linux_io 启动
  python3 x11_offscreen.py restore <window-id> <left> <top> <width> <height>
  python3 x11_offscreen.py show    <window-id>   # 无 daemon 时的一次性兜底（不推荐）
  python3 x11_offscreen.py grab    <window-id> <out.png>
  python3 x11_offscreen.py probe   <window-id>
  python3 x11_offscreen.py state

socket 协议（/tmp/wx_bot_offscreen.sock，JSON 行）：
  {"cmd": "ping"}                       -> {"ok": true, "pid": ...}
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


def close_quietly(d):
    """关闭一次性连接；守护进程里连接关闭在异常路径上可能阻塞，吞掉即可。"""
    try:
        d.close()
    except Exception:
        pass


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


def repaint_desktop_once(x, y, width, height):
    """用一次性连接重绘桌面（避免弄脏持有 redirect 的那个连接）。"""
    try:
        d = open_display()
        try:
            repaint_desktop(d, x, y, width, height)
        finally:
            close_quietly(d)
    except Exception as exc:
        log(f'repaint note: {exc!r}')


def configure_once(wid, left, top, width, height, raise_it=True):
    """用一次性连接把窗口摆回指定几何（不碰守护进程的 redirect 连接）。"""
    try:
        d = open_display()
        try:
            w = d.create_resource_object('window', int(wid))
            if raise_it:
                w.configure(x=int(left), y=int(top), width=int(width), height=int(height),
                            stack_mode=X.Above)
            else:
                w.configure(x=int(left), y=int(top), width=int(width), height=int(height))
            w.map()
            d.flush()
            d.sync()
            return True
        finally:
            close_quietly(d)
    except Exception as exc:
        log(f'configure note: {exc!r}')
        return False


class Offscreen:
    """持有 Composite 重定向的助手：hide / grab / show 都在这个对象里完成。

    self.d 是唯一允许做 composite 请求的连接，并且**只做这一个用途**：
    hide 时 redirect，show 时 unredirect（必须是 show 里的最后一个 X 动作）。
    """

    def __init__(self, wid):
        self.wid = int(wid)
        self.d = open_display()
        self.w = self.d.create_resource_object('window', self.wid)
        self.geom = None
        self.redirected = False
        self.sock = None

    # ---------- 基础动作 ----------

    def ping(self):
        return True, 'pong'

    def _current_rect(self):
        d = open_display()
        try:
            return list(window_rect(d, self.wid))
        finally:
            close_quietly(d)

    def hide(self):
        state = read_state()
        if state.get('hidden') and int(state.get('wid', -1)) == self.wid and state.get('geom'):
            self.geom = [int(v) for v in state['geom']]  # 幂等：沿用最初的几何
        else:
            try:
                left, top, width, height = self._current_rect()
            except Exception as exc:
                return False, f'geometry failed: {exc!r}'
            if width <= 1 or height <= 1:
                return False, 'bad geometry'
            self.geom = [left, top, width, height]
        left, top, width, height = [int(v) for v in self.geom]
        # 唯一一次 composite 请求：redirect 后不再 sync（避免错误包阻塞），只 flush
        try:
            self.w.composite_redirect_window(composite.RedirectAutomatic)
            self.d.flush()
            self.redirected = True
        except Exception as exc:
            return False, f'redirect failed: {exc!r}'
        write_state({'wid': self.wid, 'hidden': True, 'geom': self.geom,
                     'pid': os.getpid(), 'socket': SOCKET_PATH})
        # 窗口挪到屏幕外（xfwm4 会留一条 20~30px 的边在屏幕里，可以接受）。
        # 这一步仍走持有 redirect 的连接：configure 无回复，flush 即可，不需要 sync。
        try:
            scr = self.d.screen()
            self.w.configure(x=scr.width_in_pixels + 400, y=top, width=width, height=height)
            self.d.flush()
        except Exception as exc:
            log(f'move-offscreen note: {exc!r}')
        repaint_desktop_once(left, top, width, height)
        return True, 'hidden'

    def grab(self, out_path):
        """抓 Composite pixmap：用一次性连接，失败不会弄脏持有 redirect 的连接。"""
        d = open_display()
        try:
            w = d.create_resource_object('window', self.wid)
            g = w.get_geometry()
            if g.width <= 1 or g.height <= 1:
                return False, 'bad geometry'
            try:
                pix = w.composite_name_window_pixmap()
                raw = pix.get_image(0, 0, g.width, g.height, X.ZPixmap,
                                    0xffffffff).data
            except Exception as exc:
                return False, f'pixmap grab failed: {exc!r}'
            if len(raw) != g.width * g.height * 4:
                return False, 'unexpected pixmap size'
            from PIL import Image
            Image.frombytes('RGB', (g.width, g.height), raw, 'raw', 'BGRX').save(out_path)
            return True, out_path
        finally:
            close_quietly(d)

    def show(self):
        """召回窗口：先摆回几何/重绘（一次性连接），最后才在自己的连接上 unredirect。

        顺序很重要：unredirect 之后绝不再对这个连接做任何需要读回复的请求，
        否则一旦 unredirect 报错（重定向已被撤销/被别人抢走），连接就被弄脏，
        下面任何 sync() 都会永久阻塞 —— 这正是「召回卡死」的根因。
        """
        state = read_state()
        geom = self.geom or state.get('geom')
        if not geom:
            try:
                geom = self._current_rect()
            except Exception:
                geom = [100, 100, 1008, 756]
        left, top, width, height = [int(v) for v in geom]
        configure_once(self.wid, left, top, width, height, raise_it=True)
        repaint_desktop_once(left, top, width, height)
        if self.redirected:
            try:
                self.w.composite_unredirect_window(composite.RedirectAutomatic)
                self.d.flush()
            except Exception as exc:
                log(f'unredirect note: {exc!r}')
            self.redirected = False
        write_state({'wid': self.wid, 'hidden': False,
                     'geom': [left, top, width, height]})
        return True, 'shown'

    # ---------- 命令循环 ----------

    def _handle(self, req):
        cmd = req.get('cmd')
        if cmd == 'ping':
            return {'ok': True, 'pid': os.getpid(), 'redirected': self.redirected}
        if cmd == 'grab':
            out = req.get('path') or '/tmp/wx_offscreen_grab.png'
            ok, detail = self.grab(out)
            return {'ok': ok, 'path': detail if ok else '', 'detail': '' if ok else detail}
        if cmd == 'probe':
            return {'ok': True, 'wid': self.wid, 'redirected': self.redirected,
                    'hidden': self.redirected, 'geom': self.geom, 'pid': os.getpid()}
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
                state = read_state()
                if state.get('pid') not in (None, os.getpid()):
                    log(f'socket belongs to pid {state.get("pid")}, leaving it alone')
                    return 1
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
        log(f'daemon ready pid={os.getpid()} wid={self.wid}')

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
        # 只删自己的 socket：别的 daemon 可能已经接管了同名路径
        try:
            state = read_state()
            if state.get('pid') in (None, os.getpid()):
                os.remove(SOCKET_PATH)
        except Exception:
            pass
        # 退出前一定尽量把窗口还回去：daemon 没了但窗口还在离屏是最坏情况
        try:
            if self.redirected:
                self.show()
        except Exception as exc:
            log(f'final show note: {exc!r}')
        return 0


def cmd_oneshot_show(wid):
    """兜底：没有 daemon（比如 daemon 被 kill）时的一次性 show。

    注意：跨客户端 unredirect 在 python-xlib 上并不可靠，这里只做尽力而为，
    成功后把状态文件写成 hidden:false。
    """
    state = read_state()
    geom = state.get('geom') or [100, 100, 1008, 756]
    left, top, width, height = [int(v) for v in geom]
    configure_once(wid, left, top, width, height, raise_it=True)
    repaint_desktop_once(left, top, width, height)
    write_state({'wid': int(wid), 'hidden': False, 'geom': [left, top, width, height]})
    print(json.dumps({'hidden': False, 'geom': [left, top, width, height]}))
    return 0


def cmd_restore(wid, left, top, width, height):
    """硬恢复：守护进程已经不在（或状态错乱）时，把窗口从离屏状态拉回桌面。

    进程退出时 X 服务器会自动撤销它做的自动重定向，所以这里**不做任何 composite
    请求**（对已撤销的窗口 unredirect 会触发 python-xlib 的半成品异常并让连接
    阻塞）。只要 configure 回原几何 + map + raise + 重绘桌面就够。
    """
    left, top, width, height = int(left), int(top), int(width), int(height)
    ok = configure_once(wid, left, top, width, height, raise_it=True)
    repaint_desktop_once(left, top, width, height)
    write_state({'wid': int(wid), 'hidden': False, 'geom': [left, top, width, height]})
    print(json.dumps({'wid': int(wid), 'hidden': False, 'ok': ok,
                      'geom': [left, top, width, height]}))
    sys.stdout.flush()
    return 0


def cmd_oneshot_grab(wid, out_path):
    """兜底抓图：pixmap 读取不依赖重定向是哪个客户端做的，跨进程可用。"""
    d = open_display()
    try:
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
    finally:
        close_quietly(d)


def cmd_probe(wid):
    """报告窗口可见性与位置。

    注意：**不能用 name_window_pixmap 判断是否被重定向** —— python-xlib 在未重定向
    的窗口上也会返回一个 pixmap 对象而不抛 BadMatch（实测 Thunar、终端全部误报
    redirected=True）。所以这里只报 viewable / rect / offscreen（是否被挪到屏幕外），
    真正"是否处于重定向"只由守护进程内部状态回答。
    """
    d = open_display()
    try:
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
    finally:
        close_quietly(d)


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
    if action == 'restore':
        if len(argv) < 7:
            print('missing geometry: restore <wid> <left> <top> <width> <height>',
                  file=sys.stderr)
            return 64
        return cmd_restore(wid, argv[3], argv[4], argv[5], argv[6])
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

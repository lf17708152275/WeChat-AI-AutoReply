import os
import sys
import time
import json
import re
import random
import hashlib
import ctypes
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageGrab
import requests
from rapidocr_onnxruntime import RapidOCR

# Linux 适配：Win32 专属依赖在 Linux 上不存在，全部降级为可选
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

if IS_WIN:
    from ctypes import wintypes
else:
    wintypes = None
    try:
        import wechat_linux_io as lx        # X11 后端（抓图/点击/键盘/剪贴板）
    except Exception as _lx_err:            # 缺 python-xlib 等依赖时要能一眼看出来
        lx = None
        _LX_IMPORT_ERR = _lx_err

try:
    import pyperclip
except Exception:                           # Linux 无 xclip/xsel 时 import 即失败
    pyperclip = None

try:
    import persona_cards as pcards
except Exception:                     # 缺文件时退回纯 system_prompt 老路径
    pcards = None

try:
    from agent_client import AgentClient, build_prompt
    import voice_tts
except Exception as _agent_import_err:  # 缺文件时不影响老的大模型直连路径
    AgentClient = None
    build_prompt = None
    _AGENT_IMPORT_ERR = _agent_import_err
else:
    _AGENT_IMPORT_ERR = None

# =============================================================================
# 0. WIN32 API DEFINITIONS & DESKTOP BINDING
# =============================================================================
if IS_WIN:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shcore = ctypes.windll.shcore
    kernel32 = ctypes.windll.kernel32

    try:
        shcore.SetProcessDpiAwareness(2) # Per-monitor DPI aware
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
else:
    user32 = gdi32 = shcore = kernel32 = None

def ensure_default_desktop():
    if IS_LINUX:
        return                       # X11 没有「桌面切换」，无需绑定
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
            user32.CloseDesktop(h_desk)
    except Exception:
        pass

ensure_default_desktop()

# =============================================================================
# 1. CONSTANTS & SYSTEM PATHS
# =============================================================================
# 代码在 src/ 下，运行期数据在仓库根的 data/ 与 logs/（见 README「目录结构」）
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
for _d in (DATA_DIR, LOG_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

CONFIG_FILE = os.path.join(DATA_DIR, "wechat_config_dev.json")
CONVO_MAP_FILE = os.path.join(DATA_DIR, "wechat_convo_map.json")
PID_FILE = os.path.join(DATA_DIR, "wechat_bot_dev.pid")
LOG_FILE = os.path.join(LOG_DIR, "wechat_bot_dev.log")

try:
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("gbk", errors="ignore").decode("gbk"), flush=True)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def init_ocr():
    models_dir = os.path.join(BASE_DIR, "models")
    if not os.path.exists(models_dir):
        models_dir = os.path.join(BASE_DIR, "_internal", "models")
    
    det_model = os.path.join(models_dir, "ch_PP-OCRv4_det_infer.onnx")
    cls_model = os.path.join(models_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
    rec_model = os.path.join(models_dir, "ch_PP-OCRv4_rec_infer.onnx")
    
    # 🚀 线程数必须显式钉死。ORT 默认 intra_op_num_threads=0 会让运行时在
    # 多核机器上开出一个病态线程池：本机 20 逻辑核上同一张 1925x1313 截图
    # 默认要 25.8s，钉死 4 线程后只要 5.4s（约 4.7x）。8/12/20 线程反而更慢。
    tune = {"intra_op_num_threads": 4, "inter_op_num_threads": 1}

    if os.path.exists(det_model) and os.path.exists(rec_model):
        try:
            return RapidOCR(det_model_path=det_model, cls_model_path=cls_model,
                            rec_model_path=rec_model, **tune)
        except Exception:
            pass
    try:
        return RapidOCR(**tune)
    except Exception:
        # 老版本 rapidocr 不认这两个 kwarg，退回默认
        return RapidOCR()

# Initialize local neural network OCR engine
ocr_engine = init_ocr()

# =============================================================================
# 2. CONFIGURATION & STATE MANAGEMENT
# =============================================================================
LAST_PROCESSED_SIGNATURE = {}
LAST_PROCESSED_AT = {}          # target -> 最近一次「登记为已处理」的时间戳
SKIP_LOG_AT = {}               # (target, reason) -> 上次输出跳过日志的时间
API_CONVERSATION_HISTORY = {}
LAST_CLICKED_CANDIDATE_TIME = {}
SEND_FAIL_RETRY = {}
LAST_CHAT_HASH = None
LAST_SIDEBAR_HASH = None
LAST_FULL_SCAN_TIME = 0

# -----------------------------------------------------------------------------
# Agent 异步总线（P0 解耦）
# 主循环绝不能被 agent 的思考时间阻塞，所以：
#   AGENT_POOL    —— 4 条线程并发问 agent，一个联系人一个 contextId
#   AGENT_PENDING —— target -> 任务上下文（签名/消息/时间），用于取回结果时校验
#   AGENT_RESULTS —— 结果邮箱：key = 任务 id，value = 决策 dict
# 发送动作仍然 100% 留在主线程（send_reply_instant 会抢焦点，多线程会串窗）
# -----------------------------------------------------------------------------
AGENT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
AGENT_LOCK = threading.RLock()

# 主循环每轮把找到的微信窗口缓存在这里，供 HTTP 线程读取。
# 血泪坑：python-xlib 的 Display 连接不是线程安全的，直接在 HTTP 线程里调
# get_wechat_hwnd() 会跟主线程抢 Xlib 连接卡死（/status 一直不返回）。
WECHAT_HWND_CACHE = [None]
AGENT_PENDING = {}      # job_id -> {"target","signature","message","submit_ts","source"}
AGENT_RESULTS = {}      # job_id -> {"target","signature","decision","done_ts","cost"}
AGENT_INFLIGHT = {}     # target -> job_id（同一好友只允许一个在飞，新消息覆盖旧的）
AGENT_LATEST = {}       # target -> 最后一次提交的 job_id（只增不清，用来丢弃过期结论）
_AGENT_JOB_SEQ = [0]

# 主动发送（P2）限流账本
PROACTIVE_SENT_LOG = []      # [(ts, target)]
PROACTIVE_SERVER = [None]    # 运行中的 HTTP 服务实例
# 待确认队列（P3）：id -> 记录。落盘到 pending_confirm.jsonl，重启不丢。
PENDING_CONFIRM = {}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "whitelist_mode": True,
        "whitelist": ["好友A", "好友B"],
        "blacklist": ["文件传输助手", "微信团队", "订阅号", "公众号", "服务号"],
        "ai_engine": "openai_api",
        "openai_api": {
            "api_key": "",
            "api_base": "https://api.xiaomimimo.com/v1",
            "model": "mimo-v2.6-flash",
            "temperature": 0.7
        },
        "system_prompt": "你现在是微信账号的主人（真实本人）。请以我（主人本人）的第一人称、自然口语、亲切随和的日常语气直接回复微信好友的消息。就像我本人在用手机打字聊天一样自然。严禁透露任何AI、机器人或助理身份，严禁使用客套机械的客服腔，严禁输出任何解释或markdown格式，直接输出我回复的那句话。",
        "reply_delay_seconds": 0.0,
        "check_interval_seconds": 0.8,
        "ignored_keywords": ["按住鼠标", "语音输入文字", "按住说话", "按Enter发送", "发送(S)"],
        "agent": {
            "enabled": True,
            "url": "http://127.0.0.1:44708/a2a",
            "context_prefix": "wx-",
            "timeout_seconds": 180,
            "max_defer_seconds": 900,
            "fallback_replies": [
                "嘿嘿，我这会儿有点懵，等下再聊~",
                "啊呀，脑子突然卡壳了，晚点回你哈",
                "我这边有点小状况，一会儿再来找你~",
                "等等我，手头有点事，马上回来！"
            ]
        },
        "proactive_send": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8765,
            "whitelist_only": True,
            "max_per_hour": 3,
            "max_per_day": 20,
            "require_confirm": False,
            "allow_search": True,
            "confirm_expire_hours": 12
        },
        "contacts": []
    }

def load_convo_map():
    if os.path.exists(CONVO_MAP_FILE):
        try:
            with open(CONVO_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

_CFG_WARNED = set()

def cfg_float(dct, key, default):
    """健壮读取数字配置项：非法/缺失时回退默认值，每个键只告警一次防刷屏"""
    try:
        return float(dct.get(key, default))
    except Exception:
        if key not in _CFG_WARNED:
            _CFG_WARNED.add(key)
            log(f"⚠️ [配置项无效] {key} 不是合法数字，已回退默认值 {default}")
        return float(default)

# =============================================================================
# 3. DPI & PHYSICAL LAYOUT ENGINE (WINDOW-SIZE INVARIANT)
# =============================================================================
def get_dpi_scale():
    if IS_LINUX:
        return 1.0                   # X11 下图像即物理像素，实测 1:1
    try:
        hdc = user32.GetDC(0)
        log_w = gdi32.GetDeviceCaps(hdc, 8)  # HORZRES
        phys_w = gdi32.GetDeviceCaps(hdc, 118) # DESKTOPHORZRES
        user32.ReleaseDC(0, hdc)
        scale = phys_w / float(log_w)
        return scale if scale > 0 else 1.0
    except Exception:
        return 1.0

def _detect_chat_start_x(img, header_h, input_h):
    """用像素扫描找出左侧会话列表与右侧聊天区的分界线 x。

    微信 4.x 的会话列表是**固定像素宽**（约 354px，加上左侧图标栏约 90px ≈ 444），
    不随窗口宽度按比例缩放；按比例估算会少算 100px+，把列表里的昵称/预览/时间
    当成聊天气泡 OCR 进来（实测 Agent 收到的消息变成「昨天17:42，吃饭了，吃饭了」）。
    扫输入框上方那段空白：列表背景 ~#EEEEF0（亮度和 ≈714），聊天区背景 ~#F7F7F7（≈741），
    取第一条持续变亮的列作为分界线。失败返回 None。
    """
    try:
        arr = np.asarray(img.convert("RGB"))
        H, W = arr.shape[:2]
        y0 = max(header_h + 20, H - input_h - 90)
        y1 = max(y0 + 1, H - int(input_h * 0.9))
        band = arr[y0:y1]
        if band.size == 0:
            return None
        cols = band.mean(axis=(0, 2))
        start = int(W * 0.18)
        stop = int(W * 0.72)
        run = 0
        for x in range(start, stop):
            if cols[x] >= 244.0:
                run += 1
                if run >= 6:
                    return x - run + 1
            else:
                run = 0
        return None
    except Exception:
        return None


def compute_wechat_layout(W, H, img=None):
    """
    基于微信 PC 客户端原生物理布局结构计算关键区域：
    无论窗口拉宽、拉高、缩小或最大化，物理像素绝对精准咬合。
    img 传入时用像素扫描精确定位右侧聊天区起点（推荐），否则退回比例估算。
    """
    sidebar_w = max(55, int(0.055 * W))
    # 顶栏/输入框尺寸要先算，像素扫描会用它们定位空白带
    header_h = max(88, int(0.140 * H))
    input_h = min(220, max(140, int(0.160 * H)))
    chat_start_x = None
    if img is not None:
        chat_start_x = _detect_chat_start_x(img, header_h, input_h)
    if not chat_start_x:
        chat_start_x = max(320, int(0.285 * W))
    # 顶栏高度：微信 4.x 的标题行随窗口高度略微下移，
    # 原来 max(85, 0.110*H) 在 756 高的窗口上只给到 85，而标题 cy≈90 被切掉 →
    # 顶栏 OCR 全空 → 白名单匹配失败 → 收到消息也不回（2026-11-02 实测踩到）。
    
    return {
        "sidebar_w": sidebar_w,
        "chat_start_x": chat_start_x,
        "header_h": header_h,
        "input_h": input_h,
        "total_w_px": W,
        "total_h_px": H
    }

if IS_WIN:
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
else:
    WNDENUMPROC = None            # Linux 走窗树递归，不需要 EnumWindows 回调

def get_wechat_hwnd():
    if IS_LINUX:
        if lx is None:
            _warn_throttled("lx_missing", f"⚠️ [Linux 后端不可用] {globals().get('_LX_IMPORT_ERR')}")
            return None, None
        return lx.find_wechat_window()
    ensure_default_desktop()
    candidates = []
    def enum_proc(hwnd, lp):
        class_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buff, 256)
        c = class_buff.value
        if "Qt51514QWindowIcon" in c or "WeChatMainWndForPC" in c:
            wname = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, wname, 256)
            t = wname.value
            # 微信 4.0 伴随空壳代理窗口标题固定为纯英文 "Weixin"
            is_shell = (t == "Weixin")
            ver = 4 if "Qt" in c else 3
            candidates.append((hwnd, ver, t, is_shell))
            if is_shell:
                try:
                    user32.ShowWindow(hwnd, 0) # SW_HIDE 静默隐藏空壳代理窗口
                except Exception:
                    pass
        return True
        
    cb = WNDENUMPROC(enum_proc)
    user32.EnumWindows(cb, 0)
    
    # 严格优先选取真实主聊天窗口 (排除空壳窗口)
    real_candidates = [c for c in candidates if not c[3]]
    if real_candidates:
        return real_candidates[0][0], real_candidates[0][1]
    if candidates:
        return candidates[0][0], candidates[0][1]
    return None, None

_LAST_WARN = {}


def _warn_throttled(tag, msg, interval=10.0):
    """同一条告警 interval 秒内只打一次，避免 0.5 秒心跳把日志刷爆。"""
    now = time.time()
    if now - _LAST_WARN.get(tag, 0.0) < interval:
        return
    _LAST_WARN[tag] = now
    log(msg)


def grab_wechat_window(hwnd):
    """
    开发版后台静默离屏抓图引擎（PrintWindow / DWM）：
    无论微信被其他窗口覆盖压在底层还是处于离屏隐形状态，都能静默抓取微信自身的 100% 真实画面！
    """
    if IS_LINUX:
        if lx is None:
            return None, None, 1.0
        img, rect, scale = lx.grab_window(hwnd)
        if img is not None:
            # 全黑图兜底判定（与 Windows 侧同一套阈值）
            try:
                arr = np.asarray(img)
                if arr.size == 0 or int(arr.max()) <= 10 or float(arr.std()) < 1.0:
                    _warn_throttled("grab_blank_linux", "⚠️ [抓图空白] X11 抓到的微信画面是纯色/空白，本轮跳过")
                    return None, rect, scale
            except Exception:
                pass
        return img, rect, scale
    try:
        # PrintWindow 对隐藏/最小化窗口只会返回全黑图，抓之前先确保窗口可见。
        # 注意：最小化的窗口 IsWindowVisible 仍是 True，必须额外判 IsIconic，
        # 否则 rect 停在 (-32000,-32000)、PrintWindow 返回 276x45 黑图，每 0.5 秒刷屏。
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            ensure_wechat_visible(hwnd)
            user32.GetWindowRect(hwnd, ctypes.byref(wintypes.RECT()))  # 让 DWM 有机会刷新

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            w, h = 1100, 750

        # 最小化时 rect 会停在 -32000（此时 IsWindowVisible 仍为 True），
        # 直接早退，既不画黑图也不退到「屏幕截屏兜底」（那会抓到别的窗口）。
        if rect.left <= -30000 or rect.top <= -30000:
            _warn_throttled(
                "wx_minimized",
                "⚠️ [微信已最小化] 窗口 rect 停在 -32000，无法抓图；把微信还原成窗口后会自动恢复")
            return None, rect, 1.0

        scale = get_dpi_scale()
        
        hwnd_dc = None
        mfc_dc = None
        save_bitmap = None
        img = None
        result = 0
        try:
            hwnd_dc = user32.GetWindowDC(hwnd)
            mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
            save_bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
            gdi32.SelectObject(mfc_dc, save_bitmap)
            
            result = user32.PrintWindow(hwnd, mfc_dc, 2)
            if not result:
                result = user32.PrintWindow(hwnd, mfc_dc, 0)
                
            if result:
                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ('biSize', wintypes.DWORD),
                        ('biWidth', wintypes.LONG),
                        ('biHeight', wintypes.LONG),
                        ('biPlanes', wintypes.WORD),
                        ('biBitCount', wintypes.WORD),
                        ('biCompression', wintypes.DWORD),
                        ('biSizeImage', wintypes.DWORD),
                        ('biXPelsPerMeter', wintypes.LONG),
                        ('biYPelsPerMeter', wintypes.LONG),
                        ('biClrUsed', wintypes.DWORD),
                        ('biClrImportant', wintypes.DWORD)
                    ]
                bmi = BITMAPINFOHEADER()
                bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.biWidth = w
                bmi.biHeight = -h # top-down
                bmi.biPlanes = 1
                bmi.biBitCount = 32
                bmi.biCompression = 0
                
                buf = ctypes.create_string_buffer(w * h * 4)
                gdi32.GetDIBits(mfc_dc, save_bitmap, 0, h, buf, ctypes.byref(bmi), 0)
                img = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
        finally:
            if save_bitmap:
                gdi32.DeleteObject(save_bitmap)
            if mfc_dc:
                gdi32.DeleteDC(mfc_dc)
            if hwnd_dc:
                user32.ReleaseDC(hwnd, hwnd_dc)
        
        # 🚨 全黑图检测：PrintWindow 对隐藏/最小化的窗口会「成功」返回一张纯黑图！
        # 之前只判 max()>10 漏掉了这种情况，导致下游 OCR 读到空结果、误判成
        # 「点击未生效」，甚至退到屏幕截屏抓到了别的窗口。必须把 std 也一起判。
        if img is not None:
            try:
                arr = np.asarray(img)
                if arr.size == 0 or int(arr.max()) <= 10 or float(arr.std()) < 1.0:
                    _warn_throttled(
                        "grab_black",
                        f"⚠️ [抓图黑屏] hwnd={hwnd} rect=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
                        f"size={img.size} PrintWindow={bool(result)} → 窗口被隐藏/最小化，本轮跳过")
                    img = None
            except Exception:
                img = None

        if img is not None:
            return img, rect, scale

        # 备用：物理截屏 (仅当窗口在正常屏幕内时)
        if rect.left > -1000 and rect.top > -1000:
            bbox = (int(rect.left * scale), int(rect.top * scale), int(rect.right * scale), int(rect.bottom * scale))
            fallback_img = ImageGrab.grab(bbox=bbox).convert('RGB')
            return fallback_img, rect, scale
        return None, rect, scale
    except Exception:
        return None, None, 1.0

def activate_wechat(hwnd):
    """
    Win32 穿透提权引擎：
    突破 Windows 10/11 防抢焦点 (ASFW) 与输入队列隔离，赋予微信合法键盘输入权限。
    """
    if IS_LINUX:
        return bool(lx and lx.activate(hwnd))
    try:
        ensure_default_desktop()
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9) # SW_RESTORE
            time.sleep(0.04)
            
        cur_fore = user32.GetForegroundWindow()
        cur_thread = user32.GetWindowThreadProcessId(cur_fore, None) if cur_fore else 0
        wx_thread = user32.GetWindowThreadProcessId(hwnd, None)
        my_thread = kernel32.GetCurrentThreadId()
        
        attached_cur = False
        attached_my = False
        try:
            if cur_thread and cur_thread != wx_thread:
                attached_cur = bool(user32.AttachThreadInput(cur_thread, wx_thread, True))
            if my_thread and my_thread != wx_thread:
                attached_my = bool(user32.AttachThreadInput(my_thread, wx_thread, True))
                
            # 瞬时穿透提升 Z-Order 并切换焦点 (SWP_NOSIZE | SWP_NOMOVE = 0x0003)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003 | 0x0040)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)
        finally:
            if attached_cur:
                user32.AttachThreadInput(cur_thread, wx_thread, False)
            if attached_my:
                user32.AttachThreadInput(my_thread, wx_thread, False)
                
        time.sleep(0.03)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False

def _nonclient_offset(hwnd):
    """
    抓图用的是 PrintWindow 的「整窗」位图，其中带上了非客户区边框/阴影
    （实测微信 4.15 在本机是左边框 12px、上边框 0px）；
    而 PostMessage 的 WM_LBUTTONDOWN 用的是「客户区」坐标。
    两者必须换算，否则所有点击会整体左偏 —— 侧栏点不中会话行就是这么来的。

    Linux 侧抓的就是整窗区域，图像坐标 == 窗口坐标，偏移恒为 0。
    """
    if IS_LINUX:
        return 0, 0
    try:
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x - r.left, pt.y - r.top
    except Exception:
        return 0, 0


def ensure_wechat_visible(hwnd):
    """
    确保微信窗口处于「可见」状态。

    PrintWindow 对隐藏/最小化的窗口会「成功」返回一张全黑图，于是代码会退到
    屏幕截屏兜底，而兜底截到的是压在微信上面的别的窗口（实测抓到了 ego 浏览器），
    接着 OCR 就拿浏览器内容去匹配联系人 —— 这是真机连续失败的根因链条。
    注意：把窗口挪到屏幕外是可以的（PrintWindow 照样能画），SW_HIDE 不行。
    """
    if IS_LINUX:
        return bool(lx and lx.ensure_visible(hwnd))
    try:
        if not hwnd:
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            time.sleep(0.25)
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE，显示但不抢焦点
            time.sleep(0.25)
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def post_click_client_point(hwnd, client_x, client_y):
    """
    通过 Win32 消息向微信窗口直接发送局部点击消息，
    绝对不调用 SetCursorPos，物理鼠标 1 个像素都不会动！

    传入的是「抓图像素坐标」，内部会减去非客户区偏移换成客户区坐标。

    Linux 侧用 XTest 真实点击（Qt 对 PostMessage 等价物不可靠）。
    代价是会真的移动光标；这是 X11 下唯一稳定的注入方式。
    """
    if IS_LINUX:
        if lx:
            lx.click_client(hwnd, client_x, client_y)
        return
    try:
        dx, dy = _nonclient_offset(hwnd)
        cx = int(client_x) - dx
        cy = int(client_y) - dy
        lParam = (cy << 16) | (cx & 0xFFFF)
        user32.PostMessageW(hwnd, 0x0200, 0x0000, lParam) # WM_MOUSEMOVE 先更新 Qt 内部悬停控件
        time.sleep(0.01)
        user32.PostMessageW(hwnd, 0x0201, 0x0001, lParam) # WM_LBUTTONDOWN
        time.sleep(0.02)
        user32.PostMessageW(hwnd, 0x0202, 0x0000, lParam) # WM_LBUTTONUP
        time.sleep(0.03)
    except Exception:
        pass

def click_screen_point(x, y):
    """开发版兼容保留函数"""
    pass

# =============================================================================
# 4. HIGH-PERFORMANCE ROBUST NAME MATCHING & RED BADGE FILTER
# =============================================================================
def normalize_for_match(text):
    if not text:
        return ""
    t = str(text).lower().strip()
    t = re.sub(r'\s+', '', t)
    t = t.replace('0', 'o').replace('1', 'l').replace('i', 'l')
    t_clean = re.sub(r'[\^_\~\-\.\,\'\"\`\*\#\@\!\:\;\|\/\\]', '', t)
    return t_clean

def is_group_chat_name(text):
    if not text:
        return False
    t = str(text).strip()
    # 1. 明确包含群聊关键词
    if re.search(r'群聊|交流群|互助群|通知群|工作群|家族群|同乡群|部门群', t):
        return True
    # 2. 名字末尾紧贴括号人数: 如 '吃喝玩乐(25)' 或 '吃喝玩乐（25）'
    name_seg = re.split(r'\s+', t, maxsplit=1)[0]
    if re.search(r'[\(（]\d+[\)）]$', name_seg):
        return True
    # 3. 名字与括号人数带空格: 如 '吃喝玩乐 (25)' 或 '吃喝玩乐 (25) 好的' (第2段纯为括号人数)
    parts = re.split(r'\s+', t)
    if len(parts) >= 2 and re.match(r'^[\(（]\d+[\)）]$', parts[1]):
        return True
    return False

def looks_like_group(text):
    """群聊粗筛，但放行「明显是未读数后缀」的文本。

    会话被选中时显示的是 "备注(未读数)"，跟群聊的 "群名(人数)" 长得一样。
    区别在于：剥掉括号后能精确命中白名单的，按未读数处理 —— 这正是
    is_name_matched_strict 里剥变体 + 严格全等要做的事，所以这里不要提前一票否决。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if re.search(r'群聊|交流群|互助群|通知群|工作群|家族群|同乡群|部门群', t):
        return True
    # 剥掉尾部括号数字后仍有内容 -> 末位括号数字按"未读数"处理，交给白名单精确判定
    # 剥掉尾缀后还带群关键词（"ai聊天群(3)"）-> 仍是群
    tail_stripped = re.sub(r'\s*[\(（]\d+[\)）]\s*$', '', t).strip()
    if tail_stripped and tail_stripped != t:
        # 剥完仍是群名（"ai聊天群(3)"）或尾巴后面还有预览（"吃喝玩乐(25) 张三：好的"）-> 群
        if re.search(r'群聊|交流群|互助群|通知群|工作群|家族群|同乡群|部门群', tail_stripped):
            return True
        # "吃喝玩乐(25) 张三：好的" —— 括号人数后面还跟着预览文字，是真群
        m = re.search(r'[\(（]\d+[\)）](\s*\S.*)$', t)
        if m and re.sub(r'[：:]$', '', m.group(1).strip()):
            return True
        return False
    # 尾缀后面还跟着预览文字 -> 真群聊（"吃喝玩乐(25) 张三：好的"）
    m = re.search(r'[\(（]\d+[\)）](\s*\S.*)$', t)
    if m and re.sub(r'[：:]$', '', m.group(1).strip()):
        return True
    # 名字中间带人数、预览在第二段 -> 兼容 "吃喝玩乐 (25) 张三：好的"
    parts = re.split(r'\s+', t)
    if len(parts) >= 2 and re.match(r'^[\(（]\d+[\)）]$', parts[1]):
        return True
    return False


def is_name_matched_strict(contact_name, whitelist):
    if not contact_name or len(str(contact_name).strip()) < 2:
        return False, ""

    c_raw = str(contact_name).strip()
    # 红点数字可能被 OCR 并入行首 (如 "3张三")，生成剥离变体一并参与匹配
    raw_variants = [c_raw]
    stripped = re.sub(r'^\d{1,2}(?=[^\d])', '', c_raw).strip()
    if stripped and len(stripped) >= 2 and stripped != c_raw:
        raw_variants.append(stripped)
    # 会话行/顶栏偶尔带未读数后缀（"22-撩人精(2)" / "22-撩人精(2) 在干嘛"），
    # 剥一份变体一起匹配；末尾带预览的，再把预览切掉试一次
    if pcards:
        no_unread = pcards.normalize_title(c_raw)
        if no_unread and no_unread != c_raw:
            raw_variants.append(no_unread)
        m_unread = re.match(r'^(.*?\S)\s*[\(（]\s*\d+\s*[\)）]\s*(.+)$', c_raw)
        if m_unread:
            name_part = m_unread.group(1).strip()
            if name_part and name_part not in raw_variants:
                raw_variants.append(name_part)

    # 群聊防火墙只否决「名字本身就像群」的文本（"吃喝玩乐(25)"、"工作群"）。
    # 但 "张三(3)" / "22-撩人精(2)" 剥掉尾缀能精确命中白名单，那就是未读数，不能误杀 ——
    # 所以命中白名单时用原始文本返回，后续群名判定仍按原名走，不会把群放进来。
    strictly_matched = False
    if pcards:
        for t in whitelist:
            t_clean = str(t or "").strip()
            if not t_clean:
                continue
            # 只有「剥掉未读数尾缀后差一个 (n)」这一种情况才放行；
            # 白名单里明写了 "吃喝玩乐(25)" / "工作群" 的，仍然按群处理，不破原有防线。
            if any(
                normalize_for_match(r) == normalize_for_match(t_clean)
                and not is_group_chat_name(t_clean)
                for r in raw_variants
            ):
                strictly_matched = True
                break

    if not strictly_matched:
        for r in raw_variants:
            # 精准检测群人数标识 (如 "(25)"、" (25)") 与群关键词，排除消息预览中的量词数字 (如 "(2)楼"、"(5)分")
            if is_group_chat_name(r):
                return False, ""

    for r in raw_variants:
        norm_ocr = normalize_for_match(r)
        if not norm_ocr:
            continue

        for t in whitelist:
            t_clean = t.strip()
            if not t_clean:
                continue

            norm_w = normalize_for_match(t_clean)
            if not norm_w:
                continue
            # 1. 严格全等
            if norm_ocr == norm_w:
                return True, t_clean
            # 2. 前缀精准匹配（例如会话行 "张三 好的收到" / "张三 [图片]" 必须以好友名字开头）
            #    越配防护：原文中名字后紧跟普通字符（字母/数字/汉字，即疑似更长的另一个名字，
            #    如白名单"张三"遇到"张三丰"/"张三丰大碗面"）时一律拒绝命中防止串发；
            #    仅当名字后为空白/标点/结尾（真·名+预览分隔）才放行
            if norm_ocr.startswith(norm_w):
                m = re.match(re.escape(t_clean), r, re.IGNORECASE)
                if m:
                    tail = r[m.end():]
                    if tail and re.match(r'[0-9A-Za-z\u4e00-\u9fff]', tail[0]):
                        continue
                return True, t_clean
            # 3. 极小截断容错（仅当 OCR 极轻微少识别尾部 1 个字符时允许）
            if norm_w.startswith(norm_ocr) and len(norm_w) <= len(norm_ocr) + 1:
                return True, t_clean

    return False, ""

def load_contacts(cfg=None):
    """
    读取并清洗 contacts 列表（备注 / 微信号映射）。

    配置形态（两种写法都支持）：
        "contacts": [
            {"remark": "张三", "wxid": "zhangsan_88", "note": "大学室友"},
            "李四"
        ]

    非法项直接丢弃，不抛异常；wxid 统一去空白。
    """
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get("contacts")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            remark, wxid = item.strip(), ""
        elif isinstance(item, dict):
            remark = str(item.get("remark") or item.get("name") or "").strip()
            wxid = str(item.get("wxid") or item.get("wechat_id") or item.get("alias") or "").strip()
        else:
            continue
        if not remark and not wxid:
            continue
        if not remark:
            remark = wxid  # 只填了微信号就当显示名用
        out.append({"remark": remark, "wxid": wxid})
    return out

def effective_whitelist(cfg=None):
    """
    真正生效的白名单 = contacts 里的备注 ∪ 旧 whitelist 字符串数组（去重保序）。

    这样做的好处：老的纯字符串配置完全不用改；想用新结构就加 contacts，
    想两个都用也行。想去掉旧名单，把 whitelist 设成 [] 即可。
    """
    cfg = cfg if cfg is not None else load_config()
    seen = set()
    out = []
    for c in load_contacts(cfg):
        r = c["remark"]
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    for w in (cfg.get("whitelist") or []):
        if isinstance(w, str):
            t = w.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out

def resolve_contact(name, cfg=None):
    """把 OCR 出来的显示名 / 微信号解析成 contact 记录；匹配不到返回 None。"""
    cfg = cfg if cfg is not None else load_config()
    n = str(name or "").strip()
    if not n:
        return None
    contacts = load_contacts(cfg)
    # 1. 先按微信号精确命中（调用方可能直接传 wxid）
    for c in contacts:
        if c["wxid"] and c["wxid"].lower() == n.lower():
            return c
    # 2. 再按备注做模糊匹配（复用防串发的严格规则）
    for c in contacts:
        if is_name_matched_strict(n, [c["remark"]])[0]:
            return c
    return None

def canonical_target(name, cfg=None):
    """把「备注」或「微信号」统一成显示名（备注）；都匹配不上就原样返回。"""
    c = resolve_contact(name, cfg)
    return c["remark"] if c else str(name or "").strip()

def contact_wxid(name, cfg=None):
    c = resolve_contact(name, cfg)
    return c["wxid"] if c else ""

def is_custom_wxid(wxid):
    """
    这个微信号能不能拿去搜索。

    实测结论（用户真机验证）：**用户自己改过的微信号可以搜到**；
    微信官方下发的 `wxid_xxx` 这类**搜不到**（只会在弹出窗里给出
    「网络查找微信号：wxid_xxx」+ 一条网页结果，本地会话不会出现）。
    """
    w = str(wxid or "").strip()
    if not w:
        return False
    return not w.lower().startswith("wxid_")


def contact_search_key(name, cfg=None):
    """
    Ctrl+F 第一次用哪个词去搜。

    自定义微信号（非 `wxid_` 开头）唯一、可搜，优先用；
    官方 `wxid_xxx` 搜不到本地联系人，直接退回备注（调用方还会再做一次降级重试）。
    """
    w = contact_wxid(name, cfg)
    if is_custom_wxid(w):
        return w
    return str(name or "").strip()

def contact_context_id(name, cfg=None):
    """
    给 agent 的会话上下文字：优先微信号。

    微信号是 ASCII，天然避开 DSH A2A「中文 contextId 建会话失败」的坑；
    同时同一好友换备注、换昵称也不会丢聊天记忆。
    """
    w = contact_wxid(name, cfg)
    return w or str(name or "").strip()

def save_contacts(contacts):
    """把 contacts 写回配置文件（保留其它字段不动），原子替换避免写坏。"""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}
    cfg["contacts"] = contacts if isinstance(contacts, list) else []
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    log(f"📇 [通讯录] 已保存 {len(cfg['contacts'])} 条到 {os.path.basename(CONFIG_FILE)}")
    return cfg["contacts"]

def has_red_badge_by_text_anchor(arr, text_box, layout):
    """
    【高精度动态几何锚定红点算法】：
    以 OCR 识别出的好友名字文字 Box 为基准，自适应覆盖头像右上角红点与数字红圈区域！
    """
    total_w = layout["total_w_px"]
    total_h = layout["total_h_px"]
    
    # 获取联系人文字左边缘与顶部坐标
    txt_left_x = min(pt[0] for pt in text_box)
    txt_top_y = min(pt[1] for pt in text_box)
    
    # 拓宽头像右上角红点与数字红圈采样空间 (txt_left_x - 36 ~ txt_left_x - 3)
    x1 = max(0, int(txt_left_x - 36))
    x2 = max(0, int(txt_left_x - 3))
    y1 = max(0, int(txt_top_y - 20))
    y2 = min(total_h, int(txt_top_y + 20))
    
    patch = arr[y1:y2, x1:x2]
    if patch.size == 0:
        return False
        
    # 微信原生高纯度亮红色判定 (红像素点数 >= 15，转 int 彻底杜绝 uint8 回绕溢出)
    pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
    red_mask = (pr >= 200) & (pg <= 110) & (pb <= 110) & (pr > pg + 65)
    return np.sum(red_mask) >= 15

def extract_active_chat_title(res, layout):
    """
    顶栏好友昵称提取（Window-Size Invariant）：
    严格锁定在 X >= chat_start_x 且 Y <= header_h，绝不下沉到聊天气泡区。
    """
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    total_w = layout["total_w_px"]
    
    title_candidates = []
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        t_clean = txt.strip()
        
        # 严格限制在顶栏左侧区域（排除右上角窗口控制按钮与杂字）
        if (chat_start_x - 30) <= cx <= (chat_start_x + int(0.42 * (total_w - chat_start_x))) and cy <= header_h:
            if t_clean in ["最小化", "最大化", "关闭", "设置", "聊天信息", "搜索", "表情", "发送", "文件", "截图", "语音聊天", "视频聊天", "…", "...", "口", "-", "x", "X"]:
                continue
            title_candidates.append((cx, t_clean))
            
    # 按水平从左到右严格排序，确保分段识别的标题词序绝对正确
    title_candidates.sort(key=lambda x: x[0])
    # 未读数有时会被 OCR 单独识别成一个候选（"22-撩人精" + "(2)"），
    # 先逐个剥掉再拼，避免标题变成 "22-撩人精 (2)" 导致白名单匹配失败
    if pcards:
        title_candidates = [(cx, pcards.normalize_title(t)) for cx, t in title_candidates]
        title_candidates = [(cx, t) for cx, t in title_candidates if t]
    title = " ".join(t for _, t in title_candidates)
    # 未读数会跟在标题后面（Linux 4.1.26 实测是 "ai聊天群(3)"），
    # 不剥掉会让 is_name_matched_strict 判定失败，误以为"不是目标会话"。
    # 数字开头的备注（如「22-撩人精」）也必须能原样匹配上，所以统一走 normalize_title。
    if pcards:
        return pcards.normalize_title(title)
    return re.sub(r"\s*[（(]\s*\d+\s*[)）]\s*$", "", title).strip()

# =============================================================================
# 5. CHROMATIC BUBBLE PARSER & MULTI-LINE AGGREGATION
# =============================================================================
BUILTIN_IGNORED_KEYWORDS = [
    "按住鼠标", "语音输入文字", "按住说话", "按Enter发送", "按Ctrl+Enter发送", "发送(S)"
]

# 语音气泡的 OCR 文本形如 `) 2"`、`[语音 4"`、（悬停时还会带上「转文字」按钮）。
# 开启微信「聊天中的语音消息自动转成文字」后，转写文字会紧贴在气泡下方，
# 合并后会变成 `2"，晚上好呀……`。这里统一剥离时长/提示，保留转写正文。
VOICE_DURATION_RE = re.compile(r'^[\s)\]}）】\[]*(?:\[语音\]\s*)?(\d{1,3})\s*["″”]\s*')
VOICE_HINT_WORDS = ("转成文字", "语音转文字", "转文字")
VOICE_ONLY_PLACEHOLDER = "（对方发来一条语音，暂时没转成文字）"


def _normalize_voice_text(text):
    """剥离语音气泡的时长与「转文字」提示。

    返回 (clean_text, is_voice)。is_voice=True 表示这一行来自语音气泡；
    clean_text 为空说明只有时长、没有转写文字（微信还没转出来）。
    """
    t = (text or "").strip()
    if not t:
        return "", False
    original = t
    is_voice = False
    for hint in VOICE_HINT_WORDS:
        if hint in t:
            t = t.replace(hint, " ").strip()
            is_voice = True
    m = VOICE_DURATION_RE.match(t)
    if m:
        t = t[m.end():].strip()
        is_voice = True
    if not is_voice:
        return original, False
    return t.strip(" ，,。;；:："), True

def parse_chat_bubbles_chromatic(res, img_rgb, layout, ignored_keywords=None):
    arr = np.array(img_rgb)
    W, H = img_rgb.size
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    input_h = layout["input_h"]
    
    # 汇总系统内置提示词 + 用户自定义屏蔽词
    all_ignored = list(BUILTIN_IGNORED_KEYWORDS)
    if ignored_keywords and isinstance(ignored_keywords, list):
        for kw in ignored_keywords:
            if kw and str(kw).strip() and str(kw).strip() not in all_ignored:
                all_ignored.append(str(kw).strip())
    
    raw_lines = []
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        c_clean = txt.strip()
        if not c_clean:
            continue
            
        # 排除系统原生 UI 提示词与屏蔽词（如鼠标悬停输入框时弹出的“按住鼠标 语音输入文字”等）
        if any(bad in c_clean for bad in all_ignored):
            continue
            
        # 排除顶栏乱码与非聊天文字 (纯数字如 666, 1, 520, 21 等 100% 完整保留支持)
        if any(bad in c_clean for bad in ["P以H白", "y< o O", "查看更多", "以下为新消息", "置顶聊天"]):
            continue
        
        # 严格限定在主聊天气泡区域 (顶部避开标题栏 + 15px，底部避开输入工具栏)
        if cx > (chat_start_x + 15) and (header_h + 15) < cy < (H - int(input_h * 0.70)):
            if re.match(r'^\d{1,2}:\d{2}$', c_clean) or re.match(r'^\d{1,2}/\d{2}$', c_clean) or re.match(r'^\d{4}-\d{2}-\d{2}', c_clean):
                continue
            if c_clean in ["昨天", "前天", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日", "查看更多消息", "重新编辑", "发送"]:
                continue
            if any(c_clean.startswith(p) for p in ["[转账]", "微信转账", "已收款", "已被接收", "￥", "你撤回了一条消息"]):
                continue
            if "拍了拍" in c_clean:
                continue
                
            # 采样气泡底色 (外扩采样)
            min_x = max(0, int(min(pt[0] for pt in b) - 15))
            max_x = min(W, int(max(pt[0] for pt in b) + 15))
            min_y = max(0, int(min(pt[1] for pt in b) - 8))
            max_y = min(H, int(max(pt[1] for pt in b) + 8))
            
            patch = arr[min_y:max_y, min_x:max_x]
            pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
            green_mask = (pg > pr + 18) & (pg > pb + 18) & (pg > 45)
            has_green_bg = np.sum(green_mask) > 15
            
            box_left = min(pt[0] for pt in b)
            box_right = max(pt[0] for pt in b)

            # 语音气泡：OCR 会读到时长（如 `) 2"`）与「转文字」提示。
            # 剥离后若还剩文字，那是微信自动转写出来的正文；若为空说明还没转出来。
            norm_text, is_voice = _normalize_voice_text(c_clean)
            if not norm_text and not is_voice:
                continue

            # 真彩底色判定：绿底 100% 我发，非绿底偏左侧 100% 好友发
            is_incoming = (not has_green_bg) and (box_left < (chat_start_x + int(0.55 * (W - chat_start_x))))
            raw_lines.append((cy, box_left, box_right, is_incoming, norm_text, is_voice))
            
    raw_lines.sort(key=lambda x: x[0])
    
    # 智能多行气泡垂直合并 (< 40px)
    merged_bubbles = []
    for cy, b_left, b_right, is_inc, text, is_voice in raw_lines:
        if merged_bubbles and is_inc == merged_bubbles[-1]['is_incoming'] and abs(cy - merged_bubbles[-1]['last_y']) < 40:
            if text:
                prev = merged_bubbles[-1]
                prev['text'] = (prev['text'] + "，" + text) if prev['text'] else text
            merged_bubbles[-1]['last_y'] = cy
            merged_bubbles[-1]['box_left'] = min(merged_bubbles[-1]['box_left'], b_left)
            merged_bubbles[-1]['box_right'] = max(merged_bubbles[-1]['box_right'], b_right)
            merged_bubbles[-1]['is_voice'] = merged_bubbles[-1]['is_voice'] or is_voice
        else:
            merged_bubbles.append({
                'is_incoming': is_inc,
                'first_y': cy,
                'last_y': cy,
                'box_left': b_left,
                'box_right': b_right,
                'is_voice': is_voice,
                'text': text
            })

    # 微信「语音消息自动转成文字」会把转写文字渲染在语音气泡正下方（无气泡边框，
    # 垂直间距约 50~95px，超过普通多行气泡的 40px 合并阈值），这里做一次吸附合并。
    for i in range(len(merged_bubbles) - 1):
        cur, nxt = merged_bubbles[i], merged_bubbles[i + 1]
        if (cur.get('is_voice') and not (cur.get('text') or '').strip()
                and nxt.get('is_incoming') and not nxt.get('is_voice')):
            gap = nxt.get('first_y', 0) - cur.get('last_y', 0)
            if 0 < gap <= 100 and abs(nxt.get('box_left', 0) - cur.get('box_left', 0)) <= 80:
                cur['text'] = nxt.get('text') or ''
                cur['last_y'] = nxt.get('last_y', cur['last_y'])
                nxt['text'] = ''   # 已经是上面语音气泡的转写，不再单独算一条消息

    return merged_bubbles

def extract_pending_incoming_messages(chat_bubbles):
    """
    夹层未读穿透回溯算法 (Bubble Penetration Scanner):
    寻找最后一条我方绿底气泡 (last_outgoing_idx)，
    将其下方所有的好友连发气泡全部完整聚合提取！
    """
    if not chat_bubbles:
        return [], ""
        
    last_outgoing_idx = -1
    for i in range(len(chat_bubbles) - 1, -1, -1):
        if not chat_bubbles[i]['is_incoming']:
            last_outgoing_idx = i
            break
            
    if last_outgoing_idx == -1:
        pending_bubbles = [b for b in chat_bubbles if b['is_incoming']]
    else:
        pending_bubbles = [b for b in chat_bubbles[last_outgoing_idx + 1:] if b['is_incoming']]
        
    # 语音气泡若还没被微信转成文字（或转写失败），不要拿时长当正文丢给模型；
    # 用明确占位符让模型自然地请对方重说/打字。
    for b in pending_bubbles:
        if b.get('is_voice') and not (b.get('text') or '').strip():
            b['voice_missing_text'] = True
            b['text'] = VOICE_ONLY_PLACEHOLDER
    pending_bubbles = [b for b in pending_bubbles if (b.get('text') or '').strip()]

    texts = [b['text'].strip() for b in pending_bubbles if b['text'].strip()]
    combined_text = "，".join(texts)
    return pending_bubbles, combined_text


def _retry_voice_transcript(hwnd, layout, ignored_keywords, pending_bubbles, wait_s=1.2, attempts=2):
    """语音气泡只读到时长、还没转出文字时，等微信「自动转成文字」渲染后再扫一次。

    返回 (pending_bubbles, combined_text) 或 None（没等到转写）。
    """
    for _ in range(max(1, attempts)):
        time.sleep(wait_s)
        try:
            refetch = _scan_window(hwnd)
        except Exception:
            return None
        if not refetch:
            return None
        img_new, layout_new, res_new, _title = refetch
        if img_new is None:
            return None
        try:
            bubbles_new = parse_chat_bubbles_chromatic(res_new, img_new, layout_new, ignored_keywords)
            pending_new, combined_new = extract_pending_incoming_messages(bubbles_new)
        except Exception:
            return None
        if pending_new and combined_new and combined_new != VOICE_ONLY_PLACEHOLDER:
            if any((b.get('text') or '').strip() for b in pending_new):
                return pending_new, combined_new
    return None

# =============================================================================
# 6. LLM BRAIN & INSTANT PURE TEXT GENERATION (0 ARTIFICIAL LATENCY)
# =============================================================================
def clean_ai_reply(reply):
    if not reply:
        return ""
    reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL)
    reply = re.sub(r'^[（(][^）)]*[）)]\s*', '', reply)
    reply = re.sub(r'\s*[（(][^）)]*[）)]$', '', reply)
    reply = re.sub(r'[*_#`]', '', reply)
    reply = reply.strip()
    if reply.startswith(('"', '“')) and reply.endswith(('"', '”')):
        reply = reply[1:-1].strip()
    return reply

def generate_ai_reply(sender, message_content):
    cfg = load_config()
    now_time = time.strftime("%H:%M")
    
    custom_prompt = (cfg.get("system_prompt") or "").strip()

    if custom_prompt:
        system_prompt = (
            f"{custom_prompt}\n"
            f"【当前互动好友】：好友【{sender}】，当前时间{now_time}。\n"
            f"【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。"
        )
    else:
        system_prompt = (
            f"你现在是微信账号的主人本人（真实真人），正在微信上与好友【{sender}】聊天。当前时间是{now_time}。\n"
            f"【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。"
        )
    
    oa = cfg.get("openai_api") or {}
    api_base = str(cfg.get("api_url") or oa.get("api_base") or "https://api.xiaomimimo.com/v1").strip().rstrip("/")
    url = api_base if api_base.endswith("/chat/completions") else f"{api_base}/chat/completions"
    key = cfg.get("api_key") or oa.get("api_key") or ""
    model = cfg.get("api_model") or oa.get("model") or "mimo-v2.6-flash"
    temperature = cfg_float(oa, "temperature", 0.7)
    
    if not key or not url:
        log("⚠️ [未配置 API Key] 未检测到 API Key，跳过自动回复。请先在控制中心【AI 引擎设置】中配置有效 Key！")
        return ""
        
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    history = API_CONVERSATION_HISTORY.get(sender, [])
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-8:])
    messages.append({"role": "user", "content": message_content})
    
    payload = {"model": model, "messages": messages, "temperature": temperature}
    try:
        log(f"🤖 正在调用大模型 ({model}) 回复【{sender}】...")
        res = requests.post(url, headers=headers, json=payload, timeout=12)
        if res.status_code == 200:
            reply = res.json()["choices"][0]["message"]["content"]
            cleaned = clean_ai_reply(reply)
            history.append({"role": "user", "content": message_content})
            history.append({"role": "assistant", "content": cleaned})
            API_CONVERSATION_HISTORY[sender] = history[-10:]
            return cleaned
        else:
            log(f"⚠️ [API HTTP {res.status_code}] {res.text[:100]}")
    except Exception as e:
        log(f"⚠️ [API 请求异常] {e}")
        
    return ""

# =============================================================================
# 5.5 AGENT 大脑总线（P0 异步解耦 + P1 DSH Agent 接入）
#    主循环绝不阻塞在 agent 的思考时间上：
#      提交 submit_agent_job()  -> 线程池里问 agent -> 结果进 AGENT_RESULTS
#      取回 poll_agent_results() -> 主线程决定立即发 / 稍后发 / 不发
#    发送永远只在主线程做，因为 send_reply_instant 会抢前台焦点，多线程必串窗。
# =============================================================================
_AGENT_CLIENT = [None]
_AGENT_CLIENT_KEY = [None]
AGENT_OUTBOX = []   # [{"target","texts":[...],"signature","due_ts","attempt","source"}]
MAX_REPLY_SEGMENTS = 4   # 一次决策最多拆几条发（太多像刷屏，真人连发 1-3 条最自然）


def get_agent_client(cfg=None):
    """按配置构建（并缓存）AgentClient 单例。"""
    cfg = cfg if cfg is not None else load_config()
    agent_cfg = cfg.get("agent") or {}
    key = (agent_cfg.get("url"), bool(agent_cfg.get("enabled", True)),
           agent_cfg.get("context_prefix"), agent_cfg.get("contract") or "")
    if _AGENT_CLIENT[0] is None or _AGENT_CLIENT_KEY[0] != key:
        if AgentClient is None:
            log(f"🚨 [Agent 模块缺失] agent_client.py 导入失败: {_AGENT_IMPORT_ERR}")
            return None
        client = AgentClient(cfg)
        client.logger = log
        _AGENT_CLIENT[0] = client
        _AGENT_CLIENT_KEY[0] = key
        log(f"🔌 [Agent 大脑已接入] {client.url}  (每好友独立 context: {client.context_prefix}<昵称>)")
    return _AGENT_CLIENT[0]


def agent_enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool((cfg.get("agent") or {}).get("enabled", True)) and AgentClient is not None


def generate_agent_reply(sender, message_content):
    """同步版：问 agent 要一个决策 dict。失败也会返回合法 dict（含可爱兜底）。"""
    cfg = load_config()
    client = get_agent_client(cfg)
    if client is None:
        return {
            "action": "reply",
            "text": "嘿嘿，我这会儿有点懵，等下再聊~",
            "urgency": "now",
            "reason": "agent_unavailable",
        }
    prompt = (client.prompt_for(sender, message_content)
              if hasattr(client, "prompt_for") else build_prompt(sender, message_content))
    # 有微信号就用微信号当会话键：ASCII 稳定，且换备注/换昵称不丢记忆
    return client.ask(client.context_id_for(contact_context_id(sender, cfg)), prompt, target=sender)


def _drop_outbox_for(target, reason=""):
    """新消息 / 新决策到来时，作废这个好友还没到点的旧计划（防答非所问）。"""
    removed = 0
    for entry in list(AGENT_OUTBOX):
        if entry["target"] == target:
            AGENT_OUTBOX.remove(entry)
            removed += 1
    if removed:
        log(f"🗑️ [撤销待发] 好友【{target}】的 {removed} 条延时计划已作废{f'（{reason}）' if reason else ''}")


_ROUTE_LOGGED = {}


def _log_route_once(target, url, client=None):
    """每个好友第一次走 A2A 时打一行，方便肉眼确认"他走的是哪个关系端点"。"""
    if not target or not url:
        return
    if _ROUTE_LOGGED.get(target) == url:
        return
    _ROUTE_LOGGED[target] = url
    rel = ""
    try:
        rel = (client.relation_of(target) if client is not None else "") or ""
    except Exception:
        rel = ""
    log(f"🔀 [A2A 路由] 好友【{target}】→ {('关系「' + rel + '」→ ') if rel else ''}{url}")


def submit_agent_job(target, message, signature, cfg=None, source="scan"):
    """提交一次「该回什么」的决策请求，立刻返回 job_id（不阻塞主循环）。"""
    cfg = cfg if cfg is not None else load_config()
    client = get_agent_client(cfg)
    if client is None:
        return None

    prompt = (client.prompt_for(target, message)
              if hasattr(client, "prompt_for") else build_prompt(target, message))
    try:
        _log_route_once(target, client.url_for(target), client)
    except Exception:
        pass
    # 有微信号就用微信号当会话键：ASCII 稳定，且换备注/换昵称不丢记忆
    ctx = client.context_id_for(contact_context_id(target, cfg))

    with AGENT_LOCK:
        _AGENT_JOB_SEQ[0] += 1
        job_id = f"j{_AGENT_JOB_SEQ[0]}"
        AGENT_PENDING[job_id] = {
            "target": target,
            "signature": signature,
            "message": message,
            "submit_ts": time.time(),
            "source": source,
        }
        AGENT_INFLIGHT[target] = job_id
        AGENT_LATEST[target] = job_id
    _drop_outbox_for(target, reason="来了新消息，重新决策")

    def _worker():
        t0 = time.time()
        try:
            decision = client.ask(ctx, prompt, target=target)
        except Exception as exc:  # AgentClient 内部已兜底，这里只是最后一道保险
            log(f"🚨 [Agent 线程异常] 好友【{target}】: {exc}")
            decision = {
                "action": "reply",
                "text": "嘿嘿，我这会儿有点懵，等下再聊~",
                "urgency": "now",
                "reason": "agent_unavailable",
            }
        if not isinstance(decision, dict) or not decision.get("action"):
            decision = {
                "action": "reply",
                "text": "嘿嘿，我这会儿有点懵，等下再聊~",
                "urgency": "now",
                "reason": "agent_unavailable",
            }
        cost = time.time() - t0
        with AGENT_LOCK:
            AGENT_RESULTS[job_id] = {
                "target": target,
                "signature": signature,
                "message": message,
                "decision": decision,
                "done_ts": time.time(),
                "cost": cost,
            }

    try:
        AGENT_POOL.submit(_worker)
    except Exception as exc:
        with AGENT_LOCK:
            AGENT_PENDING.pop(job_id, None)
            if AGENT_INFLIGHT.get(target) == job_id:
                AGENT_INFLIGHT.pop(target, None)
        log(f"🚨 [Agent 提交失败] 好友【{target}】: {exc}")
        return None

    log(f"🧠 [已交给 Agent] 好友【{target}】: \"{message}\"（后台思考中，主循环继续巡检）")
    return job_id


def _segment_gap_seconds(segment, cfg=None):
    """两段消息之间的停顿：按字数估算打字时间 + 抖动，像真人在分条发。"""
    cfg = cfg or {}
    base = cfg_float(cfg, "segment_delay_min_seconds", 0.45)
    span = cfg_float(cfg, "segment_delay_span_seconds", 0.9)
    typed = min(span, len(str(segment or "")) * 0.035)
    return base + typed + random.uniform(0.0, 0.35)


def _deliver_reply(target, texts, signature, hwnd, rect, layout, cfg, source="scan"):
    """在主线程把一段或多段话真正发出去（含前缀 / 拟人延迟 / 段间停顿 / 失败重试登记）。

    texts 可以是字符串，也可以是「多段」列表：多段会依次发送，段与段之间按
    字数停一下，效果等同真人连发几条微信，而不是把一大段话挤成一条。
    """
    prefix = cfg.get("reply_prefix", "") if cfg.get("include_prefix", False) else ""
    if isinstance(texts, str):
        texts = [texts]
    payload = [f"{prefix}{t}" for t in (texts or []) if str(t or "").strip()]
    if not payload:
        return False

    reply_delay = cfg_float(cfg, "reply_delay_seconds", 0.0)
    if reply_delay > 0:
        time.sleep(reply_delay)

    for idx, segment in enumerate(payload):
        if idx > 0:
            time.sleep(_segment_gap_seconds(segment, cfg))
        if send_reply_instant(hwnd, rect, layout, target, segment):
            continue

        # 这一段（含后面还没发的）脱靶：登记补偿重试，成功后继续
        remaining = payload[idx:]
        fail_count = SEND_FAIL_RETRY.get(signature, 0) + 1
        SEND_FAIL_RETRY[signature] = fail_count
        if fail_count >= 2:
            LAST_PROCESSED_SIGNATURE[target] = signature
            LAST_PROCESSED_AT[target] = time.time()
            SEND_FAIL_RETRY.pop(signature, None)
            log(f"🚫 [重试超限熔断] 好友【{target}】消息连续 2 次发送脱靶，已放弃该条回复以防死循环。")
            return False

        log(f"⚠️ [发送脱靶记录] 好友【{target}】第 {fail_count} 次发送未成，2 秒后补偿重试（剩余 {len(remaining)} 段）。")
        with AGENT_LOCK:
            AGENT_OUTBOX.append({
                "target": target,
                "texts": remaining,
                "signature": signature,
                "due_ts": time.time() + 2.0,
                "attempt": fail_count,
                "source": f"{source}-retry",
            })
        return False

    LAST_PROCESSED_SIGNATURE[target] = signature

    LAST_PROCESSED_AT[target] = time.time()
    SEND_FAIL_RETRY.pop(signature, None)
    return True


def _schedule_outbox(target, texts, signature, delay_s, source, stickers=None, voices=None):
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t).strip() for t in (texts or []) if str(t or "").strip()]
    stickers = [str(u).strip() for u in (stickers or []) if str(u or "").strip()]
    voices = [str(v).strip() for v in (voices or []) if str(v or "").strip()]
    if delay_s <= 0 or (not texts and not stickers and not voices):
        return False
    with AGENT_LOCK:
        AGENT_OUTBOX.append({
            "target": target,
            "texts": texts,
            "stickers": stickers,
            "voices": voices,
            "signature": signature,
            "due_ts": time.time() + delay_s,
            "attempt": 0,
            "source": source,
        })
    log(f"⏳ [延时待发] 好友【{target}】将在 {delay_s:.0f} 秒后发送: {' / '.join(texts)}")
    return True


_SIG_Y_BUCKET = 40  # 气泡纵向位置按 40px 分桶：OCR 抖动 ±几像素不再当成"新消息"


def _msg_signature(target, combined_text, pending_bubbles):
    """一条"待回复消息"的稳定指纹。

    只取「谁 + 说了什么 + 几条 + 最后一条在哪个高度桶」。
    历史坑：原先用 int(last_y) + len(chat_bubbles)，OCR 每帧抖 1 像素、或时间戳行
    时有时无，签名就变 → 主循环以为来了新消息 → 反复提交 agent → 每轮结论又被
    下一条"更新消息"作废 → 永久 livelock，日志刷屏且永远不回复。
    """
    try:
        last_y = int(pending_bubbles[-1].get("last_y") or 0) if pending_bubbles else 0
    except Exception:
        last_y = 0
    return (str(target), str(combined_text), len(pending_bubbles or []), last_y // _SIG_Y_BUCKET)


def agent_busy_for(target, signature=None):
    """这个好友是否已有 agent 任务在飞 / 已有待发计划。signature 给定时只判同一条消息。"""
    def _same_round(pend_like):
        """同一条消息的判定：签名全等，或"收件人+原文"相同（签名可能因抖动而变）。"""
        if pend_like is None:
            return False
        if pend_like.get("signature") == signature:
            return True
        if signature is not None and len(signature) >= 2:
            return (pend_like.get("target") == signature[0]
                    and pend_like.get("message") == signature[1])
        return False

    with AGENT_LOCK:
        job_id = AGENT_INFLIGHT.get(target)
        if job_id:
            pend = AGENT_PENDING.get(job_id)
            if signature is None or _same_round(pend):
                return True
        for entry in AGENT_OUTBOX:
            if entry["target"] == target:
                if signature is None or _same_round(entry):
                    return True
    return False


def sanitize_outgoing_text(text):
    """把 Agent 回复里不该出现在微信里的东西清掉。

    背景：DSH profile 里装了 dsh-nekowhale-stickers 等插件，
    Agent 会往回复里塞 markdown 表情包图片（![名字](http://.../x.gif)），
    微信里发出去就是一行乱码。这里统一剥掉 markdown 图片/链接/代码块标记。
    """
    t = str(text or "")
    if not t:
        return ""
    # markdown 图片：![alt](url)  以及裸的图片链接行
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    # 表情插件标记（dsh-meme 正常会被解析成图片；漏到这里就别把方括号原文发给好友）
    t = re.sub(r"\[表情[:：]\s*[^\]\n]{1,80}\]", "", t)
    # markdown 链接保留文字： [文字](url) -> 文字
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    # 裸 URL（图片/接口地址），整行去掉
    t = re.sub(r"^\s*https?://\S+\s*$", "", t, flags=re.M)
    # 代码块/行内代码标记
    t = re.sub(r"```[a-zA-Z]*", "", t)
    t = t.replace("`", "")
    # 空行压缩
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _decision_segments(decision):
    """把 agent 决策归一成「要逐条连发的消息列表」。

    DSH 的回复长度受限，长内容会挤成一条、读起来累。新契约让模型返回
    `segments` 数组，这里逐条发送；同时兼容旧契约（只有 text）和模型
    不听话时用 ||| 分隔的写法。
    """
    segs = []
    raw = (decision or {}).get("segments")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("text") or item.get("content") or ""
            seg = sanitize_outgoing_text(item)
            if seg:
                segs.append(seg)
    if not segs:
        text = sanitize_outgoing_text((decision or {}).get("text"))
        if text:
            parts = [p for p in re.split(r"\s*\|\|\|\s*", text) if p.strip()]
            segs = [sanitize_outgoing_text(p) for p in parts]
            segs = [s for s in segs if s]
    # 相邻重复去掉（模型偶尔一句话写两遍）
    deduped = []
    for seg in segs:
        if deduped and seg == deduped[-1]:
            continue
        deduped.append(seg)
    return deduped[:MAX_REPLY_SEGMENTS]


# ---------------------------------------------------------------------------
# 表情包（表情来自 DSH 的表情插件：形如 http://127.0.0.1:25675/x-nekowhale-stickers/s/xxx.gif）
# dsh-meme 插件（Web 通道）走 /dsh-memes/... 路由，另有 [表情: 描述] 标记写法。
# ---------------------------------------------------------------------------
STICKER_PATH_MARK = "/x-nekowhale-stickers/"
STICKER_PATH_MARKS = ("/x-nekowhale-stickers/", "/dsh-memes/")
DSH_MEME_BASE = "http://127.0.0.1:25675"
DSH_MEME_API = DSH_MEME_BASE + "/dsh-memes-api?packId=all"
MEME_INDEX_TTL = 60.0
STICKER_MAX_BYTES = 12 * 1024 * 1024
STICKER_TMP_DIR = "/tmp/wx_bot_stickers"
_STICKER_LOGGED = set()
_MEME_INDEX = {"at": 0.0, "rows": []}


def _meme_index(force=False):
    """拉 dsh-meme 的图库索引（60 秒缓存）。失败返回上次结果（可能为空）。"""
    now = time.time()
    if not force and _MEME_INDEX["rows"] and now - _MEME_INDEX["at"] < MEME_INDEX_TTL:
        return _MEME_INDEX["rows"]
    try:
        req = urllib.request.Request(DSH_MEME_API, headers={"User-Agent": "wechat-ai-bot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        rows = []
        for item in (data.get("memes") or []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").lstrip("/")
            if not path:
                continue
            # 插件给的 url 已经带 packId（/dsh-memes/<pack>/memes/...），优先用它
            url = str(item.get("url") or "").strip()
            if not url.lower().startswith("/dsh-memes/"):
                url = "/dsh-memes/" + path
            rows.append({
                "path": path,
                "url": url,
                "caption": str(item.get("caption") or ""),
                "tag": str(item.get("tag") or ""),
                "keywords": str(item.get("keywords") or ""),
            })
        if rows:
            _MEME_INDEX["rows"] = rows
            _MEME_INDEX["at"] = now
        return rows or _MEME_INDEX["rows"]
    except Exception as exc:
        if "meme_index" not in _STICKER_LOGGED:
            _STICKER_LOGGED.add("meme_index")
            log(f"⚠️ [表情] 读取 dsh-meme 图库失败（稍后重试）: {exc}")
        return _MEME_INDEX["rows"]


def _resolve_meme_caption(caption):
    """把「[表情: 描述]」里的描述换成 dsh-meme 的图片路径（找不到返回 None）。"""
    target = str(caption or "").strip()
    if not target:
        return None
    # 描述可能是候选原文（caption），也可能带分类/文件名，先整串再拆词
    rows = _meme_index()
    if not rows:
        rows = _meme_index(force=True)
    if not rows:
        return None

    def norm(text):
        return re.sub(r"\s+", "", str(text or "")).lower()

    want = norm(target)
    exact, prefix, contains = [], [], []
    for row in rows:
        caption = norm(row["caption"])
        if not caption:
            continue
        if caption == want:
            exact.append(row)
        elif caption.startswith(want) or want.startswith(caption):
            prefix.append(row)
        elif want in caption or caption in want:
            contains.append(row)
    for bucket in (exact, prefix, contains):
        if bucket:
            return bucket[0]["url"]
    # 描述里只有关键词（「无语」「生气」）时按关键词/分类再找一次
    for row in rows:
        words = [norm(w) for w in re.split(r"[\s,，、/]+", (row["caption"] + " " + row["keywords"] + " " + row["tag"]))]
        if want and any(w and w in want for w in words):
            return row["url"]
    return None


def _meme_ref_to_url(ref):
    """把决策里的表情引用归一成可下载的 URL：/dsh-memes/...、绝对路径、http(s) 链接。"""
    text = str(ref or "").strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        return text if any(mark in text for mark in STICKER_PATH_MARKS) else None
    if text.startswith("/dsh-memes/"):
        return DSH_MEME_BASE + text
    if text.startswith("memes/") or text.startswith("dsh-memes/"):
        return DSH_MEME_BASE + "/dsh-memes/" + text
    if text.startswith("/"):
        # 插件候选里的绝对路径：映射回 /dsh-memes/ 路由（只允许图库目录下的文件）
        rel = None
        for root in (
            os.path.expanduser("~/.dsh/meme-packs"),
            "/home/long/.dsh/profiles/web/node_modules/dsh-meme/memes",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules", "dsh-meme", "memes"),
        ):
            try:
                real_root = os.path.realpath(root)
                real = os.path.realpath(text)
                if real == real_root or real.startswith(real_root + os.sep):
                    # 图库目录结构：<pack>/memes/<tag>/<file>.jpg → /dsh-memes/<pack>/memes/...
                    rel = "/dsh-memes/" + os.path.relpath(real, real_root).replace(os.sep, "/")
                    break
            except Exception:
                continue
        return DSH_MEME_BASE + rel if rel else None
    return None


def _decision_stickers(decision):
    """从决策里取出可发送的表情 URL（只认本机/局域网的表情插件链接）。"""
    out = []
    raw = (decision or {}).get("stickers")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            url = _meme_ref_to_url(item)
            if url and url not in out:
                out.append(url)
    # dsh-meme：模型给路径（/dsh-memes/...）或给描述（[表情: 描述]）都要能发
    refs = list((decision or {}).get("meme_paths") or []) + list((decision or {}).get("meme_captions") or [])
    for ref in refs:
        url = _meme_ref_to_url(ref)
        if not url:
            url = _meme_ref_to_url(_resolve_meme_caption(ref) or "")
        if url and url not in out:
            out.append(url)
    return out[:2]


def _voice_cfg(cfg=None):
    """读取语音配置（agent.voice 或顶层 voice），缺省不开以免打扰。"""
    cfg = cfg or {}
    raw = cfg.get("voice") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        agent = cfg.get("agent") if isinstance(cfg, dict) else None
        raw = agent.get("voice") if isinstance(agent, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or "bubble").strip().lower()
    if mode not in ("bubble", "file"):
        mode = "bubble"
    return {
        "enabled": bool(raw.get("enabled", False)),
        "max_chars": int(raw.get("max_chars") or 120),
        "voice": str(raw.get("voice") or ""),
        "instruction": str(raw.get("instruction") or ""),
        "edge_fallback": bool(raw.get("edge_fallback", True)),
        "edge_voice": str(raw.get("edge_voice") or "zh-CN-XiaoxuanNeural"),
        "timeout": float(raw.get("timeout") or 300.0),
        "mode": mode,
        "fallback_file": bool(raw.get("fallback_file", True)),
        "virtual_source": str(raw.get("virtual_source") or "wx_tts_source"),
        "virtual_sink": str(raw.get("virtual_sink") or "wx_tts_sink"),
        "record_tail": float(raw.get("record_tail") or 0.45),
        "min_record_seconds": float(raw.get("min_record_seconds") or 1.2),
    }


def _decision_voices(decision):
    """从决策里取出要合成的语音文本（已按配置过滤长度/开关）。"""
    raw = (decision or {}).get("voice_texts")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        t = str(item or "").strip()
        if not t:
            continue
        t = re.sub(r"[\[\]\r\n]+", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if t and t not in out:
            out.append(t)
    return out[:1]


def _configured_agent_hosts(cfg=None):
    """表情图的合法来源主机：回环 + 配置里出现过的 A2A 主机。"""
    from urllib.parse import urlparse
    hosts = {"127.0.0.1", "localhost", "::1"}
    agent = (cfg or {}).get("agent") if isinstance(cfg, dict) else None
    raw_urls = []
    if isinstance(agent, dict):
        raw_urls.append(agent.get("url"))
        raw_urls.append(agent.get("card_url"))
        rel = agent.get("relation_urls")
        if isinstance(rel, dict):
            raw_urls.extend(rel.values())
        elif isinstance(rel, list):
            for item in rel:
                if isinstance(item, dict):
                    raw_urls.append(item.get("url"))
    for raw in raw_urls:
        try:
            host = (urlparse(str(raw or "")).hostname or "").lower()
        except Exception:
            continue
        if host:
            hosts.add(host)
    return hosts


def download_sticker(url, cfg=None):
    """下载表情到本地临时文件；失败返回 None。"""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in _configured_agent_hosts(cfg):
            log(f"⚠️ [表情] 拒绝下载非白名单主机的表情: {host}")
            return None
        # 统一走回环，避免 nginx/Host 校验差异
        netloc = f"127.0.0.1:{parsed.port or 25675}"
        safe_url = urlunparse(("http", netloc, parsed.path, "", parsed.query, ""))
        os.makedirs(STICKER_TMP_DIR, exist_ok=True)
        tail = re.sub(r"[^0-9a-zA-Z]+", "", parsed.path.split("/")[-1]) or "sticker"
        req = urllib.request.Request(safe_url, headers={"User-Agent": "wechat-ai-bot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read(STICKER_MAX_BYTES + 1)
        if not data or len(data) > STICKER_MAX_BYTES:
            log(f"⚠️ [表情] 下载内容为空或过大（{len(data)} 字节）")
            return None
        if data[:3] == b"GIF":
            ext = ".gif"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:2] == b"\xff\xd8":
            ext = ".jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            log("⚠️ [表情] 下载内容不是图片，跳过")
            return None
        # 扩展名按真实内容定（dsh-meme 图库里有 jpg/png/webp，GdkPixbuf 靠内容识别，扩展名只影响排查）
        path = os.path.join(STICKER_TMP_DIR, f"{int(time.time()*1000)}_{tail}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        if ext not in (".gif", ".png", ".jpg"):
            # GdkPixbuf 在没有 webp loader 的机器上认不出 webp，先转成 PNG 再进剪贴板
            try:
                with Image.open(path) as im:
                    png_path = os.path.splitext(path)[0] + ".png"
                    im.convert("RGBA" if "A" in im.getbands() else "RGB").save(png_path, "PNG")
                os.remove(path)
                return png_path
            except Exception as exc:
                log(f"⚠️ [表情] 图片格式 {ext} 转 PNG 失败: {str(exc)[:80]}")
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None
        return path
    except Exception as exc:
        log(f"⚠️ [表情] 下载失败: {str(exc)[:120]}")
        return None


def _send_sticker_image(hwnd, layout, target, path):
    """把本地图片贴进当前聊天并发出去（渲染预览 → Enter 发送）。"""
    if not lx:
        return False

    # --- 发表情同样要先核验顶栏：绝不把表情发错人 ---
    img_now, _, _ = grab_wechat_window(hwnd)
    if img_now is None:
        log("⚠️ [表情] 抓不到微信画面，跳过")
        return False
    hidden = bool(lx.is_hidden(hwnd))
    _, _, _, cur_title = _scan_window(hwnd)
    if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
        log(f"⚠️ [表情纠偏] 当前顶栏【{cur_title or '未知'}】不是【{target}】，先切过去")
        if hidden:
            lx.show_window(hwnd)
            time.sleep(0.30)
        switched = _linux_search_and_open(hwnd, target)
        if hidden:
            lx.hide_window(hwnd)
            time.sleep(0.30)
        if not switched:
            log(f"🚫 [表情中止] 无法锁定会话【{target}】，放弃发这张表情")
            return False
        time.sleep(0.40)
        img_now, _, _ = grab_wechat_window(hwnd)
        if img_now is None:
            return False
        _, _, _, cur_title = _scan_window(hwnd)
        if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
            log(f"🚫 [表情中止] 切换后顶栏仍是【{cur_title or '未知'}】")
            return False

    cur_W, cur_H = img_now.size
    cur_layout = compute_wechat_layout(cur_W, cur_H, img_now)

    if hidden:
        if not lx.focus_window(hwnd):
            log("🚫 [表情] 离屏模式下拿不到键盘焦点，跳过")
            return False
    else:
        if not activate_wechat(hwnd):
            log("🚫 [表情] 微信没拿到键盘焦点，跳过（防止图片贴到别的窗口）")
            return False
        input_safe_x = int(0.55 * cur_W)
        input_safe_y = int(cur_H - cur_layout["input_h"] * 0.55)
        post_click_client_point(hwnd, input_safe_x, input_safe_y)
        time.sleep(0.10)
        if not activate_wechat(hwnd):
            log("🚫 [表情] 点击输入框后焦点脱失，跳过")
            return False

    if not lx.clipboard_copy_image(path):
        log("⚠️ [表情] 图片写剪贴板失败")
        return False
    time.sleep(0.25)
    _key_combo(0x56)                 # Ctrl+V 贴图
    time.sleep(0.9)                  # 等出现图片预览
    _key_combo(0x0D, ctrl=False)     # Enter 发送
    time.sleep(0.9)
    return True


def _deliver_stickers(target, urls, signature, hwnd, rect, layout, cfg, source="agent"):
    """逐张发表情；失败不重试（宁可少一张，也不重复刷屏）。"""
    sent = 0
    for url in urls or []:
        path = download_sticker(url, cfg)
        if not path:
            continue
        try:
            if _send_sticker_image(hwnd, layout, target, path):
                sent += 1
                log(f"🎨 [表情] 已发给【{target}】: {url.rsplit('/', 1)[-1]}")
            time.sleep(0.5)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return sent > 0


def _wait_popup(timeout=2.0, want=True):
    """等微信弹出「选择文件」对话框（或等它消失）。返回 True/False。"""
    if not lx:
        return False
    end = time.time() + max(0.2, timeout)
    while time.time() < end:
        try:
            popup = (lx.probe() or {}).get("popup") or 0
        except Exception:
            popup = 0
        if bool(popup) == bool(want):
            return True
        time.sleep(0.08)
    return False


def _pactl(*args, timeout=12.0):
    """调用 pactl，返回 (ok, stdout)。系统没装 pulseaudio-utils 时静默失败。"""
    import subprocess
    try:
        r = subprocess.run(["pactl"] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception:
        return False, ""


def _audio_sources():
    ok, out = _pactl("list", "short", "sources")
    names = []
    if ok:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                names.append(parts[1])
    return names


def _ensure_virtual_mic(sink="wx_tts_sink", source="wx_tts_source", log_fn=None):
    """准备一个「虚拟麦克风」：null sink + 它的 monitor 重映射成 source。

    语音气泡方案：微信从默认录音设备抓音，我们把 TTS 音频 paplay 进 null sink，
    微信录到的就是这段音频；发完把默认设备还回去。
    """
    log_fn = log_fn or log
    sources = _audio_sources()
    if source in sources:
        return True
    if not any(s.startswith(sink) for s in sources) and (sink + ".monitor") not in sources:
        ok, _ = _pactl("load-module", "module-null-sink", "sink_name=%s" % sink,
                       "sink_properties=device.description=%s" % sink)
        if not ok:
            log_fn("⚠️ [语音] 无法创建虚拟扬声器（pactl load-module null-sink 失败）")
            return False
    ok, _ = _pactl("load-module", "module-remap-source",
                   "master=%s.monitor" % sink, "source_name=%s" % source,
                   "source_properties=device.description=%s" % source)
    if not ok:
        log_fn("⚠️ [语音] 无法创建虚拟麦克风（pactl load-module remap-source 失败）")
        return False
    time.sleep(0.35)
    return source in _audio_sources()


def _physical_source(exclude=()):
    """挑一个真实麦克风源（非虚拟/非 monitor），用于恢复默认录音设备。"""
    skip = set(exclude) | {"wx_tts_source", "wx_tts_sink.monitor"}
    for name in _audio_sources():
        if name in skip or name.endswith(".monitor"):
            continue
        return name
    return ""


def _default_source():
    ok, out = _pactl("get-default-source")
    return out if ok else ""


def _set_default_source(name):
    if not name:
        return False
    ok, _ = _pactl("set-default-source", name)
    return ok


def _press_client_point(hwnd, client_x, client_y):
    if IS_LINUX:
        return bool(lx and lx.press_client(hwnd, client_x, client_y))
    post_click_client_point(hwnd, client_x, client_y)
    return True


def _release_client_point(hwnd, client_x, client_y):
    if IS_LINUX:
        return bool(lx and lx.release_client(hwnd, client_x, client_y))
    return True


def _voice_button_points(W, H):
    """返回 (语音按钮坐标, 按住说话条中心坐标)，按窗口比例算。

    实测 wx 4.1.13.26（1008x756）：发语音按钮 (860,706)、按住条 (770,700)。
    """
    icon = (int(0.853 * W), int(0.934 * H))
    bar = (int(0.764 * W), int(0.926 * H))
    return icon, bar


def _send_voice_bubble(hwnd, layout, target, path, cfg=None):
    """发一条真·语音气泡：虚拟麦克风喂 TTS 音频 + 按住说话条。

    流程：核验会话 -> 切默认录音设备为虚拟麦克风 -> 点「发语音」
          -> 按住语音条并播放音频 -> 松开（微信自动发送）-> 恢复默认设备。
    """
    if not lx or not path or not os.path.isfile(path):
        return False
    import subprocess

    vcfg = _voice_cfg(cfg)
    sink = vcfg["virtual_sink"]
    source = vcfg["virtual_source"]

    img_now, _, _ = grab_wechat_window(hwnd)
    if img_now is None:
        log("⚠️ [语音] 抓不到微信画面，跳过")
        return False

    hidden = bool(lx.is_hidden(hwnd))
    if hidden:
        log("🖥️ [语音] 语音气泡需要按住说话条，临时召回微信（发完重新隐藏）")
        lx.show_window(hwnd)
        time.sleep(0.45)

    prev_source = _default_source()
    player = None
    pressed = False
    try:
        # 顶栏核验 + 必要时切换会话
        _, _, _, cur_title = _scan_window(hwnd)
        if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
            switched = _linux_search_and_open(hwnd, target)
            if not switched:
                log(f"🚫 [语音] 无法锁定会话【{target}】，放弃发这条语音")
                return False
            time.sleep(0.5)
            img_now, _, _ = grab_wechat_window(hwnd)
            if img_now is None:
                return False
            _, _, _, cur_title = _scan_window(hwnd)
            if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
                log(f"🚫 [语音] 切换后顶栏仍是【{cur_title or '未知'}】")
                return False

        if not activate_wechat(hwnd):
            log("🚫 [语音] 微信没拿到键盘焦点，跳过")
            return False

        if not _ensure_virtual_mic(sink, source):
            return False
        if prev_source and prev_source != source and not _set_default_source(source):
            log("⚠️ [语音] 切换默认录音设备失败")
            return False
        time.sleep(0.25)

        cur_W, cur_H = img_now.size
        icon_pt, bar_pt = _voice_button_points(cur_W, cur_H)
        post_click_client_point(hwnd, icon_pt[0], icon_pt[1])   # 点「发语音」
        time.sleep(0.55)

        duration = voice_tts.duration_seconds(path)
        hold = max(vcfg["min_record_seconds"], duration + vcfg["record_tail"])

        _press_client_point(hwnd, bar_pt[0], bar_pt[1])
        pressed = True
        time.sleep(0.30)
        try:
            player = subprocess.Popen(["paplay", "-d", sink, os.path.abspath(path)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"⚠️ [语音] 播放失败（paplay）: {type(e).__name__}: {e}")
        time.sleep(max(0.1, hold))
        _release_client_point(hwnd, bar_pt[0], bar_pt[1])
        pressed = False
        time.sleep(0.9)
        log(f"🎙️ [语音] 已发语音气泡【{target}】：{duration:.1f}s 音频，按住 {hold:.1f}s")
        return True
    except Exception as e:
        log(f"⚠️ [语音] 气泡发送异常: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            if pressed:
                _release_client_point(hwnd, bar_pt[0], bar_pt[1])
        except Exception:
            pass
        try:
            if player is not None and player.poll() is None:
                player.terminate()
        except Exception:
            pass
        restore_to = prev_source
        if not restore_to or restore_to == source:
            restore_to = _physical_source(exclude=(source,))
        if restore_to and restore_to != source:
            _set_default_source(restore_to)
        if hidden:
            time.sleep(0.25)
            try:
                lx.hide_window(hwnd)
            except Exception:
                pass


def _send_voice_file(hwnd, layout, target, path, cfg=None):
    """把本地音频文件当「文件消息」发给当前会话（WeChat Linux 无语音气泡，走文件发送）。

    流程：核验顶栏 -> 点「文件」图标 -> 等文件选择框 -> 粘贴绝对路径 -> Enter
          -> 等附件卡出现 -> Enter 发送。
    离屏隐藏模式下需要临时召回窗口（文件框必须可见才能操作），发完自动重新隐藏。
    """
    if not lx or not path or not os.path.isfile(path):
        return False

    img_now, _, _ = grab_wechat_window(hwnd)
    if img_now is None:
        log("⚠️ [语音] 抓不到微信画面，跳过")
        return False

    hidden = bool(lx.is_hidden(hwnd))
    if hidden:
        log("🖥️ [语音] 文件发送需要点文件框，临时召回微信（发完重新隐藏）")
        lx.show_window(hwnd)
        time.sleep(0.45)

    try:
        # 顶栏核验 + 必要时切换会话
        _, _, _, cur_title = _scan_window(hwnd)
        if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
            log(f"⚠️ [语音纠偏] 当前顶栏【{cur_title or '未知'}】不是【{target}】，先切过去")
            switched = _linux_search_and_open(hwnd, target)
            if not switched:
                log(f"🚫 [语音中止] 无法锁定会话【{target}】，放弃发这条语音")
                return False
            time.sleep(0.45)
            img_now, _, _ = grab_wechat_window(hwnd)
            if img_now is None:
                return False
            _, _, _, cur_title = _scan_window(hwnd)
            if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
                log(f"🚫 [语音中止] 切换后顶栏仍是【{cur_title or '未知'}】")
                return False

        if not activate_wechat(hwnd):
            log("🚫 [语音] 微信没拿到键盘焦点，跳过（防止文件框被别的窗口接管）")
            return False

        cur_W, cur_H = img_now.size
        cur_layout = compute_wechat_layout(cur_W, cur_H, img_now)

        # 「文件」图标：输入栏左下角第二个，约占宽 60%、距底部 6.5%
        icon_x = int(0.600 * cur_W)
        icon_y = cur_H - int(0.065 * cur_H)
        post_click_client_point(hwnd, icon_x, icon_y)
        time.sleep(0.30)
        if not _wait_popup(2.5, want=True):
            log("⚠️ [语音] 文件选择框没有出现，放弃本次发送")
            return False
        time.sleep(0.30)

        if not safe_clipboard_copy(os.path.abspath(path), retries=3, delay=0.03):
            log("⚠️ [语音] 路径写剪贴板失败")
            _key_combo(0x1B, ctrl=False)   # Esc 关掉文件框
            return False
        time.sleep(0.25)
        _key_combo(0x56)                   # Ctrl+V 粘贴路径
        time.sleep(0.35)
        _key_combo(0x0D, ctrl=False)       # Enter 确认选择
        time.sleep(1.0)
        if not _wait_popup(2.0, want=False):
            log("⚠️ [语音] 文件框没关掉（路径可能没被接受），放弃发送")
            _key_combo(0x1B, ctrl=False)
            return False
        time.sleep(0.45)
        _key_combo(0x0D, ctrl=False)       # Enter 发送附件
        time.sleep(1.2)
        log(f"🎙️ [语音] 已发给【{target}】: {os.path.basename(path)}")
        return True
    except Exception as e:
        log(f"⚠️ [语音] 发送异常: {type(e).__name__}: {e}")
        return False
    finally:
        if hidden:
            time.sleep(0.25)
            try:
                lx.hide_window(hwnd)
            except Exception:
                pass


def _deliver_voices(target, voice_texts, signature, hwnd, rect, layout, cfg, source="agent"):
    """逐条合成语音并发送；失败只记日志，不重试（避免重复发同一句话）。"""
    if not voice_texts:
        return False
    vcfg = _voice_cfg(cfg)
    if not vcfg["enabled"]:
        log("ℹ️ [语音] 收到语音回复，但配置里 voice.enabled=false，跳过合成")
        return False
    sent = False
    for said in voice_texts[:1]:
        said = str(said or "").strip()
        if not said:
            continue
        if len(said) > vcfg["max_chars"]:
            said = said[: vcfg["max_chars"]]
        log(f"🎙️ [语音] 正在合成 {len(said)} 字…（时长取决于 TTS 服务算力）")
        try:
            info = voice_tts.synthesize(
                said,
                voice=vcfg["voice"],
                instruction=vcfg["instruction"],
                fallback_voice=vcfg["edge_voice"],
                allow_edge_fallback=vcfg["edge_fallback"],
                timeout=vcfg["timeout"],
            )
        except Exception as e:
            log(f"⚠️ [语音] 合成失败（降级为不发语音）: {e}")
            continue
        path = info.get("path") or ""
        try:
            ok = False
            if vcfg.get("mode") == "bubble":
                ok = _send_voice_bubble(hwnd, layout, target, path, cfg)
                if not ok and vcfg.get("fallback_file"):
                    log("ℹ️ [语音] 气泡发送失败，回退成音频文件")
                    ok = _send_voice_file(hwnd, layout, target, path, cfg)
            else:
                ok = _send_voice_file(hwnd, layout, target, path, cfg)
            if ok:
                sent = True
                log(f"🎙️ [语音] 已合成并发送（{info.get('engine')}）: {said[:24]}{'…' if len(said) > 24 else ''}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        time.sleep(0.5)
    return sent


def poll_agent_results(hwnd, rect=None, layout=None):
    """
    主循环每个心跳调用一次：
      1) 取回 agent 决策 -> 立即发 / 延时发 / 不发
      2) 检查到点的延时队列 -> 发送
    返回 True 表示本轮已经发过消息（调用方可以跳过本轮扫描）。
    """
    if hwnd is None:
        return False
    cfg = load_config()
    client = get_agent_client(cfg)
    did_send = False

    # ---------- 1) 取回 agent 决策 ----------
    with AGENT_LOCK:
        ready = list(AGENT_RESULTS.items())
        AGENT_RESULTS.clear()

    for job_id, item in ready:
        target = item["target"]
        signature = item["signature"]
        decision = item["decision"]
        with AGENT_LOCK:
            AGENT_PENDING.pop(job_id, None)
            if AGENT_INFLIGHT.get(target) == job_id:
                AGENT_INFLIGHT.pop(target, None)

        action = str(decision.get("action") or "reply").lower()
        segments = _decision_segments(decision)
        stickers = _decision_stickers(decision)
        voices = _decision_voices(decision)
        text = " / ".join(segments)          # 仅用于日志展示
        if stickers:
            text = (text + "  🎨" if text else "🎨").strip()
        if voices:
            text = (text + "  🎙️" if text else "🎙️").strip()
        urgency = str(decision.get("urgency") or "now").lower()
        reason = str(decision.get("reason") or "")
        cost = item.get("cost") or 0.0

        # 过期结果丢弃：该好友已提交过更新的任务，这条旧结论会答非所问
        with AGENT_LOCK:
            latest_job = AGENT_LATEST.get(target)
            latest_pend = AGENT_PENDING.get(latest_job) if latest_job else None
        if latest_job and latest_job != job_id:
            # 只有"更新的那条消息内容真的不一样"才作废本结论。
            # 历史坑：原先只要存在更新的 job 就丢弃，当 agent 比主循环慢时，
            # 每个结论落地时都已被下一次提交作废 → 永远不回复、日志刷屏。
            newer_text = (latest_pend or {}).get("message")
            this_text = item.get("message")
            if newer_text is not None and this_text is not None and newer_text == this_text:
                # 内容相同，只是签名抖动造成的重复提交：本结论依然有效，继续走发送
                pass
            else:
                log(f"🗑️ [丢弃过期决策] 好友【{target}】已有更新消息在决策中，本轮结论作废")
                continue

        if action == "ignore":
            LAST_PROCESSED_SIGNATURE[target] = signature
            LAST_PROCESSED_AT[target] = time.time()
            log(f"🙊 [Agent 判定不回] 好友【{target}】: {reason}  (耗时 {cost:.1f}s)")
            continue

        if action == "defer":
            LAST_PROCESSED_SIGNATURE[target] = signature
            LAST_PROCESSED_AT[target] = time.time()
            delay = client.urgency_to_seconds(urgency) if client else 60.0
            if segments or stickers or voices:
                _schedule_outbox(target, segments, signature, delay, "defer", stickers=stickers, voices=voices)
            else:
                log(f"⏸️ [Agent 判定稍后再说] 好友【{target}】: {reason}")
            continue

        # action == reply
        if not segments and not stickers and not voices:
            log(f"⚠️ [Agent 空回复] 好友【{target}】无正文也无表情，跳过")
            LAST_PROCESSED_SIGNATURE[target] = signature
            LAST_PROCESSED_AT[target] = time.time()
            continue

        delay = client.urgency_to_seconds(urgency) if client else 0.0
        if delay > 0:
            LAST_PROCESSED_SIGNATURE[target] = signature
            LAST_PROCESSED_AT[target] = time.time()
            _schedule_outbox(target, segments, signature, delay, "agent", stickers=stickers, voices=voices)
            continue

        log(f"💬 [Agent 决策] 好友【{target}】: {text}  (urgency={urgency}, 耗时 {cost:.1f}s)")
        if segments and _deliver_reply(target, segments, signature, hwnd, rect, layout, cfg, source="agent"):
            did_send = True
        if stickers:
            time.sleep(0.4)
            if _deliver_stickers(target, stickers, signature, hwnd, rect, layout, cfg, source="agent"):
                did_send = True
        if voices:
            time.sleep(0.4)
            if _deliver_voices(target, voices, signature, hwnd, rect, layout, cfg, source="agent"):
                did_send = True

    # ---------- 2) 到点的延时队列 ----------
    now = time.time()
    with AGENT_LOCK:
        due = [e for e in AGENT_OUTBOX if e["due_ts"] <= now]
        for e in due:
            AGENT_OUTBOX.remove(e)

    for entry in due:
        target = entry["target"]
        # 该好友已有更新的消息/决策在飞 -> 作废这条旧计划
        if target in AGENT_INFLIGHT:
            log(f"🗑️ [撤销待发] 好友【{target}】有更新消息在决策中，丢弃旧计划")
            continue
        pending_texts = entry.get("texts") or entry.get("text") or []
        if pending_texts and _deliver_reply(target, pending_texts, entry["signature"], hwnd, rect, layout, cfg, source=entry.get("source", "outbox")):
            did_send = True
        pending_stickers = entry.get("stickers") or []
        if pending_stickers and _deliver_stickers(target, pending_stickers, entry["signature"], hwnd, rect, layout, cfg, source=entry.get("source", "outbox")):
            did_send = True
        pending_voices = entry.get("voices") or []
        if pending_voices and _deliver_voices(target, pending_voices, entry["signature"], hwnd, rect, layout, cfg, source=entry.get("source", "outbox")):
            did_send = True

    return did_send


def _handle_decision_sync(target, message, signature, decision, hwnd, rect, layout, cfg):
    """线程池提交失败时的同步兜底：直接处理决策（会短暂阻塞主循环，仅在异常路径发生）。"""
    action = str((decision or {}).get("action") or "reply").lower()
    segments = _decision_segments(decision)
    stickers = _decision_stickers(decision)
    voices = _decision_voices(decision)
    text = " / ".join(segments)          # 仅用于日志展示
    urgency = str((decision or {}).get("urgency") or "now").lower()
    reason = str((decision or {}).get("reason") or "")

    if action == "ignore":
        LAST_PROCESSED_SIGNATURE[target] = signature
        LAST_PROCESSED_AT[target] = time.time()
        log(f"🙊 [Agent 判定不回·同步] 好友【{target}】: {reason}")
        return False
    if action == "defer":
        LAST_PROCESSED_SIGNATURE[target] = signature
        LAST_PROCESSED_AT[target] = time.time()
        client = get_agent_client(cfg)
        delay = client.urgency_to_seconds(urgency) if client else 60.0
        if segments:
            _schedule_outbox(target, segments, signature, delay, "defer-sync")
        return False
    if not segments and not stickers and not voices:
        LAST_PROCESSED_SIGNATURE[target] = signature
        LAST_PROCESSED_AT[target] = time.time()
        return False

    client = get_agent_client(cfg)
    delay = client.urgency_to_seconds(urgency) if client else 0.0
    if delay > 0:
        LAST_PROCESSED_SIGNATURE[target] = signature
        LAST_PROCESSED_AT[target] = time.time()
        _schedule_outbox(target, segments, signature, delay, "agent-sync", stickers=stickers, voices=voices)
        return False

    log(f"💬 [Agent 决策·同步] 好友【{target}】: {text}")
    sent = _deliver_reply(target, segments, signature, hwnd, rect, layout, cfg, source="agent-sync") if segments else False
    if stickers:
        time.sleep(0.4)
        sent = _deliver_stickers(target, stickers, signature, hwnd, rect, layout, cfg, source="agent-sync") or sent
    if voices:
        time.sleep(0.4)
        sent = _deliver_voices(target, voices, signature, hwnd, rect, layout, cfg, source="agent-sync") or sent
    return sent


def count_green_bubble_metrics(img, layout):
    """
    计算聊天视窗的绿底像素分布，返回三元组 (tot_green, bot_green, bands)：
    1. tot_green: 底部 220px 偏右侧整体绿底像素总数（聊天滚动会抖动，仅作兜底参考）
    2. bot_green: 最贴近输入框的最后 80px 窄带右侧绿底像素数（最新发出气泡必定落入此区域）
    3. bands: 整个聊天区右侧「绿底横带」的条数（= 我方气泡条数，聊天滚动也不变，是最可靠判据）
    """
    if not img:
        return 0, 0, 0
    try:
        W, H = img.size
        arr_v = np.array(img)
        chat_start_x = layout["chat_start_x"]
        input_h = layout["input_h"]
        
        # 区域 1: 底部 220px 整体监控区 (从 0.35 偏移起，严格锚定右侧我方气泡轨，杜绝对方发绿色表情包的干扰)
        y1_tot = max(0, int(H - input_h - 220))
        y2_tot = min(H, int(H - input_h + 10))
        x1_tot = int(chat_start_x + 0.35 * (W - chat_start_x))
        x2_tot = min(W, int(W - 10))
        
        patch_tot = arr_v[y1_tot:y2_tot, x1_tot:x2_tot]
        tot_green = 0
        if patch_tot.size > 0:
            pr = patch_tot[:, :, 0].astype(int)
            pg = patch_tot[:, :, 1].astype(int)
            pb = patch_tot[:, :, 2].astype(int)
            mask_tot = (pg > pr + 18) & (pg > pb + 18) & (pg > 45)
            tot_green = int(np.sum(mask_tot))
            
        # 区域 2: 贴底 80px 最新气泡锚定区 (只监控最下方刚长出的新气泡)
        y1_bot = max(0, int(H - input_h - 80))
        y2_bot = min(H, int(H - input_h))
        x1_bot = int(chat_start_x + 0.35 * (W - chat_start_x))
        x2_bot = min(W, int(W - 20))
        
        patch_bot = arr_v[y1_bot:y2_bot, x1_bot:x2_bot]
        bot_green = 0
        if patch_bot.size > 0:
            pr_b = patch_bot[:, :, 0].astype(int)
            pg_b = patch_bot[:, :, 1].astype(int)
            pb_b = patch_bot[:, :, 2].astype(int)
            mask_bot = (pg_b > pr_b + 18) & (pg_b > pb_b + 18) & (pg_b > 45)
            bot_green = int(np.sum(mask_bot))

        # 区域 3: 整个聊天区右侧的绿底横带计数（聊天滚动后条数不变，是唯一抗滚动的判据）
        bands = 0
        try:
            yb1 = int(layout["header_h"])
            yb2 = min(H, int(H - input_h + 10))
            xb1 = int(chat_start_x)
            xb2 = max(xb1 + 1, W - 10)
            if yb2 > yb1 and xb2 > xb1:
                sub = arr_v[yb1:yb2, xb1:xb2]
                pr_g = sub[:, :, 0].astype(int)
                pg_g = sub[:, :, 1].astype(int)
                pb_g = sub[:, :, 2].astype(int)
                mask_g = (pg_g > pr_g + 18) & (pg_g > pb_g + 18) & (pg_g > 45)
                rows = mask_g.sum(axis=1)
                idx = np.where(rows > 20)[0]
                if len(idx):
                    bands = 1
                    prev = int(idx[0])
                    for v in idx[1:]:
                        if int(v) - prev > 8:
                            bands += 1
                        prev = int(v)
        except Exception:
            bands = 0

        return tot_green, bot_green, bands
    except Exception:
        return 0, 0, 0

def verify_diff_bubble_success(img_pre, img_post, layout):
    """
    【严谨差分动态绿底增量闭环验收引擎（三态架构）】：
    返回值三态：
      True: 明确成功（差分检测到新长出的绿底气泡增量）
      False: 明确失败（画面清晰但确实未检测到新绿底增量，允许进入受控重试）
      "UNCERTAIN": 无法判定（截图异常、画面全黑或 DWM 离屏抓取受阻，安全熔断终止重试以防重复发送）
    """
    if not img_pre or not img_post:
        return "UNCERTAIN"
    try:
        arr_pre = np.array(img_pre)
        arr_post = np.array(img_post)
        if arr_pre.max() <= 10 or arr_post.max() <= 10:
            return "UNCERTAIN"
    except Exception:
        return "UNCERTAIN"

    pre_tot, pre_bot, pre_bands = count_green_bubble_metrics(img_pre, layout)
    post_tot, post_bot, post_bands = count_green_bubble_metrics(img_post, layout)

    # 判据 1（主判据，抗滚动）：聊天区绿底横带条数 +1，即新长出一条我方气泡
    if post_bands - pre_bands >= 1:
        return True

    # 判据 2（兜底）：满足任一合法增量特征即证明新气泡生成
    has_bottom_anchor_spawn = (post_bot >= 40 and (post_bot - pre_bot) >= 30)
    has_total_green_growth = ((post_tot - pre_tot) >= 50)

    if has_bottom_anchor_spawn or has_total_green_growth:
        return True
    return False

def safe_clipboard_copy(text, retries=3, delay=0.03):
    """
    带冲突退避重试的剪贴板安全写入函数：
    有效化解 Windows 剪贴板历史 (Win+V)、杀软或浏览器占用导致的 OpenClipboard 冲突。
    Linux 侧交给常驻 GTK 剪贴板守护（本机没有 xclip/xsel，pyperclip 用不了）。
    """
    if IS_LINUX:
        return bool(lx and lx.clipboard_copy(text))
    for i in range(retries):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            if i < retries - 1:
                time.sleep(delay)
    return False

def _ocr_call(img):
    """统一 OCR 入口。

    rapidocr 1.x 只吃 ndarray/bytes/路径，新版本不再接受 PIL.Image；
    老版本（项目原本打包的）PIL 和 ndarray 都吃。统一转 ndarray 两边都安全。
    """
    if img is None:
        return [], None
    if not isinstance(img, np.ndarray):
        img = np.asarray(img)
    try:
        return ocr_engine(img)
    except Exception as e:
        _warn_throttled("ocr_fail", f"\u26a0\ufe0f [OCR 失败] {e}")
        return [], None


def _ocr_header_title(img, layout):
    """顶栏标题兜底：整幅 OCR 偶尔漏掉 2~3 个字的标题（detection 阈值问题，
    实测同一帧里侧栏的同名文字能识别、顶栏却漏）。单独裁顶栏再 OCR，
    必要时放大 2 倍，能稳定拿到标题，避免"顶栏【未知】"导致反复切会话。"""
    W, H = img.size
    chat_start_x = int(layout["chat_start_x"])
    header_h = int(layout["header_h"])
    x0 = max(0, chat_start_x - 30)
    x1 = min(W, chat_start_x + int(0.45 * (W - chat_start_x)))
    y1 = min(H, header_h + 6)
    if x1 - x0 < 40 or y1 < 20:
        return ""
    band = img.crop((x0, 0, x1, y1))
    for factor in (1, 2):
        try:
            target = band if factor == 1 else band.resize(
                (band.width * factor, band.height * factor), Image.Resampling.LANCZOS)
            res_band, _ = _ocr_call(target)
        except Exception:
            continue
        if not res_band:
            continue
        shifted = [([[p[0] / factor + x0, p[1] / factor] for p in b], t, sc)
                   for b, t, sc in res_band]
        title = extract_active_chat_title(shifted, layout)
        if title:
            return title
    return ""


def _scan_window(hwnd):
    """
    抓图 + OCR + 布局 + 顶栏标题，一次到位。
    返回 (img, layout, res, title)；任一步失败返回 (None, None, None, None)。
    """
    img, rect, scale = grab_wechat_window(hwnd)
    if not img:
        return None, None, None, None
    W, H = img.size
    if W <= 100 or H <= 100:
        return None, None, None, None
    layout = compute_wechat_layout(W, H, img)
    ratio = 1000.0 / W if W > 1100 else 1.0
    if ratio < 1.0:
        small = img.resize((int(W * ratio), int(H * ratio)), Image.Resampling.BILINEAR)
        raw, _ = _ocr_call(small)
        res = [([[p[0] / ratio, p[1] / ratio] for p in b], t, s) for b, t, s in (raw or [])]
    else:
        res, _ = _ocr_call(img)
    title = extract_active_chat_title(res, layout)
    if not title:
        title = _ocr_header_title(img, layout)
    return img, layout, res, title


def _sidebar_row_y(res, layout, target, H):
    """在左侧会话列表里找【target】那一行的纵坐标；找不到返回 None。"""
    sidebar_w = layout["sidebar_w"]
    chat_start_x = layout["chat_start_x"]
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        if not (sidebar_w <= cx <= chat_start_x and (layout["header_h"] + 5) <= cy <= (H - 20)):
            continue
        t = str(txt).strip()
        if looks_like_group(t):
            continue
        if is_name_matched_strict(t, [target])[0]:
            return int(cy)
    return None


def find_contact(hwnd, target, cfg=None):
    """
    把微信切到【target】的聊天窗口。成功 True，失败 False。

    三级策略（主动发送必需：目标好友可能根本不在当前侧栏可视区）：
      1) 顶栏已经是目标 —— 直接放行
      2) 左侧会话列表当前可见 —— 点击后复核顶栏
      3) Ctrl+F 全局搜索 —— OCR 搜索结果，过滤群聊/公众号后点击，复核顶栏

    ⚠️ 任何一步复核不过都返回 False：宁可发不出去，也绝不发错人。
    """
    cfg = cfg if cfg is not None else load_config()
    target = str(target or "").strip()
    if not target:
        return False
    # 允许调用方直接传微信号：先统一成备注显示名，后面所有复核都以备注为准
    target = canonical_target(target, cfg)
    if not target:
        return False

    img, layout, res, title = _scan_window(hwnd)
    if img is None:
        log("🚫 [找人失败] 抓不到微信画面")
        return False
    if title and is_name_matched_strict(title, [target])[0]:
        return True

    # ---------- 第 2 级：当前可见的左侧会话列表 ----------
    H = img.size[1]
    row_y = _sidebar_row_y(res, layout, target, H)
    if row_y is not None:
        click_x = int(layout["sidebar_w"] + 0.45 * (layout["chat_start_x"] - layout["sidebar_w"]))
        log(f"🎯 [找人在侧栏] 点击会话列表中的【{target}】(y={row_y})")
        post_click_client_point(hwnd, click_x, row_y)
        time.sleep(0.35)
        _, _, _, title2 = _scan_window(hwnd)
        if title2 and is_name_matched_strict(title2, [target])[0]:
            return True
        log(f"⚠️ [侧栏点击未生效] 顶栏仍为【{title2 or '未知'}】，改用 Ctrl+F 全局搜索")

    # ---------- 第 3 级：Ctrl+F 全局搜索 ----------
    ps = cfg.get("proactive_send") or {}
    if not ps.get("allow_search", True):
        log("🚫 [找人失败] 侧栏没有，且配置禁止全局搜索")
        return False
    # 自定义微信号优先（唯一、无同名歧义）；搜不到再降级回备注重搜一次
    first = contact_search_key(target, cfg)
    if _search_and_open(hwnd, target, search_key=first):
        return True
    if first and first != target:
        log(f"🔁 [搜索降级] 关键词【{first}】没搜到本地联系人，改用备注【{target}】重搜")
        return _search_and_open(hwnd, target, search_key=target)
    return False


def _key_combo(vk, ctrl=True):
    """按一次组合键（默认带 Ctrl）。走真实键盘输入队列，兼容性最好。

    vk 沿用 Windows 虚拟键码；Linux 侧由后端翻译成 keysym。
    """
    if IS_LINUX:
        return bool(lx and lx.key(vk, ctrl=ctrl))
    try:
        if ctrl:
            user32.keybd_event(0x11, 0, 0, 0)
        time.sleep(0.01)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, 0x0002, 0)
        if ctrl:
            time.sleep(0.01)
            user32.keybd_event(0x11, 0, 0x0002, 0)
    except Exception:
        pass


def _open_search_box(hwnd, img, layout, res):
    """
    点开微信左上角的搜索框。成功返回 True。

    注意：搜索框里**已经有字**的时候，占位符「搜索」会被顶掉，纯靠文字匹配
    会误判成「界面上没有搜索框」。所以文字命中失败后，再按几何位置兜底
    （搜索行靠上、位于左半区、避开右侧的「+」号）。
    """
    band = layout["header_h"]
    left, right = layout["sidebar_w"], layout["chat_start_x"]

    hit = None
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        if cy > band or not (left <= cx <= right):
            continue
        if "搜索" in str(txt).strip():
            hit = (int(cx), int(cy), str(txt).strip())
            break

    if hit is None:
        # 兜底：取搜索行里最靠上、且不在最右（避开 + 号）的一个 OCR 框
        best = None
        for b, txt, sc in (res or []):
            cx = (b[0][0] + b[1][0]) / 2.0
            cy = (b[0][1] + b[2][1]) / 2.0
            if cy > band or not (left <= cx <= right - 60):
                continue
            if best is None or cy < best[1]:
                best = (int(cx), int(cy), str(txt).strip())
        if best is not None:
            hit = best
        else:
            # 最后兜底：按布局比例直接点搜索框中心
            hit = (int(left + (right - left) * 0.45), int(band * 0.68), "搜索框(几何)")

    log(f"🔍 [打开搜索框] 点中【{hit[2]}】({hit[0]},{hit[1]})")
    post_click_client_point(hwnd, hit[0], hit[1])
    time.sleep(0.35)
    return True


def _search_box_contains(res, layout, target):
    """
    搜索框里是否真的出现了目标关键词。

    这是防串发的关键闸门：Ctrl+F 在某些输入法/焦点状态下不生效，一旦不生效，
    粘贴会把联系人名直接打进聊天输入框，接着下面的逻辑会把侧栏普通会话行
    当成「搜索结果」盲点下去 —— 就是之前真机 160s 失败的根因。

    注意：微信搜索框很窄，长关键词只会露出一段（真机实测
    `wxid_xxxxxxxxxxxxxxxx` 在框里显示成 `xxxxxxxxxxxxxxxx`，头部被滚走了），
    所以不能只做全等/前缀匹配，必须允许「子串命中且长度 >= 4」。
    """
    band = layout["header_h"] + 10
    left, right = layout["sidebar_w"], layout["chat_start_x"]
    nt = normalize_for_match(target)
    if not nt:
        return False
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        if cy > band or not (left <= cx <= right):
            continue
        raw = str(txt).strip()
        if not raw:
            continue
        if is_name_matched_strict(raw, [target])[0]:
            return True
        nb = normalize_for_match(raw)
        if len(nb) < 4:
            continue
        if nb in nt or nt in nb:
            return True
    return False


WECHAT_POPUP_CLASS = "Qt51514QWindowToolSaveBits"


def _wechat_pid(hwnd):
    """取窗口所属进程 pid，失败返回 0。"""
    if IS_LINUX:
        return 0                     # Linux 侧按 WM_CLASS 找弹窗，不需要 pid
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:
        return 0


def _find_wechat_popup(main_hwnd, timeout=1.2):
    """
    找出微信的「全局搜索弹出窗」。

    真机实测（微信 4.15）：搜索结果是**独立顶层窗口**
    （class=Qt51514QWindowToolSaveBits，title=Weixin，约 644x707，
    位置在主窗左上方外侧），主窗口的 PrintWindow 位图里**完全没有它**。
    所以必须单独枚举出来抓图，否则永远只能看到没被过滤的会话列表。

    Linux（4.1.26 实测）：同样是独立顶层窗（class 仍是 wechat，约 368x426，
    位置落在主窗内部），按"除主窗之外面积最大的 wechat 窗"取。
    """
    if IS_LINUX:
        return lx.find_popup(main_hwnd, timeout=timeout) if lx else None
    pid = _wechat_pid(main_hwnd)
    if not pid:
        return None
    deadline = time.time() + max(0.0, float(timeout))
    best = None
    while True:
        found = []

        def _cb(h, lp):
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value != pid or h == main_hwnd:
                return True
            if not user32.IsWindowVisible(h):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            if cls.value != WECHAT_POPUP_CLASS:
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w >= 200 and hh >= 80:
                found.append((w * hh, h))
            return True

        cb = WNDENUMPROC(_cb)
        user32.EnumWindows(cb, 0)
        if found:
            found.sort(reverse=True)
            best = found[0][1]
            break
        if time.time() >= deadline:
            break
        time.sleep(0.10)
    return best


def _locate_in_popup(popup, target):
    """
    在搜索弹出窗里定位【target】那一行，返回可直接点的 (x, y)；找不到返回 None。

    只认「严格匹配」和「群里防火墙」，绝不把网络搜索建议（如
    `微信2分身怎么弄`）当成联系人点下去 —— 那会把用户带到搜索网页。
    """
    img, rect, scale = grab_wechat_window(popup)
    if img is None:
        return None
    W, H = img.size
    if W <= 50 or H <= 30:
        return None
    try:
        res, _ = ocr_engine(np.asarray(img))
    except Exception:
        return None
    noise = {"搜索", "取消", "全部", "联系人", "群聊", "聊天记录", "公众号",
             "最常使用", "搜索网络结果", "查看更多", "搜索指定内容", "网络查找"}
    hits = []
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        t = str(txt).strip()
        if not t or t in noise:
            continue
        if looks_like_group(t):
            continue
        if is_name_matched_strict(t, [target])[0]:
            hits.append((cy, cx, t))
    if not hits:
        return None
    hits.sort(key=lambda x: x[0])
    cy, _cx, hit = hits[0]
    click_x = int(W / 2)
    click_y = int(min(max(cy, 1), H - 2))
    log(f"   ↳ 弹出窗内命中【{hit}】(y={click_y})")
    return click_x, click_y


def _real_click_client_point(hwnd, client_x, client_y):
    """
    用「真实鼠标」点击窗口客户区坐标（会真的移动光标）。

    PostMessage 在弹出窗上不一定被 Qt 接受，这是兜底方案。
    Linux 侧本来就只有"真实鼠标"这一条路。
    """
    if IS_LINUX:
        if lx:
            lx.click_client(hwnd, client_x, client_y)
            return True
        return False
    try:
        dx, dy = _nonclient_offset(hwnd)
        pt = wintypes.POINT(int(client_x), int(client_y))
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        user32.SetCursorPos(int(pt.x), int(pt.y))
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
        time.sleep(0.04)
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
        time.sleep(0.05)
        return True
    except Exception:
        return False



# -----------------------------------------------------------------------------
# Linux 版「搜索并打开会话」
# -----------------------------------------------------------------------------
def _linux_search_and_open(hwnd, target, search_key=None):
    """Linux/X11 实现：先点侧栏已有行，再退回 Ctrl+F + 搜索弹出窗。

    与 Windows 版的关键差异：
      - 没有 PostMessage，点击一律走 XTest（会真的移动光标）
      - 搜索结果是独立顶层窗（class 仍是 wechat），坐标按该窗自己的几何算
      - 关键词一律走剪贴板粘贴：fcitx 会把逐键字符吞成候选词
    """
    key = str(search_key or target or "").strip() or target
    old_clip = lx.clipboard_paste() if lx else ""
    opened = False
    if not activate_wechat(hwnd):
        log("\U0001f6ab [搜索找人] 无法激活微信窗口，放弃")
        return False
    try:
        # ① 侧栏已经有这个会话：直接点，最稳（不会弹搜一搜）
        img, layout, res, _ = _scan_window(hwnd)
        if img is None:
            log("\U0001f6ab [搜索找人] 抓图失败")
            return False
        row = _sidebar_row_y(res, layout, target, img.size[1])
        if row is not None:
            click_x = int((layout["sidebar_w"] + layout["chat_start_x"]) / 2.0)
            log(f"\U0001f3af [侧栏命中] 【{target}】(y={row})，点击进入")
            post_click_client_point(hwnd, click_x, int(row))
            time.sleep(0.70)
            _, _, _, title = _scan_window(hwnd)
            if title and is_name_matched_strict(title, [target])[0]:
                opened = True
                return True
            log(f"   \u21b3 点完顶栏是【{title or '未知'}】，改走全局搜索")

        # ② Ctrl+F 全局搜索
        _key_combo(0x46)
        time.sleep(0.50)
        for attempt in range(1, 4):
            activate_wechat(hwnd)
            time.sleep(0.10)
            _key_combo(0x41)                # Ctrl+A
            time.sleep(0.05)
            _key_combo(0x08, ctrl=False)    # Backspace 清空旧内容
            time.sleep(0.05)
            if not safe_clipboard_copy(key):
                log("\U0001f6ab [搜索找人] 剪贴板写入失败，放弃")
                return False
            _key_combo(0x56)                # Ctrl+V
            time.sleep(1.10)                # 等搜索结果渲染

            popup = _find_wechat_popup(hwnd, timeout=1.5)
            if not popup:
                log(f"\u26a0\ufe0f [搜索找人] 第 {attempt}/3 次没等到搜索弹出窗")
                continue
            spot = _locate_in_popup(popup, target)
            if not spot:
                log(f"\u26a0\ufe0f [搜索找人] 第 {attempt}/3 次弹出窗里没有【{target}】")
                continue

            px, py = spot
            log(f"\U0001f3af [搜索命中] 弹出窗内点击 ({px},{py}) 进入【{target}】")
            post_click_client_point(popup, px, py)
            time.sleep(0.85)

            _, _, _, title = _scan_window(hwnd)
            if title and is_name_matched_strict(title, [target])[0]:
                opened = True
                return True

            log(f"   \u21b3 PostMessage 等价路径没生效（顶栏【{title or '未知'}】），改用真实鼠标重点一次")
            _real_click_client_point(popup, px, py)
            time.sleep(0.85)
            _, _, _, title = _scan_window(hwnd)
            if title and is_name_matched_strict(title, [target])[0]:
                opened = True
                return True
            log(f"\U0001f6ab [搜索点击未生效] 顶栏为【{title or '未知'}】，放弃防串发")
            return False
        return False
    finally:
        if not opened:
            _key_combo(0x1B, ctrl=False)
            time.sleep(0.10)
            ensure_wechat_visible(hwnd)
        if old_clip:
            safe_clipboard_copy(old_clip, retries=2, delay=0.02)


def _search_and_open(hwnd, target, search_key=None):
    """
    在微信里搜索并点进【target】的聊天窗口。成功返回 True。

    `target` 始终是「显示名（备注）」，用于所有复核；
    `search_key` 是真正粘进搜索框的词（默认就用备注；微信 PC 端**不支持**
    用微信号搜索，实测搜 wxid 只会得到「网络查找微信号」的网页结果）。
    """
    if IS_LINUX:
        return _linux_search_and_open(hwnd, target, search_key)

    key = str(search_key or target or "").strip() or target
    old_clip = None
    try:
        old_clip = pyperclip.paste()
    except Exception:
        pass

    if not activate_wechat(hwnd):
        log("🚫 [搜索找人] 无法激活微信窗口，放弃")
        return False

    opened = False
    try:
        img, layout, res, _ = _scan_window(hwnd)
        if img is None:
            log("🚫 [搜索找人] 抓图失败")
            return False

        if not _open_search_box(hwnd, img, layout, res):
            log("⚠️ [搜索找人] 界面上没找到搜索框，退回 Ctrl+F")
            _key_combo(0x46)
            time.sleep(0.35)

        if not safe_clipboard_copy(key):
            log("🚫 [搜索找人] 剪贴板写入失败，放弃")
            return False

        typed = False
        seen = []
        for attempt in range(1, 4):
            # 点搜索框会把微信的 Qt 弹出窗拉起来，键盘焦点会被扰动；
            # 每轮都重新把主窗顶到前台再输入，实测成功率最高。
            activate_wechat(hwnd)
            time.sleep(0.10)
            _key_combo(0x41)                # Ctrl+A
            time.sleep(0.05)
            _key_combo(0x08, ctrl=False)    # Delete，把旧内容清干净
            time.sleep(0.05)
            _key_combo(0x56)                # Ctrl+V
            time.sleep(0.95)                # 等搜索结果渲染

            img, layout, res, _ = _scan_window(hwnd)
            if img is None:
                log("🚫 [搜索找人] 搜索结果抓图失败")
                return False
            if _search_box_contains(res, layout, key):
                typed = True
                break

            seen = []
            for b, t, s in (res or []):
                cx = (b[0][0] + b[1][0]) / 2.0
                cy = (b[0][1] + b[2][1]) / 2.0
                if cy <= layout["header_h"] + 10 and layout["sidebar_w"] <= cx <= layout["chat_start_x"]:
                    seen.append(str(t)[:24])
            log(f"⚠️ [搜索找人] 第 {attempt}/3 次输入没进搜索框（框内={seen}），重试")
            if not _open_search_box(hwnd, img, layout, res):
                _key_combo(0x46)
            time.sleep(0.30)

        if not typed:
            log(f"🚫 [搜索找人] 关键词【{key}】多次尝试仍没进搜索框，放弃盲点；框内实际 OCR={seen}")
            return False

        # 微信的搜索结果在独立弹出窗里，主窗 PrintWindow 抓不到
        popup = _find_wechat_popup(hwnd, timeout=1.2)
        if popup:
            spot = _locate_in_popup(popup, target)
            if spot:
                px, py = spot
                log(f"🎯 [搜索命中] 弹出窗内点击 ({px},{py}) 进入【{target}】")
                post_click_client_point(popup, px, py)
                time.sleep(0.55)
                _, _, _, title2 = _scan_window(hwnd)
                if title2 and is_name_matched_strict(title2, [target])[0]:
                    opened = True
                    return True
                log("⚠️ [搜索命中] PostMessage 没被弹出窗接受，改用真实鼠标再试一次")
                _real_click_client_point(popup, px, py)
                time.sleep(0.60)
                _, _, _, title2 = _scan_window(hwnd)
                if title2 and is_name_matched_strict(title2, [target])[0]:
                    opened = True
                    return True
                log(f"🚫 [搜索点击未生效] 顶栏为【{title2 or '未知'}】，放弃防串发")
                return False
            log(f"🚫 [搜索找人] 弹出窗里没有【{target}】（只有网络搜索结果），放弃盲点")
            return False

        # 兜底：老版本微信没有独立弹出窗，退回主窗侧栏扫描
        log("⚠️ [搜索找人] 没找到搜索弹出窗，退回主窗扫描（老版本微信路径）")
        noise = {"搜索", "取消", "全部", "联系人", "群聊", "聊天记录", "公众号",
                 "最常使用", "搜索网络结果", "查看更多", "搜索指定内容"}
        candidates = []
        for b, txt, sc in (res or []):
            cx = (b[0][0] + b[1][0]) / 2.0
            cy = (b[0][1] + b[2][1]) / 2.0
            if cy < layout["header_h"] + 5:
                continue
            if not (0 <= cx <= layout["chat_start_x"]):
                continue
            t = str(txt).strip()
            if not t or t in noise:
                continue
            if looks_like_group(t):
                continue
            if is_name_matched_strict(t, [target])[0]:
                candidates.append((cy, cx, t))

        if not candidates:
            log(f"🚫 [搜索找人] 没搜到【{target}】")
            return False

        candidates.sort(key=lambda x: x[0])
        cy, cx, hit = candidates[0]
        click_x = int((layout["sidebar_w"] + layout["chat_start_x"]) / 2.0)
        log(f"🎯 [搜索命中] 【{hit}】(y={int(cy)})，点击 ({click_x},{int(cy)}) 进入聊天")
        post_click_client_point(hwnd, click_x, int(cy))
        time.sleep(0.50)

        _, _, _, title2 = _scan_window(hwnd)
        if title2 and is_name_matched_strict(title2, [target])[0]:
            opened = True
            return True
        log(f"🚫 [搜索点击未生效] 顶栏为【{title2 or '未知'}】，放弃防串发")
        return False
    finally:
        if not opened:
            # Esc 清掉搜索态，别把界面留在搜索；但微信里 Esc 有可能把主窗隐藏，
            # 所以按完立刻确认窗口还可见，否则下一次抓图会拿到全黑图。
            _key_combo(0x1B, ctrl=False)
            time.sleep(0.10)
            ensure_wechat_visible(hwnd)
        if old_clip:
            safe_clipboard_copy(old_clip, retries=2, delay=0.02)


def _load_pending_confirm():
    """从磁盘恢复待确认队列（进程重启不丢）。"""
    PENDING_CONFIRM.clear()
    if not os.path.exists(PENDING_CONFIRM_FILE):
        return
    try:
        with open(PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("status", "pending") == "pending" and rec.get("id"):
                    PENDING_CONFIRM[rec["id"]] = rec
        if PENDING_CONFIRM:
            log(f"📥 [待确认] 从磁盘恢复 {len(PENDING_CONFIRM)} 条待确认消息")
    except Exception as exc:
        log(f"⚠️ [待确认读取失败] {exc}")


def _save_pending_confirm():
    """把内存里的待确认队列整体写回（条数很少，整体重写最简单也最不容易错）。"""
    try:
        tmp = PENDING_CONFIRM_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in PENDING_CONFIRM.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, PENDING_CONFIRM_FILE)
        return True
    except Exception as exc:
        log(f"🚨 [待确认写入失败] {exc}")
        return False


def _expire_pending():
    """清掉放太久的待确认消息（默认 12 小时），免得重启后冒出一堆过期话术。"""
    if not PENDING_CONFIRM:
        return
    hours = float(proactive_cfg().get("confirm_expire_hours", 12))
    cutoff = time.time() - hours * 3600
    stale = [k for k, v in PENDING_CONFIRM.items() if float(v.get("created_ts") or 0) < cutoff]
    for k in stale:
        PENDING_CONFIRM.pop(k, None)
    if stale:
        _save_pending_confirm()
        log(f"🗑️ [待确认过期] 清理了 {len(stale)} 条超过 {hours:g} 小时的待确认消息")


_PENDING_ID_SEQ = [0]


def _append_pending_confirm(target, text, source="api"):
    """需要人工确认时，把待发消息落到队列，等 GUI 弹窗点确认。"""
    _expire_pending()
    # 毫秒时间戳做 id：同一毫秒内连发两条会撞 id，后一条把前一条顶掉
    # （回归测试里就踩到过），加个进程内自增序号保证唯一。
    _PENDING_ID_SEQ[0] += 1
    record = {
        "id": f"p{int(time.time() * 1000)}-{_PENDING_ID_SEQ[0]}",
        "target": target,
        "text": text,
        "source": source,
        "created_ts": time.time(),
        "status": "pending",
    }
    PENDING_CONFIRM[record["id"]] = record
    if not _save_pending_confirm():
        PENDING_CONFIRM.pop(record["id"], None)
        return None
    log(f"📝 [待确认] 主动消息已入队待人工确认: 【{target}】{text}")
    return record


def list_pending():
    """给 GUI 用的待确认列表（按时间正序）。"""
    _expire_pending()
    return sorted(PENDING_CONFIRM.values(), key=lambda x: x.get("created_ts") or 0)


def confirm_pending(pending_id, cfg=None):
    """
    人工点了「确认发送」。这里会绕过 require_confirm（因为人已经确认了），
    但白名单 / 黑名单 / 频率这些硬护栏仍然照跑，不因为人工确认就放水。
    """
    cfg = cfg if cfg is not None else load_config()
    rec = PENDING_CONFIRM.get(str(pending_id or ""))
    if not rec:
        return {"ok": False, "code": "not_found", "detail": "待确认记录不存在或已处理"}

    target = rec["target"]
    text = rec["text"]

    # 复制一份配置，临时关掉 require_confirm，其余护栏原样保留
    cfg2 = dict(cfg)
    ps2 = dict(cfg.get("proactive_send") or {})
    ps2["require_confirm"] = False
    cfg2["proactive_send"] = ps2

    result = proactive_send(target, text, cfg2, source="confirm")
    if result.get("ok"):
        PENDING_CONFIRM.pop(str(pending_id), None)
        _save_pending_confirm()
        log(f"✅ [人工确认] 已发送给【{target}】: {text}")
    return result


def reject_pending(pending_id, reason=""):
    """人工点了「跳过」。"""
    pid = str(pending_id or "")
    rec = PENDING_CONFIRM.pop(pid, None)
    if not rec:
        return {"ok": False, "code": "not_found", "detail": "待确认记录不存在或已处理"}
    _save_pending_confirm()
    log(f"🚫 [人工跳过] 取消发给【{rec['target']}】的消息{f'（{reason}）' if reason else ''}")
    return {"ok": True, "code": "ok", "detail": "已跳过"}



# -----------------------------------------------------------------------------
# Linux 版「即时发送」
# -----------------------------------------------------------------------------
def _linux_send_reply(hwnd, rect, layout, target, final_reply):
    """Linux/X11 实现：核验顶栏 → 点输入框 → Ctrl+V → Enter → 绿底增量验收。

    与 Windows 版的差异：
      - 没有 GetForegroundWindow 的强校验，改用 X11 焦点查询（lx.activate 返回）
      - PostMessage 换成 XTest（会移动光标）
      - 验收判据复用同一套 verify_diff_bubble_success（纯图像处理，平台无关）
      - 离屏隐藏模式（XComposite）下不抬窗/不点鼠标：屏幕坐标上没有窗口，
        只把键盘焦点直接给它，Ctrl+V / Enter 照样进输入框（实测可用）
    """
    hidden = bool(lx and lx.is_hidden(hwnd))
    # --- 发送前核验：顶栏必须是目标会话，绝不盲发 ---
    img_now, cur_rect, _ = grab_wechat_window(hwnd)
    if img_now is None:
        log("\u26a0\ufe0f [发送前核验失败] 无法获取微信画面，终止发送防误触")
        return False
    cur_W, cur_H = img_now.size
    cur_layout = compute_wechat_layout(cur_W, cur_H, img_now)

    _, _, res_now, cur_title = _scan_window(hwnd)
    if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
        log(f"\u26a0\ufe0f [发送前纠偏] 当前顶栏【{cur_title or '未知'}】不是【{target}】，先切过去")
        if hidden:
            log("\U0001f5a5\ufe0f [离屏] 切换会话需要鼠标，临时召回微信窗口（切完立刻重新隐藏）")
            lx.show_window(hwnd)
            time.sleep(0.30)
        switched = _linux_search_and_open(hwnd, target)
        if hidden:
            lx.hide_window(hwnd)
            time.sleep(0.30)
        if not switched:
            log(f"\U0001f6ab [发送中止] 无法锁定会话【{target}】")
            return False
        time.sleep(0.40)
        img_now, cur_rect, _ = grab_wechat_window(hwnd)
        if img_now is None:
            return False
        cur_W, cur_H = img_now.size
        cur_layout = compute_wechat_layout(cur_W, cur_H, img_now)
        _, _, _, cur_title = _scan_window(hwnd)
        if not (cur_title and is_name_matched_strict(cur_title, [target])[0]):
            log(f"\U0001f6ab [发送中止] 切换后顶栏仍是【{cur_title or '未知'}】")
            return False

    log(f"\U0001f4e4 正在回复【{target}】: {final_reply}")
    old_clip = lx.clipboard_paste() if lx else ""
    send_success = False
    send_unverified = False

    for attempt in range(1, 3):
        if not safe_clipboard_copy(final_reply, retries=3, delay=0.03):
            log(f"\u26a0\ufe0f [剪贴板写入受阻(尝试 {attempt}/2)] 稍后重试...")
            time.sleep(0.05)
            continue

        img_pre, _, _ = grab_wechat_window(hwnd)

        # 击键前必须真的拿到键盘焦点，否则按键会串到别的窗口
        if hidden:
            if not lx.focus_window(hwnd):
                log(f"\U0001f6ab [安全熔断(尝试 {attempt}/2)] 离屏模式下无法把键盘焦点给微信，终止击键")
                time.sleep(0.15)
                continue
        else:
            if not activate_wechat(hwnd):
                log(f"\U0001f6ab [安全熔断(尝试 {attempt}/2)] 微信没拿到键盘焦点，坚决终止按键防串窗")
                time.sleep(0.15)
                continue

            # 点输入框（几何中心，避开底部图标栏与顶部分割线）
            input_safe_x = int(0.55 * cur_W)
            input_safe_y = int(cur_H - cur_layout["input_h"] * 0.55)
            post_click_client_point(hwnd, input_safe_x, input_safe_y)
            time.sleep(0.10)

            if not activate_wechat(hwnd):
                log(f"\U0001f6ab [安全熔断(尝试 {attempt}/2)] 点击后焦点脱失，终止击键")
                continue

        _key_combo(0x56)                 # Ctrl+V
        time.sleep(0.18)
        _key_combo(0x0D, ctrl=False)     # Enter 发送

        if hidden:
            # 离屏窗口的渲染会被 Chromium 降帧，气泡出现可能晚 1~2 秒；
            # 这里多轮轮询，而且 Enter 已经发出就绝不重试（否则会给对方发两遍）
            verdict = False
            deadline = time.time() + 7.0
            while time.time() < deadline:
                time.sleep(0.45)
                img_post, _, _ = grab_wechat_window(hwnd)
                v = verify_diff_bubble_success(img_pre, img_post, cur_layout)
                if v is True:
                    verdict = True
                    break
                if v == "UNCERTAIN":
                    verdict = "UNCERTAIN"
                    break
            if verdict is True:
                send_success = True
                break
            if verdict == "UNCERTAIN":
                log(f"\u26a0\ufe0f [离屏发送] 画面无法比对（可能被渲染节流），按已发送处理，避免重复")
            else:
                log(f"\u26a0\ufe0f [离屏发送] 7 秒内未检测到气泡增量，按已发送处理（不重发，避免重复消息）")
            send_success = True
            send_unverified = True
            break

        time.sleep(0.55)
        img_post, _, _ = grab_wechat_window(hwnd)
        verdict = verify_diff_bubble_success(img_pre, img_post, cur_layout)
        if verdict is True:
            send_success = True
            break
        if verdict == "UNCERTAIN":
            log(f"\u26a0\ufe0f [闭环验收异常] 画面无法比对，安全熔断终止重试以防给【{target}】重复发消息")
            send_success = True
            send_unverified = True
            break
        log(f"\u26a0\ufe0f [发送闭环验收未通过(尝试 {attempt}/2)] 未检测到新长出的绿底气泡，重试...")
        time.sleep(0.20)

    if old_clip:
        safe_clipboard_copy(old_clip, retries=2, delay=0.02)

    if send_success and send_unverified:
        log(f"\u26a0\ufe0f [发送未验证] 已按发出处理并登记防重复，但未能抓图验证【{target}】是否收到")
    elif send_success:
        log(f"\u2705 [Linux 静默秒回成功] 已回复【{target}】")
    else:
        log(f"\u274c [发送脱靶告警] 2 次重试后均未检测到绿底气泡增量！")
    return send_success


def send_reply_instant(hwnd, rect, layout, target, final_reply):
    """
    极速秒回直达引擎（局部消息投递，0 弹窗 0 鼠标物理移动）：
    1. 预检当前顶栏目标并智能纠偏
    2. 剪贴板安全原子写入
    3. Win32 穿透提权与击键前前台强校验 (必须 GetForegroundWindow() == hwnd，绝不盲发按键防串窗)
    4. 动态自适应几何坐标模拟点击输入框聚焦 (0 鼠标物理移动)
    5. Ctrl+V 与 Enter 极速注入
    6. 平稳切回用户原窗口 (彻底废除 HWND_BOTTOM 压底)
    7. 动态差分绿底增量严谨闭环验收
    """
    if IS_LINUX:
        return _linux_send_reply(hwnd, rect, layout, target, final_reply)

    # --- 发送前核验 (Pre-Flight Verification) ---
    img_now, cur_rect, cur_scale = grab_wechat_window(hwnd)
    if not img_now:
        log(f"⚠️ [发送前核验失败] 无法获取微信画面，终止发送防误触")
        return False
        
    cur_W, cur_H = img_now.size
    cur_layout = compute_wechat_layout(cur_W, cur_H, img_now)
    
    # 极速轻量 OCR 顶栏与左侧列表
    target_w = 1000
    downscale_ratio = 1000.0 / cur_W if cur_W > 1100 else 1.0
    if downscale_ratio < 1.0:
        img_for_ocr = img_now.resize((target_w, int(cur_H * downscale_ratio)), Image.Resampling.BILINEAR)
        res_raw, _ = _ocr_call(img_for_ocr)
        res_now = []
        for b, txt, sc in (res_raw or []):
            mapped_box = [[pt[0] / downscale_ratio, pt[1] / downscale_ratio] for pt in b]
            res_now.append((mapped_box, txt, sc))
    else:
        res_now, _ = _ocr_call(img_now)
        
    cur_title = extract_active_chat_title(res_now, cur_layout)
    is_right_target = is_name_matched_strict(cur_title, [target])[0] if cur_title else False
    
    # 若顶栏不是目标好友，触发智能重定向
    if not is_right_target:
        log(f"⚠️ [发送前纠偏] 检测到窗口偏离（当前为【{cur_title or '非目标'}】），正在重定向锁定【{target}】...")
        found_cand_y = None
        sidebar_w = cur_layout["sidebar_w"]
        chat_start_x = cur_layout["chat_start_x"]
        for b, txt, sc in (res_now or []):
            cx = (b[0][0] + b[1][0]) / 2.0
            cy = (b[0][1] + b[2][1]) / 2.0
            if sidebar_w <= cx <= chat_start_x and (cur_layout["header_h"] + 5) <= cy <= (cur_H - 20):
                if is_name_matched_strict(txt.strip(), [target])[0]:
                    found_cand_y = cy
                    break
                    
        if found_cand_y is not None:
            post_click_client_point(hwnd, int(0.16 * cur_W), int(found_cand_y))
            time.sleep(0.30)  # 等待 Qt5 完成右侧视窗切换渲染
            # 🔥 纠偏后二次核验：重新抓图 OCR 顶栏，绝不盲信点击一定生效（防串发红线）
            img_chk, _, _ = grab_wechat_window(hwnd)
            if not img_chk:
                log(f"🚫 [安全熔断] 纠偏后无法抓图复核顶栏，放弃本次发送防串发！")
                return False
            chk_W, chk_H = img_chk.size
            chk_layout = compute_wechat_layout(chk_W, chk_H, img_chk)
            hdr = img_chk.crop((0, 0, chk_W, int(chk_layout["header_h"] + 15)))
            res_hdr_raw, _ = _ocr_call(hdr)
            res_hdr = [([[pt[0], pt[1]] for pt in b], txt, sc) for b, txt, sc in (res_hdr_raw or [])]
            chk_title = extract_active_chat_title(res_hdr, chk_layout)
            if not (chk_title and is_name_matched_strict(chk_title, [target])[0]):
                log(f"🚫 [安全熔断] 纠偏后顶栏仍为【{chk_title or '非目标'}】，点击未生效，放弃本次发送防串发！")
                return False
            log(f"🎯 [后台静默纠偏] 已重新切入【{target}】聊天视窗（顶栏复核通过）")
        else:
            log(f"🚫 [安全熔断] 未在列表中定位到目标【{target}】，放弃本次发送以防串发！")
            return False
            
    log(f"📤 正在回复【{target}】: {final_reply}")
    
    # 剪贴板原子快照保护
    user_old_clip = None
    try:
        user_old_clip = pyperclip.paste()
    except Exception:
        pass
        
    user_orig_hwnd = user32.GetForegroundWindow()
    send_success = False
    send_unverified = False
    
    for attempt in range(1, 3):
        # 1. 剪贴板安全写入
        if not safe_clipboard_copy(final_reply, retries=3, delay=0.03):
            log(f"⚠️ [剪贴板写入受阻(尝试 {attempt}/2)] 系统剪贴板被占用，稍后重试...")
            time.sleep(0.05)
            continue
        time.sleep(0.02)
        
        # 2. 发送前基线图像抓取 (用于差分验收)
        img_pre, _, _ = grab_wechat_window(hwnd)
        
        # 3. Win32 穿透提权激活微信
        activate_wechat(hwnd)
        time.sleep(0.03)
        
        # 🔥 关键安全红线：击键前强校验前台窗口！
        cur_fore = user32.GetForegroundWindow()
        if cur_fore != hwnd:
            log(f"🚫 [安全熔断(尝试 {attempt}/2)] 微信窗口未夺取到前台焦点 (当前前台HWND={cur_fore})，坚决终止按键模拟防串窗！")
            time.sleep(0.15)
            continue
            
        # 4. 动态自适应几何坐标模拟点击输入框聚焦 (0 鼠标物理移动)
        # 根据动态高度与 input_h 计算垂直安全中心，避开底部图标栏与顶部视窗分割线
        input_safe_y = int(cur_H - cur_layout["input_h"] * 0.55)
        input_safe_x = int(0.55 * cur_W)
        post_click_client_point(hwnd, input_safe_x, input_safe_y)
        time.sleep(0.03)
        
        # 二次复验前台归属
        if user32.GetForegroundWindow() != hwnd:
            log(f"🚫 [安全熔断(尝试 {attempt}/2)] 点击输入框时光标脱失，终止击键！")
            continue
            
        # 5. 极速键盘事件注入 (Ctrl+V 与 Enter)
        try:
            # Ctrl+V 粘贴
            user32.keybd_event(0x11, 0, 0, 0)
            user32.keybd_event(0x56, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(0x56, 0, 0x0002, 0)
            user32.keybd_event(0x11, 0, 0x0002, 0)
            time.sleep(0.04)
            
            # 回车发送
            user32.keybd_event(0x0D, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(0x0D, 0, 0x0002, 0)
            time.sleep(0.04)
        finally:
            # 6. 平稳恢复用户原本的前台窗口 (坚决废除 HWND_BOTTOM 压底！)
            if user_orig_hwnd and user_orig_hwnd != hwnd:
                user32.SetForegroundWindow(user_orig_hwnd)
                
        # 7. 渲染沉降与严谨差分动态绿底增量闭环验收
        time.sleep(0.35) # 等待微信 Qt5 气泡动画与排版彻底沉降
        img_post, _, _ = grab_wechat_window(hwnd)
        verdict = verify_diff_bubble_success(img_pre, img_post, cur_layout)
        if verdict is True:
            send_success = True
            break
        elif verdict == "UNCERTAIN":
            log(f"⚠️ [闭环验收异常] 离屏画面抓取受阻无法比对，安全熔断终止重试以防向好友【{target}】重复发消息！")
            send_success = True
            send_unverified = True
            break
        else:
            log(f"⚠️ [发送闭环验收未通过(尝试 {attempt}/2)] 未检测到新长出的绿底气泡增量，执行强力二次重试...")
            time.sleep(0.12)
            
    # 还原用户原本的剪贴板 (仅当原剪贴板为非空文本时写回，避免冲掉用户复制的图片/文件)
    if user_old_clip:
        safe_clipboard_copy(user_old_clip, retries=2, delay=0.02)
        
    if send_success and send_unverified:
        log(f"⚠️ [发送未验证] 已按发出处理并登记防重复，但未能抓图验证好友【{target}】是否收到，请留意确认！")
    elif send_success:
        log(f"✅ [后台静默秒回成功] 已回复【{target}】！(物理鼠标 0 移动，瞬态焦点已复原)")
    else:
        log(f"❌ [发送脱靶告警] 2 次重试后均未检测到绿底气泡增量！")
        
    return send_success

# =============================================================================
# 7. 左右双轨协同感知引擎 (DUAL-TRACK COLLABORATIVE ENGINE)
# =============================================================================
def scan_and_reply_wechat(hwnd):
    global LAST_CHAT_HASH, LAST_SIDEBAR_HASH, LAST_FULL_SCAN_TIME
    ensure_default_desktop()
    cfg = load_config()
    
    img_wechat, rect, scale = grab_wechat_window(hwnd)
    if not img_wechat:
        return
        
    W, H = img_wechat.size
    if W <= 100 or H <= 100:
        return
        
    layout = compute_wechat_layout(W, H, img_wechat)
    arr_wechat = np.array(img_wechat)
    
    # -------------------------------------------------------------------------
    # ⚡ 像素级动态感知哨兵 (Pixel Sentinel: 20ms 极速变动感知，消灭 18s OCR 空转)
    # -------------------------------------------------------------------------
    crop_x1 = max(330, int(layout["chat_start_x"] + 10))
    crop_y1 = max(0, int(layout["header_h"] + 10))
    crop_x2 = min(W, int(W - 5))
    # 血泪坑：输入框上沿有闪烁光标，若把输入区算进哨兵，hash 每秒都变，
    # 会导致「永远全量 OCR」（CPU 70%+）且连发沉降判定永远误判。
    crop_y2 = max(crop_y1 + 20, min(H, int(H - layout["input_h"] - 8)))
    
    chat_patch = arr_wechat[crop_y1:crop_y2, crop_x1:crop_x2]
    curr_chat_hash = hashlib.md5(chat_patch.tobytes()).hexdigest()
    
    sidebar_patch = arr_wechat[:, :layout["chat_start_x"]]
    sr, sg, sb = sidebar_patch[:, :, 0].astype(int), sidebar_patch[:, :, 1].astype(int), sidebar_patch[:, :, 2].astype(int)
    red_mask = (sr >= 190) & (sg <= 115) & (sb <= 115) & (sr > sg + 55)
    has_red_pixels = np.sum(red_mask) >= 15
    
    curr_sidebar_hash = hashlib.md5(sidebar_patch.tobytes()).hexdigest()
    now_ts = time.time()
    
    # 若画面未发生任何像素改变、无红点且距离上次扫描 < 1.0 秒：直接 20ms 极速放行！
    if curr_chat_hash == LAST_CHAT_HASH and curr_sidebar_hash == LAST_SIDEBAR_HASH and not has_red_pixels and (now_ts - LAST_FULL_SCAN_TIME) < 1.0:
        return
        
    LAST_CHAT_HASH = curr_chat_hash
    LAST_SIDEBAR_HASH = curr_sidebar_hash
    LAST_FULL_SCAN_TIME = now_ts
    
    # 动态下采样加速 OCR (等比下采样至 1000 宽，耗时从 5.5s 降至 0.8s)
    target_w = 1000
    downscale_ratio = 1000.0 / W if W > 1100 else 1.0
    if downscale_ratio < 1.0:
        target_h = int(H * downscale_ratio)
        img_for_ocr = img_wechat.resize((target_w, target_h), Image.Resampling.BILINEAR)
        res_raw, _ = _ocr_call(img_for_ocr)
        res = []
        for b, txt, sc in (res_raw or []):
            mapped_box = [[pt[0] / downscale_ratio, pt[1] / downscale_ratio] for pt in b]
            res.append((mapped_box, txt, sc))
    else:
        res, _ = _ocr_call(img_wechat)
        
    if not res:
        return
        
    whitelist_mode = cfg.get("whitelist_mode", True)
    # contacts 里的备注也算白名单，与旧 whitelist 字符串取并集（兼容老配置）
    whitelist = effective_whitelist(cfg)
    blacklist = [b for b in (cfg.get("blacklist") or []) if isinstance(b, str)]
    ignored_keywords = cfg.get("ignored_keywords", [])

    def is_valid_non_whitelist_target(name_str):
        if not name_str or len(str(name_str).strip()) < 2:
            return False
        n = str(name_str).strip()
        if any(b_name in n for b_name in blacklist if b_name.strip()):
            return False
        # 群聊防火墙：精准检测群人数标识 (如 "(25)"、" (25)") 与群关键词，排除消息预览中的量词数字 (如 "(2)楼"、"(5)分")
        if is_group_chat_name(n):
            return False
        if n in ["微信", "搜索", "文件传输助手", "朋友圈", "订阅号", "服务通知", "腾讯新闻"]:
            return False
        return True
    
    sidebar_w = layout["sidebar_w"]
    chat_start_x = layout["chat_start_x"]
    
    # -------------------------------------------------------------------------
    # 轨 1: 左侧会话全局看板 (提取高浓度绿色激活项 & 红点列表)
    # -------------------------------------------------------------------------
    session_items = []
    for b, txt, sc in res:
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        if sidebar_w <= cx <= chat_start_x and (layout["header_h"] + 5) <= cy <= (layout["total_h_px"] - 20):
            session_items.append((cy, cx, txt.strip(), b))
            
    session_items.sort(key=lambda x: x[0])
    
    active_green_text = None
    max_green_count = 0
    red_badge_candidates = []
    
    for cy, cx, text_clean, b in session_items:
        if any(b_name in text_clean for b_name in blacklist if b_name.strip()):
            continue
            
        # 统计该行绿色像素强度 (寻找真正被激活的会话，转 int 杜绝浅色背景回绕溢出)
        min_y = max(0, int(cy - 18))
        max_y = min(H, int(cy + 18))
        patch = arr_wechat[min_y:max_y, sidebar_w:chat_start_x]
        pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
        green_pixels = np.sum((pg > pr + 25) & (pg > pb + 25) & (pg > 80))
        
        if green_pixels > 1200 and green_pixels > max_green_count:
            max_green_count = green_pixels
            active_green_text = text_clean
            
        # 判定是否有新消息红点 (基于文字动态几何锚定)
        if whitelist_mode:
            matched, target = is_name_matched_strict(text_clean, whitelist)
        else:
            matched = is_valid_non_whitelist_target(text_clean)
            # 提取纯净的好友昵称（剥离行首红点计数与尾部消息预览）
            clean_name = re.sub(r'^\d{1,2}(?=[^\d])', '', text_clean).strip()
            target = re.split(r'\s+', clean_name, maxsplit=1)[0].strip()
        if matched and has_red_badge_by_text_anchor(arr_wechat, b, layout):
            red_badge_candidates.append((cy, target))
                
    # -------------------------------------------------------------------------
    # 轨 2: 右侧微观聊天区 (提取顶栏标题 & 消息气泡)
    # -------------------------------------------------------------------------
    active_right_title = extract_active_chat_title(res, layout)
    if not active_right_title:
        # 整幅 OCR 经常漏掉 2~3 个字的顶栏标题（detection 阈值问题）→ 裁顶栏单独 OCR 兜底。
        # 血泪坑：少了这一步，主循环会一直显示「顶栏空」→ 认不出当前会话 → 静默不回复。
        active_right_title = _ocr_header_title(img_wechat, layout)
    
    # -------------------------------------------------------------------------
    # 🌟 会话目标识别与门禁 (Whitelist Gating / Non-Whitelist Firewall)
    # -------------------------------------------------------------------------
    current_active_target = None
    
    if whitelist_mode:
        # 白名单模式：必须精准命中白名单中的指定好友
        if active_right_title:
            matched_right, right_target = is_name_matched_strict(active_right_title, whitelist)
            if matched_right:
                current_active_target = right_target
                
        if not current_active_target and active_green_text:
            matched_left, left_target = is_name_matched_strict(active_green_text, whitelist)
            if matched_left:
                current_active_target = left_target
    else:
        # 非白名单模式：允许回复普通单人好友，强制过滤黑名单、服务号与群聊
        if active_right_title and is_valid_non_whitelist_target(active_right_title):
            current_active_target = active_right_title.strip()
        elif active_green_text and is_valid_non_whitelist_target(active_green_text):
            clean_green = re.sub(r'^\d{1,2}(?=[^\d])', '', active_green_text).strip()
            current_active_target = re.split(r'\s+', clean_green, maxsplit=1)[0].strip()
    
    if not current_active_target:
        _k = ("__target__", "none")
        if now_ts - SKIP_LOG_AT.get(_k, 0) >= 60.0:
            SKIP_LOG_AT[_k] = now_ts
            log(f"🔍 [巡检] 未识别当前好友：顶栏「{active_right_title or '空'}」 绿色会话「{(active_green_text or '')[:16]}」")

    if current_active_target:
        chat_bubbles = parse_chat_bubbles_chromatic(res, img_wechat, layout, ignored_keywords)
        if chat_bubbles:
            pending_bubbles, combined_text = extract_pending_incoming_messages(chat_bubbles)
            # 🎤 语音消息：只看到时长、还没转出文字时，等微信自动转写后重扫一次
            if pending_bubbles and pending_bubbles[-1].get('voice_missing_text'):
                log(f"🎤 [语音] 收到【{current_active_target}】的语音，等待微信自动转文字…")
                retried = _retry_voice_transcript(hwnd, layout, ignored_keywords, pending_bubbles)
                if retried:
                    pending_bubbles, combined_text = retried
                    log(f"🎤 [语音] 已拿到转写：\"{combined_text}\"")
            # 🔥 第一道铁闸：只要当前好友有未回复新消息，绝对锁定在当前窗口就地秒回！
            if pending_bubbles and combined_text:
                current_sig = _msg_signature(current_active_target, combined_text, pending_bubbles)
                if LAST_PROCESSED_SIGNATURE.get(current_active_target) == current_sig:
                    # 这条消息此前登记过「已处理」（可能是发送脱靶熔断/空回复），
                    # 若它一直挂在待回复列表里，超过 TTL 必须允许重试，否则会永久静默卡死。
                    _waited = now_ts - LAST_PROCESSED_AT.get(current_active_target, now_ts)
                    if _waited >= 90.0:
                        log(f"♻️ [超时重试] 好友【{current_active_target}】的消息已 {int(_waited)}s 未成功回复，清除登记后重试一次")
                        LAST_PROCESSED_SIGNATURE.pop(current_active_target, None)
                        SEND_FAIL_RETRY.pop(current_sig, None)
                    else:
                        _k = (current_active_target, "processed")
                        if now_ts - SKIP_LOG_AT.get(_k, 0) >= 30.0:
                            SKIP_LOG_AT[_k] = now_ts
                            log(f"⏭️ [跳过] 好友【{current_active_target}】这条消息已登记处理过（{int(_waited)}s 前），若持续未回将在 90s 后自动重试: \"{combined_text[:24]}\"")
                if LAST_PROCESSED_SIGNATURE.get(current_active_target) != current_sig:
                    # 🔒 Agent 在飞/待发闸门：同一条消息已经在后台思考或排队待发，本轮直接放行，
                    #    绝不再重复提交（否则每个心跳都会重复问 agent，烧额度还会乱序）。
                    if agent_busy_for(current_active_target, current_sig):
                        return
                    
                    # 🌟 极速动态像素沉降判定 (Dynamic Pixel Settling):
                    # 短暂等待 0.35 秒 (人类打字发句最小停顿)，若右侧气泡像素无变动，立即判定单句发送完毕，0秒多余等待直接交由 AI！
                    # 若检测到右侧像素哈希发生改变（连发新气泡），则等待 0.15 秒排版沉降后增量 OCR 聚合全部短句！
                    time.sleep(0.35)
                    img_latest, _, _ = grab_wechat_window(hwnd)
                    if img_latest:
                        latest_patch = np.array(img_latest)[crop_y1:crop_y2, crop_x1:crop_x2]
                        new_chat_hash = hashlib.md5(latest_patch.tobytes()).hexdigest()
                        if new_chat_hash != curr_chat_hash:
                            time.sleep(0.15) # 等待微信 Qt5 文字渲染排版彻底完成
                            img_settled, _, _ = grab_wechat_window(hwnd)
                            if img_settled:
                                img_chat_crop_lat = img_settled.crop((crop_x1, crop_y1, crop_x2, crop_y2))
                                res_lat_raw, _ = _ocr_call(img_chat_crop_lat)
                                res_lat = []
                                for b, txt, sc in (res_lat_raw or []):
                                    res_lat.append(([[pt[0] + crop_x1, pt[1] + crop_y1] for pt in b], txt, sc))
                                chat_bubbles_lat = parse_chat_bubbles_chromatic(res_lat, img_settled, layout, ignored_keywords)
                                pending_lat, combined_lat = extract_pending_incoming_messages(chat_bubbles_lat)
                                if pending_lat and combined_lat:
                                    pending_bubbles = pending_lat
                                    combined_text = combined_lat
                                    current_sig = _msg_signature(current_active_target, combined_text, pending_bubbles)
                            
                    log(f"\n📩 [当前会话锁定·连发聚合] 收到当前好友【{current_active_target}】新消息({len(pending_bubbles)}条): \"{combined_text}\"")
                    # ================= P0/P1: 交给 Agent 后台决策，主循环立刻返回继续巡检 =================
                    if agent_enabled(cfg):
                        job_id = submit_agent_job(current_active_target, combined_text, current_sig, cfg, source="scan")
                        if job_id:
                            # 同一好友的新消息已登记在飞；本轮不阻塞，等 poll_agent_results 取回结果
                            return
                        log(f"⚠️ [Agent 提交失败] 好友【{current_active_target}】退回同步路径")
                        decision = generate_agent_reply(current_active_target, combined_text)
                        _handle_decision_sync(current_active_target, combined_text, current_sig,
                                              decision, hwnd, rect, layout, cfg)
                        return

                    # ---------- 老的大模型直连路径（配置里关掉 agent 时才走这里） ----------
                    ai_reply = generate_ai_reply(current_active_target, combined_text)
                    if ai_reply:
                        _deliver_reply(current_active_target, ai_reply, current_sig,
                                       hwnd, rect, layout, cfg, source="legacy")
                    else:
                        # 生成回复为空（如未填 Key 或 API 异常），记录告警并登记签名防空转
                        log(f"⚠️ [AI生成跳过] 好友【{current_active_target}】消息未获得有效AI回复 (API超时/网络抖动或未配Key)，登记签名防空转。")
                        LAST_PROCESSED_SIGNATURE[current_active_target] = current_sig
                        LAST_PROCESSED_AT[current_active_target] = time.time()

                    # 本次已处理新消息，返回结束本轮调度
                    return
                elif current_sig in SEND_FAIL_RETRY:
                    # 处于重试等待中，坚守当前窗口
                    return

    # -------------------------------------------------------------------------
    # 协同通道 B: 只有确认当前会话已彻底回完(最新为我发出的绿底)，才极速切入其他白名单好友红点
    # -------------------------------------------------------------------------
    if red_badge_candidates:
        now_ts = time.time()
        valid_candidates = []
        for cand_y, cand_target in red_badge_candidates:
            if current_active_target and cand_target == current_active_target:
                continue
            if active_right_title and is_name_matched_strict(active_right_title, [cand_target])[0]:
                continue
            # 防空转冷却：若 3 秒内刚点击过该联系人且未产生新回复，跳过防死循环
            if cand_target in LAST_CLICKED_CANDIDATE_TIME and (now_ts - LAST_CLICKED_CANDIDATE_TIME[cand_target]) < 3.0:
                continue
            valid_candidates.append((cand_y, cand_target))
            
        if valid_candidates:
            cand_y, cand_target = valid_candidates[0]
            LAST_CLICKED_CANDIDATE_TIME[cand_target] = now_ts
            log(f"\n🔔 [当前已回完·极速切入] 发现白名单好友【{cand_target}】未读红点，正在锁定切入...")
            hidden_mode = bool(lx and lx.is_hidden(hwnd))
            if hidden_mode:
                # 离屏隐藏时屏幕坐标上没有窗口，XTest 鼠标点不到它：临时召回再点
                log("🖥️ [离屏] 需要切入新会话，临时召回微信窗口（点完立刻重新隐藏）")
                lx.show_window(hwnd)
                time.sleep(0.25)
            activate_wechat(hwnd)
            
            # 点击左侧联系人项目中心位置 (sidebar_w + 0.45*(chat_start_x - sidebar_w), cand_y)
            click_x = int(layout["sidebar_w"] + 0.45 * (layout["chat_start_x"] - layout["sidebar_w"]))
            post_click_client_point(hwnd, click_x, int(cand_y))
            time.sleep(0.35) # 充分等待 Qt5 引擎将右侧所有新消息气泡 100% 渲染绘制完成
            if hidden_mode:
                lx.hide_window(hwnd)
                time.sleep(0.20)
            
            # 强制重置像素哨兵哈希，确保切入后下个周期 100% 触发全量 OCR 读取
            LAST_CHAT_HASH = None
            LAST_SIDEBAR_HASH = None
            LAST_FULL_SCAN_TIME = 0
            
            # 消除函数递归，直接返回交由外层主循环自然调度，彻底杜绝调用栈累积与内存堆叠
            return

# =============================================================================
# 8.5 主动发送（P2）：本地 HTTP 接口 + 安全护栏
#     外部（cron / 脚本 / MCP / 别的 agent）往 127.0.0.1:8765 发一条 POST /send，
#     机器人就主动找这个好友、把话发出去 —— 不依赖对方先发消息。
#     护栏是硬性的：白名单、频率、确认开关，任一不过就拒绝，绝不硬发。
# =============================================================================
PENDING_CONFIRM_FILE = os.path.join(DATA_DIR, "pending_confirm.jsonl")

# 系统号：主动发送永远不碰
SYSTEM_ACCOUNTS = {
    "微信", "搜索", "文件传输助手", "朋友圈", "订阅号", "服务通知",
    "腾讯新闻", "微信团队", "微信支付", "公众号", "服务号", "订阅号消息",
}


def proactive_cfg(cfg=None):
    """取主动发送配置并补默认值。"""
    cfg = cfg if cfg is not None else load_config()
    ps = dict(cfg.get("proactive_send") or {})
    ps.setdefault("enabled", False)
    ps.setdefault("host", "127.0.0.1")
    ps.setdefault("port", 8765)
    ps.setdefault("whitelist_only", True)
    ps.setdefault("max_per_hour", 3)
    ps.setdefault("max_per_day", 20)
    ps.setdefault("require_confirm", False)
    ps.setdefault("allow_search", True)
    ps.setdefault("confirm_expire_hours", 12)
    return ps


def _proactive_prune(now_ts):
    """清掉限流账本里超过 24 小时的旧记录。"""
    cutoff = now_ts - 86400
    PROACTIVE_SENT_LOG[:] = [x for x in PROACTIVE_SENT_LOG if x[0] >= cutoff]


def proactive_check(cfg, target):
    """
    主动发送前置检查。返回 (ok: bool, code: str, detail: str)。
    code 取值：disabled / not_whitelisted / rate_hour / rate_day / ok
    """
    ps = proactive_cfg(cfg)
    if not ps.get("enabled"):
        return False, "disabled", "主动发送未启用（proactive_send.enabled=false）"

    t = str(target or "").strip()
    if not t:
        return False, "bad_target", "target 不能为空"
    # 允许直接传微信号：统一成备注显示名再做所有校验
    t = canonical_target(t, cfg) or t

    # 群聊 / 黑名单 / 系统号 —— 无论开不开白名单都必须拦死。
    # 主动发送是「自己开口」，发错群社死，所以这里比被动回复更严。
    if is_group_chat_name(t):
        return False, "not_whitelisted", f"【{t}】是群聊，主动发送只允许单人好友"
    blacklist = [b for b in (cfg.get("blacklist") or []) if isinstance(b, str) and b.strip()]
    if any(b_name in t for b_name in blacklist):
        return False, "not_whitelisted", f"【{t}】命中黑名单，拒绝主动发送"
    if t in SYSTEM_ACCOUNTS:
        return False, "not_whitelisted", f"【{t}】是系统号，拒绝主动发送"

    if ps.get("whitelist_only"):
        whitelist = effective_whitelist(cfg)
        if not is_name_matched_strict(t, whitelist)[0]:
            return False, "not_whitelisted", f"【{t}】不在白名单，拒绝主动发送"

    now_ts = time.time()
    with AGENT_LOCK:
        _proactive_prune(now_ts)
        hour_cnt = sum(1 for ts, _ in PROACTIVE_SENT_LOG if now_ts - ts < 3600)
        day_cnt = len(PROACTIVE_SENT_LOG)
    if hour_cnt >= int(ps.get("max_per_hour", 3)):
        return False, "rate_hour", f"本小时已主动发送 {hour_cnt} 条，达到上限 {ps.get('max_per_hour')}"
    if day_cnt >= int(ps.get("max_per_day", 20)):
        return False, "rate_day", f"今天已主动发送 {day_cnt} 条，达到上限 {ps.get('max_per_day')}"

    return True, "ok", ""


def _append_pending_confirm_legacy(target, text, source="api"):
    """[已废弃·禁止调用] 旧版 append-only 写法。现役实现是同名函数（P3 队列版）。"""
    record = {
        "id": f"p{int(time.time() * 1000)}",
        "target": target,
        "text": text,
        "source": source,
        "created_ts": time.time(),
    }
    try:
        with open(PENDING_CONFIRM_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log(f"📝 [待确认] 主动消息已入队待人工确认: 【{target}】{text}")
        return record
    except Exception as exc:
        log(f"🚨 [待确认写入失败] {exc}")
        return None
def proactive_send(target, text, cfg=None, source="api"):
    """
    真正执行一次主动发送。返回 dict：
      {"ok":bool, "code":str, "detail":str, "sent":bool, "queued":bool}
    """
    cfg = cfg if cfg is not None else load_config()
    ok, code, detail = proactive_check(cfg, target)
    if not ok:
        log(f"🚫 [主动发送被拒] {code}: {detail}")
        return {"ok": False, "code": code, "detail": detail, "sent": False, "queued": False}

    # 传微信号也行：统一成备注显示名，后面所有校验/日志/复核都以备注为准
    t = canonical_target(str(target or "").strip(), cfg) or str(target or "").strip()
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "code": "bad_text", "detail": "text 不能为空", "sent": False, "queued": False}

    ps = proactive_cfg(cfg)
    if ps.get("require_confirm"):
        rec = _append_pending_confirm(t, body, source)
        if rec:
            return {"ok": True, "code": "pending_confirm", "detail": "已进入待确认队列",
                    "sent": False, "queued": True, "id": rec["id"]}
        return {"ok": False, "code": "confirm_write_failed", "detail": "待确认队列写入失败",
                "sent": False, "queued": False}

    hwnd, _ver = get_wechat_hwnd()
    if not hwnd:
        log("🚫 [主动发送失败] 没找到微信窗口")
        return {"ok": False, "code": "no_wechat", "detail": "未找到微信主窗口",
                "sent": False, "queued": False}

    if not find_contact(hwnd, t, cfg):
        log(f"🚫 [主动发送失败] 定位不到好友【{t}】")
        return {"ok": False, "code": "contact_not_found", "detail": f"定位不到好友【{t}】",
                "sent": False, "queued": False}

    img, rect, _scale = grab_wechat_window(hwnd)
    if not img:
        return {"ok": False, "code": "grab_failed", "detail": "抓图失败", "sent": False, "queued": False}
    layout = compute_wechat_layout(img.size[0], img.size[1], img)

    log(f"📮 [主动发送] 好友【{t}】: {body}")
    sent = send_reply_instant(hwnd, rect, layout, t, body)
    if sent:
        with AGENT_LOCK:
            PROACTIVE_SENT_LOG.append((time.time(), t))
        # 主动发出去的消息也算已处理，防止扫描循环把它当新消息再回一遍
        LAST_PROCESSED_SIGNATURE.pop(t, None)
    return {"ok": bool(sent), "code": "ok" if sent else "send_failed",
            "detail": "" if sent else "发送未通过闭环验收", "sent": bool(sent), "queued": False}


def _contacts_api_update(body):
    """
    通讯录写入接口，支持三种写法：
      1) {"contacts": [{"remark":"张三","wxid":"zhangsan_88"}, ...]}  —— 整体替换
      2) {"remark": "张三", "wxid": "zhangsan_88"}                    —— 单条新增/覆盖
      3) {"remove": "张三"}                                           —— 删除一条
    返回 (ok: bool, detail: str)。
    """
    cfg = load_config()
    current = load_contacts(cfg)

    if "remove" in body:
        rm = str(body.get("remove") or "").strip()
        kept = [c for c in current if c["remark"] != rm and c["wxid"] != rm]
        if len(kept) == len(current):
            return False, f"没找到【{rm}】"
        save_contacts(kept)
        return True, f"已删除【{rm}】"

    if isinstance(body.get("contacts"), list):
        new = []
        for item in body["contacts"]:
            if isinstance(item, str):
                if item.strip():
                    new.append(item.strip())
            elif isinstance(item, dict):
                rec = {"remark": str(item.get("remark") or "").strip(),
                       "wxid": str(item.get("wxid") or "").strip()}
                if rec["remark"] or rec["wxid"]:
                    new.append(rec)
        save_contacts(new)
        return True, f"已整体替换为 {len(new)} 条"

    remark = str(body.get("remark") or "").strip()
    wxid = str(body.get("wxid") or "").strip()
    if not remark and not wxid:
        return False, "需要 contacts 数组，或 remark / wxid 字段"

    out, replaced = [], False
    for c in current:
        hit = (remark and c["remark"] == remark) or (wxid and c["wxid"].lower() == wxid.lower())
        if hit:
            out.append({"remark": remark or c["remark"], "wxid": wxid or c["wxid"]})
            replaced = True
        else:
            out.append(c)
    if not replaced:
        out.append({"remark": remark or wxid, "wxid": wxid})
    save_contacts(out)
    return True, ("已更新" if replaced else "已新增") + f"【{remark or wxid}】"


def _cjk_count(text):
    return sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)


def _looks_like_gbk_mojibake(text):
    """
    判断一段 UTF-8 解码结果是不是「GBK 字节被当成 UTF-8」产生的乱码。

    典型特征：出现希腊字母 / 西里尔字母 / 替换符，且一个汉字都没有。
    例：GBK 的「微」= CE A2，UTF-8 恰好能解成希腊字母 '΢'。
    故意不看 Latin-1 补充区，免得把正常的 café 之类误判。
    """
    if not text:
        return False
    weird = False
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            return False
        if 0x0370 <= o <= 0x04FF or o == 0xFFFD:
            weird = True
    return weird


def _decode_smart(raw):
    """
    把原始 HTTP 请求体 / 查询串字节解成文本。

    Windows 终端默认 GBK，`curl -d "target=微信 2"` 发出去的就是 GBK 字节；
    直接按 UTF-8 解会得到 '΢�� 2' 这种半截乱码，白名单永远匹配不上。
    策略：UTF-8 严格 -> GB18030 严格 -> 按乱码特征择优选。
    """
    if isinstance(raw, str):
        return raw
    data = bytes(raw or b"")
    if not data:
        return ""
    try:
        s = data.decode("utf-8")
    except Exception:
        s = None
    try:
        g = data.decode("gb18030")
    except Exception:
        g = None

    if s is None and g is None:
        return data.decode("utf-8", "replace")
    if s is None:
        return g
    if g is None:
        return s
    if _looks_like_gbk_mojibake(s) and _cjk_count(g) > 0:
        return g
    return s


def _proactive_http_handler():
    """构造 HTTP 请求处理器（延迟导入，避免无网络场景下的额外开销）。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "WeChatBotProactive/1.0"

        def log_message(self, fmt, *args):  # 静音默认 stderr 日志
            pass

        def _json(self, code, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass

        def _query_params(self):
            try:
                qs = urllib.parse.urlsplit(self.path).query
                # http.server 用 latin-1 解请求行：终端若是 GBK，中文查询参数
                # 会变成乱码字节。这里还原成原始字节再按 UTF-8/GBK 智能解码。
                try:
                    qs = _decode_smart(qs.encode("latin-1", "ignore"))
                except Exception:
                    pass
                return dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
            except Exception:
                return {}

        def _read_body(self):
            """
            读请求体：先按 JSON 解析，失败再退回 form-urlencoded / query 参数。

            Windows 命令行有两个坑，都已兜住：
              1) PowerShell 会吃掉 `-d '{"target":"张三"}'` 里的双引号，
                 服务端只收到 `{target:张三}` 这种非法 JSON；
              2) 终端编码是 GBK 时，中文会以 GBK 字节发过来，按 UTF-8 解就是乱码。
            所以允许三种等价写法，并自动做 UTF-8 / GB18030 双解码：
              POST /send?target=张三&text=你好
              POST /send -d "target=张三&text=你好"
              POST /send -d {"target":"张三","text":"你好"}
            """
            self._raw_body = ""
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 0:
                    self._raw_body = _decode_smart(self.rfile.read(n)).strip()
            except Exception:
                self._raw_body = ""

            data = {}
            raw = self._raw_body
            if raw:
                parsed = None
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
                elif parsed is not None:
                    data = {"value": parsed}
                else:
                    # 看起来像 JSON（以 { 或 [ 开头）却解析失败，说明是被命令行的引号
                    # 吃掉了一条引号之类的坏 JSON —— 直接报错，别悄悄当成表单。
                    if raw[:1] in ("{", "["):
                        return None
                    try:
                        data = dict(urllib.parse.parse_qsl(raw.lstrip("?"), keep_blank_values=True))
                    except Exception:
                        data = {}
                    if not data:
                        return None
            q = self._query_params()
            if q:
                for k, v in q.items():
                    data.setdefault(k, v)
            return data

        def do_GET(self):
            path = (urllib.parse.urlsplit(self.path).path.rstrip("/") or "/")
            if path in ("/status", "/health"):
                cfg = load_config()
                ps = proactive_cfg(cfg)
                now_ts = time.time()
                with AGENT_LOCK:
                    _proactive_prune(now_ts)
                    hour_cnt = sum(1 for ts, _ in PROACTIVE_SENT_LOG if now_ts - ts < 3600)
                    day_cnt = len(PROACTIVE_SENT_LOG)
                return self._json(200, {
                    "ok": True,
                    "enabled": ps.get("enabled"),
                    "require_confirm": ps.get("require_confirm"),
                    "whitelist_only": ps.get("whitelist_only"),
                    "sent_last_hour": hour_cnt,
                    "sent_today": day_cnt,
                    "max_per_hour": ps.get("max_per_hour"),
                    "max_per_day": ps.get("max_per_day"),
                    "pending_confirm": len(list_pending()),
                    "wechat_running": WECHAT_HWND_CACHE[0] is not None,
                    "compositing": bool(lx and lx.compositing_enabled()),
                })
            if path == "/pending":
                return self._json(200, {"ok": True, "items": list_pending()})
            if path == "/contacts":
                return self._json(200, {"ok": True, "contacts": load_contacts(load_config())})
            if path in ("/sessions", "/contexts"):
                cfg = load_config()
                client = get_agent_client(cfg)
                if client is None:
                    return self._json(503, {"ok": False, "code": "agent_unavailable"})
                items = []
                for c in load_contacts(cfg):
                    name = str((c or {}).get("remark") or "").strip()
                    if not name:
                        continue
                    items.append({"target": name, "context": client.effective_context_id(name)})
                return self._json(200, {"ok": True, "sessions": items})
            if path == "/rotate":
                return self._json(400, {"ok": False, "code": "use_post",
                                        "detail": "请用 POST /rotate，body: {\"target\":\"张三\"}"})
            return self._json(404, {"ok": False, "code": "not_found"})

        def do_POST(self):
            path = (urllib.parse.urlsplit(self.path).path.rstrip("/") or "/")
            body = self._read_body()
            if body is None:
                return self._json(400, {
                    "ok": False, "code": "bad_json",
                    "detail": "请求体不是合法 JSON。命令行引号常被吃掉（尤其 PowerShell），"
                              "可改用：POST /send?target=张三&text=你好 "
                              "或 -d \"target=张三&text=你好\"",
                    "received": (getattr(self, "_raw_body", "") or "")[:120],
                })

            if path == "/send":
                target = body.get("target") or body.get("to") or body.get("contact")
                text = body.get("text") or body.get("message") or body.get("content")
                if not target or not text:
                    return self._json(400, {"ok": False, "code": "missing_field",
                                            "detail": "需要 target 和 text"})
                result = proactive_send(target, text, source="api")
                return self._json(200 if result.get("ok") else 403, result)

            if path == "/confirm":
                result = confirm_pending(body.get("id"))
                return self._json(200 if result.get("ok") else 403, result)

            if path == "/reject":
                result = reject_pending(body.get("id"), body.get("reason") or "")
                return self._json(200 if result.get("ok") else 404, result)

            if path == "/rotate":
                target = str(body.get("target") or body.get("contact") or "").strip()
                if not target:
                    return self._json(400, {"ok": False, "code": "missing_field",
                                            "detail": "需要 target（好友备注或微信号）"})
                client = get_agent_client(load_config())
                if client is None:
                    return self._json(503, {"ok": False, "code": "agent_unavailable"})
                base = client.context_id_for(target)
                new_ctx = client.rotate_context_id(base)
                log(f"🔄 [会话轮换] 好友【{target}】-> 新会话 {new_ctx}（下一条消息起生效）")
                return self._json(200, {"ok": True, "target": target, "context": new_ctx})

            if path == "/contacts":
                ok, detail = _contacts_api_update(body)
                return self._json(200 if ok else 400, {
                    "ok": ok, "detail": detail,
                    "contacts": load_contacts(load_config()),
                })

            return self._json(404, {"ok": False, "code": "not_found"})

    return Handler


def start_proactive_server(cfg=None):
    """后台线程起一个只监听 127.0.0.1 的 HTTP 服务（幂等，重复调用不会起两个）。"""
    if PROACTIVE_SERVER[0] is not None:
        return PROACTIVE_SERVER[0]
    cfg = cfg if cfg is not None else load_config()
    ps = proactive_cfg(cfg)
    if not ps.get("enabled"):
        log("ℹ️ [主动发送] 未启用（proactive_send.enabled=false），不启动本地接口")
        return None

    from http.server import ThreadingHTTPServer

    host = ps.get("host", "127.0.0.1")
    port = int(ps.get("port", 8765))
    try:
        srv = ThreadingHTTPServer((host, port), _proactive_http_handler())
    except Exception as exc:
        log(f"🚨 [主动发送接口启动失败] {host}:{port} -> {exc}")
        return None
    srv.daemon_threads = True

    def _run():
        log(f"🌐 [主动发送接口] 已监听 http://{host}:{port}  (POST /send, GET /status, GET/POST /contacts)")
        try:
            srv.serve_forever(poll_interval=0.5)
        except Exception as exc:
            log(f"🚨 [主动发送接口异常退出] {exc}")

    threading.Thread(target=_run, name="proactive-http", daemon=True).start()
    PROACTIVE_SERVER[0] = srv
    return srv


def stop_proactive_server():
    srv = PROACTIVE_SERVER[0]
    if srv is None:
        return
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass
    PROACTIVE_SERVER[0] = None
    log("🌐 [主动发送接口] 已关闭")


# =============================================================================
# 9. DAEMON MAIN LOOP
# =============================================================================
def main_loop():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
        
    log("=" * 60)
    log("🚀 PC 微信真人拟真 AI 自动回复系统【左右双轨协同感知版】已启动")
    log("   左轨看板: 绿色激活项背景判定 + 宽容度红点巡检")
    log("   右轨微观: 顶栏标题交叉锚定 + 贴底气泡真彩流向分析")
    log("   协同仲裁: 三元协同指纹去重，绝不漏掉任何短消息与标点")
    cfg_boot = load_config()
    if agent_enabled(cfg_boot):
        _c = get_agent_client(cfg_boot)
        log(f"🧠 大脑: DSH Agent ({_c.url if _c else '未就绪'})，后台 4 线程并发决策")
    else:
        log("🧠 大脑: 老的大模型直连 /chat/completions")
    if proactive_cfg(cfg_boot).get("enabled"):
        _load_pending_confirm()
        start_proactive_server(cfg_boot)
    log("=" * 60)
    # 环境自检：桌面合成器会让 root 截图为陈旧画面，能自动关就关掉（用户 m11297 要求加校验）
    try:
        lx.ensure_no_compositing(log=log)
    except Exception as _e:
        log(f"⚠️ [环境] 合成器自检失败: {_e}")

    _compositor_next_check = 0.0
    while True:
        try:
            # 运行期每 20 秒复查一次：合成器可能被用户/设置中途打开
            _now = time.time()
            if _now >= _compositor_next_check:
                _compositor_next_check = _now + 20.0
                try:
                    if lx.compositing_enabled():
                        if not lx.ensure_no_compositing(log=log):
                            # 关不掉：每 2 分钟提醒一次，别刷屏
                            log("⚠️ [环境] 桌面合成器仍在开启：微信截图会是旧画面，请手动关闭（见上面命令）")
                            _compositor_next_check = _now + 120.0
                except Exception:
                    pass
            hwnd, ver = get_wechat_hwnd()
            hwnd, ver = get_wechat_hwnd()
            WECHAT_HWND_CACHE[0] = hwnd
            if hwnd:
                # ① 先取回后台 agent 的决策（到点的延时消息也在这里发出）
                sent = poll_agent_results(hwnd)
                # ② 本轮已经发过消息就不马上再扫，给微信渲染留出沉降时间
                if not sent:
                    scan_and_reply_wechat(hwnd)
            cfg = load_config()
            interval = cfg_float(cfg, "check_interval_seconds", 0.8)
            time.sleep(interval)
        except Exception as e:
            log(f"⚠️ [主循环未捕获异常] {e}")
            time.sleep(1.0)

if __name__ == "__main__":
    main_loop()

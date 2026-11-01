import os
import sys
import json
import time
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import psutil
import requests
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# Force UTF-8 stdout
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

if getattr(sys, 'frozen', False):
    WORKSPACE_DIR = os.path.dirname(sys.executable)
else:
    WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(WORKSPACE_DIR, "wechat_config_dev.json")
LOG_FILE = os.path.join(WORKSPACE_DIR, "wechat_bot_dev.log")
PID_FILE = os.path.join(WORKSPACE_DIR, "wechat_bot_dev.pid")
BOT_SCRIPT = os.path.join(WORKSPACE_DIR, "wechat_ai_bot_dev.py")

_K32 = None


def _kernel32():
    """带完整原型的 kernel32，避免 64 位句柄被 ctypes 默认 c_int 截断。"""
    global _K32
    if _K32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p,
            ctypes.POINTER(wintypes.DWORD)]
        _K32 = k32
    return _K32


def fast_process_names():
    """毫秒级返回 {pid: exe 文件名}。

    `psutil.process_iter(['pid','name'])` 在本机 419 个进程上要 **3.9 秒**，
    这是「点启动/停止卡一下」的真正元凶；而 EnumProcesses +
    QueryFullProcessImageNameW 只要 **0.04 秒**（实测快 100 倍）。
    取不到名字的（系统保护进程）直接跳过，它们不可能是我们的 python 守护。
    """
    names = {}
    try:
        arr = (ctypes.c_ulong * 8192)()
        needed = wintypes.DWORD(0)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcesses.restype = wintypes.BOOL
        psapi.EnumProcesses.argtypes = [
            ctypes.POINTER(ctypes.c_ulong), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        if not psapi.EnumProcesses(
                ctypes.cast(arr, ctypes.POINTER(ctypes.c_ulong)),
                ctypes.sizeof(arr), ctypes.byref(needed)):
            return names
        total = needed.value // ctypes.sizeof(ctypes.c_ulong)
    except Exception:
        return names

    k32 = _kernel32()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    for i in range(total):
        pid = arr[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                names[int(pid)] = os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    return names
KILL_SCRIPT = os.path.join(WORKSPACE_DIR, "kill_wechat_bot_dev.py")
PENDING_FILE = os.path.join(WORKSPACE_DIR, "pending_confirm.jsonl")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)

class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT)
    ]

def get_wechat_normal_rect(hwnd):
    """获取微信在正常桌面状态下的坐标与长宽 (即便当前最小化或离屏也能准确提取)"""
    try:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            r = wp.rcNormalPosition
            w = r.right - r.left
            h = r.bottom - r.top
            if w >= 400 and h >= 300 and r.left >= -500 and r.top >= -500:
                return [int(r.left), int(r.top), int(w), int(h)]
    except Exception:
        pass
    try:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w >= 400 and h >= 300 and rect.left >= -500 and rect.top >= -500:
            return [int(rect.left), int(rect.top), int(w), int(h)]
    except Exception:
        pass
    return None

def ensure_default_desktop():
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
            user32.CloseDesktop(h_desk)
    except Exception:
        pass

def get_wechat_hwnd():
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

DEFAULT_PROMPTS = {
    "🌟 真人日常 (默认)": "你现在是微信账号的主人（真实本人）。请以我（主人本人）的第一人称、自然口语、亲切随和的日常语气直接回复微信好友的消息。就像我本人在用手机打字聊天一样自然。严禁透露任何AI、机器人或助理身份，严禁使用客套机械的客服腔，严禁输出任何解释或markdown格式，直接输出我回复的那句话。",
    "🤣 随和幽默 / 接梗达人": "你现在是微信账号的主人（真实本人）。请以极具幽默感、机智开朗、会接梗的真人语气回复好友。善于敏锐捕捉对方话里的笑点与细节，秒懂热梗并顺势幽默抛梗接招，说话风趣机敏、轻松洒脱、情商极高，让人觉得和你聊天特别有意思、超解压。严禁严肃机械，严禁使用客服腔，严禁透露AI身份，直接输出幽默自然的回复短句。",
    "🌹 暧昧推拉 / 情感高手": "你现在是真实号主本人，正在回复暧昧对象。语气要有高情商的推拉感和适度的情感张力，既幽默风趣又带一点恰到好处的撩人和嘴甜。懂分寸、不油腻、欲擒故纵，善于发现对方话里的细节并给出有趣的互动。严禁生硬直白，严禁透露AI身份，直接输出真实聊天的短句。",
    "💕 甜蜜恋人 / 女友专属": "你现在是我的替身，正在回复我的女朋友/老婆。请以真实男友的第一人称回复，语气超级宠溺、温柔、体贴且求生欲拉满。多用爱称（如宝贝、宝宝、乖等），多给足情绪价值，关心她的状态并主动报备，说话带点撒娇和甜意，严禁任何直男式说教或敷衍，严禁透露AI身份，直接输出日常打字的一两句话。",
    "😂 毒舌互怼 / 死党互损": "你现在是真实号主本人，正在回复多年的铁哥们/死党。语气要毒舌、嘴硬、爱开玩笑互损，毫不留情面地疯狂吐槽和接招，对方发什么都要幽默地怼回去或调侃两句，满嘴跑火车但感情很铁。严禁客套礼貌，严禁使用敬语，严禁透露AI身份，直接输出回怼的微信短句。",
    "⚔️ 阴阳怪气 / 仇敌对线": "你现在是真实号主本人，正在回复讨厌的人或仇敌。语气要高冷、带有极具杀伤力的高级阴阳怪气与不屑。回怼精准有力、直击要害、体面扎心且绝不吃亏，不带脏字却能把对方气得半死。严禁客套，严禁热情，严禁透露AI身份，直接输出冷嘲热讽的回复。",
    "💼 职场专业 / 严谨得体": "你现在是微信账号的主人（真实本人）。请以成熟稳重、严谨得体、高效专业的职场沟通语气回复消息。态度谦逊有礼，表达逻辑清晰、条理分明，用词规范严谨，展现出极高的职业素养。严禁轻浮玩笑，严禁透露AI身份，直接输出专业得体的回复。",
    "⏳ 忙碌代接 / 稍后细聊": "你现在是微信账号的主人（真实本人）。请以短平快的日常语气回复好友，告知自己当前正处于忙碌状态（如正在开会、开车或手头有急事），先简要确认已收到消息，并主动说明稍后空下来立刻详细回复。态度亲切真诚，严禁透露AI身份，直接输出1句话。",
    "🧊 高冷简洁 / 话少字精": "你现在是微信账号的主人（真实本人）。请以高冷、极简、话少字精的语气回复好友。回答直奔主题，通常只用几个字或一小句话（如“好”、“收到”、“晚点说”、“行”），绝不多说半句废话，言简意赅。严禁热情啰嗦，严禁透露AI身份，直接输出极短的微信回复。",
    "☕ 温和体贴 / 治愈倾听": "你现在是微信账号的主人（真实本人）。请以温暖、沉稳、善解人意且富有同理心的语气回复好友。认真倾听对方的心情，给予充分的理解、肯定与情绪支撑，让人感到被重视和治愈。严禁冷淡评判或讲大道理，严禁透露AI身份，直接输出温暖真诚的话。",
    "✨ 元气可爱 / 活泼软萌": "你现在是微信账号的主人（真实本人）。请以元气满满、甜美活泼、软萌可爱的语气回复好友。说话积极开朗，适当带上可爱的语气助词（如呀、呢、哇、啦、~、哈哈等），像一个阳光充满活力的小太阳。严禁死板严肃，严禁透露AI身份，直接输出充满元气的可爱短句。",
    "✏️ 自由定制": "你现在是微信账号的主人（真实本人）。请以我本人的第一人称、自然随和的语气直接回复好友，直接输出回复内容。"
}

class WeChatBotGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self._after_ids = []
        self._closing = False
        self.title("微信AI自动回复")
        self.geometry("1320x880")
        self.minsize(1180, 760)
        
        ico_file = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_unread_ai_v4.ico")
        if os.path.exists(ico_file):
            try:
                self.iconbitmap(ico_file)
            except Exception:
                pass
        
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("vista")
        except Exception:
            pass

        self.config_data = self.load_config()
        self.log_pos = 0
        self.is_monitoring = True
        self._confirm_dialog_open = False

        self.create_widgets()
        self.load_settings_to_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_background_monitors()
        # P3: 待确认消息轮询（2 秒一次，有就弹窗）
        self._safe_after(2500, self.poll_pending_confirm)

    def _ui_call(self, fn, delay=0):
        """后台线程回主线程改 UI 的安全版本（窗口已关闭 / 无 mainloop 时静默跳过）。"""
        def _guarded():
            try:
                fn()
            except Exception:
                pass
        try:
            self.after(delay, _guarded)
        except Exception:
            pass

    def _safe_after(self, ms, fn):
        """登记 after 定时器，关窗时统一取消，避免 Tk 打印 invalid command name 堆栈。"""
        if getattr(self, "_closing", False):
            return None
        try:
            aid = self.after(ms, fn)
        except Exception:
            return None
        try:
            self._after_ids.append(aid)
        except Exception:
            pass
        return aid

    def destroy(self):
        self._closing = True
        for aid in list(getattr(self, "_after_ids", [])):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        try:
            self._after_ids.clear()
        except Exception:
            pass
        try:
            super().destroy()
        except Exception:
            pass

    def on_close(self):
        try:
            hwnd, _ = get_wechat_hwnd()
            if hwnd:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.left < -500 or rect.top < -500:
                    saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                    user32.ShowWindow(hwnd, 9) # SW_RESTORE
                    user32.SetWindowPos(hwnd, 0, saved[0], saved[1], saved[2], saved[3], 0x0040 | 0x0010)
        except Exception:
            pass
        self.is_monitoring = False
        self.destroy()

    def load_config(self):
        default = {
            "ai_engine": "openai_api",
            "openai_api": {
                "api_key": "",
                "api_base": "https://api.xiaomimimo.com/v1",
                "model": "mimo-v2.6-flash",
                "temperature": 0.7
            },
            "system_prompt": DEFAULT_PROMPTS["🌟 真人日常 (默认)"],
            "prompt_preset": "🌟 真人日常 (默认)",
            "custom_prompt": DEFAULT_PROMPTS["✏️ 自由定制"],
            "reply_prefix": "[AI自动回复] ",
            "include_prefix": False,
            "whitelist_mode": True,
            "whitelist": [],
            "contacts": [],
            "blacklist": [
                "微信团队",
                "文件传输助手",
                "腾讯新闻",
                "微信支付",
                "服务通知",
                "订阅号消息"
            ],
            "reply_delay_seconds": 0.0,
            "check_interval_seconds": 0.8,
            "auto_focus_wechat": False,
            "ignored_keywords": [
                "按住鼠标",
                "语音输入文字",
                "按住说话",
                "按Enter发送",
                "发送(S)"
            ],
            # 下面两块 GUI 界面里不编辑，但必须原样带回来，
            # 否则用户点一次「保存」就会把它们从配置文件里抹掉。
            "agent": {
                "enabled": True,
                "url": "http://127.0.0.1:44708/a2a",
                "card_url": "http://127.0.0.1:44708/.well-known/agent-card.json",
                "context_prefix": "wx-",
                "timeout_seconds": 180,
                "connect_timeout_seconds": 5,
                "retries": 1,
                "max_defer_seconds": 900,
                "context_map_file": "wechat_agent_contexts.json",
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
            }
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default.update(loaded)
                    # 健壮性：手改配置可能把名单写成 null/非字符串列表，统一纠正防启动崩溃
                    wl = default.get("whitelist")
                    default["whitelist"] = [w for w in wl if isinstance(w, str)] if isinstance(wl, list) else []
                    bl = default.get("blacklist")
                    default["blacklist"] = [b for b in bl if isinstance(b, str)] if isinstance(bl, list) else []
                    ct = default.get("contacts")
                    default["contacts"] = ct if isinstance(ct, list) else []
            except Exception as e:
                print(f"Error loading config: {e}")
        return default

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            messagebox.showerror("保存失败", f"保存配置文件失败: {e}")
            return False

    def create_widgets(self):
        # 1. Top Header Status Bar
        top_frame = ttk.LabelFrame(self, text=" 📊 系统状态与快捷控制 ", padding=(12, 8))
        top_frame.pack(fill=tk.X, padx=12, pady=6)

        # Status Indicators
        status_left = ttk.Frame(top_frame)
        status_left.pack(side=tk.LEFT, fill=tk.Y)
        
        self.lbl_wx_status = ttk.Label(status_left, text="⚪ 微信客户端: 检测中...", font=("微软雅黑", 9, "bold"))
        self.lbl_wx_status.pack(side=tk.LEFT, padx=(0, 20))

        self.lbl_bot_status = ttk.Label(status_left, text="⚪ AI 守护进程: 正在检测...", font=("微软雅黑", 9, "bold"))
        self.lbl_bot_status.pack(side=tk.LEFT, padx=10)

        # Action Buttons
        btn_box = ttk.Frame(top_frame)
        btn_box.pack(side=tk.RIGHT)

        self.btn_start = ttk.Button(btn_box, text="🚀 启动 AI 自动回复", command=self.start_bot)
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(btn_box, text="⏹️ 停止守护", command=self.stop_bot)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_hide_wx = ttk.Button(btn_box, text="👻 隐藏微信至离屏", command=self.toggle_offscreen_wechat)
        self.btn_hide_wx.pack(side=tk.LEFT, padx=4)

        self.btn_restart = ttk.Button(btn_box, text="🔄 重启服务", command=self.restart_bot)
        self.btn_restart.pack(side=tk.LEFT, padx=4)

        # 2. Main Notebook (Tabs)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self.tab_engine = ttk.Frame(self.notebook, padding=12)
        self.tab_prompt = ttk.Frame(self.notebook, padding=12)
        self.tab_contacts = ttk.Frame(self.notebook, padding=12)
        self.tab_whitelist = ttk.Frame(self.notebook, padding=12)
        self.tab_agent = ttk.Frame(self.notebook, padding=12)
        self.tab_test = ttk.Frame(self.notebook, padding=12)

        self.notebook.add(self.tab_engine, text=" ⚙️ AI 引擎设置 ")
        self.notebook.add(self.tab_prompt, text=" 🎭 人设与指令 Prompt ")
        self.notebook.add(self.tab_whitelist, text=" 🌓 白名单与防扰 ")
        self.notebook.add(self.tab_test, text=" 🧪 AI 仿真测试沙盒 ")
        self.notebook.add(self.tab_contacts, text=" 📇 通讯录映射 ")
        self.notebook.add(self.tab_agent, text=" 🤖 大脑 / 主动发送 ")

        self.build_tab_engine()
        self.build_tab_prompt()
        self.build_tab_contacts()
        self.build_tab_whitelist()
        self.build_tab_agent()
        self.build_tab_test()

        # Bottom Log Viewer
        log_frame = ttk.LabelFrame(self, text=" 📜 实时运行日志 (Auto-Scroll) ", padding=(10, 6))
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))

        log_ctrl_bar = ttk.Frame(log_frame)
        log_ctrl_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(log_ctrl_bar, text="实时日志流 (记录未读红点、好友消息、AI回复详情):", font=("微软雅黑", 8)).pack(side=tk.LEFT)
        
        ttk.Button(log_ctrl_bar, text="清空日志", command=self.clear_logs).pack(side=tk.RIGHT, padx=2)
        ttk.Button(log_ctrl_bar, text="打开日志文件", command=self.open_log_file).pack(side=tk.RIGHT, padx=2)
        ttk.Button(log_ctrl_bar, text="📌 生成桌面快捷方式", command=self.create_desktop_shortcut).pack(side=tk.RIGHT, padx=4)

        self.txt_log = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=8, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # 1. TAB: Engine
    def build_tab_engine(self):
        engine_box = ttk.Frame(self.tab_engine)
        engine_box.pack(fill=tk.X, pady=4)

        ttk.Label(engine_box, text="AI 响应引擎:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        self.var_engine = tk.StringVar(value="openai_api")

        rb2 = ttk.Radiobutton(engine_box, text="🌐 大模型通用 API (DeepSeek / OpenAI / Kimi / Ollama 等兼容接口)", variable=self.var_engine, value="openai_api", command=self.on_engine_change)
        rb2.pack(side=tk.LEFT)

        # OpenAI Group
        self.grp_openai = ttk.LabelFrame(self.tab_engine, text=" 🌐 大模型 API 配置 ", padding=10)
        self.grp_openai.pack(fill=tk.X, pady=8)

        # Base URL
        row_base = ttk.Frame(self.grp_openai)
        row_base.pack(fill=tk.X, pady=4)
        ttk.Label(row_base, text="API Base URL:", width=15).pack(side=tk.LEFT)
        self.ent_api_base = ttk.Entry(row_base)
        self.ent_api_base.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(row_base, text="(例如: https://api.deepseek.com/v1 或 http://127.0.0.1:11434/v1)", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.RIGHT)

        # API Key
        row_key = ttk.Frame(self.grp_openai)
        row_key.pack(fill=tk.X, pady=4)
        ttk.Label(row_key, text="API Key:", width=15).pack(side=tk.LEFT)
        self.ent_api_key = ttk.Entry(row_key, show="*")
        self.ent_api_key.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_show_key = ttk.Button(row_key, text="显示", width=6, command=self.toggle_api_key_visibility)
        self.btn_show_key.pack(side=tk.RIGHT)

        # Model
        row_model = ttk.Frame(self.grp_openai)
        row_model.pack(fill=tk.X, pady=4)
        ttk.Label(row_model, text="模型 ID (Model ID):", width=16).pack(side=tk.LEFT)
        self.ent_model = ttk.Entry(row_model)
        self.ent_model.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(row_model, text="(例如: deepseek-chat, gpt-4o-mini, qwen2.5:7b)", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.RIGHT)

        # Parameters Group
        grp_params = ttk.LabelFrame(self.tab_engine, text=" ⚡ 回复格式与行为参数 ", padding=10)
        grp_params.pack(fill=tk.X, pady=8)

        row_p1 = ttk.Frame(grp_params)
        row_p1.pack(fill=tk.X, pady=4)
        self.var_prefix = tk.BooleanVar(value=False)
        self.chk_prefix = ttk.Checkbutton(row_p1, text="添加前缀标明 AI 回复:", variable=self.var_prefix)
        self.chk_prefix.pack(side=tk.LEFT, padx=(0, 6))
        self.ent_prefix = ttk.Entry(row_p1, width=20)
        self.ent_prefix.pack(side=tk.LEFT, padx=(0, 20))

        row_p2 = ttk.Frame(grp_params)
        row_p2.pack(fill=tk.X, pady=4)
        ttk.Label(row_p2, text="拟人回复延迟:").pack(side=tk.LEFT, padx=(0, 6))
        self.spin_delay = ttk.Spinbox(row_p2, from_=0.0, to=10.0, increment=0.5, width=6)
        self.spin_delay.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(row_p2, text="秒 (模拟真实打字思考时间，防高频回复)").pack(side=tk.LEFT)

        # Save Button
        btn_save_frame = ttk.Frame(self.tab_engine)
        btn_save_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_save_frame, text="💾 保存所有配置并生效", command=self.save_ui_to_config).pack(side=tk.RIGHT)

    def toggle_api_key_visibility(self):
        if self.ent_api_key.cget("show") == "*":
            self.ent_api_key.configure(show="")
            self.btn_show_key.configure(text="隐藏")
        else:
            self.ent_api_key.configure(show="*")
            self.btn_show_key.configure(text="显示")

    def on_engine_change(self):
        pass

    # 2. TAB: Prompt
    def build_tab_prompt(self):
        top_p = ttk.Frame(self.tab_prompt)
        top_p.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(top_p, text="🎭 预设人设风格:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.cbo_presets = ttk.Combobox(top_p, values=list(DEFAULT_PROMPTS.keys()), state="readonly", width=24)
        self.cbo_presets.current(0)
        self.cbo_presets.pack(side=tk.LEFT, padx=(0, 10))
        self.cbo_presets.bind("<<ComboboxSelected>>", self.on_preset_change)

        ttk.Label(top_p, text="💡 提示: 选择上方预设后，可直接在下方编辑框进行深度定制微调。", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.LEFT)

        self.txt_prompt = scrolledtext.ScrolledText(self.tab_prompt, wrap=tk.WORD, height=14, font=("微软雅黑", 9))
        self.txt_prompt.pack(fill=tk.BOTH, expand=True, pady=4)
        self.txt_prompt.bind("<KeyRelease>", self.on_prompt_text_change)

        btn_p = ttk.Frame(self.tab_prompt)
        btn_p.pack(fill=tk.X, pady=8)
        ttk.Button(btn_p, text="💾 保存人设 Prompt", command=self.save_ui_to_config).pack(side=tk.RIGHT)

    # 3. TAB: Contacts
    def build_tab_contacts(self):
        ttk.Label(
            self.tab_contacts,
            text="📇 备注 ↔ 微信号 映射表：填了微信号就优先用它去搜索找人；搜不到会自动降级用备注重搜。",
            font=("微软雅黑", 9, "bold"),
        ).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(
            self.tab_contacts,
            text="· 备注 = 微信里显示的名字（有备注就填备注，没有就填昵称）\n"
                 "· 微信号只有「你自己改过的」才能搜到；微信官方下发的 wxid_ 开头的搜不到，会自动降级用备注\n"
                 "· 这里的备注会自动并入白名单 —— 也就是说，填进来的人才会被自动回复",
            foreground="#666666", font=("微软雅黑", 8), justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        body = ttk.Frame(self.tab_contacts)
        body.pack(fill=tk.BOTH, expand=True)

        # 先 pack 固定宽度的右侧面板，再 pack 可伸缩的左侧列表，否则 expand 会把右侧挤出去
        right = ttk.LabelFrame(body, text=" 新增 / 编辑 ", padding=8)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        left = ttk.LabelFrame(body, text=" 通讯录清单 ", padding=8)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        self.tree_contacts = ttk.Treeview(left, columns=("remark", "wxid"), show="headings", height=10)
        self.tree_contacts.heading("remark", text="备注（微信显示名）")
        self.tree_contacts.heading("wxid", text="微信号 / 自定义号（可留空）")
        self.tree_contacts.column("remark", width=200, anchor=tk.W)
        self.tree_contacts.column("wxid", width=240, anchor=tk.W)
        self.tree_contacts.pack(fill=tk.BOTH, expand=True)
        self.tree_contacts.bind("<<TreeviewSelect>>", self.on_contact_select)

        row1 = ttk.Frame(right)
        row1.pack(fill=tk.X, pady=3)
        ttk.Label(row1, text="备注:", width=8).pack(side=tk.LEFT)
        self.ent_ct_remark = ttk.Entry(row1, width=24)
        self.ent_ct_remark.pack(side=tk.LEFT)

        row2 = ttk.Frame(right)
        row2.pack(fill=tk.X, pady=3)
        ttk.Label(row2, text="微信号:", width=8).pack(side=tk.LEFT)
        self.ent_ct_wxid = ttk.Entry(row2, width=24)
        self.ent_ct_wxid.pack(side=tk.LEFT)

        btns = ttk.Frame(right)
        btns.pack(fill=tk.X, pady=(10, 3))
        ttk.Button(btns, text="➕ 添加 / 更新", command=self.add_contact).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="🗑️ 删除选中", command=self.del_contact).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="📥 从白名单导入", command=self.import_contacts_from_whitelist).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="💾 保存通讯录", command=self.save_ui_to_config).pack(fill=tk.X, pady=(12, 2))

    # 4. TAB: Whitelist
    def build_tab_whitelist(self):
        top_w = ttk.Frame(self.tab_whitelist)
        top_w.pack(fill=tk.X, pady=(0, 6))

        self.var_wl_mode = tk.BooleanVar(value=True)
        self.chk_wl = ttk.Checkbutton(top_w, text="🔒 开启白名单模式（开启后【仅回复】白名单中的指定好友，未在名单者不回复）", variable=self.var_wl_mode)
        self.chk_wl.pack(anchor=tk.W)
        ttk.Label(top_w, text="⚠️ 关闭白名单模式后将回复所有单人好友（群聊/黑名单除外，群聊识别为启发式无法100%拦截），建议仅测试时使用", foreground="#c0392b", font=("微软雅黑", 8), wraplength=900, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        mid_w = ttk.Frame(self.tab_whitelist)
        mid_w.pack(fill=tk.BOTH, expand=True, pady=4)

        # Left: Whitelist
        left_w = ttk.LabelFrame(mid_w, text=" 🟢 允许自动回复的好友白名单 ", padding=8)
        left_w.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        self.lbl_wl_derived = ttk.Label(
            left_w, text="", foreground="#1a73e8", font=("微软雅黑", 8),
            wraplength=560, justify=tk.LEFT)
        self.lbl_wl_derived.pack(anchor=tk.W, pady=(0, 4))

        self.listbox_wl = tk.Listbox(left_w, selectmode=tk.SINGLE, font=("微软雅黑", 9))
        self.listbox_wl.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        wl_ctrl = ttk.Frame(left_w)
        wl_ctrl.pack(fill=tk.X)
        self.ent_add_wl = ttk.Entry(wl_ctrl)
        self.ent_add_wl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(wl_ctrl, text="添加好友", command=self.add_whitelist).pack(side=tk.LEFT, padx=2)
        ttk.Button(wl_ctrl, text="删除选中", command=self.del_whitelist).pack(side=tk.LEFT, padx=2)

        # Right: Blacklist
        right_w = ttk.LabelFrame(mid_w, text=" 🚫 严格屏蔽黑名单 (群聊/服务号/指定联系人) ", padding=8)
        right_w.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(6, 0))

        self.listbox_bl = tk.Listbox(right_w, selectmode=tk.SINGLE, font=("微软雅黑", 9))
        self.listbox_bl.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        bl_ctrl = ttk.Frame(right_w)
        bl_ctrl.pack(fill=tk.X)
        self.ent_add_bl = ttk.Entry(bl_ctrl)
        self.ent_add_bl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(bl_ctrl, text="添加屏蔽", command=self.add_blacklist).pack(side=tk.LEFT, padx=2)
        ttk.Button(bl_ctrl, text="删除选中", command=self.del_blacklist).pack(side=tk.LEFT, padx=2)

    # 5. TAB: Agent brain & proactive send
    def build_tab_agent(self):
        agent_box = ttk.LabelFrame(self.tab_agent, text=" 🤖 DSH Agent 大脑（由 Agent 决定回不回、回什么、什么时候回） ", padding=10)
        agent_box.pack(fill=tk.X, pady=(0, 8))

        row_a0 = ttk.Frame(agent_box)
        row_a0.pack(fill=tk.X, pady=3)
        self.var_agent_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_a0, text="启用 Agent 大脑（关闭则退回「AI 引擎设置」里的大模型直连）",
                        variable=self.var_agent_enabled).pack(side=tk.LEFT)

        row_a1 = ttk.Frame(agent_box)
        row_a1.pack(fill=tk.X, pady=3)
        ttk.Label(row_a1, text="A2A 地址:", width=12).pack(side=tk.LEFT)
        self.ent_agent_url = ttk.Entry(row_a1)
        self.ent_agent_url.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row_a1, text="🔌 测试连接", command=self.test_agent_connection).pack(side=tk.RIGHT)

        row_a2 = ttk.Frame(agent_box)
        row_a2.pack(fill=tk.X, pady=3)
        ttk.Label(row_a2, text="contextId 前缀:", width=12).pack(side=tk.LEFT)
        self.ent_agent_prefix = ttk.Entry(row_a2, width=8)
        self.ent_agent_prefix.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row_a2, text="超时(秒):").pack(side=tk.LEFT, padx=(0, 4))
        self.spin_agent_timeout = ttk.Spinbox(row_a2, from_=10, to=600, increment=10, width=6)
        self.spin_agent_timeout.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row_a2, text="最长延迟发送(秒):").pack(side=tk.LEFT, padx=(0, 4))
        self.spin_agent_defer = ttk.Spinbox(row_a2, from_=60, to=86400, increment=60, width=8)
        self.spin_agent_defer.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row_a2, text="每好友独立会话（中文名自动哈希）",
                  foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.LEFT)

        ttk.Label(agent_box, text="Agent 失联时的兜底回复（每行一条，随机发）：",
                  font=("微软雅黑", 8)).pack(anchor=tk.W, pady=(8, 0))
        self.txt_agent_fallback = tk.Text(agent_box, height=4, font=("微软雅黑", 9), wrap=tk.WORD)
        self.txt_agent_fallback.pack(fill=tk.X, pady=(2, 0))

        pro_box = ttk.LabelFrame(self.tab_agent, text=" 📤 主动发送接口（只监听本机，外部脚本 / Agent 主动发消息用） ", padding=10)
        pro_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        row_p0 = ttk.Frame(pro_box)
        row_p0.pack(fill=tk.X, pady=3)
        self.var_pro_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_p0, text="启用主动发送接口", variable=self.var_pro_enabled).pack(side=tk.LEFT, padx=(0, 18))
        self.var_pro_wl_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_p0, text="仅限白名单 / 通讯录", variable=self.var_pro_wl_only).pack(side=tk.LEFT, padx=(0, 18))
        self.var_pro_confirm = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_p0, text="需要人工确认后才发", variable=self.var_pro_confirm).pack(side=tk.LEFT, padx=(0, 18))
        self.var_pro_search = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_p0, text="找不到时用搜索找人", variable=self.var_pro_search).pack(side=tk.LEFT)

        row_p1 = ttk.Frame(pro_box)
        row_p1.pack(fill=tk.X, pady=3)
        ttk.Label(row_p1, text="监听地址:", width=10).pack(side=tk.LEFT)
        self.ent_pro_host = ttk.Entry(row_p1, width=14)
        self.ent_pro_host.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row_p1, text="端口:").pack(side=tk.LEFT, padx=(0, 4))
        self.spin_pro_port = ttk.Spinbox(row_p1, from_=1024, to=65535, increment=1, width=8)
        self.spin_pro_port.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row_p1, text="每小时上限:").pack(side=tk.LEFT, padx=(0, 4))
        self.spin_pro_hour = ttk.Spinbox(row_p1, from_=1, to=200, increment=1, width=6)
        self.spin_pro_hour.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row_p1, text="每天上限:").pack(side=tk.LEFT, padx=(0, 4))
        self.spin_pro_day = ttk.Spinbox(row_p1, from_=1, to=2000, increment=1, width=6)
        self.spin_pro_day.pack(side=tk.LEFT)

        row_p2 = ttk.Frame(pro_box)
        row_p2.pack(fill=tk.X, pady=(12, 3))
        ttk.Label(row_p2, text="🧪 立即测试:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(row_p2, text="发给:").pack(side=tk.LEFT)
        self.ent_pro_test_target = ttk.Entry(row_p2, width=16)
        self.ent_pro_test_target.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row_p2, text="内容:").pack(side=tk.LEFT)
        self.ent_pro_test_text = ttk.Entry(row_p2)
        self.ent_pro_test_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        ttk.Button(row_p2, text="📨 发送", command=self.send_proactive_test).pack(side=tk.RIGHT)

        self.lbl_pro_status = ttk.Label(pro_box, text="接口状态：未检测", foreground="#666666", font=("微软雅黑", 8))
        self.lbl_pro_status.pack(anchor=tk.W, pady=(8, 0))

        btn = ttk.Frame(self.tab_agent)
        btn.pack(fill=tk.X)
        ttk.Button(btn, text="💾 保存所有配置并生效", command=self.save_ui_to_config).pack(side=tk.RIGHT)
        ttk.Button(btn, text="🔄 刷新接口状态", command=self.refresh_proactive_status).pack(side=tk.RIGHT, padx=6)

    # 6. TAB: Test Sandbox
    def build_tab_test(self):
        ttk.Label(self.tab_test, text="🧪 模拟微信好友提问，测试当前 AI 引擎与 Prompt 的真实回复质量:", font=("微软雅黑", 9, "bold")).pack(anchor=tk.W, pady=(0, 6))

        in_frame = ttk.Frame(self.tab_test)
        in_frame.pack(fill=tk.X, pady=4)

        ttk.Label(in_frame, text="好友昵称:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_test_sender = ttk.Entry(in_frame, width=14)
        self.ent_test_sender.insert(0, "测试好友")
        self.ent_test_sender.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(in_frame, text="发送内容:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_test_msg = ttk.Entry(in_frame)
        self.ent_test_msg.insert(0, "在干嘛呢？晚上一起吃个饭不？")
        self.ent_test_msg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_run_test = ttk.Button(in_frame, text="🚀 触发 AI 拟真回复测试", command=self.run_sandbox_test)
        self.btn_run_test.pack(side=tk.RIGHT)

        ttk.Label(self.tab_test, text="AI 回复输出 (与微信聊天完全一致):", font=("微软雅黑", 8)).pack(anchor=tk.W, pady=(8, 2))
        self.txt_test_out = scrolledtext.ScrolledText(self.tab_test, wrap=tk.WORD, height=10, font=("微软雅黑", 9))
        self.txt_test_out.pack(fill=tk.BOTH, expand=True, pady=4)

    # Load / Save Logic
    def load_settings_to_ui(self):
        c = self.config_data
        oa = c.get("openai_api", {})
        self.ent_api_base.delete(0, tk.END)
        self.ent_api_base.insert(0, oa.get("api_base") or "https://api.xiaomimimo.com/v1")
        self.ent_api_key.delete(0, tk.END)
        self.ent_api_key.insert(0, oa.get("api_key") or "")
        self.ent_model.delete(0, tk.END)
        self.ent_model.insert(0, oa.get("model") or "mimo-v2.6-flash")

        self.var_prefix.set(c.get("include_prefix", False))
        self.ent_prefix.delete(0, tk.END)
        self.ent_prefix.insert(0, c.get("reply_prefix") or "[AI自动回复] ")
        self.spin_delay.set(c.get("reply_delay_seconds") or 0.0)

        sys_prompt = (c.get("system_prompt") or DEFAULT_PROMPTS["🌟 真人日常 (默认)"]).strip()
        self.txt_prompt.delete("1.0", tk.END)
        self.txt_prompt.insert(tk.END, sys_prompt)

        # 准确还原预设状态与下拉框选中项
        saved_preset = c.get("prompt_preset", "").strip()
        if saved_preset and saved_preset in DEFAULT_PROMPTS:
            self.cbo_presets.set(saved_preset)
        else:
            # 若未显式记录 preset，根据 system_prompt 内容智能反查
            matched = None
            for k, v in DEFAULT_PROMPTS.items():
                if k != "✏️ 自由定制" and v.strip() == sys_prompt:
                    matched = k
                    break
            if matched:
                self.cbo_presets.set(matched)
            else:
                self.cbo_presets.set("✏️ 自由定制")
        
        # 确保自由定制有备份值
        if self.cbo_presets.get() == "✏️ 自由定制" and sys_prompt:
            self.config_data["custom_prompt"] = sys_prompt
        elif "custom_prompt" not in self.config_data:
            self.config_data["custom_prompt"] = c.get("custom_prompt", DEFAULT_PROMPTS["✏️ 自由定制"])

        self.var_wl_mode.set(c.get("whitelist_mode", True))
        self.listbox_wl.delete(0, tk.END)
        for w in c.get("whitelist", []):
            self.listbox_wl.insert(tk.END, w)

        self.listbox_bl.delete(0, tk.END)
        for b in c.get("blacklist", []):
            self.listbox_bl.insert(tk.END, b)

        # 通讯录
        self._refresh_contacts_tree()

        # Agent 大脑
        ag = c.get("agent") or {}
        self.var_agent_enabled.set(bool(ag.get("enabled", True)))
        self.ent_agent_url.delete(0, tk.END)
        self.ent_agent_url.insert(0, ag.get("url") or "http://127.0.0.1:44708/a2a")
        self.ent_agent_prefix.delete(0, tk.END)
        self.ent_agent_prefix.insert(0, ag.get("context_prefix") or "wx-")
        self.spin_agent_timeout.set(ag.get("timeout_seconds") or 180)
        self.spin_agent_defer.set(ag.get("max_defer_seconds") or 900)
        self.txt_agent_fallback.delete("1.0", tk.END)
        self.txt_agent_fallback.insert(
            tk.END,
            "\n".join(ag.get("fallback_replies") or ["嘿嘿，我这会儿有点懵，等下再聊~"]),
        )

        # 主动发送
        pc = c.get("proactive_send") or {}
        self.var_pro_enabled.set(bool(pc.get("enabled", False)))
        self.var_pro_wl_only.set(bool(pc.get("whitelist_only", True)))
        self.var_pro_confirm.set(bool(pc.get("require_confirm", False)))
        self.var_pro_search.set(bool(pc.get("allow_search", True)))
        self.ent_pro_host.delete(0, tk.END)
        self.ent_pro_host.insert(0, pc.get("host") or "127.0.0.1")
        self.spin_pro_port.set(pc.get("port") or 8765)
        self.spin_pro_hour.set(pc.get("max_per_hour") or 3)
        self.spin_pro_day.set(pc.get("max_per_day") or 20)
        self._safe_after(800, self.refresh_proactive_status)

    def save_ui_to_config(self, show_msg=True):
        self.config_data["ai_engine"] = self.var_engine.get()
        self.config_data["openai_api"] = {
            "api_key": self.ent_api_key.get().strip(),
            "api_base": self.ent_api_base.get().strip(),
            "model": self.ent_model.get().strip(),
            "temperature": 0.7
        }
        self.config_data["include_prefix"] = self.var_prefix.get()
        self.config_data["reply_prefix"] = self.ent_prefix.get()
        try:
            self.config_data["reply_delay_seconds"] = float(self.spin_delay.get())
        except Exception:
            self.config_data["reply_delay_seconds"] = 0.0

        cur_prompt = self.txt_prompt.get("1.0", tk.END).strip()
        cur_preset = self.cbo_presets.get()
        self.config_data["system_prompt"] = cur_prompt
        self.config_data["prompt_preset"] = cur_preset
        if cur_preset == "✏️ 自由定制" or cur_prompt not in DEFAULT_PROMPTS.values():
            self.config_data["custom_prompt"] = cur_prompt

        self.config_data["whitelist_mode"] = self.var_wl_mode.get()
        self.config_data["whitelist"] = list(self.listbox_wl.get(0, tk.END))
        self.config_data["blacklist"] = list(self.listbox_bl.get(0, tk.END))
        self.config_data["contacts"] = self._contacts_from_tree_source()

        # Agent 大脑：保留配置里其它未在界面暴露的键（card_url / retries / context_map_file…）
        agent_merged = dict(self.config_data.get("agent") or {})
        agent_merged.update(self._agent_cfg_dict())
        self.config_data["agent"] = agent_merged

        proactive_merged = dict(self.config_data.get("proactive_send") or {})
        proactive_merged.update(self._proactive_cfg_dict())
        self.config_data["proactive_send"] = proactive_merged

        if self.save_config():
            if show_msg:
                messagebox.showinfo("保存成功", "✅ 配置已成功保存！后台 AI 守护进程将自动应用最新设置。")

    def on_prompt_text_change(self, event=None):
        if event and event.keysym in ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Caps_Lock", "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"):
            return
        cur_text = self.txt_prompt.get("1.0", tk.END).strip()
        self.config_data["custom_prompt"] = cur_text
        if self.cbo_presets.get() != "✏️ 自由定制":
            cur_preset = self.cbo_presets.get()
            if cur_preset in DEFAULT_PROMPTS and cur_text != DEFAULT_PROMPTS[cur_preset].strip():
                self.cbo_presets.set("✏️ 自由定制")

    def on_preset_change(self, event=None):
        name = self.cbo_presets.get()
        cur_text = self.txt_prompt.get("1.0", tk.END).strip()
        
        # 若当前正在编辑自定义内容，先自动备份，防止被其它标准预设覆盖冲掉
        if cur_text and cur_text not in DEFAULT_PROMPTS.values():
            self.config_data["custom_prompt"] = cur_text

        if name == "✏️ 自由定制":
            # 优先恢复用户之前的自定义 Prompt，绝不暴力覆盖为固定模板
            custom_text = self.config_data.get("custom_prompt", "").strip()
            if not custom_text:
                custom_text = cur_text if (cur_text and cur_text not in DEFAULT_PROMPTS.values()) else DEFAULT_PROMPTS["✏️ 自由定制"]
            self.txt_prompt.delete("1.0", tk.END)
            self.txt_prompt.insert(tk.END, custom_text)
            self.config_data["custom_prompt"] = custom_text
        elif name in DEFAULT_PROMPTS:
            self.txt_prompt.delete("1.0", tk.END)
            self.txt_prompt.insert(tk.END, DEFAULT_PROMPTS[name])

    def add_whitelist(self):
        val = self.ent_add_wl.get().strip()
        if val:
            if val not in self.config_data["whitelist"]:
                self.config_data["whitelist"].append(val)
                self.listbox_wl.insert(tk.END, val)
                self.save_config()
            self.ent_add_wl.delete(0, tk.END)

    def del_whitelist(self):
        sel = self.listbox_wl.curselection()
        if sel:
            idx = sel[0]
            val = self.listbox_wl.get(idx)
            if val in self.config_data["whitelist"]:
                self.config_data["whitelist"].remove(val)
                self.save_config()
            self.listbox_wl.delete(idx)

    def add_blacklist(self):
        val = self.ent_add_bl.get().strip()
        if val:
            if val not in self.config_data["blacklist"]:
                self.config_data["blacklist"].append(val)
                self.listbox_bl.insert(tk.END, val)
                self.save_config()
            self.ent_add_bl.delete(0, tk.END)

    def del_blacklist(self):
        sel = self.listbox_bl.curselection()
        if sel:
            idx = sel[0]
            val = self.listbox_bl.get(idx)
            if val in self.config_data["blacklist"]:
                self.config_data["blacklist"].remove(val)
                self.save_config()
            self.listbox_bl.delete(idx)

    # ------------------------------------------------------------------
    # 通讯录映射
    # ------------------------------------------------------------------
    def _refresh_contacts_tree(self):
        if not hasattr(self, "tree_contacts"):
            return
        for item in self.tree_contacts.get_children():
            self.tree_contacts.delete(item)
        rows = self._contacts_from_tree_source()
        for c in rows:
            self.tree_contacts.insert("", tk.END, values=(c.get("remark", ""), c.get("wxid", "")))
        # 白名单页提示：通讯录里的人会自动并入白名单
        if hasattr(self, "lbl_wl_derived"):
            remarks = [c.get("remark", "") for c in rows if c.get("remark")]
            if remarks:
                shown = "、".join(remarks[:8])
                more = f" 等 {len(remarks)} 人" if len(remarks) > 8 else ""
                self.lbl_wl_derived.configure(
                    text=f"📇 通讯录映射已自动并入白名单：{shown}{more}"
                         f"（这里只列手填的；通讯录请在「📇 通讯录映射」页管理）")
            else:
                self.lbl_wl_derived.configure(text="📇 通讯录映射里的人会自动并入白名单（当前 0 人）")

    def _contacts_from_tree_source(self):
        out = []
        for c in self.config_data.get("contacts") or []:
            if isinstance(c, str):
                c = {"remark": c}
            if not isinstance(c, dict):
                continue
            remark = str(c.get("remark") or c.get("name") or c.get("wechat_id") or c.get("alias") or "").strip()
            wxid = str(c.get("wxid") or c.get("wechat_id") or c.get("alias") or "").strip()
            if not remark and wxid:
                remark = wxid
            if remark:
                out.append({"remark": remark, "wxid": wxid})
        return out

    def on_contact_select(self, event=None):
        sel = self.tree_contacts.selection()
        if not sel:
            return
        vals = self.tree_contacts.item(sel[0], "values")
        self.ent_ct_remark.delete(0, tk.END)
        self.ent_ct_remark.insert(0, vals[0] if vals else "")
        self.ent_ct_wxid.delete(0, tk.END)
        self.ent_ct_wxid.insert(0, vals[1] if len(vals) > 1 else "")

    def add_contact(self):
        remark = self.ent_ct_remark.get().strip()
        wxid = self.ent_ct_wxid.get().strip()
        if not remark:
            messagebox.showwarning("提示", "备注不能为空（就是微信里显示的那个名字）")
            return
        contacts = self._contacts_from_tree_source()
        hit = None
        for c in contacts:
            if c["remark"] == remark or (wxid and c.get("wxid") == wxid):
                hit = c
                break
        if hit:
            hit["remark"] = remark
            hit["wxid"] = wxid
        else:
            contacts.append({"remark": remark, "wxid": wxid})
        self.config_data["contacts"] = contacts
        self._refresh_contacts_tree()
        self.save_config()

    def del_contact(self):
        sel = self.tree_contacts.selection()
        if not sel:
            return
        vals = self.tree_contacts.item(sel[0], "values")
        remark = vals[0] if vals else ""
        self.config_data["contacts"] = [
            c for c in self._contacts_from_tree_source() if c["remark"] != remark
        ]
        self._refresh_contacts_tree()
        self.save_config()

    def import_contacts_from_whitelist(self):
        contacts = self._contacts_from_tree_source()
        have = {c["remark"] for c in contacts}
        added = 0
        for name in self.config_data.get("whitelist") or []:
            name = str(name).strip()
            if name and name not in have:
                contacts.append({"remark": name, "wxid": ""})
                have.add(name)
                added += 1
        self.config_data["contacts"] = contacts
        self._refresh_contacts_tree()
        self.save_config()
        messagebox.showinfo("导入完成", f"从白名单新增 {added} 条到通讯录。")

    # ------------------------------------------------------------------
    # Agent 大脑 / 主动发送
    # ------------------------------------------------------------------
    def _agent_cfg_dict(self):
        fallbacks = [
            ln.strip()
            for ln in self.txt_agent_fallback.get("1.0", tk.END).splitlines()
            if ln.strip()
        ]
        try:
            timeout_v = int(float(self.spin_agent_timeout.get()))
        except Exception:
            timeout_v = 180
        try:
            defer_v = int(float(self.spin_agent_defer.get()))
        except Exception:
            defer_v = 900
        return {
            "enabled": bool(self.var_agent_enabled.get()),
            "url": self.ent_agent_url.get().strip() or "http://127.0.0.1:44708/a2a",
            "context_prefix": self.ent_agent_prefix.get().strip() or "wx-",
            "timeout_seconds": timeout_v,
            "max_defer_seconds": defer_v,
            "fallback_replies": fallbacks or ["嘿嘿，我这会儿有点懵，等下再聊~"],
        }

    def _proactive_cfg_dict(self):
        def _int(spin, default):
            try:
                return int(float(spin.get()))
            except Exception:
                return default

        return {
            "enabled": bool(self.var_pro_enabled.get()),
            "host": self.ent_pro_host.get().strip() or "127.0.0.1",
            "port": _int(self.spin_pro_port, 8765),
            "whitelist_only": bool(self.var_pro_wl_only.get()),
            "max_per_hour": _int(self.spin_pro_hour, 3),
            "max_per_day": _int(self.spin_pro_day, 20),
            "require_confirm": bool(self.var_pro_confirm.get()),
            "allow_search": bool(self.var_pro_search.get()),
        }

    def test_agent_connection(self):
        cfg = dict(self.config_data)
        cfg["agent"] = self._agent_cfg_dict()

        def _work():
            try:
                if WORKSPACE_DIR not in sys.path:
                    sys.path.insert(0, WORKSPACE_DIR)
                from agent_client import AgentClient
                cli = AgentClient(cfg)
                ok = cli.is_available(timeout=3.0)
                if ok:
                    msg = f"✅ Agent 在线\n{cli.url}"
                else:
                    msg = f"❌ 连不上 Agent\n{cli.url}\n\n请确认 DSH 的 A2A 服务已开启。"
            except Exception as exc:
                msg = f"❌ 测试失败：{exc}"
            self._ui_call(lambda: messagebox.showinfo("Agent 连接测试", msg))

        threading.Thread(target=_work, daemon=True).start()

    def _api_base(self):
        pc = self._proactive_cfg_dict()
        return f"http://{pc['host']}:{pc['port']}"

    def refresh_proactive_status(self):
        base = self._api_base()

        def _work():
            try:
                with urllib.request.urlopen(base + "/status", timeout=2) as r:
                    data = json.loads(r.read().decode("utf-8"))
                txt = (f"接口状态：✅ 在线  {base}   "
                       f"本小时 {data.get('sent_last_hour')}/{data.get('max_per_hour')}   "
                       f"今日 {data.get('sent_today')}/{data.get('max_per_day')}   "
                       f"待确认 {data.get('pending_confirm')}")
                color = "#1e8e3e"
            except Exception:
                txt = f"接口状态：⚪ 未启动（{base}）。勾选「启用主动发送接口」并点保存，再重启服务即可。"
                color = "#666666"
            self._ui_call(lambda: self.lbl_pro_status.configure(text=txt, foreground=color))

        threading.Thread(target=_work, daemon=True).start()

    def send_proactive_test(self):
        target = self.ent_pro_test_target.get().strip()
        text = self.ent_pro_test_text.get().strip()
        if not target or not text:
            messagebox.showwarning("提示", "请填写「发给」和「内容」")
            return
        base = self._api_base()
        url = base + "/send?" + urllib.parse.urlencode({"target": target, "text": text})

        def _work():
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if data.get("ok"):
                    msg = "✅ 已发送"
                else:
                    msg = f"❌ 被拒绝：{data.get('code')}\n{data.get('detail') or ''}"
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read().decode("utf-8"))
                    msg = f"❌ 被拒绝：{data.get('code')}\n{data.get('detail') or ''}"
                except Exception:
                    msg = f"❌ HTTP {e.code}"
            except Exception as exc:
                msg = f"❌ 请求失败：{exc}\n\n接口可能没启动，先保存配置并重启服务。"
            self._ui_call(lambda: messagebox.showinfo("主动发送测试", msg))
            self._safe_after(200, self.refresh_proactive_status)

        threading.Thread(target=_work, daemon=True).start()

    def run_sandbox_test(self):
        sender = self.ent_test_sender.get().strip() or "测试好友"
        msg = self.ent_test_msg.get().strip()
        if not msg:
            messagebox.showwarning("提示", "请输入发送内容！")
            return

        self.save_ui_to_config(show_msg=False)
        self.txt_test_out.delete("1.0", tk.END)
        self.txt_test_out.insert(tk.END, "🤖 正在调用大模型生成回复...\n")
        self.update()

        t0 = time.time()
        oa = self.config_data.get("openai_api") or {}
        api_key = (oa.get("api_key") or "").strip()
        api_base = (oa.get("api_base") or "https://api.xiaomimimo.com/v1").rstrip("/")
        model = oa.get("model") or "mimo-v2.6-flash"
        
        if not api_key:
            self.txt_test_out.delete("1.0", tk.END)
            self.txt_test_out.insert(tk.END, "❌ 请先在【AI 引擎设置】中填入有效的 API Key！")
            return
            
        url = api_base if api_base.endswith("/chat/completions") else f"{api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        base_prompt = self.config_data.get("system_prompt") or DEFAULT_PROMPTS.get("🌟 真人日常 (默认)", "")
        now_time = time.strftime("%H:%M")
        prompt = (
            f"{base_prompt}\n"
            f"【当前互动好友】：好友【{sender}】，当前时间{now_time}。\n"
            f"【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": msg}
            ],
            "temperature": 0.7
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                data = res.json()
                raw_reply = data["choices"][0]["message"]["content"]
                reply = re.sub(r'<think>.*?</think>', '', raw_reply, flags=re.DOTALL)
                reply = re.sub(r'^[（(][^）)]*[）)]\s*', '', reply)
                reply = re.sub(r'\s*[（(][^）)]*[）)]$', '', reply)
                reply = reply.strip().strip('"').strip('“').strip('”')
                dur = round(time.time() - t0, 2)
                self.txt_test_out.delete("1.0", tk.END)
                self.txt_test_out.insert(tk.END, f"{reply}\n\n[⏱️ 耗时: {dur} 秒]")
            else:
                self.txt_test_out.delete("1.0", tk.END)
                self.txt_test_out.insert(tk.END, f"❌ API 请求失败 (状态码 {res.status_code}):\n{res.text}")
        except Exception as e:
            self.txt_test_out.delete("1.0", tk.END)
            self.txt_test_out.insert(tk.END, f"❌ 请求大模型出错: {e}")

    def _iter_candidate_procs(self):
        """只枚举「可能是守护进程」的少量进程。

        两级提速：
        1) 旧代码对全系统逐个 `process_iter(attrs=['cmdline'])` 再 `proc.cmdline()` 取两遍
           命令行 —— 实测单次 8.4s；改 `process_iter([pid,name])` 后 3.9s。
        2) 现在用 `fast_process_names()`（EnumProcesses + QueryFullProcessImageNameW）
           拿名字只要 0.04s，再按进程名粗筛（python / wechat_ai_bot / 微信ai），
           通常只剩 1~5 个候选，整体降到 ~0.05s。
        """
        hints = ("python", "wechat_ai_bot", "微信ai")
        names = fast_process_names()
        if not names:
            # 兜底：ctypes 枚举失败时退回 psutil
            names = {}
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    names[p.info["pid"]] = p.info.get("name") or ""
                except Exception:
                    continue
        for pid, name in names.items():
            if not any(h in (name or "").lower() for h in hints):
                continue
            try:
                yield psutil.Process(pid)
            except Exception:
                continue

    def is_target_bot_process(self, proc):
        try:
            cmdline = proc.cmdline() or []
            cmd_str = " ".join(str(c) for c in cmdline).lower()
            name = (proc.name() or '').lower()

            # 1. 严格排除所有形态的 GUI 控制中心 (GUI 绝对豁免，优先判定)
            gui_keywords = [
                "gui", "控制中心", "wechat_bot_gui",
                "微信ai自动回复", "微信ai回复(开发版)", "微信ai回复（开发版）", "微信ai回复-开发版"
            ]
            if any(k in name or k in cmd_str for k in gui_keywords):
                return False

            # 2. 严格排除稳定版与其它无关进程
            if "稳定版" in cmd_str or "wechat_ai_bot_stable" in cmd_str:
                return False

            # 3. 精准命中开发版后台守护核心 (源码模式与打包模式)
            if "wechat_ai_bot_dev" in name or "wechat_ai_bot_dev.py" in cmd_str:
                return True
            if "wechat_ai_bot" in name and "微信ai回复_开发版" in cmd_str:
                return True
        except Exception:
            pass
        return False

    def is_bot_running(self):
        my_pid = os.getpid()
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    if pid != my_pid and psutil.pid_exists(pid):
                        p = psutil.Process(pid)
                        if self.is_target_bot_process(p):
                            return True, pid
            except Exception:
                pass
                
        for proc in self._iter_candidate_procs():
            try:
                if proc.pid == my_pid:
                    continue
                if self.is_target_bot_process(proc):
                    return True, proc.pid
            except Exception:
                pass
        return False, None

    def kill_bot_processes(self):
        my_pid = os.getpid()
        # 1. Kill by PID file (带严格校验)
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    if pid != my_pid and psutil.pid_exists(pid):
                        p = psutil.Process(pid)
                        if self.is_target_bot_process(p):
                            p.kill()
            except Exception:
                pass
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
                
        # 2. Kill only dev bot processes (never touch stable)
        for proc in self._iter_candidate_procs():
            try:
                if proc.pid == my_pid:
                    continue
                if self.is_target_bot_process(proc):
                    proc.kill()
            except Exception:
                pass

    def _set_bot_buttons(self, busy, action="start"):
        """启停进行中时禁用按钮并改文案，避免重复点击 + 让用户看到反馈。"""
        try:
            if busy:
                self.btn_start.configure(state="disabled")
                self.btn_stop.configure(state="disabled")
                if action == "start":
                    self.btn_start.configure(text="⏳ 正在启动…")
                else:
                    self.btn_stop.configure(text="⏳ 正在停止…")
            else:
                self.btn_start.configure(state="normal", text="🚀 启动 AI 自动回复")
                self.btn_stop.configure(state="normal", text="⏹️ 停止守护")
        except Exception:
            pass

    def _run_bot_action(self, action):
        """把「杀进程 / 拉进程」这种阻塞活丢到后台线程，主线程只轮询结果，彻底不卡界面。"""
        if getattr(self, "_bot_action_running", False):
            return
        self._bot_action_running = True
        self._bot_action_result = None
        self._bot_action_kind = action
        self._set_bot_buttons(True, action)
        try:
            self.lbl_bot_status.configure(
                text=("⏳ AI 守护进程: 正在启动..." if action == "start"
                      else "⏳ AI 守护进程: 正在停止..."),
                foreground="#e67e22")
        except Exception:
            pass

        def _work():
            try:
                self.kill_bot_processes()
            except Exception:
                pass
            time.sleep(0.2)
            if action != "start":
                self._bot_action_result = (True, "已停止", "⏹️ 已停止所有微信 AI 自动回复进程。")
                return
            try:
                SW_HIDE = 0
                bot_exe = os.path.join(WORKSPACE_DIR, "wechat_ai_bot_dev.exe")
                if not os.path.exists(bot_exe):
                    bot_exe = os.path.join(WORKSPACE_DIR, "wechat_ai_bot.exe")
                if os.path.exists(bot_exe):
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        0, "open", bot_exe, None, WORKSPACE_DIR, SW_HIDE)
                else:
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        0, "open", sys.executable, f'"{BOT_SCRIPT}"', WORKSPACE_DIR, SW_HIDE)
                time.sleep(0.5)
                if ret > 32:
                    self._bot_action_result = (
                        True, "启动成功",
                        "🚀 微信 AI 自动回复守护进程已在后台成功静默启动（无黑框模式）！")
                else:
                    self._bot_action_result = (
                        False, "启动失败", f"启动守护进程失败，系统返回码: {ret}")
            except Exception as e:
                self._bot_action_result = (False, "启动失败", f"启动守护进程时出错: {e}")

        threading.Thread(target=_work, daemon=True).start()
        self._safe_after(120, self._poll_bot_action)

    def _poll_bot_action(self):
        if self._bot_action_result is None:
            self._safe_after(120, self._poll_bot_action)
            return
        ok, title, msg = self._bot_action_result
        self._bot_action_result = None
        self._bot_action_running = False
        self._set_bot_buttons(False)
        if ok:
            messagebox.showinfo(title, msg)
        else:
            messagebox.showerror(title, msg)

    def start_bot(self):
        if getattr(self, "_bot_action_running", False):
            return
        try:
            self.save_ui_to_config(show_msg=False)
        except Exception:
            pass
        self._run_bot_action("start")

    def toggle_offscreen_wechat(self):
        try:
            hwnd, _ = get_wechat_hwnd()
            if not hwnd:
                messagebox.showwarning("提示", "未检测到微信窗口！请先打开微信。")
                return
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            is_iconic = bool(user32.IsIconic(hwnd))
            
            # 只有当非系统最小化且坐标处于负数离屏区时，才判定为真正的离屏隐藏状态
            is_truly_offscreen = (not is_iconic) and (rect.left < -500 or rect.top < -500)
            
            if is_truly_offscreen:
                # 处于真正的离屏状态 -> 召回并还原到用户此前自定义的坐标与长宽
                saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                x, y, w, h = saved[0], saved[1], saved[2], saved[3]
                
                screen_w = self.winfo_screenwidth()
                screen_h = self.winfo_screenheight()
                if x < 0 or x > screen_w - 200 or y < 0 or y > screen_h - 200:
                    x = max(50, int((screen_w - w) / 2))
                    y = max(50, int((screen_h - h) / 2))
                
                user32.ShowWindow(hwnd, 9) # SW_RESTORE
                user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040 | 0x0010)
                self.btn_hide_wx.configure(text="👻 隐藏微信至离屏")
                messagebox.showinfo("已召回", f"🖥️ 微信窗口已成功还原到您设定的桌面位置！\n坐标: ({x}, {y})，尺寸: {w}x{h}")
            else:
                # 处于桌面或处于系统最小化 -> 执行隐藏！
                # 1. 若当前在桌面，原子捕获并记录当前正常位置尺寸
                if not is_iconic and rect.left >= -500 and rect.top >= -500:
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w >= 400 and h >= 300:
                        self.config_data["saved_window_rect"] = [int(rect.left), int(rect.top), int(w), int(h)]
                        self.save_config()
                elif is_iconic:
                    # 若处于最小化，通过 GetWindowPlacement 提取最小化前的正常尺寸
                    norm_rect = get_wechat_normal_rect(hwnd)
                    if norm_rect:
                        self.config_data["saved_window_rect"] = norm_rect
                        self.save_config()
                
                saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                w, h = saved[2], saved[3]
                
                # 2. 唤醒 DWM 渲染并直接平滑移入离屏物理空间 (-3000, -3000)
                user32.ShowWindow(hwnd, 9) # SW_RESTORE
                user32.SetWindowPos(hwnd, 0, -3000, -3000, w, h, 0x0040 | 0x0010)
                self.btn_hide_wx.configure(text="🖥️ 召唤微信回桌面")
                messagebox.showinfo("已隐藏", "👻 微信已成功记录当前位置大小，并移入离屏隐形空间！\n\n您的桌面已彻底隐形不占空间，AI 守护进程依然在后台全速秒回。\n随时可点击此按钮召回微信。")
        except Exception as e:
            messagebox.showerror("错误", f"切换离屏状态失败: {e}")

    def stop_bot(self):
        if getattr(self, "_bot_action_running", False):
            return
        self._run_bot_action("stop")

    def restart_bot(self):
        self.start_bot()

    def clear_logs(self):
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
            self.log_pos = 0
            self.txt_log.delete("1.0", tk.END)
        except Exception:
            pass

    def open_log_file(self):
        if os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("提示", "暂无日志文件。")

    def create_desktop_shortcut(self):
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            shortcut_path = os.path.join(desktop, "微信AI自动回复_开发版.lnk")
            exe_path = os.path.join(WORKSPACE_DIR, "微信AI自动回复.exe")
            if not os.path.exists(exe_path):
                exe_path = os.path.join(WORKSPACE_DIR, "微信AI回复(开发版).exe")
            if not os.path.exists(exe_path):
                exe_path = os.path.join(WORKSPACE_DIR, "微信AI控制中心.exe")
            
            gui_script = os.path.join(WORKSPACE_DIR, "wechat_bot_gui_dev.py")
            ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_unread_ai_v4.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_v2.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_ai_v3.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_blue.ico")

            if os.path.exists(exe_path):
                target = exe_path
                args = ""
                icon = f"{exe_path},0"
            else:
                py_exe = sys.executable
                pyw_exe = py_exe.lower().replace("python.exe", "pythonw.exe")
                target = pyw_exe if os.path.exists(pyw_exe) else py_exe
                args = f'"{gui_script}"'
                icon = ico_path if os.path.exists(ico_path) else f"{target},0"

            vbs_content = f'Set WshShell = CreateObject("WScript.Shell")\n' \
                          f'Set oShortcut = WshShell.CreateShortcut("{shortcut_path}")\n' \
                          f'oShortcut.TargetPath = "{target}"\n' \
                          f'oShortcut.Arguments = {json.dumps(args)}\n' \
                          f'oShortcut.WorkingDirectory = "{WORKSPACE_DIR}"\n' \
                          f'oShortcut.IconLocation = "{icon}"\n' \
                          f'oShortcut.Description = "微信AI自动回复"\n' \
                          f'oShortcut.Save\n'
            
            tmp_vbs = os.path.join(tempfile.gettempdir(), "_tmp_sc.vbs")
            with open(tmp_vbs, "w", encoding="gbk", errors="ignore") as f:
                f.write(vbs_content)
            os.system(f'cscript //nologo "{tmp_vbs}"')
            if os.path.exists(tmp_vbs):
                try:
                    os.remove(tmp_vbs)
                except Exception:
                    pass
            messagebox.showinfo("创建成功", f"🎉 已成功在桌面创建快捷方式！\n\n快捷方式: {os.path.basename(shortcut_path)}")
        except Exception as e:
            messagebox.showerror("错误", f"创建快捷方式失败: {e}")

    def start_background_monitors(self):
        def monitor_loop():
            while self.is_monitoring:
                # 1. Check WeChat Window (Direct Win32 API)
                try:
                    hwnd, ver = get_wechat_hwnd()
                    if hwnd:
                        wx_text = f"🟢 微信客户端: 正常连接 (WeChat {ver}.x, HWND: {hwnd})"
                        wx_color = "#107c41"
                    else:
                        wx_text = "🔴 微信客户端: 未检测到窗口 (请先登录打开微信)"
                        wx_color = "#d83b01"
                except Exception as e:
                    wx_text = f"⚪ 微信客户端: 检测中..."
                    wx_color = "#333333"

                # 2. Check Bot process
                try:
                    running, pid = self.is_bot_running()
                    if running:
                        bot_text = f"🟢 AI 守护进程: 正在运行 (PID: {pid})"
                        bot_color = "#107c41"
                    else:
                        bot_text = "⚪ AI 守护进程: 未启动"
                        bot_color = "#666666"
                except Exception:
                    bot_text = "⚪ AI 守护进程: 检测中..."
                    bot_color = "#666666"

                # 3. Read incremental logs
                new_logs = ""
                if os.path.exists(LOG_FILE):
                    try:
                        file_size = os.path.getsize(LOG_FILE)
                        if file_size < self.log_pos:
                            self.log_pos = 0
                        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(self.log_pos)
                            new_logs = f.read()
                            self.log_pos = f.tell()
                    except Exception:
                        pass

                if not self.is_monitoring:
                    break
                try:
                    self._ui_call(lambda wt=wx_text, wc=wx_color, bt=bot_text, bc=bot_color, nl=new_logs: self._update_ui_state(wt, wc, bt, bc, nl))
                except Exception:
                    pass
                time.sleep(1.5)

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    def _update_ui_state(self, wx_text, wx_color, bot_text, bot_color, new_logs):
        self.lbl_wx_status.configure(text=wx_text, foreground=wx_color)
        self.lbl_bot_status.configure(text=bot_text, foreground=bot_color)
        if new_logs:
            self.txt_log.insert(tk.END, new_logs)
            self.txt_log.see(tk.END)

    # ---------------- P3: 待确认消息弹窗 ----------------
    def _load_pending_from_disk(self):
        """读 pending_confirm.jsonl，只返回 status=pending 的记录。"""
        items = []
        if not os.path.exists(PENDING_FILE):
            return items
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("status", "pending") == "pending" and rec.get("id"):
                        items.append(rec)
        except Exception:
            pass
        items.sort(key=lambda x: x.get("created_ts") or 0)
        return items

    def _rewrite_pending_file(self, records):
        """整体重写 jsonl（条数少，重写最稳）。"""
        try:
            tmp = PENDING_FILE + ".gui.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp, PENDING_FILE)
            return True
        except Exception as e:
            messagebox.showerror("写入失败", f"更新待确认队列失败：\n{e}")
            return False

    def _drop_pending(self, pending_id):
        records = [r for r in self._load_pending_from_disk() if r.get("id") != pending_id]
        return self._rewrite_pending_file(records)

    def _call_bot_http(self, path, payload):
        """直接问正在跑的 bot 进程（它才是唯一能操作微信的进程）。"""
        try:
            cfg = self.load_config()
            ps = cfg.get("proactive_send") or {}
            host = ps.get("host") or "127.0.0.1"
            port = int(ps.get("port") or 8765)
            url = f"http://{host}:{port}{path}"
            r = requests.post(url, json=payload, timeout=20)
            return r.status_code, r.json()
        except Exception as e:
            return None, {"ok": False, "code": "http_error", "detail": str(e)}

    def poll_pending_confirm(self):
        """每 2 秒轮询一次待确认队列，有新消息就弹窗。"""
        if not self.is_monitoring:
            return
        try:
            items = self._load_pending_from_disk()
            if items:
                self._show_confirm_dialog(items)
        except Exception:
            pass
        try:
            self._safe_after(2000, self.poll_pending_confirm)
        except Exception:
            pass

    def _show_confirm_dialog(self, items):
        """一次只弹一条，避免刷屏；处理完自然轮询到下一批。"""
        if getattr(self, "_confirm_dialog_open", False):
            return
        rec = items[0]
        self._confirm_dialog_open = True

        win = tk.Toplevel(self)
        win.title("📨 待确认的主动消息")
        win.geometry("520x320")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        ttk.Label(win, text=f"收件人：{rec.get('target','')}", font=("微软雅黑", 11, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        ttk.Label(win, text="内容：", font=("微软雅黑", 9)).pack(anchor="w", padx=16)

        txt = tk.Text(win, height=6, wrap=tk.WORD, font=("微软雅黑", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)
        txt.insert("1.0", rec.get("text", ""))
        txt.configure(state=tk.DISABLED)

        ttk.Label(
            win,
            text=f"来源：{rec.get('source','api')}    排队：{len(items)} 条",
            font=("微软雅黑", 8), foreground="#666666",
        ).pack(anchor="w", padx=16)

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=16, pady=12)

        def _close():
            self._confirm_dialog_open = False
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        def _do_send():
            code, body = self._call_bot_http("/confirm", {"id": rec.get("id")})
            if body.get("ok"):
                self.txt_log.insert(tk.END, f"\n✅ [待确认] 已发送给【{rec.get('target')}】: {rec.get('text')}\n")
                self.txt_log.see(tk.END)
                _close()
            else:
                # bot 没在跑 / 被护栏拦下 —— 提示但不清队列
                messagebox.showwarning(
                    "没能发送",
                    f"{body.get('detail') or body.get('code')}\n\n"
                    "（如果 bot 没在运行，请先点「启动 AI 自动回复」再确认）",
                )

        def _do_skip():
            code, body = self._call_bot_http("/reject", {"id": rec.get("id"), "reason": "gui"})
            if not body.get("ok"):
                self._drop_pending(rec.get("id"))
            self.txt_log.insert(tk.END, f"\n🚫 [待确认] 已跳过发给【{rec.get('target')}】的消息\n")
            self.txt_log.see(tk.END)
            _close()

        ttk.Button(btns, text="✅ 发送", command=_do_send).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="🚫 跳过", command=_do_skip).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="稍后再说", command=_close).pack(side=tk.RIGHT, padx=4)

        win.protocol("WM_DELETE_WINDOW", _close)
        win.update_idletasks()
        # 居中到主窗口
        try:
            x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
            win.geometry(f"+{max(x,0)}+{max(y,0)}")
        except Exception:
            pass

if __name__ == "__main__":
    app = WeChatBotGUI()
    app.mainloop()

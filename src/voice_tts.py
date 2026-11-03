#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音合成：调用本机 dsh-breeze-tts 插件把文字变成语音文件。

设计要点
- 不直接依赖 Breeze 服务的调用细节：插件已经把「Breeze /v1/audio/speech + WAV 头修复」
  封装成了 HTTP 接口，bot 只需要 POST 一次拿 WAV 二进制即可。
- 主通道：`/dsh-breeze-tts-api/speak-preview`（用插件配置里的 Breeze 端点，返回 audio/wav）。
- 兜底通道：`/dsh-breeze-tts-api/speak`（插件自带 edge-tts，返回 {url}，再下载成 mp3）。
  Breeze 服务没起来 / 模型没加载时，语音功能也不会整条废掉。
- 生成的音频统一落到 VOICE_DIR，文件名带时间戳，发送完由调用方删除。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DSH_BASE = "http://127.0.0.1:25675"
PREVIEW_PATH = "/dsh-breeze-tts-api/speak-preview"
SPEAK_PATH = "/dsh-breeze-tts-api/speak"
VOICE_DIR = os.path.join("/tmp", "wx_bot_voice")
MAX_TEXT = 300

_UA = "WeChat-AI-AutoReply/voice"


class TTSError(RuntimeError):
    pass


def _post(path: str, payload: dict, timeout: float) -> bytes:
    """POST JSON，返回原始响应体；非 2xx 抛 TTSError（带服务端 error 文本）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        DSH_BASE + path,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            return body, ctype
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise TTSError(f"HTTP {e.code} {detail}".strip())
    except urllib.error.URLError as e:
        raise TTSError(f"接口不可达: {getattr(e, 'reason', e)}")
    except Exception as e:  # socket timeout 等
        raise TTSError(f"请求失败: {type(e).__name__}: {e}")


def _sniff_ext(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:3] == b"ID3":
        return ".mp3"
    if data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if data[:4] == b"OggS":
        return ".ogg"
    return ""


def _save(data: bytes, ext: str) -> str:
    os.makedirs(VOICE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(VOICE_DIR, f"voice_{stamp}_{int(time.time() * 1000) % 1000:03d}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _download(url: str, timeout: float) -> bytes:
    if url.startswith("/"):
        url = DSH_BASE + url
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise TTSError(f"下载音频失败: {type(e).__name__}: {e}")


def duration_seconds(path):
    """返回音频时长（秒）；拿不到就 0.0。"""
    if not path or not os.path.isfile(path):
        return 0.0
    ext = os.path.splitext(path)[1].lower()
    if ext in (".wav", ".wave"):
        try:
            import wave as _wave
            with _wave.open(path, "rb") as w:
                rate = w.getframerate() or 1
                return float(w.getnframes()) / float(rate)
        except Exception:
            pass
    try:
        from mutagen import File as _MFile  # type: ignore
        f = _MFile(path)
        if f is not None and getattr(f, "info", None) is not None:
            return float(f.info.length)
    except Exception:
        pass
    return 0.0


def synthesize(
    text: str,
    voice: str = "",
    instruction: str = "",
    fallback_voice: str = "zh-CN-XiaoxuanNeural",
    allow_edge_fallback: bool = True,
    timeout: float = 180.0,
) -> dict:
    """把一段文字合成语音。

    返回 {"path": 本地音频路径, "engine": "breeze"|"edge-tts", "ext": 扩展名}
    失败抛 TTSError，调用方决定是否降级成纯文字。
    """
    text = (text or "").strip()
    if not text:
        raise TTSError("空文本")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]

    errors = []

    # ---- 1) Breeze 引擎（插件 speak-preview） ----
    payload = {"input": text}
    if voice:
        payload["voice"] = voice
    if instruction:
        payload["instructions"] = instruction
    try:
        data, ctype = _post(PREVIEW_PATH, payload, timeout)
        ext = _sniff_ext(data)
        if not ext:
            raise TTSError(f"返回内容不是音频（{ctype or 'unknown'}, {len(data)}B）")
        return {"path": _save(data, ext), "engine": "breeze", "ext": ext}
    except TTSError as e:
        errors.append(f"breeze: {e}")

    # ---- 2) 兜底：插件自带 edge-tts（返回 {url}） ----
    if allow_edge_fallback:
        try:
            body, _ctype = _post(
                SPEAK_PATH,
                {"text": text, "provider": "edge-tts", "voice": fallback_voice},
                timeout,
            )
            info = json.loads(body.decode("utf-8", "replace") or "{}")
            url = str(info.get("url") or "")
            if not url:
                raise TTSError(f"未返回音频地址: {body[:200]!r}")
            data = _download(url, timeout)
            ext = _sniff_ext(data) or ".mp3"
            return {"path": _save(data, ext), "engine": "edge-tts", "ext": ext}
        except TTSError as e:
            errors.append(f"edge-tts: {e}")

    raise TTSError(" | ".join(errors) or "未知错误")


if __name__ == "__main__":  # 手动自检：python3 voice_tts.py "你好"
    import sys

    say = " ".join(sys.argv[1:]) or "你好，这是一条测试语音。"
    try:
        info = synthesize(say)
        print(json.dumps(info, ensure_ascii=False))
    except TTSError as e:
        print("TTS 失败:", e)
        raise SystemExit(1)

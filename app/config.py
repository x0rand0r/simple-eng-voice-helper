"""Paths, languages and persistent settings (data/settings.json)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
MODELS_DIR = ROOT / "models"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SETTINGS_FILE = DATA_DIR / "settings.json"

# code -> (English name used in prompts, native name shown in the UI)
LANGUAGES: dict[str, tuple[str, str]] = {
    "ru": ("Russian", "Русский"),
    "en": ("English", "English"),
    "de": ("German", "Deutsch"),
    "fr": ("French", "Français"),
    "es": ("Spanish", "Español"),
    "it": ("Italian", "Italiano"),
    "pt": ("Portuguese", "Português"),
    "uk": ("Ukrainian", "Українська"),
    "be": ("Belarusian", "Беларуская"),
    "kk": ("Kazakh", "Қазақша"),
    "uz": ("Uzbek", "Oʻzbekcha"),
    "hy": ("Armenian", "Հայերեն"),
    "ka": ("Georgian", "ქართული"),
    "pl": ("Polish", "Polski"),
    "cs": ("Czech", "Čeština"),
    "sr": ("Serbian", "Српски"),
    "bg": ("Bulgarian", "Български"),
    "ro": ("Romanian", "Română"),
    "hu": ("Hungarian", "Magyar"),
    "el": ("Greek", "Ελληνικά"),
    "nl": ("Dutch", "Nederlands"),
    "sv": ("Swedish", "Svenska"),
    "no": ("Norwegian", "Norsk"),
    "da": ("Danish", "Dansk"),
    "fi": ("Finnish", "Suomi"),
    "tr": ("Turkish", "Türkçe"),
    "he": ("Hebrew", "עברית"),
    "ar": ("Arabic", "العربية"),
    "hi": ("Hindi", "हिन्दी"),
    "zh": ("Chinese (Mandarin)", "中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "th": ("Thai", "ไทย"),
    "id": ("Indonesian", "Bahasa Indonesia"),
}

LLM_MODELS = ["gpt-4.1-mini", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1-nano", "gpt-4o-mini", "gpt-4.1"]
STT_MODELS = ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"]

DEFAULTS: dict[str, Any] = {
    "api_key": "",
    "base_url": "",
    "llm_model": "gpt-4.1-mini",
    "stt_model": "gpt-4o-mini-transcribe",
    "lang_a": "ru",
    "lang_b": "en",
    "translate_mode": "auto",       # auto | a_to_b | off
    "coach_style": "natural",       # minimal | natural | advanced
    "explain": False,
    "mic_enabled": True,
    "loop_enabled": True,
    "mic_device": "",               # "" = default Windows microphone
    "loop_device": "",              # "" = default Windows output
    "vad_threshold": 0.5,
    "pause_ms": 650,
    "min_speech_ms": 250,
    "voice_gate_db": 12,            # 0 = keep all voices
    "echo_guard": "mic_priority",   # off | mic_priority | loop_priority
    "context_tokens": 2000,
    "ui_scale": 1.0,
    "theme": "auto",                # auto | light | dark
    "auto_exit": True,
}

CHOICES = {
    "translate_mode": {"auto", "a_to_b", "off"},
    "coach_style": {"minimal", "natural", "advanced"},
    "echo_guard": {"off", "mic_priority", "loop_priority"},
    "theme": {"auto", "light", "dark"},
    "lang_a": set(LANGUAGES),
    "lang_b": set(LANGUAGES),
}

RANGES = {
    "vad_threshold": (0.2, 0.9),
    "pause_ms": (250, 2000),
    "min_speech_ms": (100, 1500),
    "voice_gate_db": (0, 40),
    "context_tokens": (0, 32000),
    "ui_scale": (0.7, 1.8),
}

# changing these needs the audio devices to be reopened
AUDIO_KEYS = {"mic_device", "loop_device", "mic_enabled", "loop_enabled"}


def lang_name(code: str) -> str:
    return LANGUAGES.get(code, (code, code))[0]


class Settings:
    """Thread-safe settings store; audio threads read it, the web API writes it."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            self._data.update(self._validate(stored))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            print(f"[settings] {path.name} is unreadable, using defaults: {e}")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def api_key(self) -> str:
        return self._data["api_key"] or os.environ.get("OPENAI_API_KEY", "")

    def update(self, changes: dict[str, Any]) -> set[str]:
        """Apply valid changes, persist them and return the keys that actually changed."""
        clean = self._validate(changes)
        with self._lock:
            changed = {k for k, v in clean.items() if self._data.get(k) != v}
            self._data.update(clean)
            if changed:
                self._save()
        return changed

    def public(self) -> dict[str, Any]:
        data = {k: v for k, v in self._data.items() if k != "api_key"}
        key = self.api_key()
        data["api_key_set"] = bool(key)
        data["api_key_hint"] = f"{key[:3]}…{key[-4:]}" if len(key) > 12 else ""
        data["api_key_from_env"] = bool(key) and not self._data["api_key"]
        return data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _validate(raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in DEFAULTS:
                continue
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, int):
                    value = int(round(float(value)))
                elif isinstance(default, float):
                    value = float(value)
                else:
                    value = str(value).strip()
            except (TypeError, ValueError):
                continue
            if key in RANGES:
                lo, hi = RANGES[key]
                value = type(default)(min(max(value, lo), hi))
            if key in CHOICES and value not in CHOICES[key]:
                continue
            out[key] = value
        return out

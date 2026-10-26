"""Audio capture (WASAPI mic + output loopback), phrase segmentation and loudest-voice isolation."""
from __future__ import annotations

import io
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pyaudiowpatch as pyaudio
import soxr

from .config import Settings
from .vad import FRAME, SR, make_vad, rms_db

FRAME_SEC = FRAME / SR
PRE_ROLL_SEC = 0.3      # audio kept before the detected speech onset
TAIL_KEEP_SEC = 0.25    # silence kept after the last word
ONSET_FRAMES = 2        # consecutive speech frames needed to open a phrase
SOFT_MAX_SEC = 12.0     # after this, cut the phrase at the next short pause
HARD_MAX_SEC = 25.0
VOICE_HOLD_SEC = 0.3    # echo guard keeps the other side muted this long after speech
SILENT_WARN_SEC = 4.0
TTS_MAX_SEC = 30.0      # loopback mute while the UI reads a suggestion aloud (safety cap)
TTS_TAIL_SEC = 0.4


@dataclass
class Segment:
    source: str             # "mic" | "loop"
    audio: np.ndarray       # float32 mono, 16 kHz
    db: np.ndarray          # per-frame level, dBFS
    speech: np.ndarray      # per-frame VAD decision
    t_start: float          # wall clock
    t_end: float

    @property
    def duration(self) -> float:
        return self.audio.size / SR

    def wav(self) -> bytes:
        audio = self.audio
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if 1e-4 < peak < 0.7:  # quiet speech is recognized better when normalized
            audio = audio * min(0.7 / peak, 30.0)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()


# ----------------------------------------------------------------------------- devices

def _wasapi_devices(p: pyaudio.PyAudio) -> tuple[list[dict], list[dict], dict]:
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    inputs, loops = [], []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] <= 0:
            continue
        (loops if d.get("isLoopbackDevice") else inputs).append(d)
    return inputs, loops, wasapi


def _default_mic(inputs: list[dict], default_index: int) -> dict | None:
    return next((d for d in inputs if d["index"] == default_index), inputs[0] if inputs else None)


def _default_loop(p: pyaudio.PyAudio, loops: list[dict]) -> dict | None:
    try:
        return p.get_default_wasapi_loopback()
    except (OSError, LookupError):
        return loops[0] if loops else None


def list_devices() -> dict:
    p = pyaudio.PyAudio()
    try:
        inputs, loops, wasapi = _wasapi_devices(p)
        mic = _default_mic(inputs, wasapi["defaultInputDevice"])
        loop = _default_loop(p, loops)
        return {
            "inputs": [d["name"] for d in inputs],
            "loopbacks": [d["name"] for d in loops],
            "default_input": mic["name"] if mic else "",
            "default_loopback": loop["name"] if loop else "",
        }
    finally:
        p.terminate()


def _resolve(p: pyaudio.PyAudio, key: str, wanted: str) -> tuple[dict | None, str | None]:
    """Find the configured device by name; fall back to the Windows default with a warning."""
    inputs, loops, wasapi = _wasapi_devices(p)
    pool = inputs if key == "mic" else loops
    if wanted:
        for d in pool:
            if d["name"] == wanted:
                return d, None
    auto = _default_mic(inputs, wasapi["defaultInputDevice"]) if key == "mic" else _default_loop(p, loops)
    warning = f"Устройство «{wanted}» не найдено, используется «{auto['name']}»" if wanted and auto else None
    return auto, warning


# ----------------------------------------------------------------------------- segmentation

class Segmenter:
    """Cuts a 16 kHz frame stream into phrases using VAD probabilities with hysteresis."""

    def __init__(self, source: str, emit: Callable[[Segment], None]):
        self.source = source
        self.emit = emit
        self.pre: deque = deque(maxlen=round(PRE_ROLL_SEC / FRAME_SEC))
        self._reset()

    def _reset(self) -> None:
        self.active = False
        self.frames: list[tuple[np.ndarray, float, bool]] = []
        self.silence = 0
        self.onset = 0
        self.t0 = 0.0

    def push(self, frame: np.ndarray, prob: float, db: float, s: Settings) -> bool:
        """Feed one frame; returns True if it was classified as speech."""
        thr = s["vad_threshold"]
        if not self.active:
            speech = prob >= thr
            self.pre.append((frame, db, speech))
            self.onset = self.onset + 1 if speech else 0
            if self.onset >= ONSET_FRAMES:
                self.active = True
                self.t0 = time.time() - len(self.pre) * FRAME_SEC
                self.frames = list(self.pre)
                self.pre.clear()
                self.silence = 0
            return speech

        speech = prob >= thr - 0.15
        self.frames.append((frame, db, speech))
        self.silence = 0 if speech else self.silence + 1
        duration = len(self.frames) * FRAME_SEC
        pause = self.silence * FRAME_SEC
        if (pause >= s["pause_ms"] / 1000 or (duration >= SOFT_MAX_SEC and pause >= 0.2)
                or duration >= HARD_MAX_SEC):
            self._finish(s["min_speech_ms"] / 1000)
        return speech

    def _finish(self, min_speech: float) -> None:
        frames = self.frames
        extra = self.silence - round(TAIL_KEEP_SEC / FRAME_SEC)
        if extra > 0:
            frames = frames[:-extra]
        t0 = self.t0
        self._reset()
        speech = np.array([f[2] for f in frames], dtype=bool)
        if speech.sum() * FRAME_SEC < min_speech:
            return
        self.emit(Segment(
            source=self.source,
            audio=np.concatenate([f[0] for f in frames]),
            db=np.array([f[1] for f in frames], dtype=np.float32),
            speech=speech,
            t_start=t0,
            t_end=t0 + len(frames) * FRAME_SEC,
        ))


class VoiceGate:
    """Keeps only the loudest (main) voice of the incoming stream.

    A reference level follows the dominant speaker (75th percentile of voiced frames, slowly
    decaying). Phrases much quieter than it are dropped as background voices, and quiet parts
    inside a phrase (another person speaking in the main speaker's pauses) are muted.
    """

    DECAY_DB_PER_SEC = 0.5
    ENVELOPE_FRAMES = 11    # ~350 ms moving max, so soft syllables of the main voice survive

    def __init__(self):
        self.ref: float | None = None
        self.t = 0.0

    def apply(self, seg: Segment, gate_db: float, min_speech: float) -> Segment | None:
        if gate_db <= 0 or not seg.speech.any():
            return seg
        level = float(np.percentile(seg.db[seg.speech], 75))
        ref = level if self.ref is None else self.ref - self.DECAY_DB_PER_SEC * max(0.0, seg.t_end - self.t)
        if level < ref - gate_db:
            return None
        self.ref, self.t = max(level, ref), seg.t_end

        pad = self.ENVELOPE_FRAMES // 2
        env = np.lib.stride_tricks.sliding_window_view(np.pad(seg.db, pad, mode="edge"), self.ENVELOPE_FRAMES).max(axis=1)
        keep = env >= self.ref - gate_db
        if keep.all():
            return seg
        if (seg.speech & keep).sum() * FRAME_SEC < min_speech:
            return None
        gain = np.repeat(keep.astype(np.float32), FRAME)
        ramp = np.ones(160, dtype=np.float32) / 160   # 10 ms fades, no clicks
        gain = np.convolve(gain, ramp, mode="same")[: seg.audio.size]
        seg.audio = seg.audio * gain
        seg.speech = seg.speech & keep
        return seg


# ----------------------------------------------------------------------------- capture

class Source:
    """One capture device: WASAPI callback -> queue -> worker thread (resample, VAD, segment)."""

    def __init__(self, key: str, settings: Settings, on_segment: Callable[[Segment], None],
                 suppressed: Callable[[], bool]):
        self.key = key
        self.settings = settings
        self.on_segment = on_segment
        self.suppressed = suppressed
        self.device = ""
        self.level = 0.0
        self.last_voice = 0.0
        self.silent_warning = False
        self.error: str | None = None
        self._segmenter = Segmenter(key, self._emit)
        self._gate = VoiceGate() if key == "loop" else None
        self._vad = make_vad()
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=1000)
        self._running = False
        self._stream = None
        self._thread: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        return self._segmenter.active

    def voice_recent(self) -> bool:
        return time.monotonic() - self.last_voice < VOICE_HOLD_SEC

    def start(self, p: pyaudio.PyAudio, dev: dict) -> None:
        self.device = dev["name"]
        self._channels = int(dev["maxInputChannels"])
        rate = int(dev["defaultSampleRate"])
        self._resampler = soxr.ResampleStream(rate, SR, 1, dtype="float32")
        self._running = True
        self._stream = p.open(format=pyaudio.paFloat32, channels=self._channels, rate=rate, input=True,
                              input_device_index=dev["index"], frames_per_buffer=rate // 50,
                              stream_callback=self._callback)
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.key}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.level = 0.0

    def _callback(self, data, frames, time_info, status):
        try:
            self._q.put_nowait(data)
        except queue.Full:
            pass
        return None, pyaudio.paContinue

    def _run(self) -> None:
        pending = np.zeros(0, dtype=np.float32)
        last = time.monotonic()
        silent_frames = 0
        silence = np.zeros(FRAME, dtype=np.float32)
        while self._running:
            try:
                data = self._q.get(timeout=0.1)
            except queue.Empty:
                # WASAPI loopback delivers nothing while no app is playing: treat the gap as silence
                # so an open phrase still gets closed.
                now = time.monotonic()
                for _ in range(int((now - last) / FRAME_SEC)):
                    self._frame(silence)
                last = now
                continue
            last = time.monotonic()
            x = np.frombuffer(data, dtype=np.float32)
            if self._channels > 1:
                x = x.reshape(-1, self._channels).mean(axis=1)
            pending = np.concatenate([pending, self._resampler.resample_chunk(x)])
            n = pending.size // FRAME
            for i in range(n):
                frame = pending[i * FRAME:(i + 1) * FRAME]
                silent_frames = silent_frames + 1 if not frame.any() else 0
                self._frame(frame)
            pending = pending[n * FRAME:]
            if self.key == "mic" and silent_frames * FRAME_SEC >= SILENT_WARN_SEC:
                self.silent_warning = True

    def _frame(self, frame: np.ndarray) -> None:
        db = rms_db(frame)
        meter = min(1.0, max(0.0, (db + 60.0) / 55.0))
        self.level = meter if meter > self.level else self.level * 0.85
        prob = self._vad(frame)
        if self.suppressed():
            frame, prob = np.zeros_like(frame), 0.0
        if self._segmenter.push(frame, prob, db, self.settings):
            self.last_voice = time.monotonic()

    def _emit(self, seg: Segment) -> None:
        if self._gate is not None:
            seg = self._gate.apply(seg, self.settings["voice_gate_db"], self.settings["min_speech_ms"] / 1000)
            if seg is None:
                return
        self.on_segment(seg)


class AudioEngine:
    """Owns the PyAudio instance and both sources; applies the echo guard between them."""

    def __init__(self, settings: Settings, on_segment: Callable[[Segment], None]):
        self.settings = settings
        self.on_segment = on_segment
        self.sources: dict[str, Source] = {}
        self._pa: pyaudio.PyAudio | None = None
        self._lock = threading.Lock()
        self._loop_muted_until = 0.0

    def mute_loop(self, on: bool) -> None:
        """Ignore the output while our own text-to-speech is playing."""
        self._loop_muted_until = time.monotonic() + (TTS_MAX_SEC if on else TTS_TAIL_SEC)

    @property
    def running(self) -> bool:
        return self._pa is not None

    def start(self) -> list[str]:
        """Open the enabled devices. Returns human-readable warnings (errors don't raise)."""
        with self._lock:
            self._stop_locked()
            self._pa = pyaudio.PyAudio()
            warnings = []
            for key in ("mic", "loop"):
                if not self.settings[f"{key}_enabled"]:
                    continue
                src = Source(key, self.settings, self.on_segment, self._suppressor(key))
                self.sources[key] = src
                dev, warning = _resolve(self._pa, key, self.settings[f"{key}_device"])
                if warning:
                    warnings.append(warning)
                if dev is None:
                    src.error = "Устройство не найдено"
                    warnings.append("Микрофон не найден" if key == "mic" else "Устройство вывода для захвата не найдено")
                    continue
                try:
                    src.start(self._pa, dev)
                except OSError as e:
                    src.stop()
                    src.error = str(e)
                    warnings.append(f"Не удалось открыть «{dev['name']}»: {e}")
            return warnings

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        for src in self.sources.values():
            src.stop()
        self.sources = {}
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def _suppressor(self, key: str) -> Callable[[], bool]:
        # "mic_priority": my own voice leaking into the output (mic monitoring in a mixer) is ignored;
        # "loop_priority": the interlocutor coming out of speakers into the mic is ignored.
        owner = {"mic_priority": "loop", "loop_priority": "mic"}

        def suppressed() -> bool:
            if key == "loop" and time.monotonic() < self._loop_muted_until:
                return True
            mode = self.settings["echo_guard"]
            if owner.get(mode) != key:
                return False
            other = self.sources.get("mic" if key == "loop" else "loop")
            return other is not None and other.voice_recent()

        return suppressed

    def status(self) -> dict:
        return {
            key: {"level": round(src.level, 3), "speaking": src.speaking, "device": src.device, "error": src.error}
            for key, src in self.sources.items()
        }

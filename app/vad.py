"""Voice activity detection: Silero VAD (ONNX, no torch) with an energy-based fallback."""
from __future__ import annotations

import threading
import urllib.request

import numpy as np

from .config import MODELS_DIR

SR = 16000
FRAME = 512            # Silero v5+ works on 32 ms windows at 16 kHz
CONTEXT = 64           # samples of the previous window prepended to each call

MODEL_PATH = MODELS_DIR / "silero_vad.onnx"
MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"

_session = None
_session_lock = threading.Lock()


def rms_db(x: np.ndarray) -> float:
    return float(10.0 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def _load_session():
    """Load the ONNX model once (downloading it on first run); None if unavailable."""
    global _session
    with _session_lock:
        if _session is not None:
            return _session or None
        try:
            import onnxruntime as ort

            if not MODEL_PATH.exists():
                print("[vad] downloading Silero VAD model…")
                MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = MODEL_PATH.with_suffix(".part")
                urllib.request.urlretrieve(MODEL_URL, tmp)
                tmp.replace(MODEL_PATH)
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 3
            sess = ort.InferenceSession(str(MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
            if {i.name for i in sess.get_inputs()} != {"input", "state", "sr"}:
                raise RuntimeError("unsupported Silero model version")
            _session = sess
        except Exception as e:  # noqa: BLE001 - any failure falls back to the energy VAD
            print(f"[vad] Silero VAD unavailable ({e}); using energy-based detection")
            _session = False
        return _session or None


class SileroVAD:
    """Stateful Silero VAD for one audio stream; returns speech probability per 512-sample frame."""

    def __init__(self, session):
        self._sess = session
        self._sr = np.array(SR, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32, copy=False)], axis=1)
        out, self._state = self._sess.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


class EnergyVAD:
    """Fallback: speech = noticeably louder than the adaptive noise floor."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._floor = -60.0

    def __call__(self, frame: np.ndarray) -> float:
        db = rms_db(frame)
        rate = 0.2 if db < self._floor else 0.005
        self._floor += rate * (db - self._floor)
        margin = db - max(self._floor, -75.0) - 12.0
        return float(1.0 / (1.0 + np.exp(-margin / 2.0)))


def make_vad():
    session = _load_session()
    return SileroVAD(session) if session is not None else EnergyVAD()


def preload() -> None:
    """Warm the model up in the background so the first Start is instant."""
    threading.Thread(target=_load_session, name="vad-preload", daemon=True).start()

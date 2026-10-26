"""Web server (FastAPI + WebSocket) and the controller tying audio, OpenAI and the session together."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import APP_ID, __version__, vad
from .ai import AI, friendly_error
from .audio import AudioEngine, Segment, list_devices
from .config import AUDIO_KEYS, LANGUAGES, LLM_MODELS, LOGS_DIR, STATIC_DIR, STT_MODELS, Settings
from .prompts import (NOTE_SEPARATOR, clean_transcript, coach_system, strip_quotes, stt_prompt,
                      translate_system, user_message)
from .session import Item, Session, Tokenizer

LEVELS_INTERVAL_SEC = 0.07
IDLE_GRACE_SEC = 6.0        # window closed this long -> stop listening (and exit if enabled)
MAX_PARALLEL_REQUESTS = 4
TOAST_REPEAT_SEC = 8.0


class Controller:
    def __init__(self):
        self.settings = Settings()
        self.tokenizer = Tokenizer()
        self.ai = AI(self.settings)
        self.session = Session(self.tokenizer)
        self.audio = AudioEngine(self.settings, self._on_segment)
        self.clients: set[WebSocket] = set()
        self.listening = False
        self.request_exit: Callable[[], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
        self._tasks: set[asyncio.Task] = set()
        self._levels_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._toasts: dict[str, float] = {}
        self._silent_warned = False

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        vad.preload()

    async def shutdown(self) -> None:
        await self.stop_listening(notify=False)
        self.session.log.close()

    # ------------------------------------------------------------------ clients

    async def connect(self, ws: WebSocket) -> bool:
        origin = ws.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            await ws.close(code=1008)   # other websites must not drive the local app
            return False
        await ws.accept()
        self.clients.add(ws)
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        await ws.send_json({
            "type": "hello",
            "version": __version__,
            "meta": {
                "languages": [{"code": c, "name": n[1], "en": n[0]} for c, n in LANGUAGES.items()],
                "llm_models": LLM_MODELS,
                "stt_models": STT_MODELS,
            },
            "session": self.session.public(),
            **self.state(),
        })
        return True

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        if not self.clients and self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_check())

    async def _idle_check(self) -> None:
        await asyncio.sleep(IDLE_GRACE_SEC)
        self._idle_task = None
        if self.clients:
            return
        await self.stop_listening(notify=False)
        if self.settings["auto_exit"] and self.request_exit is not None:
            print("Окно закрыто — завершаю работу.")
            self.request_exit()

    async def broadcast(self, msg: dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 - a dying socket is cleaned up by its handler
                self.clients.discard(ws)

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "start":
            await self.start_listening()
        elif kind == "stop":
            await self.stop_listening()
        elif kind == "text":
            self._run(self.handle_text(str(msg.get("text", ""))))
        elif kind == "settings":
            await self.update_settings(msg.get("changes") or {})
        elif kind == "new_chat":
            await self.new_chat()
        elif kind == "clear_context":
            self.session.context_floor = self.session.items[-1].id if self.session.items else 0
            await self.broadcast_state()
        elif kind == "check_key":
            self._run(self.check_key())
        elif kind == "tts":
            self.audio.mute_loop(bool(msg.get("playing")))
        elif kind == "open_logs":
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(LOGS_DIR)  # noqa: S606 - Windows only, opens Explorer

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        budget = self.settings["context_tokens"]
        _, used, first = self.session.history(budget)
        return {
            "listening": self.listening,
            "settings": self.settings.public(),
            "context": {"used": used, "max": budget, "first_id": first, "floor": self.session.context_floor},
            "sources": self.audio.status(),
        }

    async def broadcast_state(self) -> None:
        await self.broadcast({"type": "state", **self.state()})

    async def toast(self, level: str, message: str, action: str | None = None) -> None:
        now = time.monotonic()
        if now - self._toasts.get(message, 0.0) < TOAST_REPEAT_SEC:
            return
        self._toasts[message] = now
        print(f"[{level}] {message}")
        await self.broadcast({"type": "toast", "level": level, "message": message, "action": action})

    async def update_settings(self, changes: dict[str, Any]) -> None:
        changed = self.settings.update(changes)
        if self.listening and changed & AUDIO_KEYS:
            await self._open_audio()
        await self.broadcast_state()

    async def check_key(self) -> None:
        try:
            await self.ai.check()
            await self.toast("ok", "Ключ OpenAI работает")
        except Exception as e:  # noqa: BLE001
            await self.toast("error", friendly_error(e), action="settings")

    async def new_chat(self) -> None:
        self.session.log.close()
        self.session = Session(self.tokenizer)
        await self.broadcast({"type": "session", "session": self.session.public()})
        await self.broadcast_state()

    # ------------------------------------------------------------------ listening

    async def start_listening(self) -> None:
        if self.listening:
            return
        if not self.settings.api_key():
            await self.toast("error", "Укажите API ключ OpenAI в настройках", action="settings")
            await self.broadcast_state()
            return
        if not (self.settings["mic_enabled"] or self.settings["loop_enabled"]):
            await self.toast("warn", "Включите микрофон или звук собеседника")
            await self.broadcast_state()
            return
        if not await self._open_audio():
            await asyncio.to_thread(self.audio.stop)
            await self.broadcast_state()
            return
        self.listening = True
        self._silent_warned = False
        self._levels_task = asyncio.create_task(self._levels_loop())
        await self.broadcast_state()

    async def stop_listening(self, notify: bool = True) -> None:
        if self._levels_task is not None:
            self._levels_task.cancel()
            self._levels_task = None
        if self.audio.running:
            await asyncio.to_thread(self.audio.stop)
        self.listening = False
        if notify:
            await self.broadcast_state()

    async def _open_audio(self) -> bool:
        warnings = await asyncio.to_thread(self.audio.start)
        for w in warnings:
            await self.toast("warn", w)
        return any(src.error is None for src in self.audio.sources.values())

    async def _levels_loop(self) -> None:
        while True:
            await asyncio.sleep(LEVELS_INTERVAL_SEC)
            await self.broadcast({"type": "levels", "sources": self.audio.status()})
            mic = self.audio.sources.get("mic")
            if mic is not None and mic.silent_warning and not self._silent_warned:
                self._silent_warned = True
                await self.toast("warn", f"Микрофон «{mic.device}» передаёт полную тишину. "
                                         "Проверьте, не выключен ли он в Windows или в программе микрофона.")

    # ------------------------------------------------------------------ pipeline

    def _on_segment(self, seg: Segment) -> None:
        """Called from an audio thread when a phrase is cut."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: self._run(self._handle_segment(seg)))

    def _run(self, coro) -> None:
        """Run a long operation in the background, keeping a reference so it isn't collected."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_segment(self, seg: Segment) -> None:
        if not self.listening:
            return
        kind = "me" if seg.source == "mic" else "them"
        session = self.session
        item = session.add(kind, seg.source, seg.t_start)
        await self.emit(item)
        prompt = stt_prompt(kind, self.settings["lang_a"], self.settings["lang_b"])
        try:
            async with self._sem:
                raw = await self.ai.transcribe(seg.wav(), prompt)
        except Exception as e:  # noqa: BLE001
            await self._fail(session, item, e)
            return
        text = clean_transcript(raw, prompt)
        if not text:
            session.remove(item)
            await self.broadcast({"type": "remove", "id": item.id})
            return
        item.text = text
        await self.respond(session, item)

    async def handle_text(self, text: str) -> None:
        text = " ".join(text.split())[:2000]
        if not text:
            return
        if not self.settings.api_key():
            await self.toast("error", "Укажите API ключ OpenAI в настройках", action="settings")
            return
        session = self.session
        item = session.add("me", "typed", time.time())
        item.text = text
        await self.respond(session, item)

    async def respond(self, session: Session, item: Item) -> None:
        s = self.settings
        if item.kind == "them" and s["translate_mode"] == "off":
            await self._finish(session, item)
            return
        item.status = "processing"
        await self.emit(item)
        history, _, _ = session.history(s["context_tokens"], before=item)
        if item.kind == "me":
            system = coach_system(s["lang_a"], s["lang_b"], s["coach_style"], s["explain"])
        else:
            system = translate_system(s["lang_a"], s["lang_b"], s["translate_mode"])
        user = user_message(history, item.kind, item.text)
        reply = ""
        try:
            async with self._sem:
                async for delta in self.ai.stream(system, user):
                    reply += delta
                    self._apply(item, reply)
                    await self.emit(item)
            self._apply(item, reply, final=True)
        except Exception as e:  # noqa: BLE001
            await self._fail(session, item, e)
            return
        await self._finish(session, item)

    @staticmethod
    def _apply(item: Item, reply: str, final: bool = False) -> None:
        if item.kind == "me":
            main, _, notes = reply.partition(NOTE_SEPARATOR)
            main = main.rstrip("#").strip()
            item.suggestion = strip_quotes(main) if final else main
            item.notes = notes.strip()
        else:
            text = reply.strip()
            item.translation = strip_quotes(text) if final else text

    async def _finish(self, session: Session, item: Item) -> None:
        item.status = "done"
        await self.emit(item)
        session.log.flush()
        if session is self.session:
            await self.broadcast_state()

    async def _fail(self, session: Session, item: Item, e: Exception) -> None:
        item.status = "error"
        item.error = friendly_error(e)
        await self.emit(item)
        session.log.flush()
        await self.toast("error", item.error, action="settings" if "ключ" in item.error else None)

    async def emit(self, item: Item) -> None:
        if self.session.by_id.get(item.id) is item:   # skip phrases of a chat that was replaced
            await self.broadcast({"type": "item", "item": item.public()})


controller = Controller()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await controller.startup()
    yield
    await controller.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    stamp = int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir()))
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{v}}", str(stamp))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    return {"app": APP_ID, "version": __version__}


@app.get("/api/devices")
async def devices() -> dict:
    return await asyncio.to_thread(list_devices)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    if not await controller.connect(ws):
        return
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict):
                await controller.handle(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[ws] {type(e).__name__}: {e}")
    finally:
        controller.disconnect(ws)

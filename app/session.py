"""Chat session: items, GPT context window and the crash-safe Markdown log."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, LOGS_DIR
from .prompts import NO_TRANSLATION, history_line

LOG_STALL_SEC = 30.0    # a phrase stuck in processing longer than this is logged as is


@dataclass(eq=False)
class Item:
    id: int
    kind: str               # "them" (interlocutor) | "me"
    source: str             # "loop" | "mic" | "typed"
    t: float
    status: str = "transcribing"    # transcribing | processing | done | error
    text: str = ""
    translation: str = ""
    suggestion: str = ""
    notes: str = ""
    error: str = ""
    created: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("created")
        if data["translation"] == NO_TRANSLATION:
            data["translation"] = ""
        return data

    @property
    def settled(self) -> bool:
        return self.status in ("done", "error")


class Tokenizer:
    """o200k token counter (loaded in the background); a byte-length estimate until then."""

    def __init__(self):
        self._enc = None
        threading.Thread(target=self._load, name="tokenizer", daemon=True).start()

    def _load(self) -> None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(DATA_DIR / "tiktoken"))
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding("o200k_base")
        except Exception as e:  # noqa: BLE001 - offline first run etc.
            print(f"[context] exact token counting unavailable ({e}); using an estimate")

    def count(self, text: str) -> int:
        if self._enc is not None:
            return len(self._enc.encode(text))
        return max(1, len(text.encode("utf-8")) // 3)


class ChatLog:
    """Append-only Markdown log in the format of example.md.

    Each phrase is written with a single write() followed by flush + fsync, so killing the program
    can't corrupt what is already on disk. Phrases are written in the order they were spoken, once
    their translation/suggestion is ready.
    """

    def __init__(self, started: datetime):
        self.started = started
        self.path: Path = LOGS_DIR / f"{started:%Y-%m-%d_%H-%M-%S}.md"
        self._file = None
        self._queue: deque[Item] = deque()
        self._last_kind = ""
        self._lock = threading.Lock()

    def track(self, item: Item) -> None:
        with self._lock:
            self._queue.append(item)

    def flush(self, force: bool = False) -> None:
        with self._lock:
            while self._queue:
                head = self._queue[0]
                stalled = time.monotonic() - head.created > LOG_STALL_SEC
                if not (head.settled or head.status == "removed" or stalled or force):
                    break
                self._queue.popleft()
                if head.status != "removed" and head.text:
                    self._write(self._format(head))

    def close(self) -> None:
        self.flush(force=True)
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def _format(self, item: Item) -> str:
        lines = []
        if item.kind == "them":
            if self._last_kind == "me":
                lines.append("")
            lines.append(f'User: "{item.text}"')
            if item.translation and item.translation != NO_TRANSLATION:
                lines.append(f'Translate: "{item.translation}"')
        else:
            lines.append(f'You: "{item.text}"')
            if item.suggestion:
                lines.append(f'Suggest: "{item.suggestion}"')
            if item.notes:
                lines.append(f"Note: {item.notes}")
        if item.error:
            lines.append(f"_error: {item.error}_")
        self._last_kind = item.kind
        return "\n".join(lines) + "\n"

    def _write(self, text: str) -> None:
        if self._file is None:   # created lazily: no empty logs for sessions without speech
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8", newline="\n")
            text = f"_the chat started at {self.started:%H:%M}_\n\n" + text
        self._file.write(text)
        self._file.flush()
        os.fsync(self._file.fileno())


class Session:
    def __init__(self, tokenizer: Tokenizer):
        self.started = datetime.now()
        self.tokenizer = tokenizer
        self.items: list[Item] = []
        self.by_id: dict[int, Item] = {}
        self.log = ChatLog(self.started)
        self.context_floor = 0      # items with id <= floor are excluded ("clear context")
        self._next_id = 1

    def add(self, kind: str, source: str, t: float) -> Item:
        item = Item(id=self._next_id, kind=kind, source=source, t=t)
        self._next_id += 1
        self.items.append(item)
        self.by_id[item.id] = item
        self.log.track(item)
        return item

    def remove(self, item: Item) -> None:
        item.status = "removed"
        if self.by_id.pop(item.id, None) is item:
            self.items.remove(item)
        self.log.flush()

    def history(self, budget: int, before: Item | None = None) -> tuple[list[str], int, int | None]:
        """Most recent lines that fit into the token budget, oldest first.

        Returns (lines, tokens used, id of the oldest included item)."""
        lines: list[str] = []
        used, first = 0, None
        for it in reversed(self.items):
            if before is not None and it.id >= before.id:
                continue
            if it.id <= self.context_floor:
                break
            if not it.text:
                continue
            line = history_line(it.kind, it.text)
            cost = self.tokenizer.count(line) + 1
            if used + cost > budget:
                break
            lines.append(line)
            used += cost
            first = it.id
        lines.reverse()
        return lines, used, first

    def public(self) -> dict:
        return {
            "started": self.started.timestamp(),
            "log_file": str(self.log.path),
            "items": [it.public() for it in self.items],
        }

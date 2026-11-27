"""Stand-ins for Telethon objects used by tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


def make_message(msg_id: int, *, kind: str = "document", media_id: int | None = None,
                 name: str | None = "file.bin", size: int = 1000, mime: str = "application/octet-stream",
                 grouped_id: int | None = None,
                 date: datetime = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)) -> Any:
    media_id = media_id if media_id is not None else msg_id * 10
    m = SimpleNamespace(id=msg_id, date=date, grouped_id=grouped_id, web_preview=None, media=object(),
                        photo=None, document=None, sticker=None, voice=None, video_note=None,
                        gif=None, audio=None, video=None)
    if kind == "photo":
        m.photo = SimpleNamespace(id=media_id)
        m.file = SimpleNamespace(name=None, size=size, mime_type="image/jpeg", ext=".jpg")
    else:
        m.document = SimpleNamespace(id=media_id, size=size)
        m.file = SimpleNamespace(name=name, size=size, mime_type=mime, ext=None)
        if kind != "document":
            setattr(m, {"animation": "gif"}.get(kind, kind), True)
    return m


@dataclass
class FakeClient:
    """download_media writes ``payload`` (or ``sizes[msg_id]`` bytes) into the file."""
    payload: bytes = b"x" * 1000
    fail_with: BaseException | None = None
    calls: list[int] = field(default_factory=list)

    async def download_media(self, message: Any, file: Any, progress_callback: Any = None) -> Any:
        self.calls.append(message.id)
        file.write(self.payload[: len(self.payload) // 2])
        if self.fail_with is not None:
            raise self.fail_with
        file.write(self.payload[len(self.payload) // 2:])
        if progress_callback:
            progress_callback(len(self.payload), len(self.payload))
        return file


class FakeTelegram:
    """In-memory stand-in for TelegramClient used by worker/monitor/backfill tests."""

    def __init__(self, payload: bytes = b"x" * 1000, delay: float = 0.0) -> None:
        self.payload = payload
        self.delay = delay
        self.messages: dict[tuple[int, int], Any] = {}
        self.download_failures: dict[int, list[BaseException]] = {}
        self.iter_failures: dict[int, list[BaseException]] = {}
        self.concurrent = 0
        self.max_concurrent = 0
        self.downloads: list[int] = []
        self.gate = asyncio.Event()   # set() to let blocked downloads finish
        self.gate.set()
        self.dialogs: list[Any] = []
        self.dialog_calls = 0
        self.fail_after_half: dict[int, list[BaseException]] = {}  # msg id -> errors
        self.resume_offsets: list[int] = []
        self.doc_to_msg: dict[int, int] = {}

    def add_dialog(self, chat_id: int, name: str, *, group: bool = False) -> None:
        self.dialogs.append(SimpleNamespace(id=chat_id, name=name, is_channel=True,
                                            is_group=group, is_user=False,
                                            entity=SimpleNamespace(is_self=False)))

    def add_user_dialog(self, user_id: int, name: str, *, is_self: bool = False, bot: bool = False,
                        deleted: bool = False, username: str | None = None) -> None:
        """A private chat; with is_self=True it is "Saved Messages"."""
        self.dialogs.append(SimpleNamespace(
            id=user_id, name=name, is_channel=False, is_group=False, is_user=True,
            entity=SimpleNamespace(is_self=is_self, bot=bot, deleted=deleted, username=username)))

    def add(self, chat_id: int, message: Any) -> Any:
        self.messages[(chat_id, message.id)] = message
        if getattr(message, "document", None) is not None:
            self.doc_to_msg[message.document.id] = message.id
        return message

    async def iter_download(self, document: Any, *, offset: int = 0, request_size: int = 1024,
                            file_size: Any = None):
        """Telethon-like ranged download (used for resuming)."""
        self.resume_offsets.append(offset)
        assert offset % request_size == 0, "offset must be aligned"
        msg_id = self.doc_to_msg.get(document.id)
        data = self.payload[offset:]
        for i in range(0, len(data), request_size):
            plan = self.fail_after_half.get(msg_id) if i > 0 else None
            if plan:
                raise plan.pop(0)
            await self.gate.wait()
            yield data[i:i + request_size]

    async def get_messages(self, chat_id: int, ids: Any) -> Any:
        if isinstance(ids, list):
            return [self.messages.get((chat_id, i)) for i in ids]
        return self.messages.get((chat_id, ids))

    async def get_dialogs(self) -> list[Any]:
        self.dialog_calls += 1
        return list(self.dialogs)

    async def iter_messages(self, chat_id: int, offset_id: int = 0, offset_date: Any = None):
        msgs = sorted((m for (c, _), m in self.messages.items() if c == chat_id),
                      key=lambda m: m.id, reverse=True)
        for m in msgs:
            if offset_id and m.id >= offset_id:
                continue
            if offset_date is not None and m.date >= offset_date:
                continue
            plan = self.iter_failures.get(m.id)
            if plan:
                raise plan.pop(0)
            yield m

    async def download_media(self, message: Any, file: Any, progress_callback: Any = None) -> Any:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            plan = self.download_failures.get(message.id)
            if plan:
                raise plan.pop(0)
            half = len(self.payload) // 2
            file.write(self.payload[:half])
            if progress_callback:
                progress_callback(half, len(self.payload))
            late = self.fail_after_half.get(message.id)
            if late:
                file.flush()
                raise late.pop(0)
            await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            file.write(self.payload[half:])
            if progress_callback:
                progress_callback(len(self.payload), len(self.payload))
            self.downloads.append(message.id)
            return file
        finally:
            self.concurrent -= 1


async def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)

"""Telethon client wrapper: creation, interactive login, chat helpers."""

from __future__ import annotations

import asyncio
import getpass
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import (
    PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberInvalidError, SessionPasswordNeededError,
)
from telethon.tl.types import Channel, Chat, User

from .. import __version__
from ..config import AppConfig, ensure_private_file
from ..logging_setup import RedactFilter, log_event

log = logging.getLogger("tgdl.telegram")

Translate = Callable[..., str]


# "Saved Messages" (我的收藏) is the private chat with yourself. A fixed title
# keeps its folder name stable (the UI shows a translated label).
SAVED_MESSAGES_TITLE = "Saved Messages"
SAVED_MESSAGES_SEARCH = "saved messages 我的收藏 收藏 已儲存的訊息"


def is_saved_messages(dialog: Any) -> bool:
    return bool(getattr(dialog, "is_user", False)
                and getattr(getattr(dialog, "entity", None), "is_self", False))


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    title: str
    chat_type: str  # channel / group / user / bot / saved
    username: str | None = None  # public @username, if any (search only)

    @classmethod
    def from_entity(cls, entity: Any) -> "ChatInfo":
        peer_id = utils.get_peer_id(entity)
        if isinstance(entity, User) and entity.is_self:
            return cls(chat_id=peer_id, title=SAVED_MESSAGES_TITLE, chat_type="saved")
        if isinstance(entity, Channel):
            ctype = "group" if entity.megagroup or entity.gigagroup else "channel"
        elif isinstance(entity, Chat):
            ctype = "group"
        elif isinstance(entity, User) and entity.bot:
            ctype = "bot"
        else:
            ctype = "user"
        title = utils.get_display_name(entity) or str(peer_id)
        return cls(chat_id=peer_id, title=title, chat_type=ctype,
                   username=getattr(entity, "username", None) or None)


CHAT_TYPES: tuple[str, ...] = ("saved", "channel", "group", "user", "bot")


def build_client(cfg: AppConfig, *, long_running: bool = False) -> TelegramClient:
    """Create the client.

    ``long_running`` (serve): every FloodWait raises so the app can pause the
    affected work and show it in the UI; reconnect forever; fetch updates
    missed while offline (catch_up).
    """
    api_id, api_hash = cfg.require_api()
    RedactFilter.register(api_hash)
    cfg.prepare_dirs()
    client = TelegramClient(
        str(cfg.session_path), api_id, api_hash,
        device_model="tgdl", app_version=__version__, system_version="Linux",
        flood_sleep_threshold=0 if long_running else 60,
        connection_retries=-1 if long_running else 5,
        retry_delay=5 if long_running else 1,
        catch_up=long_running,
    )
    ensure_private_file(cfg.session_path.with_suffix(".session"))
    return client


async def interactive_login(client: TelegramClient, t: Translate) -> User:
    """Log in on the terminal (phone, code, optional 2FA). Never echoes or
    logs the phone number, code or password."""
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(t("cli.login.already", name=utils.get_display_name(me)))
        return me

    phone = ""
    for _ in range(3):
        phone = input(t("cli.login.phone")).strip().replace(" ", "")
        RedactFilter.register(phone)
        try:
            await client.send_code_request(phone)
            break
        except PhoneNumberInvalidError:
            print(t("cli.login.phone_invalid"))
    else:
        raise SystemExit(1)

    for _ in range(3):
        code = input(t("cli.login.code")).strip().replace(" ", "")
        try:
            await client.sign_in(phone=phone, code=code)
            break
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            print(t("cli.login.code_invalid"))
        except SessionPasswordNeededError:
            for _ in range(3):
                pw = getpass.getpass(t("cli.login.password"))
                try:
                    await client.sign_in(password=pw)
                    break
                except PasswordHashInvalidError:
                    print(t("cli.login.password_invalid"))
            else:
                raise SystemExit(1)
            break
    else:
        raise SystemExit(1)

    me = await client.get_me()
    log_event(log, logging.INFO, "telegram_login", "Telegram login succeeded")
    return me


async def resolve_chat(client: TelegramClient, ref: str) -> ChatInfo:
    """Resolve a chat by marked id, @username, or t.me link."""
    target: Any = ref.strip()
    try:
        target = int(target)
    except ValueError:
        pass
    if isinstance(target, int):
        # Ensure the entity cache knows the id.
        try:
            entity = await client.get_entity(target)
        except ValueError:
            await client.get_dialogs()
            entity = await client.get_entity(target)
    else:
        entity = await client.get_entity(target)
    return ChatInfo.from_entity(entity)


class DialogCache:
    """The user's chats (groups, channels, private chats, bots, Saved
    Messages), cached to avoid hammering GetDialogs (rate limited)."""

    TTL = 300.0

    def __init__(self, client: Any, api_gate: Any) -> None:
        self.client = client
        self.api_gate = api_gate
        self._items: list[ChatInfo] = []
        self._at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def from_dialog(d: Any) -> ChatInfo | None:
        """Groups, channels, private chats (people and bots) and Saved
        Messages; None for dialogs that cannot be monitored (deleted
        accounts)."""
        entity = getattr(d, "entity", None)
        username = getattr(entity, "username", None) or None
        if is_saved_messages(d):
            return ChatInfo(chat_id=int(d.id), title=SAVED_MESSAGES_TITLE, chat_type="saved")
        if getattr(d, "is_user", False):
            if getattr(entity, "deleted", False):
                return None
            ctype = "bot" if getattr(entity, "bot", False) else "user"
        elif getattr(d, "is_group", False):
            ctype = "group"
        elif getattr(d, "is_channel", False):
            ctype = "channel"
        else:
            return None
        return ChatInfo(chat_id=int(d.id), title=str(d.name or d.id), chat_type=ctype,
                        username=username)

    async def get(self, refresh: bool = False) -> list[ChatInfo]:
        from ..downloader.flood import FLOOD_ERRORS

        async with self._lock:
            stale = time.monotonic() - self._at > self.TTL
            if self._items and not refresh and not stale:
                return self._items
            if self.api_gate.active and self._items:
                return self._items
            try:
                dialogs = await self.client.get_dialogs()
            except FLOOD_ERRORS as exc:
                self.api_gate.trigger(getattr(exc, "seconds", 60))
                if self._items:
                    return self._items
                raise
            self._items = sorted(
                (c for c in (self.from_dialog(d) for d in dialogs) if c is not None),
                # Saved Messages first, then alphabetically
                key=lambda c: (c.chat_type != "saved", c.title.casefold()))
            self._at = time.monotonic()
            return self._items

    def find(self, chat_id: int) -> ChatInfo | None:
        return next((c for c in self._items if c.chat_id == chat_id), None)

    @staticmethod
    def search(items: list[ChatInfo], query: str, chat_type: str = "") -> list[ChatInfo]:
        """Filter by type (empty = all) and by title, @username, id, or the
        Saved Messages aliases."""
        if chat_type in CHAT_TYPES:
            items = [c for c in items if c.chat_type == chat_type]
        q = query.strip().casefold()
        if not q:
            return items
        handle = q.lstrip("@")
        return [c for c in items if q in c.title.casefold() or q == str(c.chat_id)
                or (handle and c.username and handle in c.username.casefold())
                or (c.chat_type == "saved" and q in SAVED_MESSAGES_SEARCH)]

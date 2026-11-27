"""Telethon client wrapper: creation, interactive login, chat helpers."""

from __future__ import annotations

import getpass
import logging
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


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    title: str
    chat_type: str  # channel / group / user

    @classmethod
    def from_entity(cls, entity: Any) -> "ChatInfo":
        if isinstance(entity, Channel):
            ctype = "group" if entity.megagroup or entity.gigagroup else "channel"
        elif isinstance(entity, Chat):
            ctype = "group"
        else:
            ctype = "user"
        title = utils.get_display_name(entity) or str(utils.get_peer_id(entity))
        return cls(chat_id=utils.get_peer_id(entity), title=title, chat_type=ctype)


def build_client(cfg: AppConfig, *, flood_sleep_threshold: int = 60) -> TelegramClient:
    """``flood_sleep_threshold=0`` makes every FloodWait raise so the worker
    pool can pause and show it in the UI."""
    api_id, api_hash = cfg.require_api()
    RedactFilter.register(api_hash)
    cfg.prepare_dirs()
    client = TelegramClient(
        str(cfg.session_path), api_id, api_hash,
        device_model="tgdl", app_version=__version__, system_version="Linux",
        flood_sleep_threshold=flood_sleep_threshold,
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

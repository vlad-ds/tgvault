"""Thin async gateway around Telethon.

Only this module talks to Telegram. Everything else works on the local DB,
which keeps the network surface small and auditable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.sessions import SQLiteSession
from telethon.tl import types
from telethon.utils import get_display_name

from tgvault.paths import restrict_file


class GatewayError(RuntimeError):
    pass


def _chat_dict(entity, fallback_title: str = "") -> dict:
    if isinstance(entity, types.User):
        return {
            "chat_id": entity.id,
            "entity_type": "user",
            "kind": "user",
            "access_hash": entity.access_hash,
            "title": fallback_title or get_display_name(entity),
            "username": entity.username,
            "phone": entity.phone,
        }
    if isinstance(entity, types.Channel):
        return {
            "chat_id": entity.id,
            "entity_type": "channel",
            "kind": "group" if getattr(entity, "megagroup", False) else "channel",
            "access_hash": entity.access_hash,
            "title": fallback_title or getattr(entity, "title", str(entity.id)),
            "username": entity.username,
            "phone": None,
        }
    if isinstance(entity, types.Chat):
        return {
            "chat_id": entity.id,
            "entity_type": "chat",
            "kind": "group",
            "access_hash": None,
            "title": fallback_title or getattr(entity, "title", str(entity.id)),
            "username": None,
            "phone": None,
        }
    raise GatewayError(f"Unsupported Telegram entity: {type(entity)!r}")


def _message_dict(message) -> dict:
    media_type = None
    if message.media is not None:
        media_type = type(message.media).__name__.removeprefix("MessageMedia").lower()
    text = message.message or ""
    if not text and message.action is not None:
        text = f"[service: {type(message.action).__name__}]"

    sender_name = None
    if message.sender is not None:
        sender_name = get_display_name(message.sender) or None
    if not sender_name:
        sender_name = message.post_author

    return {
        "message_id": message.id,
        "sent_at": message.date.astimezone(UTC).replace(microsecond=0).isoformat(),
        "sender_id": message.sender_id,
        "sender_name": sender_name,
        "is_outgoing": bool(message.out),
        "text": text,
        "media_type": media_type,
        "reply_to_id": message.reply_to_msg_id,
    }


class Gateway:
    def __init__(self, session_path: Path, api_id: int, api_hash: str) -> None:
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash

    @property
    def session_file(self) -> Path:
        return Path(f"{self.session_path}.session")

    def _client(self) -> TelegramClient:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            SQLiteSession(str(self.session_path)), self.api_id, self.api_hash
        )

    async def _connected(self) -> TelegramClient:
        client = self._client()
        await client.connect()
        restrict_file(self.session_file)
        return client

    async def is_authorized(self) -> bool:
        if not self.session_file.exists():
            return False
        client = await self._connected()
        try:
            return await client.is_user_authorized()
        finally:
            await client.disconnect()

    async def me(self) -> dict | None:
        client = await self._connected()
        try:
            if not await client.is_user_authorized():
                return None
            user = await client.get_me()
            return _chat_dict(user) if user else None
        finally:
            await client.disconnect()

    async def login_qr(
        self,
        qr_callback: Callable[[str], None],
        password_callback: Callable[[], str],
        max_refreshes: int = 8,
    ) -> dict:
        client = await self._connected()
        try:
            if await client.is_user_authorized():
                return _chat_dict(await client.get_me())
            qr_login = await client.qr_login()
            refreshes = 0
            while True:
                qr_callback(qr_login.url)
                try:
                    user = await qr_login.wait()
                    return _chat_dict(user)
                except asyncio.TimeoutError:
                    refreshes += 1
                    if refreshes > max_refreshes:
                        raise GatewayError("QR login timed out. Run login again.")
                    await qr_login.recreate()
                except SessionPasswordNeededError:
                    password = password_callback().strip()
                    if not password:
                        raise GatewayError("2FA password required.")
                    user = await client.sign_in(password=password)
                    return _chat_dict(user)
        except RPCError as exc:
            raise GatewayError(f"QR login failed: {exc}") from exc
        finally:
            await client.disconnect()
            restrict_file(self.session_file)

    async def login_code(
        self,
        phone: str,
        code_callback: Callable[[], str],
        password_callback: Callable[[], str],
    ) -> dict:
        client = await self._connected()
        try:
            if await client.is_user_authorized():
                return _chat_dict(await client.get_me())
            await client.send_code_request(phone)
            code = code_callback().strip()
            try:
                user = await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                user = await client.sign_in(password=password_callback().strip())
            return _chat_dict(user)
        except RPCError as exc:
            raise GatewayError(f"Login failed: {exc}") from exc
        finally:
            await client.disconnect()
            restrict_file(self.session_file)

    async def logout(self) -> bool:
        client = await self._connected()
        try:
            if not await client.is_user_authorized():
                return False
            return bool(await client.log_out())
        finally:
            await client.disconnect()

    async def list_dialogs(self, limit: int = 200) -> list[dict]:
        client = await self._connected()
        try:
            await self._require_auth(client)
            dialogs = []
            async for dialog in client.iter_dialogs(limit=limit):
                if dialog.entity is None:
                    continue
                dialogs.append(_chat_dict(dialog.entity, fallback_title=dialog.name))
            return dialogs
        except RPCError as exc:
            raise GatewayError(f"Listing chats failed: {exc}") from exc
        finally:
            await client.disconnect()

    async def fetch_messages(
        self,
        chat: dict,
        after_message_id: int | None,
        limit: int | None,
    ) -> list[dict]:
        client = await self._connected()
        try:
            await self._require_auth(client)
            peer = self._input_peer(chat)
            collected = []
            if after_message_id:
                async for message in client.iter_messages(
                    peer, min_id=after_message_id, reverse=True, limit=limit
                ):
                    collected.append(_message_dict(message))
            else:
                async for message in client.iter_messages(peer, limit=limit):
                    collected.append(_message_dict(message))
                collected.sort(key=lambda m: m["message_id"])
            return collected
        except RPCError as exc:
            raise GatewayError(f"Fetch failed for chat {chat['chat_id']}: {exc}") from exc
        finally:
            await client.disconnect()

    async def send_message(
        self, chat: dict, text: str, reply_to_id: int | None = None
    ) -> int:
        client = await self._connected()
        try:
            await self._require_auth(client)
            peer = self._input_peer(chat)
            sent = await client.send_message(peer, text, reply_to=reply_to_id)
            return sent.id
        except RPCError as exc:
            raise GatewayError(f"Send failed: {exc}") from exc
        finally:
            await client.disconnect()

    async def _require_auth(self, client: TelegramClient) -> None:
        if not await client.is_user_authorized():
            raise GatewayError("Not logged in. Run `tgvault login` first.")

    def _input_peer(self, chat: dict) -> types.TypeInputPeer:
        entity_type = chat["entity_type"]
        chat_id = chat["chat_id"]
        access_hash = chat.get("access_hash")
        if entity_type == "user":
            if access_hash is None:
                raise GatewayError(f"Chat {chat_id} is missing an access hash.")
            return types.InputPeerUser(chat_id, access_hash)
        if entity_type == "channel":
            if access_hash is None:
                raise GatewayError(f"Chat {chat_id} is missing an access hash.")
            return types.InputPeerChannel(chat_id, access_hash)
        if entity_type == "chat":
            return types.InputPeerChat(chat_id)
        raise GatewayError(f"Unsupported entity type: {entity_type}")

"""Raven (Frappe chat) platform adapter.

Hermes signs in to a Frappe site as a normal Raven user with an API key and secret. Inbound works
like Telegram's getUpdates: poll Raven's own per-channel unread watermark, fetch the new messages,
then move the watermark forward with track_visit. This needs no public URL and no setup on the
site, and Raven's permission rules decide what Hermes can read.

Env: RAVEN_URL, RAVEN_API_KEY, RAVEN_API_SECRET (required); RAVEN_ALLOWED_USERS,
RAVEN_ALLOW_ALL_USERS, RAVEN_REQUIRE_MENTION (default true), RAVEN_POLL_INTERVAL (default 2),
RAVEN_HOME_CHANNEL (channel ID or user email).
"""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import httpx
import markdown

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    env_is_connected, extra_or_secret, get_scoped_secret, seed_extra_from_env, send_error,
)
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult, cache_document_from_bytes_async, cache_image_from_bytes_async,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, cancel_task, convert_table_to_bullets, send_chunks

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 10_000
DEFAULT_POLL_INTERVAL = 2.0
MAX_POLL_BACKOFF = 60.0
MESSAGES_PER_CHANNEL = 50
FALSE_VALUES = {"false", "0", "no", "off"}

PLATFORM_HINT = (
    "You are chatting via Raven, the team chat app of a Frappe site. Markdown renders as rich text: "
    "bold, italics, lists, links and code blocks work. Tables do not render, so use lists instead."
)


def read_settings(config: Any) -> tuple[str, str, str]:
    extra = getattr(config, "extra", None) or {}
    url = str(extra_or_secret(extra, "url", "RAVEN_URL")).strip().rstrip("/")
    api_key = str(extra_or_secret(extra, "api_key", "RAVEN_API_KEY")).strip()
    api_secret = str(extra_or_secret(extra, "api_secret", "RAVEN_API_SECRET")).strip()
    return url, api_key, api_secret


class RavenClient:
    """The few Raven and Frappe REST calls the adapter needs, signed in as one user."""

    def __init__(self, url: str, api_key: str, api_secret: str):
        self.http = httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"token {api_key}:{api_secret}"}, timeout=30)

    async def close(self) -> None:
        await self.http.aclose()

    async def get(self, method: str) -> Any:
        response = await self.http.get(f"/api/method/{method}")
        response.raise_for_status()
        return response.json().get("message")

    async def post(self, method: str, **params: Any) -> Any:
        response = await self.http.post(f"/api/method/{method}", json=params)
        response.raise_for_status()
        return response.json().get("message")

    async def get_doc(self, doctype: str, name: str) -> dict:
        response = await self.http.get(f"/api/resource/{doctype}/{name}")
        response.raise_for_status()
        return response.json()["data"]

    async def download(self, file_url: str) -> bytes:
        response = await self.http.get(file_url)
        response.raise_for_status()
        return response.content

    async def resolve_channel(self, target: str) -> str:
        """A user email becomes their DM channel with Hermes; anything else is a channel ID."""
        if "@" in target:
            return await self.post("raven.api.raven_channel.create_direct_message_channel", user_id=target)
        return target

    async def send_message(self, channel_id: str, html: str, reply_to: Optional[str] = None) -> dict:
        return await self.post(
            "raven.api.raven_message.send_message", channel_id=channel_id, text=html,
            is_reply=bool(reply_to), linked_message=reply_to)

    async def send_file(self, channel_id: str, path: str, caption_html: str = "",
                        reply_to: Optional[str] = None, file_name: Optional[str] = None) -> list[dict]:
        data = await asyncio.to_thread(Path(path).read_bytes)
        response = await self.http.post(
            "/api/method/upload_file", data={"is_private": "1"}, files={"file": (file_name or Path(path).name, data)})
        response.raise_for_status()
        uploaded = response.json()["message"]
        return await self.post(
            "raven.api.raven_message.send_message_with_attachments", channel_id=channel_id, content=caption_html,
            files=[{"file_url": uploaded["file_url"], "file_size": uploaded["file_size"]}],
            is_reply=bool(reply_to), linked_message=reply_to)


class TextExtractor(HTMLParser):
    """Raven stores messages as Tiptap HTML; the agent reads plain text with line breaks kept."""

    BLOCK_TAGS = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "pre":
            self.in_pre = True
            self.parts.append("\n```\n")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self.in_pre = False
            self.parts.append("\n```\n")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = TextExtractor()
    extractor.feed(html)
    extractor.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(extractor.parts)).strip()


def markdown_to_html(text: str) -> str:
    return markdown.markdown(convert_table_to_bullets(text), extensions=["fenced_code", "nl2br", "sane_lists"])


def remove_mention(html: str, user_id: str) -> str:
    return re.sub(rf'<span[^>]*\bdata-id="{re.escape(user_id)}"[^>]*>.*?</span>', "", html, flags=re.DOTALL)


def is_mentioned(html: str, user_id: str) -> bool:
    return re.search(rf'data-id="{re.escape(user_id)}"', html) is not None


class RavenAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("raven"))
        self.url, self.api_key, self.api_secret = read_settings(config)
        self.require_mention = str(extra_or_secret(
            config.extra, "require_mention", "RAVEN_REQUIRE_MENTION", "true", blank_is_unset=False,
        )).strip().lower() not in FALSE_VALUES
        self.poll_interval = float(extra_or_secret(
            config.extra, "poll_interval", "RAVEN_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
        self.raven: Optional[RavenClient] = None
        self.me = ""
        self.poll_task: Optional[asyncio.Task] = None
        self.dedup = MessageDeduplicator(ttl_seconds=600)
        self.channel_cache: dict[str, dict] = {}
        self.user_name_cache: dict[str, str] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.raven = RavenClient(self.url, self.api_key, self.api_secret)
        try:
            self.me = await self.raven.get("frappe.auth.get_logged_user")
        except httpx.HTTPError as error:
            await self.raven.close()
            message = f"[raven] cannot sign in to {self.url}: {error}"
            logger.error(message)
            self._set_fatal_error("raven_sign_in_failed", message, retryable=isinstance(error, httpx.TransportError))
            return False
        self.poll_task = asyncio.create_task(self.poll_loop())
        self._wire_plugin_handlers(None)
        self._mark_connected()
        logger.info("[raven] signed in to %s as %s", self.url, self.me)
        return True

    async def disconnect(self) -> None:
        await cancel_task(self.poll_task)
        self.poll_task = None
        if self.raven:
            await self.raven.close()
            self.raven = None
        self._mark_disconnected()

    async def poll_loop(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
            except Exception as error:
                failures += 1
                logger.warning("[raven] poll failed (%d in a row): %s", failures, error)
            await asyncio.sleep(min(self.poll_interval * 2 ** failures, MAX_POLL_BACKOFF))

    async def poll_once(self) -> None:
        unread_channels = await self.raven.get("raven.api.raven_message.get_unread_count_for_channels")
        for channel in unread_channels or []:
            await self.read_channel(channel["name"], bool(channel["is_direct_message"]))

    async def read_channel(self, channel_id: str, is_dm: bool) -> None:
        page = await self.raven.post(
            "raven.api.chat_stream.get_messages", channel_id=channel_id, limit=MESSAGES_PER_CHANNEL,
            update_last_visit=False)
        messages = page.get("messages") or []
        if not messages:
            return
        last_visit = str(page.get("last_visit") or "")
        for message in reversed(messages):
            if (str(message["creation"]) > last_visit and message["owner"] != self.me
                    and message["message_type"] != "System"):
                await self.dispatch(message, channel_id, is_dm)
        await self.raven.post(
            "raven.api.raven_channel_member.track_visit", channel_id=channel_id,
            last_visit=str(messages[0]["creation"]))

    async def dispatch(self, message: dict, channel_id: str, is_dm: bool) -> None:
        if self.dedup.is_duplicate(message["name"]):
            return
        html = message.get("text") or ""
        if not is_dm and self.require_mention and not is_mentioned(html, self.me):
            return
        text = html_to_text(remove_mention(html, self.me))
        media_urls, media_types = await self.download_file(message)
        if not text and not media_urls:
            return
        owner = message["owner"]
        user_name = await self.user_name(owner)
        chat_name = user_name if is_dm else (await self.channel_info(channel_id)).get("channel_name")
        source = self.build_source(
            chat_id=channel_id, chat_name=chat_name, chat_type="dm" if is_dm else "group",
            user_id=owner, user_name=user_name, message_id=message["name"])
        await self.handle_message(MessageEvent(
            text=text, message_type=message_type(text, message), source=source, raw_message=message,
            message_id=message["name"], media_urls=media_urls, media_types=media_types,
            reply_to_message_id=message.get("linked_message")))

    async def download_file(self, message: dict) -> tuple[list[str], list[str]]:
        file_url = message.get("file")
        if not file_url:
            return [], []
        try:
            data = await self.raven.download(file_url)
        except httpx.HTTPError as error:
            logger.warning("[raven] cannot download %s: %s", file_url, error)
            return [], []
        file_name = PurePosixPath(file_url).name
        if message["message_type"] == "Image":
            return [await cache_image_from_bytes_async(data, PurePosixPath(file_name).suffix or ".png")], ["image/*"]
        return [await cache_document_from_bytes_async(data, file_name)], ["application/octet-stream"]

    async def channel_info(self, channel_id: str) -> dict:
        if channel_id not in self.channel_cache:
            try:
                self.channel_cache[channel_id] = await self.raven.get_doc("Raven Channel", channel_id)
            except httpx.HTTPError:
                return {}
        return self.channel_cache[channel_id]

    async def user_name(self, user_id: str) -> str:
        if user_id not in self.user_name_cache:
            try:
                self.user_name_cache[user_id] = (await self.raven.get_doc("Raven User", user_id))["full_name"]
            except (httpx.HTTPError, KeyError):
                return user_id
        return self.user_name_cache[user_id]

    async def resolve_target(self, chat_id: str, reply_to: Optional[str]) -> tuple[str, Optional[str]]:
        channel_id = await self.raven.resolve_channel(chat_id)
        # A quoted reply helps in a busy channel; in a DM it is noise.
        if (await self.channel_info(channel_id)).get("is_direct_message", 1):
            reply_to = None
        return channel_id, reply_to

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        try:
            channel_id, reply_to = await self.resolve_target(chat_id, reply_to)
        except httpx.HTTPError as error:
            return SendResult(success=False, error=f"Raven: {error}")

        async def send_one(chunk: str) -> SendResult:
            nonlocal reply_to
            try:
                sent = await self.raven.send_message(channel_id, self.format_message(chunk), reply_to)
            except httpx.HTTPError as error:
                logger.error("[raven] send to %s failed: %s", channel_id, error)
                return SendResult(success=False, error=f"Raven: {error}")
            reply_to = None
            return SendResult(success=True, message_id=sent["name"])

        return await send_chunks(self.truncate_message(content, MAX_MESSAGE_LENGTH), send_one)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self.send_attachment(chat_id, image_path, caption, reply_to)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None, **kwargs) -> SendResult:
        return await self.send_attachment(chat_id, file_path, caption, reply_to, file_name)

    async def send_attachment(self, chat_id: str, path: str, caption: Optional[str], reply_to: Optional[str],
                              file_name: Optional[str] = None) -> SendResult:
        try:
            channel_id, reply_to = await self.resolve_target(chat_id, reply_to)
            sent = await self.raven.send_file(
                channel_id, path, markdown_to_html(caption) if caption else "", reply_to, file_name)
        except (httpx.HTTPError, OSError) as error:
            logger.error("[raven] sending %s to %s failed: %s", path, chat_id, error)
            return SendResult(success=False, error=f"Raven: {error}")
        return SendResult(success=True, message_id=sent[-1]["name"])

    def format_message(self, content: str) -> str:
        return markdown_to_html(content)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        channel = await self.channel_info(chat_id)
        return {
            "chat_id": chat_id, "name": channel.get("channel_name", chat_id),
            "type": "dm" if channel.get("is_direct_message") else "group",
        }


def message_type(text: str, message: dict) -> MessageType:
    if text.startswith("/"):
        return MessageType.COMMAND
    return {"Image": MessageType.PHOTO, "File": MessageType.DOCUMENT}.get(message["message_type"], MessageType.TEXT)


def check_requirements() -> bool:
    return all(get_scoped_secret(name, "").strip() for name in ("RAVEN_URL", "RAVEN_API_KEY", "RAVEN_API_SECRET"))


def validate_config(config: Any) -> bool:
    return all(read_settings(config))


def env_enablement() -> Optional[dict]:
    if not get_scoped_secret("RAVEN_URL", "").strip():
        return None
    return seed_extra_from_env((("RAVEN_URL", "url", None),), home_env="RAVEN_HOME_CHANNEL")


def parse_target(raw: str) -> Optional[tuple[str, None]]:
    target = raw.strip()
    return (target, None) if target else None


async def standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Cron delivery from a process that has no live gateway adapter."""
    url, api_key, api_secret = read_settings(pconfig)
    if not (url and api_key and api_secret):
        return send_error("Raven is not configured (RAVEN_URL, RAVEN_API_KEY and RAVEN_API_SECRET are required)")
    raven = RavenClient(url, api_key, api_secret)
    try:
        channel_id = await raven.resolve_channel(chat_id)
        for media in media_files or []:
            await raven.send_file(channel_id, media.get("path") if isinstance(media, dict) else media)
        sent = await raven.send_message(channel_id, markdown_to_html(message))
        return {"success": True, "platform": "raven", "chat_id": channel_id, "message_id": sent["name"]}
    except (httpx.HTTPError, OSError) as error:
        return send_error(f"Raven send failed: {error}")
    finally:
        await raven.close()


def register(ctx) -> None:
    ctx.register_platform(
        name="raven", label="Raven", adapter_factory=RavenAdapter,
        check_fn=check_requirements, validate_config=validate_config,
        is_connected=env_is_connected("RAVEN_URL", "RAVEN_API_KEY", "RAVEN_API_SECRET"),
        required_env=["RAVEN_URL", "RAVEN_API_KEY", "RAVEN_API_SECRET"],
        install_hint="Set RAVEN_URL, RAVEN_API_KEY and RAVEN_API_SECRET in ~/.hermes/.env",
        env_enablement_fn=env_enablement, cron_deliver_env_var="RAVEN_HOME_CHANNEL",
        standalone_sender_fn=standalone_send, parse_target_ref_fn=parse_target,
        allowed_users_env="RAVEN_ALLOWED_USERS", allow_all_env="RAVEN_ALLOW_ALL_USERS",
        allowlist_suppresses_pairing=True, max_message_length=MAX_MESSAGE_LENGTH,
        platform_hint=PLATFORM_HINT, emoji="🐦",
    )

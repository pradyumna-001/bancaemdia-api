"""Minimal Telegram update shape and encrypted at-rest transport payloads."""

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from bancaemdia.config import get_settings

MAX_NORMALIZED_BYTES = 64 * 1024


class InvalidTelegramUpdateError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedUpdate:
    update_id: int
    event_type: str
    sender_user_id: int | None
    chat_id: int | None
    message_id: int | None
    encrypted_payload: bytes


def _fernet() -> Fernet:
    secret = get_settings().COLETA_TOKEN_SECRET.encode()
    key = hashlib.sha256(b"telegram-transport-payload:v1:" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_payload(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_NORMALIZED_BYTES:
        raise InvalidTelegramUpdateError("telegram update is too large")
    return _fernet().encrypt(raw)


def decrypt_payload(ciphertext: bytes) -> dict[str, Any]:
    try:
        value = json.loads(_fernet().decrypt(ciphertext))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise InvalidTelegramUpdateError("unreadable telegram payload") from exc
    if not isinstance(value, dict):
        raise InvalidTelegramUpdateError("invalid telegram payload")
    return value


def _positive(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_update(raw: object) -> NormalizedUpdate:
    if not isinstance(raw, dict):
        raise InvalidTelegramUpdateError("invalid telegram update")
    update_id = _positive(raw.get("update_id"))
    if update_id is None or update_id > (1 << 63) - 1:
        raise InvalidTelegramUpdateError("invalid telegram update id")
    if isinstance(raw.get("message"), dict):
        event_type = "MESSAGE"
        message = _object(raw["message"])
        sender = _object(message.get("from"))
    elif isinstance(raw.get("edited_message"), dict):
        event_type = "EDITED_MESSAGE"
        message = _object(raw["edited_message"])
        sender = _object(message.get("from"))
    elif isinstance(raw.get("callback_query"), dict):
        event_type = "CALLBACK_QUERY"
        callback = _object(raw["callback_query"])
        message = _object(callback.get("message"))
        sender = _object(callback.get("from"))
    else:
        event_type = "UNKNOWN"
        message = {}
        sender = {}
    chat = _object(message.get("chat"))
    photos = message.get("photo")
    safe_photos: list[dict[str, str | int]] = []
    if isinstance(photos, list):
        for candidate in photos[:20]:
            photo = _object(candidate)
            file_id = photo.get("file_id")
            if isinstance(file_id, str) and len(file_id) <= 512:
                item: dict[str, str | int] = {"file_id": file_id}
                unique = photo.get("file_unique_id")
                size = _positive(photo.get("file_size"))
                width = _positive(photo.get("width"))
                height = _positive(photo.get("height"))
                if isinstance(unique, str) and len(unique) <= 512:
                    item["file_unique_id"] = unique
                if size is not None:
                    item["file_size"] = size
                if width is not None:
                    item["width"] = width
                if height is not None:
                    item["height"] = height
                safe_photos.append(item)
    text_value = message.get("text")
    caption_value = message.get("caption")
    callback_data = _object(raw.get("callback_query")).get("data")
    forward_origin = _object(message.get("forward_origin"))
    forward_type = forward_origin.get("type")
    payload = {
        "chat_type": chat.get("type")
        if chat.get("type") in {"private", "group", "supergroup", "channel"}
        else "unknown",
        "text": text_value[:4096] if isinstance(text_value, str) else None,
        "caption": caption_value[:4096] if isinstance(caption_value, str) else None,
        "photo": safe_photos,
        "media_group_id": str(message.get("media_group_id"))[:128]
        if message.get("media_group_id") is not None
        else None,
        "forwarded": any(
            key in message for key in ("forward_origin", "forward_from", "forward_date")
        ),
        "forward_origin_type": forward_type
        if forward_type in {"user", "hidden_user", "chat", "channel"}
        else None,
        "forward_date": _positive(forward_origin.get("date"))
        or _positive(message.get("forward_date")),
        "callback_data": callback_data[:256] if isinstance(callback_data, str) else None,
    }
    return NormalizedUpdate(
        update_id=update_id,
        event_type=event_type,
        sender_user_id=_positive(sender.get("id")),
        chat_id=_integer(chat.get("id")),
        message_id=_positive(message.get("message_id")),
        encrypted_payload=encrypt_payload(payload),
    )

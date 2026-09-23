"""Admit one linked private-chat photo into a resumable draft."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.materializar import descricao, mercado_principal
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.integrations.telegram.media import largest_variant
from bancaemdia.services.telegram_conversation import DraftReply, new_photo_reply

SUPPORTED_FLOW = "Para criar uma aposta, envie uma única foto do bilhete em mensagem privada."


def fields_from_coupon(
    raw: dict[str, Any], *, grave: bool = False
) -> tuple[dict[str, Any], dict[str, float]]:
    coupon = ExtracaoBilhete.model_validate(raw)
    if coupon.ilegivel:
        return {}, {}
    fields: dict[str, Any] = {}
    for field, value in (
        ("casa", coupon.casa),
        ("odd", coupon.odd_total),
        ("data_aposta", coupon.quando),
        ("evento", coupon.evento),
    ):
        if value is not None:
            fields[field] = value
    if coupon.selecoes:
        fields["descricao"] = descricao(coupon.para_bilhete())
        market = mercado_principal(coupon.para_bilhete())
        if market:
            fields["mercado_bruto"] = market
    if fields:
        fields["tipo_aposta"] = coupon.tipo.upper()
    confidence = max(0.0, min(1.0, coupon.confianca))
    if grave:
        confidence = min(confidence, 0.5)
    return fields, dict.fromkeys(fields, confidence)


def candidates_from_reading(reading: dict[str, Any]) -> list[dict[str, Any]]:
    if reading.get("nao_e_aposta"):
        return []
    raw_coupons = reading.get("cupons")
    if isinstance(raw_coupons, list) and raw_coupons:
        pairs = [
            (item.get("bilhete"), item.get("grave") is True)
            for item in raw_coupons
            if isinstance(item, dict)
        ]
    else:
        pairs = [(reading.get("bilhete"), reading.get("grave") is True)]
    candidates = []
    for raw, grave in pairs:
        if isinstance(raw, dict):
            fields, confidence = fields_from_coupon(raw, grave=grave)
            if fields:
                candidates.append({"fields": fields, "confidence": confidence})
    return candidates


async def intake_photo(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    update_id: int,
    payload: dict[str, Any],
) -> DraftReply:
    if payload.get("media_group_id"):
        return DraftReply(
            "Álbuns e várias fotos de uma vez ainda não são aceitos. " + SUPPORTED_FLOW
        )
    photos = payload.get("photo")
    if not isinstance(photos, list):
        return DraftReply(SUPPORTED_FLOW)
    variant = largest_variant(photos)
    if variant is None:
        return DraftReply("Não consegui identificar a foto. " + SUPPORTED_FLOW)
    reference = {"file_id": variant.file_id}
    caption = payload.get("caption")
    if isinstance(caption, str):
        reference["caption"] = caption[:600]
    source = {
        "update_id": update_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_unique_id": variant.file_unique_id,
        "forwarded": payload.get("forwarded") is True,
        "forward_origin_type": payload.get("forward_origin_type"),
        "forward_date": payload.get("forward_date"),
    }
    return await new_photo_reply(
        session,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        media_file_id=variant.file_id,
        media_reference=reference,
        source_metadata=source,
    )

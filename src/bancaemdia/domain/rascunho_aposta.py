"""Pure validation and wording for a resumable Telegram bet draft."""

import re
from datetime import datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from bancaemdia.domain.aposta_service import (
    CAMPOS_CORRIGIVEIS,
    ApostaInvalidaError,
    campos_ausentes_criacao,
    validar_correcao,
)
from bancaemdia.domain.materializar import casa_canonica

MIN_CONFIDENCE = 0.80
MAX_REPLY_CHARS = 1024
ALIAS = {
    "casa": "casa",
    "stake": "stake_unidades",
    "valor": "stake_unidades",
    "odd": "odd",
    "conta": "conta_casa_id",
    "conta_casa_id": "conta_casa_id",
    "data": "data_aposta",
    "data_aposta": "data_aposta",
    "data_jogo": "data_jogo",
    "jogo": "data_jogo",
    "evento": "evento",
    "descricao": "descricao",
    "descrição": "descricao",
}
LABEL = {
    "casa": "casa",
    "stake_unidades": "stake em unidades",
    "odd": "odd",
    "conta_casa_id": "conta da casa",
    "data_aposta": "data da aposta",
    "data_jogo": "data do jogo",
    "evento": "evento",
    "descricao": "descrição",
    "mercado_bruto": "mercado",
    "tipo_aposta": "tipo",
    "tipster": "tipster",
    "freebet": "freebet",
}
EXAMPLE = {
    "casa": "casa=Betano",
    "stake_unidades": "stake=2",
    "odd": "odd=1,90",
    "conta_casa_id": "conta=123",
    "data_aposta": "data=23/09/2026",
}
EDITABLE = frozenset(ALIAS.values()) & CAMPOS_CORRIGIVEIS
_FIELD = re.compile(
    r"(?im)(?:^|[,;\n])\s*(casa|stake|valor|odd|conta_casa_id|conta|data_aposta|data|data_jogo|jogo|evento|descrição|descricao)\s*(?:=|:|\s)\s*"
)


class DraftInputError(ValueError):
    pass


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%d/%m/%Y")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    return parsed


def occurrence(fields: dict[str, Any]) -> datetime | None:
    return _date(fields.get("data_aposta"))


def valid_value(field: str, value: object) -> bool:
    if value is None or value == "":
        return False
    if field == "casa":
        return isinstance(value, str) and casa_canonica(value) is not None
    if field == "data_aposta":
        return _date(value) is not None
    try:
        validar_correcao({field: value}, {})
    except ApostaInvalidaError:
        return False
    return True


def missing_fields(
    fields: dict[str, Any],
    metadata: dict[str, Any],
    *,
    account_needs_choice: bool = False,
) -> list[str]:
    """Use canonical required fields and correction validation, not a bot-only rulebook."""
    absent = set(campos_ausentes_criacao(fields))
    result: list[str] = []
    for field in (*campos_ausentes_criacao(fields), "casa", "odd", "stake_unidades"):
        if field in result:
            continue
        info = metadata.get(field)
        low_confidence = (
            isinstance(info, dict)
            and info.get("source") not in {"user", "resolver"}
            and isinstance(info.get("confidence"), (int, float))
            and (not isfinite(info["confidence"]) or info["confidence"] < MIN_CONFIDENCE)
        )
        if field in absent or not valid_value(field, fields.get(field)) or low_confidence:
            result.append(field)
    if not valid_value("data_aposta", fields.get("data_aposta")):
        result.append("data_aposta")
    if account_needs_choice and "casa" not in result and "data_aposta" not in result:
        result.append("conta_casa_id")
    return result


def parse_reply(text: str) -> dict[str, Any]:
    if len(text) > MAX_REPLY_CHARS:
        raise DraftInputError("Resposta longa demais; envie somente os campos pedidos.")
    command = text.strip()
    if command.lower().startswith("/corrigir "):
        match = re.fullmatch(r"(?is)/corrigir\s+(\S+)\s+(.+)", command)
        if match is None:
            raise DraftInputError("Use /corrigir campo valor.")
        alias = match.group(1).lower()
        if alias not in ALIAS:
            raise DraftInputError("Campo não reconhecido. Use casa, stake, odd, data ou conta.")
        chunks = [(alias, match.group(2).strip())]
    else:
        matches = list(_FIELD.finditer(command))
        if not matches or command[: matches[0].start()].strip(" ,;\n"):
            raise DraftInputError("Envie casa=Betano; stake=2, ou use /corrigir campo valor.")
        chunks = [
            (
                match.group(1).lower(),
                command[
                    match.end() : matches[index + 1].start() if index + 1 < len(matches) else None
                ].strip(" ,;\n"),
            )
            for index, match in enumerate(matches)
        ]
    patch: dict[str, Any] = {}
    for alias, raw in chunks:
        field = ALIAS[alias]
        if field not in EDITABLE or field in patch or not raw:
            raise DraftInputError("Campo repetido ou vazio na resposta.")
        if field in {"odd", "stake_unidades"}:
            try:
                value: Any = float(raw.replace(",", "."))
            except ValueError as exc:
                raise DraftInputError(f"{LABEL[field]} precisa ser um número.") from exc
        elif field == "conta_casa_id":
            try:
                value = int(raw)
            except ValueError as exc:
                raise DraftInputError("Informe o número da conta da casa.") from exc
        elif field in {"data_aposta", "data_jogo"}:
            date = _date(raw)
            if date is None:
                raise DraftInputError("Use uma data como 23/09/2026 ou 2026-09-23T21:00:00.")
            value = date.isoformat()
        else:
            value = raw
        patch[field] = value
    try:
        validar_correcao(patch, {})
    except ApostaInvalidaError as exc:
        raise DraftInputError(str(exc)) from exc
    if "casa" in patch:
        canonical = casa_canonica(patch["casa"])
        if canonical is None:
            raise DraftInputError("Não reconheço essa casa de apostas; confira o nome.")
        patch["casa"] = canonical
    return patch


def summary(fields: dict[str, Any], missing: list[str], status: str) -> str:
    if status == "AWAITING_EXTRACTION":
        return "Foto recebida. A leitura está pendente; use /continuar mais tarde."
    lines = ["Rascunho da aposta:"]
    for field in (
        "casa",
        "evento",
        "descricao",
        "mercado_bruto",
        "tipo_aposta",
        "tipster",
        "odd",
        "stake_unidades",
        "data_aposta",
        "data_jogo",
        "conta_casa_id",
        "freebet",
    ):
        value = fields.get(field)
        understood = (
            value is not None and value != ""
            if field in {"mercado_bruto", "tipo_aposta", "tipster", "freebet"}
            else valid_value(field, value)
        )
        if field not in missing and understood:
            lines.append(f"• {LABEL[field]}: {value}")
    if missing:
        labels = ", ".join(LABEL[field] for field in missing)
        lines.append(f"Falta confirmar: {labels}.")
        examples = "; ".join(EXAMPLE[field] for field in missing[:2] if field in EXAMPLE)
        lines.append(f"Responda, por exemplo: {examples}. A foto já está guardada.")
    else:
        lines.append("Dados completos. Confira o resumo antes de confirmar.")
    action = "Use /confirmar para registrar, " if not missing else "Use "
    lines.append(action + "/continuar para rever, /corrigir campo valor ou /cancelar.")
    return "\n".join(lines)

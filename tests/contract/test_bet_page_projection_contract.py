from bancaemdia import main
from bancaemdia.domain.aposta_service import validar_correcao


def test_authorized_context_is_additive_nullable_and_ids_are_exact_strings() -> None:
    schemas = main.app.openapi()["components"]["schemas"]
    bet = schemas["BetResponse"]
    for field, target in (
        ("conta_contexto", "BetAccountContextResponse"),
        ("banca_contexto", "BetBankContextResponse"),
    ):
        assert field not in bet["required"]
        assert bet["properties"][field]["anyOf"] == [
            {"$ref": f"#/components/schemas/{target}"},
            {"type": "null"},
        ]
    for name in ("BetAccountContextResponse", "BetHolderContextResponse", "BetBankContextResponse"):
        assert schemas[name]["additionalProperties"] is False
        assert schemas[name]["properties"]["id"]["type"] == "string"
        assert schemas[name]["properties"]["id"]["pattern"] == "^[1-9][0-9]*$"


def test_raw_market_correction_uses_domain_allowlist_and_formal_schema() -> None:
    schemas = main.app.openapi()["components"]["schemas"]
    for name in ("Correcao", "CorrecaoDaRevisao"):
        assert schemas[name]["properties"]["mercado_bruto"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }
    validar_correcao({"mercado_bruto": "Total de gols"}, {})
    validar_correcao({"mercado_bruto": None}, {})


def test_existing_decimal_goal_text_pattern_is_preserved() -> None:
    schemas = main.app.openapi()["components"]["schemas"]
    for name in ("MetaEntrada", "MetaAlteracao"):
        for field in ("alvo", "linha_base"):
            variant = next(
                s for s in schemas[name]["properties"][field]["anyOf"] if s.get("type") == "string"
            )
            assert variant["pattern"] == r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"

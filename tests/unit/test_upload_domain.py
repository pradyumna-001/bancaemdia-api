from __future__ import annotations

import io
import json
import zipfile

import pytest

from bancaemdia.domain import upload
from bancaemdia.extracao.precos import USD_POR_BILHETE_REFERENCIA

RECADO = "(Photo not included. Change data exporting settings to download.)"
PASTA = "ChatExport_2026-07-24/"


def _mensagem(message_id, **extra):
    return {
        "id": message_id,
        "type": "message",
        "date": "2026-07-24T16:17:52",
        "from": "girafalles",
        "text": "1u",
        **extra,
    }


def _export(mensagens, nome="Canal de apostas", chat_id=1234567890):
    return {"name": nome, "type": "private_channel", "id": chat_id, "messages": mensagens}


def _zip(dados, fotos=(), prefixo=PASTA, extras=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        if dados is not None:
            arquivo.writestr(prefixo + "result.json", json.dumps(dados, ensure_ascii=False))
        for nome, conteudo in fotos:
            arquivo.writestr(prefixo + nome, conteudo)
        for nome, conteudo in extras:
            arquivo.writestr(nome, conteudo)
    buffer.seek(0)
    return buffer


def _abrir(dados, fotos=(), prefixo=PASTA, extras=(), nome="export.zip"):
    return upload.ExportTelegram(_zip(dados, fotos, prefixo, extras), nome)


def test_the_chat_metadata_comes_from_the_export() -> None:
    with _abrir(_export([_mensagem(1)])) as export:
        assert (export.nome, export.tipo, export.chat_id) == (
            "Canal de apostas",
            "private_channel",
            1234567890,
        )


def test_service_messages_are_skipped_unless_asked_for() -> None:
    dados = _export([_mensagem(1), _mensagem(2, type="service", action="create_channel")])

    with _abrir(dados) as export:
        assert [m.message_id for m in export.mensagens()] == [1]
        assert [m.message_id for m in export.mensagens(incluir_servico=True)] == [1, 2]


def test_a_message_without_a_date_is_skipped() -> None:
    dados = _export([_mensagem(1, date="ontem"), _mensagem(2), {"id": 3, "type": "message"}])

    with _abrir(dados) as export:
        assert [m.message_id for m in export.mensagens()] == [2]


def test_the_fields_of_a_message_are_extracted() -> None:
    dados = _export([
        _mensagem(
            7,
            text=["1u ", {"type": "link", "text": "https://betano.bet.br/x"}],
            edited="2026-07-25T09:41:05",
            reply_to_message_id=6,
            forwarded_from="canal vizinho",
            photo="photos/photo_7@24-07-2026_16-17-52.jpg",
            width=1080,
            height=1920,
        )
    ])

    with _abrir(dados) as export:
        mensagem = next(iter(export.mensagens()))

    assert mensagem.texto == "1u https://betano.bet.br/x"
    assert mensagem.links == ["https://betano.bet.br/x"]
    assert (mensagem.autor, mensagem.responde_a, mensagem.encaminhada_de) == (
        "girafalles",
        6,
        "canal vizinho",
    )
    assert (mensagem.largura, mensagem.altura) == (1080, 1920)
    assert mensagem.foi_editada and mensagem.tem_foto and mensagem.e_mensagem
    with pytest.raises(AttributeError):
        mensagem.texto = "outro"  # type: ignore[misc]


def test_the_author_field_of_a_channel_wins_over_from() -> None:
    dados = _export([_mensagem(1, author="tipster", **{"from": "canal"})])

    with _abrir(dados) as export:
        assert next(iter(export.mensagens())).autor == "tipster"


def test_a_photo_that_came_in_the_zip_is_found_and_read() -> None:
    nome_da_foto = "photos/photo_1@24-07-2026_16-17-52.jpg"
    dados = _export([_mensagem(1, photo=nome_da_foto)])

    with _abrir(dados, [(nome_da_foto, b"JPEG-1")]) as export:
        mensagem = next(iter(export.mensagens()))
        assert export.foto_existe(mensagem)
        assert export.bytes_da_foto(mensagem) == b"JPEG-1"
        assert export.tamanho_da_foto(mensagem) == 6


def test_a_photo_missing_from_the_zip_is_reported_as_absent() -> None:
    dados = _export([_mensagem(1, photo="photos/photo_1.jpg")])

    with _abrir(dados) as export:
        mensagem = next(iter(export.mensagens()))
        assert mensagem.tem_foto
        assert not export.foto_existe(mensagem)
        assert export.bytes_da_foto(mensagem) is None


def test_a_photo_placeholder_is_not_a_photo() -> None:
    dados = _export([_mensagem(1, photo=RECADO)])

    with _abrir(dados) as export:
        mensagem = next(iter(export.mensagens()))
        assert not export.foto_existe(mensagem)
        assert upload.bilhetes_do_export(export) == []


def test_an_empty_photo_file_counts_as_no_photo() -> None:
    nome_da_foto = "photos/photo_1.jpg"
    dados = _export([_mensagem(1, photo=nome_da_foto)])

    with _abrir(dados, [(nome_da_foto, b"")]) as export:
        mensagem = next(iter(export.mensagens()))
        assert export.bytes_da_foto(mensagem) is None
        assert upload.bilhetes_do_export(export) == []


def test_the_folder_prefix_is_used_when_reading_the_photo() -> None:
    nome_da_foto = "photos/photo_1.jpg"
    dados = _export([_mensagem(1, photo=nome_da_foto)])

    with _abrir(dados, [(nome_da_foto, b"JPEG")], prefixo="Pasta com espaço/") as export:
        assert export.bytes_da_foto(next(iter(export.mensagens()))) == b"JPEG"


def test_the_result_json_is_found_at_the_root_of_the_zip() -> None:
    with _abrir(_export([_mensagem(1)]), prefixo="") as export:
        assert export.chat_id == 1234567890


def test_any_name_ending_in_result_json_counts_as_another_export() -> None:
    # Ported as it stands: the old reader matches on the suffix, so a leftover `old_result.json`
    # also makes it refuse instead of guessing.
    dados = _export([_mensagem(1)])

    with pytest.raises(upload.ExportInvalidoError, match="Achei 2 exports"):
        _abrir(dados, extras=[(PASTA + "photos/old_result.json", "{}")])


def test_two_exports_in_one_zip_are_refused_naming_the_folders() -> None:
    dados = _export([_mensagem(1)])
    outro = [("ChatExport_2026-08-01/result.json", json.dumps({"messages": []}))]

    with pytest.raises(upload.ExportInvalidoError) as recusa:
        _abrir(dados, extras=outro)

    assert "Achei 2 exports" in str(recusa.value)
    assert "ChatExport_2026-07-24" in str(recusa.value)
    assert "ChatExport_2026-08-01" in str(recusa.value)
    assert "Telegram Desktop" in str(recusa.value)


def test_a_zip_without_a_result_json_is_refused() -> None:
    with pytest.raises(upload.ExportInvalidoError, match=r"Não achei result\.json"):
        _abrir(None, [("photos/foto.jpg", b"JPEG")])


def test_a_json_without_messages_is_refused() -> None:
    with pytest.raises(upload.ExportInvalidoError, match="chave 'messages'"):
        _abrir({"name": "x", "id": 7})


def test_the_export_of_the_whole_account_says_what_to_export_instead() -> None:
    with pytest.raises(upload.ExportInvalidoError, match="conta inteira"):
        _abrir({"about": "x", "chats": {"list": []}})


def test_an_export_with_a_chat_id_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(upload.ExportInvalidoError, match="não parece um export do Telegram"):
        _abrir({"name": "x", "type": "channel", "id": "abc", "messages": []})


def test_a_message_with_an_id_that_is_not_a_number_is_skipped() -> None:
    dados = _export([_mensagem("abc"), _mensagem(2)])

    with _abrir(dados) as export:
        assert [m.message_id for m in export.mensagens()] == [2]


def test_a_file_that_is_not_a_zip_is_refused() -> None:
    with pytest.raises(upload.ExportInvalidoError, match="não consegui abrir este zip"):
        upload.ExportTelegram(io.BytesIO(b"isto nao e um zip"), "export.zip")


def test_a_bare_result_json_is_accepted_and_has_no_photos() -> None:
    dados = _export([_mensagem(1, photo="photos/photo_1.jpg")])

    with upload.ExportTelegram(io.BytesIO(json.dumps(dados).encode()), "result.json") as export:
        assert export.chat_id == 1234567890
        assert upload.bilhetes_do_export(export) == []


def test_another_extension_is_refused() -> None:
    with pytest.raises(upload.ExportInvalidoError, match="Telegram Desktop"):
        upload.ExportTelegram(io.BytesIO(b"{}"), "export.rar")


def test_a_broken_json_is_refused_with_a_message() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as arquivo:
        arquivo.writestr(PASTA + "result.json", b"\xff\xfe nao e utf-8")
    buffer.seek(0)

    with pytest.raises(upload.ExportInvalidoError, match="não consegui ler"):
        upload.ExportTelegram(buffer, "export.zip")


def test_a_zip_with_too_many_files_is_refused() -> None:
    fotos = [(f"photos/foto_{i}.jpg", b"J") for i in range(3)]
    dados = _export([_mensagem(1)])
    arquivo = _zip(dados, fotos)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "MAXIMO_DE_ARQUIVOS", 2)
        with pytest.raises(upload.ExportInvalidoError, match="arquivos"):
            upload.ExportTelegram(arquivo, "export.zip")


def test_a_zip_that_grows_when_opened_is_refused() -> None:
    dados = _export([_mensagem(1)])
    arquivo = _zip(dados, [("photos/foto.jpg", b"0" * (2 * 1024 * 1024))])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "RAZAO_MAXIMA", 2)
        with pytest.raises(upload.ExportInvalidoError, match="não parece um export"):
            upload.ExportTelegram(arquivo, "export.zip")


def test_a_zip_that_is_too_big_uncompressed_is_refused() -> None:
    dados = _export([_mensagem(1)])
    arquivo = _zip(dados, [("photos/foto.jpg", b"0" * 4096)])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "MAXIMO_DESCOMPACTADO", 1024)
        with pytest.raises(upload.ExportInvalidoError, match="grande demais"):
            upload.ExportTelegram(arquivo, "export.zip")


def test_a_result_json_bigger_than_the_limit_is_refused() -> None:
    dados = _export([_mensagem(1)])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "MAXIMO_DO_JSON", 10)
        with pytest.raises(upload.ExportInvalidoError, match="grande demais"):
            upload.ExportTelegram(_zip(dados), "export.zip")
        with pytest.raises(upload.ExportInvalidoError, match="grande demais"):
            upload.ExportTelegram(io.BytesIO(json.dumps(dados).encode()), "result.json")


def test_a_photo_over_the_size_limit_is_not_read() -> None:
    nome_da_foto = "photos/photo_1.jpg"
    dados = _export([_mensagem(1, photo=nome_da_foto)])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "MAXIMO_POR_FOTO", 3)
        with _abrir(dados, [(nome_da_foto, b"JPEG")]) as export:
            assert export.bytes_da_foto(next(iter(export.mensagens()))) is None


def test_a_zip_protected_by_a_password_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as arquivo:
        arquivo.writestr(PASTA + "result.json", json.dumps(_export([_mensagem(1)])))
        for info in arquivo.infolist():
            info.flag_bits |= 0x1
    buffer.seek(0)

    with pytest.raises(upload.ExportInvalidoError, match="senha"):
        upload.ExportTelegram(buffer, "export.zip")


def test_the_text_of_a_message_survives_every_shape_telegram_uses() -> None:
    assert upload.reconstruir_texto("1u") == "1u"
    assert upload.reconstruir_texto(None) == ""
    assert upload.reconstruir_texto([]) == ""
    assert upload.reconstruir_texto(["1u ", {"type": "link", "text": "http://x"}]) == "1u http://x"
    assert upload.reconstruir_texto(["a", 7, {"sem": "texto"}, None]) == "a"


def test_only_the_entities_telegram_marked_as_links_come_back() -> None:
    campo = [
        "1u ",
        {"type": "link", "text": "https://betano.bet.br/x"},
        {"type": "text_link", "href": "https://kto.bet.br/y", "text": "aqui"},
        {"type": "bold", "text": "forte"},
        {"type": "link", "text": ""},
    ]

    assert upload.extrair_links(campo) == ["https://betano.bet.br/x", "https://kto.bet.br/y"]
    assert upload.extrair_links("1u https://betano.bet.br/x") == []


def test_the_house_comes_from_the_domain_of_the_link() -> None:
    assert upload.casas_por_url("1u https://www.betano.bet.br/bookingcode/ABC") == ["Betano"]
    assert upload.casas_por_url("https://app.reidopitaco.com.br/x") == ["Pitaco"]
    assert upload.casas_por_url("https://m.betnacional.bet.br/a") == ["Betnacional"]
    assert upload.casas_por_url("olha https://t.me/canal e https://form.jotform.com/1") == []
    assert upload.casas_por_url("https://desconhecida.com.br/x") == ["Desconhecida"]
    assert upload.casas_por_url("https://betano.bet.br/a https://betano.bet.br/b") == ["Betano"]


def test_the_odds_quoted_in_the_text_are_read_and_bounded() -> None:
    assert upload.odds_citadas("BetFair @1.91 0.75u") == [1.91]
    assert upload.odds_citadas("pode ir odd 3.00, pega") == [3.0]
    assert upload.odds_citadas("odds: 2,50 e @1000") == [2.5]
    assert upload.odds_citadas("@0.99 @1.0") == []
    assert upload.para_decimal("1.2.3") is None


def test_a_month_summary_print_is_not_sent_to_the_ai() -> None:
    assert upload.e_prestacao_de_contas("APOSTAS 1256 · LUCROS 2710,98 · ROI 2,49%")
    assert upload.e_prestacao_de_contas("Parcial Mensal: +27.10u")
    assert upload.e_prestacao_de_contas("roi: +3,4 %")
    assert not upload.e_prestacao_de_contas("cashout parcial na betfair")
    # `[ \t]` e não `\s`: o `6` de uma URL colava com o "Apostas" do aviso legal duas linhas
    # abaixo e um bilhete real virava comentário.
    assert not upload.e_prestacao_de_contas("1,5u id=99\n\nApostas legais: proibido para menores")


def test_the_bill_list_prices_only_the_photos_that_came() -> None:
    dados = _export([
        _mensagem(1, photo="photos/a.jpg"),
        _mensagem(2, photo="photos/b.jpg", text="ROI 2,49% no mês"),
        _mensagem(3, photo="photos/c.jpg"),
        _mensagem(4, photo=RECADO),
        _mensagem(5),
    ])
    fotos = [("photos/a.jpg", b"JPEG-a"), ("photos/b.jpg", b"JPEG-b")]

    with _abrir(dados, fotos) as export:
        bilhetes = upload.bilhetes_do_export(export)
        estimativa = upload.estimar(sum(1 for _ in export.mensagens()), bilhetes)

    assert [(b.mensagem.message_id, b.ignorado) for b in bilhetes] == [(1, False), (2, True)]
    assert estimativa.total_mensagens == 5
    assert (estimativa.bilhetes, estimativa.ignorados, estimativa.acima_do_teto) == (1, 1, 0)
    assert estimativa.custo_usd == pytest.approx(USD_POR_BILHETE_REFERENCIA)


def test_the_daily_ceiling_leaves_the_rest_for_another_day() -> None:
    dados = _export([_mensagem(i, photo=f"photos/{i}.jpg") for i in range(1, 5)])
    fotos = [(f"photos/{i}.jpg", f"JPEG-{i}".encode()) for i in range(1, 5)]

    with _abrir(dados, fotos) as export:
        bilhetes = upload.bilhetes_do_export(export)

    estimativa = upload.estimar(4, bilhetes, restante_hoje=2)

    assert (estimativa.bilhetes, estimativa.acima_do_teto) == (2, 2)
    assert estimativa.custo_usd == pytest.approx(2 * USD_POR_BILHETE_REFERENCIA)
    assert upload.estimar(4, bilhetes, restante_hoje=0).bilhetes == 0
    assert upload.estimar(4, bilhetes, restante_hoje=None).bilhetes == 4

import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openpyxl import Workbook

from bancaemdia.coleta.catalogo import (
    Observation,
    Source,
    Technical,
    canonical,
    exact_host,
    normalize_redirects,
    safe_url,
    sha256,
    source_diff,
    version,
)
from bancaemdia.coleta.catalogo_fontes import (
    fetch_official,
    parse_csv,
    parse_html,
    parse_spreadsheet,
    spreadsheet_link,
)

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def source(observations=None, **changes):
    return Source(**{
        "source_key": "test-national",
        "kind": "federal",
        "jurisdiction": "BR",
        "url": "https://www.gov.br/test",
        "consulted_at": NOW,
        "valid_until": NOW + timedelta(days=7),
        "snapshot_sha256": sha256(b"snapshot"),
        "observations": observations or [],
        **changes,
    })


@pytest.mark.parametrize(
    "value",
    [
        "*.bet.br",
        "https://bet.br",
        "bet.br:443",
        "user@bet.br",
        "bet.br/path",
        "127.0.0.1",
        "bet.local",
        "bet.internal",
        "bet..br",
        "-bet.br",
        "bet.br ",
        "bet%2ebr",
        "localhost",
    ],
)
def test_only_exact_public_hostnames(value):
    with pytest.raises(ValueError):
        exact_host(value)


def test_host_case_idna_and_final_redirect_preserve_aliases():
    assert exact_host("EXEMPLO.BET.BR.") == "exemplo.bet.br"
    final, aliases = normalize_redirects([
        "https://old.bet.br/",
        "https://login.bet.br/history",
        "https://new.bet.br/",
    ])
    assert final == "new.bet.br"
    assert aliases == ["login.bet.br", "old.bet.br"]
    assert "*.bet.br" not in aliases


@pytest.mark.parametrize(
    "chain",
    [
        [],
        ["https://a.bet.br/"] * 2,
        ["https://u:p@a.bet.br/"],
        ["https://a.bet.br/?token=secret"],
        ["http://a.bet.br/"],
    ],
)
def test_redirect_chains_refuse_loops_and_secrets(chain):
    with pytest.raises(ValueError):
        normalize_redirects(chain)


@pytest.mark.parametrize(
    "url",
    [
        "http://gov.br/",
        "https://gov.br/?token=x",
        "https://u:p@gov.br/",
        "https://127.0.0.1/",
        "https://gov.br:81/",
    ],
)
def test_no_credentials_or_private_source_urls(url):
    with pytest.raises(ValueError):
        safe_url(url)


def test_diff_separates_add_change_remove_manual_without_deleting():
    old = [
        Observation(brand="Marca", hostname="old.bet.br", situation="autorizada"),
        Observation(brand="Marca", hostname="unchanged.bet.br", situation="autorizada"),
    ]
    new = [
        Observation(brand="Marca", hostname="new.bet.br", situation="autorizada"),
        Observation(
            brand="Marca",
            hostname="unchanged.bet.br",
            situation="autorizada",
            company="Changed company",
        ),
    ]
    assert source_diff(old, source(new), ["MANUAL|access.bet.br"]) == {
        "added": ["MARCA|new.bet.br"],
        "changed": ["MARCA|unchanged.bet.br"],
        "removed_from_source": ["MARCA|old.bet.br"],
        "manual_unchanged": ["MANUAL|access.bet.br"],
    }
    assert source_diff(new, source(new), []) == {
        "added": [],
        "changed": [],
        "removed_from_source": [],
        "manual_unchanged": [],
    }


@pytest.mark.parametrize(
    "kind,situation",
    [
        ("federal", "solicitante"),
        ("manual", "autorizada"),
        ("judicial", "autorizada"),
        ("applicants", "autorizada"),
    ],
)
def test_regulatory_classes_cannot_promote_applicants_or_access(kind, situation):
    with pytest.raises(ValueError):
        source(
            [Observation(brand="Marca", hostname="marca.bet.br", situation=situation)], kind=kind
        )


def test_legal_evidence_and_old_adapters_never_imply_support():
    assert Technical().support == "nao_avaliado"
    assert Technical().rollout == "disabled"
    with pytest.raises(ValueError, match="support requires"):
        Technical(support="suportado")
    with pytest.raises(ValueError, match="rollout requires"):
        Technical(rollout="enabled")


@pytest.mark.parametrize(
    "changes",
    [
        {"consulted_at": NOW.replace(tzinfo=None)},
        {"valid_until": NOW},
        {"jurisdiction": "XX"},
        {"url": "https://applicant.example.org/list"},
    ],
)
def test_source_requires_provenance_jurisdiction_and_validity(changes):
    with pytest.raises(ValueError):
        source(**changes)


def test_canonical_encoding_is_stable_and_rejects_unbounded_types():
    assert canonical({"z": "ação", "a": [1, True, None]}) == canonical({
        "a": [1, True, None],
        "z": "ação",
    })
    for invalid in (1.1, 2**53, {"ação": "x"}, object()):
        with pytest.raises(ValueError):
            canonical(invalid)


@pytest.mark.parametrize("v", ["0.9.9", "1.0.0", "12.3.40"])
def test_client_versions_compare_numerically(v):
    assert version(v) == tuple(int(p) for p in v.split("."))


@pytest.mark.parametrize("v", ["1.0", "01.0.0", "1.0.0-beta", "1.0.0+prod", "x.y.z"])
def test_ambiguous_versions_are_refused(v):
    with pytest.raises(ValueError):
        version(v)


ROWS = [["Empresa", "Marcas", "Domínio"], ["Company", "ALFA\nBETA", "alfa.bet.br\nbeta.bet.br"]]


def test_html_csv_excel_ingest_explicit_brand_host_pairs():
    raw = b"<table><tr><th>Empresa</th><th>Marcas</th><th>Dominio</th></tr><tr><td>Company</td><td><li>ALFA</li><li>BETA</li></td><td>alfa.bet.br<br>beta.bet.br</td></tr></table>"
    result = parse_html(raw, "autorizada")
    assert [o.key for o in result] == ["ALFA|alfa.bet.br", "BETA|beta.bet.br"]
    csv = "Empresa;Marcas;Domínio\nCompany;ALFA;alfa.bet.br\n".encode()
    assert parse_csv(csv, "judicial")[0].situation == "judicial"
    workbook = Workbook()
    for row in ROWS:
        workbook.active.append(row)
    output = io.BytesIO()
    workbook.save(output)
    assert parse_spreadsheet(output.getvalue(), "autorizada") == result


def test_merged_excel_cells_keep_company_but_never_guess_host():
    workbook = Workbook()
    for row in [
        ["Empresa", "Marcas", "Domínios"],
        ["Company", "ALFA", "alfa.bet.br"],
        [None, "BETA", "beta.bet.br"],
        ["Other", "FUTURA", "a definir"],
    ]:
        workbook.active.append(row)
    output = io.BytesIO()
    workbook.save(output)
    unresolved = []
    result = parse_spreadsheet(output.getvalue(), "autorizada", unresolved=unresolved)
    assert result[1].company == "Company"
    assert len(unresolved) == 1 and unresolved[0]["brand"] == "FUTURA"
    with pytest.raises(ValueError, match="unresolved"):
        parse_spreadsheet(output.getvalue(), "autorizada")


def test_empty_or_ambiguous_sources_cannot_remove_catalog():
    for raw in (
        b"captcha",
        b"<table><tr><td>Company</td></tr></table>",
        b"<table><tr><th>Empresa</th><th>Marcas</th><th>Dominio</th></tr><tr><td>Company</td><td>A<br>B</td><td>a.bet.br</td></tr></table>",
    ):
        with pytest.raises(ValueError):
            parse_html(raw, "autorizada")


def test_download_uses_official_xlsx_link_and_no_wildcard():
    assert (
        spreadsheet_link(b'<a href="/f.xlsx">Excel</a>', "https://www.gov.br/page")
        == "https://www.gov.br/f.xlsx"
    )
    with pytest.raises(ValueError):
        spreadsheet_link(
            b'<a href="https://attacker.example/f.xlsx">Excel</a>', "https://www.gov.br/page"
        )


def test_fetch_checks_every_redirect_origin_and_records_chain():
    def respond(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"snapshot")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        raw, chain = fetch_official("https://www.gov.br/start", {"www.gov.br"}, client)
    assert raw == b"snapshot" and chain == ["https://www.gov.br/start", "https://www.gov.br/final"]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "https://attacker.example/"})
        )
    ) as client:
        with pytest.raises(ValueError, match="origin"):
            fetch_official("https://www.gov.br/start", {"www.gov.br"}, client)

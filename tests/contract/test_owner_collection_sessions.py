"""Web owner projection stays distinct from installation capabilities."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bancaemdia.api.v1.coleta_pairing import OwnerCollectionSessionResponse
from bancaemdia.main import app


@pytest.mark.parametrize("selection", ["atual", "ultima_encerrada"])
def test_collection_session_boundary_matches_selection(selection):
    instant = datetime.now(UTC)
    data = {
        "sessao_id": uuid4(),
        "selecao": selection,
        "coletar_desde": instant,
        "criada_em": instant,
        "encerrada_em": None if selection == "atual" else instant,
    }
    assert OwnerCollectionSessionResponse.model_validate(data).selecao == selection
    data["encerrada_em"] = instant if selection == "atual" else None
    with pytest.raises(ValidationError):
        OwnerCollectionSessionResponse.model_validate(data)


def test_owner_session_projection_has_only_web_security_and_read_only_permission():
    operation = app.openapi()["paths"]["/api/v1/coleta/installations"]["get"]
    assert operation["security"] == [{"HTTPBearer": []}, {"SessionCookie": []}]
    assert operation["x-permission"] == "READ_ONLY"
    assert "402" not in operation["responses"]
    assert {"401", "429", "503"} <= set(operation["responses"])
    assert operation["responses"]["503"]["headers"]["Retry-After"]
    assert operation["x-session-selection"]["presence"] == "not_available"
    assert operation["x-session-selection"]["absence"] == {
        "estado_sessao": "sem_sessao",
        "sessao_coleta_is_null": True,
    }


def test_owner_session_schema_is_required_nullable_and_has_explicit_selection():
    schemas = app.openapi()["components"]["schemas"]
    installation = schemas["InstallationResponse"]
    assert {"estado_sessao", "sessao_coleta"} <= set(installation["required"])
    assert installation["properties"]["estado_sessao"]["enum"] == [
        "sem_sessao",
        "aberta",
        "encerrada",
    ]
    assert {"type": "null"} in installation["properties"]["sessao_coleta"]["anyOf"]
    session = schemas["OwnerCollectionSessionResponse"]
    assert set(session["properties"]) == {
        "sessao_id",
        "selecao",
        "coletar_desde",
        "criada_em",
        "encerrada_em",
    }
    assert session["properties"]["selecao"]["enum"] == ["atual", "ultima_encerrada"]
    assert session["properties"]["sessao_id"]["format"] == "uuid"
    assert session["additionalProperties"] is False


def test_extension_session_and_catalog_security_remain_installation_only():
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/coleta/sessions",
        "/api/v1/coleta/sessions/{sessao_id}",
        "/api/v1/coleta/jobs/{job_id}",
    ):
        for method in {"get", "post", "delete"} & paths[path].keys():
            assert paths[path][method]["security"] == [{"CollectionToken": []}]
    assert paths["/api/v1/coleta/catalogo"]["get"]["security"] == [{"InstallationToken": []}]

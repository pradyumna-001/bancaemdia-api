"""Typed web print handoff from the whole generated application schema."""

import json
from pathlib import Path

from bancaemdia.domain.web_print import PrintItemResponse, RetryPrint
from bancaemdia.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_full_schema_and_web_print_operations_and_binary_multipart() -> None:
    schema = app.openapi()
    assert schema == json.loads(
        (ROOT / "tests/contract/schemas/openapi.json").read_text(encoding="utf-8")
    )
    path = "/api/v1/prints/intencoes/{intent_id}/itens/{item_id}"
    assert {"get", "post"} <= set(schema["paths"][path])
    for name in (path, path + "/retry", "/api/v1/prints/intencoes/{intent_id}"):
        for op in schema["paths"][name].values():
            if not isinstance(op, dict):
                continue
            parameters = op.get("parameters", [])
            assert all(
                parameter["in"] == "path" and parameter["schema"]["format"] == "uuid"
                for parameter in parameters
            )
    post = schema["paths"][path]["post"]
    assert "multipart/form-data" in post["requestBody"]["content"]
    assert {"202", "400", "401", "402", "403", "409", "410", "413", "422", "429", "503"} <= set(
        post["responses"]
    )
    assert "402" not in schema["paths"][path]["get"]["responses"]
    components = schema["components"]["schemas"]
    assert "results" in components["PrintItemResponse"]["required"]
    assert "progress_percent" in components["PrintItemResponse"]["required"]
    assert all(
        "cost" not in name and "custo" not in name
        for name in components["PrintItemResponse"]["properties"]
    )


def test_synthetic_examples_match_published_models() -> None:
    examples = json.loads(
        (ROOT / "docs/contracts/examples/prints-web.json").read_text(encoding="utf-8")
    )
    assert PrintItemResponse.model_validate(examples["accepted"]).results is None
    completed = PrintItemResponse.model_validate(examples["completed"])
    assert completed.results is not None and completed.results[0].bet_key is None
    assert RetryPrint.model_validate(examples["retry"]).expected_attempt == 1

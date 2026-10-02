from uuid import uuid4

import pytest
from celery import Celery
from kombu import Queue

from bancaemdia.api.v1 import coleta, upload


@pytest.mark.parametrize("publication", ["coleta", "upload"])
def test_http_task_publication_preserves_payload_without_result_subscription(
    monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    queue_name = f"publication-{uuid4()}"
    publisher = Celery(queue_name, broker="memory://", backend="cache+memory://")
    publisher.conf.task_routes = {"*": {"queue": queue_name}}

    def unexpected_subscription(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP publication must not subscribe to unused task results")

    monkeypatch.setattr(publisher.backend, "on_task_call", unexpected_subscription)
    module = coleta if publication == "coleta" else upload
    monkeypatch.setattr(module, "celery", publisher)
    expected = (
        [{"usuario_id": 7, "coleta_id": 11}, {"usuario_id": 7, "coleta_id": 12}]
        if publication == "coleta"
        else [{"upload_id": 11, "usuario_id": 7}]
    )
    try:
        if publication == "coleta":
            coleta._enfileirar(7, [11, 12])
        else:
            upload._enfileirar(11, 7)
        with publisher.connection_for_read() as connection:
            queue = Queue(queue_name)(connection)
            for kwargs in expected:
                message = queue.get(no_ack=False)
                assert message is not None
                assert message.headers["task"] == module.TAREFA
                assert message.headers["ignore_result"] is True
                assert message.payload[1] == kwargs
                errbacks = message.payload[2]["errbacks"]
                if publication == "upload":
                    assert len(errbacks) == 1
                    assert errbacks[0]["task"] == upload.TAREFA_DE_FALHA
                    assert errbacks[0]["args"] == [11, 7]
                    assert errbacks[0]["immutable"] is True
                else:
                    assert errbacks is None
                message.ack()
            assert queue.get() is None
            queue.delete()
    finally:
        publisher.close()

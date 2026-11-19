"""Pruebas de mensajería e inventario (I2): backoff unitario y F01 contra LocalStack."""
import json  # Lee la RedrivePolicy devuelta por SQS.
import pytest  # Marcas y fixtures.
from fenix.services import sqs


class FakeSQS:
    """Cliente SQS mínimo que anota las llamadas a change_message_visibility."""

    def __init__(self):
        self.calls = []

    def change_message_visibility(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.parametrize("count, expected", [(1, 2), (2, 4), (3, 8), (5, 30), (9, 30)])
def test_retry_later_backoff(monkeypatch, count, expected):
    """Demora = min(30, 2 ** recepción) sobre el ReceiptHandle actual, sin send_message."""
    fake = FakeSQS()
    monkeypatch.setattr(sqs, "client", lambda service: fake)
    message = {"ReceiptHandle": "r-actual",
               "Attributes": {"ApproximateReceiveCount": str(count)}}
    assert sqs.retry_later("http://cola", message) == expected
    assert fake.calls == [{"QueueUrl": "http://cola", "ReceiptHandle": "r-actual",
                           "VisibilityTimeout": expected}]


@pytest.mark.integracion
def test_queues_are_idempotent():
    """F01 (colas): dos ejecuciones devuelven las mismas URLs y la DLQ queda asociada."""
    first = sqs.ensure_queues("fenix-test-f01")
    second = sqs.ensure_queues("fenix-test-f01")
    assert first == second
    attrs = sqs.client("sqs").get_queue_attributes(
        QueueUrl=first[0], AttributeNames=["RedrivePolicy", "VisibilityTimeout"])["Attributes"]
    policy = json.loads(attrs["RedrivePolicy"])
    assert int(policy["maxReceiveCount"]) == 3
    assert policy["deadLetterTargetArn"].endswith("fenix-test-f01-dlq")
    assert attrs["VisibilityTimeout"] == "45"


@pytest.mark.integracion
def test_setup_demo_is_idempotent(tmp_path, monkeypatch):
    """F01 (laboratorio): dos setup_demo no duplican instancia, SG ni bucket."""
    from fenix.services.demo import setup_demo
    monkeypatch.chdir(tmp_path)  # data/inventory.json de prueba, no el del grupo.
    first = setup_demo("grupo-test-f01")
    second = setup_demo("grupo-test-f01")
    assert first == second
    assert first["node-01"]["id"].startswith("i-")
    assert first["bucket-01"]["id"] == "fenix-grupo-test-f01-demo"

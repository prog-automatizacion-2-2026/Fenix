"""Escenarios de diagnóstico (I6): duplicados, orden del ACK y redrive real a la DLQ."""
import logging  # Logger del worker en las pruebas.
import time  # Deadline acotado para esperar la DLQ.
import uuid  # Prefijo de colas exclusivo por ejecución.
import pytest  # Fixtures y marcas.
from fenix.services import remediation, sqs, worker
from fenix.utils.errors import RetryableError
from fenix.utils.state import save

INVENTORY = {"node-01": {"type": "ec2", "id": "i-test"}}
EVENT = {"schema_version": 1, "event_id": "evt-node-001", "event_type": "instance.stopped",
         "resource_id": "node-01", "occurred_at": "2026-10-22T18:00:00Z",
         "simulate_failures": 0}


class CountingEC2:
    """Cliente EC2 falso: arranca en stopped y cuenta las llamadas a start_instances."""

    def __init__(self):
        self.state, self.starts = "stopped", 0

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}

    def start_instances(self, InstanceIds):
        self.starts += 1
        self.state = "running"


def test_duplicate_is_idempotent(tmp_path, monkeypatch):
    """Repetir un evento no repite la acción en EC2 ni el aviso.

    Returns:
        None cuando dos ejecuciones conservan el estado final esperado.
    Notes:
        Se usa el repair real con un cliente falso que cuenta llamadas; el ledger
        se relee de disco en cada handle, como ocurriría tras reiniciar el worker.
    """
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", INVENTORY)
    ec2, sent = CountingEC2(), []
    monkeypatch.setattr(remediation, "client", lambda service: ec2)
    monkeypatch.setattr(remediation, "notify", sent.append)
    remediation.handle(dict(EVENT))
    remediation.handle(dict(EVENT))
    assert ec2.starts == 1  # Una sola acción efectiva.
    assert len(sent) == 1  # Un solo aviso confirmado.
    assert ec2.state == "running"


def _run_once(monkeypatch, handler):
    """Ejecuta run(once=True) con una entrega fija y devuelve el orden de llamadas."""
    order = []
    message = {"MessageId": "m-1", "ReceiptHandle": "r-fijo",
               "Body": '{"event_id": "evt-node-001"}',
               "Attributes": {"ApproximateReceiveCount": "1"}}
    monkeypatch.setattr(worker, "receive", lambda url: [message])
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: order.append(("ack", receipt)))
    monkeypatch.setattr(worker, "retry_later",
                        lambda url, msg: order.append(("retry", msg["ReceiptHandle"])) or 2)

    def tracked(event):
        order.append(("handler", event["event_id"]))
        handler(event)
    worker.run("http://cola-falsa", tracked, logging.getLogger("test-ack"), once=True)
    return order


def test_ack_is_after_success(monkeypatch):
    """Un handler fallido jamás confirma; uno exitoso confirma después de terminar."""
    def failing(event):
        raise RetryableError("dependencia caída")
    assert _run_once(monkeypatch, failing) == [("handler", "evt-node-001"),
                                               ("retry", "r-fijo")]
    assert _run_once(monkeypatch, lambda event: None) == [("handler", "evt-node-001"),
                                                          ("ack", "r-fijo")]


@pytest.mark.integracion
def test_redrive_with_localstack():
    """Un mensaje venenoso termina en la DLQ real y el worker sigue vivo.

    Side Effects:
        Crea colas de prueba con prefijo propio; no toca las colas del grupo.
    Notes:
        Se compara el Body original leído de la DLQ; el contador aproximado no basta.
    """
    prefix = f"fenix-test-{uuid.uuid4().hex[:8]}"
    work, dead = sqs.ensure_queues(prefix)
    body = "{esto no es json"
    sqs.client("sqs").send_message(QueueUrl=work, MessageBody=body)
    logger = logging.getLogger("test-redrive")
    deadline = time.monotonic() + 120  # Espera total acotada.
    found = None
    while time.monotonic() < deadline and found is None:
        worker.run(work, lambda event: None, logger, once=True)  # No debe lanzar.
        rows = sqs.client("sqs").receive_message(
            QueueUrl=dead, MaxNumberOfMessages=1, WaitTimeSeconds=1,
            VisibilityTimeout=0).get("Messages", [])
        found = rows[0]["Body"] if rows else None
    assert found == body, "El mensaje no llegó a la DLQ dentro de 120 s"

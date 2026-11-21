"""Pruebas de integración y worker (I5): F09, F11, F12 y F15 con dobles locales."""
import logging  # Logger silencioso para el worker.
import pytest  # Fixtures.
from fenix.services import remediation, worker
from fenix.utils.errors import PermanentError, RetryableError
from fenix.utils.state import load, save

INVENTORY = {"node-01": {"type": "ec2", "id": "i-test",
                         "expected_tags": {"Owner": "grupo-test"}}}
EVENT = {"schema_version": 1, "event_id": "evt-node-001", "event_type": "instance.stopped",
         "resource_id": "node-01", "occurred_at": "2026-10-22T18:00:00Z",
         "simulate_failures": 0}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Carpeta aislada con inventario y contadores de reparación/aviso.

    Args:
        tmp_path: Carpeta temporal de pytest; data/ queda adentro.
        monkeypatch: Restaura cwd y funciones reemplazadas.
    Returns:
        Diccionario con los contadores de llamadas.
    """
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", INVENTORY)
    calls = {"repair": 0, "notify": 0}

    def fake_repair(resource, event_type):
        calls["repair"] += 1
        return "repaired"

    def fake_notify(text):
        calls["notify"] += 1
    monkeypatch.setattr(remediation, "repair", fake_repair)
    monkeypatch.setattr(remediation, "notify", fake_notify)
    return calls


def test_completed_event_is_not_repeated(lab):
    """F11: repetir un evento completo (incluso tras 'reiniciar') no repara ni avisa."""
    remediation.handle(dict(EVENT))
    remediation.handle(dict(EVENT))  # El ledger se relee de disco en cada llamada.
    assert lab == {"repair": 1, "notify": 1}
    record = load("data/events.json")["evt-node-001"]
    assert record["status"] == "completed" and record["attempts"] == 1


def test_simulated_failures_survive_restart(lab):
    """F09: con simulate_failures=2 fallan dos intentos y el tercero repara."""
    event = {**EVENT, "event_id": "evt-node-009", "simulate_failures": 2}
    for _ in range(2):
        with pytest.raises(RetryableError):
            remediation.handle(dict(event))
    assert lab["repair"] == 0  # Ninguna reparación antes de agotar las fallas.
    remediation.handle(dict(event))
    assert lab == {"repair": 1, "notify": 1}
    assert load("data/events.json")["evt-node-009"]["attempts"] == 3


def test_resume_only_pending_notification(lab, monkeypatch):
    """F12: corte después de repair_done y antes del aviso; reanudar solo avisa."""
    def broken_notify(text):
        raise RetryableError("proveedor caído")
    monkeypatch.setattr(remediation, "notify", broken_notify)
    with pytest.raises(RetryableError):
        remediation.handle(dict(EVENT))
    record = load("data/events.json")["evt-node-001"]
    assert record["repair_done"] is True and record["notified"] is False

    monkeypatch.setattr(remediation, "notify", lambda text: lab.update(notify=lab["notify"] + 1))
    remediation.handle(dict(EVENT))  # "Reinicio": nuevo intento con el mismo ledger.
    assert lab == {"repair": 1, "notify": 1}
    assert load("data/events.json")["evt-node-001"]["status"] == "completed"


def test_same_id_other_content_is_conflict(lab):
    """F15: mismo event_id con otro contenido -> conflicto y ledger intacto."""
    remediation.handle(dict(EVENT))
    before = load("data/events.json")
    with pytest.raises(PermanentError):
        remediation.handle({**EVENT, "occurred_at": "2026-11-01T00:00:00Z"})
    assert load("data/events.json") == before
    assert lab == {"repair": 1, "notify": 1}


def test_corrupt_message_does_not_stop_worker(monkeypatch):
    """F08 (parte local): JSON corrupto -> backoff, sin ACK, y el worker sigue."""
    log = []
    message = {"MessageId": "m-1", "ReceiptHandle": "r-1", "Body": "{roto",
               "Attributes": {"ApproximateReceiveCount": "1"}}
    monkeypatch.setattr(worker, "receive", lambda url: [message])
    monkeypatch.setattr(worker, "acknowledge", lambda url, receipt: log.append("ack"))
    monkeypatch.setattr(worker, "retry_later", lambda url, msg: log.append("retry") or 2)
    worker.run("http://cola-falsa", lambda event: log.append("handler"),
               logging.getLogger("test-worker"), once=True)
    assert log == ["retry"]  # Ni handler ni ACK; run volvió sin excepción.

"""Pruebas del aviso (I4/I6): stub HTTP para 429, timeout y fallo permanente (F14)."""
import pytest  # Fixtures y parametrización.
import requests  # Excepciones reales de la librería.
from fenix.services import notifications, remediation
from fenix.utils.errors import PermanentError, RetryableError
from fenix.utils.state import load, save


class FakeResponse:
    """Respuesta HTTP mínima, sin red."""

    def __init__(self, status, text="ok", payload=None, headers=None):
        self.status_code, self.text = status, text
        self._payload, self.headers = payload, headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no es JSON")
        return self._payload


@pytest.fixture
def slack(monkeypatch):
    """Configura Slack con una URL ficticia; nunca sale a Internet."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "slack")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.invalid/T000/B000/XXX")


def _stub(monkeypatch, *outcomes):
    """Reemplaza requests.post por una secuencia de respuestas o excepciones."""
    calls = []

    def fake_post(url, json, timeout):
        calls.append(timeout)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(notifications.requests, "post", fake_post)
    return calls


def test_slack_ok(slack, monkeypatch):
    """200 + 'ok' confirma el envío y usa timeout (2, 5)."""
    calls = _stub(monkeypatch, FakeResponse(200, "ok"))
    notifications.notify("prueba")
    assert calls == [(2, 5)]


@pytest.mark.parametrize("outcome", [
    FakeResponse(429, "rate_limited", headers={"Retry-After": "120"}),
    FakeResponse(503, "unavailable"),
    requests.Timeout("https://hooks.example.invalid/T000/B000/XXX"),
    requests.ConnectionError("https://hooks.example.invalid/T000/B000/XXX"),
])
def test_transient_failures(slack, monkeypatch, outcome):
    """429, 5xx, timeout y conexión son transitorios y no filtran la URL."""
    _stub(monkeypatch, outcome)
    with pytest.raises(RetryableError) as info:
        notifications.notify("prueba")
    assert "hooks.example" not in str(info.value)
    assert info.value.__cause__ is None or "hooks" not in str(info.value.__cause__)


@pytest.mark.parametrize("outcome", [FakeResponse(403, "invalid_token"),
                                     FakeResponse(200, "no_service")])
def test_permanent_failures(slack, monkeypatch, outcome):
    """4xx definitivo o cuerpo distinto de 'ok' es fallo permanente."""
    _stub(monkeypatch, outcome)
    with pytest.raises(PermanentError):
        notifications.notify("prueba")


def test_telegram_requires_ok_true(monkeypatch):
    """Telegram exige HTTP 200 y ok=true en el JSON."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:token-ficticio")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _stub(monkeypatch, FakeResponse(200, payload={"ok": False}))
    with pytest.raises(PermanentError):
        notifications.notify("prueba")


def test_notify_retry_does_not_repeat_repair(tmp_path, monkeypatch, slack):
    """F14: aviso con 429 y después éxito; la reparación se ejecuta una sola vez."""
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", {"node-01": {"type": "ec2", "id": "i-test"}})
    repairs = []
    monkeypatch.setattr(remediation, "repair",
                        lambda resource, kind: repairs.append(kind) or "repaired")
    _stub(monkeypatch, FakeResponse(429, "rate_limited"), FakeResponse(200, "ok"))
    event = {"schema_version": 1, "event_id": "evt-node-014",
             "event_type": "instance.stopped", "resource_id": "node-01",
             "occurred_at": "2026-10-22T18:00:00Z", "simulate_failures": 0}
    with pytest.raises(RetryableError):
        remediation.handle(dict(event))
    remediation.handle(dict(event))
    assert repairs == ["instance.stopped"]
    record = load("data/events.json")["evt-node-014"]
    assert record["notified"] is True and record["status"] == "completed"

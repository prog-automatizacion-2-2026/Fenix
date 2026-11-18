"""Pruebas del receptor HTTP (I3): F04, F05, F06, F07 y F13 sin LocalStack."""
import json  # Arma cuerpos exactos para firmar.
import time  # Timestamp actual para firmas válidas.
import pytest  # Fixtures y parametrización.
from botocore.exceptions import EndpointConnectionError  # Simula SQS caído.
from fastapi.testclient import TestClient  # Cliente HTTP en memoria.
from fenix.services import receiver
from fenix.utils.crypto import signature

SECRET = "clave-ficticia-solo-test"  # Fixture pública, no es un secreto real.
INVENTORY = {"node-01": {"type": "ec2", "id": "i-test"},
             "bucket-01": {"type": "s3", "id": "fenix-test-demo"}}
EVENT = {"schema_version": 1, "event_id": "evt-node-001", "event_type": "instance.stopped",
         "resource_id": "node-01", "occurred_at": "2026-10-22T18:00:00Z",
         "simulate_failures": 0}


@pytest.fixture
def api(monkeypatch):
    """Cliente del receptor con inventario y SQS reemplazados por dobles.

    Args:
        monkeypatch: Fixture de pytest que restaura todo al terminar.
    Returns:
        Tupla (cliente, lista de publicaciones realizadas).
    """
    published = []
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_PREFIX", "fenix-test")
    monkeypatch.setattr(receiver, "load", lambda path: INVENTORY)
    monkeypatch.setattr(receiver, "queue_url", lambda prefix: "http://cola-falsa")
    monkeypatch.setattr(receiver, "publish",
                        lambda url, event: published.append(event) or "msg-001")
    return TestClient(receiver.app), published


def _post(client, body: bytes, header: str | None):
    """Envía bytes crudos con la cabecera de firma indicada."""
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["X-Webhook-Signature"] = header
    return client.post("/webhook", content=body, headers=headers)


def _signed(body: bytes, ts: int | None = None) -> str:
    """Cabecera válida para los bytes dados."""
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={signature(SECRET, ts, body)}"


def test_valid_event_is_accepted(api):
    """Evento firmado y válido: 202 con event_id y message_id, una publicación."""
    client, published = api
    body = json.dumps(EVENT).encode()
    response = _post(client, body, _signed(body))
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "event_id": "evt-node-001",
                               "message_id": "msg-001"}
    assert len(published) == 1


@pytest.mark.parametrize("header", [
    None,                                             # F04: firma ausente
    "t=1,v1=" + "0" * 64,                             # F04: firma alterada
    "basura",                                         # cabecera malformada
])
def test_bad_signature_is_401(api, header):
    """F04: firma ausente, alterada o malformada -> 401 y nada publicado."""
    client, published = api
    response = _post(client, json.dumps(EVENT).encode(), header)
    assert response.status_code == 401
    assert published == []


def test_old_timestamp_is_401(api):
    """F04: firma correcta pero de hace una hora -> 401."""
    client, published = api
    body = json.dumps(EVENT).encode()
    response = _post(client, body, _signed(body, int(time.time()) - 3600))
    assert response.status_code == 401
    assert published == []


def test_same_json_other_bytes_is_401(api):
    """F05: se firma una serialización y se envía otra equivalente."""
    client, published = api
    signed_body = json.dumps(EVENT, separators=(",", ":")).encode()
    sent_body = json.dumps(EVENT, indent=2).encode()  # Mismo JSON, otros bytes.
    response = _post(client, sent_body, _signed(signed_body))
    assert response.status_code == 401
    assert published == []


def test_broken_json_is_400_and_list_is_422(api):
    """F06: JSON roto firmado -> 400; lista JSON firmada -> 422."""
    client, published = api
    broken = b'{"event_id": '
    assert _post(client, broken, _signed(broken)).status_code == 400
    listing = b"[1, 2, 3]"
    assert _post(client, listing, _signed(listing)).status_code == 422
    assert published == []


@pytest.mark.parametrize("change", [
    {"resource_id": "node-99"},       # F07: alias ajeno al inventario
    {"extra": "x"},                   # F07: clave extra
    {"simulate_failures": True},      # bool no es entero
])
def test_contract_errors_are_422(api, change):
    """F07: contrato incorrecto -> 422 y ninguna publicación."""
    client, published = api
    body = json.dumps({**EVENT, **change}).encode()
    assert _post(client, body, _signed(body)).status_code == 422
    assert published == []


def test_body_too_large_is_413(api):
    """Más de 16 KiB se rechaza antes de verificar la firma."""
    client, published = api
    body = b"{" + b" " * 17000 + b"}"
    assert _post(client, body, _signed(body)).status_code == 413
    assert published == []


def test_sqs_down_is_503(api, monkeypatch):
    """F13: SQS no confirma -> 503, sin aceptación falsa."""
    client, _ = api

    def down(url, event):
        raise EndpointConnectionError(endpoint_url="http://127.0.0.1:4566")
    monkeypatch.setattr(receiver, "publish", down)
    body = json.dumps(EVENT).encode()
    response = _post(client, body, _signed(body))
    assert response.status_code == 503
    assert "accepted" not in response.text

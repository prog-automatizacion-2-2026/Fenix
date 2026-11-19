"""Pruebas del contrato de eventos (I1): un caso válido y los rechazos de F07."""
import pytest  # Parametriza los rechazos.
from fenix.services.events import validate
from fenix.utils.errors import ValidationError

INVENTORY = {"node-01": {"type": "ec2", "id": "i-test"},
             "bucket-01": {"type": "s3", "id": "fenix-test-demo"}}
EVENT = {"schema_version": 1, "event_id": "evt-node-001", "event_type": "instance.stopped",
         "resource_id": "node-01", "occurred_at": "2026-10-22T18:00:00Z",
         "simulate_failures": 0}


def test_valid_event_returns_copy():
    """Un evento correcto se devuelve como copia, sin tocar el original."""
    result = validate(EVENT, INVENTORY)
    assert result == EVENT
    assert result is not EVENT


def test_list_is_not_an_event():
    """Una lista JSON no es un objeto: se rechaza antes de mirar campos."""
    with pytest.raises(ValidationError):
        validate([EVENT], INVENTORY)


@pytest.mark.parametrize("change", [
    {"extra": 1},                                   # clave extra
    {"schema_version": True},                       # bool no es el entero 1
    {"schema_version": 2},                          # versión desconocida
    {"event_id": "EVT-MAYUS"},                      # fuera del patrón
    {"event_type": "instance.deleted"},             # tipo desconocido
    {"event_type": ["instance.stopped"]},           # tipo con formato raro
    {"resource_id": "node-99"},                     # alias ajeno al inventario
    {"resource_id": "i-0123456789abcdef0"},         # ID crudo en lugar de alias
    {"event_type": "bucket.tags_missing"},          # tipo incompatible con node-01
    {"occurred_at": "2026-10-22T18:00:00"},         # fecha sin zona
    {"occurred_at": "2026-10-22T18:00:00-03:00"},   # offset no UTC
    {"simulate_failures": True},                    # bool como entero
    {"simulate_failures": 6},                       # fuera de rango
])
def test_contract_rejections(change):
    """F07 y variantes: cada desvío del contrato produce ValidationError."""
    with pytest.raises(ValidationError):
        validate({**EVENT, **change}, INVENTORY)


def test_missing_field_is_rejected():
    """Falta un campo obligatorio."""
    event = dict(EVENT)
    del event["occurred_at"]
    with pytest.raises(ValidationError):
        validate(event, INVENTORY)


def test_lab_metadata_is_not_an_alias():
    """La entrada auxiliar _lab del inventario nunca es un recurso válido."""
    inventory = {**INVENTORY, "_lab": {"type": "ec2"}}
    with pytest.raises(ValidationError):
        validate({**EVENT, "resource_id": "_lab"}, inventory)

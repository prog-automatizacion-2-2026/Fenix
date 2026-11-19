"""Contrato de eventos de autorreparación; integrante 1."""
from fenix.utils.errors import ValidationError
from fenix.utils.validation import identifier, utc_text

FIELDS = {"schema_version", "event_id", "event_type", "resource_id",
          "occurred_at", "simulate_failures"}  # Contrato cerrado de la consigna.
EVENT_TYPES = {"instance.stopped": "ec2", "bucket.tags_missing": "s3"}  # Tipo -> recurso.


def validate(event: dict, inventory: dict) -> dict:
    """Valida esquema cerrado y resuelve exclusivamente aliases autorizados.

    Args:
        event: JSON objeto con schema_version, event_id, event_type, resource_id,
            occurred_at y simulate_failures.
        inventory: Mapa alias -> recurso, preparado por setup-demo y revisado por el grupo.
    Returns:
        Copia validada del evento; no modifica inventory.
    Raises:
        ValidationError: Tipo, campo, timestamp o recurso no permitido.
    Notes:
        event_type solo permite instance.stopped y bucket.tags_missing.
        simulate_failures solo es de laboratorio: entero de 0 a 5, excluyendo bool.
        El conflicto por event_id repetido con otro contenido se resuelve en el
        ledger de remediation.handle, que es quien guarda el primer registro.
    """
    if type(event) is not dict:  # Una lista JSON o un texto no son un evento.
        raise ValidationError("El evento debe ser un objeto JSON")
    keys = set(event)
    if keys != FIELDS:  # Contrato cerrado: ni faltantes ni claves extra.
        missing, extra = sorted(FIELDS - keys), sorted(keys - FIELDS)
        raise ValidationError(f"Campos inválidos; faltan={missing} sobran={extra}")
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        raise ValidationError("schema_version debe ser el entero 1")  # True no es 1.
    identifier(event["event_id"])  # Mismo patrón para el ID de negocio...
    identifier(event["resource_id"])  # ...y para el alias del recurso.
    utc_text(event["occurred_at"])  # Fecha del evento con UTC explícito.
    failures = event["simulate_failures"]
    if type(failures) is not int or not 0 <= failures <= 5:  # bool queda afuera.
        raise ValidationError("simulate_failures debe ser un entero de 0 a 5")
    kind = event["event_type"]
    expected = EVENT_TYPES.get(kind) if isinstance(kind, str) else None
    if expected is None:  # Solo dos tipos de alerta conocidos.
        raise ValidationError("event_type desconocido")
    resource = inventory.get(event["resource_id"]) if isinstance(inventory, dict) else None
    if not isinstance(resource, dict):  # Solo aliases del inventario, nunca IDs libres.
        raise ValidationError("resource_id no pertenece al inventario del grupo")
    if resource.get("type") != expected:  # instance.stopped sobre un bucket no tiene sentido.
        raise ValidationError("event_type incompatible con el tipo del recurso")
    return dict(event)  # Copia superficial: los valores son escalares.

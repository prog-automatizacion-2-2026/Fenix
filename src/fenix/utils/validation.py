"""Contratos de validación a completar por el integrante 1."""
import re  # Comprueba el formato de los identificadores.
from datetime import datetime, timedelta, timezone  # Trabaja con fechas conscientes de UTC.
from fenix.utils.errors import ValidationError  # Error público del dominio.


def identifier(value: str) -> str:
    """Valida un identificador corto y devuelve su texto sin modificar.

    Args:
        value: Texto entre 3 y 32 caracteres; letras minúsculas, dígitos o guion.
    Returns:
        El identificador original válido.
    Raises:
        ValidationError: Si el tipo o el patrón no cumplen el contrato.
    Examples:
        identifier("grupo-01") devuelve "grupo-01"; "../ajeno" debe fallar.
    """
    if not isinstance(value, str):  # Un número o una lista no son identificadores.
        raise ValidationError("El identificador debe ser texto")
    if re.fullmatch(r"[a-z0-9-]{3,32}", value) is None:  # Patrón completo, sin recortes.
        raise ValidationError("Identificador fuera del patrón [a-z0-9-]{3,32}")
    return value  # Se devuelve tal cual: no se normaliza a minúsculas.


def ttl_seconds(value: str) -> int:
    """Valida el TTL didáctico empleado en la opción de entornos efímeros.

    Args:
        value: Texto con un entero entre 60 y 600 segundos inclusive.
    Returns:
        El número entero de segundos.
    Raises:
        ValidationError: Si no es entero o está fuera de los límites.
    Notes:
        La CLI traduce ValidationError a parser.error para salir con código 2.
    """
    try:  # int("1.5") y int("abc") lanzan ValueError.
        ttl = int(value)
    except (TypeError, ValueError) as error:  # Se conserva la causa original.
        raise ValidationError("El TTL debe ser un entero en segundos") from error
    if not 60 <= ttl <= 600:  # Ambos límites están incluidos.
        raise ValidationError("El TTL debe estar entre 60 y 600 segundos")
    return ttl


def utc_text(value: str) -> datetime:
    """Interpreta una fecha UTC explícita sin aceptar fechas ingenuas.

    Args:
        value: ISO 8601 terminado en Z o con offset +00:00.
    Returns:
        Fecha con zona UTC; por ejemplo 2026-10-22T18:00:00+00:00.
    Raises:
        ValidationError: Si falta zona, hay offset no nulo o formato inválido.
    """
    if not isinstance(value, str):  # fromisoformat solo acepta texto.
        raise ValidationError("La fecha debe ser texto ISO 8601")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value  # Z equivale a UTC.
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:  # Formato roto o fecha imposible.
        raise ValidationError("Fecha ISO 8601 inválida") from error
    if moment.tzinfo is None:  # Una fecha ingenua dependería del reloj local.
        raise ValidationError("La fecha debe indicar UTC explícito")
    if moment.utcoffset() != timedelta(0):  # Solo se acepta offset cero.
        raise ValidationError("La fecha debe estar en UTC (Z o +00:00)")
    return moment.astimezone(timezone.utc)  # Normaliza tzinfo a timezone.utc.

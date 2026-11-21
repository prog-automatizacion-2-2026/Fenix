"""Firma de apoyo y verificación a completar por el integrante 3."""
import hashlib  # Selecciona SHA-256 para autenticar el contenido.
import hmac  # Usa una comparación segura para las firmas.
import re  # Valida el formato estricto de la cabecera.
import time  # Comprueba la ventana temporal.
from fenix.utils.errors import ValidationError  # Rechazo de autenticación público.


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Firma exactamente timestamp, punto y cuerpo binario original.

    Args:
        secret: Secreto compartido de al menos 16 caracteres, leído del entorno.
        timestamp: Unix epoch en segundos, igual al enviado en la cabecera.
        body: Bytes exactos enviados por HTTP; no volver a serializar el JSON.
    Returns:
        HMAC-SHA256 hexadecimal de 64 caracteres.
    Raises:
        ValidationError: Si el secreto es demasiado corto.
    """
    if len(secret) < 16:  # Se comprueba también al enviar la prueba.
        raise ValidationError("Configurar WEBHOOK_SECRET de 16 caracteres o más")
    signed = str(timestamp).encode("ascii") + b"." + body  # Prefijo autenticado.
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, header: str, body: bytes, now: int | None = None) -> None:
    """Rechaza cabeceras ambiguas y firmas o fechas inválidas.

    Args:
        secret: Secreto compartido del entorno, nunca del payload.
        header: Cabecera exacta t=<epoch>,v1=<hex> sin claves duplicadas.
        body: Bytes del cuerpo antes de llamar a json.loads.
        now: Reloj inyectable para pruebas; None usa time.time.
    Returns:
        None si las comprobaciones son satisfactorias.
    Raises:
        ValidationError: Formato inválido, firma incorrecta o diferencia mayor a 300 s.
    Notes:
        La ventana de tiempo mitiga replay antiguo. No impide repetir un evento
        válido dentro de la ventana; event_id e idempotencia cubren ese caso.
    """
    if not isinstance(header, str) or not header:
        raise ValidationError("Firma ausente")
    fields = {}
    for part in header.split(","):  # Formato exacto: t=<epoch>,v1=<hex>.
        key, sep, value = part.partition("=")
        if not sep or key not in ("t", "v1") or key in fields:  # Sin claves extra ni repetidas.
            raise ValidationError("Cabecera de firma malformada")
        fields[key] = value
    if set(fields) != {"t", "v1"}:
        raise ValidationError("Cabecera de firma incompleta")
    if not re.fullmatch(r"[0-9]{1,12}", fields["t"]):  # Solo dígitos: sin signo ni espacios.
        raise ValidationError("Timestamp de firma inválido")
    if not re.fullmatch(r"[0-9a-f]{64}", fields["v1"]):  # SHA-256 en hex minúscula.
        raise ValidationError("Firma con formato inválido")
    timestamp = int(fields["t"])
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > 300:  # Ventana de 5 minutos hacia atrás o adelante.
        raise ValidationError("Firma fuera de la ventana de 300 segundos")
    expected = signature(secret, timestamp, body)  # Mismos bytes que llegaron por HTTP.
    if not hmac.compare_digest(expected, fields["v1"]):  # Comparación en tiempo constante.
        raise ValidationError("Firma incorrecta")

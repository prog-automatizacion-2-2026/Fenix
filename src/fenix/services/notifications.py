"""Adaptador HTTP guiado. Elegir Telegram O Slack, no ambos obligatoriamente."""
import os  # Recupera credenciales fuera del repositorio.
import requests  # Aplica timeout explícito a cada llamada HTTP.
from fenix.utils.errors import PermanentError, RetryableError


def notify(text: str) -> None:
    """Envía un mensaje breve y valida la respuesta del proveedor seleccionado.

    Args:
        text: Resumen público de hasta 1000 caracteres, sin secretos ni raw body.
    Returns:
        None únicamente después de confirmar éxito del proveedor.
    Raises:
        RetryableError: Timeout, conexión, 429 o respuesta 5xx.
        PermanentError: Configuración inválida, 4xx definitivo o rechazo funcional.
    Side Effects:
        Publica en el chat/canal del grupo. Un timeout puede causar duplicados.
    Notes:
        El modo console sirve para desarrollo; no reemplaza la evidencia final real.
        No registrar la excepción requests original: puede contener el token en URL.
    """
    mode = os.getenv("NOTIFY_PROVIDER", "console")  # Selecciona un adaptador explícito.
    if mode == "console":  # Ensayo local sin credenciales de terceros.
        print(text[:1000])  # No imprimir eventos completos o URLs privadas.
        return  # El modo debe figurar en el informe de pruebas.
    summary = text[:1000]  # Límite de la consigna para el resumen.
    if mode == "telegram":
        token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:  # Diagnóstico de configuración, sin valores.
            raise PermanentError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        url = f"https://api.telegram.org/bot{token}/sendMessage"  # Nunca se registra.
        payload = {"chat_id": chat_id, "text": summary}  # Sin parse_mode a propósito.
    elif mode == "slack":
        url = os.getenv("SLACK_WEBHOOK_URL")
        if not url:
            raise PermanentError("Falta SLACK_WEBHOOK_URL")
        payload = {"text": summary}
    else:
        raise PermanentError("NOTIFY_PROVIDER debe ser console, telegram o slack")
    try:
        response = requests.post(url, json=payload, timeout=(2, 5))  # Conexión 2 s, lectura 5 s.
    except requests.Timeout:  # No se copia la excepción: su texto incluye la URL.
        raise RetryableError("Timeout al contactar al proveedor de avisos") from None
    except requests.ConnectionError:
        raise RetryableError("Sin conexión con el proveedor de avisos") from None
    status = response.status_code
    if status == 429 or status >= 500:  # Transitorio: lo reintenta la próxima recepción SQS.
        wait = _retry_after(response.headers.get("Retry-After"))
        raise RetryableError(f"Proveedor no disponible (HTTP {status}); reintentar en {wait} s")
    if status != 200:  # 400/401/403/404: token, chat o webhook mal configurados.
        raise PermanentError(f"Proveedor rechazó el aviso (HTTP {status}); revisar configuración")
    if mode == "telegram":
        try:
            ok = response.json().get("ok") is True  # HTTP 200 no alcanza: se exige ok=true.
        except ValueError:
            ok = False
    else:
        ok = response.text.strip() == "ok"  # Slack responde texto plano, no JSON.
    if not ok:
        raise PermanentError("El proveedor no confirmó el envío del aviso")


def _retry_after(value: str | None) -> int:
    """Interpreta Retry-After acotándolo al alcance del laboratorio.

    Args:
        value: Cabecera Retry-After en segundos, o None si no vino.
    Returns:
        Segundos sugeridos entre 1 y 30; 2 si la cabecera falta o no es entera.
    """
    try:
        seconds = int(value) if value is not None else 2
    except ValueError:  # También puede venir como fecha HTTP; no se interpreta aquí.
        seconds = 2
    return max(1, min(30, seconds))  # Más de 30 s queda documentado como limitación.

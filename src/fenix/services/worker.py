"""Plantilla central de integración: un worker, un mensaje por vez."""
import json  # Convierte Body solo dentro del control por mensaje.
import logging  # Niveles numéricos para elegir WARNING o ERROR según el fallo.
import re  # Filtra el event_id antes de copiarlo al log.
import time  # Evita un bucle intensivo si el emulador no responde.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.services.sqs import receive, acknowledge, retry_later
from fenix.utils.errors import ProjectError, RetryableError


def run(url: str, handler, logger, once: bool = False) -> None:
    """Consume mensajes y confirma únicamente resultados durables.

    Args:
        url: URL de la cola de trabajo, obtenida por get_queue_url.
        handler: Callable handler(event) que revalida y ejecuta el caso de negocio.
        logger: Logger JSON del proceso worker.
        once: Si es True, realiza una sola consulta y termina; útil para pruebas.
    Returns:
        None al terminar la consulta única o al detenerse mediante Ctrl+C.
    Raises:
        NotImplementedError: Si alguna plantilla del equipo sigue incompleta.
    Side Effects:
        Recibe mensajes, ejecuta el handler y cambia o confirma su recepción.
    Notes:
        El parseo ocurre por mensaje para que un JSON corrupto no termine el
        worker ni oculte los otros mensajes. KeyboardInterrupt sube hasta la CLI.
    """
    logger.info("worker_iniciado", extra={"action": "start", "status": "running"})
    while True:
        try:  # Fallos de transporte al consultar: no hay mensaje que confirmar.
            messages = receive(url)
        except (ClientError, BotoCoreError) as error:
            logger.error("sqs_no_responde", extra={"action": "receive",
                                                   "status": type(error).__name__})
            if once:
                return
            time.sleep(2)  # Pausa antes de volver a consultar.
            continue
        if not messages:  # Cola sin mensajes disponibles: no es un error.
            logger.debug("cola_vacia", extra={"action": "receive", "status": "empty"})
        for message in messages:
            _process(url, message, handler, logger)
        if once:
            return


def _process(url: str, message: dict, handler, logger) -> None:
    """Ejecuta el handler para un mensaje y decide entre ACK o backoff.

    Args:
        url: URL de la cola de trabajo.
        message: Mensaje crudo de SQS con Body, ReceiptHandle y Attributes.
        handler: Caso de negocio; debe ser idempotente.
        logger: Logger JSON del worker.
    Returns:
        None. El resultado queda en SQS (borrado o con nueva visibilidad) y en el log.
    Raises:
        NotImplementedError: Se propaga para no simular un éxito.
    """
    ctx = {"message_id": message.get("MessageId"), "event_id": None, "resource_id": None,
           "attempt": int(message.get("Attributes", {}).get("ApproximateReceiveCount", 0))}
    try:
        event = json.loads(message["Body"])
        if type(event) is not dict:  # Una lista o un número no son un evento.
            raise ValueError("El cuerpo no es un objeto JSON")
        ctx["event_id"] = _public_id(event.get("event_id"))
        ctx["resource_id"] = _public_id(event.get("resource_id"))
        logger.info("procesando", extra={**ctx, "action": "handle", "status": "started"})
        handler(event)  # Revalida, repara, avisa y persiste cada etapa.
    except (ProjectError, ValueError, ClientError, BotoCoreError) as error:
        level = logging.WARNING if isinstance(error, RetryableError) else logging.ERROR
        logger.log(level, "fallo_procesamiento",
                   extra={**ctx, "action": "handle", "status": type(error).__name__})
        _schedule_retry(url, message, logger, ctx)  # Sin ACK: lo conserva SQS.
        return
    except OSError as error:  # Disco o permisos de data/: el estado local no es confiable.
        logger.critical("estado_local_inaccesible",
                        extra={**ctx, "action": "handle", "status": type(error).__name__})
        _schedule_retry(url, message, logger, ctx)
        return
    try:  # ACK solo después de que el handler terminó y guardó sus etapas.
        acknowledge(url, message["ReceiptHandle"])
    except (ClientError, BotoCoreError) as error:  # Volverá a llegar; el ledger lo cubre.
        logger.error("ack_fallido", extra={**ctx, "action": "ack",
                                           "status": type(error).__name__})
        return
    logger.info("confirmado", extra={**ctx, "action": "ack", "status": "acknowledged"})


def _schedule_retry(url: str, message: dict, logger, ctx: dict) -> None:
    """Aplica backoff sobre la entrega actual; RedrivePolicy decide la DLQ.

    Args:
        url: URL de la cola de trabajo.
        message: Mensaje con su ReceiptHandle actual.
        logger: Logger JSON del worker.
        ctx: Contexto de correlación ya calculado.
    Returns:
        None. Si el cambio de visibilidad falla, el mensaje igual queda sin ACK.
    """
    try:
        delay = retry_later(url, message)
    except (ClientError, BotoCoreError) as error:
        logger.error("backoff_fallido", extra={**ctx, "action": "retry",
                                               "status": type(error).__name__})
        return
    logger.info(f"reintento_en_{delay}s", extra={**ctx, "action": "retry",
                                                "status": "scheduled"})


def _public_id(value) -> str | None:
    """Devuelve el identificador solo si cumple el patrón; evita loguear basura.

    Args:
        value: Valor tomado de un mensaje todavía no validado.
    Returns:
        El mismo texto si cumple [a-z0-9-]{3,32}; None en otro caso.
    """
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9-]{3,32}", value):
        return value
    return None

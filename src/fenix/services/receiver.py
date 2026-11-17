"""Receptor FastAPI: autenticación, validación y aceptación durable por SQS."""
import os  # Recupera el secreto sin incluirlo en el repositorio.
import json  # Decodifica después de verificar los bytes.
import time  # Mide la latencia observada hasta la confirmación de SQS.
from contextlib import asynccontextmanager  # Abre y cierra el logger con el servidor.
from fastapi import FastAPI, Request, HTTPException  # Interfaz HTTP del curso.
from starlette.concurrency import run_in_threadpool  # Evita bloquear el event loop.
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv  # Permite arrancar sin --env-file.
from fenix.utils.crypto import verify
from fenix.utils.errors import ValidationError
from fenix.utils.logging_json import setup
from fenix.utils.state import load
from fenix.services.events import validate
from fenix.services.sqs import queue_url, publish

load_dotenv()  # No pisa variables ya exportadas por la terminal o por Uvicorn.
log = None  # Logger del proceso receptor; se crea al arrancar Uvicorn.


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Inicia el logger JSON del receptor y lo drena al apagar el servidor.

    Args:
        _app: Aplicación FastAPI (no se usa, la exige la firma de lifespan).
    Yields:
        None mientras el servidor atiende peticiones.
    """
    global log
    log, listener = setup("fenix-receiver")  # Archivo propio de este proceso.
    try:
        yield
    finally:
        listener.stop()  # Sin return/break/continue dentro de finally.


app = FastAPI(title="Fénix receptor de alertas", lifespan=lifespan)  # Un solo worker.


def _log(level: str, message: str, **context) -> None:
    """Registra solo si el logger ya existe (en tests sin lifespan puede no estar).

    Args:
        level: Nombre del método del logger: info, warning, error.
        message: Texto público del evento.
        **context: Campos permitidos por JSONFormatter (event_id, status, etc.).
    """
    if log is not None:
        getattr(log, level)(message, extra={"action": "receive", **context})

@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> dict:
    """Recibe un evento firmado y contesta 202 solo después de encolarlo.

    Args:
        request: Petición con X-Webhook-Signature y cuerpo de hasta 16 KiB.
    Returns:
        JSON con status=accepted, event_id y message_id de SQS.
    Raises:
        HTTPException: 401 firma; 400 JSON; 413 tamaño; 422 contrato; 503 SQS.
    Side Effects:
        Publica en SQS; no repara recursos ni llama a Telegram/Slack aquí.
    Notes:
        Leer bytes completos basta para el laboratorio; un proxy limitaría antes
        el tamaño de la transferencia en un despliegue real.
    """
    raw = await request.body()  # Se conservan los bytes originales.
    if len(raw) > 16384:  # Mantiene acotados los ejemplos didácticos.
        raise HTTPException(413, "Cuerpo demasiado grande")  # No encola.
    started = time.perf_counter()
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if len(secret) < 16:  # Error del servidor, no del cliente: no se responde 401.
        _log("critical", "webhook_secret_no_configurado", status="503")
        raise HTTPException(503, "Receptor sin configurar")
    try:  # 1) Autenticar los bytes crudos antes de interpretar el JSON.
        verify(secret, request.headers.get("X-Webhook-Signature", ""), raw)
    except ValidationError:  # Mensaje genérico: no se explica qué parte falló.
        _log("warning", "firma_rechazada", status="401")
        raise HTTPException(401, "Firma inválida") from None
    try:  # 2) Decodificar; un UTF-8 roto también es un 400.
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log("warning", "json_invalido", status="400")
        raise HTTPException(400, "JSON inválido") from None
    try:  # 3) Contrato cerrado y alias del inventario del grupo.
        inventory = await run_in_threadpool(load, "data/inventory.json")
    except (OSError, json.JSONDecodeError):  # Sin inventario no se puede validar.
        _log("error", "inventario_no_disponible", status="503")
        raise HTTPException(503, "Inventario no disponible") from None
    try:
        event = validate(event, inventory)
    except ValidationError as error:  # El motivo es público: no contiene secretos.
        _log("warning", "contrato_rechazado", status="422")
        raise HTTPException(422, str(error)) from None
    try:  # 4) Publicar y esperar la confirmación del broker.
        url = await run_in_threadpool(queue_url, os.environ["QUEUE_PREFIX"])
        message_id = await run_in_threadpool(publish, url, event)
    except (ClientError, BotoCoreError, KeyError):  # SQS caído o cola inexistente.
        _log("error", "sqs_no_disponible", event_id=event["event_id"],
             resource_id=event["resource_id"], status="503")
        raise HTTPException(503, "Cola no disponible; reintentar más tarde") from None
    elapsed = round((time.perf_counter() - started) * 1000)  # Latencia observada, no prometida.
    _log("info", f"aceptado en {elapsed} ms", event_id=event["event_id"],
         message_id=message_id, resource_id=event["resource_id"], status="accepted")
    return {"status": "accepted", "event_id": event["event_id"], "message_id": message_id}

"""Acciones convergentes a completar por integrante 3 y coordinadas por 5."""
import hashlib  # Huella del contenido asociado a cada event_id.
import json  # Serialización canónica para comparar eventos.
import logging  # Usa el logger JSON que configura la CLI del worker.
import time  # Pausa corta entre consultas de estado.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.config.settings import client
from fenix.utils.state import load, save
from fenix.services.events import validate
from fenix.services.notifications import notify
from fenix.utils.errors import RetryableError, PermanentError

INVENTORY = "data/inventory.json"  # Lo genera setup-demo.
LEDGER = "data/events.json"  # Registro durable de etapas por event_id.
TRANSIENT = {"Throttling", "ThrottlingException", "RequestLimitExceeded",
             "SlowDown", "ServiceUnavailable", "InternalError", "RequestTimeout"}
log = logging.getLogger("fenix-worker")  # Mismo nombre que crea setup() en la CLI.


def repair(resource: dict, event_type: str) -> str:
    """Acerca un recurso autorizado a su estado deseado y verifica el resultado.

    Args:
        resource: Registro de inventario con type/id y etiquetas esperadas.
        event_type: instance.stopped o bucket.tags_missing ya validado.
    Returns:
        "repaired" si cambió el estado; "already_ok" si ya cumplía la política.
    Raises:
        RetryableError: La API falla temporalmente o el estado final aún no se observa.
        PermanentError: Recurso inexistente o configuración no autorizada.
    Side Effects:
        Arranca una instancia EC2 mock detenida o restaura tags de un bucket S3.
    Notes:
        No usar reboot_instances: repetir un reinicio no es convergencia.
        Un estado pending requiere otra recepción, sin esperas largas en el worker.
    """
    try:
        if resource["type"] == "ec2" and event_type == "instance.stopped":
            return _repair_instance(client("ec2"), resource["id"])
        if resource["type"] == "s3" and event_type == "bucket.tags_missing":
            return _repair_bucket(client("s3"), resource["id"], resource["expected_tags"])
    except ClientError as error:  # Se clasifica por código, no todo ClientError es igual.
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in TRANSIENT or status >= 500:
            raise RetryableError(f"API temporalmente no disponible ({code})") from error
        raise PermanentError(f"La API rechazó la reparación ({code})") from error
    except BotoCoreError as error:  # Endpoint caído o timeout de conexión.
        raise RetryableError("Sin conexión con el emulador") from error
    raise PermanentError("Combinación de recurso y evento no autorizada")


def handle(event: dict) -> None:
    """Completa reparación y notificación usando un registro durable por evento.

    Args:
        event: Objeto recibido de SQS, que se vuelve a validar aquí.
    Returns:
        None solo cuando reparación y aviso quedaron registrados como exitosos.
    Raises:
        ValidationError: Si el evento fue inyectado directamente a SQS y no es válido.
        RetryableError: Si la simulación o una dependencia temporal falla.
        PermanentError: Conflicto de event_id o recurso no autorizado.
    Side Effects:
        Actualiza recursos, data/events.json y el canal de comunicación.
    Notes:
        Un solo proceso modifica el ledger. Un timeout al notificar puede repetir
        un aviso: no existe una transacción común entre JSON, AWS y Telegram/Slack.
    """
    try:
        inventory = load(INVENTORY)
        ledger = load(LEDGER)
    except json.JSONDecodeError as error:  # Estado local corrupto: no se supone vacío.
        log.critical("estado_local_corrupto", extra={"action": "load", "status": "corrupt"})
        raise PermanentError("data/ contiene JSON corrupto; revisar antes de seguir") from error
    event = validate(event, inventory)  # Lo inyectado directo a SQS también se valida.
    event_id, alias = event["event_id"], event["resource_id"]
    ctx = {"event_id": event_id, "resource_id": alias}
    fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    record = ledger.get(event_id)
    if record is None:  # Primera vez que se ve este event_id.
        record = {"fingerprint": fingerprint, "event": event, "attempts": 0,
                  "repair_done": False, "repair_result": None,
                  "notified": False, "status": "received"}
        ledger[event_id] = record
    elif record["fingerprint"] != fingerprint:  # Mismo ID con otro contenido: conflicto.
        log.error("conflicto_event_id", extra={**ctx, "action": "ledger", "status": "conflict"})
        raise PermanentError("event_id reutilizado con otro contenido")  # Ledger intacto.
    if record["repair_done"] and record["notified"]:  # Duplicado de un evento ya cerrado.
        log.info("evento_ya_completado", extra={**ctx, "action": "ledger", "status": "duplicate"})
        return
    record["attempts"] += 1
    save(LEDGER, ledger)  # El contador sobrevive a reinicios del worker.
    ctx["attempt"] = record["attempts"]
    if record["attempts"] <= event["simulate_failures"]:  # Falla de laboratorio previa.
        record["status"] = "simulated_failure"
        save(LEDGER, ledger)
        raise RetryableError(
            f"Falla simulada {record['attempts']} de {event['simulate_failures']}")
    if not record["repair_done"]:
        log.info("reparando", extra={**ctx, "action": "repair", "status": "started"})
        result = repair(inventory[alias], event["event_type"])
        record.update(repair_done=True, repair_result=result, status="repaired")
        save(LEDGER, ledger)  # Se persiste antes de tocar otra dependencia.
        log.info("reparacion_verificada", extra={**ctx, "action": "repair", "status": result})
    else:  # Reanudación: no se vuelve a arrancar el nodo ni a escribir tags.
        log.debug("reparacion_ya_registrada",
                  extra={**ctx, "action": "repair", "status": "skipped"})
    if not record["notified"]:
        notify(f"[Fénix] {alias} ({event['event_type']}): {record['repair_result']}. "
               f"event_id={event_id}")
        record.update(notified=True, status="notified")
        save(LEDGER, ledger)
        log.info("aviso_enviado", extra={**ctx, "action": "notify", "status": "sent"})
    record["status"] = "completed"
    save(LEDGER, ledger)


def _repair_instance(ec2, instance_id: str) -> str:
    """Lleva una instancia EC2 mock a running usando start, nunca reboot.

    Args:
        ec2: Cliente EC2 del emulador.
        instance_id: InstanceId leído del inventario.
    Returns:
        "already_ok" si ya estaba running; "repaired" si se arrancó y se verificó.
    Raises:
        RetryableError: pending/stopping, o running aún no observado tras start.
        PermanentError: Instancia terminada o en un estado no reparable.
        ClientError: Errores del SDK; los clasifica repair.
    """
    state = _instance_state(ec2, instance_id)
    if state == "running":  # Estado deseado: no se repite ninguna acción.
        return "already_ok"
    if state in ("pending", "stopping"):  # En transición: lo resuelve otra recepción.
        raise RetryableError(f"Instancia en transición ({state})")
    if state != "stopped":  # terminated / shutting-down no se pueden arrancar.
        raise PermanentError(f"Instancia en estado no reparable ({state})")
    ec2.start_instances(InstanceIds=[instance_id])  # start sobre running no causa daño.
    for _ in range(5):  # Espera acotada; no se bloquea minutos dentro de la visibilidad.
        if _instance_state(ec2, instance_id) == "running":
            return "repaired"
        time.sleep(1)
    raise RetryableError("start enviado pero running todavía no se observa")


def _instance_state(ec2, instance_id: str) -> str:
    """Consulta el estado real de una instancia.

    Args:
        ec2: Cliente EC2 del emulador.
        instance_id: InstanceId del inventario.
    Returns:
        Nombre del estado, por ejemplo running o stopped.
    Raises:
        ClientError: Si la instancia no existe o falla la API.
    """
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return reservations[0]["Instances"][0]["State"]["Name"]


def _repair_bucket(s3, bucket: str, expected: dict[str, str]) -> str:
    """Restaura las etiquetas obligatorias de un bucket preservando las ajenas.

    Args:
        s3: Cliente S3 del emulador.
        bucket: Nombre del bucket del inventario.
        expected: Etiquetas obligatorias guardadas por setup-demo.
    Returns:
        "already_ok" si ya cumplía la política; "repaired" si se reescribieron.
    Raises:
        RetryableError: Si la relectura no muestra las etiquetas esperadas.
        ClientError: Errores del SDK; los clasifica repair.
    """
    current = _bucket_tags(s3, bucket)
    desired = {**current, **expected}  # Lectura y mezcla: lo ajeno se conserva.
    if desired == current:
        return "already_ok"
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
        {"Key": k, "Value": v} for k, v in desired.items()]})  # Escribe el set completo.
    if _bucket_tags(s3, bucket) != desired:  # Verificación por API, no por suposición.
        raise RetryableError("Las etiquetas del bucket no se observan restauradas")
    return "repaired"


def _bucket_tags(s3, bucket: str) -> dict[str, str]:
    """Lee las etiquetas de un bucket; solo NoSuchTagSet equivale a vacío.

    Args:
        s3: Cliente S3 del emulador.
        bucket: Nombre del bucket.
    Returns:
        Mapa clave -> valor de las etiquetas actuales.
    Raises:
        ClientError: Cualquier otro error del servicio.
    """
    try:
        tag_set = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "NoSuchTagSet":
            raise
        return {}
    return {row["Key"]: row["Value"] for row in tag_set}

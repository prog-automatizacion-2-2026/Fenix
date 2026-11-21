"""CLI de apoyo. Los comandos existen; dependen de completar los TODO del equipo."""
import argparse  # Proporciona subcomandos reproducibles.
import json  # Lee archivos de eventos de prueba.
import os  # Recupera configuración del entorno.
import time  # Obtiene epoch del emisor.
import sys  # Comunica un código de salida al sistema operativo.
from pathlib import Path  # Lee archivos locales elegidos por el operador.
import requests  # Envía un webhook de prueba con bytes firmados.
from dotenv import load_dotenv  # Prepara el entorno de todos los comandos.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.utils.logging_json import setup
from fenix.utils.crypto import signature
from fenix.utils.errors import ProjectError
from fenix.services import sqs, demo, worker, remediation


def main() -> int:
    """Ejecuta un subcomando y cierra el logging antes de devolver el resultado.

    Returns:
        0 éxito o interrupción voluntaria; 1 error registrado; argparse usa 2.
    Raises:
        SystemExit: argparse muestra ayuda o rechaza argumentos.
    Side Effects:
        Ejecuta la operación elegida; nunca confirma mensajes desde finally.
    Notes:
        Durante desarrollo, NotImplementedError se informa como plantilla pendiente.
    """
    load_dotenv()  # No se lee ninguna credencial desde argumentos CLI.
    parser = argparse.ArgumentParser(description="Fénix autorreparación local")
    sub = parser.add_subparsers(dest="command", required=True)  # Un comando por vez.
    sub.add_parser("init-queues")  # Prepara work y DLQ.
    sub.add_parser("setup-demo")  # Crea el laboratorio autorizado.
    fault = sub.add_parser("fault")  # Introduce una falla controlada.
    fault.add_argument("alias", choices=["node-01", "bucket-01"])  # Sin IDs arbitrarios.
    send = sub.add_parser("send")  # Emite el evento por HTTP.
    send.add_argument("file")  # Archivo JSON visible para la defensa.
    send.add_argument("--firma", choices=["valida", "alterada", "vencida", "ausente"],
                      default="valida")  # Casos F04 sin editar código.
    send.add_argument("--sin-validar", action="store_true")  # Permite F06 (JSON roto firmado).
    consume = sub.add_parser("worker")  # Procesa la cola local.
    consume.add_argument("--once", action="store_true")  # Una consulta para pruebas.
    args = parser.parse_args()  # Errores sintácticos salen con código 2.
    logger, listener = setup("fenix-" + args.command)  # Archivo por proceso.
    code = 0  # El resultado se devuelve fuera de finally.
    try:  # Una frontera de entrada controla errores de las dependencias.
        prefix = os.environ["QUEUE_PREFIX"]  # Exige un prefijo explícito.
        if args.command == "init-queues":  # Crea primero la DLQ.
            print(sqs.ensure_queues(prefix))  # Las URLs provienen del SDK.
        elif args.command == "setup-demo":  # Crea o recupera recursos.
            print(demo.setup_demo(os.environ["GROUP_ID"]))  # Sin secretos.
        elif args.command == "fault":  # Perturba solo el inventario del grupo.
            demo.inject_fault(args.alias)  # I6 implementa la inyección.
        elif args.command == "send":  # Firma exactamente lo que envía.
            body = Path(args.file).read_bytes()  # No reserializa después de firmar.
            if not args.sin_validar:
                json.loads(body)  # Rechaza archivos no JSON antes de enviar.
            timestamp = int(time.time())  # La firma autentica también este número.
            if args.firma == "vencida":
                timestamp -= 3600  # Una hora atrás: fuera de la ventana de 300 s.
            digest = signature(os.environ["WEBHOOK_SECRET"], timestamp, body)
            if args.firma == "alterada":
                digest = ("0" if digest[0] != "0" else "1") + digest[1:]  # Un dígito distinto.
            headers = {"Content-Type": "application/json"}
            if args.firma != "ausente":
                headers["X-Webhook-Signature"] = f"t={timestamp},v1={digest}"
            response = requests.post("http://127.0.0.1:8000/webhook", data=body,
                                     headers=headers, timeout=(2, 15))
            print(response.status_code, response.text)  # Respuesta pública del receptor.
            logger.info("webhook_enviado", extra={"action": "send",
                                                  "status": str(response.status_code)})
            if response.status_code != 202:  # No mostrar éxito ante 4xx o 5xx.
                code = 1
        elif args.command == "worker":  # Consume un mensaje por consulta.
            worker.run(sqs.queue_url(prefix), remediation.handle, logger, args.once)
    except KeyboardInterrupt:  # Detención manual esperada.
        logger.info("detenido_por_operador")  # No se captura BaseException.
    except (ProjectError, BotoCoreError, ClientError, OSError, ValueError, KeyError,
            requests.RequestException, NotImplementedError) as error:
        logger.error("fallo_controlado", extra={"status": type(error).__name__})
        print("Revisar configuración o TODO pendiente:", type(error).__name__)  # Sin URL/token.
        code = 1  # Un fallo no se presenta como ejecución exitosa.
    finally:  # Se libera el pipeline tanto en éxito como en error.
        listener.stop()  # No return, break ni continue dentro de finally.
    return code  # El código de salida refleja el resultado de la ejecución.

if __name__ == "__main__":  # Permite python -m fenix.cli.
    sys.exit(main())  # Hace visible el resultado a la terminal.

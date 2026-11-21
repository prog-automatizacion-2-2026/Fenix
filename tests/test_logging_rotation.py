"""Pruebas de observabilidad (I4): rotación gzip (F16) y los cinco niveles de log."""
import gzip  # Abre el histórico comprimido.
import json  # Parsea cada línea.
import logging  # Niveles estándar.
from fenix.utils import logging_json


def _small_logger(tmp_path, monkeypatch, max_bytes=600):
    """Logger real con rotación reducida para no generar megabytes.

    Args:
        tmp_path: Carpeta temporal de la prueba.
        monkeypatch: Para fijar cwd y reducir maxBytes.
        max_bytes: Tamaño de rotación forzado.
    Returns:
        Tupla (logger, listener).
    """
    monkeypatch.chdir(tmp_path)
    original = logging_json.RotatingFileHandler

    def small(*args, **kwargs):
        kwargs["maxBytes"] = max_bytes
        return original(*args, **kwargs)
    monkeypatch.setattr(logging_json, "RotatingFileHandler", small)
    return logging_json.setup("test-rotacion")


def test_rotation_produces_readable_gzip(tmp_path, monkeypatch):
    """F16: forzar rotación, abrir el .gz y parsear cada línea como JSON UTC."""
    logger, listener = _small_logger(tmp_path, monkeypatch)
    try:
        for i in range(30):
            logger.info("evento_de_prueba", extra={"event_id": f"evt-{i:03d}",
                                                   "action": "test", "status": "ok"})
    finally:
        listener.stop()
    archives = list((tmp_path / "logs").glob("*.gz"))
    assert archives, "No se generó ningún histórico comprimido"
    with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows
    for row in rows:
        assert row["timestamp"].endswith("+00:00")
        assert row["event_id"].startswith("evt-")
        assert "secret" not in json.dumps(row).lower()


def test_five_levels_and_null_fields(tmp_path, monkeypatch):
    """DEBUG..CRITICAL quedan registrados; los campos no aplicables son null."""
    logger, listener = _small_logger(tmp_path, monkeypatch, max_bytes=1_000_000)
    try:
        logger.debug("cola_vacia")
        logger.info("procesando", extra={"event_id": "evt-001"})
        logger.warning("reintento", extra={"attempt": 2})
        logger.error("fallo_permanente", extra={"status": "PermanentError"})
        logger.critical("estado_local_corrupto")
    finally:
        listener.stop()
    path = next((tmp_path / "logs").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["level"] for r in rows] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    assert rows[0]["event_id"] is None and rows[0]["message_id"] is None
    assert logging.getLevelName(rows[4]["level"]) == logging.CRITICAL

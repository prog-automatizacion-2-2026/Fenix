"""Laboratorio acotado de recursos. Integrante 2 prepara, integrante 6 verifica."""
import os  # Lee el nombre del proyecto para las etiquetas.
import time  # Espaciado corto entre consultas de estado.
from botocore.exceptions import ClientError  # Se inspecciona el código, no se traga.
from fenix.config.settings import client
from fenix.services.cloud_support import (create_bucket, create_group, create_instance,
                                          mock_inputs, aws_tags)
from fenix.utils.errors import PermanentError, RetryableError, ValidationError
from fenix.utils.state import load, save
from fenix.utils.validation import identifier

INVENTORY = "data/inventory.json"  # Manifiesto local; no borrar mientras haya recursos.
ACTIVE = ["pending", "running", "stopping", "stopped"]  # Estados reutilizables.


def setup_demo(group: str) -> dict:
    """Prepara una instancia mock y un bucket a partir de identificadores estables.

    Args:
        group: ID validado del grupo; determina prefijos de los recursos.
    Returns:
        Inventario con aliases node-01 y bucket-01 y sus IDs reales del emulador.
    Raises:
        ClientError: Si el emulador no soporta una operación o falla su ejecución.
        PermanentError: Si el manifiesto o un recurso pertenecen a otro grupo.
    Side Effects:
        Crea recursos de laboratorio etiquetados y data/inventory.json.
    Notes:
        Reutilizar por tags antes de crear y guardar IDs después de cada creación.
        Obtener una ImageId con describe_images; no copiar una AMI de AWS real.
    """
    group = identifier(group)  # Evita nombres de recursos arbitrarios.
    tags = {"ManagedBy": "ppa2", "Project": os.getenv("PROJECT", "fenix"),
            "GroupId": group, "Owner": group}  # Política de propiedad del grupo.
    inventory = load(INVENTORY)  # Puede venir de una ejecución anterior.
    lab = inventory.setdefault("_lab", {"group": group})  # Datos auxiliares, no aliases.
    if lab.get("group") != group:  # Un manifiesto de otro grupo no se mezcla.
        raise PermanentError("data/inventory.json pertenece a otro GROUP_ID")
    if "vpc_id" not in lab or "image_id" not in lab:  # Se descubren una sola vez.
        lab["vpc_id"], lab["image_id"] = mock_inputs()
        save(INVENTORY, inventory)
    ec2 = client("ec2")

    sg_name = f"fenix-{group}-sg"
    found = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [sg_name]},
        {"Name": "vpc-id", "Values": [lab["vpc_id"]]},
        {"Name": "tag:GroupId", "Values": [group]}])["SecurityGroups"]
    sg_id = found[0]["GroupId"] if found else create_group(sg_name, lab["vpc_id"], tags)
    node = inventory.setdefault("node-01", {"type": "ec2"})
    node["security_group_id"] = sg_id
    save(INVENTORY, inventory)  # Se guarda apenas existe, antes del paso siguiente.

    node_name = f"fenix-{group}-node-01"
    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Project", "Values": [tags["Project"]]},
        {"Name": "tag:GroupId", "Values": [group]},
        {"Name": "tag:Name", "Values": [node_name]},
        {"Name": "instance-state-name", "Values": ACTIVE}])["Reservations"]
    instances = [i for r in reservations for i in r["Instances"]]
    if len(instances) > 1:  # Más de una coincidencia: no se elige al azar.
        raise PermanentError("Hay más de una instancia con las etiquetas del grupo")
    if instances:
        instance_id = instances[0]["InstanceId"]
    else:  # El token estable recupera una creación interrumpida.
        instance_id = create_instance(lab["image_id"], sg_id, node_name,
                                      {**tags, "Name": node_name})
    node.update(id=instance_id, expected_tags=tags)
    save(INVENTORY, inventory)

    bucket = f"fenix-{group}-demo"
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=bucket)
        exists = True
    except ClientError as error:  # Solo 404 significa que no existe.
        if error.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        exists = False
    if not exists:
        create_bucket(bucket, tags)
    else:  # Existe: se verifica identidad antes de adoptarlo.
        current = _bucket_tags(s3, bucket)
        if current.get("GroupId", group) != group:
            raise PermanentError("El bucket existe pero pertenece a otro grupo")
        if any(current.get(k) != v for k, v in tags.items()):  # Creación parcial previa.
            s3.put_bucket_tagging(Bucket=bucket,
                                  Tagging={"TagSet": aws_tags({**current, **tags})})
    inventory["bucket-01"] = {"type": "s3", "id": bucket, "expected_tags": tags}
    save(INVENTORY, inventory)
    return inventory


def inject_fault(alias: str) -> None:
    """Detiene el nodo o elimina una etiqueta del bucket de laboratorio.

    Args:
        alias: node-01 o bucket-01 presente en inventory.json.
    Returns:
        None cuando el fallo simulado se verifica mediante el SDK.
    Raises:
        ValidationError: Si el alias no pertenece al inventario.
        RetryableError: Si el estado alterado no se observa a tiempo.
    Side Effects:
        Modifica únicamente el recurso de prueba de ese grupo.
    """
    resource = load(INVENTORY).get(alias)  # Solo aliases del manifiesto propio.
    if alias.startswith("_") or not isinstance(resource, dict):
        raise ValidationError("Alias fuera del inventario; ejecutar setup-demo")
    if resource["type"] == "ec2":
        ec2 = client("ec2")
        ec2.stop_instances(InstanceIds=[resource["id"]])
        for _ in range(10):  # Espera acotada: como máximo unos 10 segundos.
            state = ec2.describe_instances(InstanceIds=[resource["id"]])[
                "Reservations"][0]["Instances"][0]["State"]["Name"]
            if state == "stopped":
                return
            time.sleep(1)
        raise RetryableError("La instancia no llegó a stopped; volver a consultar")
    s3 = client("s3")
    current = _bucket_tags(s3, resource["id"])
    current.pop("Owner", None)  # Se quita solo Owner; el resto se conserva.
    if current:
        s3.put_bucket_tagging(Bucket=resource["id"], Tagging={"TagSet": aws_tags(current)})
    else:
        s3.delete_bucket_tagging(Bucket=resource["id"])
    if "Owner" in _bucket_tags(s3, resource["id"]):  # Se verifica por API, no se supone.
        raise RetryableError("El bucket todavía conserva Owner")


def _bucket_tags(s3, bucket: str) -> dict[str, str]:
    """Lee las etiquetas de un bucket como diccionario.

    Args:
        s3: Cliente S3 ya configurado.
        bucket: Nombre del bucket del inventario.
    Returns:
        Mapa clave -> valor; vacío solo si S3 informa NoSuchTagSet.
    Raises:
        ClientError: Cualquier otro error del servicio se propaga.
    """
    try:
        tag_set = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchTagSet":
            raise
        return {}
    return {row["Key"]: row["Value"] for row in tag_set}

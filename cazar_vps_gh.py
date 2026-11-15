#!/usr/bin/env python3
"""
cazar_vps_gh.py - Intenta capturar una VM.Standard.A1.Flex (ARM, free tier)
en us-ashburn-1 probando los 3 Availability Domains.

Version para GitHub Actions:
  - Lee las credenciales de variables de entorno (nunca de ficheros).
  - Caza durante RUN_SECONDS y termina. No hay bucle infinito: el cron
    de GitHub vuelve a lanzarlo a los 5 minutos.
  - Si ya existe la instancia, termina al instante.
  - Al capturarla, abre un issue en el repo (GitHub avisa por email).
"""

import json
import logging
import os
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone

import oci

# ---------------------------------------------------------------- configuracion
REGION = "us-ashburn-1"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2.0
MEMORY_GB = 12.0
DISPLAY_NAME = "servidor-openclaw"

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "270"))
RETRY_SECONDS = 45
NET_RETRIES = 3
NET_RETRY_DELAY = 5

RETRYABLE_CODES = {
    "InternalError",
    "OutOfCapacity",
    "LimitExceeded",
    "TooManyRequests",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        logging.error("Falta la variable de entorno %s", name)
        sys.exit(2)
    return value


# ---------------------------------------------------------------------- clientes
def build_config():
    return {
        "user": env("OCI_USER"),
        "fingerprint": env("OCI_FINGERPRINT"),
        "tenancy": env("OCI_TENANCY"),
        "region": REGION,
        "key_content": env("OCI_KEY"),
    }


def build_clients(config):
    identity = oci.identity.IdentityClient(config)
    try:
        user = identity.get_user(config["user"]).data
    except Exception as exc:
        logging.error("Credenciales rechazadas por OCI: %s", exc)
        sys.exit(3)
    logging.info("Autenticado como: %s", user.name or config["user"])
    return oci.core.ComputeClient(config), identity


# ------------------------------------------------------------------ resoluciones
def get_availability_domains(identity, compartment_id):
    ads = identity.list_availability_domains(compartment_id).data
    logging.info("ADs: %s", ", ".join(a.name.split(":")[-1] for a in ads))
    return [a.name for a in ads]


def get_ubuntu_arm_image(compute, compartment_id):
    images = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        logging.error("No hay imagen Ubuntu 24.04 para A1.Flex en esta region")
        sys.exit(4)
    logging.info("Imagen: %s", images[0].display_name)
    return images[0].id


def instancia_existente(compute, compartment_id):
    """Si ya hay una instancia con nuestro nombre, no seguimos cazando."""
    try:
        instancias = oci.pagination.list_call_get_all_results(
            compute.list_instances, compartment_id=compartment_id
        ).data
    except Exception as exc:
        logging.warning("No pude listar instancias: %s", exc)
        return None

    for inst in instancias:
        if inst.display_name == DISPLAY_NAME and inst.lifecycle_state not in (
            "TERMINATED",
            "TERMINATING",
        ):
            return inst
    return None


# ------------------------------------------------------------------ lanzamiento
def launch_details(compartment_id, image_id, subnet_id, ssh_pub):
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_GB
        ),
        display_name=DISPLAY_NAME,
        image_id=image_id,
        metadata={"ssh_authorized_keys": ssh_pub},
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=50
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
    )


def is_retryable(exc):
    try:
        code = exc.code or ""
        msg = (exc.message or "").lower()
    except Exception:
        return True

    if code in RETRYABLE_CODES:
        return True
    if "capacity" in msg or "out of host" in msg:
        return True
    if getattr(exc, "status", None) in (429, 500, 502, 503, 504):
        return True
    return False


def wait_for_public_ip(compute, instance_id, tries=20, delay=10):
    network = oci.core.VirtualNetworkClient(compute.base_client.config)

    for _ in range(tries):
        try:
            inst = compute.get_instance(instance_id).data
            for att in compute.list_vnic_attachments(
                compartment_id=inst.compartment_id, instance_id=instance_id
            ).data:
                vnic = network.get_vnic(att.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
        except Exception:
            pass
        time.sleep(delay)

    return None


# ------------------------------------------------------------------ aviso
def notificar(titulo, cuerpo):
    """Abre un issue en el repo. GitHub manda email al propietario."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        logging.info("Sin GITHUB_TOKEN/GITHUB_REPOSITORY: no creo issue")
        return

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": titulo, "body": cuerpo}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cazar-vps",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            url = json.loads(resp.read().decode("utf-8")).get("html_url")
            logging.info("Issue creado: %s", url)
    except Exception as exc:
        logging.warning("No pude crear el issue: %s", exc)


# ---------------------------------------------------------------------- main
def main():
    started = time.time()
    deadline = started + RUN_SECONDS

    config = build_config()
    compute, identity = build_clients(config)

    compartment_id = os.environ.get("OCI_COMPARTMENT_ID", "").strip() or config["tenancy"]
    subnet_id = env("OCI_SUBNET_ID")
    ssh_pub = env("SSH_PUB_KEY")

    ya = instancia_existente(compute, compartment_id)
    if ya:
        logging.info("Ya existe la instancia (%s, %s). Nada que hacer.",
                     ya.id, ya.lifecycle_state)
        return 0

    ads = get_availability_domains(identity, compartment_id)
    image_id = get_ubuntu_arm_image(compute, compartment_id)
    details = launch_details(compartment_id, image_id, subnet_id, ssh_pub)

    logging.info("Forma: %s | %.0f OCPU | %.0f GB | ventana: %ds",
                 SHAPE, OCPUS, MEMORY_GB, RUN_SECONDS)

    attempt = 0
    while time.time() < deadline:
        attempt += 1
        logging.info("--- Intento %d ---", attempt)

        for ad in ads:
            if time.time() >= deadline:
                break
            details.availability_domain = ad

            for net_try in range(1, NET_RETRIES + 1):
                try:
                    inst = compute.launch_instance(details).data
                    ip = wait_for_public_ip(compute, inst.id)

                    logging.info("=" * 60)
                    logging.info("INSTANCIA CREADA")
                    logging.info("  AD:    %s", ad)
                    logging.info("  OCID:  %s", inst.id)
                    logging.info("  IP:    %s", ip or "(sin IP todavia)")
                    logging.info("=" * 60)

                    cuerpo = (
                        f"Instancia A1 capturada.\n\n"
                        f"- **AD:** {ad}\n"
                        f"- **OCID:** `{inst.id}`\n"
                        f"- **IP publica:** `{ip or 'pendiente'}`\n"
                        f"- **Usuario:** `ubuntu`\n"
                        f"- **Fecha:** {datetime.now(timezone.utc).isoformat()}\n\n"
                        f"```\nssh -i ~/.oci/servidor_openclaw.key ubuntu@{ip or 'IP'}\n```\n"
                    )
                    notificar("VPS A1 capturada", cuerpo)
                    return 0

                except oci.exceptions.ServiceError as exc:
                    if is_retryable(exc):
                        logging.warning("  %s -> sin capacidad (%s)", ad.split(":")[-1], exc.code)
                        break
                    logging.error("  %s -> error NO reintentable: %s: %s",
                                  ad, exc.code, exc.message)
                    return 1
                except Exception as exc:
                    brief = str(exc).split("(")[0].strip()[:90]
                    if net_try < NET_RETRIES:
                        logging.warning("  %s -> fallo de red (%d/%d), reintento %ds: %s",
                                        ad.split(":")[-1], net_try, NET_RETRIES,
                                        NET_RETRY_DELAY, brief)
                        time.sleep(NET_RETRY_DELAY)
                        continue
                    logging.warning("  %s -> fallo de red persistente: %s",
                                    ad.split(":")[-1], brief)

        if time.time() < deadline:
            time.sleep(min(RETRY_SECONDS, max(1, deadline - time.time())))

    logging.info("Ventana agotada tras %d intentos (%ds). El cron reintenta.",
                 attempt, int(time.time() - started))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        logging.error("Fallo inesperado:\n%s", traceback.format_exc())
        sys.exit(1)

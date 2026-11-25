import csv
import re
from collections import Counter
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Usuario
from inventario.models import EntregaHerramienta, Herramienta, normalizar


ORIGEN = "acta-equipos:"
COLUMNAS = {
    "acta", "renglon", "fecha", "responsable", "entregado_por", "tipo",
    "nombre", "marca", "serial", "cantidad", "descripcion",
}


def leer_actas(ruta):
    claves = set()
    with open(ruta, encoding="utf-8-sig", newline="") as archivo:
        lector = csv.DictReader(archivo)
        if set(lector.fieldnames or []) != COLUMNAS or len(lector.fieldnames or []) != len(COLUMNAS):
            raise CommandError("Las columnas del CSV no corresponden a la carga de actas.")
        for fila in lector:
            try:
                fila = {campo: valor.strip() for campo, valor in fila.items()}
                clave = f"{fila['acta']}:{fila['renglon']}"
                if not re.fullmatch(r"[0-9]{1,15}:[0-9]{1,8}(\.[0-9]{1,8})?", clave) or clave in claves:
                    raise ValueError("acta o renglón inválido o repetido")
                if fila["tipo"] not in {"herramienta", "material"}:
                    raise ValueError("tipo inválido")
                if not fila["responsable"]:
                    raise ValueError("falta el responsable")
                cantidad = int(fila["cantidad"])
                if cantidad < 1:
                    raise ValueError("la cantidad debe ser positiva")
                fecha = datetime.strptime(fila["fecha"], "%Y-%m-%d %H:%M:%S")
                claves.add(clave)
            except (AttributeError, ValueError) as exc:
                raise CommandError(f"Línea {lector.line_num}: {exc}.") from exc
            yield ORIGEN + clave, fila, timezone.make_aware(fecha), cantidad


def buscar_tecnico(nombre, tecnicos):
    tokens = set(normalizar(nombre).split())
    if len(tokens) < 2:
        return None
    exactos = []
    parciales = []
    for usuario in tecnicos:
        nombre_cuenta = set(normalizar(usuario.get_full_name()).split())
        if tokens == nombre_cuenta:
            exactos.append(usuario)
        elif tokens < nombre_cuenta:
            parciales.append(usuario)
    candidatos = exactos or parciales
    return candidatos[0] if len(candidatos) == 1 else None


class Command(BaseCommand):
    help = "Carga desde un CSV revisado las herramientas y su historial de entrega. Por defecto solo simula."

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="CSV de actas revisadas")
        parser.add_argument("--confirmar", action="store_true", help="Guardar la carga")
        parser.add_argument("--incluir-materiales", action="store_true",
                            help="Registrar también los materiales como herramientas auditables")

    @transaction.atomic
    def handle(self, ruta, confirmar, incluir_materiales, **opciones):
        try:
            filas = list(leer_actas(ruta))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(f"No se pudo leer el CSV: {exc}") from exc
        if not filas:
            raise CommandError("El CSV no contiene registros.")

        # Serializa cargas confirmadas sobre las mismas cuentas de técnicos.
        usuarios = Usuario.tecnicos().order_by("pk")
        tecnicos = list(usuarios.select_for_update() if confirmar else usuarios)
        existentes = Herramienta.objects.all()
        origenes = set(existentes.filter(importado_de__startswith=ORIGEN)
                       .values_list("importado_de", flat=True))
        seriales = {serial.strip().casefold() for serial in existentes.exclude(serial="")
                    .values_list("serial", flat=True)}
        responsables = {}
        conteo = Counter()
        nombres_sin_cuenta = set()
        omitidas = materiales = 0
        for clave, fila, fecha, cantidad in filas:
            if fila["tipo"] == "material" and not incluir_materiales:
                materiales += 1
                continue
            if clave in origenes:
                omitidas += 1
                continue
            nombre = fila["responsable"]
            if nombre not in responsables:
                responsables[nombre] = buscar_tecnico(nombre, tecnicos)
            tecnico = responsables[nombre]
            if tecnico is None:
                nombres_sin_cuenta.add(nombre)
            serial = fila["serial"]
            if serial and serial.casefold() in seriales:
                raise CommandError(f"{clave}: el serial {serial} ya figura en el inventario o en otra fila.")

            herramienta = Herramienta(
                nombre=fila["nombre"], marca=fila["marca"], serial=serial, cantidad=cantidad,
                descripcion=f"Acta {fila['acta']}. {fila['descripcion']}".strip(),
                estado=Herramienta.Estado.EN_USO, tecnico=tecnico,
                tecnico_nombre="" if tecnico else nombre, importado_de=clave,
            )
            entrega = EntregaHerramienta(
                herramienta_nombre=herramienta.nombre, tecnico=tecnico,
                tecnico_nombre=nombre, fecha=fecha, registrado_por=fila["entregado_por"],
                importado_de=clave,
            )
            try:
                herramienta.full_clean()
                entrega.full_clean(exclude=["herramienta", "tecnico"] if tecnico is None else ["herramienta"])
            except ValidationError as exc:
                raise CommandError(f"{clave}: {exc}") from exc
            if confirmar:
                herramienta.save()
                entrega.herramienta = herramienta
                entrega.save()
            if serial:
                seriales.add(serial.casefold())
            conteo[nombre] += 1

        modo = "Carga guardada" if confirmar else "Simulación sin guardar"
        self.stdout.write(f"{modo} | {sum(conteo.values())} registros y entregas; "
                          f"{omitidas} ya cargados; {materiales} filas de materiales omitidas.")
        for nombre, cantidad in sorted(conteo.items()):
            self.stdout.write(f"  {nombre}: {cantidad}")
        for nombre in sorted(nombres_sin_cuenta):
            self.stdout.write(self.style.WARNING(
                f"Sin cuenta técnica identificada para {nombre}. Se conserva el nombre del acta."
            ))

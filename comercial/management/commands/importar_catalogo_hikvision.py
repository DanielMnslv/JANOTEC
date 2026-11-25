import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from comercial.models import ItemCatalogo
from inventario.models import Proveedor


FUENTE = "Hikvision 2026-10-09 | Preferencial sugerido"
IVA = Decimal("19")
CENTAVO = Decimal("0.01")
FACTOR_IVA = 1 + IVA / 100
COLUMNAS = {"referencia", "descripcion_fuente", "estado", "preferencial_con_iva", "iva_pct", "hoja"}


def clave_referencia(referencia):
    return "".join(referencia.split()).casefold()


def leer_precios(ruta):
    precios = {}
    omitidos = Counter()
    with open(ruta, encoding="utf-8-sig", newline="") as archivo:
        lector = csv.DictReader(archivo, delimiter=";")
        if not COLUMNAS.issubset(lector.fieldnames or []):
            raise CommandError("Use el CSV revisado de la lista Hikvision del 9 de octubre de 2026.")
        for fila in lector:
            estado = fila["estado"]
            if estado in {"sin_precio_numerico", "referencia_en_conflicto", "duplicado"}:
                omitidos[estado] += 1
                continue
            try:
                referencia = fila["referencia"].strip()
                descripcion = fila["descripcion_fuente"].strip()
                clave = clave_referencia(referencia)
                if estado != "candidato" or not clave or not descripcion or clave in precios:
                    raise ValueError("estado, referencia o descripción inválidos")
                bruto = Decimal(fila["preferencial_con_iva"])
                if not bruto.is_finite() or bruto <= 0 or Decimal(fila["iva_pct"]) != IVA:
                    raise ValueError("precio o IVA inválido")
                neto = (bruto / FACTOR_IVA).quantize(CENTAVO, rounding=ROUND_HALF_UP)
                if (neto * FACTOR_IVA).quantize(CENTAVO, rounding=ROUND_HALF_UP) != bruto:
                    raise ValueError("el precio con IVA no se puede reproducir a dos decimales")
            except (AttributeError, TypeError, ValueError, InvalidOperation) as exc:
                raise CommandError(f"Línea {lector.line_num}: {exc}.") from exc
            marca = "HiLook" if fila["hoja"].startswith("HILOOK") and not referencia.upper().startswith("DS-") else "Hikvision"
            precios[clave] = (referencia, descripcion, marca, neto)
    return precios, omitidos


class Command(BaseCommand):
    help = "Importa precios preferenciales del CSV revisado de Hikvision. Por defecto simula la carga."

    def add_arguments(self, parser):
        parser.add_argument("archivo")
        parser.add_argument("--confirmar", action="store_true", help="Guarda los cambios.")
        parser.add_argument("--proveedor", help="Razón social exacta del proveedor para los ítems nuevos.")

    @transaction.atomic
    def handle(self, archivo, confirmar=False, proveedor=None, **opciones):
        try:
            precios, omitidos = leer_precios(archivo)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(f"No se pudo leer el CSV: {exc}") from exc
        if not precios:
            raise CommandError("El CSV no contiene precios preferenciales para cargar.")
        if proveedor:
            try:
                proveedor = Proveedor.objects.get(razon_social=proveedor)
            except Proveedor.DoesNotExist as exc:
                raise CommandError("No se encontró el proveedor indicado.") from exc

        catalogo = ItemCatalogo.objects.order_by("pk")
        por_referencia = defaultdict(list)
        for item in catalogo.select_for_update() if confirmar else catalogo:
            if item.referencia:
                por_referencia[clave_referencia(item.referencia)].append(item)

        conteo = Counter()
        for clave, (referencia, descripcion, marca, neto) in precios.items():
            coincidencias = por_referencia[clave]
            if len(coincidencias) > 1:
                self.stdout.write(self.style.WARNING(f"Referencia ambigua en el catálogo: {referencia}"))
                conteo["ambiguos"] += 1
                continue
            if coincidencias:
                item = coincidencias[0]
                if item.tipo != ItemCatalogo.Tipo.MATERIAL or (item.marca and item.marca.casefold() != marca.casefold()):
                    self.stdout.write(self.style.WARNING(f"Tipo o marca en conflicto: {referencia}"))
                    conteo["ambiguos"] += 1
                    continue
                if (item.valor_unitario, item.iva, item.fuente) == (neto, IVA, FUENTE):
                    conteo["iguales"] += 1
                    continue
                conteo["actualizados"] += 1
            else:
                limite = ItemCatalogo._meta.get_field("descripcion").max_length
                item = ItemCatalogo(
                    descripcion=f"{referencia} | {descripcion}"[:limite],
                    referencia=referencia, marca=marca, unidad="und", proveedor=proveedor,
                )
                conteo["nuevos"] += 1
            item.valor_unitario, item.iva, item.fuente = neto, IVA, FUENTE
            try:
                item.full_clean()
            except ValidationError as exc:
                raise CommandError(f"{referencia}: {exc}") from exc
            if confirmar:
                if coincidencias:
                    item.save(update_fields=["valor_unitario", "iva", "fuente", "actualizado"])
                else:
                    item.save()

        modo = "Carga guardada" if confirmar else "Simulación sin guardar"
        self.stdout.write(f"{modo}: {conteo['nuevos']} nuevos, {conteo['actualizados']} actualizados, "
                          f"{conteo['iguales']} sin cambios y {conteo['ambiguos']} en revisión.")
        for motivo, cantidad in sorted(omitidos.items()):
            self.stdout.write(f"Omitidos ({motivo}): {cantidad}")

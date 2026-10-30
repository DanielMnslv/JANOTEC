"""Carga el catálogo de precios desde el consolidado de propuestas aprobadas (hoja «DETALLE ITEMS»).

    python manage.py importar_catalogo_precios "Z:\\...\\Consolidado Propuestas Economicas JANOTEC.xlsx"
    python manage.py importar_catalogo_precios archivo.xlsx --confirmar   # sin --confirmar solo muestra lo que haría

Cada descripción queda una sola vez, con el precio de la propuesta más reciente en que aparece. Los servicios
(«Servicio…», «Mano de obra…») quedan como tipo servicio. No borra ni cambia lo que ya se ajustó a mano,
salvo el precio de los ítems que se vuelven a encontrar.
"""
import re
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from comercial.models import ItemCatalogo


def _fecha_de(codigo):
    """«PE-2026-07-08-V1» o «PE 12-jun-2026» → clave para ordenar de la más antigua a la más reciente."""
    meses = {m: i for i, m in enumerate(["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"], 1)}
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", codigo or "")
    if m:
        return tuple(int(x) for x in m.groups())
    m = re.search(r"(\d{1,2})-([a-z]{3})-(\d{4})", (codigo or "").lower())
    if m:
        return int(m.group(3)), meses.get(m.group(2), 0), int(m.group(1))
    return (0, 0, 0)


def _limpia(texto):
    """Une variantes del mismo ítem: el servicio sin el nombre de la sede y el plural de «Canaletas»."""
    texto = " ".join(str(texto or "").split()).strip(" .")
    texto = re.sub(r"\s+en oficinas EDS .*$", "", texto, flags=re.I)
    return re.sub(r"^Canaletas(?= )", "Canaleta", texto)


class Command(BaseCommand):
    help = "Carga el catálogo de precios desde el consolidado de propuestas económicas aprobadas."

    def add_arguments(self, parser):
        parser.add_argument("archivo")
        parser.add_argument("--confirmar", action="store_true", help="Guarda los cambios (sin esto, solo los muestra).")

    def handle(self, archivo, confirmar=False, **opciones):
        from openpyxl import load_workbook

        try:
            libro = load_workbook(archivo, read_only=True, data_only=True)
        except OSError as e:
            raise CommandError(f"No se pudo abrir {archivo}: {e}")
        if "DETALLE ITEMS" not in libro.sheetnames:
            raise CommandError("El archivo no tiene la hoja «DETALLE ITEMS».")
        filas = []
        for fila in libro["DETALLE ITEMS"].iter_rows(min_row=3, values_only=True):
            codigo, _cliente, _sede, descripcion, cantidad, unitario = (list(fila) + [None] * 6)[:6]
            descripcion = _limpia(descripcion)
            if not descripcion or str(codigo or "").upper().startswith("SUBTOTAL") or not isinstance(unitario, (int, float)):
                continue
            filas.append((_fecha_de(str(codigo)), str(codigo), descripcion, cantidad or 1, Decimal(str(round(unitario, 2)))))
        filas.sort(key=lambda f: f[0])  # la más reciente gana
        items = {}
        for _, codigo, descripcion, cantidad, unitario in filas:
            clave = descripcion.lower()
            servicio = descripcion.lower().startswith(("servicio", "mano de obra"))
            metros = not servicio and descripcion.lower().startswith("cable") and cantidad >= 10
            items[clave] = {"descripcion": descripcion[:250], "valor_unitario": unitario, "fuente": codigo[:120],
                            "tipo": ItemCatalogo.Tipo.SERVICIO if servicio else ItemCatalogo.Tipo.MATERIAL,
                            "unidad": "global" if servicio else ("m" if metros else "und")}
        nuevos = actualizados = 0
        with transaction.atomic():
            for datos in items.values():
                existente = ItemCatalogo.objects.filter(descripcion__iexact=datos["descripcion"]).first()
                if existente:
                    actualizados += 1
                    existente.valor_unitario, existente.fuente = datos["valor_unitario"], datos["fuente"]
                    existente.save(update_fields=["valor_unitario", "fuente", "actualizado"])
                else:
                    nuevos += 1
                    ItemCatalogo.objects.create(**datos)
                self.stdout.write(f"  {datos['tipo']:8} {datos['unidad']:6} ${datos['valor_unitario']:>14,.2f}  {datos['descripcion']}  ({datos['fuente']})")
            if not confirmar:
                transaction.set_rollback(True)
        estado = "guardados" if confirmar else "por guardar (use --confirmar)"
        self.stdout.write(self.style.SUCCESS(f"{len(items)} ítems: {nuevos} nuevos y {actualizados} con precio actualizado, {estado}."))

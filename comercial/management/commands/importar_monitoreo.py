"""Importa los contratos de monitoreo desde los Excel de facturación por cliente (hoja «ALARMA»).

Cada fila trae la sede, su dirección, el valor mensual vigente y el del año anterior; arriba va el incremento (0.23).
Por defecto solo muestra lo que haría. Con --aplicar crea los servicios y deja el ajuste anual en el historial.
Las filas cuya sede no se reconoce, o se parece a dos, quedan por fuera y se listan.

    python manage.py importar_monitoreo "GASES DEL LLANO S.A.xlsx" "BIOAGRICOLA DEL LLANO S.A.xlsx" --aplicar
"""
import os
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Empresa
from comercial.models import AjusteMonitoreo, ServicioMonitoreo

GENERICAS = {"OFICINA", "EDS", "SEDE", "ESTACION", "BIOAGRICOLA", "BIOGRICOLA", "LLANOGAS", "CUSIANAGAS", "VILLAVICENCIO",
             "LLANO", "GASES", "CUSIANA", "DEL", "LAS", "LOS", "PER", "ESP", "S A", "SAS"}
SINONIMOS = {"BOGEGA": "BODEGA", "MONTEREY": "MONTERREY", "METEOROLOGIA": "METROLOGIA", "PTLX": "PTAR"}
DOS = Decimal("0.01")


def palabras(texto):
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return {SINONIMOS.get(w, w) for w in re.findall(r"[A-Z0-9]{3,}", t)} - GENERICAS


def _letras(texto):
    """Palabras largas del nombre de un cliente, para reconocer la empresa."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return set(re.findall(r"[A-Z]{4,}", t)) - {"EMPRESA", "SERVICIOS", "PUBLICOS"}


def leer(ruta):
    """(filas, porcentaje, año del valor vigente) de la hoja «ALARMA» del Excel."""
    libro = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    hoja = next((h for h in libro.worksheets if h.title.strip().upper() == "ALARMA"), None)
    if hoja is None:
        raise CommandError(f"{os.path.basename(ruta)}: no tiene hoja «ALARMA»")
    filas = [list(f) for f in hoja.iter_rows(values_only=True)]
    porcentaje = next(Decimal(str(v)) * 100 for v in filas[0] if isinstance(v, (int, float)) and 0 < v < 1)
    i_enc = next(i for i, f in enumerate(filas) if any(str(v or "").strip().upper() in ("SEDE", "SEDES") for v in f))
    enc = [str(v or "").strip().upper() for v in filas[i_enc]]
    col = {"cliente": enc.index("CLIENTE"), "sede": next(i for i, v in enumerate(enc) if v in ("SEDE", "SEDES")),
           "direccion": enc.index("DIRECCION"), "ciudad": enc.index("CIUDAD"), "descripcion": enc.index("DESCRIPCIÓN"),
           "cant": enc.index("CANT"), "valor": next(i for i, v in enumerate(enc) if v.startswith("VALOR")),
           "anterior": next(i for i, v in enumerate(enc) if v.isdigit())}
    anio = int(enc[col["anterior"]]) + 1
    datos = []
    for f in filas[i_enc + 1:]:
        if not f[col["sede"]] or not isinstance(f[col["valor"]], (int, float)):
            continue
        datos.append({k: (str(f[i]).strip() if isinstance(f[i], str) else f[i]) for k, i in col.items()})
    return datos, porcentaje, anio


class Command(BaseCommand):
    help = "Importa los contratos de monitoreo (valor mensual por sede) desde los Excel por cliente."

    def add_arguments(self, parser):
        parser.add_argument("archivos", nargs="+")
        parser.add_argument("--aplicar", action="store_true", help="Guardar (sin esto solo muestra lo que haría).")

    def handle(self, archivos, aplicar, **kw):
        empresas = list(Empresa.objects.prefetch_related("sedes"))
        with transaction.atomic():
            for ruta in archivos:
                self._archivo(ruta, empresas)
            if not aplicar:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Solo revisión: no se guardó nada. Use --aplicar para guardar."))

    def _archivo(self, ruta, empresas):
        datos, porcentaje, anio = leer(ruta)
        nombre = os.path.basename(ruta)
        cliente = _letras(datos[0]["cliente"])
        empresa = max(empresas, key=lambda e: len(_letras(e.razon_social) & cliente))
        self.stdout.write(f"\n== {nombre} => {empresa} · {len(datos)} filas · incremento {porcentaje:g} % · valores de {anio}")
        sedes = list(empresa.sedes.all())
        creados = ya = 0
        vistas, fuera = set(), []
        for d in datos:
            p = palabras(d["sede"])
            # Si dos sedes se parecen igual, gana la que tiene exactamente el mismo nombre
            puntaje = sorted((((len(p & palabras(s.nombre)), p == palabras(s.nombre)), s) for s in sedes), key=lambda x: x[0],
                             reverse=True)
            if not puntaje or puntaje[0][0][0] == 0 or (len(puntaje) > 1 and puntaje[1][0] == puntaje[0][0]):
                fuera.append((d["sede"], "no se reconoce" if not puntaje or puntaje[0][0][0] == 0 else
                              f"se parece a «{puntaje[0][1].nombre}» y a «{puntaje[1][1].nombre}»"))
                continue
            sede = puntaje[0][1]
            if sede.pk in vistas:
                fuera.append((d["sede"], f"fila repetida de «{sede.nombre}»"))
                continue
            vistas.add(sede.pk)
            if sede.monitoreos.exists():
                ya += 1
                continue
            valor = Decimal(str(d["valor"])).quantize(DOS, ROUND_HALF_UP)
            anterior = Decimal(str(d["anterior"])).quantize(DOS, ROUND_HALF_UP)
            s = ServicioMonitoreo.objects.create(sede=sede, descripcion=d["descripcion"] or ServicioMonitoreo.DESCRIPCION,
                                                 cantidad=d["cant"] or 1, valor_mensual=valor, anio_valor=anio,
                                                 fuente=f"Excel «{nombre}»")
            AjusteMonitoreo.objects.create(servicio=s, anio=anio, porcentaje=porcentaje, valor_anterior=anterior,
                                           valor_nuevo=valor, nota=f"Importado del Excel «{nombre}»")
            cambios = []
            if not sede.direccion and d["direccion"]:
                sede.direccion = d["direccion"][:200]
                cambios.append("direccion")
            if cambios:
                sede.save(update_fields=cambios)
            creados += 1
            self.stdout.write(f"   {d['sede'][:45]:45} => {sede.nombre[:40]:40} ${valor:,.0f}"
                              + (" · se llenó la dirección" if cambios else ""))
        self.stdout.write(f"   Se crean {creados}" + (f" · ya tenían contrato {ya}" if ya else "") + f" · por fuera {len(fuera)}")
        for sede, motivo in fuera:
            self.stdout.write(self.style.WARNING(f"   POR FUERA: {sede} ({motivo})"))

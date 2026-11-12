"""Importa el Excel «Compras y facturas» que llevaban compras y financiera antes de la plataforma.

Del archivo salen dos cosas:

* **Proveedores**: los que tienen al menos una compra. Se buscan por NIT y luego por nombre; si existe sin NIT se le
  pone, y si no existe se crea. No se cargan todos los «Terceros» (hay clientes y personas que no son proveedores):
  esa hoja solo se usa para poner bien el nombre cuando una fila trae el NIT y no la razón social.
* **Historial de compras** (``CompraHistorica``): cada fila de las hojas de facturas. Se reemplaza completo en cada
  importación, así que se puede volver a subir el Excel actualizado sin que se repita nada.

Las columnas se reconocen por el encabezado de cada hoja, no por su posición. En la hoja de febrero a diciembre de
2026 las primeras filas (hasta abril) usaban otro orden: fecha de la factura donde dice «Subtotal», número donde dice
«Saldo a favor» y el enlace de la factura donde dice «Estado». Esas filas se reconocen y se leen como eran.

Uso: ``leer(archivo)`` → ``plan(datos)`` muestra qué haría sin guardar; ``aplicar(datos, usuario)`` lo guarda.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import CompraHistorica, Proveedor, normalizar

# Encabezado normalizado → campo de CompraHistorica (o dato que se usa para armarlo).
COLUMNAS = {
    "NIT": "nit", "RAZON SOCIAL": "razon_social",
    "FECHA COTIZACION": "fecha", "NO COTIZACION": "cotizacion", "N COTIZACION": "cotizacion",
    "VALOR COTIZACION": "valor_cotizacion", "ENLACE DE COTIZACION": "enlace_cotizacion",
    "ENLACE DE ORDEN DE COMPRA": "enlace_orden", "OBSERVACIONES COMPRAS": "destino",
    "FECHA FACTURA": "fecha_factura", "N FACTURA": "factura", "NO FACTURA": "factura",
    "VALOR FACTURA": "valor_factura", "ENLACE DE FACTURA": "enlace_factura",
    "SUBTOTAL": "subtotal", "IVA": "iva", "RETENCION": "retencion", "VALOR A PAGAR": "valor_a_pagar",
    "SALDO A FAVOR": "saldo_a_favor", "ESTADO DE FACTURA": "estado_factura", "LLEGO PEDIDO": "llego_pedido",
    "FECHA DE PAGO": "fecha_pago", "ANTICIPO": "anticipo",
    "COMPROBANTE CONTABLE": "comprobante", "N FC": "numero_fc",
    "OBSERVACIONES FINANCIERA": "observaciones", "OBSERVACIONES": "observaciones",
}
FECHAS = {"fecha", "fecha_factura", "fecha_pago"}
VALORES = {"valor_cotizacion", "valor_factura", "subtotal", "iva", "retencion", "valor_a_pagar", "saldo_a_favor"}
LARGOS = {f.name: f.max_length for f in CompraHistorica._meta.fields if getattr(f, "max_length", None)}


@dataclass
class Datos:
    """Lo leído del Excel, todavía sin guardar."""
    terceros: dict = field(default_factory=dict)       # NIT → razón social de la hoja «Terceros»
    filas: list = field(default_factory=list)          # dicts con los campos de CompraHistorica
    ignoradas: list = field(default_factory=list)      # (hoja, fila, motivo)
    hojas: Counter = field(default_factory=Counter)    # hoja → filas que entran


def nit_limpio(valor):
    """«900.795.612-1», 900795612.0 → «900795612» (sin dígito de verificación)."""
    if valor is None:
        return ""
    if isinstance(valor, (int, float)):
        return str(int(valor)) if valor > 0 else ""
    return re.sub(r"\D", "", str(valor).split("-")[0])


def _texto(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    return re.sub(r"\s+", " ", str(v)).strip()


def _fecha(v):
    """Fecha de Excel: celda de fecha, número de serie (46055) o texto «dd/mm/aaaa». Lo demás queda vacío."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 30000 < v < 60000:
        return date(1899, 12, 30) + timedelta(days=int(v))
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})", str(v or ""))
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def _valor(v):
    """« $1,297,860 », 1297860.0 → Decimal. Texto que no es plata queda vacío."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(round(v, 2)))
    s = re.sub(r"[$\s]", "", str(v))
    if not re.fullmatch(r"-?[\d,]+(\.\d+)?", s):
        return None
    try:
        return Decimal(s.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _es_fila_vieja(crudo):
    """Filas de feb.–abr. 2026: fecha de la factura (n.º de serie) en «Subtotal» y el enlace en «Estado»."""
    sub, estado = crudo.get("subtotal"), crudo.get("estado_factura")
    return (isinstance(sub, (int, float)) and float(sub).is_integer() and 40000 < sub < 60000
            and "http" in str(estado or "") and "fecha_factura" not in crudo)


def _encabezado(ws):
    """(n.º de fila, {columna: campo}) del primer renglón que tenga NIT y RAZÓN SOCIAL."""
    for i, fila in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), start=1):
        nombres = [normalizar(v) for v in fila]
        if "NIT" in nombres and "RAZON SOCIAL" in nombres:
            return i, {c: COLUMNAS[n] for c, n in enumerate(nombres) if n in COLUMNAS}
    return None, {}


def leer(archivo):
    """Lee el libro completo (archivo abierto o ruta). Las hojas sin encabezado reconocible se saltan."""
    from openpyxl import load_workbook

    wb = load_workbook(archivo, data_only=True, read_only=True)
    datos = Datos()
    hojas = []
    for ws in wb.worksheets:
        inicio, mapa = _encabezado(ws)
        if not inicio:
            continue
        if set(mapa.values()) <= {"nit", "razon_social"}:
            for fila in ws.iter_rows(min_row=inicio + 1, values_only=True):
                nit = nit_limpio(fila[0] if fila else None)
                nombre = _texto(fila[1]) if len(fila) > 1 else ""
                if nit and nombre:
                    datos.terceros.setdefault(nit, nombre)
        else:
            hojas.append((ws, inicio, mapa))
    for ws, inicio, mapa in hojas:
        for n, fila in enumerate(ws.iter_rows(min_row=inicio + 1, values_only=True), start=inicio + 1):
            crudo = {}
            for c, campo in mapa.items():
                if c < len(fila) and fila[c] not in (None, ""):
                    crudo.setdefault(campo, fila[c])
            _agregar(datos, ws.title.strip(), n, crudo)
    wb.close()
    return datos


def _agregar(datos, hoja, n, crudo):
    if not crudo:
        return
    if _es_fila_vieja(crudo):
        crudo["fecha_factura"] = crudo.pop("subtotal")
        crudo["enlace_factura"] = crudo.pop("estado_factura")
        if "saldo_a_favor" in crudo:
            crudo["factura"] = crudo.pop("saldo_a_favor")
    nit = nit_limpio(crudo.get("nit"))
    razon = _texto(crudo.get("razon_social"))
    if razon and (razon.startswith("$") or _valor(razon) is not None):
        datos.ignoradas.append((hoja, n, f"la razón social es un valor ({razon}): la fila está corrida"))
        return
    razon = razon or datos.terceros.get(nit, "")
    if not razon:
        if len(crudo) > 1:
            datos.ignoradas.append((hoja, n, "no tiene razón social y el NIT no está en Terceros"))
        return
    f = {"hoja": hoja, "fila": n, "nit": nit, "razon_social": razon}
    for campo, v in crudo.items():
        if campo in ("nit", "razon_social"):
            continue
        if campo in FECHAS:
            f[campo] = _fecha(v)
        elif campo in VALORES:
            f[campo] = _valor(v)
        elif campo == "anticipo":
            f[campo] = bool(_texto(v))
        else:
            f[campo] = _texto(v)
    fc = f.pop("numero_fc", "")
    f["comprobante"] = " · ".join(x for x in (f.get("comprobante", ""), fc) if x)
    if not any(f.get(c) for c in ("fecha", "fecha_factura", "cotizacion", "factura", "valor_cotizacion", "valor_factura",
                                    "valor_a_pagar", "destino")):
        datos.ignoradas.append((hoja, n, "solo tiene el proveedor"))
        return
    for campo, largo in LARGOS.items():
        if isinstance(f.get(campo), str):
            f[campo] = f[campo][:largo]
    datos.filas.append(f)
    datos.hojas[hoja] += 1


def clave_nombre(texto):
    """«Grupo Defa S.A.S.» y «GRUPO DEFA SAS» son el mismo: sin espacios, puntos ni la sigla de la sociedad al final."""
    return re.sub(r"(SAS|SA|LTDA|EU|SENC)$", "", normalizar(texto).replace(" ", "")) or normalizar(texto)


def _indices():
    por_nit, por_nombre = {}, {}
    for p in Proveedor.objects.all():
        if nit_limpio(p.nit):
            por_nit.setdefault(nit_limpio(p.nit), p)
        por_nombre.setdefault(clave_nombre(p.razon_social), p)
    return por_nit, por_nombre


def plan(datos):
    """Qué haría la importación, sin guardar nada: proveedores nuevos, a los que se les pone NIT y los que ya están."""
    por_nit, por_nombre = _indices()
    nombres = {}  # clave del proveedor (NIT o nombre) → razón social más escrita
    conteo = Counter()
    for f in datos.filas:
        clave = f["nit"] or clave_nombre(f["razon_social"])
        conteo[(clave, f["razon_social"])] += 1
    for (clave, razon), _ in conteo.most_common():
        nombres.setdefault(clave, datos.terceros.get(clave) or razon)
    nuevos, con_nit, existentes, asignado = [], [], [], {}
    for clave, razon in nombres.items():
        nit = clave if clave.isdigit() else ""
        p = por_nit.get(nit) if nit else None
        if p is None:
            p = por_nombre.get(clave_nombre(razon))
            if p is not None and nit and nit_limpio(p.nit) and nit_limpio(p.nit) != nit:
                p = None  # mismo nombre y otro NIT: es otro proveedor
        if p is None:
            nuevos.append({"clave": clave, "nit": nit, "razon_social": razon,
                           "compras": sum(n for (c, _), n in conteo.items() if c == clave)})
        elif nit and not nit_limpio(p.nit):
            con_nit.append({"proveedor": p, "nit": nit})
        else:
            existentes.append(p)
        asignado[clave] = p
    return {"nuevos": sorted(nuevos, key=lambda x: -x["compras"]), "con_nit": con_nit, "existentes": existentes,
            "asignado": asignado, "compras": len(datos.filas), "hojas": dict(datos.hojas),
            "ignoradas": datos.ignoradas, "anteriores": CompraHistorica.objects.count()}


def _nombre_libre(razon, nit, ocupados):
    nombre = razon[:200]
    if clave_nombre(nombre) in ocupados:
        nombre = f"{razon[:180]} (NIT {nit})"
    return nombre


@transaction.atomic
def aplicar(datos, usuario):
    """Crea y completa los proveedores y reemplaza todo el historial con lo que trae este Excel."""
    resumen = plan(datos)
    asignado = resumen["asignado"]
    ocupados = set(clave_nombre(r) for r in Proveedor.objects.values_list("razon_social", flat=True))
    for x in resumen["nuevos"]:
        nombre = _nombre_libre(x["razon_social"], x["nit"], ocupados)
        ocupados.add(clave_nombre(nombre))
        asignado[x["clave"]] = Proveedor.objects.create(
            nit=x["nit"], razon_social=nombre, notas="Creado desde el Excel «Compras y facturas».")
    for x in resumen["con_nit"]:
        x["proveedor"].nit = x["nit"]
        x["proveedor"].save(update_fields=["nit"])
    CompraHistorica.objects.all().delete()
    CompraHistorica.objects.bulk_create([
        CompraHistorica(proveedor=asignado.get(f["nit"] or clave_nombre(f["razon_social"])), importado_por=usuario, **f)
        for f in datos.filas], batch_size=500)
    return resumen

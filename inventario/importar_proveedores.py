"""Importa el listado de proveedores en Excel («Relación de proveedores»: NIT, razón social, dirección, ciudad, contacto).

Cada fila es un proveedor. Se busca por NIT y luego por nombre (igual que en «Importar compras del Excel»):

* si no existe, se crea con NIT, razón social y dirección (con la ciudad);
* si existe, solo se completa lo que le falta (NIT o dirección): nada de lo que ya se escribió en la plataforma se pisa;
* si el contacto es una persona (no el mismo nombre de la empresa), queda como asesor del proveedor, salvo que ya esté.

Se puede subir el mismo listado cuantas veces se quiera: lo que ya está no se repite.

Uso: ``leer(archivo)`` → ``plan(filas)`` muestra qué haría sin guardar; ``aplicar(filas)`` lo guarda.
"""
from difflib import SequenceMatcher

from django.db import transaction

from .importar_compras import _texto, clave_nombre, nit_limpio
from .models import AsesorProveedor, Proveedor, normalizar

# Encabezado normalizado → dato de la fila
COLUMNAS = {"NIT CEDULA": "nit", "NIT": "nit", "CEDULA": "nit", "NOMBRE RAZON SOCIAL RUT": "razon_social",
            "NOMBRE RAZON SOCIAL": "razon_social", "RAZON SOCIAL": "razon_social", "DIRECCION": "direccion",
            "CIUDAD": "ciudad", "NOMBRE CONTACTO": "contacto", "CONTACTO": "contacto", "ASESOR": "contacto"}


def _encabezado(ws):
    """(n.º de fila, {columna: dato}) del primer renglón que tenga NIT y razón social."""
    for i, fila in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        mapa = {c: COLUMNAS[normalizar(v)] for c, v in enumerate(fila) if normalizar(v) in COLUMNAS}
        if {"nit", "razon_social"} <= set(mapa.values()):
            return i, mapa
    return None, {}


def leer(archivo):
    """Las filas del listado: dicts con nit, razon_social, direccion, ciudad y contacto. Hojas sin encabezado, fuera."""
    from openpyxl import load_workbook

    wb = load_workbook(archivo, data_only=True, read_only=True)
    filas, vistos = [], set()
    for ws in wb.worksheets:
        inicio, mapa = _encabezado(ws)
        if not inicio:
            continue
        for fila in ws.iter_rows(min_row=inicio + 1, values_only=True):
            d = {campo: _texto(fila[c]) if c < len(fila) else "" for c, campo in mapa.items()}
            d["nit"] = nit_limpio(d.get("nit"))
            if not d.get("razon_social"):
                continue
            clave = d["nit"] or clave_nombre(d["razon_social"])
            if clave not in vistos:  # el mismo proveedor dos veces en el listado: cuenta una
                vistos.add(clave)
                filas.append(d)
    wb.close()
    return filas


def es_persona(contacto, razon_social):
    """El contacto es alguien que atiende y no el mismo nombre de la empresa escrito distinto («MYM SYSTECH SAS»)."""
    if not contacto:
        return False
    a, b = clave_nombre(contacto), clave_nombre(razon_social)
    primera = lambda t: (normalizar(t).split() or [""])[0]  # noqa: E731
    return SequenceMatcher(None, a, b).ratio() < 0.85 and primera(contacto) != primera(razon_social)


def _direccion(d):
    direccion, ciudad = d.get("direccion", ""), d.get("ciudad", "")
    if ciudad and normalizar(ciudad) not in normalizar(direccion):
        direccion = f"{direccion}, {ciudad}" if direccion else ciudad
    return direccion[:255]


def plan(filas):
    """Qué haría la importación, sin guardar: proveedores nuevos, a los que se les completa algo y los que ya están."""
    por_nit, por_nombre = {}, {}
    for p in Proveedor.objects.prefetch_related("asesores"):
        if nit_limpio(p.nit):
            por_nit.setdefault(nit_limpio(p.nit), p)
        por_nombre.setdefault(clave_nombre(p.razon_social), p)
    nuevos, completar, iguales = [], [], []
    for d in filas:
        p = por_nit.get(d["nit"]) if d["nit"] else None
        if p is None:
            p = por_nombre.get(clave_nombre(d["razon_social"]))
            if p is not None and d["nit"] and nit_limpio(p.nit) and nit_limpio(p.nit) != d["nit"]:
                p = None  # mismo nombre y otro NIT: es otro proveedor
        persona = d.get("contacto", "") if es_persona(d.get("contacto"), d["razon_social"]) else ""
        x = {"fila": d, "direccion": _direccion(d), "asesor": persona[:150]}
        if p is None:
            nuevos.append(x)
            continue
        x["proveedor"] = p
        x["cambios"] = []
        if d["nit"] and not nit_limpio(p.nit):
            x["cambios"].append("NIT")
        if x["direccion"] and not p.direccion:
            x["cambios"].append("dirección")
        if persona and not any(clave_nombre(a.nombre) == clave_nombre(persona) for a in p.asesores.all()):
            x["cambios"].append("asesor")
        else:
            x["asesor"] = ""
        (completar if x["cambios"] else iguales).append(x)
    return {"nuevos": nuevos, "completar": completar, "iguales": iguales, "total": len(filas),
            "asesores": sum(1 for x in nuevos + completar if x["asesor"])}


@transaction.atomic
def aplicar(filas):
    """Crea los proveedores nuevos y completa los que ya estaban. Devuelve el plan que se aplicó."""
    resumen = plan(filas)
    ocupados = set(clave_nombre(r) for r in Proveedor.objects.values_list("razon_social", flat=True))
    for x in resumen["nuevos"]:
        d = x["fila"]
        nombre = d["razon_social"][:200]
        if clave_nombre(nombre) in ocupados:
            nombre = f"{d['razon_social'][:180]} (NIT {d['nit']})"
        ocupados.add(clave_nombre(nombre))
        x["proveedor"] = Proveedor.objects.create(nit=d["nit"], razon_social=nombre, direccion=x["direccion"],
                                                  notas="Creado desde el listado de proveedores en Excel.")
    for x in resumen["completar"]:
        p, d = x["proveedor"], x["fila"]
        if "NIT" in x["cambios"]:
            p.nit = d["nit"]
        if "dirección" in x["cambios"]:
            p.direccion = x["direccion"]
        p.save(update_fields=["nit", "direccion"])
    for x in resumen["nuevos"] + resumen["completar"]:
        if x["asesor"]:
            AsesorProveedor.objects.create(proveedor=x["proveedor"], nombre=x["asesor"].title())
    return resumen

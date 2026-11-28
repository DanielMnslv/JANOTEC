"""Importa la relación de clientes y sedes en Excel (una fila por sede).

Columnas que reconoce (por el encabezado, no por la posición): NIT, CLIENTE, SEDE, CIUDAD, DIRECCIÓN, ABONADO,
SISTEMA DE ALARMA, VIDEOVIGILANCIA, COMERCIAL, PERSONA CONTACTO, NÚMERO CONTACTO, N° DE LÍNEA, OPERADOR, TIPO DE LÍNEA.

Qué hace con cada fila:

* **Cliente**: se busca por NIT y luego por nombre. Si no existe se crea; si existe, solo se completa lo que le falta
  (NIT, ciudad). Nada de lo que ya está escrito en la plataforma se pisa.
* **Sede**: se busca por nombre dentro del cliente. Si no existe se crea con municipio, departamento, dirección,
  abonado y contacto; si existe, solo se completan los datos vacíos.
* **Comercial**: queda como encargado del cliente (cargo «Comercial») si el cliente aún no tiene encargado.
* **Equipos** (solo si quien importa es coordinación o dirección): el panel de alarma y el sistema de
  videovigilancia quedan en los equipos instalados de la sede, con la línea de comunicación en sus características.
  Si la sede ya tiene un equipo igual (mismo sistema y equipo), no se repite.

Uso: ``leer(archivo)`` → ``plan(filas)`` muestra qué haría sin guardar; ``aplicar(filas, usuario, con_equipos)``.
"""
import re

from django.db import transaction

from accounts.models import EncargadoCliente, Empresa, Sede
from inventario.importar_compras import _texto, clave_nombre, nit_limpio
from inventario.models import normalizar

COLUMNAS = {"NIT": "nit", "CLIENTE": "cliente", "RAZON SOCIAL": "cliente", "SEDE": "sede", "CIUDAD": "ciudad",
            "MUNICIPIO": "ciudad", "DIRECCION": "direccion", "ABONADO": "abonado", "SISTEMA DE ALARMA": "alarma",
            "VIDEOVIGILANCIA": "video", "COMERCIAL": "comercial", "PERSONA CONTACTO": "contacto",
            "NUMERO CONTACTO": "telefono", "N DE LINEA": "linea", "NO DE LINEA": "linea", "OPERADOR": "operador",
            "TIPO DE LINEA": "tipo_linea"}

# Municipio → departamento (los que aparecen en la relación; lo que no esté queda en blanco para completarlo a mano)
DEPARTAMENTOS = {
    "Meta": ["VILLAVICENCIO", "PUERTO LOPEZ", "PUERTO GAITAN", "GRANADA", "ACACIAS", "CABUYARO", "CUMARAL",
             "SAN CARLOS DE GUAROA", "FUENTE DE ORO", "SAN MARTIN", "BARRANCA DE UPIA", "CUBARRAL", "EL CASTILLO",
             "PUERTO LLERAS", "PUERTO RICO", "RESTREPO", "SAN JUAN DE ARAMA", "VISTA HERMOSA", "VISTAHERMOSA",
             "GUAMAL", "CASTILLA LA NUEVA", "MESETAS", "LEJANIAS", "MAPIRIPAN", "PUERTO CONCORDIA", "LA MACARENA",
             "URIBE", "EL DORADO", "SAN JUANITO", "EL CALVARIO"],
    "Casanare": ["YOPAL", "AGUAZUL", "TAURAMENA", "VILLANUEVA", "MONTERREY", "PAZ DE ARIPORO", "MANI"],
    "Cundinamarca": ["CHIA", "GIRARDOT", "CAQUEZA", "CHIPAQUE", "FOSCA", "QUETAME", "UNE", "SOACHA", "FUSAGASUGA"],
    "Bogotá D.C.": ["BOGOTA"],
    "Guaviare": ["SAN JOSE DEL GUAVIARE", "EL RETORNO"],
    "Arauca": ["ARAUCA"],
    "Valle del Cauca": ["YUMBO", "CALI"],
    "Boyacá": ["TUNJA", "BOYACA"],
    "Huila": ["GIGANTE", "NEIVA"],
    "Vichada": ["PUERTO CARRENO", "PUERTO CARENO"],
}
DEPARTAMENTO_DE = {m: d for d, municipios in DEPARTAMENTOS.items() for m in municipios}
ETIQUETAS = {"ciudad": "municipio", "direccion": "dirección", "abonado": "abonado",
             "contacto_nombre": "contacto", "contacto_telefono": "teléfono del contacto"}
UBICACION_POR_CONFIRMAR = "Por confirmar en la próxima visita"


def _encabezado(ws):
    """(n.º de fila, {columna: dato}) del primer renglón que tenga CLIENTE y SEDE."""
    for i, fila in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        mapa = {}
        for c, v in enumerate(fila):
            n = normalizar(v)
            if n in COLUMNAS and COLUMNAS[n] not in mapa.values():
                mapa[c] = COLUMNAS[n]
        if {"cliente", "sede"} <= set(mapa.values()):
            return i, mapa
    return None, {}


def leer(archivo):
    """Las filas (una por sede) como dicts. Las filas sin cliente o sin sede no entran."""
    from openpyxl import load_workbook

    wb = load_workbook(archivo, data_only=True, read_only=True)
    filas, vistas = [], set()
    for ws in wb.worksheets:
        inicio, mapa = _encabezado(ws)
        if not inicio:
            continue
        for n, fila in enumerate(ws.iter_rows(min_row=inicio + 1, values_only=True), start=inicio + 1):
            d = {campo: _texto(fila[c]) if c < len(fila) else "" for c, campo in mapa.items()}
            if not d.get("cliente") or not d.get("sede"):
                continue
            d["nit"] = nit_limpio(d.get("nit"))
            d["fila"] = n
            clave = (d["nit"] or clave_nombre(d["cliente"]), normalizar(d["sede"]))
            if clave not in vistas:  # la misma sede dos veces: cuenta una
                vistas.add(clave)
                filas.append(d)
    wb.close()
    return filas


def municipio(texto):
    """«PUERTO LOPEZ» → («Puerto Lopez», «Meta»)."""
    texto = re.sub(r"\s+", " ", texto or "").strip()
    return texto.title(), DEPARTAMENTO_DE.get(normalizar(texto), "")


def _frase(texto):
    """«11 CÁMARAS IP + 2 PTZ 4G» → «11 cámaras IP + 2 PTZ 4G»."""
    texto = (texto or "").lower()
    texto = re.sub(r"\b(ip|ptz|nvr|dvr|\d+g)\b", lambda m: m.group(1).upper(), texto)
    return texto[:1].upper() + texto[1:]


def equipos_de(d):
    """Los equipos que la fila dice que hay en la sede: panel de alarma y videovigilancia."""
    equipos = []
    if d.get("alarma"):
        partes = d["alarma"].split(None, 1)
        linea = " · ".join(x for x in (d.get("linea") and f"Línea {d['linea']}", d.get("operador"),
                                         d.get("tipo_linea") and d["tipo_linea"].capitalize()) if x)
        equipos.append({"sistema": "alarma", "tipo": "Panel de alarma", "marca": partes[0].upper(),
                        "modelo": partes[1] if len(partes) > 1 else "",
                        "caracteristicas": f"Comunicación: {linea}" if linea else ""})
    if d.get("video"):
        equipo, _, detalle = (x.strip() for x in d["video"].partition(" - "))
        partes = equipo.split()
        es_grabador = len(partes) > 1 and partes[1].upper() in ("NVR", "DVR", "XVR")
        equipos.append({"sistema": "cctv", "tipo": partes[1].upper() if es_grabador else "Sistema de videovigilancia",
                        "marca": partes[0].upper() if partes else "",
                        "modelo": " ".join(partes[2 if es_grabador else 1:]),
                        "caracteristicas": _frase(detalle)})
    return equipos


def plan(filas, con_equipos=True):
    """Qué haría la importación, sin guardar: clientes y sedes nuevos, los que se completan y los equipos."""
    from cronograma.models import EquipoInstalado

    por_nit, por_nombre = {}, {}
    for e in Empresa.objects.prefetch_related("encargados", "sedes"):
        if nit_limpio(e.nit):
            por_nit.setdefault(nit_limpio(e.nit), e)
        por_nombre.setdefault(clave_nombre(e.razon_social), e)
    existentes = set(EquipoInstalado.objects.exclude(estado=EquipoInstalado.Estado.RETIRADO)
                     .values_list("sede_id", "sistema", "tipo"))

    clientes, sedes = {}, []  # clave del cliente → resumen
    for d in filas:
        clave = d["nit"] or clave_nombre(d["cliente"])
        c = clientes.get(clave)
        if c is None:
            e = por_nit.get(d["nit"]) if d["nit"] else None
            if e is None:
                e = por_nombre.get(clave_nombre(d["cliente"]))
                if e is not None and d["nit"] and nit_limpio(e.nit) and nit_limpio(e.nit) != d["nit"]:
                    e = None  # mismo nombre y otro NIT: es otro cliente
            ciudad = municipio(d.get("ciudad"))[0]
            c = clientes[clave] = {"clave": clave, "empresa": e, "razon_social": d["cliente"].strip()[:200],
                                   "nit": d["nit"], "ciudad": ciudad, "cambios": [], "comerciales": [], "sedes": 0}
            if e is not None:
                if d["nit"] and not e.nit and not Empresa.objects.filter(nit=d["nit"]).exists():
                    c["cambios"].append("NIT")
                if ciudad and not e.ciudad:
                    c["cambios"].append("ciudad")
        if d.get("comercial"):
            c["comerciales"].append(d["comercial"])
        c["sedes"] += 1

        e = c["empresa"]
        sede = None
        if e is not None:
            sede = next((s for s in e.sedes.all() if normalizar(s.nombre) == normalizar(d["sede"])), None)
            if sede is None and d.get("abonado"):  # escrita distinto, pero con el mismo abonado: es la misma sede
                sede = next((s for s in e.sedes.all() if s.abonado and s.abonado.strip() == d["abonado"]), None)
        ciudad, departamento = municipio(d.get("ciudad"))
        datos = {"ciudad": ciudad[:100], "departamento": departamento, "direccion": d.get("direccion", "")[:200],
                 "abonado": d.get("abonado", "")[:20], "contacto_nombre": d.get("contacto", "").title()[:150],
                 "contacto_telefono": d.get("telefono", "")[:50]}
        s = {"cliente": clave, "fila": d, "sede": sede, "nombre": d["sede"].strip()[:150], "datos": datos, "cambios": []}
        if sede is not None:
            s["cambios"] = [campo for campo, valor in datos.items()
                            if valor and not getattr(sede, campo) and campo != "departamento"]
        s["que"] = [ETIQUETAS[c] for c in s["cambios"]]
        s["equipos"] = [x for x in equipos_de(d) if not (sede and (sede.pk, x["sistema"], x["tipo"]) in existentes)]
        sedes.append(s)

    for c in clientes.values():  # el comercial que más se repite en sus sedes
        nombres = [n for n in c["comerciales"] if n]
        c["comercial"] = max(set(nombres), key=nombres.count).title() if nombres else ""
        e = c["empresa"]
        if c["comercial"] and e is not None and any(a.activo for a in e.encargados.all()):
            c["comercial"] = ""  # ya tiene encargado: no se cambia
        elif c["comercial"] and e is not None:
            c["cambios"].append("comercial")

    nuevos_clientes = [c for c in clientes.values() if c["empresa"] is None]
    return {
        "total": len(filas), "clientes": list(clientes.values()),
        "nuevos_clientes": nuevos_clientes,
        "clientes_completar": [c for c in clientes.values() if c["empresa"] is not None and c["cambios"]],
        "nuevas_sedes": [s for s in sedes if s["sede"] is None],
        "sedes_completar": [s for s in sedes if s["sede"] is not None and s["cambios"]],
        "sedes_iguales": [s for s in sedes if s["sede"] is not None and not s["cambios"]],
        "sedes": sedes, "con_equipos": con_equipos,
        "equipos": sum(len(s["equipos"]) for s in sedes) if con_equipos else 0,
    }


@transaction.atomic
def aplicar(filas, usuario, con_equipos=True):
    """Crea y completa clientes, sedes, encargados y (si se puede) equipos. Devuelve el plan que se aplicó."""
    from cronograma.models import EquipoInstalado

    resumen = plan(filas, con_equipos)
    for c in resumen["clientes"]:
        e = c["empresa"]
        if e is None:
            e = c["empresa"] = Empresa.objects.create(
                razon_social=c["razon_social"], ciudad=c["ciudad"][:100],
                nit=c["nit"] if c["nit"] and not Empresa.objects.filter(nit=c["nit"]).exists() else None)
        else:
            if "NIT" in c["cambios"]:
                e.nit = c["nit"]
            if "ciudad" in c["cambios"]:
                e.ciudad = c["ciudad"][:100]
            e.save(update_fields=["nit", "ciudad"])
        if c["comercial"]:
            EncargadoCliente.objects.create(empresa=e, nombre=c["comercial"][:150], cargo="Comercial",
                                            notas="Cargado desde la relación de clientes y sedes en Excel.")
    empresas = {c["clave"]: c["empresa"] for c in resumen["clientes"]}
    for s in resumen["sedes"]:
        sede = s["sede"]
        if sede is None:
            sede = s["sede"] = Sede.objects.create(empresa=empresas[s["cliente"]], nombre=s["nombre"], **s["datos"])
        elif s["cambios"]:
            for campo in s["cambios"]:
                setattr(sede, campo, s["datos"][campo])
            if "ciudad" in s["cambios"] and s["datos"]["departamento"]:
                sede.departamento = s["datos"]["departamento"]
            sede.save()
        if con_equipos:
            for x in s["equipos"]:
                EquipoInstalado.objects.create(
                    sede=sede, ubicacion=UBICACION_POR_CONFIRMAR, creado_por=usuario,
                    observaciones="Cargado desde la relación de clientes y sedes en Excel.", **x)
    return resumen

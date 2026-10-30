"""Importa de la exportación SQL de Inventario_Global (phpMyAdmin) las herramientas por técnico, su historial de
entregas y las auditorías. Se puede simular: corre igual dentro de una transacción que se deshace al final.

Solo lee las sentencias CREATE TABLE e INSERT del archivo (nunca ejecuta el SQL).
"""
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounts.models import Usuario

from .models import AuditoriaHerramientas, DetalleAuditoria, EntregaHerramienta, Herramienta, normalizar

ORIGEN = "inventario_global"
ESTADOS = {"DISPONIBLE": "disponible", "EN USO": "en_uso", "EN REPARACION": "en_reparacion", "DANADA": "danada", "BAJA": "baja"}
ENCONTRADOS = {"EN USO": "en_uso", "EN REPARACION": "en_reparacion", "DANADA": "danada", "NO ENCONTRADA": "no_encontrada",
               "DISPONIBLE": "en_uso"}
RESULTADOS = {"CONFORME": "conforme", "CON NOVEDAD": "con_novedad", "NO ENCONTRADA": "no_encontrada",
              "DADA DE BAJA": "dada_de_baja"}
TABLAS = ("usuarios", "marcas", "herramientas", "entregas_herramientas", "auditorias_herramientas",
          "auditoria_herramientas_detalle")


class _Simulacion(Exception):
    pass


# --- Lectura del volcado SQL -------------------------------------------------------------------
def _valores(texto, i):
    """Lee una tupla «(…)» desde texto[i] == '(' y devuelve (lista de valores, posición siguiente)."""
    valores, actual, i = [], [], i + 1
    en_cadena, crudo = False, True
    while i < len(texto):
        c = texto[i]
        if en_cadena:
            if c == "\\" and i + 1 < len(texto):
                siguiente = texto[i + 1]
                actual.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(siguiente, siguiente))
                i += 2
                continue
            if c == "'" and texto[i + 1:i + 2] == "'":
                actual.append("'")
                i += 2
                continue
            if c == "'":
                en_cadena = False
            else:
                actual.append(c)
        elif c == "'":
            en_cadena, crudo, actual = True, False, []  # descarta el espacio que va después de la coma
        elif c in ",)":
            valor = "".join(actual)
            if crudo:
                valor = valor.strip()
                valor = None if valor.upper() == "NULL" else valor
            valores.append(valor)
            actual, crudo = [], True
            if c == ")":
                return valores, i + 1
        else:
            actual.append(c)
        i += 1
    raise ValueError("Una fila del INSERT está incompleta.")


def leer_volcado(texto, tablas=TABLAS):
    """{tabla: [dict(columna=valor)]} para las tablas pedidas."""
    columnas = {}
    for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?`?(\w+)`?\s*\((.*?)\)\s*ENGINE", texto, re.S | re.I):
        cols = re.findall(r"^\s*`?(\w+)`?\s+[a-zA-Z]", m.group(2), re.M)
        columnas[m.group(1)] = [c for c in cols if c.upper() not in ("PRIMARY", "UNIQUE", "KEY", "CONSTRAINT", "INDEX", "FOREIGN")]
    filas = defaultdict(list)
    for m in re.finditer(r"INSERT (?:IGNORE )?INTO `?(\w+)`?\s*(\(([^)]*)\))?\s*VALUES\s*", texto, re.I):
        tabla = m.group(1)
        if tabla not in tablas:
            continue
        nombres = [c.strip(" `") for c in m.group(3).split(",")] if m.group(3) else columnas.get(tabla, [])
        i = m.end()
        while i < len(texto) and texto[i] == "(":
            valores, i = _valores(texto, i)
            filas[tabla].append(dict(zip(nombres, valores)))
            while i < len(texto) and texto[i] in " \r\n\t,":
                i += 1
    return filas


def _entero(v, defecto=0):
    try:
        return int(Decimal(str(v)))
    except (InvalidOperation, TypeError, ValueError):
        return defecto


def _decimal(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _fecha(v):
    if not v:
        return timezone.now()
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return timezone.make_aware(datetime.strptime(str(v)[:19], formato))
        except ValueError:
            continue
    return timezone.now()


# --- Importación -------------------------------------------------------------------------------
class ImportadorHerramientas:
    def __init__(self, ruta):
        with open(ruta, encoding="utf-8", errors="replace") as f:
            self.datos = leer_volcado(f.read())
        self.informe = defaultdict(list)
        self.conteo = Counter()
        self.usuarios = list(Usuario.objects.exclude(rol="cliente"))

    def nota(self, seccion, texto):
        self.informe[seccion].append(texto)

    def persona(self, usuario_id):
        """El funcionario de Inventario_Global → (usuario de la plataforma o None, nombre)."""
        fila = self.funcionarios.get(str(usuario_id)) if usuario_id is not None else None
        if not fila:
            return None, ""
        nombre, cedula = (fila.get("nombre_completo") or "").strip(), re.sub(r"\D", "", fila.get("documento") or "")
        tokens = set(normalizar(nombre).split())
        usuario = next((u for u in self.usuarios if (cedula and u.documento == cedula)
                        or (len(tokens) >= 2 and set(normalizar(u.get_full_name()).split()) == tokens)), None)
        if usuario is None and nombre:
            self.nota("Técnicos que aún no tienen usuario en la plataforma (se asocian al crearlo con su cédula)",
                      f"{nombre}{' · C.C. ' + cedula if cedula else ''}")
        return usuario, nombre

    def ejecutar(self, simular=True):
        self.funcionarios = {str(f.get("usuario_id")): f for f in self.datos.get("usuarios", [])}
        marcas = {str(m.get("marca_id")): m.get("nombre") or "" for m in self.datos.get("marcas", [])}
        if not self.datos.get("herramientas"):
            self.nota("Aviso", "El archivo no trae filas de la tabla «herramientas».")
        try:
            with transaction.atomic():
                por_id = {}
                for f in self.datos.get("herramientas", []):
                    usuario, nombre = self.persona(f.get("usuario_id"))
                    estado = ESTADOS.get(normalizar(f.get("estado")), "disponible")
                    if f.get("estado") and normalizar(f.get("estado")) not in ESTADOS:
                        self.nota("Estados desconocidos (quedaron «Disponible»)", f"{f.get('nombre')}: «{f.get('estado')}»")
                    h = Herramienta.objects.create(
                        nombre=(f.get("nombre") or "Sin nombre")[:180], descripcion=(f.get("descripcion") or "")[:255],
                        marca=(marcas.get(str(f.get("marca_id"))) or f.get("marca") or "")[:120], serial=(f.get("serial") or "")[:120],
                        cantidad=max(_entero(f.get("cantidad"), 1), 0), estado=estado, tecnico=usuario,
                        tecnico_nombre="" if usuario else nombre, costo_compra=_decimal(f.get("costo_compra")),
                        importado_de=f"{ORIGEN}:herramientas:{f.get('herramienta_id')}")
                    por_id[str(f.get("herramienta_id"))] = h
                    self.conteo["herramientas"] += 1
                    self.conteo[f"· {h.get_estado_display()}"] += 1
                for f in self.datos.get("entregas_herramientas", []):
                    usuario, nombre = self.persona(f.get("tecnico_id"))
                    EntregaHerramienta.objects.create(
                        herramienta=por_id.get(str(f.get("herramienta_id"))), herramienta_nombre=(f.get("herramienta_nombre") or "")[:180],
                        tecnico=usuario, tecnico_nombre=(f.get("tecnico_nombre") or nombre)[:180], fecha=_fecha(f.get("entregado_en")),
                        registrado_por=(f.get("registrado_por") or "")[:180], importado_de=f"{ORIGEN}:entregas:{f.get('entrega_id')}")
                    self.conteo["entregas de herramientas"] += 1
                auditorias = {}
                for f in self.datos.get("auditorias_herramientas", []):
                    usuario, nombre = self.persona(f.get("tecnico_id"))
                    a = AuditoriaHerramientas.objects.create(
                        tecnico=usuario, tecnico_nombre=(f.get("tecnico_nombre") or nombre)[:180], fecha=_fecha(f.get("fecha_auditoria")),
                        auditor_nombre=(f.get("auditor") or "")[:180],
                        resultado="con_novedades" if _entero(f.get("novedades")) else "conforme",
                        total_herramientas=_entero(f.get("total_herramientas")), unidades_registradas=_entero(f.get("unidades_registradas")),
                        unidades_encontradas=_entero(f.get("unidades_encontradas")), novedades=_entero(f.get("novedades")),
                        nota_general=f.get("nota_general") or "", ip=(f.get("ip") or "")[:45],
                        importado_de=f"{ORIGEN}:auditorias:{f.get('auditoria_id')}")
                    auditorias[str(f.get("auditoria_id"))] = a
                    self.conteo["auditorías"] += 1
                for f in self.datos.get("auditoria_herramientas_detalle", []):
                    a = auditorias.get(str(f.get("auditoria_id")))
                    if a is None:
                        self.nota("Filas descartadas", f"Detalle de una auditoría que no está en el archivo (#{f.get('auditoria_id')})")
                        continue
                    DetalleAuditoria.objects.create(
                        auditoria=a, herramienta=por_id.get(str(f.get("herramienta_id"))),
                        herramienta_nombre=(f.get("herramienta_nombre") or "")[:180], marca=(f.get("marca") or "")[:120],
                        serial=(f.get("serial") or "")[:120], cantidad_registrada=_entero(f.get("cantidad_registrada")),
                        cantidad_encontrada=_entero(f.get("cantidad_encontrada")),
                        estado_registrado=ESTADOS.get(normalizar(f.get("estado_registrado")), ""),
                        estado_encontrado=ENCONTRADOS.get(normalizar(f.get("estado_encontrado")), "en_uso"),
                        resultado=RESULTADOS.get(normalizar(f.get("resultado")), "con_novedad"), nota=(f.get("nota") or "")[:500])
                    self.conteo["herramientas revisadas en auditorías"] += 1
                if simular:
                    raise _Simulacion
        except _Simulacion:
            pass
        return self

    def texto_informe(self):
        lineas = ["RESULTADO", *[f"  {k}: {v}" for k, v in self.conteo.items()], ""]
        for seccion, notas in self.informe.items():
            unicas = list(dict.fromkeys(notas))
            lineas += [f"{seccion} ({len(unicas)})", *[f"  - {n}" for n in unicas], ""]
        return "\n".join(lineas)

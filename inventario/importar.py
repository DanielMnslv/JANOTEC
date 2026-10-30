"""Importa el Google Sheets «INVENTARIO» (descargado como .xlsx) con todo su historial.

Fuentes (las respuestas de los formularios, que son los datos originales):
- RPTA_COMPRA_ACTIVOS: compras de activos (COMPRAS_ACTIVOS es una copia con 12 compras de menos y 8 valores ÷1000).
- RPTA_MOVIMIENTOS_ACTIVOS: movimientos de activos (cédula del responsable y NIT del cliente).
- INVENTARIO_ACTIVOS: solo para los activos que no tienen compra registrada y para comparar el estado final.
- RPTA_COMPRA_INSUMOS: compras de insumos (COMPRA_INSUMOS dejó de copiar las 9 compras del 24 al 31 de julio).
- SALIDA_INSUMOS: salidas de insumos de bodega a los clientes.
- INVENTARIO_INSUMO: catálogo de insumos y stock para comparar. CODIGOS DE ACTIVO, PROVEDORES, CLIENTE, BD_FUNCIONARIOS.

Siempre se puede simular: corre exactamente lo mismo dentro de una transacción que se deshace al final.
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from statistics import median

from django.db import transaction

from accounts.models import Empresa, Usuario

from . import servicios
from .models import (
    Activo, CodigoActivo, Insumo, Lugar, Modalidad, MovimientoActivo, MovimientoInsumo, Proveedor, normalizar,
)

T = MovimientoActivo.Tipo
E = Activo.Estado

# Escrituras del mismo funcionario que la búsqueda por nombre no puede decidir sola («PINZON» también es Tibamoso Pinzón)
ALIAS = {
    "PINZON": "PINZON VEGA GUSTAVO CRISTOPHER SEBASTIAN",
    "PINZON VEGA": "PINZON VEGA GUSTAVO CRISTOPHER SEBASTIAN",
    "CRISTOPHER PINZON": "PINZON VEGA GUSTAVO CRISTOPHER SEBASTIAN",
    "DANIEL GUANA": "GUANA PINEDA JUAN DANIEL",
}
# Juan Daniel Guana (coordinador) no está en BD_FUNCIONARIOS pero aparece como responsable
FUNCIONARIOS_EXTRA = [("", "GUANA PINEDA JUAN DANIEL", "COORDINADOR TIC", "TECNICO")]


class _Simulacion(Exception):
    pass


def _texto(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _fecha(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and v > 30000:  # número de serie de fecha de Excel
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(v))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", _texto(v))
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _numero(v):
    """1322.92 · «1322.92» · «1.322,92» · «$1.574,27» → Decimal."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    t = re.sub(r"[^\d,.\-]", "", str(v))
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _digitos(v):
    return re.sub(r"\D", "", _texto(v))


class Importador:
    def __init__(self, ruta):
        import openpyxl

        self.libro = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
        self.informe = defaultdict(list)
        self.conteo = Counter()

    # --- Lectura ---------------------------------------------------------------------
    def hoja(self, nombre):
        hoja = next(h for h in self.libro.worksheets if h.title.strip() == nombre)
        filas = hoja.iter_rows(values_only=True)
        enc = [_texto(x) for x in next(filas)]
        datos = []
        for n, fila in enumerate(filas, 2):
            if any(v not in (None, "") for v in fila):
                d = {h: (fila[i] if i < len(fila) else None) for i, h in enumerate(enc)}
                d["_fila"] = n
                datos.append(d)
        return datos

    def nota(self, seccion, texto):
        self.informe[seccion].append(texto)

    # --- Personas y clientes ---------------------------------------------------------
    def _cargar_funcionarios(self):
        self.funcionarios = []  # (cedula, nombre, tokens)
        for r in self.hoja("BD_FUNCIONARIOS"):
            self.funcionarios.append((_digitos(r["CEDULA"]), normalizar(r["NOMBRE"]), set(normalizar(r["NOMBRE"]).split())))
        for cedula, nombre, _cargo, _rol in FUNCIONARIOS_EXTRA:
            self.funcionarios.append((cedula, nombre, set(nombre.split())))
        self.usuarios = list(Usuario.objects.exclude(rol="cliente"))
        self._personas = {}

    @staticmethod
    def _parecido(a, b):
        return a == b or (len(a) > 4 and SequenceMatcher(None, a, b).ratio() >= 0.85)  # BLADIMIR ~ VLADIMIR

    def persona(self, texto):
        """«1022324125 ESPINOSA CALDERON JORGE ALBERTO», «VLADIMIR GORDILLO»… → (usuario, nombre, cédula)."""
        texto = _texto(texto)
        if texto in self._personas:
            return self._personas[texto]
        m = re.match(r"^(\d{6,})\s+(.*)$", texto)
        cedula, nombre = (m.group(1), normalizar(m.group(2))) if m else ("", normalizar(texto))
        nombre = ALIAS.get(nombre, nombre)
        encontrado = next((f for f in self.funcionarios if cedula and f[0] == cedula), None)
        if not encontrado:
            tokens = set(nombre.split())
            candidatos = [f for f in self.funcionarios
                          if tokens and all(any(self._parecido(t, u) for u in f[2]) for t in tokens)]
            if len(candidatos) == 1:
                encontrado = candidatos[0]
            elif texto:
                self.nota("Responsables que no son funcionarios de BD_FUNCIONARIOS (se guardan tal cual)",
                          f"«{texto}»" + (" — coincide con varios" if candidatos else ""))
        if encontrado:
            cedula, nombre = encontrado[0], encontrado[1]
        usuario = next((u for u in self.usuarios if (cedula and u.documento == cedula)
                        or set(normalizar(u.get_full_name()).split()) == set(nombre.split())), None)
        resultado = (usuario, nombre.title() if not usuario else str(usuario), cedula)
        self._personas[texto] = resultado
        return resultado

    def _cargar_clientes(self):
        self.empresas_por_nit = {}
        self.empresas_por_nombre = {normalizar(e.razon_social): e for e in Empresa.objects.all()}
        for e in Empresa.objects.exclude(nit__isnull=True).exclude(nit=""):
            d = _digitos(e.nit)
            self.empresas_por_nit[d[:9] if len(d) >= 10 and d.startswith(("8", "9")) else d] = e
        self._clientes = {}

    def cliente(self, texto):
        """«822002315 SEGURIDAD JANO LTDA» o el nombre → (empresa o None, texto)."""
        texto = _texto(texto)
        if texto in self._clientes:
            return self._clientes[texto]
        m = re.match(r"^(\d{6,})\s+(.*)$", texto)
        nit, nombre = (m.group(1), m.group(2).strip()) if m else ("", texto)
        empresa = self.empresas_por_nit.get(nit) or self.empresas_por_nombre.get(normalizar(nombre))
        if not empresa and nombre and normalizar(nombre) not in ("JANOTEC", "JANO TEC S A S", "BODEGA PRINCIPAL"):
            self.nota("Clientes del inventario que no están en la plataforma (se guardan como texto)", nombre)
        self._clientes[texto] = (empresa, nombre)
        return self._clientes[texto]

    @staticmethod
    def modalidad_de(estado, observacion):
        o = normalizar(observacion)
        if estado == E.ALQUILADO or "ALQUI" in o or "AQUIL" in o or "AQLUI" in o:
            return Modalidad.ALQUILER
        if "REINVER" in o:
            return Modalidad.REINVERSION
        if estado == E.VENDIDO or o.startswith("VENTA"):
            return Modalidad.VENTA
        if o.startswith(("MANTEN", "MATEN", "MANEN", "SOPORTE")):
            return Modalidad.MANTENIMIENTO
        if estado == E.ASIGNADO:
            return Modalidad.ASIGNACION
        return ""

    # --- Catálogos -------------------------------------------------------------------
    def catalogos(self):
        vistos = set()
        for r in self.hoja("PROVEDORES"):
            nombre = re.sub(r"\s+", " ", _texto(r["RAZON SOCIAL"]))
            if nombre and normalizar(nombre) not in vistos:
                vistos.add(normalizar(nombre))
                Proveedor.objects.update_or_create(razon_social=nombre, defaults={"nit": _digitos(r["NIT"])})
                self.conteo["proveedores"] += 1
        for r in self.hoja("CODIGOS DE ACTIVO"):
            codigo = _texto(r["CODIGO"]).upper()
            if not codigo:
                continue
            _, nuevo = CodigoActivo.objects.get_or_create(
                codigo=codigo, defaults={"descripcion": _texto(r["DESCRIPCION"])[:250], "marca": _texto(r["MARCA"]).title()})
            if not nuevo:
                self.nota("Códigos repetidos en el catálogo (se dejó el primero)", f"CODIGOS DE ACTIVO: {codigo}")
            self.conteo["códigos de activo"] += 1
        self.stock_hoja = {}
        for r in self.hoja("INVENTARIO_INSUMO"):
            codigo = _texto(r["CODIGO"]).upper()
            if not codigo:
                continue
            unidad = "Metro" if normalizar(r["UNIDAD DE MEDIDA"]).startswith("MET") else "Unidad"
            _, nuevo = Insumo.objects.get_or_create(codigo=codigo, defaults={
                "descripcion": re.sub(r"\s+", " ", _texto(r["DESCRIPCION"]))[:250], "unidad": unidad,
                "marca": _texto(r["MARCA"]).title()})
            if not nuevo:
                self.nota("Códigos repetidos en el catálogo (se dejó el primero)", f"INVENTARIO_INSUMO: {codigo} (fila {r['_fila']})")
                continue
            self.stock_hoja[codigo] = _numero(r["STOCK  ACTUAL DATA"]) or Decimal(0)
            self.conteo["insumos del catálogo"] += 1
        self.insumos = {i.codigo: i for i in Insumo.objects.all()}

    def _insumo(self, codigo, fila, hoja):
        codigo = _texto(codigo).upper()
        if codigo not in self.insumos:
            self.insumos[codigo] = Insumo.objects.create(codigo=codigo, descripcion=f"{codigo} (no estaba en INVENTARIO_INSUMO)")
            self.nota("Insumos que se usan pero no estaban en el catálogo INVENTARIO_INSUMO (se crearon)",
                      f"{codigo} — {hoja} fila {fila}: complete la descripción")
        return self.insumos[codigo]

    # --- Insumos ---------------------------------------------------------------------
    def insumos_movimientos(self):
        for r in self.hoja("RPTA_COMPRA_INSUMOS"):
            insumo = self._insumo(r["Código del Insumo"], r["_fila"], "RPTA_COMPRA_INSUMOS")
            usuario, nombre, _ = self.persona(r["Responsable de la Compra"])
            cantidad = _numero(r["Cantidad"])
            if not cantidad:
                self.nota("Filas descartadas", f"RPTA_COMPRA_INSUMOS fila {r['_fila']}: cantidad vacía o 0")
                continue
            MovimientoInsumo.objects.create(
                insumo=insumo, cantidad=cantidad, tipo=T.COMPRA, origen=Lugar.PROVEEDOR, destino=Lugar.BODEGA,
                fecha=_fecha(r["Digite la Fecha de compra"]), proveedor=_texto(r["Elija el Proveedor"]),
                factura=_texto(r["Número de Factura"]), valor_unitario=_numero(r["Valor Unitario sin I.V.A"]),
                iva=_numero(r["Valor de I.V.A aplicado"]), registrado_por=usuario, registrado_por_nombre=nombre,
                importado_de=f"RPTA_COMPRA_INSUMOS:{r['_fila']}")
            self.conteo["compras de insumos"] += 1
        for r in self.hoja("SALIDA_INSUMOS"):
            cantidad = _numero(r["CANTIDAD"])
            if not cantidad:
                self.nota("Filas descartadas", f"SALIDA_INSUMOS fila {r['_fila']}: cantidad 0 ({_texto(r['OBSERVACIONES'])})")
                continue
            insumo = self._insumo(r["CODIGO"], r["_fila"], "SALIDA_INSUMOS")
            tecnico, nombre_tec, cedula = self.persona(r["ENTREGADO_A"])
            registro, nombre_reg, _ = self.persona(r["RESPONSABLE"])
            empresa, cliente = self.cliente(r["DESTINO"])
            if not cliente:
                self.nota("Salidas sin cliente de destino (se importaron con la observación)",
                          f"SALIDA_INSUMOS fila {r['_fila']}: {insumo.codigo} × {cantidad:g} — {_texto(r['OBSERVACIONES'])}")
            MovimientoInsumo.objects.create(
                insumo=insumo, cantidad=cantidad, tipo=T.SALIDA, origen=Lugar.BODEGA, destino=Lugar.CLIENTE,
                fecha=_fecha(r["FECHA"]), tecnico=tecnico, tecnico_nombre=nombre_tec, tecnico_documento=cedula,
                empresa=empresa, cliente_texto=cliente, modalidad=self.modalidad_de("", r["OBSERVACIONES"]),
                observaciones=_texto(r["OBSERVACIONES"])[:500], registrado_por=registro, registrado_por_nombre=nombre_reg,
                importado_de=f"SALIDA_INSUMOS:{r['_fila']}")
            self.conteo["salidas de insumos"] += 1

    # --- Activos ---------------------------------------------------------------------
    def activos(self):
        compras = self.hoja("RPTA_COMPRA_ACTIVOS")
        series = Counter(_texto(r["Ingrese el Serial"]) for r in compras)
        for r in compras:
            placa = _texto(r["Ingresa la Placa Asignada"]).upper().replace(" ", "")
            serial = _texto(r["Ingrese el Serial"])
            if series[serial] > 1:
                self.nota("Seriales repetidos en dos placas (revisar en físico)", f"{serial}: {placa}")
            if Activo.objects.filter(placa=placa).exists():
                self.nota("Filas descartadas", f"RPTA_COMPRA_ACTIVOS fila {r['_fila']}: la placa {placa} ya estaba registrada")
                continue
            registro, nombre_reg, _ = self.persona(r["Usuario que realiza Ingreso"])
            _, nombre_compra, _ = self.persona(r["Responsable de la compra"])
            activo = Activo.objects.create(
                placa=placa, serial="" if serial == placa else serial, categoria=_texto(r["Categoría"]).upper(),
                tipo=_texto(r["TIPO"]), subtipo=_texto(r["Subtipo"]), marca=_texto(r["Marca"]),
                referencia=_texto(r["Referencia"]).replace(" ", ""), descripcion=_texto(r["Descripción"])[:300],
                fecha_compra=_fecha(r["Fecha de Compra"]) or _fecha(r["Marca temporal"]), proveedor=_texto(r["Elija el Proveedor"]),
                factura=_texto(r["Número de Factura"]), valor=_numero(r["Valor de Adquicisión sin I.V.A"]),
                iva=_numero(r["Valor de I.V.A aplicado"]) or 0, responsable_compra=nombre_compra)
            MovimientoActivo.objects.create(
                activo=activo, tipo=T.COMPRA, origen=Lugar.PROVEEDOR, destino=Lugar.BODEGA, estado=E.DISPONIBLE,
                fecha=activo.fecha_compra, proveedor=activo.proveedor, factura=activo.factura,
                observaciones=_texto(r["Observaciones"])[:500], registrado_por=registro, registrado_por_nombre=nombre_reg,
                importado_de=f"RPTA_COMPRA_ACTIVOS:{r['_fila']}")
            activo.fecha_ultimo_movimiento = activo.fecha_compra
            activo.save(update_fields=["fecha_ultimo_movimiento"])
            self.conteo["activos comprados"] += 1

        # Los que están en INVENTARIO_ACTIVOS sin compra registrada
        self.estado_hoja = {}
        for r in self.hoja("INVENTARIO_ACTIVOS"):
            placa = _texto(r["PLACA"]).upper().replace(" ", "")
            self.estado_hoja[placa] = (normalizar(r["ESTADO"]).lower(), _texto(r["UBICACIÓN ACTIVO (cliente)"]))
            if not Activo.objects.filter(placa=placa).exists():
                Activo.objects.create(
                    placa=placa, serial=_texto(r["SERIAL"]) if _texto(r["SERIAL"]) != placa else "",
                    categoria=_texto(r["CATEGORIA"]).upper(), tipo=_texto(r["TIPO"]), subtipo=_texto(r["SUBTIPO"]),
                    marca=_texto(r["MARCA"]), referencia=_texto(r["REFERENCIA"]), descripcion=_texto(r["DESCRIPCION"])[:300])
                self.nota("Activos sin compra registrada (se crearon desde INVENTARIO_ACTIVOS, sin valor)", placa)
                self.conteo["activos sin compra"] += 1

        detalle = {_texto(r["PLACA"]).upper().replace(" ", ""): r for r in self.hoja("MOVIMIENTOS_ACTIVOS")}
        movimientos = sorted(self.hoja("RPTA_MOVIMIENTOS_ACTIVOS"),
                             key=lambda r: (_fecha(r["Coloque la fecha del Movimeinto"]) or date.min, _texto(r["Marca temporal"])))
        estados = {"disponible": E.DISPONIBLE, "asignado": E.ASIGNADO, "alquilado": E.ALQUILADO, "vendido": E.VENDIDO,
                   "mantenimiento": E.MANTENIMIENTO, "dado de baja": E.BAJA, "no encontrado": E.NO_ENCONTRADO}
        for r in movimientos:
            placa = _texto(r["Escoja o escriba la placa"]).upper().replace(" ", "")
            activo = Activo.objects.filter(placa=placa).first()
            if activo is None:
                d = detalle.get(placa, {})
                activo = Activo.objects.create(placa=placa, serial=_texto(d.get("SERIAL")), categoria=_texto(d.get("CATEGORIA")),
                                               tipo=_texto(d.get("TIPO DE MEDIO TECNOLOGICO")), marca=_texto(d.get("MARCA")),
                                               descripcion=_texto(d.get("DESCRIPCION"))[:300])
                self.nota("Activos con movimientos pero sin compra ni registro (se crearon)", placa)
            estado = estados.get(normalizar(r["Escoja el estado nuevo para el activo"]).lower())
            if estado is None:
                self.nota("Filas descartadas", f"RPTA_MOVIMIENTOS_ACTIVOS fila {r['_fila']}: estado «{r['Escoja el estado nuevo para el activo']}»")
                continue
            observacion = _texto(r["Observaciones"])
            tecnico, nombre, cedula = self.persona(r["Escoja Responsable"])
            empresa, cliente = self.cliente(r["Escoja Cliente o empresa a quien se le hará el movimiento del activo"])
            modalidad = self.modalidad_de(estado, observacion)
            if estado == E.DISPONIBLE:
                destino, tipo = Lugar.BODEGA, (T.DEVOLUCION if activo.lugar != Lugar.BODEGA else T.AJUSTE)
            elif estado == E.ASIGNADO:
                destino, tipo = Lugar.TECNICO, T.ENTREGA
            elif estado in (E.ALQUILADO, E.VENDIDO):
                destino, tipo = Lugar.CLIENTE, (T.TRASLADO if activo.lugar == Lugar.CLIENTE else T.SALIDA)
            else:
                destino, tipo = {E.MANTENIMIENTO: (Lugar.BODEGA, T.MANTENIMIENTO), E.BAJA: (Lugar.FUERA, T.BAJA),
                                 E.NO_ENCONTRADO: (Lugar.FUERA, T.AJUSTE)}[estado]
            interno = normalizar(cliente) in ("JANOTEC", "JANO TEC S A S")
            servicios.mover_activo(
                activo, tipo, destino, estado=estado, tecnico=tecnico,
                fecha=_fecha(r["Coloque la fecha del Movimeinto"]) or _fecha(r["Marca temporal"]),
                tecnico_nombre=nombre, tecnico_documento=cedula, empresa=None if interno else empresa,
                cliente_texto="" if interno else cliente, modalidad=modalidad, observaciones=observacion[:500],
                registrado_por_nombre="Google Sheets", importado_de=f"RPTA_MOVIMIENTOS_ACTIVOS:{r['_fila']}")
            self.conteo["movimientos de activos"] += 1

    # --- Comprobaciones --------------------------------------------------------------
    def comprobar(self):
        # El estado final calculado contra INVENTARIO_ACTIVOS
        estados_hoja = {"disponible": E.DISPONIBLE, "asignado": E.ASIGNADO, "alquilado": E.ALQUILADO, "vendido": E.VENDIDO}
        faltan = 0
        for a in Activo.objects.all():
            en_hoja = self.estado_hoja.get(a.placa)
            if en_hoja is None:
                faltan += 1
            elif estados_hoja.get(en_hoja[0]) not in (None, a.estado):
                self.nota("Estado distinto al de INVENTARIO_ACTIVOS", f"{a.placa}: hoja «{en_hoja[0]}» · plataforma «{a.get_estado_display()}»")
        if faltan:
            self.nota("Resumen", f"{faltan} activos comprados no aparecían en INVENTARIO_ACTIVOS (la hoja solo tenía {len(self.estado_hoja)}): "
                                 "ahora todos tienen su estado calculado con los movimientos.")
        # Valores de compra fuera de lo normal para la misma referencia
        por_ref = defaultdict(list)
        for a in Activo.objects.exclude(valor__isnull=True).exclude(referencia=""):
            por_ref[a.referencia].append(a)
        for ref, lista in por_ref.items():
            if len(lista) < 3:
                continue
            m = median(a.valor for a in lista)
            for a in lista:
                if m and (a.valor < m / 5 or a.valor > m * 5):
                    self.nota("Valores de compra sospechosos (revisar la factura)",
                              f"{a.placa} ({ref}): ${a.valor:,.0f} cuando lo normal es ${m:,.0f}")
        # Stock de insumos calculado contra la hoja
        saldos = servicios.saldos_insumos()
        for codigo, insumo in self.insumos.items():
            calculado = saldos.get(insumo.pk, {}).get("bodega", Decimal(0))
            hoja = self.stock_hoja.get(codigo)
            if calculado < 0:
                self.nota("Insumos con stock negativo (salió más de lo que se registró que entró)",
                          f"{codigo} {insumo.descripcion[:45]}: {calculado:g}")
            if hoja is not None and calculado != hoja:
                self.nota("Stock de insumo distinto al de INVENTARIO_INSUMO",
                          f"{codigo}: hoja {hoja:g} · plataforma {calculado:g}")

    # --- Ejecución -------------------------------------------------------------------
    def ejecutar(self, simular=True):
        try:
            with transaction.atomic():
                self._cargar_funcionarios()
                self._cargar_clientes()
                self.catalogos()
                self.insumos_movimientos()
                self.activos()
                self.comprobar()
                self.conteo["activos en total"] = Activo.objects.count()
                for estado, n in Counter(Activo.objects.values_list("estado", flat=True)).most_common():
                    self.conteo[f"· {dict(E.choices)[estado]}"] = n
                if simular:
                    raise _Simulacion
        except _Simulacion:
            pass
        return self

    def texto_informe(self):
        lineas = ["RESULTADO", *[f"  {k}: {v}" for k, v in self.conteo.items()], ""]
        for seccion, notas in self.informe.items():
            unicas = list(dict.fromkeys(notas))
            lineas.append(f"{seccion} ({len(unicas)})")
            lineas += [f"  - {n}" for n in unicas]
            lineas.append("")
        return "\n".join(lineas)

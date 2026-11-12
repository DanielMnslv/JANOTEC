"""Lee la factura de un proveedor y saca lo que se necesita para darle ingreso a bodega.

Acepta tres formas:

* **XML de la factura electrónica (DIAN, UBL 2.1)** o el **ZIP** que mandan los proveedores con el PDF y el XML. Es la
  forma exacta: trae cada ítem con su código, cantidad, precio e IVA tal como lo reportaron a la DIAN. Si es un
  «AttachedDocument», la factura viene adentro y también se lee.
* **PDF**: se lee el texto (``pypdf``) y se buscan las filas de la tabla de ítems: una línea que empieza con la cantidad
  y termina con el valor unitario y el total. Las líneas siguientes que no empiezan con cantidad completan el código o la
  descripción según la columna en que caen. Sirve para las facturas de texto (Syscom, GVS…); una factura escaneada como
  imagen no tiene texto y no se puede leer.

Siempre es una propuesta: la persona revisa y corrige las filas antes de dar ingreso. Por eso se compara la suma de los
ítems con el subtotal de la factura y se avisa si no cuadra.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

NIT_JANOTEC = "901542984"
UNIDADES = re.compile(r"^(PIEZA|PZA|UND|UNID|UNIDAD|UN|U|EA|NIU|94|MT|MTS|METRO|METROS|MTR|ROLLO|RLL|CAJA|CJ|PAQ|PAQUETE|"
                      r"BOLSA|KIT|JGO|JUEGO|PAR|GLN|LT|KG|SERV|SERVICIO)\.?$", re.I)
# Texto de la tabla que no es parte de la descripción del producto
RUIDO = re.compile(r"ENTREGA INMEDIATA|CLAVE PRODUCTOS|^\s*[A-Z]{2}\d{5,}\*?\s*$|GARANT[IÍ]A|^\s*SERIAL", re.I)
FIN_ITEMS = re.compile(r"SUB[\s-]?TOTAL|TOTAL BRUTO|VALOR BRUTO|BASE GRAVABLE|TOTAL ITEMS|TOTAL L[IÍ]NEAS|OBSERVACIONES:", re.I)
DINERO = r"\$?\s?-?\d[\d.,]*"
EQUIPOS = re.compile(r"C[AÁ]MARA|DVR|NVR|XVR|TERMINAL|SWITCH|UPS|RADIO|ANTENA|DISCO|MONITOR|TELEVISOR|PANEL|ACCESS ?POINT|"
                     r"ROUTER|CONTROLADORA|LECTORA|BIOM[EÉ]TRIC|SIRENA|TECLADO|C[OÓ]MPUTADOR|PORT[AÁ]TIL|IMPRESORA|"
                     r"VIDEOPORTERO|DOMO|PTZ|GRABADOR|STARLINK|INVERSOR|BATER[IÍ]A", re.I)
PIDE_UNIDADES = re.compile(r"\b(METRO|METROS|MTS?|ROLLO|CAJA|PAQ|PAQUETE|BOLSA)\b", re.I)


class FacturaIlegible(Exception):
    """El archivo no es una factura que se pueda leer (formato no soportado, imagen sin texto, dañado)."""


@dataclass
class Item:
    cantidad: Decimal
    descripcion: str
    codigo: str = ""
    unidad: str = ""
    valor_unitario: Decimal | None = None
    importe: Decimal | None = None
    iva: Decimal | None = None


@dataclass
class Factura:
    numero: str = ""
    fecha: date | None = None
    proveedor: str = ""
    nit: str = ""
    subtotal: Decimal | None = None
    iva: Decimal | None = None
    retencion: Decimal | None = None
    total: Decimal | None = None
    items: list = field(default_factory=list)
    formato: str = ""

    @property
    def suma_items(self):
        return sum((i.importe or 0) for i in self.items)

    @property
    def cuadra(self):
        """La suma de los ítems da el subtotal (con $1.000 de margen por redondeos). None si no hay subtotal."""
        if not self.subtotal:
            return None
        return abs(self.suma_items - self.subtotal) <= 1000

    @property
    def tasa_iva(self):
        """El % de IVA de la factura (19, 5 o 0), sacado de los totales."""
        if self.subtotal and self.iva is not None:
            tasa = self.iva * 100 / self.subtotal
            for t in (19, 5, 0):
                if abs(tasa - t) < Decimal("0.6"):
                    return Decimal(t)
        return Decimal(19)


def numero(texto):
    """«578,485.85», «578.485,85», «$ 1.234», «1,000» → Decimal. None si no es un número."""
    s = re.sub(r"[^\d.,-]", "", str(texto or ""))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        partes = s.split(sep)
        if len(partes) == 2 and len(partes[1]) != 3:
            s = s.replace(sep, ".")
        else:
            s = s.replace(sep, "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def nit_limpio(texto):
    return re.sub(r"\D", "", str(texto or "").split("-")[0])


def leer(archivo, nombre=""):
    """Lee la factura (archivo abierto o bytes). Lanza FacturaIlegible si no se puede."""
    datos = archivo if isinstance(archivo, bytes) else archivo.read()
    nombre = (nombre or getattr(archivo, "name", "") or "").lower()
    if datos[:2] == b"PK" or nombre.endswith(".zip"):
        return _de_zip(datos)
    if datos.lstrip()[:1] == b"<" or nombre.endswith(".xml"):
        return _de_xml(datos)
    if datos[:4] == b"%PDF" or nombre.endswith(".pdf"):
        return _de_pdf(datos)
    raise FacturaIlegible("Suba la factura en PDF, XML o el ZIP que manda el proveedor.")


def _de_zip(datos):
    try:
        zf = zipfile.ZipFile(io.BytesIO(datos))
    except zipfile.BadZipFile as e:
        raise FacturaIlegible("El ZIP está dañado.") from e
    nombres = [n for n in zf.namelist() if not n.endswith("/") and zf.getinfo(n).file_size < 20 * 1024 * 1024]
    for n in nombres:
        if n.lower().endswith(".xml"):
            try:
                return _de_xml(zf.read(n))
            except FacturaIlegible:
                continue
    for n in nombres:
        if n.lower().endswith(".pdf"):
            return _de_pdf(zf.read(n))
    raise FacturaIlegible("El ZIP no trae el XML ni el PDF de la factura.")


# --- XML (UBL) --------------------------------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _hijo(nodo, *ruta):
    """Primer descendiente siguiendo nombres locales (sin importar el espacio de nombres)."""
    actual = [nodo]
    for nombre in ruta:
        siguiente = []
        for n in actual:
            siguiente += [h for h in n if _local(h.tag) == nombre]
        if not siguiente:
            return None
        actual = siguiente
    return actual[0]


def _texto(nodo, *ruta):
    n = _hijo(nodo, *ruta) if ruta else nodo
    return (n.text or "").strip() if n is not None else ""


def _de_xml(datos):
    try:
        raiz = ElementTree.fromstring(datos)
    except ElementTree.ParseError as e:
        raise FacturaIlegible("El XML está dañado.") from e
    if _local(raiz.tag) == "AttachedDocument":  # la factura viene adentro, como texto
        for n in raiz.iter():
            if _local(n.tag) == "Description" and n.text and "Invoice" in n.text:
                return _de_xml(n.text.strip().encode("utf-8"))
        raise FacturaIlegible("El XML no trae la factura adentro.")
    if _local(raiz.tag) not in ("Invoice", "CreditNote"):
        raise FacturaIlegible("El XML no es una factura electrónica.")
    f = Factura(formato="XML de la factura electrónica")
    f.numero = _texto(raiz, "ID")
    try:
        f.fecha = date.fromisoformat(_texto(raiz, "IssueDate"))
    except ValueError:
        f.fecha = None
    parte = _hijo(raiz, "AccountingSupplierParty", "Party")
    if parte is not None:
        f.proveedor = (_texto(parte, "PartyTaxScheme", "RegistrationName") or _texto(parte, "PartyLegalEntity", "RegistrationName")
                       or _texto(parte, "PartyName", "Name"))
        f.nit = nit_limpio(_texto(parte, "PartyTaxScheme", "CompanyID") or _texto(parte, "PartyLegalEntity", "CompanyID"))
    totales = _hijo(raiz, "LegalMonetaryTotal")
    if totales is not None:
        f.subtotal = numero(_texto(totales, "LineExtensionAmount"))
        f.total = numero(_texto(totales, "PayableAmount") or _texto(totales, "TaxInclusiveAmount"))
    f.iva = sum((numero(_texto(t, "TaxAmount")) or 0) for t in raiz if _local(t.tag) == "TaxTotal") or Decimal(0)
    retenciones = [numero(_texto(t, "TaxAmount")) or 0 for t in raiz if _local(t.tag) == "WithholdingTaxTotal"]
    f.retencion = sum(retenciones) if retenciones else None
    for linea in raiz:
        if _local(linea.tag) not in ("InvoiceLine", "CreditNoteLine"):
            continue
        cant_nodo = _hijo(linea, "InvoicedQuantity")
        if cant_nodo is None:  # ojo: un nodo sin hijos es «falso», por eso no se usa «or»
            cant_nodo = _hijo(linea, "CreditedQuantity")
        cantidad = numero(cant_nodo.text if cant_nodo is not None else "1") or Decimal(1)
        importe = numero(_texto(linea, "LineExtensionAmount"))
        unitario = numero(_texto(linea, "Price", "PriceAmount"))
        if not unitario and importe and cantidad:
            unitario = (importe / cantidad).quantize(Decimal("0.01"))
        item = _hijo(linea, "Item")
        codigo = ""
        if item is not None:
            codigo = (_texto(item, "SellersItemIdentification", "ID") or _texto(item, "StandardItemIdentification", "ID")
                      or _texto(item, "BuyersItemIdentification", "ID"))
        porcentaje = _texto(linea, "TaxTotal", "TaxSubtotal", "TaxCategory", "Percent")
        f.items.append(Item(cantidad=cantidad, descripcion=_texto(item, "Description") if item is not None else "",
                            codigo=codigo, unidad=cant_nodo.get("unitCode", "") if cant_nodo is not None else "",
                            valor_unitario=unitario, importe=importe,
                            iva=numero(porcentaje) if porcentaje else None))
    if not f.items:
        raise FacturaIlegible("La factura electrónica no trae ítems.")
    return f


# --- PDF --------------------------------------------------------------------------------------------------------

def _de_pdf(datos):
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - pypdf está en requirements
        raise FacturaIlegible("El lector de PDF no está instalado en el servidor.") from e
    try:
        lector = PdfReader(io.BytesIO(datos))
        paginas = [(p.extract_text(extraction_mode="layout") or "", p.extract_text() or "") for p in lector.pages[:15]]
    except Exception as e:  # noqa: BLE001 - PDF dañado o cifrado
        raise FacturaIlegible("No se pudo abrir el PDF.") from e
    plano = "\n".join(p[1] for p in paginas)
    if len(plano.strip()) < 40:
        raise FacturaIlegible("El PDF no tiene texto (es una imagen escaneada). Suba el XML o el ZIP de la factura electrónica.")
    f = Factura(formato="PDF")
    _cabecera_pdf(f, plano)
    for disposicion, _ in paginas:
        f.items += _items_pdf(disposicion)
    _totales_pdf(f, "\n".join(p[0] for p in paginas) + "\n" + plano)
    if not f.items:
        raise FacturaIlegible("No se encontraron los ítems en el PDF. Súbalo en XML o ZIP, o regístrelo a mano.")
    return f


def _cabecera_pdf(f, plano):
    m = (re.search(r"FACTURA[^\n]{0,40}?(?:N[OÚ]\.?|N[°º]|#|NUMERO)\s*[:.]?\s*([A-Z]{0,6}[- ]?\d{3,})", plano, re.I)
         or re.search(r"^\s*([A-Z]{1,6}-?\d{4,})\s*$", plano, re.M))
    if m:
        f.numero = m.group(1).replace(" ", "")
    m = re.search(r"FECHA(?: DE)?(?: EMISI[OÓ]N| FACTURA| EXPEDICI[OÓ]N)?\s*:?\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})", plano, re.I)
    if m:
        try:
            f.fecha = date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            pass
    else:
        m = re.search(r"FECHA[^\n]{0,30}?(\d{4})-(\d{2})-(\d{2})", plano, re.I)
        if m:
            f.fecha = date(int(m[1]), int(m[2]), int(m[3]))
    lineas = plano.splitlines()
    for i, linea in enumerate(lineas):
        m = re.search(r"NIT\.?\s*:?\s*([\d.]{6,15})(?:\s*-\s*\d)?", linea, re.I)
        if m and nit_limpio(m.group(1)) != NIT_JANOTEC:
            f.nit = nit_limpio(m.group(1))
            antes = linea[:m.start()].strip(" :")
            if len(antes) > 3:
                f.proveedor = antes
            else:
                anteriores = [x.strip() for x in lineas[max(0, i - 3):i] if x.strip()]
                f.proveedor = anteriores[-1] if anteriores else ""
            break


def _items_pdf(texto):
    """Filas de ítems en el texto con disposición (columnas alineadas)."""
    items, actual, columnas = [], None, {}
    fila = re.compile(rf"^\s*(\d{{1,6}}(?:[.,]\d{{1,3}})?)\s{{2,}}(.+?)\s+({DINERO})\s+({DINERO})\s*$")
    for linea in texto.splitlines():
        if FIN_ITEMS.search(linea):
            break
        m = fila.match(linea)
        if m and numero(m.group(4)) and numero(m.group(4)) >= 100:
            actual = _nuevo_item(m, linea)
            columnas = actual.pop("_columnas")
            items.append(actual)
            continue
        if actual is None or not linea.strip() or RUIDO.search(linea):
            continue
        for trozo in re.finditer(r"\S+(?: \S+)*", linea):
            texto_trozo = trozo.group(0)
            if RUIDO.search(texto_trozo):
                continue
            columna = min(columnas.items(), key=lambda c: abs(c[1] - trozo.start()))[0] if columnas else "descripcion"
            if columna == "codigo" and " " not in texto_trozo and len(texto_trozo) <= 12:
                actual["codigo"] += texto_trozo
            elif columna == "descripcion" and len(actual["descripcion"]) < 200:
                actual["descripcion"] += " " + texto_trozo
    resultado = []
    for d in items:
        cantidad, unitario, importe = d["cantidad"], d["unitario"], d["importe"]
        if cantidad and importe and (not unitario or abs(unitario * cantidad - importe) > 1):
            unitario = (importe / cantidad).quantize(Decimal("0.01"))
        resultado.append(Item(cantidad=cantidad, descripcion=re.sub(r"\s+", " ", d["descripcion"]).strip()[:250],
                              codigo=d["codigo"][:60], unidad=d["unidad"], valor_unitario=unitario, importe=importe))
    return resultado


def _nuevo_item(m, linea):
    cantidad = numero(m.group(1).replace(",", ".")) or Decimal(1)
    medio = m.group(2)
    base = m.start(2)
    trozos = [(t.group(0), base + t.start()) for t in re.finditer(r"\S+(?: \S+)*", medio)]
    unidad, codigo, descripcion, columnas = "", "", "", {}
    for texto, pos in trozos:
        if texto in ("--", "-", "$") or numero(texto) is not None and re.fullmatch(DINERO, texto):
            continue
        if not unidad and UNIDADES.match(texto):
            unidad, columnas["unidad"] = texto, pos
        elif not codigo and " " not in texto and re.search(r"\d", texto) and re.search(r"[A-Z]", texto, re.I) and len(texto) >= 4:
            codigo, columnas["codigo"] = texto, pos
        elif len(texto) > len(descripcion):
            descripcion, columnas["descripcion"] = texto, pos
    if not descripcion and codigo:
        descripcion = codigo
    return {"cantidad": cantidad, "unidad": unidad, "codigo": codigo, "descripcion": descripcion,
            "unitario": numero(m.group(3)), "importe": numero(m.group(4)), "_columnas": columnas}


def _totales_pdf(f, texto):
    def buscar(patron):
        m = re.search(patron + rf"[^\n\d$]{{0,60}}({DINERO})", texto, re.I)
        return numero(m.group(1)) if m else None

    f.subtotal = buscar(r"SUB[\s-]?TOTAL")
    f.iva = buscar(r"(?<!RET\. )(?<!RETE)I\.?\s?V\.?\s?A\.?(?!\s*RET)")
    retenciones = [numero(m.group(1)) or 0 for m in re.finditer(rf"RET(?:\.|ENCI[OÓ]N)?\s*(?:EN LA )?(?:FTE|FUENTE)[^\n\d$]{{0,60}}({DINERO})", texto, re.I)]
    f.retencion = retenciones[0] if retenciones else None
    m = list(re.finditer(rf"(?<!SUB-)(?<!SUB )\bTOTAL(?: A PAGAR| FACTURA)?\s*:?\s*\$?\s*({DINERO})", texto, re.I))
    f.total = numero(m[-1].group(1)) if m else None
    if f.total is None:
        m = re.search(rf"TOTAL\s*\n[^\n]*\n\s*({DINERO})", texto)
        f.total = numero(m.group(1)) if m else None


# --- De la factura a las filas de ingreso ------------------------------------------------------------------------

def clave(texto):
    """«DS-K1T344MBFWX-E1» y «DSK1T344MBFWXE1» son la misma referencia."""
    return re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())


def sugerir_fila(item, tasa_iva):
    """Cómo entraría a bodega: como un insumo que ya existe, como un equipo conocido o según lo que parece."""
    from .models import Activo, Insumo

    fila = {"descripcion": item.descripcion[:250], "cantidad": item.cantidad, "valor_unitario": item.valor_unitario,
            "iva": item.iva if item.iva is not None else tasa_iva, "clase": "insumo"}
    codigo = clave(item.codigo)
    insumo = Insumo.objects.filter(codigo__iexact=item.codigo).first() if item.codigo else None
    if insumo:
        return {**fila, "codigo": insumo.codigo, "unidad": insumo.unidad, "conocido": f"Material {insumo.codigo} del catálogo"}
    if codigo:
        for a in Activo.objects.exclude(referencia="").only("placa", "referencia", "marca").order_by("-id")[:5000]:
            if clave(a.referencia) == codigo:
                return {**fila, "clase": "equipo", "referencia": a.referencia, "marca": a.marca,
                        "prefijo": Activo.prefijo(a.placa), "conocido": f"Equipo {a.referencia} (como la placa {a.placa})"}
    es_equipo = bool(EQUIPOS.search(item.descripcion)) and not PIDE_UNIDADES.search(item.unidad or "")
    if es_equipo and item.cantidad == item.cantidad.to_integral_value():
        fila = {**fila, "clase": "equipo", "referencia": item.codigo[:80]}
        codigo_placa = sugerir_codigo_placa(item.descripcion)
        if codigo_placa:
            fila.update(prefijo=codigo_placa.codigo, conocido=f"Código de placa sugerido: {codigo_placa} (revíselo)")
        return fila
    return {**fila, "codigo": item.codigo.upper()[:40], "unidad": _unidad(item.unidad)}


VACIAS = {"DE", "CON", "PARA", "LOS", "LAS", "DEL", "POR", "SIN", "UNA", "UNO", "QUE", "TIPO", "COLOR"}


def _palabras(texto):
    return {p for p in clave_palabras(texto).split() if len(p) >= 3 and p not in VACIAS}


def sugerir_codigo_placa(descripcion):
    """El código de placa del catálogo que más se parece a la descripción (al menos 2 palabras en común y sin empate)."""
    from .models import CodigoActivo

    buscadas = _palabras(descripcion)
    puntajes = sorted(((len(buscadas & _palabras(c.descripcion)), c) for c in CodigoActivo.objects.all()),
                      key=lambda x: -x[0])
    if puntajes and puntajes[0][0] >= 2 and (len(puntajes) == 1 or puntajes[1][0] < puntajes[0][0]):
        return puntajes[0][1]
    return None


def _unidad(texto):
    from .models import Insumo

    t = (texto or "").upper()
    return Insumo.Unidad.METRO if re.match(r"^(MT|MTS|METRO|METROS|MTR)", t) else Insumo.Unidad.UNIDAD


def emparejar(orden, factura):
    """Filas para recibir una orden con su factura: cada ítem de la factura se pone en la línea de la orden que le
    corresponde (por código o referencia, o por descripción parecida). Las líneas de la orden que no vienen en la factura
    quedan como «No llegó»; los ítems de la factura que no estaban en la orden quedan como adicionales."""
    from .recepcion import filas_iniciales

    referencias = dict(orden.lineas.values_list("pk", "referencia"))
    pendientes = [{**f, "_ref": referencias.get(f["linea"], "")} for f in filas_iniciales(orden) if f.get("linea")]
    usados, filas = set(), []
    tasa = factura.tasa_iva
    for item in factura.items:
        mejor, puntaje = None, 0
        for i, f in enumerate(pendientes):
            if i in usados:
                continue
            p = _parecido(item, f)
            if p > puntaje:
                mejor, puntaje = i, p
        if mejor is not None and puntaje >= 0.5:
            usados.add(mejor)
            f = {k: v for k, v in pendientes[mejor].items() if k != "_ref"}
            f.update(cantidad=item.cantidad, valor_unitario=item.valor_unitario,
                     iva=item.iva if item.iva is not None else tasa, conocido="En la orden y en la factura")
            if f.get("clase") == "equipo" and not f.get("referencia"):
                f["referencia"] = item.codigo[:80]
            filas.append(f)
        else:
            sugerida = sugerir_fila(item, tasa)
            nota = "Viene en la factura pero no estaba en la orden"
            filas.append({**sugerida, "conocido": f"{nota} · {sugerida['conocido']}" if sugerida.get("conocido") else nota})
    for i, f in enumerate(pendientes):
        if i not in usados:
            f = {k: v for k, v in f.items() if k != "_ref"}
            filas.append({**f, "clase": "no", "conocido": "Estaba en la orden y no aparece en la factura"})
    return filas


def _parecido(item, fila):
    codigos = {clave(fila.get("codigo")), clave(fila.get("referencia")), clave(fila.get("_ref"))} - {""}
    if item.codigo and clave(item.codigo) in codigos:
        return 1.0
    a = set(re.findall(r"[A-Z0-9]{3,}", clave_palabras(item.descripcion)))
    b = set(re.findall(r"[A-Z0-9]{3,}", clave_palabras(fila.get("descripcion"))))
    if item.codigo and any(clave(item.codigo) in clave(x) for x in b):
        return 0.9
    return len(a & b) / max(1, min(len(a), len(b)))


def clave_palabras(texto):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9 ]", " ", t)

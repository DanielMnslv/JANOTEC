"""Lee la «Tabla de equipos y materiales requeridos» de un informe técnico subido como archivo (Word o PDF).

Cuando el informe no se armó en la plataforma sino que se subió un archivo, con esto «Si pasa a comercial» se llena
solo con lo que ya dice el informe: nadie vuelve a escribir los equipos y materiales. Siempre es una propuesta que
la persona revisa antes de pasarla a comercial.

- Word (.docx): se leen las tablas del documento (sin librerías externas: el .docx es un zip con XML). Se toma la que
  tenga las columnas de cantidad y descripción; referencia y marca, si las tiene.
- PDF: se lee el texto (con ``pypdf``, si está instalado) entre el título de la tabla y la siguiente sección.
"""
import re
import unicodedata
import zipfile
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Palabras que delatan una fila de mano de obra (va como servicio, sin referencia ni marca)
MANO_DE_OBRA = re.compile(r"mano de obra|instalaci|configuraci|t[eé]cnico|desmonte|mantenimiento|servicio|programaci|"
                          r"puesta en marcha|cableado estructurado|dias?\b|horas?\b", re.I)
VACIO = {"", "-", "—", "–", "n/a", "na", "no aplica"}
FIN_TABLA = re.compile(r"^(justificaci|anexo|conclusion|recomendacion|garant|introducci|actividades)", re.I)


class LecturaNoDisponible(Exception):
    """El archivo no se puede leer (formato no soportado o lector de PDF no instalado)."""


def _llano(texto):
    """Minúsculas y sin tildes, para comparar encabezados."""
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def _fila(cantidad, descripcion, referencia="", marca=""):
    """Una fila con el formato de la tabla de requeridos; la mano de obra se marca como servicio."""
    cantidad, descripcion = " ".join((cantidad or "").split()), " ".join((descripcion or "").split())
    referencia = "" if _llano(referencia) in VACIO else " ".join(referencia.split())
    marca = "" if _llano(marca) in VACIO else " ".join(marca.split())
    servicio = not referencia and not marca and bool(MANO_DE_OBRA.search(descripcion))
    return {"cantidad": "1" if servicio else (cantidad or "1"), "descripcion": descripcion[:250],
            "referencia": "" if servicio else referencia[:80], "marca": "" if servicio else marca[:80], "servicio": servicio}


# --- Word -------------------------------------------------------------------------------------------
def _tablas_docx(archivo):
    """Las tablas del documento Word como listas de filas con el texto de cada celda."""
    try:
        with zipfile.ZipFile(archivo) as z:
            raiz = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise LecturaNoDisponible("El archivo Word no se pudo abrir.") from e
    tablas = []
    for tabla in raiz.iter(f"{W}tbl"):
        filas = []
        for tr in tabla.findall(f"{W}tr"):
            celdas = []
            for tc in tr.findall(f"{W}tc"):
                parrafos = ["".join(t.text or "" for t in p.iter(f"{W}t")) for p in tc.findall(f"{W}p")]
                celdas.append(" ".join(x for x in parrafos if x.strip()))
            filas.append(celdas)
        tablas.append(filas)
    return tablas


def _columnas(encabezado):
    """Posición de cada columna según el encabezado (cantidad, descripción, referencia, marca)."""
    cols = {}
    for i, celda in enumerate(map(_llano, encabezado)):
        if "cant" in celda and "cantidad" not in cols:
            cols["cantidad"] = i
        elif "descrip" in celda and "descripcion" not in cols:
            cols["descripcion"] = i
        elif ("refer" in celda or "modelo" in celda) and "imagen" not in celda and "referencia" not in cols:
            cols["referencia"] = i
        elif "marca" in celda and "marca" not in cols:
            cols["marca"] = i
    return cols


def leer_docx(archivo):
    """Filas de la tabla de equipos y materiales de un .docx (la primera tabla con cantidad y descripción)."""
    for tabla in _tablas_docx(archivo):
        for n, encabezado in enumerate(tabla[:3]):
            cols = _columnas(encabezado)
            if "cantidad" in cols and "descripcion" in cols:
                salida = []
                for celdas in tabla[n + 1:]:
                    def celda(nombre):
                        i = cols.get(nombre)
                        return celdas[i] if i is not None and i < len(celdas) else ""
                    if celda("descripcion").strip() and _llano(celda("descripcion")) not in VACIO:
                        salida.append(_fila(celda("cantidad"), celda("descripcion"), celda("referencia"), celda("marca")))
                return salida
    return []


# --- PDF --------------------------------------------------------------------------------------------
LINEA_PDF = re.compile(r"^(\d{1,3})\s+(\d+(?:[.,]\d+)?(?:\s*(?:m|mt|mts|metros|und|un|rollos?)\b)?)\s+(.+)$", re.I)


def texto_pdf(archivo):
    """Todo el texto del PDF (requiere pypdf)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise LecturaNoDisponible("Por ahora solo se leen informes en Word (.docx). Escriba la tabla o suba el Word.") from e
    try:
        return "\n".join(p.extract_text() or "" for p in PdfReader(archivo).pages)
    except Exception as e:  # noqa: BLE001 — PDF dañado o protegido
        raise LecturaNoDisponible("El PDF no se pudo leer.") from e


def leer_texto(texto):
    """Filas de la tabla a partir del texto plano: lo que hay entre «equipos y materiales requeridos» y la sección
    siguiente. Cada fila empieza con su número y la cantidad; las líneas sueltas continúan la descripción anterior."""
    lineas = [" ".join(l.split()) for l in (texto or "").splitlines()]
    inicio = next((i for i, l in enumerate(lineas) if "equipos y materiales" in _llano(l)), None)
    if inicio is None:
        return []
    filas, actual = [], None
    for linea in lineas[inicio + 1:]:
        if not linea:
            continue
        if FIN_TABLA.match(_llano(linea)):
            break
        llano = _llano(linea)
        if llano.startswith("num") and "cant" in llano:
            continue  # encabezado
        m = LINEA_PDF.match(linea)
        if m:
            actual = [m.group(2), m.group(3)]
            filas.append(actual)
        elif actual is not None:
            actual[1] += " " + linea
    return [_fila(cant, desc) for cant, desc in filas]


def _fragmentos_pdf(archivo):
    """Cada trozo de texto del PDF con su posición: (página, y desde arriba, x, texto), en orden de lectura."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise LecturaNoDisponible("Por ahora solo se leen informes en Word (.docx). Escriba la tabla o suba el Word.") from e
    trozos = []
    try:
        for n, pagina in enumerate(PdfReader(archivo).pages):
            alto = float(pagina.mediabox.height)

            def visitar(texto, cm, tm, _fuente, _tamano, n=n, alto=alto):
                if texto and texto.strip():
                    # posición real = matriz del texto × matriz de la página
                    x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                    y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                    trozos.append((n, round(alto - y, 1), round(x, 1), " ".join(texto.split())))
            pagina.extract_text(visitor_text=visitar)
    except LecturaNoDisponible:
        raise
    except Exception as e:  # noqa: BLE001 — PDF dañado o protegido
        raise LecturaNoDisponible("El PDF no se pudo leer.") from e
    return sorted(trozos)


def leer_pdf(archivo):
    """Filas de la tabla de un PDF ubicando cada texto en su columna por la posición de los encabezados
    (NUM, CANT, DESCRIPCIÓN, REFERENCIA, MARCA, IMAGEN). Así sirven las celdas que ocupan varias líneas.

    Si no encuentra los encabezados, intenta leerla como texto corrido (``leer_texto``)."""
    trozos = _fragmentos_pdf(archivo)
    desc = next((t for t in trozos if _llano(t[3]).startswith("descripci")), None)
    if desc is None:
        return leer_texto("\n".join(t[3] for t in trozos))
    pag, y_desc = desc[0], desc[1]
    # El encabezado: los textos de esa misma franja (las celdas del encabezado pueden ocupar dos líneas)
    banda = [t for t in trozos if t[0] == pag and abs(t[1] - y_desc) <= 14]
    inicio = {"descripcion": desc[2]}
    for _, _, x, texto in banda:
        llano = _llano(texto)
        if llano.startswith("can"):
            inicio.setdefault("cantidad", x)
        elif llano.startswith("marca"):
            inicio.setdefault("marca", x)
        elif llano.startswith("imagen"):
            inicio.setdefault("imagen", x)
        elif llano.startswith(("referencia", "modelo")):
            inicio["referencia"] = min(x, inicio.get("referencia", x))
    if "cantidad" not in inicio:
        return leer_texto("\n".join(t[3] for t in trozos))
    inicio["num"] = min(t[2] for t in banda)
    if "imagen" in inicio and inicio.get("referencia", 0) >= inicio["imagen"]:
        inicio.pop("referencia")  # el «REFERENCIA» de «IMAGEN DE REFERENCIA»
    columnas = sorted(inicio.items(), key=lambda c: c[1])
    fin_encabezado = max(t[1] for t in banda)

    def columna(x):
        actual = columnas[0][0]
        for nombre, desde in columnas:
            if x >= desde - 3:
                actual = nombre
        return actual

    # 1) Lo que hay entre el encabezado y la sección siguiente (o el primer texto a la izquierda que no es número)
    tabla = []
    for t in trozos:
        if (t[0], t[1]) <= (pag, fin_encabezado) or (t[0] > pag and abs(t[1] - y_desc) <= 14
                                                     and _llano(t[3]).startswith(("descripci", "num", "nu", "can"))):
            continue  # antes de la tabla, o el encabezado repetido en otra página
        if FIN_TABLA.match(_llano(t[3])) or (columna(t[2]) == "num" and not t[3].isdigit()):
            break
        tabla.append(t)
    # 2) Cada fila la marca su número (columna NUM). Las celdas suelen ir centradas en vertical, así que parte del
    #    texto queda encima del número: cada texto va a la fila cuyo número le queda más cerca.
    numeros = [t for t in tabla if columna(t[2]) == "num"]
    if not numeros:
        return []
    filas = [{"cantidad": [], "descripcion": [], "referencia": [], "marca": []} for _ in numeros]
    for t in tabla:
        col = columna(t[2])
        if col in filas[0]:
            cerca = min(range(len(numeros)), key=lambda i: (abs((numeros[i][0] - t[0]) * 1000 + numeros[i][1] - t[1]), i))
            filas[cerca][col].append(t[3])
    # La referencia se parte a mitad de palabra al no caber: se une sin espacios; lo demás, con espacio
    return [_fila(" ".join(f["cantidad"]), " ".join(f["descripcion"]), "".join(f["referencia"]), " ".join(f["marca"]))
            for f in filas if f["descripcion"]]


def leer(archivo, nombre):
    """Filas de la tabla de equipos y materiales del archivo subido (Word o PDF)."""
    nombre = (nombre or "").lower()
    if nombre.endswith(".docx"):
        return leer_docx(archivo)
    if nombre.endswith(".pdf"):
        return leer_pdf(archivo)
    raise LecturaNoDisponible("Solo se leen informes en Word (.docx) o PDF.")

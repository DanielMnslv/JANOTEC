"""PDF del acta de soporte, instalación y desinstalación (23/11/2026).

Antes el acta se imprimía desde el navegador y el resultado dependía del equipo: en el celular salían las tablas
apiladas y la barra de accesos. Ahora el PDF se arma en el servidor con el membrete y la estética del informe técnico
y de la propuesta: cuadro de datos, secciones en barras oliva, tablas de equipos, anexo fotográfico y firmas.
"""
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ImagenPDF
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from comercial.pdf import BORDE, E, E_ETIQUETA, GRIS, OLIVA, TINTE_VERDE, _esc, _p

from .informe import E_TEXTO, _anexo_fotos, _fecha_larga, _seccion

E_CELDA = ParagraphStyle("celda_acta", parent=E, fontSize=8, leading=10)
E_CABECERA = ParagraphStyle("cab_acta", parent=E_ETIQUETA, textColor=colors.white, fontSize=8, leading=10)
E_CENTRO = ParagraphStyle("centro_acta", parent=E_CELDA, alignment=TA_CENTER)


def _datos(acta, ancho):
    """Cuadro con los datos de la visita: número, fecha, cliente, sede, ticket, actividad, técnico y quién recibió."""
    fecha = timezone.localtime(acta.fecha)
    direccion = acta.sede.direccion if acta.sede and acta.sede.direccion else ""
    tecnico = (acta.tecnico.get_full_name() if acta.tecnico else "") or acta.tecnico_texto.title() or "—"
    recibio = acta.recibio.title() + (f" ({acta.cargo_recibio})" if acta.cargo_recibio else "") if acta.recibio else "—"
    filas = [("Acta N.°", str(acta.numero), "Fecha", f"{_fecha_larga(fecha)} · {fecha:%I:%M %p}".replace("AM", "a. m.").replace("PM", "p. m.")),
             ("Cliente", str(acta.empresa), "NIT", acta.empresa.nit or "—"),
             ("Sede", acta.lugar or "—", "Ciudad", acta.ciudad or (acta.sede.ciudad if acta.sede else "") or "—"),
             ("Dirección", direccion or "—", "Ticket", acta.ticket.radicado if acta.ticket else "Trabajo no programado"),
             ("Actividad", acta.actividad or "—", "Sistema", acta.get_sistema_display_corto() if acta.sistema else "—"),
             ("Modalidad", acta.get_modalidad_display() or "—", "Resultado", acta.get_resultado_display() or "—"),
             ("Técnico", tecnico, "Recibió", recibio)]
    cuerpo = [[_p(a, E_ETIQUETA), _p(b), _p(c, E_ETIQUETA), _p(d)] for a, b, c, d in filas]
    tabla = Table(cuerpo, colWidths=[ancho * x for x in (0.14, 0.38, 0.12, 0.36)])
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (0, -1), TINTE_VERDE),
                               ("BACKGROUND", (2, 0), (2, -1), TINTE_VERDE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return tabla


def _tabla(filas_encabezado, filas, anchos, ancho):
    """Tabla con encabezado oliva y filas alternadas, como las del informe."""
    cuerpo = [[Paragraph(_esc(x), E_CABECERA) for x in filas_encabezado]] + filas
    tabla = Table(cuerpo, colWidths=[ancho * x for x in anchos], repeatRows=1)
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), OLIVA),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, TINTE_VERDE]),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return tabla


def _tabla_equipos(equipos, ancho, material=False):
    """Los equipos de una lista del acta: equipo (con lo que se hizo y de quién es), marca, referencia, serial,
    cantidad y observaciones."""
    filas = []
    for i, e in enumerate(equipos, 1):
        detalle = " · ".join(x for x in (e.get_trabajo_display() if e.trabajo else "",
                                         e.get_propiedad_display() if e.propiedad else "",
                                         f"placa {e.placa}" if e.placa else "") if x)
        nombre = _esc(e.equipo or "—") + (f"<br/><font color='#686975' size='7'>{_esc(detalle)}</font>" if detalle else "")
        filas.append([_p(i, E_CENTRO), Paragraph(nombre, E_CELDA), _p(e.marca or "—", E_CELDA),
                      _p(e.referencia or "—", E_CELDA), _p(e.serial or "—", E_CELDA), _p(e.cantidad or "1", E_CENTRO),
                      _p(e.observaciones or "", E_CELDA)])
    titulo = "MATERIAL O EQUIPO" if material else "EQUIPO"
    return _tabla(("N.°", titulo, "MARCA", "REFERENCIA", "SERIAL", "CANT.", "OBSERVACIONES"), filas,
                  (0.05, 0.24, 0.11, 0.14, 0.14, 0.08, 0.24), ancho)


def _firma(firma, ancho_celda):
    """La firma dibujada en el celular sobre fondo blanco (el trazo llega en PNG transparente)."""
    try:
        with firma.imagen.open("rb") as f:
            img = ImageOps.exif_transpose(Image.open(f))
            img.load()
    except Exception:  # noqa: BLE001 — una firma dañada no detiene el acta
        return None
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGB", img.size, "white")
        fondo.paste(img, mask=img.split()[-1])
        img = fondo
    else:
        img = img.convert("RGB")
    img.thumbnail((700, 300))
    salida = BytesIO()
    img.save(salida, "PNG")
    salida.seek(0)
    escala = min((ancho_celda - 10) / img.width, 22 * mm / img.height)
    return ImagenPDF(salida, width=img.width * escala, height=img.height * escala)


def _firmas(acta, ancho):
    """Las firmas en columnas: la imagen sobre la línea, el nombre, el cargo y el papel (técnico, cliente…)."""
    orden = {"tecnico": 0, "acompanante": 1}  # primero JANOTEC, luego el cliente
    firmas = sorted(acta.firmas.all(), key=lambda f: orden.get(f.rol, 2))
    if not firmas:
        return None
    columnas = min(3, len(firmas))
    col = ancho / columnas
    nombre = ParagraphStyle("firma_nombre", parent=E_CELDA, alignment=TA_CENTER, fontName="Helvetica-Bold")
    pie = ParagraphStyle("firma_pie", parent=E_CELDA, alignment=TA_CENTER, textColor=GRIS, fontSize=7.5)
    celdas = []
    for f in firmas:
        imagen = _firma(f, col) or Spacer(1, 22 * mm)
        bloque = Table([[imagen], [Paragraph(_esc(f.nombre.title()), nombre)],
                        [Paragraph(_esc(" · ".join(x for x in (f.cargo.capitalize() if f.cargo else "", f.get_rol_display()) if x)), pie)]],
                       colWidths=[col - 8], rowHeights=[24 * mm, None, None])
        bloque.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (0, 0), "BOTTOM"),
                                    ("LINEBELOW", (0, 0), (0, 0), 0.8, GRIS),
                                    ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        celdas.append(bloque)
    filas = [celdas[i:i + columnas] + [""] * (columnas - len(celdas[i:i + columnas])) for i in range(0, len(celdas), columnas)]
    tabla = Table(filas, colWidths=[col] * columnas)
    tabla.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return tabla


def pdf(acta):
    """(nombre, bytes) del acta con el membrete de JANOTEC: datos de la visita, diagnóstico, trabajo realizado, las
    listas de equipos, el material del inventario, el anexo fotográfico y las firmas."""
    from inventario.models import MovimientoActivo, MovimientoInsumo

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=31 * mm,
                            bottomMargin=27 * mm, title=f"Acta de soporte {acta.numero}", author="JANOTEC S.A.S.")
    ancho = doc.width
    titulo = ParagraphStyle("titulo_acta", parent=E_ETIQUETA, fontSize=14, leading=18, alignment=TA_CENTER, textColor=OLIVA)
    subtitulo = ParagraphStyle("subtitulo_acta", parent=E, fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=GRIS)
    h = [Paragraph("ACTA DE SOPORTE, INSTALACIÓN Y DESINSTALACIÓN", titulo), Spacer(1, 2),
         Paragraph(f"Formulario N.° {acta.numero} · Versión 1 · Fecha de vigencia: 22/06/2026", subtitulo),
         Spacer(1, 8), _datos(acta, ancho)]
    n = 0

    def seccion(texto):
        nonlocal n
        n += 1
        return _seccion(f"{n}. {texto}", ancho)

    h += [seccion("DIAGNÓSTICO"), Paragraph(_esc(acta.diagnostico or "—"), E_TEXTO),
          seccion("TRABAJO REALIZADO Y OBSERVACIONES"), Paragraph(_esc(acta.observaciones or "—"), E_TEXTO)]
    for lista, nombre, material in ((acta.equipos_intervenidos, "EQUIPOS INTERVENIDOS", False),
                                    (acta.equipos_utilizados, "MATERIALES Y EQUIPOS UTILIZADOS", True),
                                    (acta.equipos_retirados, "EQUIPOS RETIRADOS", False),
                                    (acta.equipos_requeridos, "MATERIALES Y EQUIPOS REQUERIDOS", True)):
        if lista:
            h += [seccion(nombre), _tabla_equipos(lista, ancho, material)]

    propuesta = list(acta.material_propuesta.all())
    if propuesta:
        filas = [[_p(m.descripcion + (f" · {m.referencia}" if m.referencia else ""), E_CELDA),
                  _p(f"{m.propuesta:g}", E_CENTRO), _p(f"{m.usado:g}", E_CENTRO), _p(f"{m.sobrante:g}", E_CENTRO)]
                 for m in propuesta]
        h += [seccion("MATERIAL DE LA PROPUESTA APROBADA"),
              _tabla(("MATERIAL O EQUIPO", "PROPUESTA", "USÓ", "SOBRÓ"), filas, (0.58, 0.14, 0.14, 0.14), ancho)]

    activos = MovimientoActivo.objects.filter(acta=acta).select_related("activo")
    insumos = MovimientoInsumo.objects.filter(acta=acta).select_related("insumo")
    if activos or insumos:
        filas = [[_p(m.activo.placa, E_CELDA), _p(m.activo.descripcion_corta + (f" · S/N {m.activo.serial}" if m.activo.serial else ""), E_CELDA),
                  _p("1", E_CENTRO)] for m in activos]
        filas += [[_p(m.insumo.codigo, E_CELDA), _p(m.insumo.descripcion, E_CELDA),
                   _p(f"{m.cantidad:g} {m.insumo.get_unidad_display().lower()}", E_CENTRO)] for m in insumos]
        h += [seccion("MATERIAL DEL INVENTARIO DESCONTADO"),
              _tabla(("PLACA O CÓDIGO", "DESCRIPCIÓN", "CANTIDAD"), filas, (0.2, 0.62, 0.18), ancho)]

    celdas = []
    for e in acta.equipos.all():
        celdas += [(foto, " ".join(x for x in (e.equipo, e.marca, e.referencia) if x)) for foto in e.fotos.all()]
    celdas += [(ev, ev.get_etapa_display()) for ev in acta.evidencias.all() if ev.es_imagen]
    anexo = _anexo_fotos(celdas, ancho)
    if anexo is not None:
        h += [seccion("REGISTRO FOTOGRÁFICO"), anexo]

    firmas = _firmas(acta, ancho)
    cierre = [seccion("ASISTENTES Y FIRMAS")]
    if firmas is not None:
        cierre.append(firmas)
    notas = []
    if acta.es_virtual:
        notas.append("Atención virtual: no lleva firma del cliente.")
    if acta.copia_correo:
        notas.append(f"Copia del acta enviada a: {acta.copia_correo}")
    if acta.latitud and acta.longitud:
        notas.append(f"Ubicación registrada en la visita: {acta.latitud}, {acta.longitud}")
    cierre += [Paragraph(_esc(x), ParagraphStyle("nota_acta", parent=E_CELDA, textColor=GRIS)) for x in notas]
    if firmas is not None or notas:
        h.append(KeepTogether(cierre))

    marca = settings.BASE_DIR / "static" / "img" / "marca"

    def pagina(canvas, documento):
        """El membrete de JANOTEC en cada hoja y el número del acta con la página."""
        canvas.saveState()
        canvas.drawImage(str(marca / "membrete-inferior.jpg"), 1.5, -9.84, width=609.9, height=540.84)
        canvas.drawImage(str(marca / "membrete-superior.jpg"), -0.55, 651.7, width=612.55, height=150.75)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(GRIS)
        canvas.drawRightString(letter[0] - 28 * mm, letter[1] - 27 * mm, f"Acta {acta.numero} · Página {documento.page}")
        canvas.restoreState()

    doc.build(h, onFirstPage=pagina, onLaterPages=pagina)
    return f"Acta_{acta.numero}.pdf", buf.getvalue()

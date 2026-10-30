"""PDF «Ficha de requerimiento» (el mismo formato que usaba el área comercial en la ficha de Netlify).

Se adjunta al correo de radicación y se descarga desde la solicitud para compartirla por WhatsApp.
Hay dos versiones: la del cliente (sin datos internos de la venta) y la interna del área comercial.
"""
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ImagenPDF, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

GRIS = colors.HexColor("#686975")          # gris corporativo
OSCURO = colors.HexColor("#4e4f59")
VERDE = colors.HexColor("#acc812")         # verde corporativo
VERDE_TEXTO = colors.HexColor("#637309")
BORDE = colors.HexColor("#d9dce3")
TINTE_VERDE = colors.HexColor("#f8fbe8")

EMPRESA = "JANOTEC S.A.S."
LINEAS_EMPRESA = ["Ingeniería en telecomunicaciones y seguridad electrónica",
                  "Carrera 40A N.º 16-21, Barrio Villa María · Villavicencio", "Tel: 310 229 8160",
                  "janotec@janotec.app · @janotecsas"]
PIE = "Cra 40A N.º 16-21, Barrio Villa María · Villavicencio · Tel: 310 229 8160 · janotec@janotec.app · @janotecsas"
CONSTANCIA = ("Constancia de recepción: JANOTEC S.A.S. confirma la recepción del presente requerimiento bajo el "
              "radicado indicado. Nuestro equipo de ingeniería se pondrá en contacto con el solicitante para coordinar "
              "la visita técnica de diagnóstico sin costo, dentro del horario de atención registrado. Posterior a la "
              "visita se remitirá la propuesta técnico-económica formal.")

E = ParagraphStyle("base", fontName="Helvetica", fontSize=8.5, leading=11)
E_ETIQUETA = ParagraphStyle("etiqueta", parent=E, fontName="Helvetica-Bold")
E_ETIQUETA_OP = ParagraphStyle("etiqueta_op", parent=E_ETIQUETA, textColor=VERDE_TEXTO)
E_SECCION = ParagraphStyle("seccion", parent=E, fontName="Helvetica-Bold", textColor=colors.white)
E_DERECHA = ParagraphStyle("derecha", parent=E, alignment=TA_RIGHT, textColor=GRIS, leading=11.5)


def _esc(texto):
    texto = "" if texto in (None, "") else str(texto)
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _p(texto, estilo=E):
    return Paragraph(_esc(texto) or "—", estilo)


def _fecha(valor):
    return timezone.localtime(valor).strftime("%d/%m/%Y %H:%M") if valor else ""


def _logo():
    ruta = settings.BASE_DIR / "static" / "img" / "marca" / "logo-color.png"
    try:
        img = Image.open(ruta).convert("RGBA")
    except OSError:
        return ""
    fondo = Image.new("RGB", img.size, "white")
    fondo.paste(img, mask=img.split()[3])
    png = BytesIO()
    fondo.save(png, "PNG")
    png.seek(0)
    ancho = 48 * mm
    return ImagenPDF(png, width=ancho, height=ancho * img.height / img.width)


def calificacion(ficha):
    """Qué tan madura está la oportunidad, a partir de lo que registró el asesor."""
    puntos = 0
    puntos += {"decide": 2, "influye": 1}.get(ficha.nivel_decision, 0)
    puntos += 1 if ficha.presupuesto.strip() else 0
    puntos += 1 if ficha.ticket.impacto == "alta" else 0
    puntos += 1 if len(ficha.servicios) > 1 or ficha.otras_sedes else 0
    if puntos >= 4:
        return "Oportunidad caliente"
    if puntos >= 2:
        return "Oportunidad tibia"
    return "Oportunidad por calentar"


def _seccion(titulo, filas, color=GRIS, oportunidad=False):
    estilo_etiqueta = E_ETIQUETA_OP if oportunidad else E_ETIQUETA
    # Sobre el verde corporativo el título va en gris oscuro: el blanco no se lee
    estilo_titulo = ParagraphStyle("seccion_verde", parent=E_SECCION, textColor=OSCURO) if color == VERDE else E_SECCION
    datos = [[_p(titulo.upper(), estilo_titulo), ""]] + [[_p(e, estilo_etiqueta), _p(v)] for e, v in filas]
    tabla = Table(datos, colWidths=[52 * mm, None])
    estilos = [
        ("SPAN", (0, 0), (1, 0)), ("BACKGROUND", (0, 0), (1, 0), color),
        ("GRID", (0, 1), (-1, -1), 0.5, BORDE), ("BOX", (0, 0), (-1, -1), 0.5, BORDE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    if oportunidad:
        estilos.append(("BACKGROUND", (0, 1), (-1, -1), TINTE_VERDE))
    tabla.setStyle(TableStyle(estilos))
    return [KeepTogether(tabla), Spacer(1, 4 * mm)]


def ficha_pdf(ficha, interna=False):
    """Devuelve (nombre_archivo, bytes) de la ficha. `interna` agrega los datos que el cliente no ve."""
    t = ficha.ticket
    sede = t.sede
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=20 * mm, title=f"Ficha de requerimiento {t.radicado}", author=EMPRESA)
    historia = []

    encabezado = Table([[_logo(), [_p(EMPRESA, ParagraphStyle("emp", parent=E, fontName="Helvetica-Bold", fontSize=12,
                                                               alignment=TA_RIGHT, textColor=OSCURO, spaceAfter=4))]
                         + [_p(linea, E_DERECHA) for linea in LINEAS_EMPRESA]]], colWidths=[70 * mm, None])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    historia += [encabezado, Spacer(1, 3 * mm)]
    franja = Table([["", ""]], colWidths=["67%", "33%"], rowHeights=[1.6 * mm])
    franja.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), VERDE), ("BACKGROUND", (1, 0), (1, 0), GRIS)]))
    historia += [franja, Spacer(1, 5 * mm)]

    historia += [
        _p("FICHA DE REQUERIMIENTO", ParagraphStyle("titulo", parent=E, fontName="Helvetica-Bold", fontSize=16,
                                                    leading=20, textColor=OSCURO)),
        _p("Captura inicial de requerimiento comercial · soporte de radicación", ParagraphStyle("sub", parent=E, textColor=GRIS)),
        _p("Tecnología que protege lo que más te importa.", ParagraphStyle("lema", parent=E, fontName="Helvetica-BoldOblique",
                                                                           textColor=VERDE_TEXTO)),
        Spacer(1, 3 * mm),
    ]
    asesor = (ficha.comercial.get_full_name() or ficha.comercial.username) if ficha.comercial else "—"
    caja = Table([[Paragraph(f"<b>Radicado:</b> {_esc(t.radicado)}", E), Paragraph(f"<b>Fecha:</b> {_fecha(t.creado)}", E),
                   Paragraph(f"<b>Comercial:</b> {_esc(asesor)}", E)]], colWidths=["38%", "30%", "32%"])
    caja.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, VERDE), ("TOPPADDING", (0, 0), (-1, -1), 6),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    historia += [caja, Spacer(1, 4 * mm)]

    no_atiende = t.resumen_no_disponible
    horario = "; ".join(x for x in [f"No puede atender: {no_atiende}" if no_atiende else "", t.restricciones] if x)
    contacto = [
        ("Nombre del contacto", t.contacto_nombre), ("Cargo / rol", ficha.contacto_cargo),
        ("Empresa / entidad", t.empresa.razon_social), ("Teléfono / WhatsApp", t.contacto_telefono),
        ("Correo electrónico", t.contacto_email), ("Municipio / ubicación", ficha.municipio or (sede.nombre if sede else "")),
        ("Dirección", ficha.direccion or (sede.direccion if sede else "")), ("Ciudad", sede.ciudad if sede else ""),
        ("Horario de atención", horario),
    ]
    if interna:
        contacto.append(("Correo del comercial (copia interna)", ficha.comercial.email if ficha.comercial else ""))
    historia += _seccion("1 · Contacto y horario de atención", contacto)

    historia += _seccion("2 · Necesidad", [("Requerimiento del cliente", t.asunto),
                                           ("Descripción", t.descripcion if t.descripcion != t.asunto else ""),
                                           ("Urgencia", t.get_impacto_display() if t.impacto else "")])

    adjuntos = list(t.adjuntos.filter(es_propuesta=False))
    fotos = [a for a in adjuntos if a.es_imagen]
    historia += _seccion("3 · Calificación técnica preliminar", [
        ("Sistema actual", ficha.sistema_actual), ("Energía en el sitio", ficha.get_energia_display()),
        ("Conectividad a internet", ficha.get_internet_display()), ("Tamaño / puntos aprox.", ficha.tamano),
        ("Registro fotográfico", "Sí" if fotos else "No"),
    ])

    oportunidad = [("Servicios de interés", ", ".join(ficha.servicios_display)),
                   ("Otras necesidades", ficha.otras_necesidades), ("Otras sedes / fincas", "Sí" if ficha.otras_sedes else "No")]
    if interna:
        oportunidad[2:2] = [("Nivel del contacto", ficha.get_nivel_decision_display())]
        oportunidad += [("Presupuesto / tiempos", ficha.presupuesto), ("Calificación de la oportunidad", calificacion(ficha).upper())]
    historia += _seccion("4 · Oportunidad de negocio" + (" (interno)" if interna else ""), oportunidad, color=VERDE,
                         oportunidad=True)

    historia += _seccion("5 · Visita y seguimiento", [
        ("Fecha/hora máx. propuesta económica", _fecha(ficha.fecha_max_propuesta)),
        ("Fecha/hora de la visita técnica", _fecha(t.visita_programada)),
        ("Fotos adjuntas", len(fotos)), ("Anexos adjuntos", len(adjuntos) - len(fotos)),
    ])

    informe = getattr(ficha, "informe", None) if ficha.informe_listo else None
    if informe:
        historia += _seccion("6 · Informe del requerimiento" + (" (con visita)" if informe.con_visita else " (sin visita)"), [
            ("Qué quiere y necesita el cliente", informe.necesidad), ("Solución recomendada", informe.solucion),
            ("Servicios a cotizar", informe.servicios), ("Materiales y equipos a suministrar", informe.materiales),
            ("Condiciones del sitio", informe.observaciones),
        ])

    constancia = Table([[_p(CONSTANCIA)]], colWidths=[None])
    constancia.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, VERDE), ("BACKGROUND", (0, 0), (-1, -1), TINTE_VERDE),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                    ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    firmas = Table([[HRFlowable(width="100%", color=GRIS, thickness=0.5), "",
                     HRFlowable(width="100%", color=GRIS, thickness=0.5)],
                    [_p("Elaboró · Asesor comercial", ParagraphStyle("f", parent=E, textColor=GRIS)), "",
                     _p("Recibió · Cliente", ParagraphStyle("f2", parent=E, textColor=GRIS))]],
                   colWidths=["40%", "20%", "40%"])
    historia += [KeepTogether([constancia, Spacer(1, 14 * mm), firmas])]

    def pie(canvas, documento):
        canvas.saveState()
        ancho, _ = letter
        canvas.setStrokeColor(VERDE)
        canvas.setLineWidth(1.2)
        canvas.line(16 * mm, 15 * mm, ancho - 16 * mm, 15 * mm)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColor(OSCURO)
        canvas.drawString(16 * mm, 11.5 * mm, EMPRESA)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRIS)
        canvas.drawString(16 * mm + canvas.stringWidth(EMPRESA + "   ", "Helvetica-Bold", 7), 11.5 * mm, PIE)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(16 * mm, 8.5 * mm, f"Documento generado automáticamente · Radicado {t.radicado}")
        canvas.drawRightString(ancho - 16 * mm, 11.5 * mm, f"Pág. {documento.page}")
        canvas.restoreState()

    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    sufijo = "-interna" if interna else ""
    return f"Ficha-requerimiento-{t.radicado}{sufijo}.pdf", buffer.getvalue()


def _pesos(valor):
    return f"${valor:,.0f}".replace(",", ".")


def propuesta_pdf(cotizacion, codigo, numero, borrador=False):
    """PDF de la propuesta económica armada en el cotizador. Devuelve (nombre_archivo, bytes)."""
    from reportlab.lib.enums import TA_CENTER

    ficha = cotizacion.ficha
    t = ficha.ticket
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=20 * mm, title=f"Propuesta económica {codigo}", author=EMPRESA)
    derecha = ParagraphStyle("der", parent=E, alignment=TA_RIGHT)
    centro = ParagraphStyle("cen", parent=E, alignment=TA_CENTER)
    negrita_der = ParagraphStyle("negder", parent=derecha, fontName="Helvetica-Bold")
    blanco = ParagraphStyle("blanco", parent=E_ETIQUETA, textColor=colors.white)
    historia = []
    encabezado = Table([[_logo(), [_p(EMPRESA, ParagraphStyle("emp", parent=E, fontName="Helvetica-Bold", fontSize=12,
                                                               alignment=TA_RIGHT, textColor=OSCURO, spaceAfter=4))]
                         + [_p(linea, E_DERECHA) for linea in LINEAS_EMPRESA]]], colWidths=[70 * mm, None])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    franja = Table([["", ""]], colWidths=["67%", "33%"], rowHeights=[1.6 * mm])
    franja.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), VERDE), ("BACKGROUND", (1, 0), (1, 0), GRIS)]))
    historia += [encabezado, Spacer(1, 3 * mm), franja, Spacer(1, 5 * mm)]
    historia += [
        _p("PROPUESTA ECONÓMICA" + (" · BORRADOR" if borrador else ""),
           ParagraphStyle("titulo", parent=E, fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=OSCURO)),
        _p(f"{codigo} · versión {numero}", ParagraphStyle("cod", parent=E, fontName="Helvetica-Bold", textColor=VERDE_TEXTO)),
        Spacer(1, 3 * mm),
    ]
    asesor = (ficha.comercial.get_full_name() or ficha.comercial.username) if ficha.comercial else "—"
    caja = Table([
        [Paragraph(f"<b>Cliente:</b> {_esc(t.empresa.razon_social)}" + (f" · NIT {_esc(t.empresa.nit)}" if t.empresa.nit else ""), E),
         Paragraph(f"<b>Fecha:</b> {timezone.localdate():%d/%m/%Y}", E)],
        [Paragraph(f"<b>Sede:</b> {_esc(t.sede or '—')}", E), Paragraph(f"<b>Radicado:</b> {_esc(t.radicado)}", E)],
        [Paragraph(f"<b>Atención:</b> {_esc(t.contacto_nombre or '—')}", E), Paragraph(f"<b>Asesor:</b> {_esc(asesor)}", E)],
    ], colWidths=["62%", "38%"])
    caja.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, VERDE), ("TOPPADDING", (0, 0), (-1, -1), 4),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    historia += [caja, Spacer(1, 4 * mm)]
    if cotizacion.alcance.strip():
        historia += _seccion("Alcance", [("Requerimiento", t.asunto), ("Qué incluye", cotizacion.alcance)])

    filas = [[_p("N.º", blanco), _p("Descripción", blanco), _p("Cant.", blanco), _p("Vlr. unitario", blanco),
              _p("Desc.", blanco), _p("Vlr. total antes de IVA", blanco)]]
    for i, linea in enumerate(cotizacion.lineas.all(), 1):
        c = linea.calculo()
        filas.append([_p(i, centro), _p(linea.descripcion), _p(f"{linea.cantidad.normalize():f}", centro),
                      _p(_pesos(linea.valor_unitario), derecha), _p(f"{linea.descuento.normalize():f}%" if linea.descuento else "—", centro),
                      _p(_pesos(c["base"]), derecha)])
    tabla = Table(filas, colWidths=[10 * mm, None, 14 * mm, 28 * mm, 15 * mm, 32 * mm], repeatRows=1)
    tabla.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GRIS), ("GRID", (0, 0), (-1, -1), 0.5, BORDE),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f7f9")])]))
    historia += [tabla, Spacer(1, 3 * mm)]

    tot = cotizacion.totales()
    resumen = [[_p("Subtotal", E_ETIQUETA), _p(_pesos(tot["bruto"]), derecha)]]
    if tot["descuento"]:
        resumen += [[_p("Descuento", E_ETIQUETA), _p("−" + _pesos(tot["descuento"]), derecha)],
                    [_p("Subtotal con descuento", E_ETIQUETA), _p(_pesos(tot["base"]), derecha)]]
    M = cotizacion.Modalidad
    monitoreo, alquiler = cotizacion.modalidad == M.MONITOREO, cotizacion.modalidad == M.ALQUILER
    utilidad = f"{Decimal(str(cotizacion.aiu_utilidad)).normalize():f}"
    resumen += [[_p(f"IVA (utilidad {utilidad} %)" if monitoreo else "IVA", E_ETIQUETA), _p(_pesos(tot["iva"]), derecha)],
                [_p("VALOR MENSUAL" if monitoreo else "VALOR DE LOS BIENES" if alquiler else "TOTAL",
                    ParagraphStyle("tt", parent=E_ETIQUETA, fontSize=10)),
                 _p(_pesos(tot["total"]), ParagraphStyle("tv", parent=negrita_der, fontSize=10))]]
    caja_tot = Table(resumen, colWidths=[45 * mm, 35 * mm], hAlign="RIGHT")
    caja_tot.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, -1), (-1, -1), TINTE_VERDE),
                                  ("LINEABOVE", (0, -1), (-1, -1), 1.2, VERDE)]))
    historia += [KeepTogether([caja_tot])]
    if tot["descuento"]:
        historia += [Spacer(1, 2 * mm), _p(f"Ahorro para el cliente: {_pesos(tot['ahorro'])} (IVA incluido)", negrita_der)]
    if monitoreo:
        historia += [Spacer(1, 3 * mm), _p(
            f"Valor mensual del servicio, con AIU: el IVA del 19 % se liquida sobre la utilidad ({utilidad} %). "
            "Incluye únicamente el servicio descrito; no incluye equipos ni su instalación. La tarifa se incrementa al "
            "inicio de cada vigencia fiscal de acuerdo con el incremento del salario mínimo.")]
    if alquiler:
        c = cotizacion.canones()
        filas_c = [[_p("Plazo", blanco), _p("Canon mensual", blanco), _p("Total del contrato", blanco)]]
        filas_c += [[_p(f"{f['plazo']} meses", centro), _p(_pesos(f["canon"]), negrita_der), _p(_pesos(f["total"]), derecha)]
                    for f in c["filas"]]
        tabla_c = Table(filas_c, colWidths=[30 * mm, 40 * mm, 40 * mm], hAlign="RIGHT")
        tabla_c.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GRIS), ("GRID", (0, 0), (-1, -1), 0.5, BORDE)]))
        historia += [Spacer(1, 4 * mm), _p("ALQUILER CON OPCIÓN DE COMPRA", ParagraphStyle(
            "alq", parent=E, fontName="Helvetica-Bold", textColor=OSCURO)), Spacer(1, 2 * mm), KeepTogether([tabla_c]),
            Spacer(1, 2 * mm), _p(
            "Beneficios: evita invertir en activos fijos, mantiene los indicadores de rentabilidad y patrimonio, e incluye "
            "los mantenimientos durante el contrato. Al finalizar, los bienes se transfieren al cliente mediante acta por "
            f"el {Decimal(str(cotizacion.opcion_compra)).normalize():f} % del valor inicial ({_pesos(c['opcion_compra'])}).")]
    historia += [Spacer(1, 5 * mm)]
    historia += _seccion("Condiciones", [("Vigencia", cotizacion.VIGENCIA.replace("Vigencia de la propuesta: ", "")),
                                         ("Condiciones", cotizacion.condiciones)])
    firmas = Table([[HRFlowable(width="100%", color=GRIS, thickness=0.5), "", HRFlowable(width="100%", color=GRIS, thickness=0.5)],
                    [_p(f"{asesor} · Asesor comercial", ParagraphStyle("f", parent=E, textColor=GRIS)), "",
                     _p("Aprobación del cliente", ParagraphStyle("f2", parent=E, textColor=GRIS))]],
                   colWidths=["40%", "20%", "40%"])
    historia += [Spacer(1, 12 * mm), KeepTogether([firmas])]

    def pie(canvas, documento):
        canvas.saveState()
        ancho, alto = letter
        if borrador:
            canvas.setFont("Helvetica-Bold", 70)
            canvas.setFillColor(colors.HexColor("#eeeeee"))
            canvas.translate(ancho / 2, alto / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "BORRADOR")
            canvas.rotate(-35)
            canvas.translate(-ancho / 2, -alto / 2)
        canvas.setStrokeColor(VERDE)
        canvas.setLineWidth(1.2)
        canvas.line(16 * mm, 15 * mm, ancho - 16 * mm, 15 * mm)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColor(OSCURO)
        canvas.drawString(16 * mm, 11.5 * mm, EMPRESA)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRIS)
        canvas.drawString(16 * mm + canvas.stringWidth(EMPRESA + "   ", "Helvetica-Bold", 7), 11.5 * mm, PIE)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(16 * mm, 8.5 * mm, f"Propuesta {codigo} · Radicado {t.radicado}")
        canvas.drawRightString(ancho - 16 * mm, 11.5 * mm, f"Pág. {documento.page}")
        canvas.restoreState()

    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    return f"{codigo}.pdf", buffer.getvalue()

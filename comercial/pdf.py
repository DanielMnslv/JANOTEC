"""PDF «Ficha de requerimiento» (el mismo formato que usaba el área comercial en la ficha de Netlify).

Se adjunta al correo de radicación y se descarga desde la solicitud para compartirla por WhatsApp.
Hay dos versiones: la del cliente (sin datos internos de la venta) y la interna del área comercial.
"""
import re
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from django.utils.formats import date_format
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ImagenPDF, Indenter, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
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

# Carta de presentación de las propuestas (texto de las propuestas reales aprobadas por los clientes)
CARTA_PROPUESTA = [
    "Nos complace presentar la siguiente propuesta económica detallada para la implementación de soluciones tecnológicas en "
    "seguridad por parte de JANO TEC S.A.S.",
    "Nuestra propuesta ha sido diseñada cuidadosamente para abordar las necesidades específicas identificadas y proporcionar "
    "una solución efectiva y eficiente. Hemos considerado cuidadosamente las especificaciones técnicas necesarias y los costos "
    "asociados para garantizar una implementación sin contratiempos.",
    "Nuestro enfoque se centra en ofrecer una solución integral que fortalezca el esquema de seguridad, contribuya con el "
    "control de la operación, potencialice las comunicaciones, facilite la identificación de oportunidades y mejoras a los "
    "procesos, entre otros beneficios.",
    "Quedamos a su disposición para discutir cualquier aspecto adicional de nuestra propuesta y responder a cualquier pregunta "
    "que pueda surgir. Agradecemos sinceramente la oportunidad de presentar esta propuesta y esperamos poder colaborar con "
    "ustedes en la implementación exitosa de estas soluciones tecnológicas.",
]
FIRMA_PROPUESTA = ("MIGUEL ANGEL TIBAMOSO PINZON", "Director TIC", "JANO TEC SAS")

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


def _texto_codigo(descripcion):
    """El [CÓDIGO] de bodega no le sirve al cliente."""
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", descripcion)


def _firma_usuario(usuario, ancho, alto):
    """La firma escaneada del usuario (si la subió), ajustada al espacio; None si no hay o no se puede leer."""
    archivo = getattr(usuario, "firma", None) if usuario else None
    if not archivo:
        return None
    try:
        with archivo.open("rb") as f:
            img = Image.open(BytesIO(f.read())).convert("RGBA")
    except (OSError, ValueError):
        return None
    fondo = Image.new("RGB", img.size, "white")
    fondo.paste(img, mask=img.split()[3])
    png = BytesIO()
    fondo.save(png, "PNG")
    png.seek(0)
    escala = min(ancho / img.width, alto / img.height)
    firma = ImagenPDF(png, width=img.width * escala, height=img.height * escala)
    firma.hAlign = "LEFT"
    return firma


def _carta_propuesta(cotizacion, codigo, linea_servicio):
    """Página 1: la carta de presentación firmada por dirección."""
    t = cotizacion.ficha.ticket
    hoy = timezone.localdate()
    cuerpo = ParagraphStyle("carta", parent=E, fontSize=10, leading=13, alignment=TA_JUSTIFY, spaceAfter=7)
    plano = ParagraphStyle("carta_plano", parent=cuerpo, alignment=TA_LEFT, spaceAfter=0)
    if cotizacion.es_alquiler:
        asunto = f"Servicio de alquiler de equipos para el sistema de {linea_servicio.lower()}." if linea_servicio else t.asunto
    else:
        asunto = f"Suministro e instalación del sistema de {linea_servicio.lower()}." if linea_servicio else t.asunto
    sede = t.sede.nombre if t.sede else ""
    margen_carta = 28  # La carta va más angosta y más abajo que las tablas, como en la propuesta real
    historia = [
        Spacer(1, 19 * mm), Indenter(left=margen_carta, right=margen_carta),
        _p(f"Villavicencio, {hoy.day} de {date_format(hoy, 'F').lower()} de {hoy.year}", plano),
        _p(codigo, ParagraphStyle("carta_cod", parent=plano, alignment=TA_RIGHT)),
        _p("Señores:", plano),
        Paragraph(f"<b>{_esc(t.empresa.razon_social)}</b>", plano),
    ]
    if sede:
        historia.append(Paragraph(f"<b>{_esc(sede)}</b>", plano))
    historia += [_p((t.sede.ciudad if t.sede and t.sede.ciudad else "Ciudad"), plano), Spacer(1, 7 * mm),
                 Paragraph(f"<b>Asunto:</b> {_esc(asunto)}", plano), Spacer(1, 7 * mm)]
    historia += [_p(parrafo, cuerpo) for parrafo in CARTA_PROPUESTA]
    historia += [Spacer(1, 20 * mm)]
    historia += [Paragraph(f"<b>{_esc(FIRMA_PROPUESTA[0])}</b>", plano), _p(FIRMA_PROPUESTA[1], plano),
                 Paragraph(f"<b>{_esc(FIRMA_PROPUESTA[2])}</b>", plano), Indenter(left=-margen_carta, right=-margen_carta)]
    return historia


OLIVA = colors.HexColor("#76923c")          # títulos de la propuesta económica
GRIS_TITULO = colors.HexColor("#7f7f7f")
CREMA = colors.HexColor("#fde9d9")


def _barra(texto, ancho):
    barra = Table([[_p(texto, ParagraphStyle("barra", parent=E_ETIQUETA, fontSize=8, alignment=TA_CENTER, textColor=colors.white))]],
                  colWidths=[ancho])
    barra.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), OLIVA), ("BOX", (0, 0), (-1, -1), 0.5, GRIS),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return barra


def _resumen_propuesta(cotizacion, codigo, linea_servicio):
    """Página 2: datos del proyecto, resumen por categoría, opciones de alquiler y desglose de lo cotizado."""
    from .models import LineaCotizacion

    ficha = cotizacion.ficha
    t = ficha.ticket
    hoy = timezone.localdate()
    ANCHO = 498
    # El alquiler agrega dos tablas: la letra baja para que todo quede en una página, como en la propuesta real
    tam, interlinea = (6.5, 7.5) if cotizacion.es_alquiler else (7.5, 9)
    separacion = 1 * mm if cotizacion.es_alquiler else 1.5 * mm
    chico = ParagraphStyle("prop", parent=E, fontSize=tam, leading=interlinea)
    chico_b = ParagraphStyle("prop_b", parent=chico, fontName="Helvetica-Bold")
    derecha = ParagraphStyle("prop_d", parent=chico, alignment=TA_RIGHT)
    derecha_b = ParagraphStyle("prop_db", parent=chico_b, alignment=TA_RIGHT)
    centro = ParagraphStyle("prop_c", parent=chico, alignment=TA_CENTER)
    centro_b = ParagraphStyle("prop_cb", parent=chico_b, alignment=TA_CENTER)
    titulo = ParagraphStyle("prop_t", parent=centro_b, textColor=colors.white)
    titulo_d = ParagraphStyle("prop_td", parent=derecha_b, textColor=colors.white)
    malla = [("GRID", (0, 0), (-1, -1), 0.5, GRIS), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    M = cotizacion.Modalidad
    alquiler = cotizacion.es_alquiler
    modalidad = "Venta/Alquiler" if cotizacion.modalidad == M.HIBRIDO else cotizacion.get_modalidad_display().split(" (")[0]
    alcance = cotizacion.alcance.strip()
    objeto = t.asunto if not alcance or alcance.startswith("Ver el informe") else alcance
    datos = [("N° Propuesta:", codigo), ("ID PROYECTO:", codigo.rsplit("-V", 1)[0]), ("MODALIDAD:", modalidad),
             ("Cliente:", t.empresa.razon_social), ("Sede:", t.sede or "—"), ("NIT:", t.empresa.nit or "—"), ("Objeto:", objeto),
             ("Línea de Servicio:", linea_servicio or "—"), ("Fecha:", f"{hoy:%d/%m/%Y}"), ("Trimestre:", f"Q{(hoy.month - 1) // 3 + 1}"),
             ("Año:", hoy.year)]
    tabla_datos = Table([[_p(e, centro_b), _p(v, centro_b if e in ("ID PROYECTO:", "MODALIDAD:") else chico)] for e, v in datos],
                        colWidths=[95, ANCHO - 95])
    tabla_datos.setStyle(TableStyle(malla + [("BACKGROUND", (1, 1), (1, 1), CREMA), ("BACKGROUND", (1, 2), (1, 2), TINTE_VERDE)]))
    historia = [_barra("PROPUESTA ECONÓMICA — MEDIOS TECNOLÓGICOS", ANCHO), tabla_datos, Spacer(1, separacion)]

    orden = list(LineaCotizacion.Categoria.values)
    lineas = sorted(cotizacion.lineas.all(), key=lambda linea: orden.index(linea.categoria_final))
    por_categoria = {c: {"base": Decimal(0), "iva": Decimal(0), "total": Decimal(0)} for c in LineaCotizacion.Categoria.values}
    for linea in lineas:
        c = linea.calculo()
        for clave in ("base", "iva", "total"):
            por_categoria[linea.categoria_final][clave] += c[clave]

    def celdas(valores):
        return [_p(_pesos(valores[k]) if valores[k] else "-", derecha) for k in ("base", "iva", "total")]

    def celdas_b(valores):
        return [_p(_pesos(valores[k]), derecha_b) for k in ("base", "iva", "total")]

    C = LineaCotizacion.Categoria
    filas = [[_p(x, titulo) for x in ("Categoría", "Subtotal sin IVA", "IVA", "Total con IVA")]]
    productos = [C.EQUIPOS, C.ENERGIA, C.LICENCIAS, C.CONECTIVIDAD, C.ACCESORIOS]
    subtotal = {k: sum(por_categoria[c][k] for c in productos) for k in ("base", "iva", "total")}
    filas += [[_p(c.label.upper(), centro), *celdas(por_categoria[c])] for c in productos]
    filas.append([_p("SUBTOTAL EQUIPOS, ENERGÍA, LICENCIAS, CONECTIVIDAD Y ACCESORIOS", centro_b), *celdas_b(subtotal)])
    filas.append([_p("MANO DE OBRA", centro), *celdas(por_categoria[C.MANO_OBRA])])
    tot = cotizacion.totales()
    filas.append([_p("VALOR TOTAL", titulo), *[_p(_pesos(tot[k]), titulo_d) for k in ("base", "iva", "total")]])
    resumen = Table(filas, colWidths=[ANCHO - 240, 80, 80, 80])
    gris = colors.HexColor("#d9d9d9")
    resumen.setStyle(TableStyle(malla + [("BACKGROUND", (0, 0), (-1, 0), GRIS_TITULO), ("BACKGROUND", (0, 6), (-1, 6), gris),
                                         ("BACKGROUND", (0, 7), (-1, 7), TINTE_VERDE), ("BACKGROUND", (0, 8), (-1, 8), OLIVA)]))
    historia += [_barra("RESUMEN DE PROPUESTA ECONÓMICA", ANCHO), resumen, Spacer(1, separacion)]

    if alquiler:
        c = cotizacion.canones()
        incluida = Table([[_p("¿Mano de obra incluida en el canon de alquiler?", ParagraphStyle("prop_i", parent=chico_b, fontName="Helvetica-BoldOblique")),
                           _p("Sí" if cotizacion.mano_obra_en_canon else "No", ParagraphStyle("prop_si", parent=centro_b, textColor=colors.HexColor("#0000c0"))),
                           _p(_pesos(c["mano_obra"]) if c["mano_obra"] else "-", ParagraphStyle("prop_mo", parent=derecha_b, textColor=colors.HexColor("#c00000")))]],
                         colWidths=[ANCHO - 160, 80, 80])
        incluida.setStyle(TableStyle(malla))
        filas_c = [[_p(x, titulo) for x in ("Plazo", "Valor antes de IVA", "Valor IVA", "Canon mensual")]]
        filas_c += [[_p(f"{f['plazo']:02d} meses", centro), _p(_pesos(f["antes_iva"]), derecha), _p(_pesos(f["iva"]), derecha),
                     _p(_pesos(f["canon"]), derecha_b)] for f in c["filas"]]
        opciones = Table(filas_c, colWidths=[ANCHO - 240, 80, 80, 80])
        opciones.setStyle(TableStyle(malla + [("BACKGROUND", (0, 0), (-1, 0), GRIS_TITULO)]))
        aprobado = Table([[_p("PLAZO DE ALQUILER APROBADO POR EL CLIENTE:", chico_b), "", _p("Canon Mensual", centro_b), ""]],
                         colWidths=[150, 140, 80, ANCHO - 370], rowHeights=[24])
        aprobado.setStyle(TableStyle(malla + [("BACKGROUND", (0, 0), (0, 0), TINTE_VERDE), ("BACKGROUND", (1, 0), (1, 0), CREMA),
                                              ("BACKGROUND", (2, 0), (3, 0), TINTE_VERDE)]))
        historia += [incluida, Spacer(1, separacion), _barra("OPCIONES DE ALQUILER (canon mensual)", ANCHO), opciones,
                     Spacer(1, separacion), aprobado, Spacer(1, separacion)]

    con_descuento = any(linea.descuento for linea in lineas)
    titulos = ["Categoría", "Descripción", "Cant.", "Vlr Unit"] + (["Desc."] if con_descuento else []) + ["Total"]
    filas_d = [[_p(x, titulo) for x in titulos]]
    etiquetas = dict(C.choices)
    for linea in lineas:
        fila = [_p(etiquetas[linea.categoria_final].upper(), centro), _p(_texto_codigo(linea.descripcion), chico),
                _p(f"{linea.cantidad.normalize():f}", centro), _p(_pesos(linea.precio_unitario), derecha)]
        if con_descuento:
            fila.append(_p(f"{linea.descuento.normalize():f}%" if linea.descuento else "—", centro))
        filas_d.append(fila + [_p(_pesos(linea.calculo()["base"]), derecha)])
    anchos = [84, None, 40, 70] + ([34] if con_descuento else []) + [66]
    anchos[1] = ANCHO - sum(a for a in anchos if a)
    # Los totales van en la MISMA tabla del desglose: la etiqueta ocupa de «Cant.» a la columna antes de «Total» y el
    # valor queda justo bajo «Total», así las columnas y los bordes coinciden (antes era otra tabla y quedaba corrida).
    ultima_linea = len(filas_d) - 1
    pie_filas = [("TOTAL (sin IVA):", tot["base"]), ("IVA:", tot["iva"]), ("TOTAL (IVA incluido):", tot["total"])]
    if tot["descuento"]:
        pie_filas.insert(0, ("Descuento:", tot["descuento"]))
    vacias = len(anchos) - 4  # columnas que ocupa la etiqueta además de la primera
    for i, (etiqueta, valor) in enumerate(pie_filas):
        estilo = titulo_d if i == len(pie_filas) - 1 else derecha_b
        filas_d.append(["", "", _p(etiqueta, estilo)] + [""] * vacias + [_p(_pesos(valor), estilo)])
    primera_total = ultima_linea + 1
    estilo_desglose = [s for s in malla if s[0] != "GRID"] + [
        ("GRID", (0, 0), (-1, ultima_linea), 0.5, GRIS), ("GRID", (2, primera_total), (-1, -1), 0.5, GRIS),
        ("BACKGROUND", (0, 0), (-1, 0), GRIS_TITULO),
        ("BACKGROUND", (2, primera_total), (-1, -2), gris), ("BACKGROUND", (2, -1), (-1, -1), OLIVA)]
    estilo_desglose += [("SPAN", (2, r), (-2, r)) for r in range(primera_total, len(filas_d))]
    desglose = Table(filas_d, colWidths=anchos, repeatRows=1)
    desglose.setStyle(TableStyle(estilo_desglose))
    historia += [_barra("DESGLOSE DE EQUIPOS COTIZADOS", ANCHO), desglose, Spacer(1, 3 * mm)]

    persona = cotizacion.elaborada_por or ficha.comercial  # quien la armó en administrativa (antes, el asesor)
    elaboro = (persona.get_full_name() or persona.username) if persona else ""
    firma = _firma_usuario(persona, ANCHO / 2 * 0.6, 26)
    firmas = Table([[_p("Elaboró (firma):", chico_b), _p("Aprobó:", chico_b)], [firma or "", ""],
                    [HRFlowable(width="85%", color=GRIS, thickness=0.5, hAlign="LEFT"), HRFlowable(width="85%", color=GRIS, thickness=0.5, hAlign="LEFT")],
                    [_p(elaboro or " ", chico), _p("Nombre y cargo:", chico)],
                    [_p(f"Cargo: {persona.cargo}" if persona and persona.cargo else "Cargo:", chico), ""]],
                   colWidths=[ANCHO / 2, ANCHO / 2], rowHeights=[None, 28 if firma else 14, 3, None, None])
    firmas.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    historia.append(KeepTogether([firmas]))
    return historia


def _observaciones_propuesta(cotizacion, linea_servicio):
    """Página 3: cómo se cobra, vigencia y garantía."""
    M = cotizacion.Modalidad
    cuerpo = ParagraphStyle("obs", parent=E, fontSize=10, leading=13, alignment=TA_JUSTIFY, spaceAfter=7)
    negrita = ParagraphStyle("obs_b", parent=cuerpo, fontName="Helvetica-Bold")
    historia = [_p("Observaciones:", negrita)]
    if cotizacion.es_alquiler:
        sistema = f"sistema de {linea_servicio.lower()}" if linea_servicio else "sistema"
        historia += [_p(f"La presente propuesta se encuentra presentada bajo la modalidad de alquiler de los equipos del {sistema}. "
                        "Una vez aprobada la propuesta, se generarán los siguientes cobros:", cuerpo),
                     Paragraph("1. <b>Canon mensual de alquiler:</b> corresponde al valor mensual de los equipos, de acuerdo con la "
                               "opción de plazo seleccionada por el cliente.", cuerpo)]
        if cotizacion.mano_obra_en_canon:
            historia.append(_p("La mano de obra, la instalación y la configuración están incluidas en el canon mensual.", cuerpo))
        else:
            historia += [Paragraph("2. <b>Mano de obra, instalación y configuración:</b> corresponde a un único cobro, efectuado por "
                                   "una sola vez durante la etapa de instalación y puesta en funcionamiento del sistema.", cuerpo),
                         _p("Por lo anterior, el valor mensual del alquiler y el valor correspondiente a la instalación, configuración "
                            "y puesta en marcha serán facturados de manera independiente.", cuerpo)]
        if cotizacion.opcion_compra:
            c = cotizacion.canones()
            historia.append(_p("Al finalizar el contrato, los bienes se transfieren al cliente mediante acta por el "
                               f"{Decimal(str(cotizacion.opcion_compra)).normalize():f} % del valor inicial ({_pesos(c['opcion_compra'])}).", cuerpo))
    historia += [Spacer(1, 3 * mm), Paragraph("<b>Vigencia de la propuesta:</b> Siete (7) días Hábiles", cuerpo)]
    condiciones = cotizacion.condiciones.strip()
    if condiciones:
        historia += [Spacer(1, 3 * mm), Paragraph(f"<b>Nota:</b> {_esc(condiciones)}", cuerpo)]
    return historia


def propuesta_pdf(cotizacion, codigo, numero, borrador=False):
    """PDF de la propuesta económica, con el formato de las propuestas reales. Devuelve (nombre_archivo, bytes)."""
    t = cotizacion.ficha.ticket
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=31 * mm,
                            bottomMargin=27 * mm, title=f"Propuesta económica {codigo}", author=EMPRESA)
    linea_servicio = t.categoria.nombre if t.categoria_id else ""
    historia = _carta_propuesta(cotizacion, codigo, linea_servicio) + [PageBreak()]
    historia += _resumen_propuesta(cotizacion, codigo, linea_servicio) + [PageBreak()]
    historia += _observaciones_propuesta(cotizacion, linea_servicio)
    marca = settings.BASE_DIR / "static" / "img" / "marca"

    def membrete(canvas, documento):
        canvas.saveState()
        ancho, alto = letter
        canvas.drawImage(str(marca / "membrete-inferior.jpg"), 1.5, -9.84, width=609.9, height=540.84)
        canvas.drawImage(str(marca / "membrete-superior.jpg"), -0.55, 651.7, width=612.55, height=150.75)
        if borrador:
            canvas.setFont("Helvetica-Bold", 70)
            canvas.setFillColor(colors.HexColor("#e6e6e6"))
            canvas.translate(ancho / 2, alto / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "BORRADOR")
        canvas.restoreState()

    doc.build(historia, onFirstPage=membrete, onLaterPages=membrete)
    return f"{codigo}.pdf", buffer.getvalue()

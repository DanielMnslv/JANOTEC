"""Borrador del informe técnico armado a partir de las actas del ticket (plantilla, sin IA).

El texto sale de frases fijas llenadas con los datos del acta; las tablas de equipos y el anexo
fotográfico se toman de las actas al momento de generar el PDF. Coordinación lo aprueba y se pasa a limpio.
"""
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ImagenPDF
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from comercial.pdf import BORDE, E, E_DERECHA, E_ETIQUETA, E_SECCION, GRIS, LINEAS_EMPRESA, PIE, TINTE_VERDE, VERDE, _esc, _logo, _p

from .models import ActaSoporte, EquipoActa, InformeBorrador

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
# Actividad del acta → cómo se nombra el trabajo en el informe
TRABAJOS = {"desinstalación": "desmonte", "instalación": "instalación", "soporte": "soporte técnico"}
CLASES = [(EquipoActa.Clase.RETIRADO, "equipos retirados"), (EquipoActa.Clase.INTERVENIDO, "equipos intervenidos"),
          (EquipoActa.Clase.UTILIZADO, "materiales y equipos instalados")]


def _fecha_larga(valor):
    return f"{valor.day} de {MESES[valor.month - 1]} de {valor.year}"


def _y(partes):
    partes = [p for p in dict.fromkeys(partes) if p]
    return " y ".join([", ".join(partes[:-1]), partes[-1]]) if len(partes) > 1 else (partes[0] if partes else "")


def actas_del_ticket(ticket):
    return list(ticket.actas.prefetch_related("equipos__fotos").order_by("fecha"))


def _sistemas(actas):
    return _y([a.get_sistema_display_corto() if a.sistema in ("CCTV", "GPS") else a.get_sistema_display_corto().lower()
               for a in actas if a.sistema])


def _trabajos(actas):
    return _y([TRABAJOS.get(x.strip().lower(), x.strip().lower()) for a in actas for x in a.actividad.split(",") if x.strip()]) \
        or "visita técnica"


def _minuscula(texto):
    """«Panel de alarma» → «panel de alarma», pero respeta siglas: «NVR», «GPRS»."""
    return texto[:1].lower() + texto[1:] if texto[1:2].islower() else texto


def _n(cantidad, singular, plural):
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _lista_equipos(equipos):
    return ", ".join(_minuscula(e.equipo) + (f" ({e.cantidad})" if e.cantidad and e.cantidad not in ("1", "01") else "")
                     for e in equipos[:15]) + ("…" if len(equipos) > 15 else "")


def textos(ticket):
    """Los textos del borrador (asunto, introducción, actividades, justificación, conclusiones)."""
    actas = actas_del_ticket(ticket)
    a0 = actas[0]
    sede = a0.lugar or (str(ticket.sede) if ticket.sede else "")
    ciudad = (a0.ciudad or (ticket.sede.ciudad if ticket.sede else "")).strip()
    sistemas = _sistemas(actas) or "seguridad electrónica"
    trabajo = _trabajos(actas)
    fechas = _y([_fecha_larga(timezone.localtime(a.fecha)) for a in actas])
    equipos = [e for a in actas for e in a.equipos.all()]
    retirados = [e for e in equipos if e.clase == EquipoActa.Clase.RETIRADO]
    intervenidos = [e for e in equipos if e.clase == EquipoActa.Clase.INTERVENIDO]
    utilizados = [e for e in equipos if e.clase == EquipoActa.Clase.UTILIZADO]
    hay_fotos = any(e.fotos.all() for e in equipos) or any(a.evidencias.exists() for a in actas)

    documenta = ["las actividades realizadas"]
    if retirados:
        documenta.append("el inventario de los equipos retirados (referencias, marcas y cantidades)")
    elif intervenidos or utilizados:
        documenta.append("la relación de los equipos intervenidos e instalados")
    if hay_fotos:
        documenta.append("las evidencias fotográficas de la intervención")
    introduccion = (f"En atención a la solicitud {ticket.radicado} ({ticket.asunto}), el día {fechas} se realizó el {trabajo} "
                    f"del sistema de {sistemas} en la sede {sede} de {ticket.empresa}" + (f", en {ciudad}" if ciudad else "")).rstrip(".")
    introduccion += f". El presente informe documenta {_y(documenta)}."

    actividades = []
    for a in actas:
        actividades += InformeBorrador.lineas(a.diagnostico)
    for a in actas:
        sistema = f" del sistema de {_minuscula(a.get_sistema_display_corto())}" if a.sistema else ""
        for clase, frase in ((EquipoActa.Clase.RETIRADO, "Desmonte y recopilación de los componentes"),
                             (EquipoActa.Clase.INTERVENIDO, "Revisión e intervención de los equipos"),
                             (EquipoActa.Clase.UTILIZADO, "Instalación de los materiales y equipos")):
            lista = [e for e in a.equipos.all() if e.clase == clase]
            if lista:
                actividades.append(f"{frase}{sistema}: {_lista_equipos(lista)}.")
    if any(e.referencia or e.serial or e.placa for e in equipos):
        actividades.append("Registro de los equipos con las referencias, seriales y números de inventario visibles en sus etiquetas.")
    if hay_fotos:
        actividades.append("Registro de evidencias fotográficas de los equipos.")

    if retirados:
        justificacion = (f"El registro del {trabajo} y del inventario permite dejar constancia de los elementos recuperados del "
                         f"sistema de {sistemas} de la sede {sede}, apoyar su trazabilidad y facilitar los procesos "
                         "administrativos de control, almacenamiento o traslado de activos.")
    else:
        justificacion = (f"La intervención permite mantener en funcionamiento el sistema de {sistemas} de la sede {sede} y "
                         "deja constancia técnica del estado de los equipos y de los trabajos realizados.")
    if hay_fotos:
        justificacion += " Las fotografías incorporadas como anexo sirven de soporte visual de los equipos documentados."

    resultados = {ActaSoporte.Resultado.SOLUCIONADO: "quedó solucionada", ActaSoporte.Resultado.PARCIAL:
                  "quedó solucionada parcialmente", ActaSoporte.Resultado.RECOMENDACIONES: "quedó atendida con recomendaciones"}
    resultado = next((resultados[a.resultado] for a in reversed(actas) if a.resultado in resultados), "fue atendida")
    conclusiones = [f"Se realizó el {trabajo} del sistema de {sistemas} de la sede {sede}" + (f", en {ciudad}" if ciudad else "") +
                    f"; la solicitud {resultado}."]
    if retirados:
        con_placa = [f"{e.equipo} {e.referencia}".strip() + f", inventario {e.placa or e.serial}" for e in retirados
                     if e.placa or e.serial]
        conclusiones.append(f"Se inventariaron {_n(len(retirados), 'equipo retirado', 'equipos retirados')} con sus referencias y marcas" +
                            (f" y, cuando estuvo visible, su número de inventario ({'; '.join(con_placa[:4])})" if con_placa else "") + ".")
        del_cliente = [e for e in retirados if e.propiedad == EquipoActa.Propiedad.CLIENTE]
        de_janotec = [e for e in retirados if e.propiedad == EquipoActa.Propiedad.JANOTEC]
        if del_cliente and len(del_cliente) == len(retirados):
            conclusiones.append("Los equipos retirados son del cliente y se le entregan.")
        elif del_cliente:
            conclusiones.append(f"{_n(len(del_cliente), 'equipo es', 'equipos son')} del cliente y se le entregan.")
        if de_janotec:
            conclusiones.append(f"{_n(len(de_janotec), 'equipo es', 'equipos son')} de JANOTEC (alquiler) y regresan a la bodega.")
    if hay_fotos:
        conclusiones.append("Se realizó el registro fotográfico de la actividad como soporte técnico de la intervención efectuada.")
    recomendaciones = [l for a in actas for l in InformeBorrador.lineas(a.observaciones)]

    return {
        "destinatario": str(ticket.empresa),
        "asunto": f"{trabajo.capitalize()} del sistema de {sistemas} · sede {sede}",
        "introduccion": introduccion,
        "actividades": "\n".join(actividades),
        "justificacion": justificacion,
        "conclusiones": "\n".join(conclusiones),
        "recomendaciones": "\n".join(recomendaciones),
    }


GARANTIA = ("Vida útil: la vida útil de los equipos es de un (1) año, contado a partir de la fecha de entrega a satisfacción. "
            "JANOTEC S.A.S. reemplazará a sus expensas los equipos, materiales o partes que resulten de mala calidad o con "
            "defecto de fabricación durante el periodo de garantía; si el equipo no puede ser reparado, será restituido por "
            "uno nuevo de las mismas características del inicialmente entregado.")


def tipo_sugerido(ticket):
    """Estudio si es un levantamiento para la propuesta; si no, informe de la visita."""
    from .models import Ticket

    ficha = getattr(ticket, "ficha", None)
    if ficha is not None and ficha.etapa == "levantamiento":
        return InformeBorrador.Tipo.ESTUDIO
    if ticket.tipo == Ticket.Tipo.REQUERIMIENTO and not (ficha and ficha.versiones.exists()):
        return InformeBorrador.Tipo.ESTUDIO
    return InformeBorrador.Tipo.VISITA


def _sistema_ticket(ticket, actas):
    return _sistemas(actas) or (ticket.sistema_display if hasattr(ticket, "sistema_display") else "") or "seguridad electrónica"


def textos_estudio(ticket):
    """Informe de estudio (como los de Diana Corporación o Pastos y Leguminosas): zonas, equipos a suministrar y garantía."""
    actas = actas_del_ticket(ticket)
    sistema = _sistema_ticket(ticket, actas)
    sistema_txt = sistema if sistema[:2].isupper() else _minuscula(sistema)
    a0 = actas[0] if actas else None
    sede = (a0.lugar if a0 else "") or (str(ticket.sede) if ticket.sede else "")
    ciudad = ((a0.ciudad if a0 else "") or (ticket.sede.ciudad if ticket.sede else "")).strip()
    lugar = " · ".join(x for x in (sede, ciudad) if x)
    introduccion = (f"En atención a la solicitud {ticket.radicado} ({ticket.asunto}), me permito enviar el informe del estudio "
                    f"realizado por el personal técnico de JANOTEC S.A.S. en las instalaciones de {ticket.empresa}"
                    + (f" ({lugar})" if lugar else "")).rstrip(".")
    introduccion += (". En él se relacionan las zonas a cubrir, con la visual de cada punto, y los equipos y materiales "
                     "necesarios, que son la base de la propuesta económica.")
    actividades = [f"Estudio del sistema de {sistema_txt} en sitio."]
    for a in actas:
        actividades += InformeBorrador.lineas(a.diagnostico)
    actividades += ["Levantamiento de las zonas a cubrir, con registro fotográfico de la visual de cada punto.",
                    "Relación de los equipos y materiales necesarios para la solución."]
    ficha = getattr(ticket, "ficha", None)
    previo = getattr(ficha, "informe", None) if ficha else None
    recomendaciones = ["Informar de cualquier novedad presentada en el sistema para su verificación.",
                       f"Realizar mantenimiento preventivo del sistema de {sistema_txt} cada seis meses."]
    recomendaciones += [l for a in actas for l in InformeBorrador.lineas(a.observaciones)]
    return {
        "destinatario": str(ticket.empresa),
        "asunto": f"Estudio del sistema de {sistema_txt}" + (f" · {sede}" if sede else ""),
        "introduccion": introduccion,
        "actividades": "\n".join(actividades),
        "justificacion": "",
        "conclusiones": "\n".join([
            "Con el estudio se definieron las zonas a cubrir y la ubicación de cada equipo.",
            "Los equipos, materiales y servicios relacionados son la base de la propuesta económica que enviará el área comercial."]),
        "recomendaciones": "\n".join(recomendaciones),
        "garantia": GARANTIA,
        "suministros": previo.materiales if previo else "",
        "servicios": previo.servicios if previo else "",
    }


def _zonas_desde_actas(borrador):
    """Cada equipo del acta con fotos se vuelve una zona del estudio (el técnico la renombra si hace falta)."""
    from .models import FotoZona, ZonaInforme

    n = 0
    for a in actas_del_ticket(borrador.ticket):
        for e in a.equipos.all():
            fotos = list(e.fotos.all())
            if not fotos:
                continue
            n += 1
            zona = ZonaInforme.objects.create(borrador=borrador, orden=n, nombre=e.equipo[:150], descripcion=e.observaciones)
            for f in fotos:
                FotoZona.objects.create(zona=zona, archivo=f.archivo.name, nombre_original=f.nombre_original)


def generar(ticket, autor, borrador=None, tipo=None):
    """Crea (o vuelve a redactar) el borrador. El de visita exige acta; el de estudio no.

    Al volver a redactar se reemplaza el texto, pero se conservan las zonas, fotos, equipos y servicios ya escritos.
    """
    tipo = tipo or (borrador.tipo if borrador else tipo_sugerido(ticket))
    datos = textos_estudio(ticket) if tipo == InformeBorrador.Tipo.ESTUDIO else textos(ticket)
    if borrador is None:
        borrador = InformeBorrador.objects.create(ticket=ticket, creado_por=autor, tipo=tipo, **datos)
    else:
        for campo, valor in datos.items():
            if campo in ("suministros", "servicios") and getattr(borrador, campo).strip():
                continue
            setattr(borrador, campo, valor)
        borrador.tipo = tipo
        borrador.estado = InformeBorrador.Estado.BORRADOR
        borrador.save()
    if tipo == InformeBorrador.Tipo.ESTUDIO and not borrador.zonas.exists():
        _zonas_desde_actas(borrador)
    return borrador


def copia_pdf(borrador):
    """El PDF aprobado como archivo nuevo para adjuntarlo al ticket."""
    with borrador.archivo.open("rb") as f:
        return ContentFile(f.read(), name=f"Informe_tecnico_{borrador.ticket.radicado}.pdf")


def vigente(ticket):
    return ticket.informes_borrador.select_related("aprobado_por", "creado_por").first()


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
E_TEXTO = ParagraphStyle("texto", parent=E, fontSize=9.5, leading=13.5, alignment=TA_JUSTIFY)
E_TITULO = ParagraphStyle("titulo_inf", parent=E_ETIQUETA, fontSize=10.5, leading=14, spaceBefore=10, spaceAfter=4)
E_PIE_FOTO = ParagraphStyle("pie_foto", parent=E, fontSize=7.5, leading=9.5, alignment=TA_CENTER, textColor=GRIS)


def _foto(foto, ancho):
    """La foto reducida (y enderezada según el celular) para que el PDF no pese demasiado."""
    try:
        with foto.archivo.open("rb") as f:
            img = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
    except Exception:  # noqa: BLE001 — una foto dañada o HEIC no detiene el informe
        return None
    img.thumbnail((900, 900))
    salida = BytesIO()
    img.save(salida, "JPEG", quality=72)
    salida.seek(0)
    alto = min(ancho * img.height / img.width, ancho * 1.25)
    return ImagenPDF(salida, width=alto * img.width / img.height, height=alto)


def _vinetas(texto):
    return [Paragraph(f"•&nbsp;&nbsp;{_esc(l)}", ParagraphStyle("vineta", parent=E_TEXTO, leftIndent=10, firstLineIndent=-8,
                                                                    spaceAfter=2)) for l in InformeBorrador.lineas(texto)]


def _barra(texto, ancho, fondo=VERDE):
    barra = Table([[Paragraph(_esc(texto), E_SECCION)]], colWidths=[ancho])
    barra.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), fondo)]))
    return barra


def _cuerpo_estudio(borrador, ancho):
    """Resultados por zona (fotos de la visual), tabla de lo que se va a suministrar, servicios y garantía."""
    h = []
    zonas = list(borrador.zonas.prefetch_related("fotos"))
    if zonas:
        h.append(Paragraph("RESULTADOS DEL TRABAJO", E_TITULO))
    for z in zonas:
        bloque = [_barra(z.nombre.upper(), ancho, GRIS)]
        imagenes = [i for i in (_foto(f, ancho / 3 - 8) for f in z.fotos.all()) if i]
        if imagenes:
            celdas = [[i, Paragraph("Visual", E_PIE_FOTO)] for i in imagenes]
            filas = [celdas[i:i + 3] + [""] * (3 - len(celdas[i:i + 3])) for i in range(0, len(celdas), 3)]
            tabla = Table(filas, colWidths=[ancho / 3] * 3)
            tabla.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                       ("TOPPADDING", (0, 0), (-1, -1), 6)]))
            bloque.append(tabla)
        if z.descripcion.strip():
            bloque += [Spacer(1, 3), Paragraph(_esc(z.descripcion), E_TEXTO)]
        h += [KeepTogether(bloque), Spacer(1, 8)]

    filas = borrador.filas_suministros()
    if filas:
        cuerpo = [[_p(x, E_ETIQUETA) for x in ("N.º", "Cant.", "Equipos y materiales", "Marca")]]
        cuerpo += [[_p(i), _p(c), _p(d), _p(m or "N/A")] for i, (c, d, m) in enumerate(filas, 1)]
        tabla = Table(cuerpo, colWidths=[ancho * x for x in (0.07, 0.12, 0.59, 0.22)], repeatRows=1)
        tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        h += [Spacer(1, 6), _barra(f"TABLA DE EQUIPOS Y MATERIALES A SUMINISTRAR · {borrador.ticket.empresa}".upper(), ancho), tabla]
    if borrador.servicios.strip():
        h += [Paragraph("SERVICIOS", E_TITULO), *_vinetas(borrador.servicios)]
    if borrador.garantia.strip():
        h += [Paragraph("GARANTÍA Y VIDA ÚTIL", E_TITULO), Paragraph(_esc(borrador.garantia), E_TEXTO)]
    return h


def pdf(borrador, definitivo=False):
    """(nombre, bytes). Si no es el definitivo lleva la marca de agua BORRADOR."""
    t = borrador.ticket
    actas = actas_del_ticket(t)
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=14 * mm,
                            bottomMargin=18 * mm, title=f"Informe técnico {t.radicado}")
    ancho = doc.width
    logo = _logo()
    encabezado = Table([[logo or "", Paragraph("<br/>".join(LINEAS_EMPRESA), E_DERECHA)]], colWidths=[ancho * 0.45, ancho * 0.55])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    ciudad = next((a.ciudad for a in actas if a.ciudad), "") or (t.sede.ciudad if t.sede else "")
    fecha = borrador.aprobado_en or timezone.now()
    h = [encabezado, Spacer(1, 10),
         _p(f"{ciudad.upper() + ', ' if ciudad else ''}{_fecha_larga(timezone.localtime(fecha)).upper()}", E_ETIQUETA), Spacer(1, 8),
         _p(f"SEÑORES: {borrador.destinatario.upper()}", E_ETIQUETA), Spacer(1, 6),
         Paragraph(f"<b>ASUNTO:</b> {'INFORME DE ESTUDIO' if borrador.es_estudio else 'INFORME DE VISITA TÉCNICA'}<br/>"
                   f"{_esc(borrador.asunto.upper())}", E_TEXTO),
         Paragraph(f"Ticket {t.radicado}", ParagraphStyle("rad", parent=E, textColor=GRIS)),
         Paragraph("INTRODUCCIÓN", E_TITULO), Paragraph(_esc(borrador.introduccion), E_TEXTO),
         Paragraph("ACTIVIDADES REALIZADAS", E_TITULO), *_vinetas(borrador.actividades)]

    if borrador.es_estudio:
        h += _cuerpo_estudio(borrador, ancho)
    # Tablas de equipos por sistema y por lo que se hizo con ellos (el estudio usa sus zonas y su tabla)
    for a in ([] if borrador.es_estudio else actas):
        for clase, nombre in CLASES:
            equipos = [e for e in a.equipos.all() if e.clase == clase]
            if not equipos:
                continue
            sistema = a.get_sistema_display_corto().upper() if a.sistema else ""
            cabeza = Table([[Paragraph(f"TABLA DE {nombre.upper()}" + (f" · SISTEMA DE {sistema}" if sistema else ""), E_SECCION)]],
                           colWidths=[ancho])
            cabeza.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), VERDE)]))
            filas = [[_p(x, E_ETIQUETA) for x in ("N.º", "Cant.", "Descripción", "Referencia", "Marca", "Serial / placa")]]
            for i, e in enumerate(equipos, 1):
                filas.append([_p(i), _p(e.cantidad or "1"), _p(e.equipo), _p(e.referencia or "Sin referencia"),
                              _p(e.marca or "No registrada"), _p(" · ".join(x for x in (e.serial, e.placa) if x) or "—")])
            tabla = Table(filas, colWidths=[ancho * x for x in (0.06, 0.08, 0.3, 0.2, 0.16, 0.2)], repeatRows=1)
            tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            h += [Spacer(1, 10), cabeza, tabla]

    if borrador.justificacion.strip():
        h += [Paragraph("JUSTIFICACIÓN", E_TITULO), Paragraph(_esc(borrador.justificacion), E_TEXTO)]

    # Anexo fotográfico: tres fotos por fila con su descripción
    celdas = []
    for a in ([] if borrador.es_estudio else actas):
        for e in a.equipos.all():
            for foto in e.fotos.all():
                imagen = _foto(foto, ancho / 3 - 8)
                if imagen:
                    pie = " ".join(x for x in (e.equipo, e.marca, e.referencia) if x)
                    celdas.append([imagen, Paragraph(_esc(pie), E_PIE_FOTO)])
        for ev in a.evidencias.all():  # el trabajo en sí: durante y cómo quedó
            imagen = _foto(ev, ancho / 3 - 8) if ev.es_imagen else None
            if imagen:
                celdas.append([imagen, Paragraph(_esc(ev.get_etapa_display()), E_PIE_FOTO)])
    if celdas:
        h.append(Paragraph("ANEXO DE EVIDENCIAS FOTOGRÁFICAS", E_TITULO))
        filas = [celdas[i:i + 3] + [""] * (3 - len(celdas[i:i + 3])) for i in range(0, len(celdas), 3)]
        anexo = Table(filas, colWidths=[ancho / 3] * 3)
        anexo.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        h.append(anexo)

    h += [Paragraph("CONCLUSIONES", E_TITULO), *_vinetas(borrador.conclusiones)]
    if borrador.recomendaciones.strip():
        h += [Paragraph("RECOMENDACIONES", E_TITULO), *_vinetas(borrador.recomendaciones)]

    tecnicos = _y([a.tecnico.get_full_name() if a.tecnico else a.tecnico_texto for a in actas]) or \
        ((borrador.creado_por.get_full_name() or borrador.creado_por.username) if borrador.creado_por else "Área técnica")
    aprobo = (f"Aprobó: {borrador.aprobado_por.get_full_name() or borrador.aprobado_por.username}\nCoordinación del área técnica"
              if definitivo and borrador.aprobado_por else "Aprobó: pendiente\nCoordinación del área técnica")
    h += [Spacer(1, 30), KeepTogether(Table([["_" * 38, "_" * 38], [_p(f"Elaboró: {tecnicos}\nÁrea técnica · JANOTEC S.A.S."), _p(aprobo)]],
                                            colWidths=[ancho / 2, ancho / 2]))]

    def pagina(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRIS)
        canvas.drawCentredString(letter[0] / 2, 9 * mm, PIE)
        if not definitivo:
            canvas.setFont("Helvetica-Bold", 90)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.12))
            canvas.translate(letter[0] / 2, letter[1] / 2)
            canvas.rotate(45)
            canvas.drawCentredString(0, -30, "BORRADOR")
        canvas.restoreState()

    doc.build(h, onFirstPage=pagina, onLaterPages=pagina)
    sufijo = "" if definitivo else "_BORRADOR"
    return f"Informe_tecnico_{t.radicado}{sufijo}.pdf", buf.getvalue()


def aprobar(borrador, usuario):
    """Pasa a limpio: PDF definitivo guardado y el borrador queda aprobado."""
    borrador.estado = InformeBorrador.Estado.APROBADO
    borrador.aprobado_por = usuario
    borrador.aprobado_en = timezone.now()
    nombre, contenido = pdf(borrador, definitivo=True)
    borrador.archivo.save(nombre, ContentFile(contenido), save=False)
    borrador.save()
    return borrador

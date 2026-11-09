"""Borrador del informe técnico armado a partir de las actas del ticket (plantilla, sin IA).

El texto sale de frases fijas llenadas con los datos del acta, y el anexo fotográfico se toma de las actas al generar
el PDF. Coordinación lo envía a dirección, que lo aprueba y se pasa a limpio.

Todo informe sigue el formato de JANOTEC, en este orden: introducción, actividades realizadas (lo que dice el acta),
tabla de equipos y materiales requeridos (lo que se propone), justificación, anexo de evidencias fotográficas y
conclusiones (qué se hizo, qué se propone y su alcance, con las recomendaciones y la garantía). Un informe solo
informativo, sin nada que proponer, no lleva la tabla.
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
# Actividad del acta → cómo se nombra el trabajo en el informe, con su artículo («se realizó la instalación»)
TRABAJOS = {"desinstalación": "el desmonte", "instalación": "la instalación", "soporte": "el soporte técnico",
            "mantenimiento preventivo": "el mantenimiento preventivo", "ampliación o mejoramiento": "la ampliación y mejora",
            "reubicación o traslado": "la reubicación"}


def _fecha_larga(valor):
    """La fecha en palabras: «3 de noviembre de 2026»."""
    return f"{valor.day} de {MESES[valor.month - 1]} de {valor.year}"


def _y(partes):
    """Une las partes sin repetir ni vacías: «a, b y c»."""
    partes = [p for p in dict.fromkeys(partes) if p]
    return " y ".join([", ".join(partes[:-1]), partes[-1]]) if len(partes) > 1 else (partes[0] if partes else "")


def actas_del_ticket(ticket):
    """Las actas del ticket en orden de fecha, con sus equipos y fotos."""
    return list(ticket.actas.prefetch_related("equipos__fotos").order_by("fecha"))


def _sistemas(actas):
    """Los sistemas intervenidos en las actas, en palabras (CCTV y GPS en mayúscula, los demás en minúscula)."""
    return _y([a.get_sistema_display_corto() if a.sistema in ("CCTV", "GPS") else a.get_sistema_display_corto().lower()
               for a in actas if a.sistema])


def _trabajos(actas, articulo=True):
    """Los trabajos hechos en las actas, en palabras: «el desmonte y la instalación». Sin artículo sirve de título.

    Una actividad escrita a mano en actas anteriores se nombra «el servicio de …»; si no hay ninguna, «la visita técnica».
    """
    nombres = [TRABAJOS.get(x.strip().lower(), f"el servicio de {x.strip().lower()}")
               for a in actas for x in a.actividad.split(",") if x.strip()] or ["la visita técnica"]
    if not articulo:
        nombres = [n.split(" ", 1)[1] for n in nombres]
    return _y(nombres)


def _de(trabajo):
    """«de» + el trabajo, con la contracción del español: «del desmonte», «de la instalación»."""
    return "del " + trabajo[3:] if trabajo.startswith("el ") else "de " + trabajo


def _minuscula(texto):
    """«Panel de alarma» → «panel de alarma», pero respeta siglas: «NVR», «GPRS»."""
    return texto[:1].lower() + texto[1:] if texto[1:2].islower() else texto


def _n(cantidad, singular, plural):
    """Cantidad con el sustantivo en singular o plural: «1 cámara», «3 cámaras»."""
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _lista_equipos(equipos):
    """Los equipos en una línea con su cantidad (hasta 15; si hay más, termina en «…»)."""
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
    introduccion = (f"En atención a la solicitud {ticket.radicado} ({ticket.asunto}), el día {fechas} se realizó {trabajo} "
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
        justificacion = (f"El registro {_de(trabajo)} y del inventario permite dejar constancia de los elementos recuperados del "
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
    conclusiones = [f"Se realizó {trabajo} del sistema de {sistemas} de la sede {sede}" + (f", en {ciudad}" if ciudad else "") +
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
        "asunto": f"{_trabajos(actas, articulo=False).capitalize()} del sistema de {sistemas} · sede {sede}",
        "introduccion": introduccion,
        "actividades": "\n".join(actividades),
        "justificacion": justificacion,
        "conclusiones": "\n".join(conclusiones),
        "recomendaciones": "\n".join(recomendaciones),
    }


# Va en las conclusiones cuando el informe propone equipos, materiales o servicios
ALCANCE = ("Se propone el suministro e instalación de los equipos, materiales y servicios relacionados en la tabla de equipos "
           "y materiales requeridos. El alcance comprende únicamente lo relacionado en ella; cualquier trabajo adicional se "
           "cotiza por aparte.")

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
    """El sistema del informe: el de las actas o, si no, el del ticket; por defecto «seguridad electrónica»."""
    return _sistemas(actas) or (ticket.sistema_display if hasattr(ticket, "sistema_display") else "") or "seguridad electrónica"


def textos_estudio(ticket):
    """Informe de requerimientos técnicos (como los de Llanogas La Primavera o Ganacampo): zonas con la visual de cada
    punto, equipos a suministrar, justificación y garantía. Es la base de la propuesta económica."""
    actas = actas_del_ticket(ticket)
    sistema = _sistema_ticket(ticket, actas)
    sistema_txt = sistema if sistema[:2].isupper() else _minuscula(sistema)
    a0 = actas[0] if actas else None
    sede = (a0.lugar if a0 else "") or (str(ticket.sede) if ticket.sede else "")
    ciudad = ((a0.ciudad if a0 else "") or (ticket.sede.ciudad if ticket.sede else "")).strip()
    lugar = " · ".join(x for x in (sede, ciudad) if x)
    introduccion = (f"En atención a la solicitud {ticket.radicado} ({ticket.asunto}), me permito enviar el informe de requerimientos técnicos "
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
        "asunto": f"Requerimientos técnicos para el sistema de {sistema_txt}" + (f" · {sede}" if sede else ""),
        "introduccion": introduccion,
        "actividades": "\n".join(actividades),
        "justificacion": (f"Los requerimientos se definieron a partir de lo observado en sitio: las zonas a cubrir, las "
                          f"distancias y las condiciones de la instalación. Los equipos y materiales relacionados permiten "
                          f"implementar el sistema de {sistema_txt} con la cobertura requerida por {ticket.empresa}."),
        "conclusiones": "\n".join([
            "Con la visita se definieron las zonas a cubrir y la ubicación de cada equipo.",
            ALCANCE,
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


def requeridos_de_actas(ticket):
    """Los equipos y materiales «requeridos» de las actas del ticket, como líneas de la tabla del informe.

    Formato de cada línea: «cantidad equipo | referencia | marca» (lo que lee InformeBorrador._fila).
    """
    lineas = []
    for a in actas_del_ticket(ticket):
        for e in a.equipos.all():
            if e.clase != EquipoActa.Clase.REQUERIDO or not e.equipo.strip():
                continue
            linea = f"{(e.cantidad or '1').strip()} {e.equipo.strip()}"
            if e.referencia or e.marca:
                linea += f" | {e.referencia.strip()} | {e.marca.strip()}"
            lineas.append(linea)
    return lineas


def generar(ticket, autor, borrador=None, tipo=None):
    """Crea (o vuelve a redactar) el borrador. El de visita exige acta; el de estudio no.

    Al volver a redactar se reemplaza el texto, pero se conservan las zonas, fotos, equipos y servicios ya escritos.
    """
    tipo = tipo or (borrador.tipo if borrador else tipo_sugerido(ticket))
    datos = textos_estudio(ticket) if tipo == InformeBorrador.Tipo.ESTUDIO else textos(ticket)
    # Lo que el técnico anotó como «requerido» en el acta llena la tabla de equipos y materiales requeridos
    if not datos.get("suministros"):
        datos["suministros"] = "\n".join(requeridos_de_actas(ticket))
    # Un informe con equipos o servicios requeridos también dice qué se propone y su alcance
    con_requeridos = datos["suministros"].strip() or (borrador is not None and (borrador.suministros.strip()
                                                                                or borrador.servicios.strip()))
    if con_requeridos and ALCANCE not in datos["conclusiones"]:
        datos["conclusiones"] += "\n" + ALCANCE
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
    """El borrador de informe más reciente del ticket (o None si no tiene)."""
    return ticket.informes_borrador.select_related("aprobado_por", "creado_por").first()


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
E_TEXTO = ParagraphStyle("texto", parent=E, fontSize=9.5, leading=13.5, alignment=TA_JUSTIFY)
E_TITULO = ParagraphStyle("titulo_inf", parent=E_ETIQUETA, fontSize=10.5, leading=14, spaceBefore=10, spaceAfter=4)
E_PIE_FOTO = ParagraphStyle("pie_foto", parent=E, fontSize=7.5, leading=9.5, alignment=TA_CENTER, textColor=GRIS)


def _foto(foto, ancho):
    """La foto reducida (y enderezada según el celular) para que el PDF no pese demasiado."""
    return _imagen(foto.archivo, ancho)


def _imagen(archivo, ancho):
    """La imagen de un archivo guardado, reducida para el PDF; None si no se puede leer."""
    try:
        with archivo.open("rb") as f:
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
    """Cada línea del texto como viñeta del PDF."""
    return [Paragraph(f"•&nbsp;&nbsp;{_esc(l)}", ParagraphStyle("vineta", parent=E_TEXTO, leftIndent=10, firstLineIndent=-8,
                                                                    spaceAfter=2)) for l in InformeBorrador.lineas(texto)]


def _barra(texto, ancho, fondo=VERDE):
    """Barra de color con el título de una sección del PDF."""
    barra = Table([[Paragraph(_esc(texto), E_SECCION)]], colWidths=[ancho])
    barra.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), fondo)]))
    return barra


def _sin_tildes(texto):
    """Minúsculas sin tildes ni espacios repetidos, para comparar descripciones."""
    import unicodedata

    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return " ".join("".join(c for c in texto if unicodedata.category(c) != "Mn").split())


def imagenes_del_catalogo(descripciones):
    """{descripción: imagen} de los ítems del catálogo de precios que tienen imagen de referencia.

    Un ítem corresponde a una línea si su descripción es igual a la de la línea o está contenida en ella (o al revés);
    si varios corresponden, gana la descripción más larga, que es la más específica.
    """
    from comercial.models import ItemCatalogo

    con_imagen = [(_sin_tildes(i.descripcion), i.imagen) for i in ItemCatalogo.objects.exclude(imagen="")]
    salida = {}
    for d in descripciones:
        buscada = _sin_tildes(d)
        candidatos = [(len(desc), img) for desc, img in con_imagen if desc and (desc in buscada or buscada in desc)]
        if buscada and candidatos:
            salida[d] = max(candidatos, key=lambda c: c[0])[1]
    return salida


def imagenes_de_la_tabla(borrador, descripciones):
    """{descripción: imagen} para la tabla: la subida en el informe y, si no hay, la del catálogo de precios."""
    propias = {_sin_tildes(i.descripcion): i.archivo for i in borrador.imagenes_equipos.all()} if borrador else {}
    salida = imagenes_del_catalogo(descripciones)
    salida.update({d: propias[_sin_tildes(d)] for d in descripciones if _sin_tildes(d) in propias})
    return salida


def _tabla_equipos(titulo, filas, ancho, borrador=None):
    """Tabla de equipos con las columnas del formato de JANOTEC: NUM, CANT, DESCRIPCIÓN, REFERENCIA, MARCA e IMAGEN DE
    REFERENCIA. Cada fila es {cantidad, descripcion, referencia, marca}; la mano de obra («servicio») solo lleva la
    cantidad y la descripción."""
    imagenes = imagenes_de_la_tabla(borrador, [f["descripcion"] for f in filas if not f.get("servicio")])
    cuerpo = [[_p(x, E_ETIQUETA) for x in ("NUM", "CANT", "DESCRIPCIÓN", "REFERENCIA", "MARCA", "IMAGEN DE REFERENCIA")]]
    for i, f in enumerate(filas, 1):
        if f.get("servicio"):
            cuerpo.append([_p(i), _p("1"), _p(f["descripcion"]), "", "", ""])
            continue
        imagen = _imagen(imagenes[f["descripcion"]], ancho * 0.16) if f["descripcion"] in imagenes else None
        cuerpo.append([_p(i), _p(f["cantidad"] or "1"), _p(f["descripcion"]), _p(f["referencia"] or "N/A"),
                       _p(f["marca"] or "N/A"), imagen or ""])
    tabla = Table(cuerpo, colWidths=[ancho * x for x in (0.06, 0.07, 0.33, 0.2, 0.14, 0.2)], repeatRows=1)
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (5, 1), (5, -1), "CENTER")]))
    return [Spacer(1, 10), _barra(titulo, ancho), tabla]


def firmante(borrador):
    """Quién firma el informe: el coordinador del área técnica.

    Si lo generó el coordinador, él; si no, el coordinador activo (el que tenga firma escaneada, de preferencia).
    """
    from accounts.models import Usuario

    autor = borrador.creado_por
    if autor and autor.rol == Usuario.Rol.SUPERVISOR:
        return autor
    coordinadores = Usuario.objects.filter(rol=Usuario.Rol.SUPERVISOR, is_active=True).order_by("pk")
    return next((u for u in coordinadores if u.firma), None) or coordinadores.first() or autor


def _bloque_firma(usuario, ancho):
    """Firma centrada al final del informe.

    Si el coordinador subió su firma digital autorizada (campo «firma escaneada» de su usuario), va TAL CUAL:
    la imagen completa (firma, línea, nombre y cargo), centrada y sin agregarle texto. Si no la ha subido, se
    escribe en texto: espacio para firmar, la línea, el nombre y el cargo.
    """
    from comercial.pdf import _firma_usuario

    imagen = _firma_usuario(usuario, ancho * 0.5, 110)
    if imagen:
        imagen.hAlign = "CENTER"
        return imagen
    nombre = (usuario.get_full_name() or usuario.username) if usuario else "Coordinación del área técnica"
    cargo = (usuario.cargo if usuario else "") or "Coordinador TIC"
    centrado = ParagraphStyle("firma", parent=E_TEXTO, alignment=1, spaceBefore=0, spaceAfter=0, leading=13)
    filas = [[Spacer(1, 40)], ["_" * 40], [Paragraph(f"<b>{_esc(nombre)}</b>", centrado)],
             [Paragraph(_esc(cargo), centrado)], [Paragraph("JANOTEC S.A.S.", centrado)]]
    tabla = Table(filas, colWidths=[ancho * 0.6], hAlign="CENTER")
    tabla.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return tabla


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
         Paragraph(f"<b>ASUNTO:</b> {'INFORME DE REQUERIMIENTOS TÉCNICOS' if borrador.es_estudio else 'INFORME DE VISITA TÉCNICA'}<br/>"
                   f"{_esc(borrador.asunto.upper())}", E_TEXTO),
         Paragraph(f"Ticket {t.radicado}", ParagraphStyle("rad", parent=E, textColor=GRIS)),
         Paragraph("INTRODUCCIÓN", E_TITULO), Paragraph(_esc(borrador.introduccion), E_TEXTO),
         Paragraph("ACTIVIDADES REALIZADAS", E_TITULO), *_vinetas(borrador.actividades)]

    # Orden fijo de JANOTEC: introducción, actividades realizadas (lo que dice el acta), tabla de equipos y materiales
    # requeridos (lo que se propone), justificación, anexo de evidencias fotográficas y conclusiones (con el alcance).
    # Un informe solo informativo (sin equipos, materiales ni mano de obra que proponer) no lleva esta sección
    requeridas = borrador.filas_requeridas()
    if requeridas:
        h += _tabla_equipos("TABLA DE EQUIPOS Y MATERIALES REQUERIDOS", requeridas, ancho, borrador)

    if borrador.justificacion.strip():
        h += [Paragraph("JUSTIFICACIÓN", E_TITULO), Paragraph(_esc(borrador.justificacion), E_TEXTO)]

    # Anexo fotográfico, tres fotos por fila con su descripción: la visual de cada zona y lo registrado en el acta
    celdas = []
    for z in borrador.zonas.prefetch_related("fotos"):
        pie = z.nombre + (f": {z.descripcion.strip()}" if z.descripcion.strip() else "")
        for foto in z.fotos.all():
            imagen = _foto(foto, ancho / 3 - 8)
            if imagen:
                celdas.append([imagen, Paragraph(_esc(pie[:160]), E_PIE_FOTO)])
    for a in ([] if borrador.es_estudio else actas):  # en el estudio las fotos del acta ya son las zonas
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

    # Conclusiones: qué se hizo, qué se propone y su alcance; dentro van las recomendaciones y la garantía
    h += [Paragraph("CONCLUSIONES", E_TITULO), *_vinetas(borrador.conclusiones)]
    if borrador.recomendaciones.strip():
        h += [Spacer(1, 4), Paragraph("<b>Recomendaciones:</b>", E_TEXTO), *_vinetas(borrador.recomendaciones)]
    if borrador.garantia.strip():
        h += [Spacer(1, 4), Paragraph(f"<b>Garantía y vida útil:</b> {_esc(borrador.garantia)}", E_TEXTO)]

    # Firma al final, centrada: la del coordinador del área técnica (su firma escaneada, la línea, nombre y cargo).
    # La aprobación de dirección queda registrada en la plataforma (quién y cuándo), no en el documento.
    h += [Spacer(1, 26), KeepTogether(_bloque_firma(firmante(borrador), ancho))]

    def pagina(canvas, documento):
        """Pie de página en cada hoja y, mientras no esté aprobado, la marca de agua «BORRADOR»."""
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


def enviar_a_direccion(borrador, usuario):
    """Coordinación da el informe por revisado y lo envía a dirección, que lo aprueba o lo devuelve."""
    from . import notificaciones
    from .models import Seguimiento

    borrador.estado = InformeBorrador.Estado.REVISION
    borrador.save(update_fields=["estado", "actualizado"])
    Seguimiento.objects.create(ticket=borrador.ticket, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                               mensaje="Envió el informe técnico a dirección para su revisión y aprobación.")
    notificaciones.informe_para_aprobar(borrador, usuario)


def devolver(borrador, usuario, motivo):
    """Dirección devuelve el informe a coordinación con lo que hay que corregir; vuelve a borrador."""
    from . import notificaciones
    from .models import Seguimiento

    borrador.estado = InformeBorrador.Estado.BORRADOR
    borrador.save(update_fields=["estado", "actualizado"])
    Seguimiento.objects.create(ticket=borrador.ticket, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                               mensaje=f"Devolvió el informe técnico para corregir: {motivo}")
    notificaciones.informe_revisado(borrador, usuario, aprobado=False, motivo=motivo)


def aprobar(borrador, usuario):
    """Pasa a limpio: PDF definitivo guardado y el borrador queda aprobado."""
    borrador.estado = InformeBorrador.Estado.APROBADO
    borrador.aprobado_por = usuario
    borrador.aprobado_en = timezone.now()
    nombre, contenido = pdf(borrador, definitivo=True)
    borrador.archivo.save(nombre, ContentFile(contenido), save=False)
    borrador.save()
    return borrador

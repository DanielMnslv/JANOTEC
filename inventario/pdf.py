"""PDF de bodega: acta de entrega de materiales y equipos (bodega → técnico), solicitud de cotización (una por
proveedor) y orden de compra, con el formato de los PDF de la plataforma."""
import re
from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ImagenPDF, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

from comercial.pdf import BORDE, E, E_DERECHA, E_ETIQUETA, E_SECCION, GRIS, LINEAS_EMPRESA, PIE, TINTE_VERDE, VERDE, _logo, _p

SECCIONES = [("aprobado", "Aprobado por el cliente"), ("adicional", "Adicionales para instalar")]


def _firma(archivo, ancho):
    """La firma a mano (PNG) sobre la línea; si no hay firma, solo la línea para firmar en papel."""
    if archivo:
        try:
            with archivo.open("rb") as f:
                datos = BytesIO(f.read())
            from PIL import Image
            img = Image.open(datos)
            alto = min(ancho * img.height / img.width, 22 * mm)
            datos.seek(0)
            firma = ImagenPDF(datos, width=min(alto * img.width / img.height, ancho - 6), height=alto)
            firma.hAlign = "LEFT"
            return [firma, HRFlowable(width=ancho - 12, color=GRIS, thickness=0.6, hAlign="LEFT")]
        except Exception:  # noqa: BLE001 — una firma dañada no impide el acta
            pass
    return [Spacer(1, 14 * mm), HRFlowable(width=ancho - 12, color=GRIS, thickness=0.6, hAlign="LEFT")]


def acta_entrega_pdf(acta):
    """(nombre, bytes) del acta: solo lo que se entregó, separado en lo aprobado y los adicionales."""
    s = acta.solicitud
    t = s.ticket
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Acta de entrega {acta.numero}")
    ancho = doc.width
    logo = _logo()
    encabezado = Table([[logo or "", Paragraph("<br/>".join(LINEAS_EMPRESA), E_DERECHA)]], colWidths=[ancho * 0.45, ancho * 0.55])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    titulo = Table([[Paragraph(f"ACTA DE ENTREGA DE MATERIALES Y EQUIPOS · {acta.numero}", E_SECCION)]], colWidths=[ancho])
    titulo.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GRIS), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    cuando = timezone.localtime(acta.creado or timezone.now())
    datos = [
        [_p("Fecha", E_ETIQUETA), _p(cuando.strftime("%d/%m/%Y %I:%M %p")), _p("Solicitud", E_ETIQUETA), _p(s.numero)],
        [_p("Ticket", E_ETIQUETA), _p(t.radicado), _p("Cliente", E_ETIQUETA), _p(t.empresa)],
        [_p("Sede", E_ETIQUETA), _p(t.sede or "—"), _p("Para", E_ETIQUETA), _p(t.asunto)],
        [_p("Entrega (bodega)", E_ETIQUETA), _p(acta.entregado_por.get_full_name() if acta.entregado_por else "—"),
         _p("Recibe (técnico)", E_ETIQUETA), _p(acta.tecnico.get_full_name() if acta.tecnico else "—")],
    ]
    tabla_datos = Table(datos, colWidths=[ancho * 0.16, ancho * 0.34, ancho * 0.16, ancho * 0.34])
    tabla_datos.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))

    historia = [encabezado, Spacer(1, 6), titulo, Spacer(1, 6), tabla_datos, Spacer(1, 8)]
    for clave, nombre in SECCIONES:
        filas = [x for x in acta.detalle if x.get("seccion", "aprobado") == clave]
        if not filas:
            continue
        cuerpo = [[_p("Cant.", E_ETIQUETA), _p("Descripción", E_ETIQUETA), _p("Código / placa", E_ETIQUETA)]]
        cuerpo += [[_p(x.get("cantidad")), _p(x.get("descripcion")), _p(x.get("codigo") or "—")] for x in filas]
        tabla = Table(cuerpo, colWidths=[ancho * 0.12, ancho * 0.6, ancho * 0.28], repeatRows=1)
        tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        cabeza = Table([[Paragraph(nombre.upper(), E_SECCION)]], colWidths=[ancho])
        cabeza.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), VERDE)]))
        historia += [cabeza, tabla, Spacer(1, 8)]

    historia.append(_p("Solo se relaciona lo que bodega entregó (lo disponible). El técnico lo descuenta en el acta de "
                       "servicio y devuelve a bodega lo que no use.", E))
    historia.append(Spacer(1, 14))
    # Aprobación de salida: las firmas a mano de bodega y de quien autorizó
    aprobaciones = [f for f in s.firmas_ronda if f.aprobada]
    if aprobaciones:
        cabeza = Table([[Paragraph("APROBACIÓN DE SALIDA", E_SECCION)]], colWidths=[ancho])
        cabeza.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GRIS)]))
        celdas = [[_firma(f.imagen, ancho / 3) for f in aprobaciones],
                  [_p(f"{f.usuario.get_full_name() if f.usuario else ''}\n{f.get_rol_display()} · "
                      f"{timezone.localtime(f.creado):%d/%m/%Y %I:%M %p}") for f in aprobaciones]]
        historia += [cabeza, Table(celdas, colWidths=[ancho / max(len(aprobaciones), 2)] * len(aprobaciones), hAlign="LEFT"),
                     Spacer(1, 10)]
    recibido = (f"Firmado el {timezone.localtime(acta.recibido_en):%d/%m/%Y %I:%M %p}" if acta.recibido_en
                else "Pendiente de la firma del técnico")
    firmas = Table([[_firma(acta.firma_entrega, ancho / 2.4), _firma(acta.firma_recibe, ancho / 2.4)],
                    [_p(f"Entrega: {acta.entregado_por.get_full_name() if acta.entregado_por else ''}\nBodega, inventario y logística"),
                     _p(f"Recibe: {acta.tecnico.get_full_name() if acta.tecnico else ''}\nTécnico · {recibido}")]],
                   colWidths=[ancho / 2, ancho / 2])
    firmas.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    historia.append(KeepTogether([firmas]))

    def pie(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRIS)
        canvas.drawCentredString(letter[0] / 2, 9 * mm, PIE)
        canvas.restoreState()

    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    return f"Acta_entrega_{acta.numero}_{t.radicado}.pdf", buf.getvalue()


def _cabecera_compra(ancho, texto):
    """El logo, los datos de JANOTEC y el título de lo que se le manda a un proveedor."""
    logo = _logo()
    encabezado = Table([[logo or "", Paragraph("<br/>".join(LINEAS_EMPRESA), E_DERECHA)]], colWidths=[ancho * 0.45, ancho * 0.55])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    titulo = Table([[Paragraph(texto, E_SECCION)]], colWidths=[ancho])
    titulo.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GRIS), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return [encabezado, Spacer(1, 6), titulo, Spacer(1, 6)]


def _contacto_proveedor(pr):
    asesor = pr.asesor if pr else None
    contacto = (asesor.nombre, asesor.celular, asesor.correo) if asesor else (pr.telefono, pr.correo) if pr else ()
    return " · ".join(x for x in contacto if x) or "—"


def solicitud_cotizacion_pdf(cotizacion):
    """(nombre, bytes) de la solicitud para un proveedor: solo lo que se le pregunta a él, sin valores ni compromiso."""
    return solicitud_pdf(cotizacion.solicitud, cotizacion.proveedor)


def solicitud_pdf(pedido, pr):
    """(nombre, bytes) de la solicitud de cotización para un proveedor, solo con sus elementos.

    Cada proveedor tiene la suya (cámara a uno y cable a otro = dos PDF), lista para el correo o para WhatsApp.
    """
    lineas = pedido.lineas_para(pr)
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Solicitud de cotización {pedido.numero}")
    ancho = doc.width
    datos = [
        [_p("Fecha", E_ETIQUETA), _p(timezone.localdate().strftime("%d/%m/%Y")),
         _p("Responder antes del", E_ETIQUETA), _p(pedido.fecha_limite.strftime("%d/%m/%Y") if pedido.fecha_limite else "Lo antes posible")],
        [_p("Proveedor", E_ETIQUETA), _p(pr.razon_social), _p("NIT", E_ETIQUETA), _p(pr.nit or "—")],
        [_p("Contacto", E_ETIQUETA), _p(_contacto_proveedor(pr)),
         _p("Entregar en", E_ETIQUETA), _p("Bodega JANOTEC · Carrera 40A N.º 16-21, Villavicencio")],
    ]
    tabla_datos = Table(datos, colWidths=[ancho * 0.16, ancho * 0.34, ancho * 0.18, ancho * 0.32])
    tabla_datos.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    cuerpo = [[_p(x, E_ETIQUETA) for x in ("N.º", "Cant.", "Tipo", "Descripción", "Valor unit.", "IVA")]]
    for i, l in enumerate(lineas, 1):
        cuerpo.append([_p(i), _p(format(l.total.normalize(), "f")), _p(l.get_tipo_display()), _p(l.descripcion), "", ""])
    tabla = Table(cuerpo, colWidths=[ancho * 0.06, ancho * 0.09, ancho * 0.11, ancho * 0.46, ancho * 0.16, ancho * 0.12],
                  repeatRows=1)
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    historia = _cabecera_compra(ancho, f"SOLICITUD DE COTIZACIÓN · {pedido.numero}") + [
        tabla_datos, Spacer(1, 8), _p("Le pedimos cotizar los siguientes productos, con precio, IVA y fecha de entrega. "
                                      "Este documento no es una orden de compra ni compromete a JANOTEC S.A.S."),
        Spacer(1, 8), tabla]
    if pedido.notas:
        historia += [Spacer(1, 8), _p("Observaciones: " + pedido.notas)]
    historia += [Spacer(1, 30), Table([["_" * 40], [_p("Bodega, inventario y logística · JANOTEC S.A.S.")]], colWidths=[ancho / 2])]
    doc.build(historia)
    proveedor = re.sub(r"[^A-Za-z0-9]+", "_", pr.razon_social).strip("_")[:40]
    return f"Solicitud_de_cotizacion_{pedido.numero}_{proveedor}.pdf", buf.getvalue()


def orden_compra_pdf(orden):
    """(nombre, bytes) de la orden de compra para enviarle al proveedor."""
    from decimal import Decimal

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Orden de compra {orden.numero}")
    ancho = doc.width
    pr = orden.proveedor
    datos = [
        [_p("Fecha", E_ETIQUETA), _p(timezone.localtime(orden.creado).strftime("%d/%m/%Y")),
         _p("Entrega estimada", E_ETIQUETA), _p(orden.fecha_estimada.strftime("%d/%m/%Y") if orden.fecha_estimada else "Por definir")],
        [_p("Proveedor", E_ETIQUETA), _p(pr.razon_social if pr else "—"), _p("NIT", E_ETIQUETA), _p(pr.nit if pr else "—")],
        [_p("Contacto", E_ETIQUETA), _p(_contacto_proveedor(pr)),
         _p("Entregar en", E_ETIQUETA), _p("Bodega JANOTEC · Carrera 40A N.º 16-21, Villavicencio")],
    ]
    tabla_datos = Table(datos, colWidths=[ancho * 0.16, ancho * 0.34, ancho * 0.18, ancho * 0.32])
    tabla_datos.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))

    def pesos(v):
        return "$" + f"{Decimal(v).quantize(Decimal('1')):,}".replace(",", ".")

    cuerpo = [[_p("Cant.", E_ETIQUETA), _p("Descripción", E_ETIQUETA), _p("Valor unit.", E_ETIQUETA), _p("IVA", E_ETIQUETA),
               _p("Subtotal", E_ETIQUETA)]]
    for l in orden.lineas.all():
        detalle = " · ".join(x for x in (l.marca, l.referencia) if x)
        texto = l.descripcion + (f" ({detalle})" if detalle else "") + (" [servicio]" if l.es_servicio else "")
        cuerpo.append([_p(format(l.total.normalize(), "f")), _p(texto), _p(pesos(l.valor_unitario)),
                       _p(f"{l.iva.normalize():f}%"), _p(pesos(l.subtotal))])
    t = orden.totales()
    cuerpo += [["", "", "", _p("Subtotal", E_ETIQUETA), _p(pesos(t["base"]))],
               ["", "", "", _p("IVA", E_ETIQUETA), _p(pesos(t["iva"]))],
               ["", "", "", _p("Total", E_ETIQUETA), _p(pesos(t["total"]))]]
    tabla = Table(cuerpo, colWidths=[ancho * 0.1, ancho * 0.46, ancho * 0.16, ancho * 0.1, ancho * 0.18], repeatRows=1)
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -4), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (3, -3), (-1, -1), 0.5, BORDE)]))
    historia = _cabecera_compra(ancho, f"ORDEN DE COMPRA · {orden.numero}") + [tabla_datos, Spacer(1, 8), tabla]
    pago = (f"anticipo del {orden.porcentaje_anticipo or '—'} %: despachar al recibir el soporte del anticipo; el saldo "
            "se paga al recibir el pedido." if orden.es_anticipo else "contado: despachar al recibir el soporte de pago del 100 %.")
    historia += [Spacer(1, 8), _p("Forma de pago: " + pago + " Enviar la factura electrónica con el despacho.")]
    if orden.notas:
        historia += [Spacer(1, 4), _p("Condiciones: " + orden.notas)]
    proyecto = orden.ticket or (orden.solicitud.ticket if orden.solicitud else None)
    if proyecto:
        version = (proyecto.ficha.versiones.filter(estado="aprobada").exclude(codigo="").order_by("-numero").first()
                   if hasattr(proyecto, "ficha") else None)
        partes = [f"Proyecto {version.codigo.rsplit('-V', 1)[0]}" if version else "", orden.solicitud.numero if orden.solicitud else "",
                  proyecto.radicado]
        historia += [Spacer(1, 4), _p("Referencia interna: " + " · ".join(x for x in partes if x))]
    historia += [Spacer(1, 18), _firmas_orden(ancho, [orden])]
    doc.build(historia)
    return f"Orden_de_compra_{orden.numero}.pdf", buf.getvalue()


def _firmas_orden(ancho, ordenes):
    """Realizó (quien la prepara), Revisó (coordinación administrativa) y Aprobó (dirección), como en la hoja de JANOTEC.
    Sin firma escaneada: la aprobación queda en la plataforma con su fecha."""
    def personas(campo, fecha):
        vistos, filas = set(), []
        for o in ordenes:
            u = getattr(o, campo)
            if u and u.pk not in vistos:
                vistos.add(u.pk)
                cuando = getattr(o, fecha, None)
                filas.append(f"<b>{(u.get_full_name() or u.username).upper()}</b><br/>{(u.cargo or '').upper()}"
                             + (f"<br/>{timezone.localtime(cuando):%d/%m/%Y}" if cuando else ""))
        return Paragraph("<br/>".join(filas) or "&nbsp;", E)

    urgente = next((o for o in ordenes if o.motivo_urgencia), None)
    aprobo = personas("aprobada_por", "aprobada_en")
    if urgente and not any(o.aprobada_por for o in ordenes):
        aprobo = _p("Pasada por urgencia, sin aprobación de dirección: " + urgente.motivo_urgencia)
    titulo = ParagraphStyle("firma_t", parent=E_SECCION, alignment=TA_CENTER)
    tabla = Table([[Paragraph("FIRMAS Y APROBACIONES", titulo), "", ""],
                   [Paragraph("REALIZÓ", titulo), Paragraph("REVISÓ", titulo), Paragraph("APROBÓ", titulo)],
                   [personas("creado_por", "creado"), personas("revisada_por", "revisada_en"), aprobo]],
                  colWidths=[ancho / 3] * 3, rowHeights=[None, None, 46])  # espacio para firmar a mano
    tabla.setStyle(TableStyle([("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 1), AZUL_OC),
                               ("GRID", (0, 1), (-1, -1), 0.5, BORDE), ("BOX", (0, 0), (-1, -1), 0.5, BORDE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 2), (-1, 2), 6),
                               ("BOTTOMPADDING", (0, 2), (-1, 2), 6)]))
    return KeepTogether([tabla])


# Colores de la hoja «Orden de compra» de JANOTEC
AZUL_OC = colors.HexColor("#1f3864")
AZUL_CLARO = colors.HexColor("#2f5597")
CREMA_OC = colors.HexColor("#fff2cc")
ROSA_OC = colors.HexColor("#f4cccc")


def orden_proyecto_pdf(h):
    """(nombre, bytes) de la orden de compra del proyecto: la hoja consolidada de JANOTEC (inventario.proyecto.hoja)."""
    from decimal import Decimal

    def pesos(v):
        return "-" if v in (None, 0) else "$" + f"{Decimal(v).quantize(Decimal('1')):,}".replace(",", ".")

    def cant(v):
        return "-" if not v else format(Decimal(v).normalize(), "f")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm,
                            bottomMargin=10 * mm, title=f"Orden de compra {h['id_proyecto']}")
    ancho = doc.width
    chico = ParagraphStyle("oc", parent=E, fontSize=7, leading=8.5)
    chico_b = ParagraphStyle("oc_b", parent=chico, fontName="Helvetica-Bold")
    centro = ParagraphStyle("oc_c", parent=chico, alignment=TA_CENTER)
    derecha = ParagraphStyle("oc_d", parent=chico, alignment=TA_RIGHT)
    derecha_b = ParagraphStyle("oc_db", parent=derecha, fontName="Helvetica-Bold")
    blanco = ParagraphStyle("oc_w", parent=chico_b, alignment=TA_CENTER, textColor=colors.white)
    blanco_d = ParagraphStyle("oc_wd", parent=blanco, alignment=TA_RIGHT)

    def barra(texto):
        t = Table([[Paragraph(texto, blanco)]], colWidths=[ancho])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), AZUL_OC), ("TOPPADDING", (0, 0), (-1, -1), 2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        return t

    # Encabezado con los datos de la propuesta
    numero_estilo = ParagraphStyle("oc_n", parent=chico_b, textColor=colors.HexColor("#c00000") if not h["aprobada"] else colors.black)
    datos = [("N° ORDEN DE COMPRA:", Paragraph(h["numero"], numero_estilo)), ("N° Propuesta:", h["codigo"]),
             ("ID PROYECTO:", Paragraph(h["id_proyecto"], ParagraphStyle("oc_id", parent=chico_b, textColor=colors.HexColor("#0000c0")))),
             ("MODALIDAD:", h["modalidad"]), ("Cliente:", h["cliente"]), ("Sede:", h["sede"]), ("NIT:", h["nit"]),
             ("Objeto:", h["objeto"]), ("Línea de Servicio:", h["linea_servicio"]), ("Fecha:", f"{h['fecha']:%d/%m/%Y}"),
             ("Fecha Aprobación:", f"{h['fecha_aprobacion']:%d/%m/%Y}" if h["fecha_aprobacion"] else "")]
    encabezado = Table([[Paragraph(e, chico_b), v if not isinstance(v, str) else Paragraph(v, chico)] for e, v in datos],
                       colWidths=[110, ancho * 0.5 - 110])
    estilo = [("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("BACKGROUND", (1, 2), (1, 2), CREMA_OC)]
    if not h["aprobada"]:
        estilo.append(("BACKGROUND", (1, 0), (1, 0), ROSA_OC))
    encabezado.setStyle(TableStyle(estilo))
    encabezado.hAlign = "LEFT"
    historia = [barra("ORDEN DE COMPRA"), encabezado, Spacer(1, 6), barra("DETALLE DE ÍTEMS A COMPRAR (precios proveedor)")]

    # Detalle: un renglón por ítem de la propuesta
    titulos = ["Categoría", "Descripción", "Cant. a Comprar", "Proveedor", "Marca", "Referencia", "Precio Unit. sin IVA",
               "Precio Total sin IVA", "Valor IVA", "Valor Total con IVA", "Cant. Requerida", "Decisión de Compra"]
    anchos = [62, None, 40, 60, 52, 66, 50, 54, 46, 54, 40, 58]
    anchos[1] = ancho - sum(a for a in anchos if a)
    filas = [[Paragraph(t, blanco) for t in titulos]]
    gris_inventario = []
    for i, f in enumerate(h["filas"], start=1):
        decision = f["decision"]
        filas.append([Paragraph(f["categoria"], centro), Paragraph(f["descripcion"], chico), Paragraph(cant(f["comprar"]), centro),
                      Paragraph(f["proveedor"].razon_social if f["proveedor"] else "", centro), Paragraph(f["marca"], centro),
                      Paragraph(f["referencia"], centro), Paragraph(pesos(f["unitario"]), derecha), Paragraph(pesos(f["base"]), derecha),
                      Paragraph(pesos(f["iva"]), derecha), Paragraph(pesos(f["total"]), derecha), Paragraph(cant(f["requerida"]), centro),
                      Paragraph(decision, ParagraphStyle("oc_dec", parent=centro, fontName="Helvetica-Oblique" if decision == "INVENTARIO" else "Helvetica"))])
        if decision == "INVENTARIO":
            gris_inventario.append(i)
    t = h["totales"]
    filas.append(["", "", "", "", "", "", Paragraph("TOTALES:", blanco_d), Paragraph(pesos(t["base"]), blanco_d),
                  Paragraph(pesos(t["iva"]), blanco_d), Paragraph(pesos(t["total"]), blanco_d), "", ""])
    detalle = Table(filas, colWidths=anchos, repeatRows=1)
    estilo = [("GRID", (0, 0), (-1, -2), 0.4, BORDE), ("BACKGROUND", (0, 0), (-1, 0), AZUL_CLARO), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("BACKGROUND", (0, -1), (-1, -1), AZUL_OC),
              ("BACKGROUND", (10, 1), (10, -2), CREMA_OC), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
              ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    estilo += [("BACKGROUND", (11, i), (11, i), colors.HexColor("#d9d9d9")) for i in gris_inventario]
    detalle.setStyle(TableStyle(estilo))
    historia += [detalle, Spacer(1, 8)]

    # Observaciones y resumen por proveedor
    resumen = [[Paragraph(x, blanco) for x in ("Proveedor", "Ítems", "Total sin IVA", "Valor IVA", "Total con IVA")]]
    resumen += [[Paragraph(g["proveedor"], chico), Paragraph(str(g["items"]), centro), Paragraph(pesos(g["base"]), derecha),
                 Paragraph(pesos(g["iva"]), derecha), Paragraph(pesos(g["total"]), derecha)] for g in h["resumen"]]
    resumen.append([Paragraph("TOTAL", chico_b), Paragraph(str(h["items_comprar"]), ParagraphStyle("oc_cb", parent=centro, fontName="Helvetica-Bold")),
                    Paragraph(pesos(t["base"]), derecha_b), Paragraph(pesos(t["iva"]), derecha_b), Paragraph(pesos(t["total"]), derecha_b)])
    ancho_res = ancho * 0.55
    tabla_res = Table([[Paragraph("RESUMEN POR PROVEEDOR", blanco), "", "", "", ""]] + resumen,
                      colWidths=[ancho_res * 0.36, ancho_res * 0.1, ancho_res * 0.18, ancho_res * 0.18, ancho_res * 0.18])
    tabla_res.setStyle(TableStyle([("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), AZUL_OC),
                                   ("BACKGROUND", (0, 1), (-1, 1), AZUL_CLARO), ("LINEBELOW", (0, 1), (-1, -1), 0.4, BORDE),
                                   ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    ordenes = ", ".join(f"{o.numero} ({o.proveedor or 'sin proveedor'}: {o.get_estado_display().lower()})" for o in h["ordenes"])
    observaciones = Table([[Paragraph("OBSERVACIONES:", blanco)],
                           [Paragraph(("Órdenes por proveedor: " + ordenes) if ordenes else "&nbsp;", chico)]],
                          colWidths=[ancho - ancho_res - 8], rowHeights=[None, None])
    observaciones.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), AZUL_OC), ("BOX", (0, 0), (-1, -1), 0.5, BORDE),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    juntas = Table([[observaciones, tabla_res]], colWidths=[ancho - ancho_res, ancho_res])
    juntas.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    historia += [KeepTogether([juntas, Spacer(1, 8), _firmas_orden(ancho, h["ordenes"])])]
    doc.build(historia)
    return f"Orden_de_compra_{h['id_proyecto']}.pdf", buf.getvalue()


# Hoja carta de 30 etiquetas (3 × 10, 66,7 × 25,4 mm: tipo Avery 5160), la más común en papelerías
ETIQUETA = {"columnas": 3, "filas": 10, "ancho": 66.7 * mm, "alto": 25.4 * mm, "izquierda": 4.8 * mm, "arriba": 12.7 * mm,
            "separacion": 3.2 * mm}


def etiquetas_pdf(activos, desde=1, copias=1):
    """Etiquetas con QR para marcar los equipos. El QR lleva la placa: un lector o el celular la escribe en el buscador
    del inventario. «desde» deja en blanco las primeras posiciones (para usar una hoja ya empezada)."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    e = ETIQUETA
    por_hoja = e["columnas"] * e["filas"]
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle("Etiquetas de placas · JANOTEC")
    ancho_pag, alto_pag = letter
    lista = [a for a in activos for _ in range(copias)]
    posicion = max(1, min(desde, por_hoja)) - 1
    lado = e["alto"] - 4 * mm
    for i, a in enumerate(lista):
        if i and posicion % por_hoja == 0:
            c.showPage()
        n = posicion % por_hoja
        fila, col = divmod(n, e["columnas"])
        x = e["izquierda"] + col * (e["ancho"] + e["separacion"])
        y = alto_pag - e["arriba"] - (fila + 1) * e["alto"]
        qr = QrCodeWidget(a.placa, barLevel="M")
        x1, y1, x2, y2 = qr.getBounds()
        dibujo = Drawing(lado, lado, transform=[lado / (x2 - x1), 0, 0, lado / (y2 - y1), 0, 0])
        dibujo.add(qr)
        renderPDF.draw(dibujo, c, x + 2 * mm, y + 2 * mm)
        tx = x + lado + 4 * mm
        c.setFillColor(VERDE)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(tx, y + e["alto"] - 6 * mm, "JANOTEC S.A.S.")
        c.setFillColor(colors.black)
        tamano = 11 if len(a.placa) <= 16 else 9
        c.setFont("Helvetica-Bold", tamano)
        c.drawString(tx, y + e["alto"] - 11.5 * mm, a.placa)
        c.setFont("Helvetica", 6.5)
        texto = (a.descripcion or a.tipo or "").strip()
        ancho_texto = e["ancho"] - (tx - x) - 2 * mm
        renglones, linea = [], ""
        for palabra in texto.split():
            prueba = f"{linea} {palabra}".strip()
            if c.stringWidth(prueba, "Helvetica", 6.5) <= ancho_texto:
                linea = prueba
            else:
                renglones.append(linea)
                linea = palabra
        renglones.append(linea)
        for k, r in enumerate(r for r in renglones[:2] if r):
            c.drawString(tx, y + e["alto"] - 15 * mm - k * 2.8 * mm, r)
        if a.serial:
            c.setFillColor(GRIS)
            c.drawString(tx, y + 2.5 * mm, f"S/N {a.serial}"[:40])
            c.setFillColor(colors.black)
        posicion += 1
    if not lista:
        c.setFont("Helvetica", 10)
        c.drawString(20 * mm, alto_pag - 20 * mm, "No hay equipos para imprimir.")
    c.save()
    return buffer.getvalue()

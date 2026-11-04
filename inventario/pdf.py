"""PDF de bodega: acta de entrega de materiales y equipos (bodega → técnico), solicitud de cotización (una por
proveedor) y orden de compra, con el formato de los PDF de la plataforma."""
import re
from io import BytesIO

from django.utils import timezone
from reportlab.lib.pagesizes import letter
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
        cuerpo.append([_p(format(l.total.normalize(), "f")), _p(l.descripcion), _p(pesos(l.valor_unitario)),
                       _p(f"{l.iva.normalize():f}%"), _p(pesos(l.subtotal))])
    t = orden.totales()
    cuerpo += [["", "", "", _p("Subtotal", E_ETIQUETA), _p(pesos(t["base"]))],
               ["", "", "", _p("IVA", E_ETIQUETA), _p(pesos(t["iva"]))],
               ["", "", "", _p("Total", E_ETIQUETA), _p(pesos(t["total"]))]]
    tabla = Table(cuerpo, colWidths=[ancho * 0.1, ancho * 0.46, ancho * 0.16, ancho * 0.1, ancho * 0.18], repeatRows=1)
    tabla.setStyle(TableStyle([("GRID", (0, 0), (-1, -4), 0.5, BORDE), ("BACKGROUND", (0, 0), (-1, 0), TINTE_VERDE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (3, -3), (-1, -1), 0.5, BORDE)]))
    historia = _cabecera_compra(ancho, f"ORDEN DE COMPRA · {orden.numero}") + [tabla_datos, Spacer(1, 8), tabla]
    if orden.notas:
        historia += [Spacer(1, 8), _p("Condiciones: " + orden.notas)]
    if orden.solicitud:
        historia += [Spacer(1, 4), _p(f"Referencia interna: {orden.solicitud.numero} · {orden.solicitud.ticket.radicado}")]
    historia += [Spacer(1, 30), Table([["_" * 40], [_p("Bodega, inventario y logística · JANOTEC S.A.S.")]], colWidths=[ancho / 2])]
    doc.build(historia)
    return f"Orden_de_compra_{orden.numero}.pdf", buf.getvalue()


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

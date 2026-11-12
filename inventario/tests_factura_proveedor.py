"""Ingreso a bodega desde la factura del proveedor: leer PDF, XML o ZIP y llenar las filas para revisar."""
import io
import zipfile
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import lectura_factura as lf
from .models import Activo, Insumo, LineaOrdenCompra, MovimientoInsumo, OrdenCompra, Proveedor
from .tests import BaseInventario

UBL = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"


def xml_factura(numero="FE1234", lineas=None):
    """Una factura electrónica UBL mínima como las de la DIAN."""
    lineas = lineas or [("CAB-UTP-EXT", "Cable UTP exterior", "305", "MTR", "1000.00", "305000.00"),
                        ("DS-2CD1027G2-LIU", "Cámara bala 2MP Hikvision", "2", "94", "150000.00", "300000.00")]
    filas = "".join(f"""
  <cac:InvoiceLine><cbc:ID>{n}</cbc:ID><cbc:InvoicedQuantity unitCode="{u}">{c}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="COP">{t}</cbc:LineExtensionAmount>
    <cac:TaxTotal><cbc:TaxAmount>0</cbc:TaxAmount><cac:TaxSubtotal><cac:TaxCategory><cbc:Percent>19.00</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
    <cac:Item><cbc:Description>{d}</cbc:Description><cac:SellersItemIdentification><cbc:ID>{cod}</cbc:ID></cac:SellersItemIdentification></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="COP">{p}</cbc:PriceAmount></cac:Price></cac:InvoiceLine>"""
                    for n, (cod, d, c, u, p, t) in enumerate(lineas, 1))
    subtotal = sum(Decimal(x[5]) for x in lineas)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="{UBL}" xmlns:cac="{CAC}" xmlns:cbc="{CBC}">
  <cbc:ID>{numero}</cbc:ID><cbc:IssueDate>2026-11-10</cbc:IssueDate>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:RegistrationName>SYSCOM COLOMBIA SAS</cbc:RegistrationName>
    <cbc:CompanyID schemeID="0">901192317</cbc:CompanyID></cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="COP">{subtotal * Decimal('0.19'):.2f}</cbc:TaxAmount></cac:TaxTotal>
  <cac:WithholdingTaxTotal><cbc:TaxAmount currencyID="COP">15125.00</cbc:TaxAmount></cac:WithholdingTaxTotal>
  <cac:LegalMonetaryTotal><cbc:LineExtensionAmount>{subtotal}</cbc:LineExtensionAmount>
    <cbc:PayableAmount>{subtotal * Decimal('1.19'):.2f}</cbc:PayableAmount></cac:LegalMonetaryTotal>{filas}
</Invoice>""".encode("utf-8")


def zip_adjunto(xml):
    """El ZIP que manda el proveedor: AttachedDocument con la factura adentro, más el PDF."""
    adjunto = f"""<?xml version="1.0" encoding="UTF-8"?>
<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2" xmlns:cac="{CAC}" xmlns:cbc="{CBC}">
  <cac:Attachment><cac:ExternalReference><cbc:Description><![CDATA[{xml.decode()}]]></cbc:Description></cac:ExternalReference></cac:Attachment>
</AttachedDocument>"""
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w") as z:
        z.writestr("ad0901192317.xml", adjunto)
        z.writestr("fv0901192317.pdf", b"%PDF-1.4 vacio")
    return salida.getvalue()


def pdf_factura():
    """Un PDF de texto con la forma de la tabla de Syscom: el código sigue en la línea de abajo."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    salida = io.BytesIO()
    c = canvas.Canvas(salida, pagesize=letter)
    c.setFont("Courier", 7)
    lineas = [
        (40, 760, "FACTURA ELECTRONICA DE VENTA"), (40, 750, "FA240685"), (40, 740, "FECHA: 26/10/2026 09:03:02"),
        (300, 750, "SYSCOM COLOMBIA SAS"), (300, 740, "NIT 901192317-0"),
        (40, 720, "JANO TEC SAS"), (40, 710, "NIT: 901542984-6"),
        (40, 680, "CANT"), (70, 680, "UNIDAD"), (120, 680, "CODIGO"), (200, 680, "DESCRIPCION"), (480, 680, "IMPORTE"),
        (40, 665, "1"), (70, 665, "PIEZA"), (120, 665, "DSK1T344MBFW"), (200, 665, "TERMINAL DE RECONOCIMIENTO FACIAL"),
        (420, 665, "578,485.85"), (490, 665, "578,485.85"),
        (120, 655, "XE1"), (200, 655, "PANTALLA TACTIL 4.5"),
        (40, 640, "10"), (70, 640, "METRO"), (120, 640, "CABUTP6E"), (200, 640, "CABLE UTP CATEGORIA 6"),
        (420, 640, "2,000.00"), (490, 640, "20,000.00"),
        (380, 560, "SUB-TOTAL"), (480, 560, "$ 598,485.85"),
        (380, 550, "RET. FTE"), (480, 550, "$ 14,462.15"),
        (380, 540, "19%   I.V.A."), (480, 540, "$ 113,712.31"),
        (380, 530, "TOTAL"), (480, 530, "$ 697,735.01"),
    ]
    for x, y, texto in lineas:
        c.drawString(x, y, texto)
    c.save()
    return salida.getvalue()


class LecturaFacturaTests(BaseInventario):
    def test_numeros_con_coma_o_punto(self):
        casos = {"578,485.85": "578485.85", "578.485,85": "578485.85", "$ 1.234": "1234.00", "1,000": "1000.00",
                 "12,5": "12.50", "abc": None}
        for texto, esperado in casos.items():
            self.assertEqual(lf.numero(texto), Decimal(esperado) if esperado else None, texto)

    def test_xml_de_la_factura_electronica(self):
        f = lf.leer(xml_factura(), "factura.xml")
        self.assertEqual((f.numero, f.fecha, f.proveedor, f.nit), ("FE1234", date(2026, 11, 10), "SYSCOM COLOMBIA SAS", "901192317"))
        self.assertEqual(len(f.items), 2)
        cable = f.items[0]
        self.assertEqual((cable.codigo, cable.cantidad, cable.valor_unitario, cable.iva, cable.unidad),
                         ("CAB-UTP-EXT", Decimal("305.00"), Decimal("1000.00"), Decimal("19.00"), "MTR"))
        self.assertEqual((f.subtotal, f.retencion, f.cuadra), (Decimal("605000.00"), Decimal("15125.00"), True))

    def test_zip_con_attached_document(self):
        f = lf.leer(zip_adjunto(xml_factura("SETT77")), "z.zip")
        self.assertEqual((f.numero, len(f.items)), ("SETT77", 2))

    def test_pdf_de_texto(self):
        f = lf.leer(pdf_factura(), "f.pdf")
        self.assertEqual((f.numero, f.fecha, f.nit), ("FA240685", date(2026, 10, 26), "901192317"))
        self.assertIn("SYSCOM", f.proveedor)
        self.assertEqual([(i.codigo, i.cantidad, i.valor_unitario) for i in f.items],
                         [("DSK1T344MBFWXE1", Decimal("1.00"), Decimal("578485.85")),
                          ("CABUTP6E", Decimal("10.00"), Decimal("2000.00"))])
        self.assertIn("PANTALLA TACTIL", f.items[0].descripcion)
        self.assertEqual((f.subtotal, f.iva, f.retencion, f.total), (Decimal("598485.85"), Decimal("113712.31"),
                                                                    Decimal("14462.15"), Decimal("697735.01")))
        self.assertTrue(f.cuadra)
        self.assertEqual(f.tasa_iva, 19)

    def test_archivos_que_no_se_pueden_leer(self):
        for datos, nombre in ((b"hola", "x.txt"), (b"<html></html>", "x.xml"), (b"PK\x03\x04roto", "x.zip")):
            with self.assertRaises(lf.FacturaIlegible):
                lf.leer(datos, nombre)

    def test_sugerencia_reconoce_insumo_y_equipo_conocidos(self):
        f = lf.leer(xml_factura(), "f.xml")
        cable, camara = (lf.sugerir_fila(i, f.tasa_iva) for i in f.items)
        self.assertEqual((cable["clase"], cable["codigo"]), ("insumo", "CAB-UTP-EXT"))  # ya está en el catálogo
        # La cámara: en bodega ya hay una DS2CD1027G2HLIU, pero esta es otra referencia → equipo nuevo por la descripción
        self.assertEqual((camara["clase"], camara["referencia"]), ("equipo", "DS-2CD1027G2-LIU"))
        self.assertNotIn("prefijo", camara)
        from .models import CodigoActivo
        CodigoActivo.objects.create(codigo="BIO-FAC-POE", descripcion="Biometrico Facial Poe con lector QR")
        CodigoActivo.objects.create(codigo="CAM-BAL-2MP", descripcion="Camara IP bala de 2 Megapixeles")
        terminal = lf.Item(cantidad=Decimal(1), descripcion="TERMINAL DE RECONOCIMIENTO FACIAL WIFI POE", codigo="DSK1T344")
        self.assertEqual(lf.sugerir_fila(terminal, 19)["prefijo"], "BIO-FAC-POE")
        Activo.objects.filter(pk=self.camara.pk).update(referencia="DS-2CD1027G2-LIU")
        camara = lf.sugerir_fila(f.items[1], f.tasa_iva)
        self.assertEqual(camara["prefijo"], Activo.prefijo(self.camara.placa))


class IngresoDesdeFacturaTests(BaseInventario):
    @property
    def url(self):
        return reverse("inventario:ingreso_factura")

    def test_leer_y_dar_ingreso_sin_orden(self):
        self.client.force_login(self.bodega)
        r = self.client.post(self.url, {"accion": "leer", "factura_archivo": SimpleUploadedFile("fe.xml", xml_factura())})
        self.assertContains(r, "Se leyó la factura")
        self.assertContains(r, "La suma de los ítems da el subtotal")
        filas = r.context["filas"]
        self.assertEqual(filas.initial[0]["codigo"], "CAB-UTP-EXT")
        self.assertEqual(r.context["cabecera"].initial["factura"], "FE1234")
        antes = MovimientoInsumo.objects.filter(insumo=self.cable).count()
        datos = {"factura": "FE1234", "fecha": "2026-11-10", "proveedor": "SYSCOM COLOMBIA SAS",
                 "factura_token": r.context["factura_token"], "factura_ext": "xml",
                 "filas-TOTAL_FORMS": "2", "filas-INITIAL_FORMS": "0", "filas-MIN_NUM_FORMS": "0", "filas-MAX_NUM_FORMS": "1000",
                 "filas-0-clase": "insumo", "filas-0-descripcion": "Cable UTP exterior", "filas-0-cantidad": "305",
                 "filas-0-valor_unitario": "1000", "filas-0-iva": "19", "filas-0-codigo": "CAB-UTP-EXT · Cable UTP exterior",
                 "filas-0-unidad": "Metro",
                 "filas-1-clase": "equipo", "filas-1-descripcion": "Cámara bala 2MP", "filas-1-cantidad": "2",
                 "filas-1-valor_unitario": "150000", "filas-1-iva": "19", "filas-1-prefijo": "CAM-BAL-2MP",
                 "filas-1-referencia": "DS-2CD1027G2-LIU", "filas-1-marca": "Hikvision", "filas-1-seriales": "S1\nS2"}
        r = self.client.post(self.url, datos, follow=True)
        self.assertContains(r, "Factura FE1234 ingresada a bodega")
        self.assertEqual(MovimientoInsumo.objects.filter(insumo=self.cable).count(), antes + 1)
        nuevas = Activo.objects.filter(factura="FE1234")
        self.assertEqual(sorted(nuevas.values_list("serial", flat=True)), ["S1", "S2"])
        self.assertEqual(nuevas.first().proveedor, "SYSCOM COLOMBIA SAS")
        # Si la vuelven a leer, avisa que ya se ingresó
        r = self.client.post(self.url, {"accion": "leer", "factura_archivo": SimpleUploadedFile("fe.xml", xml_factura())})
        self.assertContains(r, "Esta factura ya se había ingresado")

    def test_factura_que_no_se_lee_y_permisos(self):
        self.client.force_login(self.bodega)
        r = self.client.post(self.url, {"accion": "leer", "factura_archivo": SimpleUploadedFile("f.pdf", b"%PDF-1.4 roto")}, follow=True)
        self.assertContains(r, "No se pudo")
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class RecibirOrdenConFacturaTests(BaseInventario):
    def test_los_items_caen_en_las_lineas_de_la_orden(self):
        proveedor = Proveedor.objects.create(razon_social="Syscom Colombia", nit="901192317")
        orden = OrdenCompra.objects.create(proveedor=proveedor, creado_por=self.bodega, estado=OrdenCompra.Estado.LISTA)
        cable = LineaOrdenCompra.objects.create(orden=orden, descripcion="Cable UTP exterior", adicional=305,
                                                referencia="CAB-UTP-EXT", valor_unitario=900)
        LineaOrdenCompra.objects.create(orden=orden, descripcion="Conector RJ45", adicional=50, valor_unitario=500)
        self.client.force_login(self.bodega)
        url = reverse("inventario:orden_recibir", args=[orden.pk])
        r = self.client.post(url, {"accion": "leer", "factura_archivo": SimpleUploadedFile("fe.zip", zip_adjunto(xml_factura()))})
        filas = r.context["filas"].initial
        self.assertEqual(r.context["cabecera"].initial["proveedor"], "Syscom Colombia")  # por el NIT
        primera = filas[0]
        self.assertEqual((primera["linea"], primera["cantidad"], primera["valor_unitario"]), (cable.pk, Decimal("305.00"), Decimal("1000.00")))
        self.assertTrue(filas[1]["conocido"].startswith("Viene en la factura pero no estaba en la orden"))
        self.assertEqual((filas[2]["clase"], filas[2]["conocido"]), ("no", "Estaba en la orden y no aparece en la factura"))
        self.assertContains(r, "Si no sube otra, queda la factura que se leyó")
        # Al ingresar, la factura leída queda como soporte de la orden
        datos = {"factura": "FE1234", "fecha": "2026-11-10", "proveedor": "Syscom Colombia",
                 "factura_token": r.context["factura_token"], "factura_ext": "zip",
                 "filas-TOTAL_FORMS": "1", "filas-INITIAL_FORMS": "0", "filas-MIN_NUM_FORMS": "0", "filas-MAX_NUM_FORMS": "1000",
                 "filas-0-linea": cable.pk, "filas-0-clase": "insumo", "filas-0-descripcion": "Cable UTP exterior",
                 "filas-0-cantidad": "305", "filas-0-valor_unitario": "1000", "filas-0-iva": "19", "filas-0-codigo": "CAB-UTP-EXT"}
        self.client.post(url, datos)
        orden.refresh_from_db()
        cable.refresh_from_db()
        self.assertEqual((cable.recibido, orden.factura), (Decimal("305.00"), "FE1234"))
        self.assertTrue(orden.soporte.name.endswith(".zip"))
        self.assertTrue(Insumo.objects.filter(codigo="CAB-UTP-EXT").exists())

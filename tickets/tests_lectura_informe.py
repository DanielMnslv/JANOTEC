"""Lectura de la tabla de equipos y materiales de un informe subido (tickets/lectura_informe.py)."""
import zipfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from tickets import lectura_informe as lectura
from tickets.models import Ticket
from tickets.tests import BaseTickets

E = Ticket.Estado
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(*tablas):
    """Un .docx mínimo con estas tablas (listas de filas de celdas)."""
    def celda(texto):
        return f"<w:tc><w:p><w:r><w:t>{texto}</w:t></w:r></w:p></w:tc>"
    cuerpo = "".join("<w:tbl>" + "".join("<w:tr>" + "".join(map(celda, fila)) + "</w:tr>" for fila in t) + "</w:tbl>"
                     for t in tablas)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document {W}><w:body><w:p><w:r><w:t>Informe</w:t></w:r></w:p>{cuerpo}</w:body></w:document>')
    return buf.getvalue()


INFORME_WORD = docx(
    [["Empresa", "Bioagrícola"]],  # una tabla que no es la de equipos
    [["NUM", "CANT", "DESCRIPCIÓN", "REFERENCIA", "MARCA", "IMAGEN DE REFERENCIA"],
     ["1", "5", "Kit Solar IP All in One / Cámara Bala ColorVu", "DS-2XS2T47G1-LDH/4G/C18S40", "HIKVISION", ""],
     ["2", "120 m", "Cable UTP cat 6", "—", "", ""],
     ["3", "1", "Dos días 2 técnicos para la instalación", "—", "—", ""]],
)


class LeerTablaTests(TestCase):
    def test_word_trae_equipos_y_mano_de_obra(self):
        filas = lectura.leer(BytesIO(INFORME_WORD), "Informe.docx")
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0], {"cantidad": "5", "descripcion": "Kit Solar IP All in One / Cámara Bala ColorVu",
                                    "referencia": "DS-2XS2T47G1-LDH/4G/C18S40", "marca": "HIKVISION", "servicio": False})
        self.assertEqual((filas[1]["cantidad"], filas[1]["referencia"], filas[1]["servicio"]), ("120 m", "", False))
        self.assertTrue(filas[2]["servicio"])

    def test_texto_de_pdf(self):
        texto = ("INTRODUCCIÓN\nbla\nTABLA DE EQUIPOS Y MATERIALES REQUERIDOS\nNUM CANT DESCRIPCIÓN REFERENCIA MARCA\n"
                 "1 5 Kit Solar IP All in One / Cámara\nBala ColorVu\n2 120 m Cable UTP cat 6\n"
                 "3 1 Mano de obra: instalación y configuración\nJUSTIFICACIÓN\n4 9 no es de la tabla")
        filas = lectura.leer_texto(texto)
        self.assertEqual([f["descripcion"] for f in filas],
                         ["Kit Solar IP All in One / Cámara Bala ColorVu", "Cable UTP cat 6", "Mano de obra: instalación y configuración"])
        self.assertEqual(filas[1]["cantidad"], "120 m")
        self.assertTrue(filas[2]["servicio"])

    def test_pdf_generado_por_la_plataforma(self):
        """La tabla del PDF tal como la arma el informe: celdas de varias líneas, centradas en vertical."""
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import Paragraph, SimpleDocTemplate

        from tickets import informe as inf
        filas = [{"cantidad": "5", "descripcion": "Kit Solar IP All in One / Cámara Bala ColorVu 4 MP con panel solar y "
                  "batería integrada", "referencia": "DS-2XS2T47G1-LDH/4G/C18S40", "marca": "HIKVISION"},
                 {"cantidad": "120 m", "descripcion": "Cable UTP cat 6", "referencia": "", "marca": ""},
                 {"cantidad": "1", "descripcion": "Dos días 2 técnicos para la instalación de las cámaras", "servicio": True}]
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        doc.build([inf._seccion("1. INTRODUCCIÓN", doc.width), Paragraph("Texto.", inf.E_TEXTO),
                   *inf._tabla_equipos("TABLA DE EQUIPOS Y MATERIALES REQUERIDOS", filas, doc.width),
                   inf._seccion("4. JUSTIFICACIÓN", doc.width), Paragraph("1 2 no es de la tabla", inf.E_TEXTO)])
        leidas = lectura.leer(BytesIO(buf.getvalue()), "Informe.PDF")
        self.assertEqual(leidas[0], {"cantidad": "5", "descripcion": filas[0]["descripcion"],
                                     "referencia": "DS-2XS2T47G1-LDH/4G/C18S40", "marca": "HIKVISION", "servicio": False})
        self.assertEqual((leidas[1]["cantidad"], leidas[1]["descripcion"], leidas[1]["referencia"]), ("120 m", "Cable UTP cat 6", ""))
        self.assertEqual((leidas[2]["descripcion"], leidas[2]["servicio"]), (filas[2]["descripcion"], True))
        self.assertEqual(len(leidas), 3)

    def test_formatos_no_soportados(self):
        with self.assertRaises(lectura.LecturaNoDisponible):
            lectura.leer(BytesIO(b"x"), "informe.doc")
        with self.assertRaises(lectura.LecturaNoDisponible):
            lectura.leer(BytesIO(b"no es zip"), "informe.docx")
        self.assertEqual(lectura.leer(BytesIO(docx([["a", "b"]])), "x.docx"), [])


class InformeSubidoAComercialTests(BaseTickets):
    def setUp(self):
        super().setUp()
        self.ticket = self.crear_ticket(sede=self.sede_a, estado=E.INFORME, asignado_a=self.agente)

    def test_el_word_llena_la_tabla_y_pasa_a_comercial(self):
        self.client.force_login(self.supervisor)
        detalle = self.client.get(self.ticket.get_absolute_url())
        self.assertContains(detalle, "la tabla se llena sola")
        r = self.client.post(reverse("leer_materiales_informe", args=[self.ticket.radicado]),
                             {"informe": SimpleUploadedFile("Informe.docx", INFORME_WORD)})
        res = r.json()
        self.assertEqual(len(res["filas"]), 3)
        self.assertIn("Revíselas", res["mensaje"])
        # Lo que la tabla envía (ya revisado) pasa a comercial
        datos = {"nota": "Cotizar kit solar", "destino": "comercial", "urgente": "1", "motivo_urgencia": "El cliente lo necesita ya",
                 "filas-total": "3", "informe": SimpleUploadedFile("Informe.docx", INFORME_WORD)}
        for i, f in enumerate(res["filas"]):
            datos.update({f"fila-{i}-{k}": f[k] for k in ("cantidad", "descripcion", "referencia", "marca")})
            datos[f"fila-{i}-tipo"] = "servicio" if f["servicio"] else "equipo"
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]), datos)
        self.ticket.refresh_from_db()
        informe = self.ticket.ficha.informe
        self.assertIn("5 Kit Solar IP All in One", informe.materiales)
        self.assertIn("DS-2XS2T47G1-LDH/4G/C18S40", informe.materiales)
        self.assertIn("120 m Cable UTP cat 6", informe.materiales)
        self.assertIn("Dos días 2 técnicos", informe.servicios)

    def test_solo_quien_entrega_el_informe_lo_lee(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("leer_materiales_informe", args=[self.ticket.radicado]),
                             {"informe": SimpleUploadedFile("Informe.docx", INFORME_WORD)})
        self.assertIn(r.status_code, (302, 403, 404))

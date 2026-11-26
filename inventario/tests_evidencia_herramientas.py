from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from accounts.models import Usuario
from . import herramientas
from .models import EvidenciaHerramienta, Herramienta


class EvidenciaHerramientasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.bodega = Usuario.objects.create(username="bodega", rol=Usuario.Rol.BODEGA)
        cls.tecnico = Usuario.objects.create(username="asignado", rol=Usuario.Rol.AGENTE)
        cls.otro = Usuario.objects.create(username="otro", rol=Usuario.Rol.AGENTE)
        cls.herramienta = herramientas.guardar(Herramienta(nombre="Probador", tecnico=cls.tecnico), cls.bodega)

    def subir_foto(self, usuario, archivo=None):
        self.client.force_login(usuario)
        if archivo is None:
            datos = BytesIO()
            Image.new("RGB", (40, 25), "white").save(datos, format="PNG")
            archivo = SimpleUploadedFile("prueba.png", datos.getvalue(), content_type="image/png")
        return self.client.post(reverse("inventario:herramienta_evidencia", args=[self.herramienta.pk]), {
            "foto": archivo, "condicion": "desgaste", "observaciones": "Marca en la carcasa.",
        })

    def test_acceso_y_foto_del_responsable(self):
        self.assertEqual(self.subir_foto(self.otro).status_code, 404)
        self.assertEqual(self.subir_foto(self.tecnico).status_code, 302)
        evidencia = EvidenciaHerramienta.objects.get()
        self.assertEqual(evidencia.tecnico, self.tecnico)
        self.assertEqual(evidencia.registrado_por, self.tecnico)
        self.assertContains(self.client.get(self.herramienta.get_absolute_url()), "Con desgaste")
        url = reverse("inventario:herramienta_foto", args=[self.herramienta.pk, evidencia.pk])
        respuesta = self.client.get(url)
        self.assertEqual(respuesta.headers["Content-Type"], "image/jpeg")
        self.assertEqual(respuesta.headers["Cache-Control"], "private, no-store")
        with Image.open(BytesIO(respuesta.content)) as foto:
            self.assertEqual(foto.format, "JPEG")
        self.client.force_login(self.otro)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.bodega)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_reasignacion_pide_nueva_evidencia(self):
        self.subir_foto(self.tecnico)
        evidencia = EvidenciaHerramienta.objects.get()
        herramientas.entregar(self.herramienta, self.otro, self.bodega)
        self.client.force_login(self.otro)
        self.assertContains(self.client.get(self.herramienta.get_absolute_url()), "Pendiente de verificar")
        url = reverse("inventario:herramienta_foto", args=[self.herramienta.pk, evidencia.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        herramientas.entregar(self.herramienta, self.tecnico, self.bodega)
        self.client.force_login(self.tecnico)
        self.assertContains(self.client.get(self.herramienta.get_absolute_url()), "Pendiente de verificar")

    def test_rechaza_archivo_que_no_es_foto(self):
        archivo = SimpleUploadedFile("falso.png", b"<html>falso</html>", content_type="image/png")
        self.assertEqual(self.subir_foto(self.tecnico, archivo).status_code, 302)
        self.assertFalse(EvidenciaHerramienta.objects.exists())

    def test_descargas_por_tecnico_incluyen_solo_sus_herramientas_activas(self):
        from openpyxl import load_workbook
        from pypdf import PdfReader

        self.subir_foto(self.tecnico)
        pdf_url = reverse("inventario:herramientas_tecnico_pdf", args=[self.tecnico.pk])
        excel_url = reverse("inventario:herramientas_tecnico_excel", args=[self.tecnico.pk])
        for url in (pdf_url, excel_url):
            self.assertEqual(self.client.get(url).status_code, 403)
        Herramienta.objects.create(nombre="Taladro fuera de servicio", estado=Herramienta.Estado.BAJA,
                                   tecnico=self.tecnico)
        Herramienta.objects.create(nombre="Martillo de otro técnico", tecnico=self.otro)
        self.client.force_login(self.bodega)
        listado = self.client.get(reverse("inventario:herramientas"))
        self.assertContains(listado, pdf_url)
        self.assertContains(listado, excel_url)
        self.assertNotContains(listado, "Descargar PDF completo")
        respuesta = self.client.get(pdf_url, {"tecnico": self.otro.pk, "q": "no coincide"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("attachment", respuesta.headers["Content-Disposition"])
        pdf = PdfReader(BytesIO(respuesta.content))
        texto = "\n".join(pagina.extract_text() for pagina in pdf.pages)
        for esperado in ("Probador", "Responsable: asignado", "Con desgaste", "Marca en la carcasa.", "1 registros"):
            self.assertIn(esperado, texto)
        self.assertNotIn("Taladro fuera de servicio", texto)
        self.assertNotIn("Martillo de otro técnico", texto)
        self.assertTrue(any(imagen.image.size == (40, 25) for pagina in pdf.pages for imagen in pagina.images))

        respuesta = self.client.get(excel_url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("herramientas-asignado-", respuesta.headers["Content-Disposition"])
        libro = load_workbook(BytesIO(respuesta.content), read_only=True)
        hoja = libro.active
        self.assertEqual(hoja["A2"].value, "Responsable: asignado")
        self.assertEqual(hoja.max_row, 5)
        self.assertEqual(hoja["A5"].value, "Probador")
        self.assertEqual(hoja["D5"].value, 1)
        self.assertEqual(hoja["H5"].value, "Con desgaste")
        libro.close()

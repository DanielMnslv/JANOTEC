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

    def test_pdf_completo_incluye_evidencia_y_protege_la_descarga(self):
        from pypdf import PdfReader

        self.subir_foto(self.tecnico)
        url = reverse("inventario:herramientas_pdf")
        self.assertEqual(self.client.get(url).status_code, 403)
        Herramienta.objects.create(nombre="Taladro fuera de servicio", estado=Herramienta.Estado.BAJA)
        self.client.force_login(self.bodega)
        respuesta = self.client.get(url, {"tecnico": self.tecnico.pk, "q": "no coincide"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("attachment", respuesta.headers["Content-Disposition"])
        pdf = PdfReader(BytesIO(respuesta.content))
        texto = "\n".join(pagina.extract_text() for pagina in pdf.pages)
        for esperado in ("Probador", "Con desgaste", "Marca en la carcasa.", "Taladro fuera de servicio", "2 registros"):
            self.assertIn(esperado, texto)
        self.assertTrue(any(imagen.image.size == (40, 25) for pagina in pdf.pages for imagen in pagina.images))

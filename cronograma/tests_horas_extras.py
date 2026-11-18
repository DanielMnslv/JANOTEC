"""Horas extras con validación (13/11/2026): en el momento (hora del servidor, ubicación del celular y foto) o tardío
(hora escrita y motivo); la coordinación valida lo que tiene alertas y lo rechazado no suma."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import horas_validacion as hv
from .models import ReporteHorasExtra
from .tests import MEDIA_TMP, BaseCronograma

CERCA = {"lat": "4.1425000", "lon": "-73.6272000", "precision": "15"}  # Villavicencio (sintético)
LEJOS = {"lat": "3.4500000", "lon": "-76.5300000", "precision": "15"}  # otra ciudad: también vale


def foto(nombre="foto.png"):
    png = BytesIO()
    Image.new("RGB", (20, 20), "red").save(png, "PNG")
    return SimpleUploadedFile(nombre, png.getvalue(), content_type="image/png")


def a_las(hora, dia=None):
    """Un momento de hoy (o de ``dia``) a esa hora, con zona horaria."""
    return timezone.make_aware(datetime.combine(dia or timezone.localdate(), time(*hora)))


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class HorasExtrasValidacionTests(BaseCronograma):
    url = reverse("cronograma:horas_extras")

    def iniciar(self, usuario, **extra):
        self.client.force_login(usuario)
        datos = {"accion": "iniciar", "i-rol": "conductor", "i-cliente": "LLANOGAS", "i-evidencia_salida": foto(), **CERCA}
        datos.update(extra)
        return self.client.post(self.url, datos)

    def cerrar(self, reporte, **extra):
        datos = {"accion": "cerrar", "reporte": reporte.pk, f"c{reporte.pk}-evidencia_llegada": foto(), **CERCA}
        datos.update(extra)
        return self.client.post(self.url, datos)

    def test_en_el_momento_con_foto_y_ubicacion_queda_verificado(self):
        with mock.patch("django.utils.timezone.now", return_value=a_las((18, 32))):
            r = self.iniciar(self.tec, **{"i-fecha": "2020-01-01", "i-hora_salida": "06:00"})  # lo escrito se ignora
        self.assertRedirects(r, self.url)
        reporte = ReporteHorasExtra.objects.get()
        self.assertEqual((reporte.fecha, reporte.hora_salida), (timezone.localdate(), time(18, 32)))
        self.assertEqual((reporte.lat_salida, reporte.salida_tardia), (Decimal("4.1425000"), False))
        self.assertEqual(reporte.validacion, "por_validar")  # abierto
        with mock.patch("django.utils.timezone.now", return_value=a_las((21, 2))):
            self.cerrar(reporte)
        reporte.refresh_from_db()
        self.assertEqual((reporte.hora_llegada, reporte.duracion, reporte.validacion), (time(21, 2), "2:30", "verificado"))
        self.assertEqual(hv.alertas(reporte), [])

    def test_sin_foto_no_se_registra(self):
        self.client.force_login(self.tec)
        self.client.post(self.url, {"accion": "iniciar", "i-rol": "conductor", "i-cliente": "LLANOGAS", **CERCA})
        self.assertFalse(ReporteHorasExtra.objects.exists())

    def test_donde_este_vale_y_sin_ubicacion_queda_por_validar(self):
        self.iniciar(self.tec, **LEJOS)  # en otra ciudad: no hay punto fijo con qué compararlo
        reporte = ReporteHorasExtra.objects.get()
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
            self.cerrar(reporte, **LEJOS)
        reporte.refresh_from_db()
        self.assertEqual((hv.alertas(reporte), reporte.validacion), ([], "verificado"))
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(self.url), "https://www.google.com/maps?q=3.4500000,-76.5300000")
        self.iniciar(self.tec)
        otro = ReporteHorasExtra.objects.get(hora_llegada=None)
        self.cerrar(otro, lat="", lon="", precision="")
        otro.refresh_from_db()
        self.assertEqual(otro.validacion, "por_validar")
        self.assertIn("Fin sin ubicación del celular", hv.alertas(otro))

    def test_registro_tardio_pide_motivo_y_hora_pasada(self):
        self.client.force_login(self.tec)
        ayer = timezone.localdate() - timedelta(days=1)
        datos = {"accion": "iniciar_tardio", "t-rol": "acompanante", "t-fecha": ayer.isoformat(), "t-hora_salida": "19:00",
                 "t-cliente": "LLANOGAS"}
        r = self.client.post(self.url, datos)  # sin motivo
        self.assertContains(r, "Explique en una frase")
        manana = timezone.localdate() + timedelta(days=1)
        r = self.client.post(self.url, {**datos, "t-fecha": manana.isoformat(), "t-motivo": "Se me descargó el celular"})
        self.assertContains(r, "no pueden ser posteriores")
        self.assertFalse(ReporteHorasExtra.objects.exists())
        self.client.post(self.url, {**datos, "t-motivo": "Se me descargó el celular en la vía"})  # foto opcional
        reporte = ReporteHorasExtra.objects.get()
        self.assertEqual((reporte.fecha, reporte.hora_salida, reporte.salida_tardia), (ayer, time(19), True))
        self.client.post(self.url, {"accion": "cerrar_tardio", "reporte": reporte.pk, f"t{reporte.pk}-hora_llegada": "22:00",
                                    f"t{reporte.pk}-motivo": "Llegué sin batería al celular"})
        reporte.refresh_from_db()
        self.assertEqual((reporte.duracion, reporte.llegada_tardia, reporte.validacion), ("3:00", True, "por_validar"))
        self.assertIn("Inicio registrado tarde: Se me descargó el celular en la vía", hv.alertas(reporte))

    def test_la_coordinacion_aprueba_o_rechaza_y_lo_rechazado_no_suma(self):
        self.iniciar(self.tec, lat="", lon="")
        reporte = ReporteHorasExtra.objects.get()
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
            self.cerrar(reporte)
        self.client.force_login(self.coord)
        lista = self.client.get(self.url)
        self.assertContains(lista, "Por validar")
        self.assertContains(lista, "sin ubicación")
        self.assertNotContains(lista, "Ubicación de la oficina")
        self.client.post(self.url, {"accion": "validar", "reporte": reporte.pk, "decision": "rechazar"})  # sin nota no
        reporte.refresh_from_db()
        self.assertEqual(reporte.validacion, "por_validar")
        self.client.post(self.url, {"accion": "validar", "reporte": reporte.pk, "decision": "rechazar",
                                    "nota": "Estaba en su casa"})
        reporte.refresh_from_db()
        self.assertEqual((reporte.validacion, reporte.validado_por, reporte.minutos), ("rechazado", self.coord, 0))
        self.client.post(self.url, {"accion": "validar", "reporte": reporte.pk, "decision": "aprobar", "nota": ""})
        reporte.refresh_from_db()
        self.assertEqual(reporte.validacion, "aprobado")
        self.assertGreater(reporte.minutos, 0)
        excel = self.client.get(self.url + f"?mes={reporte.fecha:%Y-%m}&formato=excel")
        self.assertIn("spreadsheetml", excel["Content-Type"])

    def test_el_tecnico_solo_ve_lo_suyo_y_no_valida(self):
        otro = ReporteHorasExtra.objects.create(colaborador=self.tec2, fecha=timezone.localdate(), hora_salida="22:00",
                                                hora_llegada="23:30", cliente="Otro cliente")
        self.client.force_login(self.tec)
        lista = self.client.get(self.url)
        self.assertNotContains(lista, "Otro cliente")
        self.assertNotContains(lista, 'name="i-colaborador"')
        self.assertEqual(self.client.post(self.url, {"accion": "validar", "reporte": otro.pk, "decision": "aprobar"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("cronograma:horas_extras_foto", args=[otro.pk, "salida"])).status_code, 404)
        self.assertEqual(self.client.get(self.url + "?formato=excel")["Content-Type"], "text/html; charset=utf-8")

    def test_el_coordinador_registra_por_otro_queda_por_validar(self):
        self.iniciar(self.coord, **{"i-colaborador": self.tec2.pk})
        reporte = ReporteHorasExtra.objects.get()
        self.assertEqual((reporte.colaborador, reporte.registrado_por), (self.tec2, self.coord))
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
            self.cerrar(reporte)
        reporte.refresh_from_db()
        self.assertEqual(reporte.validacion, "por_validar")
        self.assertTrue(any("no el colaborador" in a for a in hv.alertas(reporte)))

    def test_el_cliente_no_entra(self):
        self.client.force_login(self.cli)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class BorrarHorasExtrasTests(BaseCronograma):
    """Comando borrar_horas_extras (18/11/2026): lo registrado eran pruebas; simula, respalda y borra."""

    def test_simula_y_luego_respalda_y_borra(self):
        import gzip
        import io
        import json
        import shutil
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, carpeta, ignore_errors=True)
        ReporteHorasExtra.objects.create(colaborador=self.tec, fecha=timezone.localdate(), hora_salida="22:00",
                                         hora_llegada="23:30", cliente="Cliente de prueba")
        salida = io.StringIO()
        call_command("borrar_horas_extras", "--carpeta", carpeta, stdout=salida)
        self.assertIn("SIMULACIÓN", salida.getvalue())
        self.assertEqual(ReporteHorasExtra.objects.count(), 1)

        call_command("borrar_horas_extras", "--confirmar", "--carpeta", carpeta, stdout=io.StringIO())
        self.assertFalse(ReporteHorasExtra.objects.exists())
        respaldo = list(Path(carpeta).glob("horas_extras_*.json.gz"))
        self.assertEqual(len(respaldo), 1)
        datos = json.load(gzip.open(respaldo[0], "rt", encoding="utf-8"))
        self.assertEqual(datos[0]["fields"]["cliente"], "Cliente de prueba")

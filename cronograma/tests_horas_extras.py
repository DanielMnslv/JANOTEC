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
from .models import Actividad, ReporteHorasExtra, UbicacionOficina
from .tests import MEDIA_TMP, BaseCronograma

OFICINA = (Decimal("4.1420000"), Decimal("-73.6270000"))  # Villavicencio (sintético)
CERCA = {"lat": "4.1425000", "lon": "-73.6272000", "precision": "15"}  # ~60 m
LEJOS = {"lat": "4.1600000", "lon": "-73.6400000", "precision": "15"}  # ~2,5 km


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

    def setUp(self):
        super().setUp()
        UbicacionOficina.objects.create(latitud=OFICINA[0], longitud=OFICINA[1], actualizada_por=self.coord)

    def iniciar(self, usuario, **extra):
        self.client.force_login(usuario)
        datos = {"accion": "iniciar", "i-rol": "conductor", "i-cliente": "LLANOGAS", "i-evidencia_salida": foto(), **CERCA}
        datos.update(extra)
        return self.client.post(self.url, datos)

    def cerrar(self, reporte, **extra):
        datos = {"accion": "cerrar", "reporte": reporte.pk, f"c{reporte.pk}-evidencia_llegada": foto(), **CERCA}
        datos.update(extra)
        return self.client.post(self.url, datos)

    def test_distancia(self):
        self.assertAlmostEqual(hv.distancia(*OFICINA, Decimal("4.1425"), Decimal("-73.6272")), 59, delta=3)

    def test_en_el_momento_y_en_el_sitio_queda_verificado(self):
        with mock.patch("django.utils.timezone.now", return_value=a_las((18, 32))):
            r = self.iniciar(self.tec, **{"i-fecha": "2020-01-01", "i-hora_salida": "06:00"})  # lo escrito se ignora
        self.assertRedirects(r, self.url)
        reporte = ReporteHorasExtra.objects.get()
        self.assertEqual((reporte.fecha, reporte.hora_salida), (timezone.localdate(), time(18, 32)))
        self.assertEqual((reporte.referencia_salida, reporte.salida_tardia), ("Oficina JANOTEC", False))
        self.assertLess(reporte.distancia_salida, 100)
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

    def test_lejos_o_sin_ubicacion_queda_por_validar(self):
        self.iniciar(self.tec, **LEJOS)
        reporte = ReporteHorasExtra.objects.get()
        self.cerrar(reporte, lat="", lon="", precision="")
        reporte.refresh_from_db()
        alertas = hv.alertas(reporte)
        self.assertEqual(reporte.validacion, "por_validar")
        self.assertTrue(any("2,5 km de Oficina JANOTEC" in a for a in alertas), alertas)
        self.assertIn("Regreso sin ubicación del celular", alertas)

    def test_acompanante_se_confronta_con_el_cliente_de_su_visita(self):
        from tickets.models import Ticket

        Ticket.objects.filter(pk=self.ticket.pk).update(latitud=Decimal("4.0800000"), longitud=Decimal("-73.6800000"))
        a = Actividad.objects.create(ticket=self.ticket, fecha=timezone.localdate(), hora_inicio=time(8), hora_fin=time(12),
                                     cliente="Grupo del Llano", actividad="Mantenimiento")
        a.encargados.add(self.tec)
        self.iniciar(self.tec, **{"i-rol": "acompanante", "lat": "4.0801000", "lon": "-73.6801000"})
        reporte = ReporteHorasExtra.objects.get()
        self.assertEqual(reporte.referencia_salida, "Grupo del Llano · Manare")
        self.assertLess(reporte.distancia_salida, 50)

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
        self.iniciar(self.tec, **LEJOS)
        reporte = ReporteHorasExtra.objects.get()
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
            self.cerrar(reporte)
        self.client.force_login(self.coord)
        lista = self.client.get(self.url)
        self.assertContains(lista, "Por validar")
        self.assertContains(lista, "Oficina JANOTEC")
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

    def test_fijar_la_oficina(self):
        UbicacionOficina.objects.all().delete()
        self.client.force_login(self.coord)
        self.client.post(self.url, {"accion": "oficina", "lat": "4.1500000", "lon": "-73.6300000", "precision": "8"})
        self.assertEqual(hv.oficina().latitud, Decimal("4.1500000"))
        self.client.force_login(self.tec)
        self.assertEqual(self.client.post(self.url, {"accion": "oficina", **CERCA}).status_code, 403)

    def test_el_cliente_no_entra(self):
        self.client.force_login(self.cli)
        self.assertEqual(self.client.get(self.url).status_code, 403)

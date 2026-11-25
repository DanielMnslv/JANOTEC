"""Solicitud del vehículo desde el cronograma (23/11/2026): cuál (JSN758 Nissan o JSP949 Suzuki híbrido), quién conduce y
hasta cuándo; el correo le llega a quien asigna los vehículos y, si algo cambia o se cancela, también se le avisa."""
from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from .models import Actividad, Contacto
from .tests import BaseCronograma, proximo


@override_settings(NOTIFICAR_VEHICULOS_EMAIL="")
class SolicitudVehiculoTests(BaseCronograma):
    def setUp(self):
        super().setUp()
        Contacto.objects.create(nombre="F Estepa", email="vehiculos@janotec.test", tipo=Contacto.Tipo.VEHICULOS)
        self.dia = proximo(1)

    def correos_vehiculo(self):
        return [m for m in mail.outbox if "VEHÍCULO" in m.subject]

    def test_pide_vehiculo_conductor_y_fecha_final(self):
        r = self.programar(self.dia, vehiculo="on")
        self.assertContains(r, "Elija qué vehículo se solicita")
        self.assertContains(r, "Indique quién conduce el vehículo")
        self.assertContains(r, "Indique la hora de salida del vehículo")
        r = self.programar(self.dia, vehiculo="on", vehiculo_placa="JSN758", vehiculo_conductor=self.tec2.pk,
                           vehiculo_hora_salida="08:00")
        self.assertContains(r, "Elija una hora distinta al inicio de la actividad")
        r = self.programar(self.dia, vehiculo="on", vehiculo_placa="JSN758", vehiculo_conductor=self.tec2.pk,
                           vehiculo_hora_salida="07:00",
                           vehiculo_hasta=(self.dia - timedelta(days=1)).isoformat())
        self.assertContains(r, "no puede ser antes de la salida")
        self.assertFalse(Actividad.objects.exists())

    def test_envia_la_solicitud_y_luego_la_actualizacion_y_la_cancelacion(self):
        mail.outbox.clear()
        r = self.programar(self.dia, vehiculo="on", vehiculo_placa="JSP949", vehiculo_conductor=self.tec2.pk,
                           vehiculo_hora_salida="07:00")
        self.assertContains(r, "Solicitud del vehículo JSP949 enviada")
        a = Actividad.objects.get()
        self.assertEqual(a.vehiculo_hasta, self.dia)  # por defecto termina el mismo día
        self.assertEqual(a.hora_inicio.strftime("%H:%M"), "08:00")
        self.assertEqual(a.vehiculo_hora_salida.strftime("%H:%M"), "07:00")
        [m] = self.correos_vehiculo()
        self.assertEqual(sorted(m.to), ["coord@janotec.test", "vehiculos@janotec.test"])
        self.assertTrue(m.subject.startswith("SOLICITUD DE VEHÍCULO PARA SERVICIO DE MANTENIMIENTO GRUPO DEL LLANO"))
        for texto in ("JSP949 · Suzuki híbrido", "Nombre del conductor: Alberto", f"Fecha de salida: {self.dia:%d/%m/%Y}",
                      "a las 07:00 a. m.", "Recorrido: GRUPO DEL LLANO · MANARE"):
            self.assertIn(texto, m.body)
        self.assertIsNotNone(a.vehiculo_enviada)

        # Guardar sin cambios no repite el correo
        datos = {"ticket": self.ticket.pk, "fecha": self.dia.isoformat(), "hora_inicio": "08:00", "hora_fin": "12:00",
                 "cliente": "Grupo del Llano", "sede": "Manare", "actividad": "x", "tipo": "mantenimiento",
                 "linea": "CCTV", "encargados": [self.tec.pk], "notificar_a": "fernando@llano.co",
                 "sst_duracion": "medio día", "vehiculo": "on", "vehiculo_placa": "JSP949",
                 "vehiculo_conductor": self.tec2.pk, "vehiculo_hora_salida": "07:00",
                 "vehiculo_hasta": self.dia.isoformat()}
        self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        self.assertEqual(len(self.correos_vehiculo()), 1)

        datos.update(hora_inicio="09:00")
        self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        self.assertEqual(len(self.correos_vehiculo()), 1)

        datos.update(vehiculo_hora_salida="06:30")
        self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        self.assertEqual(len(self.correos_vehiculo()), 2)
        self.assertIn("a las 06:30 a. m.", self.correos_vehiculo()[-1].body)

        # Recorrido de dos días con el otro vehículo: se envía la actualización
        datos.update(vehiculo_placa="JSN758", vehiculo_hasta=(self.dia + timedelta(days=1)).isoformat())
        self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        m = self.correos_vehiculo()[-1]
        self.assertTrue(m.subject.startswith("ACTUALIZACIÓN DE SOLICITUD DE VEHÍCULO"))
        self.assertIn("JSN758 · Nissan", m.body)

        # Ya no lo requiere: se avisa la cancelación
        del datos["vehiculo"]
        self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        self.assertTrue(self.correos_vehiculo()[-1].subject.startswith("CANCELACIÓN DE SOLICITUD DE VEHÍCULO"))
        self.assertIn("a las 06:30 a. m.", self.correos_vehiculo()[-1].body)
        a.refresh_from_db()
        self.assertEqual((a.vehiculo_placa, a.vehiculo_conductor, a.vehiculo_enviada), ("", None, None))
        self.assertIsNone(a.vehiculo_hora_salida)

    def test_al_quitar_la_actividad_se_cancela_el_vehiculo(self):
        self.programar(self.dia, vehiculo="on", vehiculo_placa="JSN758", vehiculo_conductor=self.tec2.pk,
                       vehiculo_hora_salida="07:00")
        a = Actividad.objects.get()
        self.client.post(reverse("cronograma:eliminar", args=[a.pk]))
        self.assertTrue(self.correos_vehiculo()[-1].subject.startswith("CANCELACIÓN DE SOLICITUD DE VEHÍCULO"))

    def test_sin_correo_de_vehiculos_avisa_al_coordinador(self):
        Contacto.objects.all().delete()
        r = self.programar(self.dia, vehiculo="on", vehiculo_placa="JSN758", vehiculo_conductor=self.tec2.pk,
                           vehiculo_hora_salida="07:00")
        self.assertContains(r, "no hay a quién pedirlo")
        self.assertEqual(self.correos_vehiculo(), [])
        self.assertIsNone(Actividad.objects.get().vehiculo_enviada)

"""Eliminar un ticket del todo: quién puede, qué lo bloquea, qué se borra y qué registro queda."""
from datetime import time, timedelta

from django.contrib.admin.models import DELETION, LogEntry
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Usuario
from cronograma.models import Actividad

from .models import ActaSoporte, Adjunto, Seguimiento, Ticket
from .tests import BaseTickets


class EliminarTicketTests(BaseTickets):
    def setUp(self):
        super().setUp()
        self.direccion = Usuario.objects.create_user("miguel", "dir@x.co", "Clave-segura-1", rol="direccion")
        self.t = self.crear_ticket(asunto="Prueba radicada con un cliente real")
        seg = Seguimiento.objects.create(ticket=self.t, autor=self.supervisor, mensaje="Nota")
        self.adjunto = Adjunto.objects.create(ticket=self.t, seguimiento=seg, archivo=SimpleUploadedFile("foto.png", b"png"),
                                              nombre_original="foto.png", tamano=3, subido_por=self.supervisor)

    def eliminar(self, usuario, ticket=None, **datos):
        ticket = ticket or self.t
        self.client.force_login(usuario)
        return self.client.post(reverse("eliminar_ticket", args=[ticket.radicado]),
                                {"motivo": "Era una prueba radicada con un cliente real", "confirmar": ticket.radicado, **datos})

    def visita(self, ticket, dias):
        return Actividad.objects.create(ticket=ticket, fecha=timezone.localdate() + timedelta(days=dias), hora_inicio=time(8),
                                        hora_fin=time(12), cliente=str(ticket.empresa), actividad="Visita")

    def test_coordinacion_elimina_con_motivo_registro_y_aviso_a_direccion(self):
        ruta = self.adjunto.archivo.path
        futura = self.visita(self.t, 3)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.eliminar(self.supervisor)
        self.assertRedirects(r, reverse("inicio"), fetch_redirect_response=False)
        self.assertFalse(Ticket.objects.filter(pk=self.t.pk).exists())
        self.assertFalse(Actividad.objects.filter(pk=futura.pk).exists(), "la visita que no ha pasado sale del cronograma")
        import os
        self.assertFalse(os.path.exists(ruta), "el archivo adjunto se borra del disco")
        registro = LogEntry.objects.get(action_flag=DELETION)
        self.assertIn(self.t.radicado, registro.object_repr)
        self.assertIn("Era una prueba", registro.change_message)
        self.assertEqual(registro.user, self.supervisor)
        self.assertTrue(any("dir@x.co" in m.to and "eliminado" in m.subject for m in mail.outbox), "dirección recibe el aviso")

    def test_direccion_elimina_sin_avisarse_a_si_misma(self):
        self.eliminar(self.direccion)
        self.assertFalse(Ticket.objects.filter(pk=self.t.pk).exists())
        self.assertFalse(any("eliminado" in m.subject for m in mail.outbox))

    def test_nadie_mas_puede(self):
        for usuario in (self.agente, self.cli_a):
            self.assertEqual(self.eliminar(usuario).status_code, 403)
        self.assertTrue(Ticket.objects.filter(pk=self.t.pk).exists())

    def test_exige_motivo_y_el_radicado(self):
        self.eliminar(self.supervisor, confirmar="JNT-0000-000000")
        self.eliminar(self.supervisor, motivo="corto")
        self.assertTrue(Ticket.objects.filter(pk=self.t.pk).exists())

    def test_cliente_real_con_soportes_no_se_borra(self):
        ActaSoporte.objects.create(numero=5001, fecha=timezone.now(), empresa=self.emp_a, ticket=self.t, tecnico=self.agente,
                                   diagnostico="Revisado", resultado="solucionado")
        self.visita(self.t, -2)
        self.client.force_login(self.supervisor)
        html = self.client.get(self.t.get_absolute_url()).content.decode()
        self.assertIn("No se puede eliminar porque tiene actas de servicio firmadas; tiene visitas del cronograma", html)
        self.eliminar(self.supervisor)
        self.assertTrue(Ticket.objects.filter(pk=self.t.pk).exists())

    def test_cliente_prueba_se_borra_con_sus_actas_y_visitas(self):
        prueba = Empresa.objects.create(razon_social="PRUEBA", nit="999")
        cliente = Usuario.objects.create_user("cli_prueba", "p@janotec.test", "Clave-segura-1", rol="cliente", empresa=prueba)
        t = self.crear_ticket(solicitante=cliente)
        acta = ActaSoporte.objects.create(numero=5002, fecha=timezone.now(), empresa=prueba, ticket=t, tecnico=self.agente,
                                          diagnostico="Ensayo", resultado="solucionado")
        pasada = self.visita(t, -5)
        self.eliminar(self.supervisor, ticket=t)
        self.assertFalse(Ticket.objects.filter(pk=t.pk).exists())
        self.assertFalse(ActaSoporte.objects.filter(pk=acta.pk).exists())
        self.assertFalse(Actividad.objects.filter(pk=pasada.pk).exists())

    def test_el_recuadro_solo_lo_ven_direccion_y_coordinacion(self):
        for usuario, ve in ((self.supervisor, True), (self.direccion, True), (self.agente, False)):
            self.client.force_login(usuario)
            r = self.client.get(self.t.get_absolute_url())
            if r.status_code == 200:
                self.assertEqual("Eliminar este ticket" in r.content.decode(), ve, usuario)

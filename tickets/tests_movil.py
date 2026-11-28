from datetime import time, timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from cronograma.models import Actividad

from .tests import BaseTickets, E


class VisitasMovilesTests(BaseTickets):
    def test_login_con_csrf_permite_consultar_la_api_desde_la_app(self):
        cliente = Client(enforce_csrf_checks=True)
        ingreso = reverse("login")
        self.assertEqual(cliente.get(ingreso).status_code, 200)
        token = cliente.cookies["csrftoken"].value
        respuesta = cliente.post(ingreso, {
            "username": self.agente.username,
            "password": "Clave-segura-1",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(cliente.get(reverse("movil_visitas")).status_code, 200)

    def test_sesion_y_acceso_restringido_al_rol_tecnico(self):
        url = reverse("movil_visitas")
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_tecnico_recibe_solo_sus_tickets_abiertos(self):
        visible = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO)
        self.crear_ticket(asignado_a=self.agente2, estado=E.AUTORIZADO)
        self.crear_ticket(asignado_a=self.agente, estado=E.CERRADO)
        self.client.force_login(self.agente)
        respuesta = self.client.get(reverse("movil_visitas"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual([v["radicado"] for v in respuesta.json()["visitas"]], [visible.radicado])
        self.assertIn("no-store", respuesta["Cache-Control"])

    def test_cronograma_incluye_solo_las_actividades_asignadas_de_la_semana(self):
        hoy = timezone.localdate()
        lunes = hoy - timedelta(days=hoy.weekday())
        for fecha, tecnico, cliente in (
            (hoy, self.agente, "Principal"),
            (hoy, self.agente2, "Otro técnico"),
            (lunes - timedelta(days=1), self.agente, "Semana anterior"),
        ):
            actividad = Actividad.objects.create(
                fecha=fecha, cliente=cliente, actividad="Preparar equipos",
                hora_inicio=time(8), hora_fin=time(10), insumos="Cable",
            )
            actividad.encargados.add(tecnico)
        self.client.force_login(self.agente)
        respuesta = self.client.get(reverse("movil_visitas")).json()
        self.assertEqual(respuesta["hoy"], hoy.isoformat())
        self.assertEqual([a["cliente"] for a in respuesta["cronograma"]], ["Principal"])
        self.assertEqual(respuesta["cronograma"][0]["insumos"], "Cable")

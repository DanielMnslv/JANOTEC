"""Plan de mantenimiento por sede: los tickets se crean solos antes de la fecha y la siguiente se corre por la frecuencia."""
from datetime import date, timedelta

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.models import Categoria, Ticket

from . import mantenimiento as m
from .models import EjecucionMantenimiento, PlanMantenimiento


class PlanMantenimientoTests(TestCase):
    def setUp(self):
        self.coord = Usuario.objects.create_user("coord", "coord@janotec.test", "Clave-segura-1", rol="supervisor")
        self.tecnico = Usuario.objects.create_user("tec", "tec@janotec.test", "Clave-segura-1", rol="agente")
        self.emp = Empresa.objects.create(razon_social="Avícola Sintética", nit="900")
        self.sede = Sede.objects.create(empresa=self.emp, nombre="Granja 1", ciudad="Granada", contacto_nombre="Luis",
                                        contacto_telefono="3100000000")
        Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")
        self.hoy = timezone.localdate()

    def plan(self, **kw):
        datos = {"sede": self.sede, "sistema": "cctv", "frecuencia": 3, "proxima": self.hoy + timedelta(days=10),
                 "detalle": "16 cámaras y NVR", "creado_por": self.coord}
        datos.update(kw)
        return PlanMantenimiento.objects.create(**datos)

    def test_sumar_meses(self):
        self.assertEqual(m.sumar_meses(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(m.sumar_meses(date(2026, 11, 15), 3), date(2027, 2, 15))
        self.assertEqual(m.sumar_meses(date(2026, 6, 30), 12), date(2027, 6, 30))

    def test_crea_el_ticket_antes_de_la_fecha_y_corre_la_siguiente(self):
        p = self.plan()
        lejano = self.plan(sistema="alarma", proxima=self.hoy + timedelta(days=40))
        creados = m.generar()
        self.assertEqual(len(creados), 1)
        t = creados[0]
        self.assertEqual((t.tipo, t.area, t.estado, t.sede, t.categoria.nombre),
                         (Ticket.Tipo.MANTENIMIENTO, Ticket.Area.TECNICA, Ticket.Estado.GENERADO, self.sede, "Mantenimiento CCTV IP"))
        self.assertIn("16 cámaras y NVR", t.descripcion)
        self.assertIn("Sin contrato", t.descripcion)
        self.assertEqual(t.contacto_nombre, "Luis")
        self.assertEqual(mail.outbox, [])  # al cliente no le llega nada por esto
        p.refresh_from_db()
        self.assertEqual(p.proxima, m.sumar_meses(self.hoy + timedelta(days=10), 3))
        self.assertEqual(p.en_curso.ticket, t)
        self.assertEqual(lejano.ejecuciones.count(), 0)

        # No se duplica: ni en otra pasada ni cuando vuelve a tocar con el anterior abierto
        self.assertEqual(m.generar(), [])
        self.assertEqual([x.sede for x in m.generar(self.hoy + timedelta(days=200))], [self.sede])  # solo la alarma
        self.assertEqual(p.ejecuciones.count(), 1)

        # Al cerrarse, la siguiente pasada crea el próximo
        Ticket.objects.filter(pk=t.pk).update(estado=Ticket.Estado.CERRADO, fecha_cierre=timezone.now())
        self.assertEqual(p.ultima.ticket, t)
        p.refresh_from_db()
        self.assertEqual(len(m.generar(p.proxima)), 1)
        self.assertEqual(EjecucionMantenimiento.objects.filter(plan=p).count(), 2)

    def test_contrato_vencido_y_planes_inactivos(self):
        self.plan(con_contrato=True, contrato="CT-2025-08", contrato_vence=self.hoy - timedelta(days=1))
        self.plan(sistema="acceso", activo=False, proxima=self.hoy)
        creados = m.generar()
        self.assertEqual(len(creados), 1)
        self.assertIn("CT-2025-08", creados[0].descripcion)
        self.assertIn("el contrato está vencido", creados[0].descripcion)

    def test_pantallas(self):
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(reverse("cronograma:mantenimiento")).status_code, 403)
        self.client.force_login(self.coord)
        r = self.client.post(reverse("cronograma:mantenimiento_nuevo"), {
            "sede": self.sede.pk, "sistema": "alarma", "detalle": "Panel y 8 sensores", "frecuencia": "6",
            "proxima": (self.hoy + timedelta(days=60)).isoformat(), "activo": "on", "observaciones": ""})
        p = PlanMantenimiento.objects.get(sistema="alarma")
        self.assertRedirects(r, reverse("cronograma:mantenimiento_detalle", args=[p.pk]))
        self.assertEqual(p.creado_por, self.coord)
        # Con contrato hay que decir cuál
        r = self.client.post(reverse("cronograma:mantenimiento_editar", args=[p.pk]), {
            "sede": self.sede.pk, "sistema": "alarma", "frecuencia": "6", "proxima": p.proxima.isoformat(),
            "con_contrato": "on", "activo": "on"})
        self.assertContains(r, "Indique el número del contrato")

        r = self.client.get(reverse("cronograma:mantenimiento"))
        self.assertContains(r, "Avícola Sintética")
        self.assertContains(r, "Alarma de intrusión")

        # «Crear el ticket ya» sin esperar la fecha
        r = self.client.post(reverse("cronograma:mantenimiento_detalle", args=[p.pk]), {"accion": "generar"}, follow=True)
        t = p.ejecuciones.get().ticket
        self.assertContains(r, t.radicado)
        self.assertEqual(t.radicado_por, self.coord)
        p.refresh_from_db()
        self.assertEqual(p.proxima, m.sumar_meses(self.hoy + timedelta(days=60), 6))

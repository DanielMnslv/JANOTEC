"""Plan de mantenimiento por sede y sistema: 15 días antes se avisa a coordinación (no crea tickets solo) y el ticket
se crea con «Programar mantenimiento» en la ficha de la sede, con sus equipos; la siguiente fecha se corre por la frecuencia."""
from datetime import date, timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.models import ActaSoporte, Categoria, Ticket

from . import mantenimiento as m
from .models import EjecucionMantenimiento, EquipoInstalado, PlanMantenimiento


@override_settings(NOTIFICAR_SOPORTE_EMAIL="soporte@janotec.test")
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

    def test_avisa_15_dias_antes_sin_crear_tickets_ni_repetir(self):
        p = self.plan()
        self.plan(sistema="alarma", proxima=self.hoy + timedelta(days=40))
        avisados = m.avisar()
        self.assertEqual(avisados, [p])
        self.assertEqual(Ticket.objects.count(), 0)  # solo avisa: coordinación programa
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["coord@janotec.test"])
        self.assertIn(f"/soporte/sedes/{self.sede.pk}/", mail.outbox[0].body)
        self.assertEqual(m.avisar(), [])  # el mismo aviso no se repite
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(p.estado, "proximo")
        self.assertEqual([x.pk for x in m.por_programar()], [p.pk])

    def test_cliente_prueba_no_manda_correo(self):
        self.emp.razon_social = "PRUEBA Avícola"
        self.emp.save()
        self.plan()
        self.assertEqual(len(m.avisar()), 1)
        self.assertEqual(mail.outbox, [])

    def test_programar_crea_el_ticket_con_los_equipos_y_corre_la_fecha(self):
        p = self.plan()
        EquipoInstalado.objects.create(sede=self.sede, sistema="cctv", tipo="Cámara domo", marca="Hikvision",
                                       modelo="DS-2CD1123", serial="SN123", ubicacion="Portería")
        EquipoInstalado.objects.create(sede=self.sede, sistema="cctv", tipo="NVR viejo", ubicacion="Rack",
                                       estado=EquipoInstalado.Estado.RETIRADO)
        EquipoInstalado.objects.create(sede=self.sede, sistema="alarma", tipo="Panel", ubicacion="Oficina")
        t = m.programar(p, autor=self.coord)
        self.assertEqual((t.tipo, t.area, t.estado, t.sede, t.categoria.nombre),
                         (Ticket.Tipo.MANTENIMIENTO, Ticket.Area.TECNICA, Ticket.Estado.GENERADO, self.sede, "Mantenimiento CCTV IP"))
        self.assertIn("16 cámaras y NVR", t.descripcion)
        self.assertIn("Cámara domo · Hikvision DS-2CD1123 · Portería (serial SN123)", t.descripcion)
        self.assertNotIn("NVR viejo", t.descripcion)  # retirado
        self.assertNotIn("Panel", t.descripcion)  # otro sistema
        self.assertEqual(t.contacto_nombre, "Luis")
        self.assertEqual(mail.outbox, [])  # al cliente no le llega nada por esto
        p.refresh_from_db()
        self.assertEqual(p.proxima, m.sumar_meses(self.hoy + timedelta(days=10), 3))
        self.assertEqual(p.en_curso.ticket, t)
        self.assertEqual(p.estado, "programado")
        self.assertIsNone(m.programar(p))  # con el anterior abierto no se duplica
        self.assertEqual(p.ejecuciones.count(), 1)

        # El último realizado sale del acta del ticket
        self.assertIsNone(m.ultimo(p))
        ActaSoporte.objects.create(numero=90100, fecha=timezone.now(), empresa=self.emp, sede=self.sede, ticket=t)
        self.assertEqual(m.ultimo(p), self.hoy)
        Ticket.objects.filter(pk=t.pk).update(estado=Ticket.Estado.CERRADO, fecha_cierre=timezone.now())
        p.refresh_from_db()
        self.assertEqual(len([m.programar(p)]), 1)
        self.assertEqual(EjecucionMantenimiento.objects.filter(plan=p).count(), 2)

    def test_contrato_vencido_y_planes_inactivos(self):
        p = self.plan(con_contrato=True, contrato="CT-2025-08", contrato_vence=self.hoy - timedelta(days=1))
        self.plan(sistema="acceso", activo=False, proxima=self.hoy)
        self.assertEqual(m.avisar(), [p])
        t = m.programar(p)
        self.assertIn("CT-2025-08", t.descripcion)
        self.assertIn("el contrato está vencido", t.descripcion)

    def test_estado_del_plan(self):
        self.assertEqual(self.plan(proxima=self.hoy - timedelta(days=1)).estado, "vencido")
        self.assertEqual(self.plan(sistema="alarma", proxima=self.hoy + timedelta(days=60)).estado, "al_dia")
        self.assertEqual(self.plan(sistema="red", activo=False).estado, "inactivo")

    def test_las_pantallas_viejas_llevan_a_clientes_y_sedes(self):
        p = self.plan()
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(reverse("cronograma:mantenimiento")).status_code, 403)
        self.client.force_login(self.coord)
        self.assertRedirects(self.client.get(reverse("cronograma:mantenimiento")), reverse("clientes") + "?mantenimiento=con")
        self.assertRedirects(self.client.get(reverse("cronograma:mantenimiento_detalle", args=[p.pk])),
                             reverse("sede_detalle", args=[self.sede.pk]))
        self.assertRedirects(self.client.get(reverse("cronograma:mantenimiento_editar", args=[p.pk])),
                             reverse("sede_plan_editar", args=[self.sede.pk, p.pk]))
        self.assertRedirects(self.client.get(reverse("cronograma:mantenimiento_nuevo") + f"?sede={self.sede.pk}"),
                             reverse("sede_plan_nuevo", args=[self.sede.pk]))

"""Clientes y sedes (23/11/2026): ficha de la sede con su mantenimiento por sistema, equipos instalados con hoja de vida,
«Programar mantenimiento» y los equipos de la sede traídos al acta."""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from cronograma.models import EquipoInstalado, PlanMantenimiento

from .models import ActaSoporte, Categoria, EquipoActa, Ticket
from .tests import ActaBase


class ClientesSedesEquiposTests(ActaBase):
    def setUp(self):
        super().setUp()
        Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")
        self.hoy = timezone.localdate()
        self.ficha_sede = reverse("sede_detalle", args=[self.sede_a.pk])

    def equipo(self, **kw):
        datos = {"sede": self.sede_a, "sistema": "cctv", "tipo": "Cámara domo", "marca": "Hikvision", "modelo": "DS-2CD1123",
                 "serial": "SN-001", "ubicacion": "Portería principal"}
        datos.update(kw)
        return EquipoInstalado.objects.create(**datos)

    def test_coordinacion_carga_el_plan_y_los_equipos(self):
        self.client.force_login(self.supervisor)
        r = self.client.post(reverse("sede_plan_nuevo", args=[self.sede_a.pk]), {
            "sistema": "cctv", "frecuencia": "3", "ultimo_realizado": (self.hoy - timedelta(days=80)).isoformat(),
            "proxima": (self.hoy + timedelta(days=10)).isoformat(), "activo": "on"})
        self.assertRedirects(r, self.ficha_sede)
        plan = PlanMantenimiento.objects.get(sede=self.sede_a)
        self.assertEqual(plan.creado_por, self.supervisor)
        # No se repite el sistema en la sede
        r = self.client.post(reverse("sede_plan_nuevo", args=[self.sede_a.pk]), {
            "sistema": "cctv", "frecuencia": "6", "proxima": self.hoy.isoformat(), "activo": "on"})
        self.assertContains(r, "ya tiene un plan para ese sistema")

        foto = SimpleUploadedFile("domo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 60, content_type="image/png")
        datos = {"sistema": "cctv", "tipo": "Cámara domo", "marca": "Hikvision", "modelo": "DS-2CD1123", "serial": "SN-9",
                 "ubicacion": "Portería", "propiedad": "alquiler", "placa": "", "estado": "operativo",
                 "caracteristicas": "4 MP, IR 30 m"}
        r = self.client.post(reverse("equipo_nuevo", args=[self.sede_a.pk]), datos)
        self.assertContains(r, "llevan su placa de inventario")  # lo de JANOTEC lleva placa
        datos["placa"] = "CAM-001"
        r = self.client.post(reverse("equipo_nuevo", args=[self.sede_a.pk]), {**datos, "otro": "1"})
        self.assertRedirects(r, reverse("equipo_nuevo", args=[self.sede_a.pk]) + "?sistema=cctv", fetch_redirect_response=False)
        e = EquipoInstalado.objects.get(serial="SN-9")
        self.assertEqual((e.de_janotec, e.creado_por), (True, self.supervisor))

        r = self.client.get(self.ficha_sede)
        for texto in ("CCTV / videovigilancia", "Trimestral", "Programar mantenimiento", "Cámara domo", "SN-9", "Alquilado"):
            self.assertContains(r, texto)
        self.assertContains(self.client.get(reverse("clientes") + "?mantenimiento=por_programar"), self.emp_a.razon_social)
        self.assertNotIn(self.emp_a, self.client.get(reverse("clientes") + "?mantenimiento=sin").context["lista"])

    def test_programar_mantenimiento_desde_la_sede(self):
        plan = PlanMantenimiento.objects.create(sede=self.sede_a, sistema="cctv", frecuencia=6, proxima=self.hoy + timedelta(days=5))
        self.equipo()
        self.client.force_login(self.agente)
        self.assertEqual(self.client.post(self.ficha_sede, {"plan": plan.pk, "accion": "programar"}).status_code, 403)
        self.assertNotContains(self.client.get(self.ficha_sede), "Programar mantenimiento")  # el técnico solo consulta
        self.client.force_login(self.supervisor)
        r = self.client.post(self.ficha_sede, {"plan": plan.pk, "accion": "programar"}, follow=True)
        t = plan.ejecuciones.get().ticket
        self.assertContains(r, t.radicado)
        self.assertEqual(t.radicado_por, self.supervisor)
        self.assertIn("SN-001", t.descripcion)
        r = self.client.post(self.ficha_sede, {"plan": plan.pk, "accion": "programar"}, follow=True)
        self.assertContains(r, "Ya hay un mantenimiento en curso")
        self.assertEqual(plan.ejecuciones.count(), 1)

    def test_mi_dia_de_coordinacion_avisa_lo_por_programar(self):
        PlanMantenimiento.objects.create(sede=self.sede_a, sistema="alarma", frecuencia=12, proxima=self.hoy + timedelta(days=3))
        from . import mi_dia
        grupo = next(g for g in mi_dia.coordinacion(self.supervisor) if g["clave"] == "mantenimientos")
        self.assertEqual(len(grupo["items"]), 1)
        self.assertIn("Alarma de intrusión", grupo["items"][0]["detalle"])

    def test_el_acta_trae_los_equipos_de_la_sede_y_arma_la_hoja_de_vida(self):
        domo = self.equipo()
        nvr = self.equipo(tipo="NVR", serial="SN-NVR", ubicacion="Rack")
        ajeno = self.equipo(sede=self.sede_b, serial="SN-OTRA")
        self.client.force_login(self.agente)
        url = reverse("acta_nueva", args=[self.ticket.radicado])
        r = self.client.get(url)
        self.assertContains(r, "Equipos cargados en la sede (2)")
        self.assertContains(r, 'data-serial="SN-NVR"')
        self.assertNotContains(r, "SN-OTRA")
        r = self.client.post(url, self.datos_acta(**{
            "equipos-TOTAL_FORMS": "3",
            "equipos-0-equipo": "Cámara domo", "equipos-0-equipo_instalado": str(domo.pk), "equipos-0-accion": "reparado",
            "equipos-1-equipo": "NVR", "equipos-1-equipo_instalado": str(nvr.pk), "equipos-1-accion": "retirado",
            "equipos-1-propiedad": "cliente",
            "equipos-2-equipo": "Cámara ajena", "equipos-2-equipo_instalado": str(ajeno.pk), "equipos-2-propiedad": "cliente"}))
        self.assertEqual(r.status_code, 302)
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        enlaces = dict(acta.equipos.values_list("equipo", "equipo_instalado"))
        self.assertEqual(enlaces, {"Cámara domo": domo.pk, "NVR": nvr.pk, "Cámara ajena": None})  # solo de esta sede
        nvr.refresh_from_db()
        self.assertEqual(nvr.estado, EquipoInstalado.Estado.RETIRADO)  # se desmontó
        r = self.client.get(reverse("equipo_detalle", args=[domo.pk]))
        self.assertContains(r, f"Acta {acta.numero}")
        self.assertContains(r, "Reparado")
        self.assertEqual(EquipoActa.objects.filter(equipo_instalado=domo).count(), 1)

    def test_el_acta_del_mantenimiento_marca_los_equipos_del_sistema(self):
        from cronograma import mantenimiento
        plan = PlanMantenimiento.objects.create(sede=self.sede_a, sistema="cctv", frecuencia=6, proxima=self.hoy)
        self.equipo()
        self.equipo(sistema="alarma", tipo="Panel", serial="SN-PANEL")
        t = mantenimiento.programar(plan, self.supervisor)
        Ticket.objects.filter(pk=t.pk).update(asignado_a=self.agente, estado=Ticket.Estado.AUTORIZADO)
        self.client.force_login(self.agente)
        html = self.client.get(reverse("acta_nueva", args=[t.radicado])).content.decode()
        self.assertIn("vienen marcados los del mantenimiento", html)
        import re
        casillas = {s: c for c, s in re.findall(r'(<input class="form-check-input equipo-sede"[^>]*?data-serial="([^"]*)")', html)}
        self.assertIn(" checked", casillas["SN-001"])
        self.assertNotIn(" checked", casillas["SN-PANEL"])

    def test_bodega_edita_datos_pero_no_equipos(self):
        from accounts.models import Usuario
        bodega = Usuario.objects.create_user("bodega1", "bo@x.co", "Clave-segura-1", rol="bodega")
        self.client.force_login(bodega)
        self.assertEqual(self.client.get(reverse("cliente_detalle", args=[self.emp_a.pk])).status_code, 200)
        r = self.client.post(reverse("encargado_nuevo", args=[self.emp_a.pk]), {"nombre": "Paula Parra", "activo": "on"})
        self.assertRedirects(r, reverse("cliente_detalle", args=[self.emp_a.pk]))
        self.assertEqual(self.client.get(reverse("equipo_nuevo", args=[self.sede_a.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("sede_plan_nuevo", args=[self.sede_a.pk])).status_code, 403)

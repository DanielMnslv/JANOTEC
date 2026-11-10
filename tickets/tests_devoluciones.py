"""Devolver el ticket al área anterior (tickets/devoluciones.py): los cinco tramos, el aviso y la corrección."""
from django.core import mail
from django.urls import reverse

from accounts.models import Usuario
from comercial import servicios
from comercial.tests import BaseComercial
from tickets import devoluciones
from tickets.models import Devolucion, Ticket

T = Ticket
D = Devolucion.Tramo
MOTIVO = "Falta la dirección exacta y el contacto de la sede"


class DevolucionesTests(BaseComercial):
    def setUp(self):
        super().setUp()
        self.bodega = Usuario.objects.create_user("bodeguero", "bodega@janotec.test", "Clave-segura-1", rol="bodega")
        self.vendedor = Usuario.objects.create_user("vendedor", "vendedor@janotec.test", "Clave-segura-1", rol="comercial")
        self.admin_prop = Usuario.objects.create_user("yuri", "yuri@janotec.test", "Clave-segura-1", rol="bodega",
                                                      tambien_administrativo=True)

    # --- Ayudas -----------------------------------------------------------------------
    def en_levantamiento(self):
        """Ticket comercial que pidió visita de levantamiento: está en técnica, asignado al técnico."""
        t = self.crear()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "¿Domo o bala?", "jornada": "tarde"})
        t.refresh_from_db()
        t.asignado_a = self.tecnico
        t.save(update_fields=["asignado_a"])
        return t

    def con_informe(self):
        """El técnico ya devolvió el levantamiento: está en comercial, elaborando la propuesta."""
        t = self.en_levantamiento()
        self.client.force_login(self.tecnico)
        self.client.post(reverse("comercial:devolver", args=[t.radicado]),
                         {"necesidad": "Vigilar el parqueadero de noche", "materiales": "2 domos y 1 bala"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (T.Area.COMERCIAL, "elaborando"))
        return t

    def devolver(self, usuario, t, tramo, motivo=MOTIVO):
        self.client.force_login(usuario)
        return self.client.post(reverse("devolver_ticket", args=[t.radicado]), {"tramo": tramo, "motivo": motivo})

    # --- Técnica → comercial ------------------------------------------------------------
    def test_tecnica_devuelve_a_comercial_y_se_cierra_al_pedir_otra_vez_el_levantamiento(self):
        t = self.en_levantamiento()
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(t.get_absolute_url()), "Devolver al área anterior")
        self.devolver(self.coord, t, D.A_COMERCIAL, "corto")  # sin motivo claro no pasa
        t.refresh_from_db()
        self.assertEqual(t.area, T.Area.TECNICA)
        mail.outbox.clear()
        r = self.devolver(self.coord, t, D.A_COMERCIAL)
        self.assertRedirects(r, reverse("mi_dia"), fetch_redirect_response=False)
        t.refresh_from_db()
        self.assertEqual((t.area, t.estado, t.ficha.etapa, t.asignado_a), (T.Area.COMERCIAL, T.Estado.EN_PROCESO, "gestion", None))
        dev = t.devoluciones.get()
        self.assertTrue(dev.pendiente)
        self.assertTrue(any("asesor@janotec.test" in m.to and MOTIVO in m.body for m in mail.outbox))
        self.assertFalse(any("cli@a.co" in m.to for m in mail.outbox))  # al cliente no le llega nada
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje__contains=MOTIVO).exists())
        # Aparece en rojo en «Mi día» del asesor y en la ficha
        self.client.force_login(self.asesor)
        dia = self.client.get(reverse("mi_dia")).content.decode()
        self.assertIn("Devueltos a su área", dia)
        self.assertIn(t.radicado, dia)
        self.assertContains(self.client.get(reverse("comercial:detalle", args=[t.radicado])), MOTIVO)
        # Comercial completa los datos y vuelve a pedir la visita: la devolución queda corregida
        mail.outbox.clear()
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]),
                         {"nota": "Dirección: Calle 10 # 5-20, contacto Leidy", "jornada": "tarde"})
        dev.refresh_from_db()
        t.refresh_from_db()
        self.assertFalse(dev.pendiente)
        self.assertEqual(t.area, T.Area.TECNICA)
        self.assertTrue(any("coord@janotec.test" in m.to and "Corregido" in m.subject for m in mail.outbox))
        self.assertNotIn("Devueltos a su área", self.client.get(reverse("mi_dia")).content.decode())

    # --- Comercial → técnica --------------------------------------------------------------
    def test_comercial_devuelve_el_informe_a_tecnica_y_el_tecnico_lo_corrige_con_el_boton(self):
        t = self.con_informe()
        self.devolver(self.vendedor, t, D.A_TECNICA, "El informe no trae las medidas del cableado")
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa, t.asignado_a, t.estado),
                         (T.Area.TECNICA, "levantamiento", self.tecnico, T.Estado.RECEPCIONADO))
        dev = t.devoluciones.get()
        # El técnico lo ve en su ficha con el botón «Listo, corregido»
        self.client.force_login(self.tecnico)
        self.assertContains(self.client.get(t.get_absolute_url()), "Listo, corregido")
        self.client.post(reverse("resolver_devolucion", args=[dev.pk]), {"nota": "x"})  # sin decir qué se corrigió no pasa
        dev.refresh_from_db()
        self.assertTrue(dev.pendiente)
        mail.outbox.clear()
        self.client.post(reverse("resolver_devolucion", args=[dev.pk]), {"nota": "Se agregaron 80 m de UTP y las fotos"})
        dev.refresh_from_db()
        t.refresh_from_db()
        self.assertFalse(dev.pendiente)
        self.assertEqual(dev.resuelta_por, self.tecnico)
        self.assertEqual((t.area, t.ficha.etapa, t.estado, t.asignado_a), (T.Area.COMERCIAL, "elaborando", T.Estado.EN_PROCESO, None))
        self.assertTrue(any("vendedor@janotec.test" in m.to for m in mail.outbox))  # le avisa a quien lo devolvió

    def test_si_el_tecnico_reenvia_el_levantamiento_la_devolucion_se_cierra_sola(self):
        t = self.con_informe()
        self.devolver(self.asesor, t, D.A_TECNICA, "El informe no trae las medidas del cableado")
        self.client.force_login(self.tecnico)
        self.client.post(reverse("comercial:devolver", args=[t.radicado]),
                         {"necesidad": "Vigilar el parqueadero de noche", "materiales": "2 domos, 1 bala y 80 m UTP"})
        t.refresh_from_db()
        self.assertEqual(t.area, T.Area.COMERCIAL)
        self.assertFalse(t.devoluciones.get().pendiente)
        self.assertIn("80 m UTP", t.ficha.informe.materiales)

    # --- Administrativa → comercial ---------------------------------------------------------
    def test_administrativa_devuelve_el_requerimiento_a_comercial(self):
        t = self.con_informe()
        opciones = [tramo for tramo, _ in devoluciones.opciones(t, self.admin_prop)]
        self.assertEqual(opciones, [D.A_TECNICA, D.A_REQUERIMIENTO])
        self.assertEqual([tramo for tramo, _ in devoluciones.opciones(t, self.vendedor)], [D.A_TECNICA])  # comercial no se devuelve a sí misma
        self.devolver(self.admin_prop, t, D.A_REQUERIMIENTO, "Falta saber cuántos metros hay hasta el rack")
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (T.Area.COMERCIAL, "gestion"))
        servicios.registrar_informe(t.ficha, self.asesor, {"necesidad": "Vigilar el parqueadero", "materiales": "80 m UTP hasta el rack"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "elaborando")
        self.assertFalse(t.devoluciones.get().pendiente)

    # --- Bodega y técnica con la propuesta aprobada --------------------------------------------
    def en_instalacion(self, estado_solicitud="validacion"):
        from inventario.models import SolicitudMaterial
        t = self.con_informe()
        t.ficha.etapa = "instalacion"
        t.ficha.save(update_fields=["etapa"])
        t.area = T.Area.TECNICA
        t.save(update_fields=["area"])
        s = SolicitudMaterial.objects.create(ticket=t, origen=SolicitudMaterial.Origen.COMERCIAL, estado=estado_solicitud,
                                             solicitado_por=self.asesor)
        return t, s

    def test_bodega_devuelve_la_lista_de_material_y_se_cierra_al_reenviarla(self):
        from inventario import solicitudes
        t, s = self.en_instalacion()
        t.cambiar_estado(T.Estado.EN_INVENTARIO, self.asesor)
        self.devolver(self.bodega, t, D.A_LISTA, "La propuesta dice 2 domos pero el informe pide 3")
        s.refresh_from_db()
        self.assertEqual(s.estado, "borrador")
        self.assertIn("2 domos", s.motivo_devolucion)
        self.client.force_login(self.asesor)
        self.assertIn(t.radicado, self.client.get(reverse("mi_dia")).content.decode())
        solicitudes.enviar_a_bodega(s, self.asesor, "3 Cámara domo")
        self.assertFalse(t.devoluciones.get().pendiente)

    def test_tecnica_devuelve_a_bodega_y_bodega_lo_regresa_corregido(self):
        t, s = self.en_instalacion(estado_solicitud="entregada")
        t.cambiar_estado(T.Estado.RECEPCIONADO, self.coord)
        self.assertFalse(any(tramo == D.A_BODEGA for tramo, _ in devoluciones.opciones(t, self.bodega)))  # bodega no la hace
        self.devolver(self.coord, t, D.A_BODEGA, "Faltan 2 conectores RJ45 y la fuente de 12 V")
        t.refresh_from_db()
        self.assertEqual(t.estado, T.Estado.EN_INVENTARIO)
        dev = t.devoluciones.get()
        self.client.force_login(self.bodega)
        self.assertContains(self.client.get(reverse("inventario:solicitud", args=[s.pk])), "Faltan 2 conectores")
        self.client.post(reverse("resolver_devolucion", args=[dev.pk]), {"nota": "Se entregaron los conectores y la fuente"})
        t.refresh_from_db()
        self.assertEqual(t.estado, T.Estado.RECEPCIONADO)
        dev.refresh_from_db()
        self.assertFalse(dev.pendiente)

    def test_si_coordinacion_lo_saca_de_inventario_la_devolucion_a_bodega_se_cierra(self):
        t, _ = self.en_instalacion(estado_solicitud="entregada")
        t.cambiar_estado(T.Estado.RECEPCIONADO, self.coord)
        devoluciones.devolver(t, self.coord, D.A_BODEGA, "Faltan 2 conectores RJ45")
        t.refresh_from_db()
        t.reingresar(self.coord, "Ya está el material")
        self.assertFalse(t.devoluciones.get().pendiente)

    # --- Permisos ---------------------------------------------------------------------------
    def test_solo_el_area_que_lo_tiene_puede_devolverlo_y_solo_la_anterior_corregirlo(self):
        t = self.con_informe()
        self.assertEqual(devoluciones.opciones(t, self.tecnico), [])  # está en comercial: técnica no lo devuelve
        self.assertEqual(devoluciones.opciones(t, self.cli), [])
        self.client.force_login(self.cli)
        self.client.post(reverse("devolver_ticket", args=[t.radicado]), {"tramo": D.A_TECNICA, "motivo": MOTIVO})
        self.assertFalse(t.devoluciones.exists())
        dev = devoluciones.devolver(t, self.vendedor, D.A_TECNICA, MOTIVO)
        self.assertFalse(devoluciones.puede_resolver(self.vendedor, dev))  # quien devuelve no se lo corrige
        self.assertTrue(devoluciones.puede_resolver(self.coord, dev))
        t.refresh_from_db()
        self.assertEqual([tramo for tramo, _ in devoluciones.opciones(t, self.vendedor)], [])  # ya no lo tiene

"""«Cerrar y finalizar» sin propuesta: coordinación técnica cierra cuando el área técnica se encargó de todo."""
from django.urls import reverse

from comercial.tests import BaseComercial
from tickets.models import Seguimiento, Ticket

T = Ticket
MOTIVO = "Se cambió la fuente con material de bodega; no requiere cotización"


class FinalizarSinPropuestaTests(BaseComercial):
    def en_levantamiento(self):
        t = self.crear()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "¿Domo o bala?", "jornada": "tarde"})
        t.refresh_from_db()
        return t

    def finalizar(self, usuario, t, motivo=MOTIVO, **extra):
        self.client.force_login(usuario)
        return self.client.post(reverse("finalizar_sin_propuesta", args=[t.radicado]), {"motivo": motivo, **extra})

    def test_coordinacion_cierra_el_levantamiento_sin_propuesta(self):
        t = self.en_levantamiento()
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(t.get_absolute_url()), "Cerrar y finalizar")
        self.finalizar(self.coord, t, "corto")  # sin motivo no se cierra
        t.refresh_from_db()
        self.assertTrue(t.abierto)
        self.finalizar(self.coord, t, avisar_cliente="1")
        t.refresh_from_db()
        self.assertEqual((t.estado, t.area, t.ficha.etapa), (T.Estado.CERRADO, T.Area.TECNICA, "cancelada"))
        self.assertIsNotNone(t.fecha_cierre)
        nota = t.seguimientos.get(tipo=Seguimiento.Tipo.NOTA_INTERNA, mensaje__contains="sin propuesta")
        self.assertIn(MOTIVO, nota.mensaje)
        self.assertTrue(nota.interno)
        self.assertFalse(t.seguimientos.get(estado_nuevo=T.Estado.CERRADO).interno)  # el cliente ve el cierre
        self.assertNotContains(self.client.get(t.get_absolute_url()), "Cerrar y finalizar (el área")

    def test_solo_coordinacion_puede_finalizar(self):
        t = self.en_levantamiento()
        t.asignado_a = self.tecnico
        t.save(update_fields=["asignado_a"])
        self.client.force_login(self.tecnico)
        self.assertNotContains(self.client.get(t.get_absolute_url()), "Cerrar y finalizar")
        self.assertEqual(self.finalizar(self.tecnico, t).status_code, 403)
        t.refresh_from_db()
        self.assertTrue(t.abierto)

    def test_sin_avisar_al_cliente_el_cierre_queda_interno(self):
        t = self.en_levantamiento()
        self.finalizar(self.coord, t)
        t.refresh_from_db()
        self.assertEqual(t.estado, T.Estado.CERRADO)
        self.assertTrue(t.seguimientos.get(estado_nuevo=T.Estado.CERRADO).interno)

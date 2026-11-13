"""Indicadores comerciales y de proyectos del trimestre (pedidos por la coordinación TIC el 12/11/2026).

Un proyecto sintético: propuesta con cotizador al 25 % de margen (costo 750.000 → venta 1.000.000), material que sale de
bodega y vuelve en parte, un servicio subcontratado en la orden de compra, el cierre del ticket y las actas del
trimestre. Todo se compara con el trimestre anterior.
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Usuario
from inventario import servicios
from inventario.models import Insumo, LineaOrdenCompra, Lugar, MovimientoInsumo, OrdenCompra
from tickets.models import ActaSoporte, Categoria, Ticket

from . import trimestre as tr
from .models import Cotizacion, FichaComercial, LineaCotizacion, PropuestaVersion
from .tests import BaseComercial

T = MovimientoInsumo.Tipo


def momento(dia):
    """Mediodía de esa fecha, con zona horaria."""
    return timezone.make_aware(datetime.combine(dia, time(12)))


class TrimestreTests(BaseComercial):
    def setUp(self):
        super().setUp()
        self.t4 = tr.Trimestre(2026, 4)
        self.bodega = Usuario.objects.create_user("bod", "bod@janotec.test", "Clave-segura-1", rol="bodega")
        self.cat = Categoria.objects.create(nombre="Instalación de CCTV")

    def proyecto(self, empresa, enviada, respondida, estado="aprobada", costos=((Decimal("750000"), 1),)):
        """Ticket con ficha y una versión con cotizador al 25 %."""
        t = Ticket.objects.create(empresa=empresa, solicitante=self.cli, categoria=self.cat, asunto="Cámaras", descripcion="x")
        ficha = FichaComercial.objects.create(ticket=t, etapa=FichaComercial.Etapa.APROBADA)
        v = PropuestaVersion.objects.create(ficha=ficha, numero=1, codigo="PE-2026-10-001-V1", estado=estado,
                                            enviada=momento(enviada), respondida=momento(respondida) if respondida else None,
                                            valor=Decimal("1000000"))
        cot = Cotizacion.objects.create(ficha=ficha, version=v, margen=Decimal("25"))
        for costo, cantidad in costos:
            LineaCotizacion.objects.create(cotizacion=cot, descripcion="Cámara bala 4 MP", cantidad=cantidad,
                                           valor_unitario=costo, iva=19)
        return t

    def test_trimestres(self):
        self.assertEqual((self.t4.desde, self.t4.hasta), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertEqual(self.t4.anterior.clave, "2026-3")
        self.assertEqual(tr.Trimestre(2026, 1).anterior.clave, "2025-4")
        self.assertEqual(tr.Trimestre.de_texto("basura", date(2026, 11, 13)).clave, "2026-4")

    def test_propuestas_margen_y_clientes_nuevos(self):
        # Cliente A ya era cliente (acta de julio); Cliente B es nuevo en el trimestre 4
        ActaSoporte.objects.create(numero=5001, fecha=momento(date(2026, 7, 5)), empresa=self.emp, resultado="solucionado")
        self.proyecto(self.emp, date(2026, 10, 2), date(2026, 10, 9))
        self.proyecto(self.otra, date(2026, 10, 3), date(2026, 10, 10), costos=((Decimal("600000"), 1),))
        self.proyecto(self.emp, date(2026, 10, 4), date(2026, 10, 11), estado="rechazada")
        self.proyecto(self.emp, date(2026, 8, 4), date(2026, 8, 11))  # trimestre anterior
        # Lo de prueba no cuenta
        prueba = Empresa.objects.create(razon_social="PRUEBA Cliente", nit="99")
        self.proyecto(prueba, date(2026, 10, 2), date(2026, 10, 9))

        v = tr.medir(self.t4.desde, self.t4.hasta)
        self.assertEqual((v["entregadas"], v["aprobadas"], v["rechazadas"]), (3, 2, 1))
        self.assertEqual(v["aprobacion"], 66.7)
        # Venta 1.000.000 + 800.000; costo 750.000 + 600.000 → (1.800.000 − 1.350.000) ÷ 1.800.000 = 25 %
        self.assertEqual(v["margen_utilidad"], 25.0)
        self.assertEqual(v["clientes_nuevos"], 1)
        self.assertEqual(v["utilidad_nuevos"], Decimal("200000"))

        r = tr.calcular(self.t4)
        aprobadas = next(i for i in r["indicadores"] if i.clave == "aprobadas")
        self.assertEqual((aprobadas.valor, aprobadas.anterior, aprobadas.tendencia), (2, 1, "mejor"))

    def test_rentabilidad_real_del_proyecto_entregado(self):
        t = self.proyecto(self.emp, date(2026, 9, 20), date(2026, 9, 25))
        cable = Insumo.objects.create(codigo="CAB-1", descripcion="Cable UTP", unidad="Metro")
        servicios.mover_insumo(cable, 100, T.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA, self.bodega,
                               valor_unitario=Decimal("2000"), iva=19)
        servicios.mover_insumo(cable, 60, T.ENTREGA, Lugar.BODEGA, Lugar.TECNICO, self.bodega, tecnico=self.tecnico, ticket=t)
        servicios.mover_insumo(cable, 10, T.DEVOLUCION, Lugar.TECNICO, Lugar.BODEGA, self.bodega, tecnico=self.tecnico, ticket=t)
        oc = OrdenCompra.objects.create(ticket=t, estado="enviada")
        LineaOrdenCompra.objects.create(orden=oc, descripcion="Mano de obra subcontratada", cantidad=1,
                                        valor_unitario=Decimal("300000"), es_servicio=True)
        LineaOrdenCompra.objects.create(orden=oc, descripcion="Cable (entra a bodega)", cantidad=100, valor_unitario=Decimal("2000"))
        Ticket.objects.filter(pk=t.pk).update(estado=Ticket.Estado.CERRADO, fecha_cierre=momento(date(2026, 10, 20)))

        costo = tr.costo_real(t)
        self.assertEqual(costo["material"], Decimal("100000"))  # 50 m netos × 2.000 sin IVA
        self.assertEqual(costo["servicios"], Decimal("300000"))  # el cable de la orden no se suma dos veces
        v = tr.medir(self.t4.desde, self.t4.hasta)
        self.assertEqual(v["entregados"], 1)
        p = v["proyectos"][0]
        self.assertEqual((p.venta, p.utilidad, p.margen_real, p.margen_cotizado), (Decimal("1000000"), Decimal("600000"), 60.0, 25.0))
        self.assertEqual(v["margen_consolidado"], 60.0)

    def test_desempeno_por_actas_y_pantalla(self):
        for n, resultado in enumerate(["solucionado", "solucionado", "solucionado", "parcial", "recomendaciones"]):
            ActaSoporte.objects.create(numero=6000 + n, fecha=momento(date(2026, 11, 2)), empresa=self.emp,
                                       tecnico=self.tecnico, resultado=resultado)
        v = tr.medir(self.t4.desde, self.t4.hasta)
        self.assertEqual(v["desempeno"], 75.0)
        fila = tr.por_tecnico(self.t4.desde, self.t4.hasta)[0]
        self.assertEqual((fila["solucionadas"], fila["parciales"], fila["desempeno"]), (3, 1, 75.0))

        url = reverse("reportes_trimestre") + "?t=2026-4"
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.coord)
        r = self.client.get(url)
        self.assertContains(r, "Desempeño del área técnica")
        self.assertContains(r, "75,0 %")
        r = self.client.get(url + "&formato=excel")
        self.assertEqual(r["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("indicadores_trimestre_2026-4.xlsx", r["Content-Disposition"])

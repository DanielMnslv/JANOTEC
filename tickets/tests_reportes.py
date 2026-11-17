"""Metas configurables (tickets/metas.py), Reportes en Excel (tickets/reportes.py) y el retiro de «Salud de los procesos»."""
import io
from types import SimpleNamespace

from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from openpyxl import load_workbook

from accounts.models import Usuario

from . import metas, reportes
from .models import Categoria, Empresa, MetaIndicador, Ticket


class MetasTests(TestCase):
    def setUp(self):
        self.direccion = Usuario.objects.create_user("dir", "dir@x.co", "clave-prueba-123", rol="direccion")
        self.tec = Usuario.objects.create_user("tec", "tec@x.co", "clave-prueba-123", rol="agente")

    def test_sin_cambios_rigen_los_defectos(self):
        self.assertEqual(metas.valores(), metas.DEFECTOS)
        self.assertEqual(metas.valores()["cierre"], 90)

    def test_lo_guardado_manda_y_vacio_quita_la_meta(self):
        MetaIndicador.objects.create(clave="cierre", valor=95)
        MetaIndicador.objects.create(clave="satisfaccion", valor=None)
        v = metas.valores()
        self.assertEqual((v["cierre"], v["satisfaccion"]), (95.0, None))
        indicadores = [SimpleNamespace(clave="cierre", meta=1), SimpleNamespace(clave="otro", meta=7)]
        metas.aplicar(indicadores)
        self.assertEqual([i.meta for i in indicadores], [95.0, 7])

    def test_leer_valida_el_numero(self):
        d = metas.POR_CLAVE
        self.assertIsNone(metas.leer("  ", d["cierre"]))
        self.assertEqual(float(metas.leer("87,5", d["cierre"])), 87.5)
        self.assertEqual(float(metas.leer("$ 1.500.000", d["utilidad_nuevos"])), 1500000)
        for texto, clave in (("120", "cierre"), ("-1", "solucion_dias"), ("11", "satisfaccion"), ("abc", "cierre")):
            with self.assertRaises(ValueError, msg=texto):
                metas.leer(texto, d[clave])

    def test_guardar_solo_lo_que_cambio_y_nada_si_hay_errores(self):
        datos = {f"meta_{d.clave}": "" if d.defecto is None else str(d.defecto) for d in metas.CATALOGO}
        datos["meta_cierre"] = "92"
        cambiadas, errores = metas.guardar(datos, self.direccion)
        self.assertEqual(([d.clave for d in cambiadas], errores), (["cierre"], []))
        self.assertEqual(MetaIndicador.objects.get().actualizada_por, self.direccion)
        datos.update(meta_cierre="80", meta_aprobacion="150")
        cambiadas, errores = metas.guardar(datos, self.direccion)
        self.assertEqual((cambiadas, len(errores)), ([], 1))
        self.assertEqual(metas.valores()["cierre"], 92.0)  # no se guardó nada

    def test_pagina_de_metas(self):
        url = reverse("indicadores_metas")
        self.client.force_login(self.tec)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.direccion)
        self.assertEqual(self.client.get(url).status_code, 200)
        datos = {f"meta_{d.clave}": "" if d.defecto is None else str(d.defecto) for d in metas.CATALOGO}
        datos["meta_primera_visita"] = "75"
        r = self.client.post(url, datos)
        self.assertRedirects(r, url)
        self.assertEqual(metas.valores()["primera_visita"], 75.0)


class ReportesDescargablesTests(TestCase):
    url = reverse("reportes_descargables")

    def setUp(self):
        self.direccion = Usuario.objects.create_user("dir", "dir@x.co", "clave-prueba-123", rol="direccion")
        self.bodega = Usuario.objects.create_user("bod", "bod@x.co", "clave-prueba-123", rol="bodega")
        self.tec = Usuario.objects.create_user("tec", "tec@x.co", "clave-prueba-123", rol="agente")
        cat = Categoria.objects.create(nombre="CCTV")
        for nombre, nit in (("Cliente real", "1"), ("PRUEBA S.A.S.", "2")):
            emp = Empresa.objects.create(razon_social=nombre, nit=nit)
            cli = Usuario.objects.create_user(f"cli{nit}", f"c{nit}@x.co", "clave-prueba-123", rol="cliente", empresa=emp)
            Ticket.objects.create(empresa=emp, solicitante=cli, categoria=cat, asunto=f"Falla {nombre}", descripcion="y")

    def libro(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        return r, load_workbook(io.BytesIO(r.content)).active

    def test_direccion_ve_y_baja_todos(self):
        self.client.force_login(self.direccion)
        r = self.client.get(self.url, {"periodo": "anio"})
        self.assertContains(r, "Descargar Excel", count=len(reportes.REPORTES))
        for rep in reportes.REPORTES:
            r, hoja = self.libro(periodo="anio", reporte=rep.clave)
            self.assertIn(f"{rep.clave}_", r["Content-Disposition"])

    def test_tickets_sin_los_clientes_de_prueba(self):
        self.client.force_login(self.direccion)
        _, hoja = self.libro(periodo="mes", reporte="tickets")
        clientes = [hoja.cell(n, 3).value for n in range(2, hoja.max_row + 1)]
        self.assertIn("Cliente real", clientes)
        self.assertNotIn("PRUEBA S.A.S.", clientes)
        self.assertIn("1 fila(s)", hoja.cell(hoja.max_row, 1).value)

    def test_cada_quien_solo_lo_suyo(self):
        self.client.force_login(self.bodega)
        r = self.client.get(self.url)
        self.assertEqual([x.clave for x in reportes.disponibles(self.bodega)], ["ordenes_compra"])
        self.assertContains(r, "Órdenes de compra")
        self.assertEqual(self.client.get(self.url, {"reporte": "tickets"}).status_code, 404)
        self.libro(reporte="ordenes_compra")
        self.client.force_login(self.tec)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_enlace_en_el_menu(self):
        self.client.force_login(self.direccion)
        self.assertContains(self.client.get(reverse("indicadores_centro")), "Reportes en Excel")
        self.client.force_login(self.tec)
        self.assertNotContains(self.client.get(reverse("panel")), "Reportes en Excel")


class SaludRetiradaTests(TestCase):
    def test_ya_no_existen_las_paginas(self):
        for nombre in ("salud_procesos", "hallazgo_revisar"):
            with self.assertRaises(NoReverseMatch):
                reverse(nombre, args=[1]) if nombre == "hallazgo_revisar" else reverse(nombre)

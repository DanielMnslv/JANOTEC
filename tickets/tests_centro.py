"""Centro de indicadores (tickets/centro.py): periodo unificado, comparación con el anterior completo, pestañas y Excel."""
from datetime import date

from django.test import TestCase
from django.urls import reverse

from accounts.models import Usuario

from . import centro


class PeriodoTests(TestCase):
    hoy = date(2026, 11, 17)  # martes

    def p(self, **params):
        return centro.periodo(params, self.hoy)

    def test_mes_en_curso_frente_al_mes_anterior_completo(self):
        p = self.p(periodo="mes")
        self.assertEqual((p.desde, p.hasta, p.fin, p.en_curso), (date(2026, 11, 1), self.hoy, date(2026, 11, 30), True))
        self.assertEqual(p.anterior, (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual((p.nombre, p.nombre_anterior), ("Noviembre de 2026", "Octubre de 2026"))

    def test_trimestre_semestre_y_anio(self):
        p = self.p(periodo="trimestre")
        self.assertEqual((p.nombre, p.nombre_anterior), ("Trimestre 4 de 2026", "Trimestre 3 de 2026"))
        self.assertEqual(p.anterior, (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(self.p(periodo="trimestre_anterior").nombre_anterior, "Trimestre 2 de 2026")
        self.assertEqual(self.p(periodo="semestre").nombre_anterior, "Semestre 1 de 2026")
        p = self.p(periodo="anio_anterior")
        self.assertEqual((p.nombre, p.en_curso, p.nombre_anterior), ("Año 2025", False, "Año 2024"))

    def test_semana_y_rango(self):
        p = self.p(periodo="semana")
        self.assertEqual((p.desde, p.fin, p.anterior[0]), (date(2026, 11, 16), date(2026, 11, 22), date(2026, 11, 9)))
        self.assertEqual(p.nombre, "Semana del 16/11 al 22/11/2026")
        p = self.p(periodo="rango", desde="2026-10-10", hasta="2026-10-01")  # al revés: se ordena
        self.assertEqual((p.desde, p.fin, p.anterior), (date(2026, 10, 1), date(2026, 10, 10), (date(2026, 9, 21), date(2026, 9, 30))))
        # Un rango de un trimestre exacto se compara con el trimestre anterior entero
        self.assertEqual(self.p(periodo="rango", desde="2026-04-01", hasta="2026-06-30").nombre_anterior, "Trimestre 1 de 2026")
        self.assertEqual(self.p(periodo="otro").clave, "mes")


class CentroVistaTests(TestCase):
    url = reverse("indicadores_centro")

    def setUp(self):
        self.direccion = Usuario.objects.create_user("dir", "dir@x.co", "clave-prueba-123", rol="direccion")
        self.coord = Usuario.objects.create_user("coord", "coord@x.co", "clave-prueba-123", rol="supervisor")
        self.tec = Usuario.objects.create_user("tec", "tec@x.co", "clave-prueba-123", rol="agente")

    def test_todas_las_pestanas_cargan_y_bajan_en_excel(self):
        self.client.force_login(self.direccion)
        for pestana in ("resumen", "tecnica", "bodega", "comercial", "cronograma"):
            for periodo in ("mes", "trimestre", "semana", "anio_anterior"):
                r = self.client.get(self.url, {"pestana": pestana, "periodo": periodo})
                self.assertEqual((r.status_code, r.context["pestana"]), (200, pestana), (pestana, periodo))
            r = self.client.get(self.url, {"pestana": pestana, "periodo": "mes", "formato": "excel"})
            self.assertIn("spreadsheetml", r["Content-Type"], pestana)
            self.assertIn(f"indicadores_{pestana}_", r["Content-Disposition"])

    def test_resumen_trae_lo_clave_de_cada_area(self):
        self.client.force_login(self.direccion)
        r = self.client.get(self.url)
        self.assertEqual([g["clave"] for g in r.context["grupos"]], ["tecnica", "bodega", "comercial"])
        self.assertContains(r, "Eficacia en el cierre")
        self.assertContains(r, "Margen de rentabilidad consolidado")

    def test_la_coordinacion_tambien_ve_lo_comercial_y_el_tecnico_no_entra(self):
        self.client.force_login(self.coord)  # la coordinación TIC pidió los indicadores comerciales y de proyectos
        r = self.client.get(self.url, {"pestana": "comercial"})
        self.assertEqual(r.context["pestana"], "comercial")
        self.assertContains(r, "Margen de rentabilidad")
        self.client.force_login(self.tec)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_las_direcciones_antiguas_llevan_al_centro(self):
        self.client.force_login(self.direccion)
        self.assertRedirects(self.client.get(reverse("reportes") + "?periodo=anio"),
                             self.url + "?periodo=anio&pestana=tecnica", fetch_redirect_response=False)
        r = self.client.get(reverse("reportes_trimestre") + "?t=2026-3")
        self.assertIn("pestana=comercial", r["Location"])
        self.assertIn("desde=2026-07-01&hasta=2026-09-30", r["Location"])
        self.assertIn("pestana=cronograma", self.client.get(reverse("cronograma:indicadores") + "?d=2026-11-16")["Location"])
        menu = self.client.get(reverse("mi_dia")).context["menu"]
        enlaces = [e.nombre for g in menu["propios"] + menu["otros"] for e in g.enlaces]
        self.assertIn("Indicadores", enlaces)
        self.assertNotIn("Reportes", enlaces)

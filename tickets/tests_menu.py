"""Menú lateral según el rol (tickets/menu.py): lo que la persona trabaja va desplegado, lo demás en «Más áreas»."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import Usuario


def usuario(nombre, **campos):
    return Usuario.objects.create_user(nombre, f"{nombre}@x.co", "clave-prueba-123", **campos)


class MenuPorRolTests(TestCase):
    def menu(self, u, url="mi_dia"):
        self.client.force_login(u)
        return self.client.get(reverse(url)).context["menu"]

    @staticmethod
    def claves(grupos):
        return [g.clave for g in grupos]

    def test_direccion_ve_primero_lo_suyo_y_lo_tecnico_en_mas_areas(self):
        m = self.menu(usuario("miguel", rol="direccion", tambien_tecnico=True))
        self.assertEqual(self.claves(m["propios"]), ["comercial", "administrativa", "gestion"])
        self.assertEqual(self.claves(m["otros"]), ["tecnica", "bodega", "financiera"])
        self.assertFalse(m["otros_activo"])

    def test_superusuario_con_rol_de_coordinador_ve_desplegado_solo_lo_tecnico(self):
        u = usuario("guana", rol="supervisor", is_superuser=True)
        m = self.menu(u)
        self.assertEqual(self.claves(m["propios"]), ["tecnica", "gestion"])
        self.assertIn("bodega", self.claves(m["otros"]))
        r = self.client.get(reverse("mi_dia"))
        self.assertContains(r, "Más áreas")
        self.assertContains(r, reverse("inventario:productos"))  # sigue teniendo el acceso, plegado

    def test_la_pagina_abierta_despliega_su_grupo_dentro_de_mas_areas(self):
        u = usuario("guana2", rol="supervisor", is_superuser=True)
        self.client.force_login(u)
        r = self.client.get(reverse("inventario:productos"))
        self.assertTrue(r.context["menu"]["otros_activo"])
        self.assertContains(r, 'data-clave="mas" open data-activo')

    def test_bodega_y_administrativa_arranca_con_mi_trabajo_abierto(self):
        m = self.menu(usuario("yuri", rol="bodega", tambien_administrativo=True))
        grupos = {g.clave: g.abierto for g in m["propios"]}
        self.assertEqual(grupos["bodega-0"], True)
        self.assertEqual(grupos["administrativa"], True)
        self.assertFalse(any(v for k, v in grupos.items() if k.startswith("bodega-") and k != "bodega-0"))
        self.assertEqual(m["otros"], [])

    def test_superusuario_sin_area_lo_ve_todo_desplegado(self):
        m = self.menu(usuario("programador", is_superuser=True))
        self.assertEqual(m["otros"], [])
        self.assertIn("gestion", self.claves(m["propios"]))

    def test_tecnico_y_cliente_sin_mas_areas(self):
        self.assertEqual(self.menu(usuario("tec", rol="agente"))["otros"], [])
        self.client.force_login(usuario("tec2", rol="agente"))
        self.assertNotContains(self.client.get(reverse("mi_dia")), "Más áreas")

class MenuPasoAPasoTests(TestCase):
    """Cada área ve sus opciones en el orden de su trabajo, numeradas; las de apoyo van debajo sin número."""

    def pasos(self, u, url="mi_dia", **params):
        self.client.force_login(u)
        menu = self.client.get(reverse(url), params, follow=True).context["menu"]
        return {g.clave: [(e.paso, e.nombre) for e in g.enlaces] for g in menu["propios"]}

    def test_coordinador_sigue_el_paso_a_paso_del_ticket(self):
        tecnica = self.pasos(usuario("coord", rol="supervisor"))["tecnica"]
        numerados = [n for p, n in tecnica if p]
        self.assertEqual(numerados[:3], ["Recepcionar", "Asignar técnico", "Programar visita"])
        self.assertEqual(numerados[-2:], ["Informe", "Confirmar y cerrar"])
        self.assertEqual([p for p, _ in tecnica if p], list(range(1, len(numerados) + 1)))
        self.assertTrue(all(p == 0 for p, _ in tecnica[len(numerados):]))  # el apoyo, debajo

    def test_la_vista_abierta_marca_su_paso(self):
        u = usuario("coord2", rol="supervisor")
        self.client.force_login(u)
        r = self.client.get(reverse("panel"), {"estado": "por_programar"})
        activos = [e.nombre for g in r.context["menu"]["propios"] for e in g.enlaces if e.activo]
        self.assertEqual(activos, ["Programar visita"])
        self.assertContains(r, 'class="menu-paso"')

    def test_tecnico_bodega_comercial_y_administrativa(self):
        self.assertEqual(self.pasos(usuario("tec", rol="agente"))["tecnica"][0], (1, "Mis visitas por atender"))
        bodega = self.pasos(usuario("bod", rol="bodega"))
        self.assertEqual([n for p, n in bodega["bodega-0"] if p],
                         ["Tickets en bodega", "Validar solicitudes de material", "Comprar lo que falta", "Recibir compras",
                          "Entregar al técnico", "Devoluciones y cuadre"])
        self.assertNotIn("Entregar a técnico", [n for _, n in bodega["bodega-3"]])  # no se repite abajo
        self.assertEqual(self.pasos(usuario("ases", rol="comercial"))["comercial"][0], (1, "Nuevo requerimiento"))
        self.assertEqual(self.pasos(usuario("adm", rol="administrativo"))["administrativa"][0], (1, "Propuestas por armar"))

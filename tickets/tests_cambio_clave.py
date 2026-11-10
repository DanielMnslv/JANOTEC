"""Correo con el enlace personal para que cada usuario cree su propia contraseña (invitar_cambio_clave)."""
import re
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Empresa, Usuario


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", SITIO_URL="https://app.janotec.app")
class InvitarCambioClaveTests(TestCase):
    def setUp(self):
        u = Usuario.objects.create_user
        self.miguel = u("mtibamoso", "miguel@janotec.test", "Azar-asignada-1", rol="direccion", first_name="Miguel")
        self.jguana = u("jguana", "jguana@janotec.test", "Azar-asignada-2", rol="supervisor")
        Usuario.objects.create_superuser("admin", "admin@janotec.test", "Clave-admin-1")
        u("prueba.tecnico", "prueba@janotec.test", "Clave-prueba-1", rol="agente")
        u("tecnico.prueba", "tecnico.prueba@janotec.app", "Clave-prueba-1", rol="agente")
        u("sincorreo", "", "Clave-segura-1", rol="agente")
        u("retirado", "retirado@janotec.test", "Clave-segura-1", rol="agente", is_active=False)
        self.cliente = u("cliente", "cliente@a.co", "Clave-segura-1", rol="cliente",
                         empresa=Empresa.objects.create(razon_social="Cliente A", nit="1"))

    def correr(self, *args):
        salida = StringIO()
        call_command("invitar_cambio_clave", *args, stdout=salida)
        return salida.getvalue()

    def test_sin_enviar_solo_muestra_la_lista(self):
        salida = self.correr()
        self.assertIn("mtibamoso", salida)
        self.assertIn("jguana", salida)
        for fuera in ("admin@", "prueba.tecnico", "tecnico.prueba", "sincorreo", "retirado", "cliente@a.co"):
            self.assertNotIn(fuera, salida)
        self.assertIn("Total: 2", salida)
        self.assertEqual(mail.outbox, [])
        self.assertIn("cliente@a.co", self.correr("--con-clientes"))

    def test_cada_uno_recibe_su_enlace_y_crea_su_contrasena(self):
        self.correr("--enviar")
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["jguana@janotec.test", "miguel@janotec.test"])
        correo = next(m for m in mail.outbox if m.to == ["miguel@janotec.test"])
        self.assertIn("Su usuario: mtibamoso", correo.body)
        enlace = re.search(r"https://app\.janotec\.app(/cuenta/reset/\S+)", correo.body).group(1)
        # El enlace lleva a la pantalla de la nueva contraseña, que muestra el usuario
        r = self.client.get(enlace, follow=True)
        self.assertContains(r, "mtibamoso")
        self.client.post(r.redirect_chain[-1][0], {"new_password1": "Mi-clave-propia-2026", "new_password2": "Mi-clave-propia-2026"})
        self.miguel.refresh_from_db()
        self.assertTrue(self.miguel.check_password("Mi-clave-propia-2026"))
        # Sirve una sola vez
        self.assertContains(self.client.get(enlace, follow=True), "ya fue usado o venció")

    def test_solo_a_los_usuarios_indicados(self):
        self.correr("--solo", "jguana", "--enviar")
        self.assertEqual([m.to for m in mail.outbox], [["jguana@janotec.test"]])

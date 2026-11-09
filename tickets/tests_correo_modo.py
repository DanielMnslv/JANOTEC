"""Pruebas del modo del correo (CORREO_MODO) y de la firma con el nombre de quien actúa."""
from django.core import mail
from django.core.mail import EmailMessage, send_mail
from django.test import TestCase, override_settings

from accounts.models import Usuario
from config.correo import _actor, firmar

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


@override_settings(EMAIL_BACKEND="config.correo.JanotecBackend", EMAIL_BACKEND_REAL=LOCMEM,
                   DEFAULT_FROM_EMAIL="Plataforma JANOTEC <notificaciones@janotec.test>",
                   CORREO_CLIENTES_PRUEBA="daniel@janotec.test, yuri@janotec.test")
class CorreoModoTests(TestCase):
    """Configurar el correo sin que clientes, personal ni proveedores reciban nada de verdad."""

    def setUp(self):
        self.coordinador = Usuario.objects.create_user(
            "jguana", "coordinadortic@janotec.test", "Clave-segura-1", rol=Usuario.Rol.SUPERVISOR,
            first_name="Juan Daniel", last_name="Guana")
        self.tecnico = Usuario.objects.create_user("tec", "tec@janotec.test", "Clave-segura-1", rol=Usuario.Rol.AGENTE)

    @override_settings(CORREO_MODO="todo")
    def test_modo_todo_solo_llega_a_quienes_prueban(self):
        send_mail("Visita", "x", None, ["cliente@empresa.co", self.tecnico.email, "proveedor@ferreteria.co"])
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["daniel@janotec.test", "yuri@janotec.test"])
        self.assertEqual((m.cc, m.bcc), ([], []))
        self.assertIn("iba para: cliente@empresa.co, tec@janotec.test, proveedor@ferreteria.co", m.subject)

    @override_settings(CORREO_MODO="todo", CORREO_CLIENTES_PRUEBA="")
    def test_modo_todo_sin_buzon_de_pruebas_no_envia_nada(self):
        send_mail("Visita", "x", None, ["cliente@empresa.co"])
        self.assertEqual(mail.outbox, [])

    @override_settings(CORREO_MODO="real")
    def test_sale_con_el_nombre_de_quien_actua_y_responde_a_su_correo(self):
        marca = _actor.set(self.coordinador)
        try:
            send_mail("Se le asignó un ticket", "x", None, [self.tecnico.email])
        finally:
            _actor.reset(marca)
        m = mail.outbox[-1]
        self.assertEqual(m.to, [self.tecnico.email])
        self.assertEqual(m.from_email, '"Juan Daniel Guana · JANOTEC" <notificaciones@janotec.test>')
        self.assertEqual(m.reply_to, ["coordinadortic@janotec.test"])

    @override_settings(CORREO_MODO="todo")
    def test_en_modo_todo_el_asunto_dice_de_quien_era(self):
        marca = _actor.set(self.coordinador)
        try:
            send_mail("Se le asignó un ticket", "x", None, [self.tecnico.email])
        finally:
            _actor.reset(marca)
        self.assertIn("de: Juan Daniel Guana · JANOTEC · iba para: tec@janotec.test", mail.outbox[-1].subject)

    def test_sin_quien_actue_o_si_es_cliente_queda_el_remitente_de_la_plataforma(self):
        cliente = Usuario.objects.create_user("cli", "cli@empresa.co", "Clave-segura-1", rol=Usuario.Rol.CLIENTE)
        for usuario in (None, cliente):
            m = firmar(EmailMessage("x", "x", None, ["a@b.co"]), usuario)
            self.assertEqual(m.from_email, "Plataforma JANOTEC <notificaciones@janotec.test>")
            self.assertEqual(m.reply_to, [])

    def test_respeta_otro_remitente_y_responder_a_ya_puestos(self):
        m = firmar(EmailMessage("x", "x", "Bodega <otra@janotec.test>", ["a@b.co"], reply_to=["yo@janotec.test"]),
                   self.coordinador)
        self.assertEqual((m.from_email, m.reply_to), ("Bodega <otra@janotec.test>", ["yo@janotec.test"]))

    def test_en_una_peticion_firma_quien_inicio_sesion(self):
        """El middleware deja al usuario de la petición como remitente y lo suelta al terminar."""
        from config.correo import RemitenteMiddleware

        visto = {}

        class Peticion:
            user = self.coordinador

        RemitenteMiddleware(lambda r: visto.setdefault("actor", _actor.get()))(Peticion())
        self.assertEqual(visto["actor"], self.coordinador)
        self.assertIsNone(_actor.get())

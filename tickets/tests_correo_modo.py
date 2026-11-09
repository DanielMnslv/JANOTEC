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
        self.assertTrue(m.subject.startswith("[PRUEBA] "))
        self.assertIn("Iba para: cliente@empresa.co, tec@janotec.test, proveedor@ferreteria.co", m.body)

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
        m = mail.outbox[-1]
        self.assertEqual(m.subject, "[PRUEBA] Se le asignó un ticket")
        self.assertIn("De: Juan Daniel Guana · JANOTEC · Iba para: tec@janotec.test", m.body)
        html = m.alternatives[0][0]
        self.assertIn("Correo de prueba", html)
        self.assertIn("Iba para: tec@janotec.test", html)

    @override_settings(CORREO_MODO="real")
    def test_si_va_al_cliente_la_respuesta_llega_al_buzon_oficial(self):
        marca = _actor.set(self.coordinador)
        try:
            send_mail("Su visita", "x", None, ["cliente@empresa.co"])
        finally:
            _actor.reset(marca)
        m = mail.outbox[-1]
        self.assertEqual(m.from_email, '"Juan Daniel Guana · JANOTEC" <notificaciones@janotec.test>')
        self.assertEqual(m.reply_to, ["notificaciones@janotec.test"])

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


@override_settings(EMAIL_BACKEND="config.correo.JanotecBackend", EMAIL_BACKEND_REAL=LOCMEM, CORREO_MODO="real",
                   DEFAULT_FROM_EMAIL="Plataforma JANOTEC <notificaciones@janotec.test>", CORREO_CLIENTES_PRUEBA="")
class CorreoConDisenoTests(TestCase):
    """Todo correo sale con el diseño de JANOTEC, aunque se haya escrito solo en texto."""

    def test_el_texto_plano_sale_tambien_en_html_con_la_marca(self):
        send_mail("[JNT-2026-000001] Algo pasó", "Hay novedades.\n\nEmpresa: ACME\n\nVer: https://x.test/t/1/",
                  None, ["a@janotec.test"])
        html, tipo = mail.outbox[-1].alternatives[0]
        self.assertEqual(tipo, "text/html")
        for parte in ("JANO", "Algo pasó", "JNT-2026-000001", "ACME", 'href="https://x.test/t/1/"', "Plataforma de soporte"):
            self.assertIn(parte, html)

    def test_firma_quien_actua_con_su_area(self):
        jguana = Usuario.objects.create_user("jguana", "coordinadortic@janotec.test", "Clave-segura-1",
                                             rol=Usuario.Rol.SUPERVISOR, first_name="Juan Daniel", last_name="Guana")
        marca = _actor.set(jguana)
        try:
            send_mail("Aviso", "Texto", None, ["a@janotec.test"])
        finally:
            _actor.reset(marca)
        self.assertIn(f"Juan Daniel Guana · {jguana.rol_corto}", mail.outbox[-1].alternatives[0][0])

    def test_respeta_el_saludo_y_la_despedida_que_ya_trae(self):
        send_mail("Solicitud de cotización", "Señores Ferretería X:\n\nLe pedimos cotizar.\n\nCordialmente,\n"
                  "Sindy Pérez\nBodega · JANOTEC S.A.S.", None, ["p@ferreteria.co"])
        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn("Señores Ferretería X:", html)
        self.assertNotIn("Cordial saludo.", html)
        self.assertIn("Sindy Pérez · Bodega · JANOTEC S.A.S.", html)
        self.assertEqual(html.count("Cordialmente,"), 1)

    def test_no_toca_un_correo_que_ya_trae_su_html(self):
        from django.core.mail import EmailMultiAlternatives

        m = EmailMultiAlternatives("x", "x", None, ["a@janotec.test"])
        m.attach_alternative("<p>propio</p>", "text/html")
        m.send()
        self.assertEqual(mail.outbox[-1].alternatives, [("<p>propio</p>", "text/html")])


class SMTPServidorLocalTests(TestCase):
    """Conectar a 127.0.0.1 validando el certificado con el nombre janotec.app."""

    def _validado_con(self, **ajustes):
        import smtplib
        from unittest import mock

        from config.correo import SMTPServidorLocalBackend

        contexto = mock.Mock()
        with override_settings(EMAIL_USE_SSL=True, EMAIL_USE_TLS=False, **ajustes), \
                mock.patch.object(smtplib.SMTP, "_get_socket", return_value=object()):
            clase = SMTPServidorLocalBackend().connection_class
            conexion = clase(context=contexto)
            conexion._host = "127.0.0.1"  # lo pone connect() antes de abrir el socket
            conexion._get_socket("127.0.0.1", 465, 5)
        return contexto.wrap_socket.call_args.kwargs["server_hostname"]

    def test_valida_el_certificado_con_el_nombre_configurado(self):
        self.assertEqual(self._validado_con(EMAIL_CERTIFICADO_NOMBRE="janotec.app"), "janotec.app")

    def test_sin_nombre_valida_con_la_direccion_de_siempre(self):
        self.assertEqual(self._validado_con(EMAIL_CERTIFICADO_NOMBRE=""), "127.0.0.1")

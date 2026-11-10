import shutil
import tempfile

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Empresa, Sede, Usuario

from .models import ActaSoporte, Adjunto, Categoria, Seguimiento, Ticket

MEDIA_TMP = tempfile.mkdtemp()
E = Ticket.Estado


@override_settings(MEDIA_ROOT=MEDIA_TMP, NOTIFICAR_SOPORTE_EMAIL="soporte@janotec.test",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseTickets(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        self.cat = Categoria.objects.create(nombre="CCTV")
        self.emp_a = Empresa.objects.create(razon_social="Empresa A", nit="1")
        self.emp_b = Empresa.objects.create(razon_social="Empresa B", nit="2")
        self.sede_a = Sede.objects.create(empresa=self.emp_a, nombre="Principal")
        self.sede_b = Sede.objects.create(empresa=self.emp_b, nombre="Otra")
        self.cli_a = Usuario.objects.create_user("cli_a", "a@x.co", "Clave-segura-1", rol="cliente", empresa=self.emp_a)
        self.cli_a2 = Usuario.objects.create_user("cli_a2", "a2@x.co", "Clave-segura-1", rol="cliente", empresa=self.emp_a)
        self.cli_b = Usuario.objects.create_user("cli_b", "b@x.co", "Clave-segura-1", rol="cliente", empresa=self.emp_b)
        self.agente = Usuario.objects.create_user("agente", "ag@x.co", "Clave-segura-1", rol="agente")
        self.agente2 = Usuario.objects.create_user("agente2", "ag2@x.co", "Clave-segura-1", rol="agente")
        self.supervisor = Usuario.objects.create_user("super", "sup@x.co", "Clave-segura-1", rol="supervisor")

    def crear_ticket(self, solicitante=None, **kw):
        solicitante = solicitante or self.cli_a
        datos = dict(empresa=solicitante.empresa, solicitante=solicitante, categoria=self.cat,
                     asunto="Cámara sin imagen", descripcion="No se ve nada")
        datos.update(kw)
        return Ticket.objects.create(**datos)


    def datos_radicacion(self, **kw):
        """Lo que se llena en el formulario de registro de la solicitud."""
        datos = {
            "tipo": "soporte", "categoria": self.cat.pk, "sede": self.sede_a.pk,
            "departamento": "Meta", "municipio": "Villavicencio", "direccion": "Calle 1 # 2-3",
            "falla_nombre": "Cámara caída", "descripcion": "Cámara caída en la entrada", "impacto": "media",
            "alturas": "no", "sst": "no", "jornada": "manana",
            "contacto_nombre": "Portero", "contacto_telefono": "3101234567", "contacto_email": "portero@a.co",
        }
        datos.update(kw)
        return datos


class RadicadoTests(BaseTickets):
    def test_radicado_consecutivo_y_formato(self):
        t1, t2 = self.crear_ticket(), self.crear_ticket()
        anio = t1.creado.astimezone().year
        self.assertRegex(t1.radicado, rf"^JNT-{anio}-\d{{6}}$")
        self.assertEqual(int(t2.radicado[-6:]), int(t1.radicado[-6:]) + 1)

    def test_sede_de_otra_empresa_rechazada(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.crear_ticket(sede=self.sede_b)


class ClienteTests(BaseTickets):
    def test_cliente_crea_ticket_con_adjunto_y_recibe_correo(self):
        self.client.force_login(self.cli_a)
        archivo = SimpleUploadedFile("foto.jpg", b"\xff\xd8\xff" + b"0" * 100, content_type="image/jpeg")
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(archivos=[archivo]))
        t = Ticket.objects.get()
        self.assertRedirects(r, t.get_absolute_url())
        self.assertEqual((t.empresa, t.solicitante, t.estado), (self.emp_a, self.cli_a, E.GENERADO))
        self.assertEqual((t.radicado_por, t.municipio, t.asunto), (self.cli_a, "Villavicencio", "Cámara caída"))
        self.assertEqual(t.adjuntos.count(), 1)
        destinatarios = {d for m in mail.outbox for d in m.to + m.cc}
        self.assertIn("a@x.co", destinatarios)
        self.assertIn("soporte@janotec.test", destinatarios)
        self.assertIn(t.radicado, mail.outbox[0].subject)

    def test_cliente_no_puede_usar_sede_ajena(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(sede=self.sede_b.pk))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Ticket.objects.exists())

    def test_extension_no_permitida(self):
        self.client.force_login(self.cli_a)
        malo = SimpleUploadedFile("virus.exe", b"MZ")
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(archivos=[malo]))
        self.assertFalse(Ticket.objects.exists())

    def test_formulario_no_pide_prioridad_ni_responsable(self):
        self.client.force_login(self.cli_a)
        form = self.client.get(reverse("nuevo_ticket")).context["form"]
        self.assertNotIn("prioridad", form.fields)
        self.assertNotIn("asignado_a", form.fields)
        self.assertNotIn("estado", form.fields)
        # El contacto arranca con los datos de quien radica
        self.assertEqual(form.initial["contacto_nombre"], "cli_a")

    def test_cliente_no_puede_asignar_ni_fijar_prioridad(self):
        self.client.force_login(self.cli_a)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(
            impacto="alta", prioridad="baja", asignado_a=self.agente.pk, estado="cerrado",
        ))
        t = Ticket.objects.get()
        # Llega sin responsable y sin urgencia (la define el coordinador); la prioridad de arranque sale del tipo
        self.assertEqual((t.estado, t.asignado_a, t.impacto, t.prioridad), (E.GENERADO, None, "", "media"))
        self.assertEqual((t.contacto_nombre, t.contacto_telefono), ("Portero", "3101234567"))

    def test_urgencia_solo_la_define_el_coordinador(self):
        # El cliente y el técnico no ven el nivel de urgencia
        for usuario in (self.cli_a, self.agente):
            self.client.force_login(usuario)
            r = self.client.get(reverse("nuevo_ticket"))
            self.assertNotIn("impacto", r.context["form"].fields)
            self.assertNotContains(r, 'name="impacto"')
        # El coordinador sí, y es obligatorio; la prioridad arranca igual a la urgencia
        self.client.force_login(self.supervisor)
        form = self.client.get(reverse("nuevo_ticket")).context["form"]
        self.assertEqual([c[0] for c in form.fields["impacto"].choices], ["baja", "media", "alta"])
        for impacto in ("baja", "media", "alta"):
            self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(empresa=self.emp_a.pk, impacto=impacto, descripcion=impacto))
            self.assertEqual(Ticket.objects.get(descripcion=impacto).prioridad, impacto)
        for valor in ("total", ""):
            r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(empresa=self.emp_a.pk, impacto=valor))
            self.assertIn("impacto", r.context["form"].errors)

    def test_sin_urgencia_no_se_recepciona_hasta_que_el_coordinador_la_define(self):
        self.client.force_login(self.cli_a)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(alturas="no", sst="no"))
        t = Ticket.objects.get()
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(t.get_absolute_url()), "Por definir")
        r = self.client.post(reverse("recepcionar", args=[t.radicado]), follow=True)
        t.refresh_from_db()
        self.assertEqual(t.estado, E.GENERADO)
        self.assertContains(r, "defina el nivel de urgencia")
        self.assertEqual(r.redirect_chain[-1][0], reverse("editar_ticket", args=[t.radicado]) + "?recepcionar=1")
        datos = self.datos_radicacion(empresa_nombre=self.emp_a.razon_social, impacto="alta", recepcionar="1",
                                      motivo_ajuste="Cámara de la entrada principal")
        self.assertEqual(self.client.post(reverse("editar_ticket", args=[t.radicado]), datos).status_code, 302)
        t.refresh_from_db()
        self.assertEqual((t.impacto, t.prioridad, t.estado), ("alta", "alta", E.RECEPCIONADO))

    def test_la_falla_o_necesidad_es_el_asunto(self):
        self.client.force_login(self.cli_a)
        r = self.client.get(reverse("nuevo_ticket"))
        self.assertNotIn("asunto", r.context["form"].fields)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(falla_nombre="No graba el DVR"))
        self.assertEqual(Ticket.objects.get().asunto, "No graba el DVR")
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(falla_nombre=""))
        self.assertIn("falla_nombre", r.context["form"].errors)

    def test_campos_obligatorios_del_registro(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(
            sede="", departamento="", municipio="", direccion="", descripcion="", contacto_telefono="",
            contacto_nombre="", falla_nombre="", alturas="", sst=""))
        self.assertEqual(r.status_code, 200)
        for campo in ("sede_nombre", "departamento", "municipio", "direccion", "descripcion", "contacto_telefono",
                      "contacto_nombre", "falla_nombre", "alturas", "sst"):
            self.assertIn(campo, r.context["form"].errors, campo)
        self.assertFalse(Ticket.objects.exists())

    def test_tipos_alturas_y_sst(self):
        # El cliente responde con preguntas sencillas; SST no se le pregunta (lo decide el área técnica)
        self.client.force_login(self.cli_a)
        form = self.client.get(reverse("nuevo_ticket")).context["form"]
        self.assertEqual([c for c in form.fields["tipo"].choices],
                         [("mantenimiento", "Mantenimiento"), ("soporte", "Servicio técnico"),
                          ("instalacion", "Instalación"), ("requerimiento", "Toma de requerimientos")])
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(tipo="mantenimiento", alturas="si", sst="si"))
        t = Ticket.objects.get()
        self.assertEqual((t.tipo, t.trabajo_alturas, t.acompanamiento_sst, t.por_confirmar), ("mantenimiento", True, True, []))
        self.assertTrue(any("ALTURAS" in m.body for m in mail.outbox if "soporte@janotec.test" in m.to))
        # El personal JANOTEC ve el formulario técnico completo
        self.client.force_login(self.agente)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(empresa=self.emp_a.pk, descripcion="tecnico",
                                                                        alturas="si", sst="si"))
        t = Ticket.objects.get(descripcion="tecnico")
        self.assertEqual((t.trabajo_alturas, t.acompanamiento_sst), (True, True))

    def test_no_se_queda_por_confirmar_y_el_area_tecnica_lo_confirma(self):
        self.client.force_login(self.cli_a)
        Categoria.objects.get_or_create(nombre="Diagnóstico técnico")
        datos = self.datos_radicacion(sistema="No sé")
        datos.pop("categoria")
        self.client.post(reverse("nuevo_ticket"), datos)
        t = Ticket.objects.get()
        self.assertEqual((t.por_confirmar, t.sistema_display), (["sistema"], "Diagnóstico técnico"))
        # «Recepcionar» lleva primero a confirmar
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("panel")), "Por confirmar: Sistema a intervenir")
        r = self.client.post(reverse("recepcionar", args=[t.radicado]))
        self.assertRedirects(r, reverse("editar_ticket", args=[t.radicado]) + "?recepcionar=1", fetch_redirect_response=False)
        t.refresh_from_db()
        self.assertEqual(t.estado, E.GENERADO)
        editar = self.datos_radicacion(empresa=self.emp_a.pk, sistema="CCTV", alturas="si", recepcionar="1")
        editar.pop("categoria")
        self.client.post(reverse("editar_ticket", args=[t.radicado]), editar)
        t.refresh_from_db()
        self.assertEqual((t.estado, t.por_confirmar, t.categoria, t.trabajo_alturas, t.confirmado_por),
                         (E.RECEPCIONADO, [], self.cat, True, self.supervisor))
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje__contains="no supo").exists())

    def test_el_cliente_nombra_el_sistema_con_sus_palabras(self):
        from .forms import sistema_por_alias
        cctv = Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")[0]
        instalar = Categoria.objects.get_or_create(nombre="Instalación CCTV IP")[0]
        red = Categoria.objects.get_or_create(nombre="Fibra óptica / red")[0]
        self.assertEqual(sistema_por_alias("Cámaras (CCTV)"), cctv)
        self.assertEqual(sistema_por_alias("instalar cámaras nuevas"), instalar)
        self.assertEqual(sistema_por_alias("cámaras", tipo="requerimiento"), instalar)
        self.assertEqual(sistema_por_alias("el internet no sirve"), red)
        self.client.force_login(self.cli_a)
        datos = self.datos_radicacion(sistema="las cámaras de la entrada")
        datos.pop("categoria")
        self.client.post(reverse("nuevo_ticket"), datos)
        self.assertEqual((Ticket.objects.get().categoria, Ticket.objects.get().por_confirmar), (cctv, []))

    def test_rangos_de_fecha_y_hora_sin_disponibilidad(self):
        self.client.force_login(self.cli_a)
        rangos = {"rangos-TOTAL_FORMS": "2", "rangos-INITIAL_FORMS": "0", "rangos-MIN_NUM_FORMS": "0",
                  "rangos-MAX_NUM_FORMS": "8", "rangos-0-desde": "2030-03-08T08:00", "rangos-0-hasta": "2030-03-08T12:00"}
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(**rangos, **{"rangos-1-desde": "2030-03-09T10:00",
                                                                                         "rangos-1-hasta": "2030-03-09T09:00"}))
        self.assertEqual(r.status_code, 200)  # el segundo rango termina antes de empezar
        self.assertFalse(Ticket.objects.exists())
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(**rangos))
        t = Ticket.objects.get()
        self.assertEqual(str(t.rangos_no_disponible.get()), "08/03/2030 de 08:00 a 12:00")
        self.assertIn("08/03/2030", t.resumen_no_disponible)

    def test_cliente_ve_tickets_de_su_empresa_pero_no_de_otra(self):
        propio = self.crear_ticket(self.cli_a)
        companero = self.crear_ticket(self.cli_a2)
        ajeno = self.crear_ticket(self.cli_b)
        self.client.force_login(self.cli_a)
        lista = self.client.get(reverse("mis_tickets")).content.decode()
        self.assertIn(propio.radicado, lista)
        self.assertIn(companero.radicado, lista)
        self.assertNotIn(ajeno.radicado, lista)
        self.assertEqual(self.client.get(ajeno.get_absolute_url()).status_code, 404)

    def test_cliente_no_ve_notas_internas_ni_sus_adjuntos(self):
        t = self.crear_ticket(asignado_a=self.agente, estado=E.EN_CRONOGRAMA)
        nota = Seguimiento.objects.create(ticket=t, autor=self.agente, tipo="nota_interna",
                                          interno=True, mensaje="SECRETO-INTERNO")
        adj = Adjunto.objects.create(ticket=t, seguimiento=nota, nombre_original="interno.pdf",
                                     archivo=SimpleUploadedFile("interno.pdf", b"%PDF"))
        Seguimiento.objects.create(ticket=t, autor=self.agente, tipo="comentario", mensaje="RESPUESTA-PUBLICA")
        self.client.force_login(self.cli_a)
        html = self.client.get(t.get_absolute_url()).content.decode()
        self.assertNotIn("SECRETO-INTERNO", html)
        self.assertIn("RESPUESTA-PUBLICA", html)
        self.assertEqual(self.client.get(reverse("descargar_adjunto", args=[adj.pk])).status_code, 404)
        # El encargado del ticket sí la ve y la descarga
        self.client.force_login(self.agente)
        self.assertIn("SECRETO-INTERNO", self.client.get(t.get_absolute_url()).content.decode())
        self.assertEqual(self.client.get(reverse("descargar_adjunto", args=[adj.pk])).status_code, 200)

    def test_cliente_no_entra_al_panel_ni_gestiona(self):
        t = self.crear_ticket()
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(reverse("panel")).status_code, 403)
        r = self.client.post(reverse("gestionar", args=[t.radicado]),
                             {"estado": "cerrado", "prioridad": "baja", "visibilidad": "publico"})
        self.assertEqual(r.status_code, 403)
        t.refresh_from_db()
        self.assertEqual(t.estado, E.GENERADO)

    def test_respuesta_del_cliente_queda_en_el_historial(self):
        t = self.crear_ticket(estado=E.RECEPCIONADO)
        self.client.force_login(self.cli_a)
        self.client.post(reverse("responder_cliente", args=[t.radicado]), {"mensaje": "Ya les envío la foto"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.RECEPCIONADO)
        self.assertTrue(t.seguimientos.filter(mensaje="Ya les envío la foto", autor=self.cli_a).exists())

    def test_confirmar_y_calificar(self):
        t = self.crear_ticket(estado=E.VISITA_EJECUTADA)
        self.client.force_login(self.cli_a)
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "aceptar"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.CERRADO)
        self.assertIsNotNone(t.fecha_cierre)
        self.client.post(reverse("calificar", args=[t.radicado]), {"calificacion": "5", "comentario": "Excelente"})
        t.refresh_from_db()
        self.assertEqual(t.calificacion, 5)

    def test_si_no_quedo_solucionado_queda_pendiente_nueva_visita(self):
        t = self.crear_ticket(estado=E.VISITA_EJECUTADA)
        self.client.force_login(self.cli_a)
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "reabrir", "motivo": "Sigue igual"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.PENDIENTE_VISITA)

    def test_cancelar_solo_antes_del_cronograma(self):
        t = self.crear_ticket(estado=E.EN_CRONOGRAMA)
        self.client.force_login(self.cli_a)
        self.client.post(reverse("cancelar", args=[t.radicado]))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.EN_CRONOGRAMA)
        otro = self.crear_ticket(estado=E.RECEPCIONADO)
        self.client.post(reverse("cancelar", args=[otro.radicado]))
        otro.refresh_from_db()
        self.assertEqual(otro.estado, E.CANCELADO)


class PersonalTests(BaseTickets):
    def test_supervisor_asigna_cambia_estado_y_notifica(self):
        t = self.crear_ticket()
        self.client.force_login(self.supervisor)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "mensaje": "Iremos mañana a las 9 a.m.", "visibilidad": "publico",
            "estado": E.RECEPCIONADO, "prioridad": "alta", "asignado_a": self.agente.pk,
        })
        t.refresh_from_db()
        self.assertEqual((t.estado, t.prioridad, t.asignado_a), (E.RECEPCIONADO, "alta", self.agente))
        visibles = t.seguimientos.filter(interno=False, tipo="cambio_estado")
        self.assertTrue(visibles.filter(mensaje__contains="mañana").exists())
        self.assertFalse(any("a@x.co" in m.to for m in mail.outbox))
        self.assertTrue(any("ag@x.co" in m.to for m in mail.outbox))  # aviso al encargado

    def test_nota_interna_no_notifica_al_cliente(self):
        t = self.crear_ticket(asignado_a=self.agente, estado=E.EN_CRONOGRAMA)
        self.client.force_login(self.agente)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "mensaje": "Revisar contrato", "visibilidad": "interno", "estado": t.estado, "prioridad": t.prioridad,
        })
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje="Revisar contrato").exists())
        self.assertFalse(any("a@x.co" in m.to for m in mail.outbox))

    def test_personal_y_central_radican_a_nombre_del_cliente(self):
        central = Usuario.objects.create_user("central", "central@x.co", "Clave-segura-1", rol="central")
        for usuario in (self.agente, central):
            self.client.force_login(usuario)
            self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(empresa=self.emp_a.pk, descripcion=usuario.username))
            t = Ticket.objects.get(descripcion=usuario.username)
            self.assertEqual((t.empresa, t.radicado_por, t.estado), (self.emp_a, usuario, E.GENERADO))
        # La sede debe ser de la empresa elegida
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(empresa=self.emp_b.pk))
        self.assertIn("sede_nombre", r.context["form"].errors)
        # La central ve lo que registró, no las demás solicitudes; el cliente ve las de su empresa
        ajeno = self.crear_ticket(self.cli_b)
        lista = self.client.get(reverse("mis_tickets")).content.decode()
        self.assertIn(Ticket.objects.get(descripcion="central").radicado, lista)
        self.assertNotIn(ajeno.radicado, lista)
        self.assertEqual(self.client.get(reverse("panel")).status_code, 403)
        self.client.force_login(self.cli_a)
        self.assertIn(Ticket.objects.get(descripcion="central").radicado, self.client.get(reverse("mis_tickets")).content.decode())

    def test_encargado_solo_ve_sus_tickets(self):
        mio = self.crear_ticket(asignado_a=self.agente, estado=E.EN_CRONOGRAMA, asunto="MIO-ASIGNADO")
        ajeno = self.crear_ticket(asignado_a=self.agente2, estado=E.EN_CRONOGRAMA, asunto="DE-OTRO")
        sin = self.crear_ticket(asunto="SIN-ASIGNAR")
        adj = Adjunto.objects.create(ticket=ajeno, nombre_original="a.pdf",
                                     archivo=SimpleUploadedFile("a.pdf", b"%PDF"))
        self.client.force_login(self.agente)
        html = self.client.get(reverse("panel")).content.decode()
        self.assertIn("MIO-ASIGNADO", html)
        self.assertNotIn("DE-OTRO", html)
        self.assertNotIn("SIN-ASIGNAR", html)
        self.assertEqual(self.client.get(mio.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(ajeno.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(sin.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("descargar_adjunto", args=[adj.pk])).status_code, 404)
        r = self.client.post(reverse("gestionar", args=[ajeno.radicado]), {"visibilidad": "publico", "estado": E.VISITA_EJECUTADA})
        self.assertEqual(r.status_code, 404)

    def test_encargado_no_reasigna_ni_cambia_prioridad(self):
        t = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO, prioridad="media")
        self.client.force_login(self.agente)
        form = self.client.get(t.get_absolute_url()).context["form_gestion"]
        self.assertNotIn("asignado_a", form.fields)
        self.assertNotIn("prioridad", form.fields)
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "visibilidad": "publico", "mensaje": "Falta un repuesto", "estado": E.PENDIENTE_VISITA,
            "asignado_a": self.agente2.pk, "prioridad": "critica",
        })
        t.refresh_from_db()
        self.assertEqual((t.estado, t.asignado_a, t.prioridad), (E.PENDIENTE_VISITA, self.agente, "media"))
        # Cerrar, cancelar o recepcionar no le corresponde
        for estado in (E.CANCELADO, E.CERRADO, E.RECEPCIONADO):
            self.client.post(reverse("gestionar", args=[t.radicado]), {"visibilidad": "publico", "estado": estado})
            t.refresh_from_db()
            self.assertEqual(t.estado, E.PENDIENTE_VISITA)

    def test_encargado_no_ve_reportes(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("reportes")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("panel")), reverse("reportes"))

    def test_supervisor_ve_todo(self):
        self.crear_ticket(asignado_a=self.agente2, estado=E.EN_CRONOGRAMA, asunto="DE-OTRO")
        self.crear_ticket(asunto="SIN-ASIGNAR")
        self.client.force_login(self.supervisor)
        html = self.client.get(reverse("panel")).content.decode()
        self.assertIn("DE-OTRO", html)
        self.assertIn("SIN-ASIGNAR", html)

    def test_indicador_radicados_hoy(self):
        # Se calcula con rangos de fecha: funciona aunque MySQL no tenga tablas de zona horaria
        self.crear_ticket()
        self.crear_ticket()
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("panel")).context["ind"]["hoy"], 2)

    def test_panel_y_reportes_cargan(self):
        self.crear_ticket(prioridad="critica")
        self.client.force_login(self.supervisor)
        for nombre in ("panel", "reportes"):
            self.assertEqual(self.client.get(reverse(nombre)).status_code, 200, nombre)
        self.assertEqual(self.client.get(reverse("panel") + "?orden=prioridad&asignado=sin&tipo=mantenimiento").status_code, 200)

    def test_anonimo_redirigido_a_login(self):
        r = self.client.get(reverse("panel"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])


class HistorialServicioTests(BaseTickets):
    def setUp(self):
        super().setUp()
        from django.utils import timezone
        from .models import ActaSoporte
        self.acta_a = ActaSoporte.objects.create(numero=1001, fecha=timezone.now(), empresa=self.emp_a, sede=self.sede_a,
                                                 tecnico=self.agente2, diagnostico="DIAG-A", resultado="solucionado")
        self.acta_b = ActaSoporte.objects.create(numero=1002, fecha=timezone.now(), empresa=self.emp_b,
                                                 tecnico=self.agente2, diagnostico="DIAG-B", resultado="parcial")

    def test_cliente_solo_ve_actas_de_su_empresa(self):
        self.client.force_login(self.cli_a)
        html = self.client.get(reverse("actas_lista")).content.decode()
        self.assertIn("DIAG-A", html)
        self.assertNotIn("DIAG-B", html)
        self.assertEqual(self.client.get(self.acta_a.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(self.acta_b.get_absolute_url()).status_code, 404)

    def test_encargado_ve_las_suyas_y_las_de_sus_clientes(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(self.acta_a.get_absolute_url()).status_code, 404)
        # Al asignarle un ticket de la empresa A, ve el historial de ese cliente (no el de B)
        self.crear_ticket(asignado_a=self.agente, estado=E.EN_CRONOGRAMA, sede=self.sede_a)
        self.assertEqual(self.client.get(self.acta_a.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(self.acta_b.get_absolute_url()).status_code, 404)
        # Quien hizo el acta siempre la ve
        self.client.force_login(self.agente2)
        self.assertEqual(self.client.get(self.acta_b.get_absolute_url()).status_code, 200)

    def test_supervisor_ve_todo_y_filtra(self):
        self.client.force_login(self.supervisor)
        html = self.client.get(reverse("actas_lista")).content.decode()
        self.assertIn("DIAG-A", html)
        self.assertIn("DIAG-B", html)
        html = self.client.get(reverse("actas_lista") + "?resultado=parcial").content.decode()
        self.assertNotIn("DIAG-A", html)
        self.assertIn("DIAG-B", self.client.get(reverse("actas_lista") + "?q=1002").content.decode())

    def test_detalle_del_ticket_muestra_historial_de_la_sede(self):
        t = self.crear_ticket(sede=self.sede_a)
        self.client.force_login(self.cli_a)
        self.assertIn("DIAG-A", self.client.get(t.get_absolute_url()).content.decode())


class ImportarMaestrosTests(TestCase):
    def _excel(self, ruta, activo="SI"):
        import openpyxl
        libro = openpyxl.Workbook()
        libro.remove(libro.active)
        hojas = {
            "Empresas": [["clave", "razón social", "NIT", "ciudad", "sector", "activa"],
                         ["acme", "ACME S.A.S", "900-1", "Villavicencio", "Privado", "SI"],
                         ["muni", "MUNICIPIO X", "", "Granada", "Público", "SI"],
                         ["otra", "OTRA SIN NIT", "", "", "Privado", "NO"]],
            "Sedes": [["clave empresa", "empresa", "sede", "ciudad", "dirección", "abonado alarma"],
                      ["acme", "ACME", "Bodega", "Villavicencio", "Calle 1", "5511"]],
            "Personal": [["usuario", "nombres", "apellidos", "cargo", "rol (agente/supervisor)", "correo", "activo"],
                         ["tec1", "Ana", "Ruiz", "Técnica", "agente", "ana@x.co", activo],
                         ["coord", "Luis", "Paz", "Coordinador TIC", "supervisor", "luis@x.co", "SI"]],
            "Actas": [["N° acta", "fecha", "clave empresa", "sede (listado)", "sede escrita", "ciudad", "técnico", "actividad",
                       "sistema", "diagnóstico", "resultado", "observaciones", "recibió (cliente)", "cargo", "latitud", "longitud",
                       "enlace imágenes"],
                      [501, "2026-01-05 13:14:31", "acme", "Bodega", "bodega ppal", "Villavicencio", "tec1", "Soporte", "CCTV",
                       "Cámara sin visual", "Solucionado Parcialmente", "Se cambia fuente", "Pedro", "Vigilante", "4.1", "-73.6",
                       "https://ejemplo.co/501"]],
            "Equipos en actas": [["N° acta", "equipo", "marca", "serial", "cantidad", "observaciones"],
                                 [501, "Fuente", "Genérica", "", "1", "12V"]],
        }
        for nombre, filas in hojas.items():
            ws = libro.create_sheet(nombre)
            for f in filas:
                ws.append(f)
        libro.save(ruta)

    def test_importa_y_es_idempotente(self):
        import os
        from django.core.management import call_command
        from .models import ActaSoporte, EquipoActa
        ruta = os.path.join(MEDIA_TMP, "maestro.xlsx")
        os.makedirs(MEDIA_TMP, exist_ok=True)
        self._excel(ruta)
        call_command("importar_maestros", ruta, stdout=open(os.devnull, "w"))
        call_command("importar_maestros", ruta, stdout=open(os.devnull, "w"))  # segunda vez: no duplica
        self.assertEqual(Empresa.objects.count(), 3)
        self.assertEqual(Empresa.objects.get(codigo="muni").sector, "publico")
        self.assertIsNone(Empresa.objects.get(codigo="muni").nit)  # varias empresas sin NIT no chocan
        self.assertFalse(Empresa.objects.get(codigo="otra").activa)
        self.assertEqual(Sede.objects.get(nombre="Bodega").abonado, "5511")
        acta = ActaSoporte.objects.get(numero=501)
        self.assertEqual((acta.resultado, acta.sede.nombre, acta.tecnico.username), ("parcial", "Bodega", "tec1"))
        self.assertEqual(EquipoActa.objects.count(), 1)
        coord = Usuario.objects.get(username="coord")
        self.assertTrue(coord.is_staff and coord.rol == "supervisor")
        self.assertFalse(coord.has_usable_password())  # define su clave con «¿Olvidó su contraseña?»

    def test_simular_no_guarda(self):
        import os
        from django.core.management import call_command
        ruta = os.path.join(MEDIA_TMP, "maestro2.xlsx")
        os.makedirs(MEDIA_TMP, exist_ok=True)
        self._excel(ruta)
        call_command("importar_maestros", ruta, "--simular", stdout=open(os.devnull, "w"))
        self.assertFalse(Empresa.objects.exists())

    def test_no_cambia_contrasena_existente(self):
        import os
        from django.core.management import call_command
        u = Usuario.objects.create_user("tec1", "", "Clave-propia-123", rol="agente")
        ruta = os.path.join(MEDIA_TMP, "maestro3.xlsx")
        os.makedirs(MEDIA_TMP, exist_ok=True)
        self._excel(ruta, activo="NO")
        call_command("importar_maestros", ruta, stdout=open(os.devnull, "w"))
        u.refresh_from_db()
        self.assertTrue(u.check_password("Clave-propia-123"))
        self.assertFalse(u.is_active)


class ConfiguracionTests(BaseTickets):
    """Lo que puede hacer el área administrativa en Configuración (admin de Django)."""

    def setUp(self):
        super().setUp()
        self.raiz = Usuario.objects.create_superuser("programador", "dev@x.co", "Clave-segura-1")
        self.jefe = Usuario.objects.create_user("jefe", "jefe@x.co", "Clave-segura-1", rol="supervisor")

    def test_rol_define_acceso_y_grupo(self):
        from accounts.grupos import GRUPO_AREA_ADMINISTRATIVA
        self.assertTrue(self.supervisor.is_staff)
        self.assertTrue(self.supervisor.groups.filter(name=GRUPO_AREA_ADMINISTRATIVA).exists())
        self.assertFalse(self.agente.is_staff)
        self.supervisor.rol = "agente"
        self.supervisor.save()
        self.assertFalse(self.supervisor.is_staff)
        self.assertFalse(self.supervisor.groups.exists())

    def test_supervisor_gestiona_clientes_empresas_y_sedes(self):
        self.client.force_login(self.supervisor)
        for url in ("/admin/", "/admin/accounts/empresa/", "/admin/accounts/sede/", "/admin/accounts/usuario/",
                    "/admin/tickets/actasoporte/", f"/admin/accounts/usuario/{self.cli_a.pk}/change/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.post("/admin/accounts/usuario/add/", {
            "username": "nuevo_cli", "password1": "Clave-segura-77", "password2": "Clave-segura-77",
            "first_name": "Ana", "last_name": "Gil", "email": "ana@a.co", "rol": "cliente", "empresa": self.emp_a.pk,
            "usable_password": "true",
        })
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertTrue(Usuario.objects.filter(username="nuevo_cli", empresa=self.emp_a).exists())
        # No puede borrar empresas (se desactivan para no perder el historial)
        self.assertEqual(self.client.get(f"/admin/accounts/empresa/{self.emp_a.pk}/delete/").status_code, 403)

    def test_supervisor_no_toca_superusuarios_ni_otros_supervisores(self):
        self.client.force_login(self.supervisor)
        lista = self.client.get("/admin/accounts/usuario/").content.decode()
        self.assertNotIn("programador", lista)
        self.assertNotIn(">jefe<", lista)
        for u in (self.raiz, self.jefe):
            r = self.client.get(f"/admin/accounts/usuario/{u.pk}/change/")
            self.assertNotEqual(r.status_code, 200)
            r = self.client.get(f"/admin/accounts/usuario/{u.pk}/password/")
            self.assertNotEqual(r.status_code, 200)

    def test_supervisor_no_puede_escalar_privilegios(self):
        self.client.force_login(self.supervisor)
        form = self.client.get(f"/admin/accounts/usuario/{self.agente.pk}/change/").context["adminform"].form
        self.assertNotIn("is_superuser", form.fields)
        self.assertNotIn("groups", form.fields)
        self.assertNotIn("supervisor", [c[0] for c in form.fields["rol"].choices])
        # Ni siquiera enviando los campos a mano
        datos = {"username": "agente", "first_name": "", "last_name": "", "email": "ag@x.co", "rol": "supervisor",
                 "is_active": "on", "is_superuser": "on", "telefono": "", "cargo": ""}
        self.client.post(f"/admin/accounts/usuario/{self.agente.pk}/change/", datos)
        self.agente.refresh_from_db()
        self.assertFalse(self.agente.is_superuser)
        self.assertEqual(self.agente.rol, "agente")

    def test_superusuario_gestiona_supervisores(self):
        self.client.force_login(self.raiz)
        self.assertEqual(self.client.get(f"/admin/accounts/usuario/{self.jefe.pk}/change/").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/accounts/usuario/{self.jefe.pk}/password/").status_code, 200)


@override_settings(MEDIA_ROOT=MEDIA_TMP, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CuentasPruebaTests(TestCase):
    def test_crea_y_elimina_sin_tocar_datos_reales(self):
        import os
        from pathlib import Path
        from django.core.management import call_command
        from tickets.management.commands import cuentas_prueba
        from unittest import mock
        archivo = mock.patch.object(cuentas_prueba, "ARCHIVO", Path(MEDIA_TMP) / "cuentas_de_prueba.txt")
        archivo.start()
        self.addCleanup(archivo.stop)
        os.makedirs(MEDIA_TMP, exist_ok=True)
        real = Empresa.objects.create(razon_social="Cliente real", nit="1", codigo="real")
        cat = Categoria.objects.create(nombre="CCTV")
        call_command("cuentas_prueba", stdout=open(os.devnull, "w"))
        cli = Usuario.objects.get(username="cliente.prueba")
        self.assertEqual(cli.empresa.codigo, "pruebas")
        self.assertTrue(Usuario.objects.get(username="tecnico.prueba").rol == "agente")
        clave = [l for l in cuentas_prueba.ARCHIVO.read_text(encoding="utf-8").splitlines() if "cliente.prueba" in l][0].split()[-1]
        self.assertTrue(self.client.login(username="cliente.prueba", password=clave))
        Ticket.objects.create(empresa=cli.empresa, solicitante=cli, categoria=cat, asunto="x", descripcion="y")
        call_command("cuentas_prueba", "--eliminar", stdout=open(os.devnull, "w"))
        self.assertFalse(Empresa.objects.filter(codigo="pruebas").exists())
        self.assertFalse(Usuario.objects.filter(username__in=["cliente.prueba", "tecnico.prueba"]).exists())
        self.assertFalse(Ticket.objects.exists())
        self.assertTrue(Empresa.objects.filter(pk=real.pk).exists())
        self.assertFalse(cuentas_prueba.ARCHIVO.exists())


class FallasYRestriccionesTests(BaseTickets):
    def setUp(self):
        super().setUp()
        from .models import TipoFalla
        self.sin_imagen = TipoFalla.objects.create(categoria=self.cat, nombre="Cámara sin imagen")
        otra_cat = Categoria.objects.create(nombre="Alarmas")
        self.falla_ajena = TipoFalla.objects.create(categoria=otra_cat, nombre="Zona con falla")

    def test_falla_opcional_pero_del_sistema_elegido(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(falla=self.falla_ajena.pk))
        self.assertIn("falla_nombre", r.context["form"].errors)
        self.assertFalse(Ticket.objects.exists())
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(falla=self.sin_imagen.pk))
        self.assertEqual(Ticket.objects.get().falla, self.sin_imagen)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(descripcion="Sin falla de la lista"))
        self.assertIsNone(Ticket.objects.get(descripcion="Sin falla de la lista").falla)

    def test_varios_correos_reciben_las_respuestas(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(contacto_email=""))
        self.assertIn("contacto_email", r.context["form"].errors)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(contacto_email="portero@a.co; no-es-correo"))
        self.assertIn("contacto_email", r.context["form"].errors)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(contacto_email="portero@a.co, jefe@a.co"))
        t = Ticket.objects.get()
        self.assertEqual(t.correos_respuesta, ["portero@a.co", "jefe@a.co"])
        destinatarios = {d for m in mail.outbox for d in m.to + m.cc}
        self.assertTrue({"portero@a.co", "jefe@a.co"} <= destinatarios)

    def test_correo_principal_y_con_copia_como_en_un_correo(self):
        self.client.force_login(self.cli_a)
        r = self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(contacto_copias="jefe@a.co; no-es-correo"))
        self.assertIn("contacto_copias", r.context["form"].errors)
        mail.outbox.clear()
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(
            contacto_email="portero@a.co", contacto_copias="jefe@a.co; PORTERO@a.co; compras@a.co"))
        t = Ticket.objects.get()
        self.assertEqual(t.correos_respuesta, ["portero@a.co", "jefe@a.co", "compras@a.co"])
        al_cliente = next(m for m in mail.outbox if "portero@a.co" in m.to)
        self.assertEqual(al_cliente.to, ["portero@a.co"])
        self.assertTrue({"jefe@a.co", "compras@a.co"} <= set(al_cliente.cc))
        # Al volver a abrir el formulario, el principal y las copias se ven por separado
        self.client.force_login(self.supervisor)
        form = self.client.get(reverse("editar_ticket", args=[t.radicado])).context["form"]
        self.assertEqual((form.initial["contacto_email"], form.initial["contacto_copias"]),
                         ("portero@a.co", "jefe@a.co; compras@a.co"))

    def test_dias_no_disponibles_se_guardan_y_los_ve_el_area_administrativa(self):
        self.client.force_login(self.cli_a)
        self.client.post(reverse("nuevo_ticket"), self.datos_radicacion(
            nd_viernes="tarde", nd_sabado="todo", nd_domingo="todo",
        ))
        t = Ticket.objects.get()
        self.assertEqual(t.no_disponible, {"viernes": "tarde", "sabado": "todo"})  # el domingo no se trabaja
        self.assertEqual(t.resumen_no_disponible, "Viernes tarde · Sábado todo el día")
        self.client.force_login(self.supervisor)
        self.assertIn("Viernes tarde", self.client.get(t.get_absolute_url()).content.decode())
        # La próxima solicitud arranca con lo que indicó la vez anterior
        self.client.force_login(self.cli_a)
        form = self.client.get(reverse("nuevo_ticket")).context["form"]
        self.assertEqual((form.initial["nd_viernes"], form.initial["contacto_email"]), ("tarde", "portero@a.co"))

    def test_inicializar_carga_fallas_sin_duplicar(self):
        import os
        from django.core.management import call_command
        from .models import TipoFalla
        call_command("inicializar", stdout=open(os.devnull, "w"))
        n = TipoFalla.objects.count()
        call_command("inicializar", stdout=open(os.devnull, "w"))
        self.assertEqual(TipoFalla.objects.count(), n)
        cctv = Categoria.objects.get(nombre="Mantenimiento CCTV IP")
        self.assertTrue(cctv.fallas.filter(nombre__icontains="sin imagen").exists())


class ActaBase(BaseTickets):
    FIRMA = "data:image/png;base64," + __import__("base64").b64encode(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"0" * 40).decode()

    def setUp(self):
        super().setUp()
        self.ticket = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO, sede=self.sede_a,
                                        contacto_nombre="Portero", contacto_email="portero@a.co")

    def datos_acta(self, **kw):
        foto = SimpleUploadedFile("camara.jpg", bytes([255, 216, 255]) + b"0" * 50, content_type="image/jpeg")
        datos = {
            "ciudad": "Villavicencio", "actividad": ["Soporte"], "sistema": "CCTV", "diagnostico": "Sin visual",
            "resultado": "solucionado", "observaciones": "Se cambió la fuente", "cargo_tecnico": "Analista",
            "firma_tecnico": self.FIRMA, "recibio": "Portero", "cargo_recibio": "Vigilante", "firma_cliente": self.FIRMA,
            "modalidad": "presencial",
            "copia_correo": "coordinadortic@janotec.test ; otro@a.co", "marcar_resuelto": "on",
            "latitud": "4.1354083", "longitud": "-73.6390567",
            "equipos-TOTAL_FORMS": "1", "equipos-INITIAL_FORMS": "0", "equipos-MIN_NUM_FORMS": "0", "equipos-MAX_NUM_FORMS": "30",
            "equipos-0-equipo": "Cámara", "equipos-0-marca": "Hikvision", "equipos-0-cantidad": "1", "equipos-0-fotos": [foto],
        }
        datos.update(kw)
        for clave in [k for k in datos if k.startswith("equipos-") and k.endswith("-equipo") and datos[k]]:
            datos.setdefault(clave.replace("-equipo", "-propiedad"), "cliente")  # en el acta es obligatorio decir de quién es
        return datos


class ActaEnPlataformaTests(ActaBase):
    def test_encargado_diligencia_acta_y_resuelve(self):
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Diligenciar acta de soporte")
        form = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).context["form"]
        self.assertEqual((form.initial["recibio"], form.initial["sistema"]), ("Portero", "CCTV"))
        mail.outbox.clear()
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta())
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertRedirects(r, acta.get_absolute_url())
        self.assertEqual((acta.numero, acta.origen, acta.tecnico, acta.resultado), (100001, "plataforma", self.agente, "solucionado"))
        self.assertEqual(acta.equipos.get().fotos.count(), 1)
        self.assertEqual(sorted(acta.firmas.values_list("rol", flat=True)), ["cliente", "tecnico"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.VISITA_EJECUTADA)
        self.assertTrue(self.ticket.seguimientos.filter(interno=False, mensaje__contains="100001").exists())
        destinatarios = {d for m in mail.outbox for d in m.to + m.cc}
        self.assertTrue({"otro@a.co", "portero@a.co"} <= destinatarios)
        # Segunda acta: siguiente número
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(marcar_resuelto=""))
        self.assertEqual(ActaSoporte.objects.filter(ticket=self.ticket).count(), 2)
        self.assertTrue(ActaSoporte.objects.filter(numero=100002).exists())

    def test_si_no_quedo_solucionado_queda_pendiente_nueva_visita(self):
        self.client.force_login(self.agente)
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(resultado="parcial"))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.PENDIENTE_VISITA)

    def test_sin_firma_del_cliente_no_se_guarda(self):
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(firma_cliente=""))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ActaSoporte.objects.exists())
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(firma_tecnico="data:image/png;base64,AAAA"))
        self.assertFalse(ActaSoporte.objects.exists())

    def test_solo_el_encargado_y_no_el_cliente(self):
        self.client.force_login(self.agente2)  # otro técnico: no ve el ticket
        self.assertEqual(self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).status_code, 404)
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).status_code, 403)

    def test_cliente_ve_acta_fotos_y_firmas_pero_no_otra_empresa(self):
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta())
        acta = ActaSoporte.objects.get()
        foto, firma = acta.equipos.get().fotos.get(), acta.firmas.first()
        self.client.force_login(self.cli_a)
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Actas de la visita técnica")
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["foto", foto.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["firma", firma.pk])).status_code, 200)
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["foto", foto.pk])).status_code, 404)
        self.assertEqual(self.client.get(acta.get_absolute_url()).status_code, 404)


class CierreAutomaticoTests(BaseTickets):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def resuelto_hace(self, dias, **kw):
        from datetime import timedelta
        from django.utils import timezone
        t = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO, contacto_email="contacto@a.co", **kw)
        t.cambiar_estado(E.VISITA_EJECUTADA, self.agente, "Listo")
        Ticket.objects.filter(pk=t.pk).update(fecha_resolucion=timezone.now() - timedelta(days=dias, minutes=1))
        t.refresh_from_db()
        return t

    def test_comando_cierra_solo_los_vencidos_y_avisa(self):
        import os
        from django.core.management import call_command
        vencido, reciente = self.resuelto_hace(3), self.resuelto_hace(2)
        mail.outbox.clear()
        call_command("cerrar_resueltos", stdout=open(os.devnull, "w"))
        vencido.refresh_from_db()
        reciente.refresh_from_db()
        self.assertEqual((vencido.estado, reciente.estado), (E.CERRADO, E.VISITA_EJECUTADA))
        self.assertIsNotNone(vencido.fecha_cierre)
        seg = vencido.seguimientos.filter(estado_nuevo=E.CERRADO).get()
        self.assertIsNone(seg.autor)
        self.assertIn("automáticamente", seg.mensaje)
        self.assertFalse(any("contacto@a.co" in m.to for m in mail.outbox))

    def test_red_de_seguridad_al_abrir_la_plataforma(self):
        t = self.resuelto_hace(4)
        self.client.force_login(self.cli_a)
        self.client.get(reverse("mis_tickets"))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.CERRADO)
        # El cliente aún puede calificar tras el cierre automático
        self.assertContains(self.client.get(t.get_absolute_url()), "¿Cómo califica la atención recibida?")

    def test_cliente_ve_la_fecha_limite(self):
        t = self.resuelto_hace(1)
        self.client.force_login(self.cli_a)
        html = self.client.get(t.get_absolute_url()).content.decode()
        self.assertIn("se cerrará automáticamente", html)
        self.assertIsNotNone(t.fecha_cierre_automatico)

    @override_settings(TICKET_DIAS_CIERRE_AUTOMATICO=5)
    def test_plazo_configurable(self):
        import os
        from django.core.management import call_command
        t = self.resuelto_hace(4)
        call_command("cerrar_resueltos", stdout=open(os.devnull, "w"))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.VISITA_EJECUTADA)

    def test_si_el_cliente_reabre_no_se_cierra(self):
        import os
        from django.core.management import call_command
        t = self.resuelto_hace(1)
        self.client.force_login(self.cli_a)
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "reabrir", "motivo": "Sigue igual"})
        from datetime import timedelta
        from django.utils import timezone
        Ticket.objects.filter(pk=t.pk).update(actualizado=timezone.now() - timedelta(days=10))
        call_command("cerrar_resueltos", stdout=open(os.devnull, "w"))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.PENDIENTE_VISITA)


class OrdenTipoCorreoTests(BaseTickets):
    def crear_en(self, horas_atras, **kw):
        from datetime import timedelta
        from django.utils import timezone
        t = self.crear_ticket(**kw)
        Ticket.objects.filter(pk=t.pk).update(creado=timezone.now() - timedelta(hours=horas_atras))
        return t

    def radicados(self, url):
        import re
        return re.findall(r'class="radicado" href="[^"]+">(JNT-[\d-]+)', self.client.get(url).content.decode())

    def test_el_ultimo_que_llega_queda_de_primero(self):
        viejo, medio, nuevo = self.crear_en(50), self.crear_en(5), self.crear_en(0)
        esperado = [nuevo.radicado, medio.radicado, viejo.radicado]
        self.client.force_login(self.cli_a)
        self.assertEqual(self.radicados(reverse("mis_tickets")), esperado)
        self.client.force_login(self.supervisor)
        self.assertEqual(self.radicados(reverse("panel")), esperado)
        self.assertEqual(self.radicados(reverse("panel") + "?estado=generado"), esperado)  # también al filtrar

    def test_por_prioridad_el_mas_reciente_arriba_en_cada_nivel(self):
        alta_vieja = self.crear_en(30, prioridad="alta")
        alta_nueva = self.crear_en(1, prioridad="alta")
        baja = self.crear_en(0, prioridad="baja")
        self.client.force_login(self.supervisor)
        self.assertEqual(self.radicados(reverse("panel") + "?orden=prioridad"),
                         [alta_nueva.radicado, alta_vieja.radicado, baja.radicado])

    def test_fecha_estilo_correo_y_nuevos_en_negrita(self):
        from datetime import timedelta
        from django.utils import timezone
        from tickets.templatetags.tickets_extras import fecha_correo
        ahora = timezone.now()
        self.assertIn(":", fecha_correo(ahora))
        self.assertEqual(fecha_correo(ahora - timedelta(days=1)), "Ayer")
        self.assertRegex(fecha_correo(ahora - timedelta(days=800)), r"^\d\d/\d\d/\d{4}$")
        self.crear_en(0)
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("panel")), "fila-nueva")


class ModalidadActaTests(ActaBase):
    """Presencial o virtual: debe quedar indicado en toda acta nueva."""

    def guardar(self, **kw):
        self.client.force_login(self.agente)
        return self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(**kw), follow=True)

    def test_es_obligatorio_indicarla(self):
        r = self.guardar(modalidad="")
        self.assertFalse(ActaSoporte.objects.filter(ticket=self.ticket).exists())
        self.assertContains(r, "obligatorio")

    def test_presencial_sigue_exigiendo_firma_y_guarda_la_ubicacion(self):
        self.guardar(firma_cliente="")
        self.assertFalse(ActaSoporte.objects.filter(ticket=self.ticket).exists())
        self.guardar()
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertEqual(acta.modalidad, "presencial")
        self.assertIsNotNone(acta.latitud)
        self.assertIsNotNone(acta.firma_cliente)

    def test_virtual_sin_firma_del_cliente_ni_ubicacion(self):
        r = self.guardar(modalidad="virtual", firma_cliente=self.FIRMA)  # aunque llegue una firma, no se guarda
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertEqual(acta.modalidad, "virtual")
        self.assertIsNone(acta.firma_cliente)
        self.assertIsNotNone(acta.firma_tecnico)
        self.assertIsNone(acta.latitud)
        self.assertIsNone(acta.longitud)
        self.assertEqual(acta.recibio, "Portero")
        self.assertContains(r, "no lleva firma del cliente")
        self.assertContains(self.client.get(reverse("actas_lista")), "Virtual")

    def test_virtual_exige_firma_del_tecnico_y_quien_atendio(self):
        self.guardar(modalidad="virtual", firma_cliente="", firma_tecnico="")
        self.guardar(modalidad="virtual", firma_cliente="", recibio="")
        self.assertFalse(ActaSoporte.objects.filter(ticket=self.ticket).exists())

    def test_el_correo_dice_la_verdad_segun_la_modalidad(self):
        mail.outbox.clear()
        self.guardar(modalidad="virtual", firma_cliente="")
        cuerpo = " ".join(m.body for m in mail.outbox if "Acta" in m.subject)
        self.assertIn("atendido de manera virtual", cuerpo)
        self.assertNotIn("en sus instalaciones", cuerpo)

    def test_actas_anteriores_no_muestran_modalidad(self):
        self.guardar()
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        ActaSoporte.objects.filter(pk=acta.pk).update(modalidad="")
        self.assertNotContains(self.client.get(acta.get_absolute_url()), "Modalidad")


class OportunidadesTests(ActaBase):
    """Oportunidades comerciales reportadas por el técnico en el acta."""

    def test_acta_con_oportunidad_llega_al_area_administrativa(self):
        from .models import OportunidadComercial
        self.client.force_login(self.agente)
        mail.outbox.clear()
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(
            oportunidad_servicios=["cctv", "energia_solar"], oportunidad_nota="NOTA-OPORTUNIDAD-77 cámara para el parqueadero",
        ))
        o = OportunidadComercial.objects.get()
        self.assertEqual((o.empresa, o.detectada_por, o.estado), (self.emp_a, self.agente, "nueva"))
        self.assertEqual(o.servicios_display, ["CCTV", "Energía solar"])
        self.assertTrue(any("[Oportunidad]" in m.subject for m in mail.outbox))
        # El cliente NO la ve; el área administrativa sí, y la puede avanzar
        self.client.force_login(self.cli_a)
        self.assertNotContains(self.client.get(o.acta.get_absolute_url()), "NOTA-OPORTUNIDAD-77")
        self.assertEqual(self.client.get(reverse("oportunidades")).status_code, 403)
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("oportunidades")).status_code, 403)
        # El coordinador técnico tampoco: las oportunidades son del área comercial
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("oportunidades")).status_code, 403)
        self.client.force_login(Usuario.objects.create_user("asesor_op", "op@janotec.test", "Clave-segura-1", rol="comercial"))
        self.assertContains(self.client.get(reverse("oportunidades")), "NOTA-OPORTUNIDAD-77")
        self.assertContains(self.client.get(reverse("oportunidades") + "?servicio=energia_solar"), "NOTA-OPORTUNIDAD-77")
        self.assertNotContains(self.client.get(reverse("oportunidades") + "?servicio=radio"), "NOTA-OPORTUNIDAD-77")
        self.client.post(reverse("oportunidad_actualizar", args=[o.pk]),
                         {"estado": "cotizado", "nota": "Se envió COT-12", "volver": "https://sitio-externo.com/"})
        o.refresh_from_db()
        self.assertEqual(o.estado, "cotizado")
        self.assertIn("COT-12", o.seguimiento)

    def test_acta_sin_oportunidad_no_crea_nada(self):
        from .models import OportunidadComercial
        self.client.force_login(self.agente)
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta())
        self.assertFalse(OportunidadComercial.objects.exists())


class RecepcionTests(BaseTickets):
    """Paso 2: el área técnica recepciona y corrige las solicitudes."""

    def test_recepcionar_avisa_al_cliente_y_deja_listo_para_el_cronograma(self):
        t = self.crear_ticket(contacto_email="portero@a.co", impacto="media")
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("panel")), "Recepcionar")
        mail.outbox.clear()
        self.client.post(reverse("recepcionar", args=[t.radicado]))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.RECEPCIONADO)
        self.assertFalse(any("portero@a.co" in m.to for m in mail.outbox))
        self.assertIn(t, Ticket.objects.filter(estado__in=Ticket.ESTADOS_PROGRAMABLES))
        # El técnico y el cliente no recepcionan
        otro = self.crear_ticket(asignado_a=self.agente)
        for usuario in (self.agente, self.cli_a):
            self.client.force_login(usuario)
            self.assertEqual(self.client.post(reverse("recepcionar", args=[otro.radicado])).status_code, 403)

    def test_editar_cualquier_campo_deja_nota_interna(self):
        t = self.crear_ticket(sede=self.sede_a, contacto_email="portero@a.co", departamento="Meta",
                              municipio="Villavicencio", direccion="Calle 1", contacto_telefono="300", impacto="baja")
        self.client.force_login(self.supervisor)
        datos = self.datos_radicacion(empresa=self.emp_b.pk, sede=self.sede_b.pk, descripcion="Corregido por Guana",
                                      impacto="alta", alturas="si")
        self.client.post(reverse("editar_ticket", args=[t.radicado]), datos)
        t.refresh_from_db()
        self.assertEqual((t.empresa, t.sede, t.descripcion, t.impacto, t.trabajo_alturas),
                         (self.emp_b, self.sede_b, "Corregido por Guana", "alta", True))
        nota = t.seguimientos.get(interno=True)
        self.assertIn("Empresa", nota.mensaje)
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("editar_ticket", args=[t.radicado])).status_code, 403)


class EscribirConSugerenciasTests(BaseTickets):
    """Empresa, sede, sistema y falla se escriben: las listas sugieren, pero no limitan."""

    def setUp(self):
        super().setUp()
        from .models import TipoFalla
        self.sin_imagen = TipoFalla.objects.create(categoria=self.cat, nombre="Cámara sin imagen")

    def escrito(self, **kw):
        datos = self.datos_radicacion(**kw)
        for campo in ("sede", "categoria"):
            datos.pop(campo, None)
        return datos

    def test_falla_y_sistema_que_no_estan_en_la_lista(self):
        self.client.force_login(self.cli_a)
        html = self.client.get(reverse("nuevo_ticket")).content.decode()
        self.assertIn("<datalist", html)
        self.assertIn('value="Cámara sin imagen"', html)  # se sugiere
        self.client.post(reverse("nuevo_ticket"), self.escrito(sede_nombre="Principal", sistema="cctv",
                                                              falla_nombre="cámara sin imagen", descripcion="uno"))
        t = Ticket.objects.get(descripcion="uno")
        self.assertEqual((t.categoria, t.falla, t.falla_texto), (self.cat, self.sin_imagen, ""))  # coincide sin importar mayúsculas
        self.client.post(reverse("nuevo_ticket"), self.escrito(sede_nombre="Principal", sistema="Cerca eléctrica",
                                                              falla_nombre="La cerca da corrientazo en la puerta", descripcion="dos"))
        t = Ticket.objects.get(descripcion="dos")
        self.assertEqual((t.categoria.nombre, t.sistema_texto, t.falla, t.falla_texto),
                         ("Otro", "Cerca eléctrica", None, "La cerca da corrientazo en la puerta"))
        self.assertEqual((t.sistema_display, t.falla_display), ("Cerca eléctrica", "La cerca da corrientazo en la puerta"))
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(t.get_absolute_url()), "La cerca da corrientazo en la puerta")

    def test_sede_nueva_escrita_por_el_cliente(self):
        self.client.force_login(self.cli_a)
        self.client.post(reverse("nuevo_ticket"), self.escrito(sede_nombre="Bodega norte", sistema="CCTV"))
        t = Ticket.objects.get()
        self.assertEqual((t.sede.nombre, t.sede.empresa, t.sede.ciudad), ("Bodega norte", self.emp_a, "Villavicencio"))

    def test_quien_radica_para_el_cliente_escribe_la_empresa(self):
        self.client.force_login(self.supervisor)
        self.client.post(reverse("nuevo_ticket"), self.escrito(empresa_nombre="  empresa   a ", sede_nombre="principal", sistema="CCTV"))
        t = Ticket.objects.get()
        self.assertEqual((t.empresa, t.sede), (self.emp_a, self.sede_a))
        r = self.client.post(reverse("nuevo_ticket"), self.escrito(empresa_nombre="No existe SAS", sistema="CCTV"))
        self.assertIn("No encontramos", r.context["form"].errors["empresa_nombre"][0])


class EtapasEInformeTests(ActaBase):
    """Unificación de janotecsas: etapas con fecha estimada y nota, e «Informe en elaboración»."""

    def test_soporte_no_lleva_informe_ni_propuesta(self):
        from . import etapas
        t = self.crear_ticket()
        self.assertEqual(etapas.claves_de(t), ["recibida", "programada", "inspeccion", "cierre"])
        self.assertEqual([e["estado"] for e in etapas.etapas_cliente(t)], ["actual", "pendiente", "pendiente", "pendiente"])

    def test_requerimiento_con_acta_queda_en_informe(self):
        from . import etapas
        self.ticket.tipo = Ticket.Tipo.REQUERIMIENTO
        self.ticket.save()
        self.client.force_login(self.agente)
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta())
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.INFORME)
        actual = [e for e in etapas.etapas_cliente(self.ticket) if e["estado"] == "actual"]
        self.assertEqual(actual[0]["clave"], "informe")
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Pasar a comercial para la propuesta")

    def test_diagnostico_tambien_lleva_informe(self):
        from . import etapas
        diag, _ = Categoria.objects.get_or_create(nombre="Diagnóstico técnico")
        self.assertIn("informe", etapas.claves_de(self.crear_ticket(categoria=diag)))

    def test_supervisor_guarda_fecha_estimada_y_nota_que_ve_el_cliente(self):
        t = self.crear_ticket()
        self.client.force_login(self.supervisor)
        r = self.client.post(reverse("guardar_etapas", args=[t.radicado]),
                             {"fecha_programada": "2026-11-05", "nota_programada": "Esperando autorización del conjunto"})
        self.assertRedirects(r, t.get_absolute_url())
        e = t.etapas_cliente.get()
        self.assertEqual((e.clave, str(e.fecha), e.nota), ("programada", "2026-11-05", "Esperando autorización del conjunto"))
        self.client.force_login(self.cli_a)
        r = self.client.get(t.get_absolute_url())
        self.assertContains(r, "Esperando autorización del conjunto")
        self.assertContains(r, "Fecha estimada")
        self.client.force_login(self.agente)  # el técnico no edita etapas
        self.assertNotEqual(self.client.post(reverse("guardar_etapas", args=[t.radicado]), {"nota_cierre": "x"}).status_code, 302)

    def test_informe_al_cliente(self):
        self.ticket.estado = E.INFORME
        self.ticket.save()
        Usuario.objects.create_user("miguel", "dir@x.co", "Clave-segura-1", rol="direccion")
        url = reverse("entregar_informe", args=[self.ticket.radicado])
        pdf = lambda: SimpleUploadedFile("informe.pdf", b"%PDF-1.4 prueba", content_type="application/pdf")
        datos = {"nota": "Se requieren 4 cámaras", "destino": "cliente"}
        # Sin aprobación de dirección no sale, aunque se adjunte el PDF a mano
        self.client.force_login(self.agente)
        self.client.post(url, {**datos, "informe": [pdf()], "urgente": "1", "motivo_urgencia": "El cliente lo necesita hoy"})
        self.client.force_login(self.supervisor)
        self.client.post(url, {**datos, "informe": [pdf()]})
        self.client.post(url, {**datos, "informe": [pdf()], "urgente": "1", "motivo_urgencia": "hoy"})  # motivo muy corto
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.INFORME)
        # Coordinación lo entrega como urgente: queda el motivo y dirección recibe el aviso
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {**datos, "informe": [pdf()], "urgente": "1", "motivo_urgencia": "El cliente lo necesita hoy"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.VISITA_EJECUTADA)
        self.assertTrue(Adjunto.objects.filter(ticket=self.ticket, es_informe=True).exists())
        self.assertTrue(self.ticket.seguimientos.filter(interno=True, mensaje__contains="El cliente lo necesita hoy").exists())
        self.assertTrue(any("dir@x.co" in m.to and "urgencia" in m.subject for m in mail.outbox))
        # Los cambios de estado no le llegan al cliente por correo; el informe lo ve en su solicitud
        self.assertFalse(any("portero@a.co" in m.to for m in mail.outbox))

    def test_informe_a_comercial_crea_la_ficha_para_la_propuesta(self):
        from . import etapas
        self.ticket.estado, self.ticket.tipo = E.INFORME, Ticket.Tipo.REQUERIMIENTO
        self.ticket.save()
        self.client.force_login(Usuario.objects.create_user("miguel", "dir@x.co", "Clave-segura-1", rol="direccion"))
        # Sin servicios ni materiales no pasa: con ellos se arma la propuesta y bodega alista lo aprobado
        r = self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]),
                             {"nota": "Cotizar 4 cámaras IP", "destino": "comercial"}, follow=True)
        self.assertContains(r, "escriba los servicios a cotizar")
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.area, self.ticket.seguimientos.filter(mensaje__startswith="Informe técnico").count()),
                         (Ticket.Area.TECNICA, 0))
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]),
                         {"nota": "Cotizar 4 cámaras IP", "destino": "comercial", "servicios": "1 Instalación de cámaras",
                          "materiales": "4 Cámara IP 4 MP\n60 m Cable UTP cat 6"})
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.area, self.ticket.estado), (Ticket.Area.COMERCIAL, E.EN_PROCESO))
        self.assertEqual(self.ticket.ficha.etapa, "elaborando")
        informe = self.ticket.ficha.informe
        self.assertEqual((informe.servicios, informe.materiales), ("1 Instalación de cámaras", "4 Cámara IP 4 MP\n60 m Cable UTP cat 6"))
        self.assertIn("Cotizar 4 cámaras IP", self.ticket.ficha.levantamiento)
        actual = [e["clave"] for e in etapas.etapas_cliente(self.ticket) if e["estado"] == "actual"]
        self.assertEqual(actual, ["propuesta"])

    def test_informe_solo_en_su_estado(self):
        self.client.force_login(self.agente)
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]), {"nota": "x", "destino": "cliente"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.AUTORIZADO)


class ConsultaSinUsuarioTests(BaseTickets):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.t = self.crear_ticket(contacto_telefono="+57 310 123 4567")
        self.t.seguimientos.create(mensaje="Nota interna secreta", interno=True)

    def test_radicado_y_telefono_muestran_las_etapas_sin_datos_internos(self):
        r = self.client.post(reverse("consulta"), {"radicado": self.t.radicado.lower(), "telefono": "310-123-4567"}, follow=True)
        self.assertContains(r, self.t.radicado)
        self.assertContains(r, "Etapas del servicio")
        self.assertNotContains(r, "Nota interna secreta")

    def test_telefono_equivocado_no_muestra_nada(self):
        r = self.client.post(reverse("consulta"), {"radicado": self.t.radicado, "telefono": "3209999999"})
        self.assertContains(r, "No encontramos")
        self.assertNotContains(r, "Etapas del servicio")
        r = self.client.post(reverse("consulta"), {"radicado": self.t.radicado, "telefono": "4567"})
        self.assertContains(r, "No encontramos")

    def test_limite_de_intentos(self):
        for _ in range(10):
            self.client.post(reverse("consulta"), {"radicado": "JNT-0", "telefono": "3100000000"})
        r = self.client.post(reverse("consulta"), {"radicado": self.t.radicado, "telefono": "3101234567"})
        self.assertEqual(r.status_code, 429)

    def test_enlace_firmado_del_correo(self):
        from .consulta import enlace_consulta
        mail.outbox.clear()
        from . import notificaciones
        notificaciones.ticket_creado(self.t)
        enlace = enlace_consulta(self.t)
        self.assertTrue(any(enlace in m.body for m in mail.outbox))
        ruta = enlace.split("://", 1)[-1].split("/", 1)[-1]
        self.assertContains(self.client.get("/" + ruta), self.t.radicado)
        r = self.client.get(reverse("consulta_token", args=["falso:123"]))
        self.assertRedirects(r, reverse("consulta"))

    def test_login_ofrece_la_consulta(self):
        self.assertContains(self.client.get(reverse("login")), reverse("consulta"))


class ClientesYSedesTests(BaseTickets):
    """El coordinador administra los clientes y sus sedes: es el listado que se sugiere al radicar."""

    def test_crear_editar_y_sede_aparece_al_radicar(self):
        from accounts.models import Empresa
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("clientes")), self.emp_a.razon_social)
        self.client.post(reverse("cliente_nuevo_admin"), {"razon_social": "  Clínica  del Llano ", "nit": "900123",
                                                          "sector": "privado", "ciudad": "Villavicencio", "activa": "on"})
        emp = Empresa.objects.get(nit="900123")
        self.assertEqual(emp.razon_social, "Clínica del Llano")
        r = self.client.post(reverse("cliente_nuevo_admin"), {"razon_social": "clínica del llano", "sector": "privado"})
        self.assertContains(r, "Ya existe el cliente")
        self.client.post(reverse("cliente_detalle", args=[emp.pk]), {"nombre": "Sede Norte", "departamento": "Meta",
                                                                      "ciudad": "Villavicencio", "activa": "on"})
        sede = emp.sedes.get()
        self.client.post(reverse("cliente_detalle", args=[emp.pk]), {"sede": sede.pk, "nombre": "Sede Norte 2",
                                                                      "departamento": "Meta", "activa": "on"})
        sede.refresh_from_db()
        self.assertEqual(sede.nombre, "Sede Norte 2")
        self.client.post(reverse("cliente_editar_admin", args=[emp.pk]), {"razon_social": "Clínica del Llano SAS",
                                                                          "sector": "privado", "activa": "on"})
        emp.refresh_from_db()
        self.assertEqual(emp.razon_social, "Clínica del Llano SAS")
        # Aparece en el autocompletado del formulario de radicación
        html = self.client.get(reverse("nuevo_ticket")).content.decode()
        self.assertIn("Clínica del Llano SAS", html)
        self.assertIn("Sede Norte 2", html)

    def test_eliminar_sin_historial_e_inactivar_con_historial(self):
        from accounts.models import Empresa, Sede
        self.client.force_login(self.supervisor)
        libre = Empresa.objects.create(razon_social="Sin historial")
        Sede.objects.create(empresa=libre, nombre="Única")
        self.client.post(reverse("cliente_eliminar", args=[libre.pk]))
        self.assertFalse(Empresa.objects.filter(pk=libre.pk).exists())
        self.crear_ticket(sede=self.sede_a)
        self.client.post(reverse("sede_eliminar", args=[self.sede_a.pk]))
        self.sede_a.refresh_from_db()
        self.assertFalse(self.sede_a.activa)
        r = self.client.post(reverse("cliente_eliminar", args=[self.emp_a.pk]), follow=True)
        self.assertContains(r, "quedó inactivo")
        self.emp_a.refresh_from_db()
        self.assertFalse(self.emp_a.activa)
        self.assertTrue(Ticket.objects.filter(empresa=self.emp_a).exists())

    def test_solo_el_area_tecnica(self):
        for usuario in (self.agente, self.cli_a):
            self.client.force_login(usuario)
            self.assertEqual(self.client.get(reverse("clientes")).status_code, 403)
            self.assertEqual(self.client.post(reverse("cliente_eliminar", args=[self.emp_a.pk])).status_code, 403)


class ActaDelTecnicoAsignadoTests(BaseTickets):
    """El técnico al que se le asigna el ticket (con o sin cronograma) diligencia el acta."""

    def test_asignado_directo_puede_diligenciar_el_acta(self):
        from .models import PasoSaltado
        for estado in (E.RECEPCIONADO, E.EN_PROCESO, E.EN_CRONOGRAMA):
            t = self.crear_ticket(asignado_a=self.agente, estado=estado, impacto="media")
            if estado == E.RECEPCIONADO:  # paso a paso: sin cronograma, coordinación omite la programación
                PasoSaltado.objects.create(ticket=t, paso="programacion", motivo="Asignado directo", autor=self.supervisor)
            self.client.force_login(self.agente)
            self.assertContains(self.client.get(t.get_absolute_url()), "Diligenciar acta de soporte", msg_prefix=estado)
            self.assertEqual(self.client.get(reverse("acta_nueva", args=[t.radicado])).status_code, 200)
        # Sin recepcionar, ya cerrado o de otro técnico no
        for kw in ({"estado": E.GENERADO, "asignado_a": self.agente}, {"estado": E.CERRADO, "asignado_a": self.agente},
                   {"estado": E.RECEPCIONADO}):
            t = self.crear_ticket(**kw)
            self.client.force_login(self.supervisor if "asignado_a" not in kw else self.agente)
            if "asignado_a" in kw:
                self.assertNotContains(self.client.get(t.get_absolute_url()), "Diligenciar acta de soporte")

    def test_el_area_tecnica_no_pone_estados_comerciales(self):
        t = self.crear_ticket(estado=E.RECEPCIONADO)
        self.client.force_login(self.supervisor)
        opciones = [v for v, _ in self.client.get(t.get_absolute_url()).context["form_gestion"].fields["estado"].choices]
        self.assertIn("en_proceso", opciones)  # el coordinador lo pasa a comercial (con traspaso real)
        self.assertNotIn("propuesta_enviada", opciones)

    def test_coordinador_pasa_a_gestion_comercial_de_verdad(self):
        t = self.crear_ticket(estado=E.INFORME, asignado_a=self.agente, impacto="media", contacto_email="portero@a.co")
        self.client.force_login(self.supervisor)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "estado": "en_proceso", "prioridad": t.prioridad, "asignado_a": self.agente.pk, "visibilidad": "publico",
            "mensaje": "Cotizar alarma: 1 panel, 4 sensores PIR, 1 sirena"})
        t.refresh_from_db()
        self.assertEqual((t.estado, t.area, t.ficha.etapa), (E.EN_PROCESO, Ticket.Area.COMERCIAL, "elaborando"))
        self.assertIn("4 sensores PIR", t.ficha.informe.materiales)
        self.assertFalse(any("portero@a.co" in m.to for m in mail.outbox))  # el texto va al informe, no al cliente


class EquiposIntervenidosYUtilizadosTests(ActaBase):
    """El acta separa los equipos del cliente que se intervinieron de lo que se usó para dejar el servicio funcionando."""

    def test_sin_desmonte_el_acta_se_guarda_aunque_la_fila_de_retirados_quede_vacia(self):
        """Como lo envía el navegador: la fila vacía de «Equipos retirados» no obliga a llenarla (sección opcional)."""
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        html = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).content.decode()
        self.assertIn("Se retiró (desmonte)", html)
        self.assertNotIn("Revisado, reparado o configurado", html)  # cada trabajo es una opción aparte
        self.assertNotIn("Agregar equipo retirado", html)  # ya no es una sección aparte
        for accion, propiedad in (("revisado", ""), ("retirado", "cliente")):  # fila vacía, con lo que quede marcado
            ActaSoporte.objects.filter(ticket=self.ticket).delete()
            r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(**{
                "equipos-0-equipo": "Cámara IP bala", "equipos-0-accion": "reparado",
                "equipos-TOTAL_FORMS": "2", "equipos-1-equipo": "", "equipos-1-accion": accion,
                "equipos-1-propiedad": propiedad, "equipos-1-placa": ""}))
            self.assertEqual(r.status_code, 302, accion)
            acta = ActaSoporte.objects.get(ticket=self.ticket)
            self.assertEqual(acta.equipos_retirados, [])
            self.assertEqual([e.trabajo for e in acta.equipos_intervenidos], ["reparado"])
        self.assertContains(self.client.get(acta.get_absolute_url()), "Reparado")

    def test_cada_equipo_intervenido_dice_de_quien_es(self):
        """Del cliente o de JANOTEC define el seguimiento; lo de JANOTEC lleva placa y sale en su hoja de vida."""
        from inventario.models import Activo

        from .models import ActaSoporte
        self.client.force_login(self.agente)
        url = reverse("acta_nueva", args=[self.ticket.radicado])
        self.assertContains(self.client.get(url), "¿De quién es?")
        r = self.client.post(url, self.datos_acta(**{"equipos-0-propiedad": "", "equipos-0-accion": "reparado"}))
        self.assertContains(r, "Indique si el equipo es del cliente o de JANOTEC.")
        r = self.client.post(url, self.datos_acta(**{"equipos-0-propiedad": "janotec", "equipos-0-accion": "configurado"}))
        self.assertContains(r, "Para hacerle seguimiento escriba la placa")
        r = self.client.post(url, self.datos_acta(**{"equipos-0-propiedad": "janotec", "equipos-0-accion": "configurado",
                                                     "equipos-0-placa": "cam-bal-2mp009"}))
        self.assertEqual(r.status_code, 302)
        e = ActaSoporte.objects.get(ticket=self.ticket).equipos.get()
        self.assertEqual((e.propiedad, e.placa, e.trabajo), ("janotec", "CAM-BAL-2MP009", "configurado"))
        Activo.objects.create(placa="CAM-BAL-2MP009", descripcion="Cámara bala")
        self.client.force_login(Usuario.objects.create_superuser("jefe.prueba", "jefe@janotec.test", "x"))
        self.assertContains(self.client.get(reverse("inventario:activo", args=["CAM-BAL-2MP009"])), e.acta.numero)

    def test_dos_listas_en_el_acta(self):
        from .models import ActaSoporte, EquipoActa
        self.client.force_login(self.agente)
        html = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).content.decode()
        self.assertIn("Equipos intervenidos", html)
        self.assertIn("Materiales y equipos utilizados", html)
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(**{
            "equipos-0-equipo": "Cámara IP bala", "equipos-0-observaciones": "Se cambió el conector",
            "utilizados-TOTAL_FORMS": "2", "utilizados-INITIAL_FORMS": "0", "utilizados-MIN_NUM_FORMS": "0",
            "utilizados-MAX_NUM_FORMS": "30", "utilizados-0-equipo": "Router", "utilizados-0-marca": "Ruijie",
            "utilizados-0-cantidad": "1", "utilizados-1-equipo": "",
        }))
        self.assertEqual(r.status_code, 302)  # la fila vacía no impide guardar
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertEqual([(e.equipo, e.clase) for e in acta.equipos_intervenidos], [("Cámara IP bala", EquipoActa.Clase.INTERVENIDO)])
        self.assertEqual([(e.equipo, e.marca) for e in acta.equipos_utilizados], [("Router", "Ruijie")])
        detalle = self.client.get(acta.get_absolute_url())
        self.assertContains(detalle, "Equipos intervenidos")
        self.assertContains(detalle, "Materiales y equipos utilizados")
        self.assertContains(detalle, "Ruijie")


class ResponsableDeCualquierAreaTests(BaseTickets):
    """El ticket puede quedar en manos de comercial, bodega, central o dirección mientras se resuelve."""

    def test_se_asigna_a_bodega_y_lo_gestiona(self):
        bodega = Usuario.objects.create_user("bodega1", "bodega1@janotec.test", "Clave-segura-1", rol="bodega",
                                             first_name="Alejandra")
        comercial = Usuario.objects.create_user("com1", "com1@janotec.test", "Clave-segura-1", rol="comercial")
        t = self.crear_ticket(estado=E.RECEPCIONADO, impacto="media")
        self.client.force_login(self.supervisor)
        opciones = [str(v) for v, _ in self.client.get(t.get_absolute_url()).context["form_gestion"].fields["asignado_a"].choices]
        self.assertIn(str(bodega.pk), opciones)
        self.assertIn(str(comercial.pk), opciones)
        self.assertNotIn(str(self.cli_a.pk), opciones)
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": t.estado, "prioridad": t.prioridad,
                                                                     "asignado_a": bodega.pk, "visibilidad": "interno",
                                                                     "mensaje": "Pendiente de material en bodega"})
        t.refresh_from_db()
        self.assertEqual(t.asignado_a, bodega)
        # Bodega lo ve en «Asignados a mí», lo abre y deja su gestión; no puede cambiar el estado
        self.client.force_login(bodega)
        self.assertContains(self.client.get(reverse("asignados")), t.radicado)
        r = self.client.get(t.get_absolute_url())
        self.assertEqual([v for v, _ in r.context["form_gestion"].fields["estado"].choices], [E.RECEPCIONADO])
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": t.estado, "visibilidad": "interno",
                                                                     "mensaje": "Material listo para entregar"})
        self.assertTrue(t.seguimientos.filter(autor=bodega, mensaje="Material listo para entregar").exists())
        # Sin asignación no lo ve ni lo gestiona
        otro = self.crear_ticket(estado=E.RECEPCIONADO)
        self.assertEqual(self.client.get(otro.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.post(reverse("gestionar", args=[otro.radicado]), {"estado": otro.estado}).status_code, 404)


class PanelDelTecnicoTests(BaseTickets):
    """El técnico arranca viendo lo que tiene programado en el cronograma y aún no ha atendido."""

    def test_arranca_en_programados_por_atender(self):
        from datetime import timedelta

        from django.utils import timezone
        ahora = timezone.now()
        luego = self.crear_ticket(asignado_a=self.agente, estado=E.EN_CRONOGRAMA, visita_programada=ahora + timedelta(days=2))
        pronto = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO, visita_programada=ahora + timedelta(days=1))
        hecho = self.crear_ticket(asignado_a=self.agente, estado=E.VISITA_EJECUTADA)
        self.client.force_login(self.agente)
        r = self.client.get(reverse("panel"))
        self.assertEqual(r.context["form"].data["estado"], "por_atender")
        self.assertEqual(list(r.context["pagina"]), [pronto, luego])  # la próxima visita primero
        self.assertNotIn(hecho, list(r.context["pagina"]))
        self.assertEqual(r.context["ind"]["por_atender"], 2)
        # Puede ver todo lo demás con el filtro
        self.assertIn(hecho, list(self.client.get(reverse("panel") + "?estado=abiertos").context["pagina"]))
        # El coordinador sigue arrancando en los abiertos
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("panel")).context["form"].data["estado"], "abiertos")


@override_settings(CORREO_CLIENTES_PRUEBA="pruebas@janotec.test", NOTIFICAR_SOPORTE_EMAIL="soporte@janotec.test",
                   EMAIL_BACKEND="config.correo.RedirigirClientesBackend",
                   EMAIL_BACKEND_REAL="django.core.mail.backends.locmem.EmailBackend")
class CorreoEnFaseDePruebasTests(BaseTickets):
    """En pruebas, lo que iba para un cliente llega al buzón de pruebas; el personal recibe normal."""

    def test_cliente_redirigido_y_personal_normal(self):
        from django.core.mail import send_mail
        send_mail("Hola", "x", "de@janotec.test", ["cliente@empresa.co", self.agente.email, "soporte@janotec.test"])
        m = mail.outbox[-1]
        self.assertEqual(set(m.to), {self.agente.email, "soporte@janotec.test", "pruebas@janotec.test"})
        self.assertIn("Iba para: cliente@empresa.co", m.body)
        send_mail("Interno", "x", "de@janotec.test", [self.agente.email])
        self.assertEqual((mail.outbox[-1].to, mail.outbox[-1].subject), ([self.agente.email], "Interno"))

    @override_settings(CORREO_COMERCIAL_PRUEBA="yuri@janotec.test", NOTIFICAR_COMERCIAL_EMAIL="comercial@janotec.test")
    def test_lo_del_cliente_prueba_solo_les_llega_a_quienes_prueban(self):
        from django.core.mail import send_mail
        bodega = Usuario.objects.create_user("sindy", "sindy@janotec.test", "Clave-segura-1", rol="bodega")
        prueba = self.crear_ticket()
        prueba.empresa = Empresa.objects.create(razon_social="PRUEBA", nit="9")
        prueba.save()
        real = self.crear_ticket()

        send_mail(f"[{prueba.radicado}] x", "x", "de@janotec.test", [self.agente.email, "cliente@empresa.co"])
        self.assertEqual(mail.outbox[-1].to, ["pruebas@janotec.test"])
        self.assertIn(self.agente.email, mail.outbox[-1].body)
        send_mail("[MAT-1] Material · PRUEBA", f"Solicitud · {prueba.radicado}", "de@janotec.test", [bodega.email])
        self.assertEqual(mail.outbox[-1].to, ["yuri@janotec.test"])
        send_mail("[Oportunidad] PRUEBA · CCTV", "x", "de@janotec.test", ["comercial@janotec.test", self.agente.email])
        self.assertEqual(set(mail.outbox[-1].to), {"yuri@janotec.test", "pruebas@janotec.test"})

        # Lo real no cambia, tampoco si en un correo van juntos un ticket real y uno de prueba
        send_mail(f"[{real.radicado}] x", "x", "de@janotec.test", [self.agente.email, bodega.email])
        self.assertEqual((set(mail.outbox[-1].to), mail.outbox[-1].subject),
                         ({self.agente.email, bodega.email}, f"[{real.radicado}] x"))
        send_mail("Semana", f"{real.radicado} y {prueba.radicado}", "de@janotec.test", [self.agente.email])
        self.assertEqual(mail.outbox[-1].to, [self.agente.email])
        send_mail("Esto es una prueba", "x", "de@janotec.test", [self.agente.email])
        self.assertEqual(mail.outbox[-1].to, [self.agente.email])


class TresCorreosAlClienteTests(BaseTickets):
    """Los cambios de estado no le llegan al cliente por correo (tampoco «en proceso» ni «cerrada»)."""

    def test_ningun_cambio_de_estado_le_llega(self):
        from . import notificaciones
        t = self.crear_ticket(contacto_email="portero@a.co", impacto="media")
        recibidos = {}
        for estado in (E.RECEPCIONADO, E.EN_CRONOGRAMA, E.AUTORIZADO, E.VISITA_EJECUTADA, E.PENDIENTE_VISITA,
                       E.INFORME, E.CERRADO):
            mail.outbox.clear()
            notificaciones.actualizacion_para_cliente(t, t.cambiar_estado(estado, self.supervisor, "Cambio"))
            recibidos[estado] = [m.subject for m in mail.outbox if "portero@a.co" in m.to]
        self.assertEqual({e for e, asuntos in recibidos.items() if asuntos}, set())

    def test_una_respuesta_escrita_al_cliente_si_le_llega(self):
        from . import notificaciones
        from .models import Seguimiento
        t = self.crear_ticket(contacto_email="portero@a.co", estado=E.EN_CRONOGRAMA)
        seg = Seguimiento.objects.create(ticket=t, autor=self.supervisor, mensaje="Llevamos el repuesto el lunes")
        notificaciones.actualizacion_para_cliente(t, seg)
        self.assertTrue(any("Tenemos una respuesta" in m.subject for m in mail.outbox))


class ActaRequeridosTests(ActaBase):
    """El acta tiene tres listas plegables: intervenidos, utilizados y requeridos; lo requerido va al informe."""

    def test_tres_listas_plegables_y_solo_se_abre_la_que_se_usa(self):
        self.client.force_login(self.agente)
        html = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).content.decode()
        for prefijo, titulo in (("equipos", "Equipos intervenidos"), ("utilizados", "Materiales y equipos utilizados"),
                                ("requeridos", "Materiales y equipos requeridos")):
            self.assertIn(titulo, html)
            self.assertIn(f'data-bs-target="#grupo-{prefijo}"', html)
            self.assertIn(f'id="grupo-{prefijo}"', html)
            self.assertIn(f'name="{prefijo}-TOTAL_FORMS"', html)
        self.assertNotIn('collapse show" id="grupo-requeridos"', html)  # cerradas hasta que el técnico las abra

    def test_lo_requerido_queda_en_el_acta_y_pasa_al_informe(self):
        from . import informe
        from .models import ActaSoporte, EquipoActa
        self.client.force_login(self.agente)
        datos = self.datos_acta(**{
            "requeridos-TOTAL_FORMS": "2", "requeridos-INITIAL_FORMS": "0", "requeridos-MIN_NUM_FORMS": "0",
            "requeridos-MAX_NUM_FORMS": "30",
            "requeridos-0-equipo": "Fuente de poder 12 V 5 A", "requeridos-0-cantidad": "2", "requeridos-0-marca": "Hikvision",
            "requeridos-1-equipo": "Disco duro 2 TB", "requeridos-1-referencia": "WD20PURZ",
        })
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), datos)
        self.assertEqual(r.status_code, 302)
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertEqual([(e.equipo, e.cantidad) for e in acta.equipos_requeridos],
                         [("Fuente de poder 12 V 5 A", "2"), ("Disco duro 2 TB", "")])
        self.assertTrue(all(e.clase == EquipoActa.Clase.REQUERIDO for e in acta.equipos_requeridos))
        self.assertContains(self.client.get(acta.get_absolute_url()), "Materiales y equipos requeridos")
        b = informe.generar(self.ticket, self.supervisor, tipo="visita")
        filas = b.filas_requeridas()
        self.assertEqual([(f["cantidad"], f["descripcion"], f["referencia"], f["marca"]) for f in filas],
                         [("2", "Fuente de poder 12 V 5 A", "", "Hikvision"), ("1", "Disco duro 2 TB", "WD20PURZ", "")])
        self.assertIn(informe.ALCANCE, b.conclusiones)


class AyudaRedaccionTests(ActaBase):
    """Los campos que escribe el técnico traen guía, frases rápidas y, si hay clave, el botón «Mejorar redacción»."""

    def _respuesta(self, texto, motivo="end_turn"):
        from types import SimpleNamespace
        return SimpleNamespace(stop_reason=motivo, content=[SimpleNamespace(type="text", text=texto)])

    def test_campos_del_acta_traen_la_ayuda_y_sin_clave_no_hay_boton_de_ia(self):
        import json
        import os
        from unittest import mock
        self.client.force_login(self.agente)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            r = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado]))
        html = r.content.decode()
        for tipo in ("diagnostico", "resultado", "oportunidad", "equipo"):
            self.assertIn(f'data-redaccion="{tipo}"', html)
        self.assertIn("js/redaccion.js", html)
        config = r.context["redaccion"]
        self.assertEqual(config["url"], "")
        self.assertIn("Se encontró ", config["tipos"]["diagnostico"]["frases"])
        self.assertIn('id="redaccion-config"', html)
        json.dumps(config)  # se puede pasar a la página

    def test_con_clave_aparece_el_boton(self):
        import os
        from unittest import mock
        self.client.force_login(self.agente)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "clave-de-prueba"}):
            r = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado]))
        self.assertEqual(r.context["redaccion"]["url"], reverse("mejorar_redaccion"))

    def test_mejorar_devuelve_la_propuesta_sin_guardar_nada(self):
        import os
        from unittest import mock
        self.client.force_login(self.agente)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "clave-de-prueba"}), \
                mock.patch("anthropic.Anthropic") as cliente:
            crear = cliente.return_value.beta.messages.create
            crear.return_value = self._respuesta("La cámara 3 no presentaba imagen. Se reemplazó el conector.")
            r = self.client.post(reverse("mejorar_redaccion"),
                                 {"texto": "camara 3 no daba imagen se cambio conector", "tipo": "diagnostico"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["texto"], "La cámara 3 no presentaba imagen. Se reemplazó el conector.")
        kwargs = crear.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["fallbacks"], "default")
        self.assertIn("camara 3 no daba imagen se cambio conector", kwargs["messages"][0]["content"])
        self.assertIn("diagnóstico", kwargs["messages"][0]["content"])

    def test_si_se_niega_o_no_hay_texto_avisa_sin_romper(self):
        import os
        from unittest import mock
        self.client.force_login(self.agente)
        url = reverse("mejorar_redaccion")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "clave-de-prueba"}), \
                mock.patch("anthropic.Anthropic") as cliente:
            cliente.return_value.beta.messages.create.return_value = self._respuesta("", motivo="refusal")
            r = self.client.post(url, {"texto": "algo", "tipo": "resultado"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("error", r.json())
            self.assertEqual(self.client.post(url, {"texto": "  ", "tipo": "resultado"}).status_code, 400)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertIn("no está activada", self.client.post(url, {"texto": "algo"}).json()["error"])

    def test_revisar_ortografia_gratuita_devuelve_errores_y_sugerencias(self):
        import io
        import json
        from unittest import mock
        self.client.force_login(self.agente)
        html = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado]))
        self.assertEqual(html.context["redaccion"]["ortografia"], reverse("revisar_ortografia"))  # siempre, sin clave
        lt = {"matches": [{"offset": 0, "length": 6, "shortMessage": "Error de ortografía",
                           "replacements": [{"value": "Cámara"}, {"value": "Cámaras"}, {"value": "Camada"},
                                            {"value": "Casara"}]}]}
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(lt).encode())) as abrir:
            r = self.client.post(reverse("revisar_ortografia"), {"texto": "camara 3 sin imagen"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["errores"], [{"inicio": 0, "largo": 6, "error": "camara", "mensaje": "Error de ortografía",
                                                "sugerencias": ["Cámara", "Cámaras", "Camada"]}])
        pedido = abrir.call_args.args[0]
        self.assertIn("languagetool.org", pedido.full_url)
        self.assertIn(b"language=es", pedido.data)

    def test_revisar_ortografia_sin_conexion_avisa(self):
        import urllib.error
        from unittest import mock
        self.client.force_login(self.agente)
        url = reverse("revisar_ortografia")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("sin red")):
            r = self.client.post(url, {"texto": "camara"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("conexión", r.json()["error"])
        self.assertEqual(self.client.post(url, {"texto": " "}).status_code, 400)

    def test_solo_el_personal_y_solo_por_post(self):
        url = reverse("mejorar_redaccion")
        self.client.force_login(self.cli_a)
        self.assertNotEqual(self.client.post(url, {"texto": "algo"}).status_code, 200)
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(url).status_code, 405)


class ActaSinProgramarTests(ActaBase):
    """El técnico deja constancia de un trabajo que no estaba en el cronograma (sin ticket)."""

    def test_tecnico_registra_acta_sin_ticket(self):
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        self.assertContains(self.client.get(reverse("actas_lista")), "Nueva acta")
        url = reverse("acta_sin_programar")
        self.assertContains(self.client.get(url, {"empresa": self.emp_a.pk}), "Principal")
        forma = self.client.get(url, {"empresa": self.emp_a.pk, "sede": self.sede_a.pk})
        self.assertContains(forma, "Trabajo no programado")
        self.assertNotIn("marcar_resuelto", forma.context["form"].fields)
        datos = self.datos_acta(copia_correo="otro@a.co")
        datos.pop("marcar_resuelto")
        mail.outbox.clear()
        r = self.client.post(f"{url}?empresa={self.emp_a.pk}&sede={self.sede_a.pk}", datos)
        self.assertEqual(r.status_code, 302)
        acta = ActaSoporte.objects.get(ticket=None, empresa=self.emp_a)
        self.assertEqual((acta.sede, acta.tecnico), (self.sede_a, self.agente))
        self.assertEqual(self.ticket.actas.count(), 0)
        # Copia al cliente y aviso al coordinador
        self.assertTrue(any("otro@a.co" in m.to for m in mail.outbox))
        self.assertTrue(any("Trabajo no programado" in m.subject and "soporte@janotec.test" in m.to for m in mail.outbox))
        self.assertContains(self.client.get(reverse("actas_lista")), "No programado")

    def test_sin_sede_especifica_y_solo_personal(self):
        url = reverse("acta_sin_programar")
        self.client.force_login(self.agente)
        self.assertContains(self.client.get(url, {"empresa": self.emp_b.pk, "sede": "0"}), "Sin sede específica")
        # Una sede de otro cliente no sirve
        self.assertNotContains(self.client.get(url, {"empresa": self.emp_b.pk, "sede": self.sede_a.pk}), "Trabajo no programado")
        self.client.force_login(self.cli_a)
        self.assertNotEqual(self.client.get(url).status_code, 200)


class BodegaYComercialTests(BaseTickets):
    """Una persona de bodega y logística puede atender también lo comercial."""

    def test_bodega_que_tambien_es_comercial(self):
        from . import notificaciones
        b = Usuario.objects.create_user("yuri", "yuri@janotec.test", "Clave-segura-1", rol="bodega", tambien_comercial=True)
        solo_bodega = Usuario.objects.create_user("bod", "bod@janotec.test", "Clave-segura-1", rol="bodega")
        self.assertTrue(b.es_bodega and b.es_comercial and b.ve_comercial)
        self.assertEqual(b.rol_corto, "Bodega y comercial")
        self.assertFalse(solo_bodega.es_comercial)
        self.client.force_login(b)
        html = self.client.get(reverse("inventario:panel")).content.decode()
        self.assertIn(reverse("comercial:bandeja"), html)
        self.assertEqual(self.client.get(reverse("comercial:bandeja")).status_code, 200)
        with self.settings(NOTIFICAR_COMERCIAL_EMAIL=""):
            self.assertEqual(notificaciones.correos_area_comercial(), ["yuri@janotec.test"])


class CalificacionDeUnoADiezTests(BaseTickets):
    """Al cerrar, el correo trae 10 estrellas; el cliente califica sin usuario y solo una vez."""

    def test_correo_de_cierre_con_estrellas_y_calificacion_publica(self):
        from . import notificaciones
        from .calificacion import enlace
        t = self.crear_ticket(contacto_email="portero@a.co", estado=E.VISITA_EJECUTADA)
        mail.outbox.clear()
        t.cambiar_estado(E.CERRADO, self.supervisor, "Listo")
        # El correo de cierre ya no sale solo; las estrellas siguen disponibles para un correo que las pida
        notificaciones.enviar_al_cliente("Califique", ["portero@a.co"], "Su solicitud", ["Gracias."], calificar=t)
        correo = next(m for m in mail.outbox if "portero@a.co" in m.to)
        html = correo.alternatives[0][0]
        self.assertIn("¿Cómo califica la atención que recibió?", html)
        self.assertEqual(html.count("/calificar/"), 20)  # 10 estrellas y su número
        self.assertNotIn("Consultar mi solicitud", html)
        # Al tocar la estrella la página registra la nota sola (la envía el navegador, no un antivirus que solo la abre)
        url = enlace(t, 7).replace("http://127.0.0.1:8000", "").split("?")[0]
        pagina = self.client.get(url, {"n": 7})
        self.assertContains(pagina, 'id="calificacion-automatica"')
        self.assertContains(pagina, 'name="calificacion" value="7"')
        t.refresh_from_db()
        self.assertIsNone(t.calificacion)
        self.client.post(url, {"calificacion": "7"})
        t.refresh_from_db()
        self.assertEqual(t.calificacion, 7)
        self.assertContains(self.client.get(url), "¿Quiere contarnos algo más?")
        self.client.post(url, {"comentario": "Muy puntuales"})
        t.refresh_from_db()
        self.assertEqual((t.calificacion, t.comentario_calificacion), (7, "Muy puntuales"))
        self.assertIsNotNone(t.fecha_calificacion)
        self.client.post(url, {"calificacion": "2"})  # una sola vez
        t.refresh_from_db()
        self.assertEqual(t.calificacion, 7)
        self.assertContains(self.client.get(url), "¡Gracias por calificarnos!")

    def test_enlace_alterado_o_ticket_abierto(self):
        from .calificacion import enlace
        t = self.crear_ticket(estado=E.EN_CRONOGRAMA)
        url = enlace(t).replace("http://127.0.0.1:8000", "")
        self.assertContains(self.client.get(url), "aún no está cerrada")
        self.client.post(url, {"calificacion": "9"})
        t.refresh_from_db()
        self.assertIsNone(t.calificacion)
        self.assertEqual(self.client.get(url[:-3] + "xyz/").status_code, 302)


class IndicadoresDeGestionTests(BaseTickets):
    def test_indicadores_del_periodo(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import indicadores
        from .calificacion import registrar
        a = self.crear_ticket()
        a.cambiar_estado(E.RECEPCIONADO, self.supervisor)
        a.cambiar_estado(E.VISITA_EJECUTADA, self.supervisor)
        a.cambiar_estado(E.CERRADO, self.supervisor)
        registrar(a, 9)
        b = self.crear_ticket()
        b.cambiar_estado(E.RECEPCIONADO, self.supervisor)
        hoy = timezone.localdate()
        r = indicadores.calcular(Ticket.objects.all(), hoy - timedelta(days=2), hoy)
        v = r["valores"]
        self.assertEqual((v["recibidos"], v["cerrados"], v["cierre"], v["satisfaccion"], v["satisfechos"]), (2, 1, 50.0, 9, 100.0))
        porc = {i.clave: i for i in r["indicadores"]}
        self.assertFalse(porc["cierre"].cumple)
        self.assertTrue(porc["satisfaccion"].cumple)
        self.client.force_login(self.supervisor)
        for q in ("", "?periodo=mes_anterior", "?periodo=anio", "?periodo=rango&desde=2026-01-01&hasta=2026-03-31"):
            self.assertContains(self.client.get(reverse("reportes") + q), "Indicadores del periodo")


class VincularActaHistoricaTests(BaseTickets):
    """Un acta que ya existía (SafetyTrack) se vincula a un ticket; si no coincide, se advierte y se pide confirmar."""

    def test_vincular_con_advertencia_y_desvincular(self):
        from django.utils import timezone

        from .models import ActaSoporte
        t = self.crear_ticket(estado=E.VISITA_EJECUTADA, sede=self.sede_a)
        otra = ActaSoporte.objects.create(numero=38402, fecha=timezone.now() - timezone.timedelta(days=60), empresa=self.emp_b,
                                          origen=ActaSoporte.Origen.SAFETYTRACK, tecnico_texto="Carlos")
        self.client.force_login(self.supervisor)
        r = self.client.post(reverse("vincular_acta", args=[t.radicado]), {"numero": "38402"}, follow=True)
        self.assertContains(r, "Revise antes de vincular el acta 38402")
        self.assertContains(r, "Empresa B")
        otra.refresh_from_db()
        self.assertIsNone(otra.ticket)  # sin confirmar no se vincula
        self.client.post(reverse("vincular_acta", args=[t.radicado]), {"numero": "38402", "confirmar": "1"})
        otra.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual((otra.ticket, t.estado), (t, E.VISITA_EJECUTADA))  # el estado no cambia
        self.assertTrue(t.seguimientos.filter(mensaje__contains="Se vinculó el acta N.º 38402").exists())
        # Otro ticket no puede tomar la misma acta
        t2 = self.crear_ticket()
        r = self.client.post(reverse("vincular_acta", args=[t2.radicado]), {"numero": "38402", "confirmar": "1"}, follow=True)
        self.assertContains(r, f"ya está vinculada a {t.radicado}")
        self.client.post(reverse("desvincular_acta", args=[t.radicado, 38402]))
        otra.refresh_from_db()
        self.assertIsNone(otra.ticket)
        # El cliente no puede vincular
        self.client.force_login(self.cli_a)
        self.assertNotEqual(self.client.post(reverse("vincular_acta", args=[t.radicado]), {"numero": "38402"}).status_code, 302)


class InformeBorradorTests(BaseTickets):
    """El borrador del informe sale del acta; coordinación lo envía a dirección, dirección lo aprueba (pasa a limpio)
    y es el que se entrega."""

    def setUp(self):
        super().setUp()
        self.direccion = Usuario.objects.create_user("miguel", "dir@x.co", "Clave-segura-1", rol="direccion",
                                                     first_name="Miguel", cargo="Director TI")
        from io import BytesIO

        from django.utils import timezone
        from PIL import Image

        from .models import ActaSoporte, EquipoActa, FotoEquipo
        self.ticket = self.crear_ticket(sede=self.sede_a, estado=E.INFORME, asignado_a=self.agente)
        acta = ActaSoporte.objects.create(numero=100900, fecha=timezone.now(), empresa=self.emp_a, sede=self.sede_a,
                                          ciudad="Bogotá D. C.", tecnico=self.agente, actividad="Desinstalación",
                                          sistema="ALARMA", diagnostico="Se desconectó la alimentación del panel.",
                                          resultado="solucionado", observaciones="Sellar la canalización que quedó abierta.",
                                          ticket=self.ticket, origen=ActaSoporte.Origen.PLATAFORMA)
        panel = EquipoActa.objects.create(acta=acta, clase=EquipoActa.Clase.RETIRADO, equipo="Panel de alarma",
                                          marca="Honeywell", referencia="VISTA 48LA", serial="166516", cantidad="1",
                                          propiedad=EquipoActa.Propiedad.CLIENTE)
        png = BytesIO()
        Image.new("RGB", (60, 40), "red").save(png, "PNG")
        FotoEquipo.objects.create(equipo=panel, archivo=SimpleUploadedFile("panel.png", png.getvalue()), nombre_original="panel.png")
        self.url = reverse("informe_borrador", args=[self.ticket.radicado])

    def test_texto_y_pdf_salen_del_acta(self):
        from . import informe
        self.client.force_login(self.agente)
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Generar borrador del informe")
        r = self.client.post(self.url, {"accion": "generar"})
        self.assertRedirects(r, self.url)
        b = informe.vigente(self.ticket)
        self.assertEqual(b.estado, "borrador")
        self.assertIn("desmonte del sistema de alarma en la sede Principal", b.introduccion)
        self.assertIn("panel de alarma", b.actividades)
        self.assertIn("Sellar la canalización", b.recomendaciones)
        self.assertIn("se le entregan", b.conclusiones)
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn("BORRADOR", r["Content-Disposition"])

    def test_el_informe_se_titula_segun_lo_que_se_hizo(self):
        from . import informe
        acta = self.ticket.actas.get()
        for actividad, titulo, frase in (("Mantenimiento preventivo", "Mantenimiento preventivo del sistema",
                                          "se realizó el mantenimiento preventivo"),
                                         ("Instalación", "Instalación del sistema", "se realizó la instalación"),
                                         ("Reubicación o traslado", "Reubicación del sistema", "se realizó la reubicación"),
                                         ("Soporte , Desinstalación", "Soporte técnico y desmonte del sistema",
                                          "se realizó el soporte técnico y el desmonte")):
            acta.actividad = actividad
            acta.save()
            textos = informe.textos(self.ticket)
            self.assertTrue(textos["asunto"].startswith(titulo), textos["asunto"])
            self.assertIn(frase, textos["introduccion"])
        acta.actividad = "Desinstalación"
        acta.save()
        self.assertIn("El registro del desmonte", informe.textos(self.ticket)["justificacion"])

    def test_informe_solo_informativo_no_lleva_tabla(self):
        from unittest import mock

        from . import informe
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar"})
        b = informe.vigente(self.ticket)
        with mock.patch.object(informe, "_tabla_equipos", wraps=informe._tabla_equipos) as tabla:
            self.assertTrue(informe.pdf(b)[1].startswith(b"%PDF"))
            tabla.assert_not_called()
            b.servicios = "1 Mantenimiento preventivo"
            informe.pdf(b)
            tabla.assert_called_once()

    def test_requerimientos_lleva_justificacion_e_imagen_del_catalogo(self):
        from io import BytesIO

        from PIL import Image

        from comercial.models import ItemCatalogo
        from . import informe
        png = BytesIO()
        Image.new("RGB", (40, 40), "blue").save(png, "PNG")
        ItemCatalogo.objects.create(descripcion="Cámara bala IP 4 MP", valor_unitario=100,
                                    imagen=SimpleUploadedFile("camara.png", png.getvalue()))
        ItemCatalogo.objects.create(descripcion="Cable UTP", valor_unitario=1)  # sin imagen
        imagenes = informe.imagenes_del_catalogo(["Cámara bala IP 4 MP DS-2CD1043G0E-I", "Cable UTP cat 6", "Sirena"])
        self.assertEqual(list(imagenes), ["Cámara bala IP 4 MP DS-2CD1043G0E-I"])
        textos = informe.textos_estudio(self.ticket)
        self.assertTrue(textos["asunto"].startswith("Requerimientos técnicos para el sistema"))
        self.assertIn("se definieron a partir de lo observado en sitio", textos["justificacion"])
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar", "tipo": "estudio"})
        b = informe.vigente(self.ticket)
        b.suministros = "3 Cámara bala IP 4 MP DS-2CD1043G0E-I | Hikvision\n80 m Cable UTP cat 6"
        b.save()
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertContains(self.client.get(self.url), "Informe de requerimientos técnicos")

    def test_tabla_de_requeridos_y_alcance_en_las_conclusiones(self):
        from . import informe
        from .models import InformeBorrador
        b = InformeBorrador(suministros="3 Cámara bala IP 4 MP | DS-2CD1043G0E-I | Hikvision\n80 m Cable UTP | Linkedpro\nNVR",
                            servicios="1 Instalación y configuración")
        self.assertEqual(b.filas_requeridas(), [
            {"cantidad": "3", "descripcion": "Cámara bala IP 4 MP", "referencia": "DS-2CD1043G0E-I", "marca": "Hikvision",
             "servicio": False},
            {"cantidad": "80 m", "descripcion": "Cable UTP", "referencia": "", "marca": "Linkedpro", "servicio": False},
            {"cantidad": "1", "descripcion": "NVR", "referencia": "", "marca": "", "servicio": False},
            {"cantidad": "1", "descripcion": "Instalación y configuración", "referencia": "", "marca": "", "servicio": True}])
        self.assertEqual(b.materiales_para_comercial().splitlines()[0], "3 Cámara bala IP 4 MP DS-2CD1043G0E-I Hikvision")
        # Una visita que propone algo dice en las conclusiones qué se propone y su alcance
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar"})
        visita = informe.vigente(self.ticket)
        self.assertNotIn(informe.ALCANCE, visita.conclusiones)
        visita.suministros = "2 Sensor de movimiento | NV5 | Paradox"
        visita.save()
        visita = informe.generar(self.ticket, self.agente, borrador=visita)
        self.assertIn(informe.ALCANCE, visita.conclusiones)
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertContains(self.client.get(self.url), "Tabla de equipos y materiales requeridos")

    def test_la_tabla_se_diligencia_fila_por_fila_con_mano_de_obra(self):
        from io import BytesIO

        from PIL import Image

        from . import informe
        png = BytesIO()
        Image.new("RGB", (40, 40), "green").save(png, "PNG")
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar"})
        datos = {"destinatario": "Empresa A", "asunto": "A", "introduccion": "I", "actividades": "A", "conclusiones": "C",
                 "accion": "guardar", "filas-total": "4",
                 "fila-0-tipo": "equipo", "fila-0-cantidad": "2", "fila-0-descripcion": "Sensor de movimiento",
                 "fila-0-referencia": "NV5", "fila-0-marca": "Paradox",
                 "fila-0-imagen": SimpleUploadedFile("sensor.png", png.getvalue()),
                 "fila-1-tipo": "equipo", "fila-1-cantidad": "", "fila-1-descripcion": "",  # vacía: no cuenta
                 "fila-2-tipo": "servicio", "fila-2-descripcion": "Instalación y configuración | con alturas",
                 "fila-3-tipo": "equipo", "fila-3-cantidad": "30 m", "fila-3-descripcion": "Cable UTP"}
        self.client.post(self.url, datos)
        b = informe.vigente(self.ticket)
        self.assertEqual(b.suministros, "2 Sensor de movimiento | NV5 | Paradox\n30 m Cable UTP |  |")
        self.assertEqual(b.servicios, "1 Instalación y configuración / con alturas")
        self.assertEqual([f["descripcion"] for f in b.filas_requeridas() if f["servicio"]], ["Instalación y configuración / con alturas"])
        self.assertEqual(list(informe.imagenes_de_la_tabla(b, ["Sensor de movimiento", "Cable UTP"])), ["Sensor de movimiento"])
        pagina = self.client.get(self.url)
        self.assertContains(pagina, 'value="Sensor de movimiento"')
        self.assertContains(pagina, "Imagen cargada")
        self.assertContains(pagina, "Mano de obra")
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_coordinacion_envia_y_solo_direccion_aprueba(self):
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar"})
        datos = {"destinatario": "Empresa A", "asunto": "Desmonte", "introduccion": "Texto corregido", "actividades": "Una",
                 "justificacion": "", "conclusiones": "Listo", "recomendaciones": ""}
        self.client.post(self.url, {**datos, "accion": "guardar"})
        b = self.ticket.informes_borrador.get()
        self.assertEqual(b.introduccion, "Texto corregido")
        self.assertEqual(self.client.post(self.url, {**datos, "accion": "aprobar"}).status_code, 403)
        # Sin aprobar no se entrega sin PDF adjunto
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]), {"nota": "x", "destino": "cliente"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.INFORME)

        # Coordinación no aprueba: lo envía a dirección, y mientras está en revisión nadie más lo corrige
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.post(self.url, {**datos, "accion": "aprobar"}).status_code, 403)
        mail.outbox.clear()
        self.client.post(self.url, {**datos, "accion": "enviar"})
        b.refresh_from_db()
        self.assertTrue(b.en_revision)
        self.assertEqual([m.to for m in mail.outbox], [["dir@x.co"]])
        self.assertIn("/informe/borrador.pdf", mail.outbox[0].body)
        # Dirección revisa el informe en limpio, sin la marca BORRADOR
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertNotIn("BORRADOR", r["Content-Disposition"])
        self.client.post(self.url, {**datos, "introduccion": "Cambio de coordinación", "accion": "guardar"})
        b.refresh_from_db()
        self.assertEqual(b.introduccion, "Texto corregido")
        # Dirección lo devuelve con observaciones: vuelve a borrador y le llega a coordinación
        self.client.force_login(self.direccion)
        self.assertContains(self.client.get(self.url), "Devolver a coordinación")
        mail.outbox.clear()
        self.client.post(self.url, {**datos, "accion": "devolver", "motivo": "Falta la referencia del panel"})
        b.refresh_from_db()
        self.assertEqual(b.estado, "borrador")
        self.assertIn("sup@x.co", mail.outbox[0].to)
        self.assertIn("Falta la referencia del panel", mail.outbox[0].body)
        self.assertTrue(self.ticket.seguimientos.filter(mensaje__contains="Falta la referencia del panel").exists())
        # Corregido y enviado de nuevo, dirección lo aprueba
        self.client.force_login(self.supervisor)
        self.client.post(self.url, {**datos, "accion": "enviar"})
        self.client.force_login(self.direccion)
        self.assertRedirects(self.client.post(self.url, {**datos, "accion": "aprobar"}), self.ticket.get_absolute_url(),
                             fetch_redirect_response=False)
        b.refresh_from_db()
        self.assertTrue(b.aprobado and b.archivo and b.aprobado_por == self.direccion)
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertTrue(b"".join(r.streaming_content).startswith(b"%PDF"))
        # Aprobado ya no se edita hasta que coordinación lo devuelva a borrador
        self.client.post(self.url, {**datos, "introduccion": "Otro", "accion": "guardar"})
        b.refresh_from_db()
        self.assertEqual(b.introduccion, "Texto corregido")

        # Al entregar se adjunta solo el informe aprobado
        self.client.force_login(self.agente)
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]), {"nota": "", "destino": "cliente"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.VISITA_EJECUTADA)
        adj = Adjunto.objects.get(ticket=self.ticket, es_informe=True)
        self.assertEqual(adj.nombre_original, f"Informe_tecnico_{self.ticket.radicado}.pdf")

    def test_reabrir_y_sin_acta_no_hay_borrador(self):
        self.client.force_login(self.direccion)
        self.client.post(self.url, {"accion": "generar"})
        datos = {"destinatario": "Empresa A", "asunto": "A", "introduccion": "B", "actividades": "C", "conclusiones": "D"}
        self.client.post(self.url, {**datos, "accion": "aprobar"})
        self.client.post(self.url, {"accion": "reabrir"})
        self.assertEqual(self.ticket.informes_borrador.get().estado, "borrador")
        otro = self.crear_ticket(estado=E.INFORME)
        self.client.post(reverse("informe_borrador", args=[otro.radicado]), {"accion": "generar"})
        self.assertFalse(otro.informes_borrador.exists())
        # Un cliente no ve el borrador
        self.client.force_login(self.cli_a)
        self.assertIn(self.client.get(self.url).status_code, (302, 403, 404))

    def test_informe_de_estudio_con_zonas_y_tabla_que_pasa_a_comercial(self):
        from io import BytesIO

        from PIL import Image
        self.ticket.tipo = Ticket.Tipo.REQUERIMIENTO
        self.ticket.save()
        self.client.force_login(self.agente)
        self.client.post(self.url, {"accion": "generar", "tipo": "estudio"})
        b = self.ticket.informes_borrador.get()
        self.assertTrue(b.es_estudio)
        self.assertIn("Vida útil", b.garantia)
        self.assertEqual(b.zonas.count(), 1)  # el equipo del acta con foto se vuelve zona
        png = BytesIO()
        Image.new("RGB", (30, 30), "blue").save(png, "PNG")
        zona = b.zonas.get()
        datos = {"destinatario": "Empresa A", "asunto": "Estudio CCTV", "introduccion": "I", "actividades": "A",
                 "conclusiones": "C", "recomendaciones": "", "garantia": b.garantia, "servicios": "1 Instalación y configuración",
                 "suministros": "3 Cámara IP 4 MP bala | Hikvision\n120 m Cable UTP cat 6\nNVR 8 canales",
                 f"zona-{zona.pk}-nombre": "Parqueadero", f"zona-{zona.pk}-descripcion": "1 cámara en el poste",
                 "zona-nueva-nombre": "Báscula", "zona-nueva-fotos": SimpleUploadedFile("b.png", png.getvalue()), "accion": "guardar"}
        self.client.post(self.url, datos)
        self.assertEqual(list(b.zonas.values_list("nombre", flat=True)), ["Parqueadero", "Báscula"])
        foto = b.zonas.get(nombre="Báscula").fotos.get()
        self.assertEqual(self.client.get(reverse("foto_zona", args=[foto.pk])).status_code, 200)
        b.refresh_from_db()
        self.assertEqual(b.filas_suministros(), [("3", "Cámara IP 4 MP bala", "Hikvision"), ("120 m", "Cable UTP cat 6", ""),
                                                 ("1", "NVR 8 canales", "")])
        r = self.client.get(reverse("informe_borrador_pdf", args=[self.ticket.radicado]))
        self.assertTrue(r.content.startswith(b"%PDF"))

        self.client.force_login(self.direccion)
        datos.pop("zona-nueva-fotos"), datos.pop("zona-nueva-nombre")
        self.client.post(self.url, {**datos, "accion": "aprobar"})
        b.refresh_from_db()
        self.assertTrue(b.aprobado)
        detalle = self.client.get(self.ticket.get_absolute_url())
        self.assertContains(detalle, "la tabla del informe técnico</b>, tal cual")
        self.assertContains(detalle, "<td>Cámara IP 4 MP bala</td>")
        self.assertNotContains(detalle, 'name="filas-total"')  # no se pide escribirla otra vez
        # Pasa a comercial sin escribir de nuevo los equipos: van solos los del estudio
        self.client.post(reverse("entregar_informe", args=[self.ticket.radicado]), {"destino": "comercial"})
        informe = self.ticket.ficha.informe
        self.assertIn("120 m Cable UTP cat 6", informe.materiales)
        self.assertIn("Instalación y configuración", informe.servicios)

    def test_aprobar_estudio_exige_equipos_o_servicios(self):
        self.client.force_login(self.direccion)
        self.client.post(self.url, {"accion": "generar", "tipo": "estudio"})
        b = self.ticket.informes_borrador.get()
        self.client.post(self.url, {"destinatario": "E", "asunto": "A", "introduccion": "I", "actividades": "A",
                                    "conclusiones": "C", "accion": "aprobar"})
        b.refresh_from_db()
        self.assertFalse(b.aprobado)


class PasoAPasoTecnicaTests(BaseTickets):
    """El área técnica avanza un paso a la vez: lo siguiente se habilita cuando lo anterior está completo."""

    def test_acta_se_habilita_con_tecnico_y_visita_programada_u_omitida(self):
        from . import pasos
        t = self.crear_ticket(sede=self.sede_a)
        self.assertEqual(pasos.calcular(t)["actual"], "recepcion")
        t.estado, t.asignado_a = E.RECEPCIONADO, self.agente
        t.save()
        self.assertEqual(pasos.calcular(t)["actual"], "programacion")
        self.client.force_login(self.agente)
        r = self.client.get(reverse("acta_nueva", args=[t.radicado]), follow=True)
        self.assertContains(r, "Paso a paso: el acta se habilita")
        self.assertContains(self.client.get(t.get_absolute_url()), "El acta se habilita cuando la visita esté programada")
        # El técnico no puede omitir pasos; coordinación sí, con motivo
        self.assertEqual(self.client.post(reverse("omitir_paso", args=[t.radicado]),
                                          {"paso": "programacion", "motivo": "Ya estaba en la sede"}).status_code, 403)
        self.client.force_login(self.supervisor)
        self.client.post(reverse("omitir_paso", args=[t.radicado]), {"paso": "programacion", "motivo": "corto"})
        self.assertEqual(pasos.calcular(t)["actual"], "programacion")
        self.client.post(reverse("omitir_paso", args=[t.radicado]),
                         {"paso": "programacion", "motivo": "El técnico ya estaba en la sede y lo atendió"})
        info = pasos.calcular(t)
        self.assertEqual(info["actual"], "visita")
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje__contains="Omitió el paso «Programar la visita»").exists())
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("acta_nueva", args=[t.radicado])).status_code, 200)
        self.assertContains(self.client.get(t.get_absolute_url()), "Ahora: Visita y acta")

    def test_visita_ejecutada_exige_acta_y_estados_validos(self):
        t = self.crear_ticket(sede=self.sede_a, estado=E.EN_CRONOGRAMA, asignado_a=self.agente)
        self.client.force_login(self.supervisor)
        r = self.client.get(t.get_absolute_url())
        opciones = r.context["form_gestion"].fields["estado"].choices
        self.assertNotIn("cerrado", [v for v, _ in opciones])  # de «En cronograma» no se cierra directo
        self.client.post(reverse("gestionar", args=[t.radicado]),
                         {"estado": "visita_ejecutada", "visibilidad": "interno", "mensaje": "listo",
                          "prioridad": t.prioridad, "asignado_a": self.agente.pk})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.EN_CRONOGRAMA)

    def test_ticket_que_ya_avanzo_no_queda_atascado(self):
        from . import pasos
        t = self.crear_ticket(estado=E.INFORME, tipo=Ticket.Tipo.REQUERIMIENTO)  # sin técnico asignado
        self.assertEqual(pasos.calcular(t)["actual"], "informe")
        t.estado = E.EN_INVENTARIO
        t.save()
        self.assertEqual(pasos.calcular(t)["otra_area"], "bodega")


class PdfOriginalActasTests(ActaBase):
    """Cada acta puede tener su PDF original, tomado del comprimido de clientes por número de acta."""

    def test_vincula_por_numero_y_deja_por_fuera_las_dudosas(self):
        import io
        import os
        import zipfile

        from django.core.management import call_command
        from django.utils import timezone

        from .models import ActaSoporte
        a1 = ActaSoporte.objects.create(numero=30268, fecha=timezone.now(), empresa=self.emp_a, sede_texto="Serramonte")
        a2 = ActaSoporte.objects.create(numero=30269, fecha=timezone.now(), empresa=self.emp_a, sede_texto="Principal")
        carpeta = tempfile.mkdtemp()  # propia: MEDIA_TMP lo borra al terminar otra clase de pruebas
        self.addCleanup(shutil.rmtree, carpeta, ignore_errors=True)
        ruta = os.path.join(carpeta, "clientes.zip")
        with zipfile.ZipFile(ruta, "w") as z:
            z.writestr("3. CLIENTES/05. Acta de Soporte/Enero/30268 EMPRESA A SERRAMONTE.pdf", b"%PDF-1.4 uno")
            z.writestr("3. CLIENTES/02. Privado/OTRA/ACTA SOPORTE-30269 - OTRO CLIENTE.pdf", b"%PDF-1.4 dos")
            z.writestr("3. CLIENTES/02. Privado/X/ENTREGA EQUIPOS-30268.pdf", b"%PDF no")
        salida = io.StringIO()
        call_command("vincular_pdf_actas", ruta, stdout=salida)  # solo revisión
        a1.refresh_from_db()
        self.assertFalse(a1.pdf_original)
        self.assertIn("Se vinculan: 1", salida.getvalue())
        self.assertIn("30269", salida.getvalue())  # dudosa: el archivo es de otro cliente
        call_command("vincular_pdf_actas", ruta, "--aplicar", stdout=io.StringIO())
        a1.refresh_from_db(), a2.refresh_from_db()
        self.assertTrue(a1.pdf_original)
        self.assertFalse(a2.pdf_original)
        self.client.force_login(self.supervisor)
        r = self.client.get(reverse("archivo_acta", args=["original", a1.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 uno")
        self.assertContains(self.client.get(a1.get_absolute_url()), "Ver acta original (PDF)")
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["original", a1.pk])).status_code, 404)


class EvidenciasActaTests(ActaBase):
    """El acta lleva evidencias del trabajo en sí (durante y resultado), no solo fotos de cada equipo."""

    def setUp(self):
        super().setUp()
        self.ticket = self.crear_ticket(asignado_a=self.agente, estado=E.AUTORIZADO, sede=self.sede_a,
                                        contacto_nombre="Portero", contacto_email="portero@a.co")

    def archivo(self, nombre="resultado.jpg"):
        return SimpleUploadedFile(nombre, bytes([255, 216, 255]) + b"0" * 50, content_type="image/jpeg")

    def test_evidencias_al_diligenciar_y_despues(self):
        from .models import ActaSoporte
        self.client.force_login(self.agente)
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]),
                             self.datos_acta(evidencias_resultado=[self.archivo("virus.exe")]))
        self.assertFalse(ActaSoporte.objects.exists())  # solo fotos, video mp4 o PDF
        self.assertContains(r, "virus.exe")
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(
            evidencias_proceso=[self.archivo("antes.jpg")], evidencias_resultado=[self.archivo(), self.archivo("informe.pdf")]))
        acta = ActaSoporte.objects.get()
        self.assertEqual(sorted(acta.evidencias.values_list("etapa", flat=True)), ["proceso", "resultado", "resultado"])
        ev = acta.evidencias.filter(etapa="resultado").first()
        r = self.client.get(acta.get_absolute_url())
        self.assertContains(r, "Evidencias del trabajo")
        self.assertContains(r, reverse("archivo_acta", args=["evidencia", ev.pk]))
        # Después, el técnico del acta agrega lo que le faltó; otro técnico no puede
        self.client.post(reverse("acta_evidencias", args=[acta.numero]), {"etapa": "resultado", "archivos": [self.archivo("monitor.png")]})
        self.assertEqual(acta.evidencias.count(), 4)
        self.assertTrue(self.ticket.seguimientos.filter(mensaje__contains="evidencia(s) al acta").exists())
        self.client.force_login(self.agente2)
        self.assertIn(self.client.post(reverse("acta_evidencias", args=[acta.numero]),
                                       {"etapa": "resultado", "archivos": [self.archivo()]}).status_code, (403, 404))
        self.assertEqual(acta.evidencias.count(), 4)
        # El cliente las ve; otra empresa no
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["evidencia", ev.pk])).status_code, 200)
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(reverse("archivo_acta", args=["evidencia", ev.pk])).status_code, 404)


class TambienTecnicoTests(TestCase):
    """Dirección que también va a visitas: sale en las listas de técnicos sin perder lo que ya podía hacer."""

    def setUp(self):
        self.director = Usuario.objects.create_user("director", "d@janotec.test", "Clave-segura-1", rol="direccion", first_name="Dir")
        self.otro = Usuario.objects.create_user("otro", "o@janotec.test", "Clave-segura-1", rol="direccion", first_name="Otro")
        self.coord = Usuario.objects.create_user("coord", "c@janotec.test", "Clave-segura-1", rol="supervisor", first_name="Coord")
        self.director.tambien_tecnico = True
        self.director.save()

    def test_sale_en_las_listas_de_tecnicos_y_conserva_direccion(self):
        from django.utils import timezone

        from cronograma.forms import tecnicos_queryset
        from inventario.forms import tecnicos
        from .indicadores import por_responsable
        for lista in (tecnicos_queryset(), tecnicos()):
            self.assertIn(self.director, lista)
            self.assertIn(self.coord, lista)
            self.assertNotIn(self.otro, lista)
        hoy = timezone.localdate()
        empresa = Empresa.objects.create(codigo="acme", razon_social="ACME")
        for numero, u in enumerate((self.director, self.coord), 1):
            ActaSoporte.objects.create(numero=numero, empresa=empresa, tecnico=u, fecha=timezone.now())
        nombres = [f["nombre"] for f in por_responsable(Ticket.objects.all(), hoy, hoy)]
        self.assertIn("Dir", nombres)
        self.assertNotIn("Coord", nombres)
        self.assertTrue(self.director.es_direccion and self.director.gestiona_inventario and self.director.is_staff)
        self.assertEqual(self.director.rol_corto, "Dirección y técnico")
        self.assertEqual(self.otro.rol_corto, "Dirección")

    def test_otro_rol_con_la_marca_ve_lo_de_un_tecnico(self):
        bodega = Usuario.objects.create_user("b", "b@janotec.test", "Clave-segura-1", rol="bodega", tambien_tecnico=True)
        self.assertTrue(bodega.es_personal and bodega.ve_inventario)
        self.assertFalse(bodega.es_supervisor)

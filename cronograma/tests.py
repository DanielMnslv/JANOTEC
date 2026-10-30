import re
import shutil
import tempfile
from datetime import date, datetime, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.models import Categoria, RangoNoDisponible, Ticket

from . import herramientas, servicios
from .models import Actividad, DocumentoPersonal, EnlaceCronograma

MEDIA_TMP = tempfile.mkdtemp()
E = Ticket.Estado
A = Actividad.Autorizacion


def proximo(dia_semana):
    """La próxima fecha (desde mañana) que cae en ese día de la semana (0 = lunes)."""
    hoy = timezone.localdate()
    return hoy + timedelta(days=(dia_semana - hoy.weekday()) % 7 or 7)


@override_settings(MEDIA_ROOT=MEDIA_TMP, NOTIFICAR_SOPORTE_EMAIL="coordinador@janotec.test",
                   NOTIFICAR_SST_EMAIL="sst@janotec.test", SITIO_URL="http://testserver",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseCronograma(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        self.cat, _ = Categoria.objects.get_or_create(nombre="CCTV / Videovigilancia")
        self.emp = Empresa.objects.create(razon_social="Grupo del Llano", nit="1")
        self.sede = Sede.objects.create(empresa=self.emp, nombre="Manare")
        self.cli = Usuario.objects.create_user("cli", "cli@llano.co", "Clave-segura-1", rol="cliente", empresa=self.emp)
        self.coord = Usuario.objects.create_user("coord", "coord@janotec.test", "Clave-segura-1", rol="supervisor")
        self.tec = Usuario.objects.create_user("freyder", "freyder@janotec.test", "Clave-segura-1", rol="agente",
                                               first_name="Freyder", last_name="Gordillo")
        self.tec2 = Usuario.objects.create_user("alberto", "alberto@janotec.test", "Clave-segura-1", rol="agente",
                                                first_name="Alberto")
        # El cliente no puede los viernes en la tarde
        self.ticket = Ticket.objects.create(
            empresa=self.emp, sede=self.sede, solicitante=self.cli, categoria=self.cat, tipo="mantenimiento", impacto="media",
            asunto="Mantenimiento preventivo", descripcion="Mantenimiento preventivo zona 013",
            contacto_email="fernando@llano.co; admin@llano.co", estado=E.RECEPCIONADO, no_disponible={"viernes": "tarde"},
        )

    def programar(self, fecha, inicio="08:00", fin="12:00", ticket=None, **extra):
        ticket = ticket or self.ticket
        datos = {"ticket": ticket.pk, "fecha": fecha.isoformat(), "hora_inicio": inicio, "hora_fin": fin,
                 "cliente": ticket.empresa.razon_social, "sede": "Manare", "actividad": ticket.descripcion,
                 "tipo": "mantenimiento", "linea": "CCTV / videovigilancia", "encargados": [self.tec.pk],
                 "notificar_a": ticket.contacto_email, "sst_duracion": "medio día"}
        datos.update(extra)
        self.client.force_login(self.coord)
        return self.client.post(reverse("cronograma:crear"), datos, follow=True)

    def documento(self, tecnico, tipo, vence=None):
        archivo = SimpleUploadedFile(f"{tipo}.pdf", b"%PDF-1.4 " + tipo.encode(), content_type="application/pdf")
        return DocumentoPersonal.objects.create(usuario=tecnico, tipo=tipo, archivo=archivo, nombre_original=f"{tipo}.pdf",
                                                vigente_hasta=vence or timezone.localdate() + timedelta(days=30))

    def con_documentos(self, *tecnicos, alturas=False):
        for t in tecnicos:
            self.documento(t, "cedula")
            self.documento(t, "seguridad_social")
            if alturas:
                self.documento(t, "alturas")


class ProgramarTests(BaseCronograma):
    def test_arrastrar_ticket_lo_programa_y_avisa(self):
        self.client.force_login(self.coord)
        html = self.client.get(reverse("cronograma:semana")).content.decode()
        self.assertIn(self.ticket.radicado, html)  # está en «Tickets por programar»
        self.assertIn("fernando@llano.co ; admin@llano.co", html)  # «notificar a» se llena solo
        mail.outbox.clear()
        martes = proximo(1)
        self.programar(martes, acompanantes=[self.tec2.pk])
        a = Actividad.objects.get()
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.asignado_a), (E.EN_CRONOGRAMA, self.tec))
        self.assertEqual(timezone.localtime(self.ticket.visita_programada).date(), martes)
        self.assertEqual((a.choque, a.en_rojo, a.autorizacion), ("", False, A.SIN_ENVIAR))
        self.assertEqual([u.username for u in a.tecnicos()], ["freyder", "alberto"])
        self.assertFalse(any("fernando@llano.co" in m.to for m in mail.outbox))  # programar no le escribe al cliente
        # Ya no aparece por programar; sí en el día
        html = self.client.get(reverse("cronograma:semana") + f"?d={martes:%Y-%m-%d}").content.decode()
        self.assertIn("Grupo del Llano", html)
        self.assertNotIn('data-ticket="%s"' % self.ticket.pk, html)

    def test_cruce_con_la_disponibilidad_alerta_y_queda_en_rojo(self):
        viernes = proximo(4)
        r = self.programar(viernes, "14:00", "16:00")
        self.assertContains(r, "no puede atender en la tarde")
        a = Actividad.objects.get()
        self.assertTrue(a.en_rojo)
        self.assertTrue(self.ticket.seguimientos.filter(interno=True, mensaje__contains="restricción").exists())
        # Una mañana que termina después de las 12 también cruza con «no en la tarde»
        otro = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="x", descripcion="x",
                                     estado=E.RECEPCIONADO, no_disponible={"viernes": "tarde"}, contacto_email="a@llano.co")
        self.programar(viernes, "10:00", "13:00", ticket=otro)
        self.assertTrue(Actividad.objects.get(ticket=otro).choque)

    def test_rangos_de_fecha_y_hora_sin_disponibilidad(self):
        martes = proximo(1)
        desde = timezone.make_aware(datetime.combine(martes, datetime.min.time()).replace(hour=9))
        RangoNoDisponible.objects.create(ticket=self.ticket, desde=desde, hasta=desde + timedelta(hours=2))
        self.programar(martes, "10:30", "12:00")
        self.assertIn("no puede atender", Actividad.objects.get().choque)
        self.programar(martes + timedelta(days=7), "10:30", "12:00", ticket=Ticket.objects.create(
            empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="y", descripcion="y",
            estado=E.RECEPCIONADO, contacto_email="a@llano.co"))
        self.assertEqual(Actividad.objects.filter(choque="").count(), 1)

    def test_solo_tickets_recepcionados_y_sin_correo_queda_en_rojo(self):
        generado = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="g",
                                         descripcion="g", estado=E.GENERADO)
        r = self.programar(proximo(1), ticket=generado)
        self.assertContains(r, "solo se programan")
        self.assertFalse(Actividad.objects.exists())
        self.programar(proximo(1), notificar_a="")
        a = Actividad.objects.get()
        self.assertEqual(a.motivo_rojo, "Sin correo para notificar al cliente.")

    def test_retirar_devuelve_el_ticket_a_recepcionado(self):
        self.programar(proximo(1))
        a = Actividad.objects.get()
        self.client.post(reverse("cronograma:eliminar", args=[a.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.visita_programada), (E.RECEPCIONADO, None))

    def test_tecnico_ve_solo_sus_visitas_y_no_programa(self):
        self.programar(proximo(1))
        self.client.force_login(self.tec2)
        r = self.client.get(reverse("cronograma:semana") + f"?d={proximo(1):%Y-%m-%d}")
        self.assertNotContains(r, "Grupo del Llano")
        self.assertEqual(self.client.post(reverse("cronograma:crear"), {}).status_code, 403)
        self.client.force_login(self.tec)
        self.assertContains(self.client.get(reverse("cronograma:semana") + f"?d={proximo(1):%Y-%m-%d}"), "Grupo del Llano")
        # Puede diligenciar el acta de su visita
        self.assertEqual(self.client.get(reverse("acta_nueva", args=[self.ticket.radicado])).status_code, 200)
        self.client.force_login(self.cli)
        self.assertEqual(self.client.get(reverse("cronograma:semana")).status_code, 403)


class AutorizacionTests(BaseCronograma):
    def enviar(self, actividad, **extra):
        self.client.force_login(self.coord)
        return self.client.post(reverse("cronograma:enviar_autorizacion", args=[actividad.pk]), extra, follow=True)

    def enlace(self, correo):
        return re.search(r"http://testserver(/cronograma/autorizacion/[^/\s]+/)", correo.body).group(1)

    def test_envia_correo_con_documentos_y_botones(self):
        self.programar(proximo(1), acompanantes=[self.tec2.pk])
        a = Actividad.objects.get()
        # Sin documentos no se envía (salvo que se confirme)
        r = self.enviar(a)
        self.assertContains(r, "falta documento de identidad")
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, A.SIN_ENVIAR)
        self.con_documentos(self.tec, self.tec2)
        mail.outbox.clear()
        self.enviar(a)
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, A.ENVIADA)
        correo = mail.outbox[0]
        self.assertEqual(set(correo.to), {"fernando@llano.co", "admin@llano.co"})
        self.assertEqual(len(correo.attachments), 4)  # cédula y seguridad social de cada técnico
        html = correo.alternatives[0][0]
        self.assertIn("Aceptar", html)
        self.assertIn("Rechazar", html)
        self.assertIn("Freyder Gordillo", html)
        self.assertTrue(self.ticket.seguimientos.filter(mensaje__contains="autorización de ingreso").exists())

    def test_trabajo_en_alturas_exige_y_adjunta_certificados(self):
        self.programar(proximo(1), trabajo_alturas="on")
        a = Actividad.objects.get()
        self.con_documentos(self.tec)
        self.assertContains(self.enviar(a), "falta certificado de trabajo seguro en alturas")
        self.documento(self.tec, "alturas")
        mail.outbox.clear()
        self.enviar(a)
        self.assertEqual(len(mail.outbox[0].attachments), 3)

    def test_documento_vencido_avisa_pero_se_puede_enviar_de_todas_formas(self):
        self.programar(proximo(1))
        a = Actividad.objects.get()
        self.documento(self.tec, "cedula")
        self.documento(self.tec, "seguridad_social", vence=timezone.localdate() - timedelta(days=1))
        self.assertContains(self.enviar(a), "vencido")
        self.enviar(a, forzar="1")
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, A.ENVIADA)

    def test_cliente_acepta_desde_el_correo_y_se_avisa_a_sst(self):
        self.programar(proximo(1), sst="on", sst_persona="Javier Jaramillo", sst_trabajo="trabajos en alturas")
        a = Actividad.objects.get()
        self.con_documentos(self.tec)
        self.enviar(a)
        url = self.enlace(mail.outbox[-1])
        self.client.logout()
        # Abrir el enlace (como lo haría un antivirus) no responde por el cliente
        r = self.client.get(url + "?r=aceptar")
        self.assertContains(r, "Aceptar la visita")
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, A.ENVIADA)
        mail.outbox.clear()
        self.client.post(url, {"accion": "aceptar"})
        a.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual((a.autorizacion, self.ticket.estado), (A.ACEPTADA, E.AUTORIZADO))
        sst = next(m for m in mail.outbox if "sst@janotec.test" in m.to)
        self.assertIn("Javier Jaramillo", sst.body)
        self.assertTrue(any("coordinador@janotec.test" in m.to and "ACEPTÓ" in m.subject for m in mail.outbox))

    def test_sin_sst_no_se_avisa_y_rechazo_marca_en_rojo(self):
        self.programar(proximo(1))
        a = Actividad.objects.get()
        self.con_documentos(self.tec)
        self.enviar(a)
        url = self.enlace(mail.outbox[-1])
        self.client.logout()
        mail.outbox.clear()
        self.client.post(url, {"accion": "rechazar", "motivo": "Ese día hay inventario"})
        a.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual((a.autorizacion, a.motivo_rechazo, a.en_rojo), (A.RECHAZADA, "Ese día hay inventario", True))
        self.assertEqual(self.ticket.estado, E.EN_CRONOGRAMA)
        self.assertFalse(any("sst@janotec.test" in m.to for m in mail.outbox))
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(reverse("cronograma:semana") + f"?d={a.fecha:%Y-%m-%d}"), "Ese día hay inventario")

    def test_reprogramar_invalida_el_enlace_anterior(self):
        self.programar(proximo(1))
        a = Actividad.objects.get()
        self.con_documentos(self.tec)
        self.enviar(a)
        url = self.enlace(mail.outbox[-1])
        self.client.post(reverse("cronograma:mover", args=[a.pk]), {"fecha": proximo(2).isoformat()},
                         content_type="application/json")
        a.refresh_from_db()
        self.assertEqual((a.fecha, a.autorizacion, a.version_enlace), (proximo(2), A.SIN_ENVIAR, 2))
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.post(url, {"accion": "aceptar"})
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, A.SIN_ENVIAR)
        self.assertEqual(self.client.get("/cronograma/autorizacion/token-falso/").status_code, 404)


class DocumentosTests(BaseCronograma):
    def subir(self, usuario, tecnico, tipo, vence="2030-01-31"):
        self.client.force_login(usuario)
        archivo = SimpleUploadedFile("doc.pdf", b"%PDF-1.4", content_type="application/pdf")
        url = reverse("cronograma:documentos_tecnico", args=[tecnico.pk])
        return self.client.post(url, {"tipo": tipo, "archivo": archivo, "vigente_hasta": vence})

    def test_actualizar_deja_historial_y_solo_uno_actual(self):
        self.subir(self.coord, self.tec, "seguridad_social", "2030-01-31")
        self.subir(self.coord, self.tec, "seguridad_social", "2030-02-28")
        docs = DocumentoPersonal.objects.filter(usuario=self.tec, tipo="seguridad_social")
        self.assertEqual(docs.count(), 2)
        self.assertEqual(docs.get(actual=True).vigente_hasta, date(2030, 2, 28))
        # La seguridad social exige fecha de vigencia
        self.subir(self.coord, self.tec, "seguridad_social", "")
        self.assertEqual(docs.count(), 2)
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(reverse("cronograma:documentos")), "Hasta 28/02/2030")

    def test_el_tecnico_actualiza_los_suyos_pero_no_ve_los_de_otro(self):
        self.subir(self.tec, self.tec, "cedula")
        doc = DocumentoPersonal.objects.get(usuario=self.tec)
        self.assertEqual(doc.subido_por, self.tec)
        self.client.force_login(self.tec)
        self.assertEqual(self.client.get(reverse("cronograma:documento", args=[doc.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("cronograma:documentos")).status_code, 403)
        self.client.force_login(self.tec2)
        self.assertEqual(self.client.get(reverse("cronograma:documento", args=[doc.pk])).status_code, 404)
        self.assertEqual(self.subir(self.tec2, self.tec, "cedula").status_code, 403)
        self.client.force_login(self.cli)
        self.assertEqual(self.client.get(reverse("cronograma:documento", args=[doc.pk])).status_code, 403)


@override_settings(NOTIFICAR_ADMINISTRATIVA_EMAIL="administrativa@janotec.test")
class SemanalTests(BaseCronograma):
    def setUp(self):
        super().setUp()
        self.lunes = Actividad.lunes_de(proximo(0))
        self.otro = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="s",
                                          descripcion="Revisar cámara 1", tipo="soporte", estado=E.RECEPCIONADO,
                                          contacto_email="a@llano.co")
        self.programar(self.lunes, insumos="3 batería 12V 7.5 A, 20 m UTP")
        self.programar(self.lunes + timedelta(days=1), ticket=self.otro, sede="Acacías", tipo="soporte",
                       insumos="1 batería 12v 7.5 a; 1 PIR sensor", encargados=[self.tec2.pk])

    def test_insumos_consolidados_e_indicadores(self):
        from . import semanal
        filas = {f["descripcion"].lower(): f for f in semanal.consolidar_insumos(semanal.actividades_de(self.lunes))}
        self.assertEqual(filas["batería 12v 7.5 a"]["cantidad"], 4)
        self.assertEqual((filas["utp"]["cantidad"], filas["utp"]["unidad"]), (20, "m"))
        self.assertEqual(len(filas["batería 12v 7.5 a"]["sedes"]), 2)
        ind = semanal.indicadores(semanal.actividades_de(self.lunes))
        self.assertEqual((ind["total"], ind["cliente_top"]["etiqueta"]), (2, "Grupo del Llano"))
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(reverse("cronograma:insumos") + f"?d={self.lunes:%Y-%m-%d}"), "PIR sensor")
        self.assertContains(self.client.get(reverse("cronograma:indicadores") + f"?d={self.lunes:%Y-%m-%d}"), "Carga por técnico")
        self.client.force_login(self.tec)
        self.assertEqual(self.client.get(reverse("cronograma:insumos")).status_code, 403)

    def test_envio_semanal_a_tecnicos_y_administrativa(self):
        sin_correo = Usuario.objects.create_user("carlos", "", "Clave-segura-1", rol="agente", first_name="Carlos")
        a = Actividad.objects.get(ticket=self.ticket)
        a.acompanantes.add(sin_correo)
        url = reverse("cronograma:correos") + f"?d={self.lunes:%Y-%m-%d}"
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(url + "&ver=0"), "Vista previa")
        mail.outbox.clear()
        r = self.client.post(url, {"accion": "enviar"}, follow=True)
        self.assertContains(r, "Sin correo: Carlos")
        por_destino = {m.to[0]: m for m in mail.outbox}
        self.assertEqual(set(por_destino), {"freyder@janotec.test", "alberto@janotec.test", "administrativa@janotec.test"})
        freyder = por_destino["freyder@janotec.test"].alternatives[0][0]
        self.assertIn("20 m UTP", freyder)
        self.assertNotIn("Revisar cámara 1", freyder)  # solo sus visitas
        admin = por_destino["administrativa@janotec.test"].alternatives[0][0]
        self.assertIn("Revisar cámara 1", admin)
        self.assertIn("Insumos consolidados", admin)
        # Ya enviada y sin cambios: no hay aviso pendiente
        self.assertContains(self.client.get(url), "No hay cambios desde entonces")

    def test_aviso_de_cambios_solo_a_los_afectados(self):
        from . import semanal
        semanal.enviar_semana(self.lunes, self.coord)
        a = Actividad.objects.get(ticket=self.ticket)
        self.client.post(reverse("cronograma:mover", args=[a.pk]), {"fecha": (self.lunes + timedelta(days=2)).isoformat()},
                         content_type="application/json")
        self.assertContains(self.client.get(reverse("cronograma:semana") + f"?d={self.lunes:%Y-%m-%d}"),
                            "cambió después de enviarlo")
        mail.outbox.clear()
        self.client.post(reverse("cronograma:correos") + f"?d={self.lunes:%Y-%m-%d}", {"accion": "cambios"})
        self.assertEqual({m.to[0] for m in mail.outbox}, {"freyder@janotec.test", "administrativa@janotec.test"})
        self.assertTrue(all(m.subject.startswith("IMPORTANTE") for m in mail.outbox))
        self.assertIn("Fecha", mail.outbox[0].alternatives[0][0])
        self.assertFalse(semanal.cambios(self.lunes)["hay"])
        # Si a un técnico le quitan su visita, también se le avisa
        Actividad.objects.get(ticket=self.otro).encargados.set([self.tec])
        mail.outbox.clear()
        semanal.notificar_cambios(self.lunes, self.coord)
        self.assertIn("alberto@janotec.test", {m.to[0] for m in mail.outbox})

    def test_comando_de_los_jueves(self):
        import io
        from django.core.management import call_command
        dia = self.lunes.isoformat()
        salida = io.StringIO()
        call_command("enviar_cronograma", "--semana", dia, "--simular", stdout=salida)
        self.assertIn("freyder@janotec.test", salida.getvalue())
        mail.outbox.clear()
        call_command("enviar_cronograma", "--semana", dia, stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 3)
        mail.outbox.clear()
        salida = io.StringIO()
        call_command("enviar_cronograma", "--semana", dia, stdout=salida)
        self.assertIn("sin cambios", salida.getvalue())
        self.assertEqual(len(mail.outbox), 0)
        Actividad.objects.filter(ticket=self.ticket).update(insumos="2 cámaras domo")
        call_command("enviar_cronograma", "--semana", dia, stdout=io.StringIO())
        self.assertTrue(any(m.subject.startswith("IMPORTANTE") for m in mail.outbox))


class PlataformaAnteriorTests(BaseCronograma):
    """Lo que traía cronogramajanotec.netlify.app."""

    def setUp(self):
        super().setUp()
        from .models import Contacto
        self.lunes = Actividad.lunes_de(proximo(0))
        Contacto.objects.create(nombre="Administrativa", email="admin@janotec.test", tipo="administrativa")
        Contacto.objects.create(nombre="Área SST", email="hse@janotec.test", tipo="sst")
        self.programar(self.lunes, insumos="3 batería 12V 7.5 A", sst="on", sst_persona="Javier Jaramillo")
        self.a = Actividad.objects.get()

    def url(self, nombre):
        return reverse(f"cronograma:{nombre}") + f"?d={self.lunes:%Y-%m-%d}"

    def test_tabla_y_vaciar(self):
        self.client.force_login(self.coord)
        html = self.client.get(self.url("semana") + "&vista=tabla").content.decode()
        self.assertIn("Notificar a", html)
        self.assertIn("fernando@llano.co", html)
        self.assertNotIn("Duplicar", html)  # no se crean copias de actividades sin ticket
        self.client.post(self.url("vaciar"))
        self.assertFalse(Actividad.objects.exists())
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.RECEPCIONADO)

    def test_pdf_para_whatsapp(self):
        self.client.force_login(self.tec)
        r = self.client.get(self.url("pdf"))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_respaldo_e_importar_el_json_de_la_plataforma_anterior(self):
        import json
        self.client.force_login(self.coord)
        datos = json.loads(self.client.get(self.url("respaldo")).content)
        self.assertEqual(datos["rows"][0]["enc"], ["Freyder Gordillo"])
        # El formato de cronogramajanotec: nombres cortos, «dest» con nombres del directorio
        anterior = {"week": "2030-08-05", "dir": [{"nombre": "Fernando", "email": "fernando@palmeras.co", "tipo": "Contacto"},
                                                   {"nombre": "Freyder", "email": "", "tipo": "Técnico"}],
                    "rows": [{"fecha": "2030-08-05", "hi": "08:00", "hf": "12:00", "cliente": "Grupo del Llano",
                              "sede": "Manare", "actividad": "Mantenimiento preventivo", "tipo": "Mantenimiento",
                              "linea": "Alarmas / intrusión", "enc": ["Freyder"], "aco": ["Javier"], "ins": "1 PIR sensor",
                              "dest": ["Fernando"], "sst": {"req": True}, "veh": True, "via": False},
                             {"fecha": "", "cliente": "Sin fecha"}]}

        def subir(contenido):
            archivo = SimpleUploadedFile("cronograma.json", contenido, content_type="application/json")
            return self.client.post(reverse("cronograma:importar"), {"archivo": archivo}, follow=True)

        r = subir(json.dumps(anterior).encode())
        self.assertContains(r, "1 actividad(es) importada(s)")
        self.assertContains(r, "No se encontraron como usuarios: Javier")
        nueva = Actividad.objects.get(fecha=date(2030, 8, 5))
        self.assertEqual((nueva.tipo, nueva.notificar_a, nueva.sst, nueva.vehiculo),
                         ("mantenimiento", "fernando@palmeras.co", True, True))
        self.assertEqual(list(nueva.encargados.all()), [self.tec])
        self.assertContains(subir(json.dumps(anterior).encode()), "1 ya existían")  # no duplica
        self.assertContains(subir(b"no es json"), "no es un respaldo")

    def test_historial_por_rango(self):
        self.client.force_login(self.coord)
        desde, hasta = self.lunes - timedelta(days=30), self.lunes + timedelta(days=30)
        r = self.client.get(reverse("cronograma:historial") + f"?desde={desde}&hasta={hasta}")
        self.assertEqual(r.context["ind"]["total"], 1)
        self.assertContains(r, "Semanas registradas")
        self.assertEqual(self.client.get(reverse("cronograma:historial") + "?rapido=anio").status_code, 200)

    def test_directorio_y_configuracion_mandan_en_los_envios(self):
        import io
        from django.core.management import call_command
        from . import semanal
        from .models import ConfiguracionEnvio, Contacto
        self.client.force_login(self.coord)
        self.client.post(reverse("cronograma:directorio"), {"nombre": "Almacén", "email": "almacen@janotec.test", "tipo": "contacto"})
        self.assertTrue(Contacto.objects.filter(email="almacen@janotec.test").exists())
        self.assertContains(self.client.get(self.url("semana")), "+ Almacén")  # se agrega a «Notificar a» con un clic
        mail.outbox.clear()
        semanal.enviar_semana(self.lunes, self.coord)
        destinos = {m.to[0] for m in mail.outbox}
        self.assertIn("admin@janotec.test", destinos)  # la administrativa sale del directorio
        self.assertNotIn("fernando@llano.co", destinos)  # «avisar a contactos» está apagado por defecto
        self.client.post(reverse("cronograma:configuracion"), {
            "envio_automatico": "on", "hora": "jue-16", "semana_objetivo": "actual",
            "remitente": "Coordinación TIC JANOTEC", "avisar_contactos": "on"})
        mail.outbox.clear()
        semanal.enviar_semana(self.lunes, self.coord)
        contacto = next(m for m in mail.outbox if m.to[0] == "fernando@llano.co")
        self.assertIn("Visitas programadas", contacto.subject)
        self.assertIn("Coordinación TIC JANOTEC", contacto.alternatives[0][0])
        ConfiguracionEnvio.objects.update(envio_automatico=False)
        salida = io.StringIO()
        call_command("enviar_cronograma", stdout=salida)
        self.assertIn("apagado", salida.getvalue())

    def test_visor_de_solo_lectura_sin_usuario(self):
        r = self.client.get(reverse("cronograma:visor", args=[herramientas.token_visor(self.lunes)]))
        self.assertContains(r, "Grupo del Llano")
        self.assertContains(r, "solo lectura")
        self.assertNotContains(r, "Enviar autorización")
        self.assertEqual(self.client.get("/cronograma/visor/falso/").status_code, 404)

    def test_correo_de_una_actividad_y_sst_manual(self):
        self.client.force_login(self.coord)
        mail.outbox.clear()
        self.client.post(reverse("cronograma:enviar_actividad", args=[self.a.pk]))
        self.assertEqual({m.to[0] for m in mail.outbox}, {"freyder@janotec.test", "admin@janotec.test"})
        self.assertIn("3 batería 12V 7.5 A", mail.outbox[0].alternatives[0][0])
        r = self.client.get(reverse("cronograma:correo_sst", args=[self.a.pk]))
        self.assertContains(r, "Javier Jaramillo")
        self.assertContains(r, "hse@janotec.test")
        mail.outbox.clear()
        self.client.post(reverse("cronograma:correo_sst", args=[self.a.pk]))
        self.assertEqual(mail.outbox[0].to, ["hse@janotec.test"])
        self.assertEqual(self.client.get(self.url("correos")).context["resumen"]["sst"], 1)
        self.client.force_login(self.tec)
        for nombre in ("historial", "directorio", "configuracion", "respaldo"):
            self.assertEqual(self.client.get(reverse(f"cronograma:{nombre}")).status_code, 403, nombre)


class CorreosComoLaAnteriorTests(BaseCronograma):
    """«Correos y envío» como en cronogramajanotec: todos los destinatarios, SST, enviar uno y programar el jueves."""

    def setUp(self):
        super().setUp()
        self.lunes = Actividad.lunes_de(proximo(0))
        sin_correo = Usuario.objects.create_user("alberto2", "", "Clave-segura-1", rol="agente", first_name="Alberto")
        self.programar(self.lunes, sst="on", sst_persona="Javier Jaramillo", encargados=[self.tec.pk, sin_correo.pk])
        self.a = Actividad.objects.get()
        self.url = reverse("cronograma:correos") + f"?d={self.lunes:%Y-%m-%d}"
        self.client.force_login(self.coord)

    def test_lista_con_quien_no_tiene_correo_y_la_solicitud_sst(self):
        from . import semanal
        correos, faltan = semanal.correos_semana(self.lunes)
        tipos = [(c["tipo"], c["nombre"], bool(c["email"])) for c in correos]
        self.assertIn(("Técnico", "Alberto", False), tipos)
        self.assertIn(("Administrativa", "Administrativa", False), tipos)  # sin directorio ni .env
        self.assertIn(("SST", "Grupo del Llano Manare", True), tipos)
        r = self.client.get(self.url + "&ver=0")
        self.assertContains(r, "sin correo")
        self.assertContains(r, "Solicitudes SST (1)")
        self.assertIn("Semana del", semanal.semana_larga(self.lunes))
        sst = next(c for c in correos if c["tipo"] == "SST")
        self.assertIn("SOLICITUD DE ACOMPAÑAMIENTO HSE", sst["html"].upper())
        self.assertIn("Javier Jaramillo", sst["html"])

    def test_enviar_todos_incluye_sst_una_sola_vez(self):
        from . import servicios
        mail.outbox.clear()
        r = self.client.post(self.url, {"accion": "enviar"}, follow=True)
        self.assertContains(r, "Sin correo: Alberto")
        destinos = [m.to[0] for m in mail.outbox]
        self.assertIn("sst@janotec.test", destinos)
        self.assertNotIn("", destinos)
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.sst_enviada)
        # Si el cliente acepta después, no se repite la solicitud SST
        mail.outbox.clear()
        servicios.responder_autorizacion(self.a, True)
        self.assertNotIn("sst@janotec.test", [m.to[0] for m in mail.outbox])
        # Si se reprograma, la solicitud SST vuelve a quedar pendiente
        self.client.post(reverse("cronograma:mover", args=[self.a.pk]), {"fecha": (self.lunes + timedelta(days=1)).isoformat()},
                         content_type="application/json")
        self.a.refresh_from_db()
        self.assertIsNone(self.a.sst_enviada)

    def test_enviar_ahora_un_solo_correo(self):
        from . import semanal
        correos, _ = semanal.correos_semana(self.lunes)
        freyder = next(i for i, c in enumerate(correos) if c["nombre"] == "Freyder Gordillo")
        alberto = next(i for i, c in enumerate(correos) if c["nombre"] == "Alberto")
        mail.outbox.clear()
        self.client.post(self.url, {"accion": "uno", "indice": str(freyder)})
        self.assertEqual([m.to for m in mail.outbox], [["freyder@janotec.test"]])
        self.assertIsNone(semanal.cambios(self.lunes))  # un correo suelto no marca la semana como enviada
        r = self.client.post(self.url, {"accion": "uno", "indice": str(alberto)}, follow=True)
        self.assertContains(r, "no tiene correo")

    def test_programar_para_el_jueves(self):
        import io
        from django.core.management import call_command
        from .models import ProgramacionSemana
        lejana = Actividad.lunes_de(proximo(0) + timedelta(days=21))
        self.programar(lejana, ticket=Ticket.objects.create(
            empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="p", descripcion="Programada",
            estado=E.RECEPCIONADO, contacto_email="a@llano.co"))
        url = reverse("cronograma:correos") + f"?d={lejana:%Y-%m-%d}"
        self.assertContains(self.client.post(url, {"accion": "programar"}, follow=True), "programada")
        self.assertTrue(ProgramacionSemana.objects.filter(lunes=lejana).exists())
        mail.outbox.clear()
        call_command("enviar_cronograma", stdout=io.StringIO())  # sin --semana: envía la programada
        self.assertTrue(any("Programada" in m.alternatives[0][0] for m in mail.outbox))
        self.assertFalse(ProgramacionSemana.objects.exists())  # ya salió


class CorreccionDelTicketTests(BaseCronograma):
    """Guana corrige lo que el cliente puso por error: antes → ahora, motivo, aviso al cliente y cronograma."""

    def datos(self, **kw):
        datos = {"empresa": self.emp.pk, "sede": self.sede.pk, "departamento": "Meta", "municipio": "Villavicencio",
                 "direccion": "Calle 1", "tipo": "mantenimiento", "categoria": self.cat.pk,
                 "descripcion": self.ticket.descripcion, "impacto": "media", "contacto_telefono": "300",
                 "contacto_email": self.ticket.contacto_email, "contacto_nombre": "Fernando", "falla_nombre": self.ticket.asunto,
                 "alturas": "no", "sst": "no"}
        datos.update(kw)
        return datos

    def setUp(self):
        super().setUp()
        Ticket.objects.filter(pk=self.ticket.pk).update(departamento="Meta", municipio="Villavicencio", direccion="Calle 1",
                                                        contacto_telefono="300", impacto="media")
        self.ticket.refresh_from_db()
        self.client.force_login(self.coord)
        self.url = reverse("editar_ticket", args=[self.ticket.radicado])

    def test_queda_antes_y_ahora_con_motivo_y_se_informa_al_cliente(self):
        mail.outbox.clear()
        self.client.post(self.url, self.datos(tipo="soporte", impacto="alta", descripcion="Cámara del parqueadero sin imagen",
                                              motivo_ajuste="Lo confirmé por teléfono con la administradora.",
                                              informar_cliente="on"))
        nota = self.ticket.seguimientos.get(interno=True)
        self.assertIn("Tipo de ticket: Mantenimiento → Servicio técnico", nota.mensaje)
        self.assertIn("Nivel de urgencia: Media → Alta", nota.mensaje)
        self.assertIn("Lo confirmé por teléfono", nota.mensaje)
        visible = self.ticket.seguimientos.get(interno=False, tipo="comentario")
        self.assertIn("Ajustamos su solicitud", visible.mensaje)
        self.assertTrue(any("fernando@llano.co" in m.to for m in mail.outbox))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.descripcion, "Cámara del parqueadero sin imagen")

    def test_sin_informar_el_cliente_no_ve_el_ajuste(self):
        mail.outbox.clear()
        self.client.post(self.url, self.datos(impacto="baja"))
        self.assertFalse(self.ticket.seguimientos.filter(interno=False).exists())
        self.assertEqual(mail.outbox, [])
        self.client.force_login(self.cli)
        self.assertNotContains(self.client.get(self.ticket.get_absolute_url()), "Media → Baja")

    def test_la_actividad_del_cronograma_se_actualiza_si_no_se_ha_enviado(self):
        otra = Sede.objects.create(empresa=self.emp, nombre="Acacías")
        self.programar(proximo(0))
        a = Actividad.objects.get()
        self.client.force_login(self.coord)
        r = self.client.post(self.url, self.datos(sede=otra.pk, descripcion="Revisar zona 013", contacto_email="nuevo@llano.co",
                                                  nd_lunes="todo"), follow=True)
        a.refresh_from_db()
        self.assertEqual((a.sede, a.actividad, a.notificar_a), ("Acacías", "Revisar zona 013", "nuevo@llano.co"))
        self.assertTrue(a.en_rojo)  # ahora el lunes no puede atender
        self.assertContains(r, "la visita programada cruza")

    def test_con_la_autorizacion_enviada_no_se_cambia_la_actividad_y_se_avisa(self):
        self.programar(proximo(0))
        Actividad.objects.update(autorizacion=Actividad.Autorizacion.ENVIADA)
        r = self.client.post(self.url, self.datos(descripcion="Otra cosa"), follow=True)
        self.assertEqual(Actividad.objects.get().actividad, "Mantenimiento preventivo zona 013")
        self.assertContains(r, "reenvíe la autorización")


class DatosDelAreaTecnicaTests(BaseCronograma):
    """Horas sin disponibilidad, agenda del experto SST y ubicación del sitio."""

    def test_no_puede_en_un_rango_de_horas(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(no_disponible={"martes": "08:00-10:00"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.resumen_no_disponible, "Martes de 08:00 a 10:00")
        martes = proximo(1)
        self.programar(martes, "09:00", "11:00")
        self.assertIn("de 08:00 a 10:00", Actividad.objects.get().choque)
        otro = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="o", descripcion="o",
                                     estado=E.RECEPCIONADO, no_disponible={"martes": "08:00-10:00"}, contacto_email="a@llano.co")
        self.programar(martes, "11:00", "12:00", ticket=otro)  # a las 11 sí puede
        self.assertEqual(Actividad.objects.get(ticket=otro).choque, "")

    def test_al_radicar_solo_se_indican_los_dias_sin_horas(self):
        from tickets.forms import RadicacionForm
        form = RadicacionForm({"nd_lunes": "horas", "nd_sabado": "tarde"}, usuario=self.cli)
        form.is_valid()
        self.assertNotIn("jornada", form.fields)  # solo los días en que no pueden recibir al personal
        self.assertNotIn("restricciones", form.fields)
        self.assertIn("nd_lunes", form.errors)  # ya no se eligen horas
        self.assertIn("nd_sabado", form.errors)  # el sábado solo hay mañana
        self.assertNotIn("nd_domingo", form.fields)  # el domingo no se trabaja

    def test_domingo_y_sabado_tarde_quedan_en_rojo(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(no_disponible={})
        self.programar(proximo(6))
        self.assertEqual(Actividad.objects.get().choque, "Los domingos no se trabaja.")
        Actividad.objects.all().delete()
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=E.RECEPCIONADO)
        self.programar(proximo(5), "13:00", "16:00")
        self.assertIn("sábado solo se trabaja", Actividad.objects.get().choque)
        Actividad.objects.all().delete()
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=E.RECEPCIONADO)
        self.programar(proximo(5), "08:00", "12:00")
        self.assertEqual(Actividad.objects.get().choque, "")

    def test_la_visita_respeta_la_jornada_pedida(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(no_disponible={}, jornada="tarde")
        self.ticket.refresh_from_db()
        self.programar(proximo(2), "08:00", "12:00")
        self.assertIn("jornada de la tarde", Actividad.objects.get().choque)
        Actividad.objects.all().delete()
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=E.RECEPCIONADO)
        self.programar(proximo(2), "14:00", "17:00")
        self.assertEqual(Actividad.objects.get().choque, "")
        from cronograma.models import Actividad as A
        self.assertEqual(A.valores_desde_ticket(self.ticket)["hora_inicio"], "14:00")

    def test_el_experto_sst_no_puede_estar_en_dos_sitios(self):
        martes = proximo(1)
        self.programar(martes, "08:00", "12:00", sst="on", sst_persona="Javier Jaramillo")
        otro = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="s", descripcion="s",
                                     estado=E.RECEPCIONADO, contacto_email="a@llano.co")
        r = self.programar(martes, "10:00", "14:00", ticket=otro, sst="on", sst_persona="Javier Jaramillo")
        self.assertContains(r, "El experto SST (Javier Jaramillo) ya tiene otra actividad")
        self.assertTrue(Actividad.objects.get(ticket=otro).en_rojo)
        tercero = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="t", descripcion="t",
                                        estado=E.RECEPCIONADO, contacto_email="a@llano.co")
        self.programar(martes, "13:00", "15:00", ticket=tercero, sst="on", sst_persona="Javier Jaramillo")
        self.assertIn("ya tiene otra actividad", Actividad.objects.get(ticket=tercero).choque)  # cruza con la de 10 a 14
        self.programar(martes + timedelta(days=1), "13:00", "15:00", ticket=Ticket.objects.create(
            empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="u", descripcion="u",
            estado=E.RECEPCIONADO, contacto_email="a@llano.co"), sst="on", sst_persona="Javier Jaramillo")
        self.assertEqual(Actividad.objects.filter(fecha=martes + timedelta(days=1)).get().choque, "")

    def test_ubicacion_y_mapa(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(direccion="Calle 15 # 40-20", municipio="Villavicencio", departamento="Meta")
        self.ticket.refresh_from_db()
        self.assertIn("Calle%2015", self.ticket.enlace_mapa)
        Ticket.objects.filter(pk=self.ticket.pk).update(latitud="4.1420000", longitud="-73.6270000")
        self.ticket.refresh_from_db()
        self.assertIn("query=4.1420000,-73.6270000", self.ticket.enlace_mapa)
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Ver ubicación exacta en el mapa")


class AplazamientoTests(BaseCronograma):
    """Guana aplaza una visita programada: vuelve a «Por programar», queda el motivo y el distintivo «Aplazado»."""

    def aplazar(self, usuario=None, **kw):
        datos = {"motivo": "cliente", "informar_cliente": "on",
                 "detalle": "La administradora pidió pasar la visita: hay auditoría en la sede ese día."}
        datos.update(kw)
        self.client.force_login(usuario or self.coord)
        return self.client.post(reverse("aplazar", args=[self.ticket.radicado]), datos, follow=True)

    def test_aplazar_devuelve_a_por_programar_con_motivo_y_avisos(self):
        from tickets.models import Aplazamiento, Seguimiento
        self.programar(proximo(0))
        a = Actividad.objects.get()
        servicios.enviar_autorizacion(a, self.coord)
        enlace = servicios.token_autorizacion(a)
        mail.outbox.clear()
        r = self.aplazar()
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.visita_programada, self.ticket.veces_aplazado), (E.RECEPCIONADO, None, 1))
        radicado = self.ticket.radicado
        self.assertFalse(Actividad.objects.exists())
        ap = Aplazamiento.objects.get()
        self.assertEqual((ap.numero, ap.motivo, ap.autor, ap.informado_cliente), (1, "cliente", self.coord, True))
        self.assertIn("Freyder", ap.programacion)
        seg = self.ticket.seguimientos.get(tipo=Seguimiento.Tipo.APLAZAMIENTO)
        self.assertTrue(seg.interno)
        self.assertIn("auditoría", seg.mensaje)
        destinatarios = {d for m in mail.outbox for d in m.to}
        self.assertIn("freyder@janotec.test", destinatarios)  # el técnico ya no va
        self.assertIn("fernando@llano.co", destinatarios)  # se le informó al cliente
        self.assertContains(r, "Este ticket se aplazó 1 vez")
        self.assertIsNone(servicios.actividad_de_token(enlace))  # la autorización anterior ya no sirve

        # Vuelve recepcionado (mismo radicado), listo para reprogramar, sin repetirle al cliente «en proceso»
        self.client.force_login(self.coord)
        self.assertContains(self.client.get(reverse("panel") + "?aplazados=1"), radicado)
        self.assertFalse(any("en proceso" in m.subject.lower() for m in mail.outbox))
        self.assertEqual(self.ticket.radicado, radicado)
        # Se reprograma y se vuelve a aplazar: el distintivo muestra cuántas veces
        self.programar(proximo(1))
        self.aplazar(motivo="tecnico", informar_cliente="", detalle="Se dañó la camioneta y no hay cómo llegar a Manare.")
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.veces_aplazado, self.ticket.estado), (2, E.RECEPCIONADO))
        self.assertFalse(self.ticket.aplazamientos.get(numero=2).informado_cliente)
        panel = self.client.get(reverse("panel") + "?aplazados=1")
        self.assertContains(panel, "Aplazado ×2")
        self.assertContains(panel, self.ticket.radicado)
        semana = self.client.get(reverse("cronograma:semana"))
        self.assertContains(semana, "Inconveniente técnico: Se dañó la camioneta")
        # El cliente solo ve lo que se le informó
        self.client.force_login(self.cli)
        cliente = self.client.get(self.ticket.get_absolute_url())
        self.assertContains(cliente, "fue aplazada")
        self.assertNotContains(cliente, "camioneta")

    def test_el_motivo_debe_quedar_claro(self):
        self.programar(proximo(0))
        r = self.aplazar(detalle="Cliente")
        self.assertContains(r, "No se aplazó")
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.veces_aplazado), (E.EN_CRONOGRAMA, 0))

    def test_solo_el_coordinador_y_solo_si_esta_programado(self):
        director = Usuario.objects.create_user("director", "dir@janotec.test", "Clave-segura-1", rol="direccion")
        r = self.aplazar()
        self.assertContains(r, "Solo se aplaza una visita que está programada")
        self.programar(proximo(0))
        for usuario in (self.tec, director):
            self.client.force_login(usuario)
            self.assertEqual(self.client.post(reverse("aplazar", args=[self.ticket.radicado]),
                                              {"motivo": "otro", "detalle": "x" * 30}).status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, E.EN_CRONOGRAMA)


class InsumosDesdeInventarioTests(BaseCronograma):
    """Guana planea los insumos y equipos de la visita eligiendo del inventario (con lo que hay en bodega)."""

    def setUp(self):
        super().setUp()
        from inventario import servicios as inv
        from inventario.models import Activo, Insumo, Lugar, MovimientoActivo
        self.bateria = Insumo.objects.create(codigo="BAT-12V7A", descripcion="Bateria 12V-7 ah seca")
        self.cable = Insumo.objects.create(codigo="CAB-UTP-EXT", descripcion="Cable UTP exterior, cat6", unidad="Metro")
        inv.mover_insumo(self.bateria, 4, MovimientoActivo.Tipo.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA)
        inv.mover_insumo(self.cable, 305, MovimientoActivo.Tipo.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA)
        for n in (1, 2):
            Activo.objects.create(placa=f"CAM-BAL-4MP00{n}", tipo="Cámara", subtipo="Bala", marca="Hikvisión",
                                  referencia="DS2CD1043G2LIU")

    def test_el_cronograma_ofrece_el_inventario_con_existencias(self):
        self.client.force_login(self.coord)
        r = self.client.get(reverse("cronograma:semana"))
        catalogo = {i["codigo"]: i for i in r.context["catalogo_inventario"]}
        self.assertEqual(catalogo["BAT-12V7A"]["disponible"], 4)
        self.assertEqual((catalogo["CAB-UTP-EXT"]["unidad"], catalogo["CAB-UTP-EXT"]["disponible"]), ("m", 305))
        self.assertEqual((catalogo["DS2CD1043G2LIU"]["clase"], catalogo["DS2CD1043G2LIU"]["disponible"]), ("activo", 2))
        self.assertContains(r, 'id="inv-buscar"')
        self.client.force_login(self.tec)  # el técnico solo ve sus visitas
        self.assertNotIn("catalogo_inventario", self.client.get(reverse("cronograma:semana")).context)

    def test_los_insumos_de_la_semana_dicen_cuanto_falta(self):
        self.programar(proximo(0), insumos="3 Bateria 12V-7 ah seca [BAT-12V7A]\n20 m Cable UTP exterior cat6 [CAB-UTP-EXT]\n"
                                           "2 Cámara Bala Hikvisión DS2CD1043G2LIU [DS2CD1043G2LIU]")
        otro = Ticket.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.cat, asunto="o", descripcion="o",
                                     estado=E.RECEPCIONADO, contacto_email="a@llano.co")
        self.programar(proximo(0) + timedelta(days=1), ticket=otro, insumos="2 Bateria 12V-7 ah seca [BAT-12V7A], cinta aislante")
        r = self.client.get(reverse("cronograma:insumos") + f"?d={proximo(0):%Y-%m-%d}")
        filas = {f["codigo"] or f["descripcion"]: f for f in r.context["consolidado"]}
        self.assertEqual((filas["BAT-12V7A"]["cantidad"], filas["BAT-12V7A"]["en_bodega"], filas["BAT-12V7A"]["falta"]), (5, 4, 1))
        self.assertEqual(filas["CAB-UTP-EXT"]["falta"], 0)
        self.assertEqual(filas["DS2CD1043G2LIU"]["en_bodega"], 2)
        self.assertNotIn("en_bodega", filas["cinta aislante"])  # escrito a mano
        self.assertContains(r, "faltan 1")


class CorreoAlTecnicoTests(BaseCronograma):
    """Al agendar, reprogramar o retirar a un técnico de una visita, le llega un correo."""

    def correos_de(self, direccion):
        return [m for m in mail.outbox if direccion in m.to]

    def test_agendar_reprogramar_y_retirar(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.programar(proximo(0))
        a = Actividad.objects.get()
        nuevo = self.correos_de("freyder@janotec.test")
        self.assertEqual(len(nuevo), 1)
        self.assertIn("Se le programó una visita", nuevo[0].subject)
        self.assertIn(self.ticket.radicado, nuevo[0].subject)
        self.assertIn(servicios.cuando(a), nuevo[0].body)
        # Se agrega a Alberto y se cambia la hora: Alberto recibe «programó», Freyder «reprogramada»
        mail.outbox.clear()
        datos = {"ticket": self.ticket.pk, "fecha": a.fecha.isoformat(), "hora_inicio": "13:00", "hora_fin": "17:00",
                 "cliente": a.cliente, "sede": a.sede, "actividad": a.actividad, "tipo": a.tipo, "linea": a.linea,
                 "encargados": [self.tec.pk], "acompanantes": [self.tec2.pk], "notificar_a": a.notificar_a,
                 "sst_duracion": "medio día"}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cronograma:editar", args=[a.pk]), datos)
        self.assertIn("Se le programó una visita", self.correos_de("alberto@janotec.test")[0].subject)
        self.assertIn("Visita reprogramada", self.correos_de("freyder@janotec.test")[0].subject)
        # Al quitar la actividad, los dos saben que ya no van
        mail.outbox.clear()
        self.client.post(reverse("cronograma:eliminar", args=[a.pk]))
        for correo in ("freyder@janotec.test", "alberto@janotec.test"):
            self.assertIn("Ya no tiene esta visita", self.correos_de(correo)[0].subject)


class AuditoriaDeCorreosTests(BaseCronograma):
    """Recorre los casos de uso que deben avisar por correo y verifica a quién le llega cada aviso."""

    def para(self, direccion):
        return [m.subject for m in mail.outbox if direccion in m.to]

    def test_cada_caso_avisa_a_quien_corresponde(self):
        bodega = Usuario.objects.create_user("yuri", "bodega@janotec.test", "Clave-segura-1", rol="bodega", first_name="Yuri")
        Usuario.objects.create_user("miguel", "direccion@janotec.test", "Clave-segura-1", rol="direccion")
        t = self.ticket
        self.client.force_login(self.coord)

        # 1. jguana le pasa el ticket a bodega pidiendo elementos de logística: le llega con lo que se le pidió
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "estado": t.estado, "prioridad": t.prioridad, "asignado_a": bodega.pk, "visibilidad": "interno",
            "mensaje": "Necesito 2 rollos de cable UTP y 20 conectores RJ45 para esta visita."})
        aviso = [m for m in mail.outbox if "bodega@janotec.test" in m.to]
        self.assertEqual(len(aviso), 1)
        self.assertIn("Se le asignó un ticket", aviso[0].subject)
        self.assertIn("20 conectores RJ45", aviso[0].body)
        self.assertEqual(self.para("fernando@llano.co"), [])  # la nota interna no le llega al cliente

        # 2. Bodega responde en el ticket: el área técnica se entera
        mail.outbox.clear()
        self.client.force_login(bodega)
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": t.estado, "visibilidad": "interno",
                                                                     "mensaje": "Material listo en bodega."})
        self.assertTrue(any("Nota interna de Yuri" in s for s in self.para("coordinador@janotec.test")))

        # 3. jguana le escribe a quien tiene el ticket: le llega a bodega
        mail.outbox.clear()
        self.client.force_login(self.coord)
        self.client.post(reverse("gestionar", args=[t.radicado]), {
            "estado": t.estado, "prioridad": t.prioridad, "asignado_a": bodega.pk, "visibilidad": "interno",
            "mensaje": "¿Me confirma si hay conectores?"})
        self.assertEqual(len(self.para("bodega@janotec.test")), 1)

        # 4. Agenda la visita con material: técnico, cliente y la solicitud de material a bodega (autoriza después)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.programar(proximo(0), insumos="2 Cable UTP\n20 Conector RJ45")
        self.assertTrue(any("Se le programó una visita" in s for s in self.para("freyder@janotec.test")))
        self.assertFalse(self.para("fernando@llano.co"), "al cliente no le llega «Programamos la visita»")
        self.assertTrue(self.para("bodega@janotec.test"), "bodega recibe la solicitud de material")
        self.assertFalse(self.para("direccion@janotec.test"), "dirección no se entera hasta que bodega valide")

        # 5. Autorización de ingreso al cliente (desde la plataforma exige los documentos vigentes del técnico)
        mail.outbox.clear()
        servicios.enviar_autorizacion(Actividad.objects.get(), self.coord)
        self.assertTrue(self.para("fernando@llano.co"), "el cliente recibe la autorización de ingreso")


class ActividadInternaTests(BaseCronograma):
    """Sin ticket es una actividad interna: se le avisa al técnico, pero no hay autorización ni correo al cliente."""

    def test_actividad_interna(self):
        self.client.force_login(self.coord)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cronograma:crear"), {
                "fecha": proximo(2).isoformat(), "hora_inicio": "08:00", "hora_fin": "12:00", "cliente": "JANOTEC",
                "actividad": "Recoger demos en Palmeras", "tipo": "soporte", "linea": "Otro", "encargados": [self.tec.pk],
                "notificar_a": "cliente@otro.co", "sst_duracion": "medio día"})
        a = Actividad.objects.get(ticket__isnull=True)
        self.assertEqual((a.tipo, a.notificar_a, a.correos, a.en_rojo), (Actividad.Tipo.INTERNA, "", [], False))
        self.assertFalse(any("cliente@otro.co" in m.to for m in mail.outbox))
        self.assertTrue(any(self.tec.email in m.to for m in mail.outbox))  # el técnico sí se entera
        r = self.client.post(reverse("cronograma:enviar_autorizacion", args=[a.pk]), follow=True)
        self.assertContains(r, "no se le envía autorización")
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, Actividad.Autorizacion.SIN_ENVIAR)
        semana = self.client.get(reverse("cronograma:semana") + f"?d={a.fecha:%Y-%m-%d}").content.decode()
        self.assertIn("Actividad interna", semana)
        self.assertIn("Agregar actividad interna", semana)

    def test_visita_de_ticket_no_puede_ser_interna(self):
        r = self.programar(proximo(1), tipo="interna")
        self.assertFalse(Actividad.objects.filter(ticket=self.ticket).exists())
        self.assertContains(r, "no puede ser «Actividad interna»")


class EnlaceCompartidoTests(BaseCronograma):
    """El cronograma en vivo para quien no usa la plataforma."""

    def setUp(self):
        super().setUp()
        self.fecha = proximo(1)
        self.programar(self.fecha, insumos="3 batería 12V 7.5 A")
        self.enlace = EnlaceCronograma.objects.create(creado_por=self.coord)
        self.client.logout()
        self.url = reverse("cronograma:compartido", args=[self.enlace.token]) + f"?d={self.fecha:%Y-%m-%d}"

    def test_se_ve_sin_usuario_y_sin_correos_del_cliente(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Grupo del Llano")
        self.assertContains(r, "Freyder Gordillo")
        self.assertContains(r, "solo lectura")
        self.assertContains(r, "noindex")
        self.assertIn("no-store", r["Cache-Control"])
        self.assertNotContains(r, "fernando@llano.co")
        self.assertNotContains(r, self.ticket.radicado)
        self.assertNotContains(r, "<form")

    def test_lo_nuevo_aparece_sin_generar_otro_enlace(self):
        Actividad.objects.filter(ticket=self.ticket).update(actividad="Revisión de última hora")
        r = self.client.get(self.url + "&parcial=1")
        self.assertContains(r, "Revisión de última hora")
        self.assertNotContains(r, "<html")

    def test_abre_en_la_semana_actual(self):
        r = self.client.get(reverse("cronograma:compartido", args=[self.enlace.token]))
        self.assertEqual(r.context["lunes"], Actividad.lunes_de(timezone.localdate()))

    def test_revocado_o_falso_no_abre(self):
        self.client.force_login(self.coord)
        r = self.client.post(reverse("cronograma:compartir"), {"accion": "revocar"}, follow=True)
        self.assertContains(r, "revocado")
        self.assertNotContains(r, self.enlace.token)
        self.client.logout()
        for url in (self.url, self.url + "&parcial=1", "/cronograma/compartido/falso/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 404)
            self.assertNotContains(r, "Grupo del Llano", status_code=404)

    def test_solo_la_coordinacion_administra_los_enlaces(self):
        url = reverse("cronograma:compartir")
        self.assertEqual(self.client.get(url).status_code, 302)  # sin sesión va al ingreso
        for usuario in (self.tec, self.cli):
            self.client.force_login(usuario)
            self.assertIn(self.client.get(url).status_code, (302, 403))
            self.assertIn(self.client.post(url).status_code, (302, 403))
        self.assertEqual(EnlaceCronograma.objects.count(), 1)

    def test_crear_da_un_solo_enlace_con_la_direccion_publica(self):
        self.client.force_login(self.coord)
        url = reverse("cronograma:compartir")
        self.client.post(url)  # ya había uno activo: no se crea otro
        self.assertEqual(EnlaceCronograma.objects.count(), 1)
        self.client.post(url, {"accion": "revocar"})
        self.assertContains(self.client.get(url), "Crear enlace")
        with self.settings(SITIO_URL="https://janotec.ejemplo/"):
            r = self.client.post(url, follow=True)
        nuevo = EnlaceCronograma.objects.get(activo=True)
        self.assertEqual(nuevo.creado_por, self.coord)
        self.assertContains(r, f"https://janotec.ejemplo/cronograma/compartido/{nuevo.token}/")
        self.assertNotEqual(nuevo.token, self.enlace.token)
        self.assertEqual(self.client.get(self.url).status_code, 404)  # el anterior sigue sin abrir

    def test_registra_el_acceso_sin_escribir_en_cada_consulta(self):
        self.client.get(self.url)
        self.enlace.refresh_from_db()
        primero = self.enlace.ultimo_acceso
        self.assertIsNotNone(primero)
        self.client.get(self.url)
        self.enlace.refresh_from_db()
        self.assertEqual(self.enlace.ultimo_acceso, primero)

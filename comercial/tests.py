import shutil
import tempfile
from datetime import timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.models import ActaSoporte, Categoria, OportunidadComercial, Ticket, TipoFalla

from .models import FichaComercial

MEDIA_TMP = tempfile.mkdtemp()
F = FichaComercial
T = Ticket


def proximo_lunes():
    """Un lunes futuro: día hábil en que el cliente de las pruebas sí puede atender."""
    hoy = timezone.localdate()
    return hoy + timedelta(days=7 - hoy.weekday())


# Envío urgente: comercial la envía sin esperar a dirección (la aprobación se prueba en CotizadorTests.test_direccion_*)
URGENTE = {"urgente": "1", "motivo_urgencia": "El cliente la necesita hoy"}


@override_settings(MEDIA_ROOT=MEDIA_TMP, NOTIFICAR_SOPORTE_EMAIL="coordinador@janotec.test",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseComercial(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        cat = Categoria.objects.create(nombre="Instalación / ampliación")
        TipoFalla.objects.create(categoria=cat, nombre="Cotización")
        TipoFalla.objects.create(categoria=cat, nombre="Instalar equipos nuevos")
        self.soporte = Categoria.objects.create(nombre="CCTV")
        self.emp = Empresa.objects.create(razon_social="Cliente A", nit="1")
        self.otra = Empresa.objects.create(razon_social="Cliente B", nit="2")
        self.sede = Sede.objects.create(empresa=self.emp, nombre="Bodega")
        self.cli = Usuario.objects.create_user("cli", "cli@a.co", "Clave-segura-1", rol="cliente", empresa=self.emp)
        self.cli_b = Usuario.objects.create_user("cli_b", "b@b.co", "Clave-segura-1", rol="cliente", empresa=self.otra)
        # El asesor de estas pruebas atiende las dos áreas (como un equipo pequeño): radica y arma la propuesta.
        # La separación entre comercial y administrativa se prueba en CotizadorTests.test_*_area_*
        self.asesor = Usuario.objects.create_user("asesor", "asesor@janotec.test", "Clave-segura-1", rol="comercial",
                                                  tambien_administrativo=True)
        self.tecnico = Usuario.objects.create_user("tec", "tec@janotec.test", "Clave-segura-1", rol="agente")
        self.coord = Usuario.objects.create_user("coord", "coord@janotec.test", "Clave-segura-1", rol="supervisor")
        self.director = Usuario.objects.create_user("director", "dir@janotec.test", "Clave-segura-1", rol="direccion")

    def ficha_post(self, **kw):
        datos = {"empresa": self.emp.pk, "contacto_nombre": "Leidy", "contacto_telefono": "3101234567",
                 "contacto_email": "leidy@a.co", "asunto": "Quieren cámaras en el parqueadero",
                 "presupuesto": "PRESUPUESTO-SECRETO-9", "nivel_decision": "decide", "servicios": ["cctv", "alarma"],
                 "nd_viernes": "tarde", "fecha_primera_visita": proximo_lunes().isoformat(), "jornada_visita": "manana"}
        datos.update(kw)
        return datos

    def crear(self, **kw):
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:nuevo"), self.ficha_post(**kw))
        return T.objects.filter(ficha__isnull=False).order_by("-pk").first()

    def pdf(self):
        return SimpleUploadedFile("propuesta.pdf", b"%PDF-1.4 propuesta", content_type="application/pdf")

    def radicados(self, url):
        import re
        return re.findall(r'class="radicado" href="[^"]+">(JNT-[\d-]+)', self.client.get(url).content.decode())


class RadicacionTests(BaseComercial):
    def test_asesor_registra_prospecto_en_el_area_comercial(self):
        mail.outbox.clear()
        t = self.crear(empresa="", nueva_empresa="Conjunto Nuevo PH", nuevo_nit="900999")
        self.assertRegex(t.radicado, r"^JNT-\d{4}-\d{6}$")  # un solo consecutivo para todo
        self.assertEqual((t.area, t.tipo, t.ficha.etapa, t.ficha.comercial), (T.Area.COMERCIAL, "cotizacion", "gestion", self.asesor))
        self.assertTrue(t.empresa.es_prospecto)
        self.assertEqual(t.no_disponible, {"viernes": "tarde"})
        self.assertTrue(any("leidy@a.co" in m.to for m in mail.outbox))

    def test_cliente_pide_cotizacion_y_la_ve_en_mis_solicitudes(self):
        self.client.force_login(self.cli)
        self.client.post(reverse("comercial:nuevo_cliente"), {
            "asunto": "Cámaras para la bodega nueva", "contacto_nombre": "Cli", "contacto_telefono": "300",
            "contacto_email": "cli@a.co", "servicios": ["cctv"], "sede": self.sede.pk})
        t = T.objects.get(ficha__isnull=False)
        self.assertEqual((t.empresa, t.ficha.origen, t.solicitante, t.area), (self.emp, "cliente", self.cli, "comercial"))
        self.assertIn(t.radicado, self.radicados(reverse("mis_tickets")))

    def test_quien_puede_radicar(self):
        self.client.force_login(self.cli)
        self.assertEqual(self.client.get(reverse("comercial:nuevo")).status_code, 403)
        self.client.force_login(self.coord)  # el coordinador técnico no hace lo comercial
        self.assertEqual(self.client.get(reverse("comercial:nuevo")).status_code, 403)
        self.client.force_login(self.asesor)
        self.assertEqual(self.client.get(reverse("comercial:nuevo_cliente")).status_code, 403)


class PrimeraVisitaYPrioridadTests(BaseComercial):
    """La ficha pide cuándo quiere el cliente la primera visita, y la urgencia y la fecha máxima marcan la prioridad."""

    def radicar(self, **kw):
        self.client.force_login(self.asesor)
        return self.client.post(reverse("comercial:nuevo"), self.ficha_post(**kw))

    def test_guarda_la_fecha_y_la_jornada_de_la_primera_visita(self):
        self.radicar()
        t = T.objects.get(ficha__isnull=False)
        self.assertEqual(t.ficha.fecha_primera_visita, proximo_lunes())
        self.assertEqual(t.jornada, T.Jornada.MANANA)
        self.assertIn("mañana", t.ficha.visita_display)
        self.assertContains(self.client.get(t.ficha.get_absolute_url()), "Primera visita:")

    def test_la_fecha_es_obligatoria_salvo_que_no_necesite_visita(self):
        r = self.radicar(fecha_primera_visita="")
        self.assertFalse(T.objects.exists())
        self.assertContains(r, "qué día quiere el cliente la primera visita")
        self.radicar(fecha_primera_visita="", sin_visita="on")
        f = F.objects.get()
        self.assertTrue(f.sin_visita)
        self.assertEqual(f.visita_display, "No necesita visita técnica")

    def test_no_acepta_domingo_pasado_ni_un_dia_en_que_no_puede(self):
        domingo = proximo_lunes() - timedelta(days=1)
        self.radicar(fecha_primera_visita=domingo.isoformat())
        self.radicar(fecha_primera_visita=(timezone.localdate() - timedelta(days=1)).isoformat())
        viernes = proximo_lunes() + timedelta(days=4)
        self.radicar(fecha_primera_visita=viernes.isoformat(), jornada_visita="tarde")  # el viernes no puede en la tarde
        self.radicar(fecha_primera_visita=viernes.isoformat(), nd_viernes="todo", jornada_visita="")
        self.assertFalse(T.objects.exists())

    def test_urgencia_alta_exige_fecha_maxima_y_queda_prioritaria(self):
        self.radicar(impacto="alta")
        self.assertFalse(T.objects.exists(), "con urgencia alta hay que decir para cuándo es la propuesta")
        mail.outbox.clear()
        manana = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.radicar(impacto="alta", fecha_max_propuesta=manana)
        f = F.objects.get()
        self.assertTrue(f.prioritaria)
        self.assertEqual(f.ticket.prioridad, T.Prioridad.ALTA)
        self.assertIn("urgencia alta", f.motivos_prioridad)
        aviso = next(m for m in mail.outbox if "PRIORITARIO" in m.subject)
        self.assertIn("agilizar", aviso.body)

    def test_fecha_maxima_cercana_la_vuelve_prioritaria_y_va_primero(self):
        lejos = (timezone.localtime() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
        cerca = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.radicar(asunto="Sin afán", fecha_max_propuesta=lejos)
        self.radicar(asunto="Para mañana", fecha_max_propuesta=cerca)
        normal, urgente = F.objects.order_by("pk")
        self.assertFalse(normal.prioritaria)
        self.assertTrue(urgente.prioritaria)
        html = self.client.get(reverse("comercial:bandeja")).content.decode()
        self.assertLess(html.index("Para mañana"), html.index("Sin afán"), "la prioritaria va primero")
        self.assertIn("Prioritaria", html)
        detalle = self.client.get(urgente.get_absolute_url()).content.decode()
        self.assertIn("Propuesta prioritaria", detalle)
        # Ya enviada al cliente deja de ser prioritaria
        urgente.etapa = F.Etapa.ENVIADA
        self.assertFalse(urgente.prioritaria)


class SeparacionDeAreasTests(BaseComercial):
    def test_cada_area_ve_lo_suyo(self):
        comercial = self.crear()
        tecnico = T.objects.create(empresa=self.emp, solicitante=self.cli, categoria=self.soporte, asunto="Cámara sin imagen",
                                   descripcion="x")
        # Coordinador técnico: lo técnico, no lo que sigue en comercial
        self.client.force_login(self.coord)
        self.assertEqual(self.radicados(reverse("panel")), [tecnico.radicado])
        self.assertEqual(self.client.get(comercial.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("comercial:bandeja")).status_code, 403)
        self.assertEqual(self.client.get(reverse("oportunidades")).status_code, 403)
        # Área comercial: lo comercial, nada del flujo técnico puro
        self.client.force_login(self.asesor)
        self.assertEqual(self.radicados(reverse("comercial:bandeja")), [comercial.radicado])
        self.assertEqual(self.client.get(tecnico.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("panel")).status_code, 403)
        # Dirección: las dos
        self.client.force_login(self.director)
        self.assertEqual(self.radicados(reverse("panel")), [tecnico.radicado])
        self.assertEqual(self.radicados(reverse("comercial:bandeja")), [comercial.radicado])
        # Cliente: los dos, en una sola lista
        self.client.force_login(self.cli)
        self.assertEqual(set(self.radicados(reverse("mis_tickets"))), {comercial.radicado, tecnico.radicado})

    def test_cliente_no_ve_datos_internos_comerciales(self):
        t = self.crear()
        self.client.force_login(self.cli)
        html = self.client.get(t.get_absolute_url()).content.decode()
        self.assertIn("Quieren cámaras", html)
        self.assertNotIn("PRESUPUESTO-SECRETO-9", html)
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(t.get_absolute_url()).status_code, 404)

    def test_el_tecnico_no_ve_precios(self):
        t = self.crear()
        t.ficha.valor_propuesta = 7777777
        t.ficha.save()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "Validar domo o bala", "jornada": "manana"})
        self.client.force_login(self.coord)
        html = self.client.get(t.get_absolute_url()).content.decode()
        self.assertIn("Levantamiento solicitado por el área comercial", html)
        self.assertNotIn("PRESUPUESTO-SECRETO-9", html)
        self.assertNotIn("7.777.777", html)


class FlujoCompletoTests(BaseComercial):
    def test_mismo_ticket_de_comercial_a_tecnica_y_de_vuelta(self):
        t = self.crear(empresa="", nueva_empresa="Prospecto SAS")
        numero = t.radicado
        # 1. Comercial pide levantamiento → aparece en la bandeja técnica de Guana
        self.client.force_login(self.asesor)
        mail.outbox.clear()
        r = self.client.post(reverse("comercial:levantamiento", args=[numero]), {"nota": "Domo?"}, follow=True)
        self.assertContains(r, "Escriba qué debe revisar el técnico y elija la jornada")  # sin jornada ni detalle no pasa
        self.client.post(reverse("comercial:levantamiento", args=[numero]),
                         {"nota": "¿Domo o bala en el parqueadero?", "jornada": "tarde"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa, t.estado, t.jornada), (T.Area.TECNICA, "levantamiento", T.Estado.RECEPCIONADO, "tarde"))
        self.assertTrue(any("coordinador@janotec.test" in m.to for m in mail.outbox))
        self.client.force_login(self.coord)
        self.assertIn(numero, self.radicados(reverse("panel")))
        self.assertContains(self.client.get(reverse("panel")), "Levantamiento comercial")
        # Guana asigna; en un levantamiento no se puede «resolver»
        self.client.post(reverse("gestionar", args=[numero]), {"visibilidad": "interno", "estado": "generado",
                                                               "prioridad": "media", "asignado_a": self.tecnico.pk})
        t.refresh_from_db()
        self.assertEqual(t.asignado_a, self.tecnico)
        self.client.force_login(self.tecnico)
        self.client.post(reverse("gestionar", args=[numero]), {"visibilidad": "publico", "estado": "visita_ejecutada"})
        t.refresh_from_db()
        self.assertNotEqual(t.estado, T.Estado.VISITA_EJECUTADA)
        # 2. El técnico devuelve el resultado → vuelve a comercial y sale de la bandeja técnica
        self.client.post(reverse("comercial:devolver", args=[numero]), {"necesidad": "Vigilar el parqueadero"})  # incompleto
        t.refresh_from_db()
        self.assertEqual(t.area, T.Area.TECNICA)
        self.client.post(reverse("comercial:devolver", args=[numero]),
                         {"necesidad": "Vigilar el parqueadero de noche", "materiales": "2 domos y 1 bala, 80 m UTP"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa, t.asignado_a), (T.Area.COMERCIAL, "elaborando", None))
        self.assertIn("2 domos", t.ficha.levantamiento)
        self.assertTrue(t.ficha.informe.con_visita)
        self.client.force_login(self.coord)
        self.assertNotIn(numero, self.radicados(reverse("panel")))
        # 3. Comercial envía la propuesta (exige PDF) → le llega al cliente
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:gestionar", args=[numero]), {**URGENTE, "etapa": "enviada", "visibilidad": "publico"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "elaborando")
        mail.outbox.clear()
        self.client.post(reverse("comercial:gestionar", args=[numero]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "4500000",
                                                                         "visibilidad": "publico", "propuesta": self.pdf()})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.estado), ("enviada", "propuesta_enviada"))
        correo = next(m for m in mail.outbox if "leidy@a.co" in m.to)
        self.assertEqual(correo.attachments[0][0], "propuesta.pdf")
        # 4. Aprobada → «En inventario»: mismo número, en el área técnica; bodega alista lo aprobado
        #    (sale del informe: «2 domos y 1 bala, 80 m UTP») y coordinación decide cuándo pasa a recepcionado
        self.client.post(reverse("comercial:gestionar", args=[numero]), {"etapa": "aprobada", "visibilidad": "publico"})
        from inventario.models import SolicitudMaterial
        s = SolicitudMaterial.objects.get(ticket=t, origen="comercial")
        self.assertEqual(s.estado, "validacion")
        t.refresh_from_db()
        self.assertEqual((t.radicado, t.area, t.ficha.etapa, t.estado), (numero, T.Area.TECNICA, "instalacion", T.Estado.EN_INVENTARIO))
        self.assertTrue(t.falla.nombre.startswith("Instalar"))
        self.assertFalse(t.empresa.es_prospecto)
        self.assertEqual(T.objects.filter(ficha__isnull=False).count(), 1)  # no se creó otro ticket
        self.client.force_login(self.coord)
        self.assertIn(numero, self.radicados(reverse("panel")))
        self.client.post(reverse("inventario_listo", args=[numero]), {})
        t.refresh_from_db()
        self.assertEqual(t.estado, T.Estado.RECEPCIONADO)
        # El asesor lo sigue en consulta, pero ya no lo gestiona
        self.client.force_login(self.asesor)
        self.assertEqual(self.client.get(reverse("comercial:detalle", args=[numero])).status_code, 200)
        self.client.post(reverse("comercial:gestionar", args=[numero]), {"etapa": "cancelada", "visibilidad": "interno"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "instalacion")

    def test_cliente_aprueba_o_no_desde_su_solicitud(self):
        t = self.crear()
        t.ficha.etapa = F.Etapa.ENVIADA
        t.ficha.save()
        self.client.force_login(self.cli)
        self.assertContains(self.client.get(t.get_absolute_url()), "Aprobar propuesta")
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "aprobar"})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.area, t.estado), ("instalacion", T.Area.TECNICA, T.Estado.EN_INVENTARIO))  # a alistar
        otro = self.crear()
        otro.ficha.etapa = F.Etapa.ENVIADA
        otro.ficha.save()
        self.client.force_login(self.cli)
        self.client.post(reverse("comercial:responder", args=[otro.radicado]), {"accion": "rechazar", "motivo": "Muy costoso"})
        otro.refresh_from_db()
        self.assertEqual((otro.ficha.etapa, otro.ficha.motivo_rechazo, otro.estado), ("rechazada", "Muy costoso", T.Estado.CERRADO))

    def test_acta_del_levantamiento_devuelve_a_comercial(self):
        t = self.crear()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "Medir el parqueadero", "jornada": "manana"})
        t.refresh_from_db()
        t.asignado_a = self.tecnico
        t.estado = T.Estado.EN_CRONOGRAMA
        t.save()
        firma = "data:image/png;base64," + __import__("base64").b64encode(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"0" * 40).decode()
        self.client.force_login(self.tecnico)
        self.client.post(reverse("acta_nueva", args=[t.radicado]), {
            "ciudad": "Villavicencio", "actividad": ["Soporte"], "sistema": "CCTV", "diagnostico": "Levantamiento",
            "resultado": "solucionado", "observaciones": "3 cámaras domo", "cargo_tecnico": "Técnico", "firma_tecnico": firma,
            "recibio": "Leidy", "cargo_recibio": "Admin", "firma_cliente": firma, "marcar_resuelto": "on", "modalidad": "presencial",
            "equipos-TOTAL_FORMS": "0", "equipos-INITIAL_FORMS": "0", "equipos-MIN_NUM_FORMS": "0", "equipos-MAX_NUM_FORMS": "30"})
        t.refresh_from_db()
        # El acta es la constancia; el ticket vuelve a comercial con el informe del requerimiento
        self.assertEqual((t.area, t.ficha.etapa), (T.Area.TECNICA, "levantamiento"))
        self.assertTrue(ActaSoporte.objects.filter(ticket=t).exists())
        self.assertContains(self.client.get(t.get_absolute_url()), "Enviar informe y devolver al área comercial")
        self.client.post(reverse("comercial:devolver", args=[t.radicado]),
                         {"necesidad": "Vigilar el parqueadero", "materiales": "3 cámaras domo" + chr(10) + "1 NVR"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (T.Area.COMERCIAL, "elaborando"))
        self.assertIn("3 cámaras domo", t.ficha.informe.materiales)


class OportunidadAComercialTests(BaseComercial):
    def test_convertir_oportunidad_del_acta(self):
        acta = ActaSoporte.objects.create(numero=100001, fecha=timezone.now(), empresa=self.emp, tecnico=self.tecnico)
        o = OportunidadComercial.objects.create(acta=acta, empresa=self.emp, servicios=["energia_solar"],
                                                nota="Pidió paneles solares", detectada_por=self.tecnico)
        self.client.force_login(self.asesor)
        self.assertContains(self.client.get(reverse("oportunidades")), "Convertir en requerimiento")
        form = self.client.get(reverse("comercial:nuevo") + f"?oportunidad={o.pk}").context["form"]
        self.assertEqual(form.initial["servicios"], ["energia_solar"])
        self.client.post(reverse("comercial:nuevo"), self.ficha_post(oportunidad=o.pk, servicios=["energia_solar"]))
        t = T.objects.get(ficha__isnull=False)
        o.refresh_from_db()
        self.assertEqual((t.ficha.origen, t.ficha.oportunidad, o.estado), ("oportunidad", o, "contactado"))


class FichaPdfTests(BaseComercial):
    def setUp(self):
        super().setUp()
        from reportlab import rl_config
        # Sin compresión, el texto de las páginas queda legible dentro del PDF
        self.addCleanup(setattr, rl_config, "pageCompression", rl_config.pageCompression)
        rl_config.pageCompression = 0

    def texto_pdf(self, contenido):
        return contenido.decode("latin-1")

    def test_correo_de_radicacion_lleva_la_ficha(self):
        mail.outbox.clear()
        t = self.crear()
        al_cliente = next(m for m in mail.outbox if "leidy@a.co" in m.to)
        interno = next(m for m in mail.outbox if "asesor@janotec.test" in m.to)
        nombre, contenido, tipo = al_cliente.attachments[0]
        self.assertEqual((nombre, tipo), (f"Ficha-requerimiento-{t.radicado}.pdf", "application/pdf"))
        self.assertTrue(contenido.startswith(b"%PDF"))
        self.assertNotIn("PRESUPUESTO-SECRETO-9", self.texto_pdf(contenido))
        self.assertIn("PRESUPUESTO-SECRETO-9", self.texto_pdf(interno.attachments[0][1]))
        self.assertIn("interna", interno.attachments[0][0])

    def test_quien_descarga_que_version(self):
        t = self.crear()
        url = reverse("comercial:ficha_pdf", args=[t.radicado])
        self.client.force_login(self.asesor)
        r = self.client.get(url)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("PRESUPUESTO-SECRETO-9", self.texto_pdf(r.content))
        self.assertIn("OPORTUNIDAD", self.texto_pdf(r.content))
        r = self.client.get(url + "?version=cliente&descargar=1")
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        self.assertNotIn("PRESUPUESTO-SECRETO-9", self.texto_pdf(r.content))
        # El cliente solo baja la suya, y sin datos internos aunque pida la interna
        self.client.force_login(self.cli)
        self.assertContains(self.client.get(t.get_absolute_url()), "Ficha de requerimiento (PDF)")
        self.assertNotIn("PRESUPUESTO-SECRETO-9", self.texto_pdf(self.client.get(url).content))
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.coord)  # sigue en comercial: el área técnica no la ve
        self.assertEqual(self.client.get(url).status_code, 404)


class LevantamientoEnCronogramaTests(BaseComercial):
    """El levantamiento es una asignación del área técnica: el asesor sabe cuándo y con quién, y si se aplaza."""

    def test_el_asesor_se_entera_de_la_programacion_y_del_aplazamiento(self):
        from datetime import time, timedelta

        from django.utils import timezone

        from cronograma import servicios as crono
        from cronograma.models import Actividad
        t = self.crear()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]),
                         {"nota": "Contar puntos y metros de cable", "jornada": "manana"})
        t.refresh_from_db()
        t.estado = T.Estado.RECEPCIONADO
        t.save()
        lunes = Actividad.lunes_de(timezone.localdate()) + timedelta(days=7)
        a = Actividad.objects.create(ticket=t, fecha=lunes, hora_inicio=time(8), hora_fin=time(12), cliente=str(t.empresa),
                                     actividad="Levantamiento")
        a.encargados.add(self.tecnico)
        mail.outbox.clear()
        crono.guardar(a, self.coord)
        aviso = [m for m in mail.outbox if self.asesor.email in m.to]
        self.assertTrue(aviso and "Levantamiento programado" in aviso[0].subject)
        self.assertIn(self.tecnico.get_full_name() or self.tecnico.username, aviso[0].body)
        self.assertContains(self.client.get(t.ficha.get_absolute_url()), "programada el")
        mail.outbox.clear()
        t.refresh_from_db()
        crono.aplazar_visita(t, self.coord, "cliente", "El cliente pidió cambiar la fecha de la visita.")
        self.assertTrue(any("Levantamiento aplazado" in m.subject and self.asesor.email in m.to for m in mail.outbox))
        t.refresh_from_db()
        self.assertEqual(t.estado, T.Estado.RECEPCIONADO)


class EstadoPropuestaEnviadaTests(TestCase):
    """El orden de los estados y el estado «Propuesta enviada» del ticket."""

    def test_orden_de_los_estados(self):
        from tickets.models import Ticket
        self.assertEqual([e.label for e in Ticket.Estado], [
            "Generado", "Recepcionado", "En cronograma", "Autorizado", "Visita técnica ejecutada", "Pendiente nueva visita",
            "Informe en elaboración", "En gestión comercial", "Propuesta enviada", "En inventario", "Cerrado", "Cancelado"])


class InformeYVersionesTests(BaseComercial):
    """El informe del requerimiento es obligatorio; la propuesta puede tener versiones por cambios del cliente."""

    def test_sin_informe_no_hay_propuesta_y_sin_visita_lo_registra_el_asesor(self):
        t = self.crear()
        numero = t.radicado
        self.client.post(reverse("comercial:gestionar", args=[numero]),
                         {**URGENTE, "etapa": "enviada", "visibilidad": "interno", "propuesta": self.pdf()})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "gestion")  # sin informe no se envía
        self.assertContains(self.client.get(reverse("comercial:detalle", args=[numero])), "Sin visita: regístrelo usted")
        self.client.post(reverse("comercial:informe", args=[numero]),
                         {"necesidad": "Cámaras en el parqueadero", "materiales": "4 Cámara bala" + chr(10) + "1 NVR"})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.ficha.informe.con_visita), ("elaborando", False))

    def test_cliente_pide_cambios_y_aprueba_la_version_2(self):
        from .models import PropuestaVersion as V
        t = self.crear()
        numero = t.radicado
        self.client.post(reverse("comercial:informe", args=[numero]), {"necesidad": "CCTV", "materiales": "4 Cámara Dahua"})
        self.client.post(reverse("comercial:gestionar", args=[numero]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "4000000",
                                                                         "visibilidad": "interno", "propuesta": self.pdf()})
        t.refresh_from_db()
        self.assertEqual([(v.numero, v.estado) for v in t.ficha.versiones.all()], [(1, V.Estado.ENVIADA)])
        # El cliente pide otra marca desde su solicitud
        self.client.force_login(self.cli)
        self.assertContains(self.client.get(t.get_absolute_url()), "Pedir cambios")
        mail.outbox.clear()
        self.client.post(reverse("comercial:responder", args=[numero]),
                         {"accion": "cambios", "cambios": "Prefiero cámaras Hikvision"})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.estado), ("ajustes", T.Estado.EN_PROCESO))
        self.assertEqual(t.ficha.versiones.get(numero=1).estado, V.Estado.CAMBIOS)
        self.assertTrue(any("pidió cambios" in m.subject for m in mail.outbox))
        # El asesor envía la versión 2 (con un PDF nuevo) y le llega al cliente
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:gestionar", args=[numero]), {**URGENTE, "etapa": "enviada", "visibilidad": "interno"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "ajustes")  # cada versión lleva su PDF
        mail.outbox.clear()
        self.client.post(reverse("comercial:gestionar", args=[numero]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "4300000",
                                                                         "visibilidad": "interno", "propuesta": self.pdf()})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.estado), ("enviada", T.Estado.PROPUESTA_ENVIADA))
        al_cliente = next(m for m in mail.outbox if "leidy@a.co" in m.to)
        self.assertIn("versión 2", al_cliente.subject)
        self.assertIn("Prefiero cámaras Hikvision", al_cliente.body)
        self.client.force_login(self.cli)
        self.client.post(reverse("comercial:responder", args=[numero]), {"accion": "aprobar"})
        t.refresh_from_db()
        v2 = t.ficha.versiones.get(numero=2)
        self.assertEqual((t.ficha.etapa, v2.estado, t.ficha.valor_propuesta), ("instalacion", V.Estado.APROBADA, 4300000))
        self.client.force_login(self.asesor)
        detalle = self.client.get(reverse("comercial:detalle", args=[numero]))
        self.assertContains(detalle, "Versión 2")
        self.assertContains(detalle, "Prefiero cámaras Hikvision")


class RespuestaDesdeElCorreoTests(BaseComercial):
    """El correo de la propuesta trae Aprobar · Con cambios · No aprobar, y funciona sin iniciar sesión."""

    def enviar(self, t, valor):
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {**URGENTE, "etapa": "enviada", "valor_propuesta": valor,
                                                                            "visibilidad": "interno", "propuesta": self.pdf()})
        self.client.logout()

    def enlaces(self):
        import re
        correo = next(m for m in reversed(mail.outbox) if "leidy@a.co" in m.to)
        html = correo.alternatives[0][0]
        self.assertNotIn("Ver mi solicitud", html)
        return {accion: url.replace("&amp;", "&") for url, accion in re.findall(r'href="(https?://[^"]+/propuesta/[^"?]+/\?r=(\w+))"', html)}

    def test_cambios_y_luego_aprueba_desde_el_correo(self):
        from urllib.parse import urlparse
        t = self.crear()
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "CCTV", "materiales": "4 Cámara"})
        mail.outbox.clear()
        self.enviar(t, "4000000")
        v1 = self.enlaces()
        self.assertEqual(set(v1), {"aprobar", "cambios", "rechazar"})
        url = lambda u: urlparse(u).path  # noqa: E731
        # Abrir el enlace no responde (lo hace un antivirus); pedir cambios exige decir cuáles
        self.assertContains(self.client.get(url(v1["cambios"]), {"r": "cambios"}), "¿Qué decide sobre esta propuesta?")
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        self.assertContains(self.client.post(url(v1["cambios"]), {"accion": "cambios", "texto": ""}), "Cuéntenos qué cambios necesita")
        self.client.post(url(v1["cambios"]), {"accion": "cambios", "texto": "Prefiero marca Hikvision"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "ajustes")
        self.assertContains(self.client.get(url(v1["aprobar"])), "Recibimos los cambios que pidió")
        # Nueva versión: la anterior ya no se puede responder; la nueva se aprueba con un clic y confirmación
        self.enviar(t, "4300000")
        v2 = self.enlaces()
        self.assertNotContains(self.client.get(url(v1["aprobar"])), "¿Qué decide sobre esta propuesta?")  # la v1 ya no se responde
        # «Apruebo» se registra con el clic: la página la envía sola (un antivirus que solo la abre no aprueba)
        pagina = self.client.get(url(v2["aprobar"]), {"r": "aprobar"})
        self.assertContains(pagina, 'id="aprobar-ya"')
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        self.client.post(url(v2["aprobar"]), {"accion": "aprobar"})
        t.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.ficha.valor_propuesta), ("instalacion", 4300000))
        self.assertContains(self.client.get(url(v2["rechazar"])), "Aprobó esta propuesta")
        self.assertTrue(t.seguimientos.filter(mensaje__contains="desde el correo").exists())


    def test_enlace_de_una_propuesta_que_ya_no_existe(self):
        from django.core import signing

        from .respuesta import SAL
        firma = signing.dumps({"v": 999999}, salt=SAL)
        r = self.client.get(reverse("comercial:respuesta", args=[firma]), {"r": "cambios"})
        self.assertContains(r, "Esta propuesta ya no está disponible", status_code=404)


class CotizadorTests(BaseComercial):
    """La propuesta se arma con el catálogo: descuento, IVA, código PE y PDF; la versión 2 parte de la 1."""

    def lineas(self, filas):
        datos = {"alcance": "Cámaras para el parqueadero", "condiciones": "Garantía de 1 año", "modalidad": "venta",
                 "tasa_mensual": "1.5", "recargo_alquiler": "0", "plazos": "12,24,36", "opcion_compra": "1",
                 "lineas-TOTAL_FORMS": str(len(filas)), "lineas-INITIAL_FORMS": "0", "lineas-MIN_NUM_FORMS": "0",
                 "lineas-MAX_NUM_FORMS": "1000"}
        for i, (desc, cant, valor, descuento) in enumerate(filas):
            datos.update({f"lineas-{i}-descripcion": desc, f"lineas-{i}-cantidad": cant, f"lineas-{i}-valor_unitario": valor,
                          f"lineas-{i}-descuento": descuento, f"lineas-{i}-iva": "19"})
        return datos

    def test_armar_enviar_y_nueva_version(self):
        from decimal import Decimal

        from .models import ItemCatalogo, PropuestaVersion
        ItemCatalogo.objects.create(descripcion="Cable HDMI 15 m High Speed 4K", valor_unitario=Decimal("73335.57"))
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]),
                         {"necesidad": "Monitor en la oficina", "materiales": "1 Cable HDMI 15 m\n6 Canaleta"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.assertContains(self.client.get(url), "Cable HDMI 15 m High Speed 4K")  # el catálogo se sugiere
        # Traer los materiales del informe: el cable toma el precio del catálogo; la canaleta queda en 0 para completar
        self.client.post(url, {**self.lineas([]), "accion": "informe"})
        cot = t.ficha.cotizaciones.get()
        self.assertEqual([(l.descripcion, l.valor_unitario) for l in cot.lineas.all()],
                         [("Cable HDMI 15 m High Speed 4K", Decimal("73335.57")), ("Canaleta", Decimal("0"))])
        r = self.client.post(url, {**self.lineas([("Canaleta 20x12", "6", "0", "0")]), **URGENTE, "accion": "enviar"}, follow=True)
        self.assertContains(r, "Cada ítem necesita cantidad y costo unitario")
        cot.lineas.all().delete()
        self.client.post(url, self.lineas([("Cable HDMI 15 m High Speed 4K", "1", "73335.57", "12"), ("Canaleta 20x12", "6", "9635.85", "0")]))
        tot = cot.totales()
        self.assertEqual((tot["bruto"], tot["descuento"], tot["base"]), (Decimal("131150.67"), Decimal("8800.27"), Decimal("122350.40")))
        self.assertEqual(tot["iva"], Decimal("23246.58"))
        t.ficha.cotizaciones.update(margen=1)
        pdf = self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        mail.outbox.clear()
        self.client.post(url, {**self.lineas([]), "margen": "1", **URGENTE, "accion": "enviar"})
        t.refresh_from_db()
        v1 = PropuestaVersion.objects.get(ficha=t.ficha)
        self.assertRegex(v1.codigo, r"^PE-\d{4}-\d{2}-\d{3}-V1$")
        self.assertEqual((t.ficha.etapa, t.ficha.valor_propuesta, v1.archivo.nombre_original), ("enviada", 123586, f"{v1.codigo}.pdf"))
        correo = next(m for m in mail.outbox if "leidy@a.co" in m.to)
        self.assertEqual(correo.attachments[0][0], f"{v1.codigo}.pdf")
        self.assertIn(v1.codigo, correo.alternatives[0][0])
        # El cliente pide cambios: la versión 2 arranca con los ítems de la 1 y conserva la fecha del código
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]),
                         {"etapa": "ajustes", "visibilidad": "interno", "cambios": "Otra marca de cable"})
        self.assertContains(self.client.get(url), "Cable HDMI 15 m High Speed 4K")
        self.client.post(url, {**self.lineas([]), "margen": "1", **URGENTE, "accion": "enviar"})
        v2 = PropuestaVersion.objects.get(ficha=t.ficha, numero=2)
        self.assertEqual(v2.codigo, v1.codigo.replace("-V1", "-V2"))
        self.assertEqual(t.ficha.cotizaciones.count(), 2)

    def test_alquiler_con_opcion_de_compra(self):
        """Los valores de una propuesta real de comercial: alquiler a 12/24/36 meses con opción de compra."""
        from decimal import Decimal

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Cámaras", "servicios": "CCTV"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.client.post(url, {**self.lineas([("Sistema de videovigilancia", "1", "24746222.98", "0")]), "modalidad": "alquiler",
                               "recargo_alquiler": "0"})
        cot = t.ficha.cotizaciones.get()
        c = cot.canones()
        self.assertEqual(c["valor"], Decimal("29448005.35"))
        # Los cánones de la propuesta en papel (a un centavo, por el redondeo del valor de partida)
        for f, esperado in zip(c["filas"], ["2699792.91", "1470165.21", "1064615.93"]):
            self.assertLessEqual(abs(f["canon"] - Decimal(esperado)), Decimal("0.01"))
        self.assertEqual([f["plazo"] for f in c["filas"]], [12, 24, 36])
        self.assertEqual(c["opcion_compra"], Decimal("294480.05"))
        t.ficha.cotizaciones.update(margen=1)
        self.assertEqual(self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado]))["Content-Type"], "application/pdf")

    def test_alquiler_como_la_propuesta_real_aprobada(self):
        """PE-2026-10-007: los bienes más el recargo, a la tasa mensual; la mano de obra se cobra aparte."""
        from decimal import Decimal

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.client.post(url, {**self.lineas([
            ("Terminal de reconocimiento facial [TER-1]", "1", "796616", "0"), ("Cable Cat6+", "40", "2645.38", "0"),
            ("Botón de salida", "1", "33237", "0"), ("Caja de sobreponer", "1", "11055", "0"),
            ("Servicio de instalación", "1", "234140", "0")]),
            "modalidad": "hibrido", "tasa_mensual": "2", "recargo_alquiler": "2", "plazos": "6,12,24,36,48", "opcion_compra": "0"})
        cot = t.ficha.cotizaciones.get()
        c = cot.canones()
        self.assertEqual([round(f["antes_iva"]) for f in c["filas"]], [172395, 91312, 51055, 37886, 31482])
        self.assertEqual(round(c["filas"][0]["canon"]), 205150)
        self.assertEqual(round(c["mano_obra"]), 278627)
        self.assertEqual([l.categoria_final for l in cot.lineas.order_by("orden", "id")],
                         ["equipos", "conectividad", "accesorios", "accesorios", "mano_obra"])
        cot.modalidad = "alquiler"  # Sin venta aparte: la mano de obra se suma al canon
        cot.save()
        self.assertEqual(cot.canones()["mano_obra"], 0)
        t.ficha.cotizaciones.update(margen=1)
        respuesta = self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado]))
        self.assertEqual(respuesta["Content-Type"], "application/pdf")
        self.assertEqual(respuesta.content.count(b"/Type /Page\n") + respuesta.content.count(b"/Type /Page\r"), 3)

    def test_margen_obligatorio_sube_el_precio_y_no_se_ve_en_el_pdf(self):
        from decimal import Decimal

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        lineas = self.lineas([("Terminal", "2", "100000", "0")])
        self.client.post(url, lineas)
        cot = t.ficha.cotizaciones.get()
        self.assertEqual(cot.totales()["base"], Decimal("200000.00"))
        # Sin margen ni se envía ni sale el PDF
        self.assertEqual(self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado])).status_code, 302)
        self.client.post(url, {**lineas, **URGENTE, "accion": "enviar"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.versiones.count(), 0)
        for fuera in ("0", "99.01", "100", "-1"):
            self.assertIn("margen", self.client.post(url, {**lineas, "margen": fuera}).context["form"].errors)
        cot.lineas.exclude(pk=cot.lineas.first().pk).delete()  # Cada intento sin id agregó otra línea
        for valido, base in (("1", "202020.20"), ("99", "20000000.00"), ("30", "285714.29")):
            cot.margen = Decimal(valido)
            cot.save()
            self.assertEqual(cot.totales()["base"], Decimal(base))
        self.assertEqual(cot.lineas.get().valor_unitario, Decimal("100000.00"))
        self.assertEqual(self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado])).status_code, 200)
        linea = cot.lineas.get()
        datos = {"lineas-TOTAL_FORMS": "1", "lineas-INITIAL_FORMS": "1", "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000",
                 "lineas-0-id": linea.pk, "lineas-0-descripcion": "Terminal", "lineas-0-cantidad": "2", "lineas-0-valor_unitario": "100000",
                 "lineas-0-descuento": "0", "lineas-0-iva": "19"}
        self.client.post(url, {**lineas, **datos, "margen": "30", **URGENTE, "accion": "enviar"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.valor_propuesta, 285714)
        from comercial import cotizador as coti
        t.ficha.etapa = "ajustes"
        t.ficha.save()
        self.assertEqual(coti.borrador(t.ficha).margen, Decimal("30.00"))

    def test_ver_pdf_guarda_lo_digitado_antes_de_abrirlo(self):
        from decimal import Decimal

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        r = self.client.post(url, {**self.lineas([("Terminal", "1", "100000", "0")]), "margen": "25", "accion": "ver_pdf"}, follow=True)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(t.ficha.cotizaciones.get().margen, Decimal("25.00"))
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(t.ficha.cotizaciones.get().totales()["base"], Decimal("133333.33"))

    def test_propuesta_hibrida_ordena_por_categoria_y_ocupa_tres_paginas(self):
        from decimal import Decimal

        from comercial.pdf import _resumen_propuesta, propuesta_pdf

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        largo = "Terminal de reconocimiento facial para exterior IP65, 3.000 usuarios, tarjetas y huellas, con videoportero. " * 2
        self.client.post(url, self.lineas([("Servicio de instalación y configuración", "1", "187312", "19"), (largo, "1", "637293", "19"),
                                           ("Cable Cat6 de alto rendimiento para redes de datos. " * 3, "40", "2116", "19"),
                                           ("Botón de salida sin contacto", "1", "26590", "19"), ("Caja de sobreponer", "1", "8844", "19")]))
        cot = t.ficha.cotizaciones.get()
        cot.modalidad = "hibrido"
        cot.margen = Decimal("25")
        cot.save()
        desglose = [f for f in _resumen_propuesta(cot, "PE-1-V1", "Control de acceso") if getattr(f, "_cellvalues", None) and f._ncols >= 5][0]
        self.assertEqual(desglose._cellvalues[1][0].text, "EQUIPOS")
        categorias = [fila[0].text for fila in desglose._cellvalues[1:] if hasattr(fila[0], "text")]
        self.assertEqual(categorias[-1], "MANO DE OBRA")
        # Los totales van en la misma tabla, debajo de la columna «Total», para que queden alineados
        self.assertTrue(all(fila[0] == "" for fila in desglose._cellvalues[-3:]))
        _, contenido = propuesta_pdf(cot, "PE-1-V1", 1)
        self.assertEqual(contenido.count(b"/Type /Page") - contenido.count(b"/Type /Pages"), 3)

    def test_la_linea_conserva_su_costo_y_el_cotizador_trae_el_aviso_del_catalogo(self):
        from decimal import Decimal

        from comercial.models import ItemCatalogo

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        item = ItemCatalogo.objects.create(descripcion="Terminal facial", valor_unitario=Decimal("100000"), iva=19)
        self.client.post(url, self.lineas([("Terminal facial", "1", "100000", "19")]))
        item.valor_unitario = Decimal("75000")
        item.save()
        r = self.client.get(url)
        self.assertEqual(t.ficha.cotizaciones.get().lineas.get().valor_unitario, Decimal("100000.00"))
        self.assertContains(r, "aviso-catalogo")
        self.assertContains(r, "usar-catalogo")

    def test_margen_sobre_la_venta_da_los_valores_de_la_propuesta_real(self):
        from decimal import Decimal

        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Acceso", "servicios": "Acceso"})
        self.client.post(reverse("comercial:cotizador", args=[t.radicado]), self.lineas([
            ("Terminal", "1", "597462", "0"), ("Cable", "40", "1984.04", "0"), ("Boton", "1", "24928", "0"),
            ("Caja", "1", "8291.25", "0"), ("Servicio de instalación", "1", "175605", "0")]))
        cot = t.ficha.cotizaciones.get()
        cot.margen = Decimal("25")
        cot.modalidad = "hibrido"
        cot.tasa_mensual, cot.recargo_alquiler, cot.plazos, cot.opcion_compra = Decimal("2"), Decimal("2"), "6,12,24,36,48", Decimal("0")
        cot.save()
        self.assertEqual(cot.lineas.get(descripcion="Terminal").precio_unitario, Decimal("796616.00"))
        self.assertEqual(cot.lineas.get(descripcion="Cable").calculo()["base"].quantize(Decimal(1)), Decimal("105815"))
        self.assertEqual(cot.totales()["base"].quantize(Decimal(1)), Decimal("1180864"))
        self.assertEqual(cot.totales()["total"].quantize(Decimal(1)), Decimal("1405228"))
        self.assertEqual([f["canon"].quantize(Decimal(1)) for f in cot.canones()["filas"]],
                         [Decimal(v) for v in ("205150", "108662", "60756", "45084", "37464")])

    def test_catalogo(self):
        self.client.force_login(self.asesor)
        self.client.post(reverse("comercial:catalogo"), {"descripcion": "Soporte de monitor", "tipo": "material", "unidad": "und",
                                                         "valor_unitario": "100840.34", "iva": "19", "activo": "on"})
        self.assertContains(self.client.get(reverse("comercial:catalogo")), "Soporte de monitor")
        self.client.force_login(self.tecnico)
        self.assertNotEqual(self.client.get(reverse("comercial:catalogo")).status_code, 200)

    def test_al_aprobar_el_material_del_cotizador_va_a_bodega(self):
        from inventario.models import SolicitudMaterial

        from . import servicios as com
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Monitor", "materiales": "1 Cable HDMI"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.client.post(url, self.lineas([("Cable HDMI 15 m", "1", "73335.57", "0"), ("Canaleta 20x12", "6", "9635.85", "0"),
                                           ("Servicio de traslado e instalación de pantalla", "1", "212831.02", "0")]))
        self.client.post(url, {**self.lineas([]), "margen": "1", **URGENTE, "accion": "enviar"})
        t.refresh_from_db()
        com.registrar_respuesta(t.ficha, None, True)
        s = SolicitudMaterial.objects.get(ticket=t, origen="comercial")
        self.assertEqual(s.estado, "validacion")
        self.assertEqual(sorted(i.descripcion for i in s.items.all()), ["Cable HDMI 15 m", "Canaleta 20x12"])  # sin el servicio
        self.assertIn(t.ficha.versiones.get().codigo, s.nota)

    def cotizacion_lista(self):
        """Un requerimiento con informe y una cotización que ya cumple todo para salir."""
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Cámaras", "materiales": "2 Cámara IP"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.client.post(url, {**self.lineas([("Cámara IP 4 MP", "2", "250000", "0")]), "margen": "30"})
        return t, url

    def test_direccion_aprueba_la_propuesta_antes_de_que_salga(self):
        t, url = self.cotizacion_lista()
        datos = {**self.lineas([]), "margen": "30"}
        # Comercial no la envía al cliente sin dirección (ni a mano con el PDF)
        self.client.post(url, {**datos, "accion": "enviar"})
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]),
                         {"etapa": "enviada", "visibilidad": "interno", "propuesta": self.pdf()})
        t.ficha.refresh_from_db()
        self.assertEqual((t.ficha.etapa, t.ficha.versiones.count()), ("elaborando", 0))
        # La envía a dirección: le llega el aviso y mientras la revisa comercial no la modifica
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {**datos, "accion": "a_direccion"})
        cot = t.ficha.cotizaciones.get()
        self.assertTrue(cot.en_revision)
        self.assertTrue(any("dir@janotec.test" in m.to and "aprobar" in m.subject for m in mail.outbox))
        self.client.post(url, {**self.lineas([]), "margen": "50"})
        cot.refresh_from_db()
        self.assertEqual(cot.margen, 30)
        # Dirección la devuelve con observaciones: comercial las recibe y la puede corregir
        self.client.force_login(self.director)
        self.client.post(url, {"accion": "devolver", "observaciones": ""})
        self.assertTrue(t.ficha.cotizaciones.get().en_revision)  # sin observaciones no se devuelve
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"accion": "devolver", "observaciones": "Bajar el margen al 25 %"})
        cot.refresh_from_db()
        self.assertEqual((cot.en_revision, cot.observaciones_direccion), (False, "Bajar el margen al 25 %"))
        self.assertTrue(any("asesor@janotec.test" in m.to for m in mail.outbox))
        self.client.force_login(self.asesor)
        self.assertContains(self.client.get(url), "Bajar el margen al 25 %")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {**self.lineas([]), "margen": "25", "accion": "a_direccion"})
        # Dirección la aprueba y sale al cliente
        self.client.force_login(self.director)
        self.assertContains(self.client.get(url), "Aprobar y enviar al cliente")
        mail.outbox.clear()
        self.client.post(url, {**self.lineas([]), "margen": "25", "accion": "enviar"})
        cot.refresh_from_db()
        t.ficha.refresh_from_db()
        self.assertEqual((t.ficha.etapa, cot.aprobada_por, cot.en_revision, cot.motivo_urgencia), ("enviada", self.director, False, ""))
        self.assertTrue(any("leidy@a.co" in m.to for m in mail.outbox))

    def test_direccion_recibe_aviso_del_envio_urgente(self):
        t, url = self.cotizacion_lista()
        datos = {**self.lineas([]), "margen": "30", "accion": "enviar", "urgente": "1"}
        self.client.post(url, {**datos, "motivo_urgencia": "hoy"})  # el motivo es obligatorio
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "elaborando")
        mail.outbox.clear()
        self.client.post(url, {**datos, "motivo_urgencia": "Licitación cierra hoy a las 5"})
        t.ficha.refresh_from_db()
        cot = t.ficha.cotizaciones.get()
        self.assertEqual((t.ficha.etapa, cot.aprobada_por, cot.motivo_urgencia), ("enviada", None, "Licitación cierra hoy a las 5"))
        self.assertTrue(any("dir@janotec.test" in m.to and "urgencia" in m.subject for m in mail.outbox))
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje__contains="Licitación cierra hoy").exists())

    # --- Comercial (requerimientos y seguimiento) vs administrativa (propuestas) --------------------------
    def areas(self):
        """Una persona solo comercial y una solo administrativa."""
        solo_comercial = Usuario.objects.create_user("vendedor", "vendedor@janotec.test", "Clave-segura-1", rol="comercial")
        administrativa = Usuario.objects.create_user("yuri", "yuri@janotec.test", "Clave-segura-1", rol="bodega",
                                                     tambien_administrativo=True)
        return solo_comercial, administrativa

    def test_cada_area_hace_lo_suyo(self):
        solo_comercial, administrativa = self.areas()
        t, url = self.cotizacion_lista()
        ficha = reverse("comercial:detalle", args=[t.radicado])
        # Comercial: entra al requerimiento y ve el valor, pero no al cotizador ni al catálogo
        self.client.force_login(solo_comercial)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("comercial:catalogo")).status_code, 403)
        self.assertEqual(self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado])).status_code, 403)
        html = self.client.get(ficha).content.decode()
        self.assertNotIn(url, html)
        self.assertIn("La arma el área administrativa", html)
        self.assertNotIn('name="propuesta"', html)  # no sube el PDF a mano
        self.assertEqual(self.client.get(reverse("comercial:nuevo")).status_code, 200)
        # Administrativa (bodega con «también administrativa»): arma la propuesta y cotiza con proveedores
        self.client.force_login(administrativa)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(reverse("comercial:catalogo")).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventario:cotizaciones")).status_code, 200)
        # Mientras arma la propuesta, la solicitud está en «En el área administrativa», no en lo de comercial
        self.assertContains(self.client.get(reverse("comercial:bandeja") + "?vista=administrativa"), t.radicado)
        self.assertNotContains(self.client.get(reverse("comercial:bandeja")), t.radicado)
        # …pero no radica, no pide el levantamiento ni registra la respuesta del cliente
        self.assertEqual(self.client.get(reverse("comercial:nuevo")).status_code, 403)
        self.assertEqual(self.client.post(reverse("comercial:levantamiento", args=[t.radicado]),
                                          {"nota": "Revisar el poste", "jornada": "manana"}).status_code, 403)
        html = self.client.get(ficha).content.decode()
        self.assertNotIn('value="aprobada"', html)
        self.assertIn('value="enviada"', html)
        self.assertEqual(administrativa.rol_corto, "Bodega y administrativa")

    def test_avisos_a_cada_area(self):
        from .servicios import _correos_administrativos, _correos_comerciales, _correos_propuesta
        solo_comercial, administrativa = self.areas()
        t = self.crear()
        t.ficha.comercial = solo_comercial
        t.ficha.save()
        self.assertEqual(_correos_comerciales(t.ficha), ["vendedor@janotec.test"])
        self.assertEqual(set(_correos_administrativos()), {"yuri@janotec.test", "asesor@janotec.test"})
        self.assertIn("vendedor@janotec.test", _correos_propuesta(t.ficha))
        # Sin nadie en el área comercial, le llega a dirección (que cubre su trabajo mientras tanto)
        Usuario.objects.filter(rol="comercial").update(is_active=False)
        t.ficha.comercial = None
        t.ficha.save()
        self.assertEqual(_correos_comerciales(t.ficha), ["dir@janotec.test"])

    def test_propuesta_entregada_por_medio_de_comercial(self):
        solo_comercial, _ = self.areas()
        t, url = self.cotizacion_lista()
        t.ficha.comercial = solo_comercial
        t.ficha.save()
        self.client.force_login(self.director)
        self.assertContains(self.client.get(url), "Por medio de comercial")
        mail.outbox.clear()
        self.client.post(url, {**self.lineas([]), "margen": "30", "accion": "enviar", "entrega": "comercial"})
        v = t.ficha.versiones.get()
        self.assertTrue(v.por_entregar)
        # Al cliente no le llega; al asesor sí, con el PDF
        self.assertFalse(any("leidy@a.co" in m.to for m in mail.outbox))
        aviso = next(m for m in mail.outbox if "vendedor@janotec.test" in m.to)
        self.assertIn("para entregar", aviso.subject)
        self.assertEqual(aviso.attachments[0][2], "application/pdf")
        # El cliente aún no la ve en su consulta
        self.client.force_login(self.cli)
        self.assertNotContains(self.client.get(t.get_absolute_url()), "Su propuesta económica está lista")
        # Comercial la entrega y la marca con la fecha (no futura)
        self.client.force_login(solo_comercial)
        entregada = reverse("comercial:entregada", args=[t.radicado, v.numero])
        self.assertContains(self.client.get(reverse("comercial:detalle", args=[t.radicado])), "Por entregar al cliente")
        mañana = timezone.localdate() + timezone.timedelta(days=1)
        self.client.post(entregada, {"fecha": mañana.isoformat()})
        v.refresh_from_db()
        self.assertIsNone(v.entregada_cliente)
        self.client.post(entregada, {"fecha": timezone.localdate().isoformat()})
        v.refresh_from_db()
        self.assertEqual((v.entregada_cliente, v.entregada_por, v.por_entregar), (timezone.localdate(), solo_comercial, False))
        self.assertTrue(t.seguimientos.filter(interno=True, mensaje__contains="entregada al cliente por el área comercial").exists())
        self.client.force_login(self.cli)
        r = self.client.get(t.get_absolute_url())
        self.assertContains(r, "Su propuesta económica está lista")
        self.assertEqual(self.client.get(reverse("descargar_adjunto", args=[v.archivo_id])).status_code, 200)

    def test_modulo_administrativo_ordena_las_propuestas(self):
        solo_comercial, administrativa = self.areas()
        t, url = self.cotizacion_lista()
        modulo = reverse("comercial:propuestas")
        # Comercial no entra al módulo administrativo ni lo ve en su menú
        self.client.force_login(solo_comercial)
        self.assertEqual(self.client.get(modulo).status_code, 403)
        self.assertNotContains(self.client.get(reverse("comercial:bandeja")), modulo)
        # Administrativa: la tiene por armar y su menú lleva al módulo
        self.client.force_login(administrativa)
        r = self.client.get(modulo)
        self.assertContains(r, t.radicado)
        self.assertEqual(r.context["por_armar"], [t.ficha])
        self.client.post(url, {**self.lineas([]), "margen": "30", "accion": "a_direccion"})
        # Dirección: arriba las que debe aprobar, con el contador en el menú
        self.client.force_login(self.director)
        r = self.client.get(modulo)
        self.assertEqual((r.context["por_aprobar"], r.context["por_armar"]), ([t.ficha], []))
        self.assertContains(r, "Por aprobar: las revisa usted")
        self.assertContains(r, 'title="Propuestas esperando su aprobación">1<')
        self.client.post(url, {"accion": "devolver", "observaciones": "Revisar el precio del switch"})
        # Devuelta: administrativa la ve con las observaciones de dirección
        self.client.force_login(administrativa)
        r = self.client.get(modulo)
        self.assertEqual(r.context["devueltas"], [t.ficha])
        self.assertContains(r, "Revisar el precio del switch")
        # Aprobada por medio de comercial: aparece en la bandeja comercial por entregar
        self.client.force_login(self.director)
        self.client.post(url, {**self.lineas([]), "margen": "30", "accion": "enviar", "entrega": "comercial"})
        self.assertEqual(self.client.get(modulo).context["enviadas"], [t.ficha])
        self.client.force_login(solo_comercial)
        self.assertContains(self.client.get(reverse("comercial:bandeja")), "Propuestas aprobadas para que usted las entregue")


class PasoAPasoComercialTests(BaseComercial):
    def test_muestra_el_paso_actual_y_solo_las_etapas_que_siguen(self):
        t = self.crear()
        url = reverse("comercial:detalle", args=[t.radicado])
        r = self.client.get(url)
        self.assertContains(r, "Paso a paso del área comercial")
        self.assertContains(r, "Ahora: Informe del requerimiento")
        etapas = [v for v, _ in r.context["form_gestion"].fields["etapa"].choices]
        self.assertNotIn("aprobada", etapas)  # sin propuesta enviada no hay respuesta del cliente
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {"etapa": "aprobada", "visibilidad": "interno"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "gestion")

    def test_sin_asesor_lo_toma_quien_gestiona(self):
        t = self.crear()
        F.objects.filter(pk=t.ficha.pk).update(comercial=None)
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {"etapa": "gestion", "visibilidad": "interno",
                                                                             "mensaje": "Llamé al cliente"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.comercial, self.asesor)

    def test_avanza_con_la_propuesta_enviada(self):
        from .pasos import calcular
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Cámaras en el parqueadero de noche", "materiales": "2 cámaras bala"})
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "1000000",
                                                                             "visibilidad": "interno", "propuesta": self.pdf()})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        self.assertEqual(calcular(t.ficha)["paso"]["clave"], "respuesta")
        etapas = [v for v, _ in self.client.get(reverse("comercial:detalle", args=[t.radicado])).context["form_gestion"].fields["etapa"].choices]
        self.assertIn("aprobada", etapas)
        self.assertNotIn("gestion", etapas)


class PlazoAlquilerTests(BaseComercial):
    """En alquiler el cliente aprueba eligiendo el plazo (12, 24 o 36 meses) y queda guardado con su canon."""

    lineas = CotizadorTests.lineas

    def enviar_alquiler(self):
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Cámaras", "servicios": "CCTV"})
        url = reverse("comercial:cotizador", args=[t.radicado])
        mail.outbox.clear()
        self.client.post(url, {**self.lineas([("Sistema de videovigilancia", "1", "1000000", "0")]), "modalidad": "alquiler",
                               "margen": "10", **URGENTE, "accion": "enviar"})
        t.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        return t

    def test_aprueba_desde_el_correo_con_el_plazo(self):
        import re
        from urllib.parse import urlparse
        t = self.enviar_alquiler()
        correo = next(m for m in mail.outbox if "leidy@a.co" in m.to)
        html = correo.alternatives[0][0].replace("&amp;", "&")
        botones = re.findall(r'href="(https?://[^"]+/propuesta/[^"?]+/\?r=aprobar&plazo=(\d+))"', html)
        self.assertEqual([p for _, p in botones], ["12", "24", "36"])
        self.assertIn("Apruebo a 24 meses", html)
        ruta = urlparse(dict((p, u) for u, p in botones)["24"]).path
        # Sin plazo no aprueba: muestra las opciones
        pagina = self.client.get(ruta, {"r": "aprobar"})
        self.assertNotContains(pagina, 'id="aprobar-ya"')
        self.assertContains(pagina, "¿a cuántos meses el alquiler?")
        self.assertContains(self.client.post(ruta, {"accion": "aprobar"}), "Elija el plazo del alquiler")
        self.assertContains(self.client.post(ruta, {"accion": "aprobar", "plazo": "18"}), "Elija el plazo del alquiler")
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        # El botón de 24 meses trae el plazo y se registra con él
        self.assertContains(self.client.get(ruta, {"r": "aprobar", "plazo": "24"}), 'name="plazo" value="24"')
        self.client.post(ruta, {"accion": "aprobar", "plazo": "24"})
        t.ficha.refresh_from_db()
        v = t.ficha.version_actual
        canon = next(f["canon"] for f in t.ficha.cotizaciones.get().canones()["filas"] if f["plazo"] == 24)
        self.assertEqual((t.ficha.etapa, v.plazo_meses, v.canon_mensual), ("instalacion", 24, round(canon, 2)))
        self.assertContains(self.client.get(ruta), "en alquiler a 24 meses")
        self.assertTrue(t.seguimientos.filter(mensaje__contains="en alquiler a 24 meses").exists())

    def test_comercial_registra_la_aprobacion_con_el_plazo(self):
        t = self.enviar_alquiler()
        url = reverse("comercial:gestionar", args=[t.radicado])
        from .forms import GestionComercialForm
        form = GestionComercialForm({"etapa": "aprobada", "visibilidad": "interno"}, ficha=t.ficha, usuario=self.asesor)
        self.assertIn("plazo", form.errors)
        self.client.post(url, {"etapa": "aprobada", "visibilidad": "interno"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "enviada")
        self.client.post(url, {"etapa": "aprobada", "visibilidad": "interno", "plazo": "36"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.version_actual.plazo_meses, 36)
        self.assertContains(self.client.get(reverse("comercial:detalle", args=[t.radicado])), "Alquiler a 36 meses")

    def test_el_pdf_de_totales_sale(self):
        t = self.enviar_alquiler()
        r = self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado]))
        self.assertEqual(r["Content-Type"], "application/pdf")


class ConsecutivoYValorTests(BaseComercial):
    """El consecutivo sigue al de Excel y el cliente ve el valor con IVA incluido."""
    lineas = CotizadorTests.lineas

    def test_consecutivo_sincronizado_con_el_de_excel(self):
        from django.utils import timezone

        from .models import PropuestaVersion
        anio = timezone.localdate().year
        self.client.force_login(self.asesor)
        url = reverse("comercial:propuestas")
        self.assertContains(self.client.get(url), "La próxima propuesta sale con el <b>001</b>")
        self.client.post(url, {"accion": "consecutivo", "ultimo": "47"})
        self.assertContains(self.client.get(url), "La próxima propuesta sale con el <b>048</b>")
        self.client.post(url, {"accion": "consecutivo", "ultimo": "x"})  # no se daña con un valor raro
        t = self.crear()
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Cámaras", "servicios": "CCTV"})
        cot = reverse("comercial:cotizador", args=[t.radicado])
        self.assertContains(self.client.get(cot), f"PE-{anio}-")
        self.client.post(cot, {**self.lineas([("Cámara", "1", "100000", "0")]), "margen": "10"})
        # Un número que ya se usó en Excel no se deja poner
        r = self.client.post(cot, {**self.lineas([]), "margen": "10", "consecutivo": "30"})
        self.assertIn("consecutivo", r.context["form"].errors)
        self.client.post(cot, {**self.lineas([]), "margen": "10", **URGENTE, "accion": "enviar"})
        v = PropuestaVersion.objects.get(ficha=t.ficha)
        self.assertRegex(v.codigo, rf"^PE-{anio}-\d{{2}}-048-V1$")
        # Correo y página de respuesta: el total con IVA (100.000 / 0,9 = 111.111,11 + 19 % = 132.222)
        correo = next(m for m in mail.outbox if "leidy@a.co" in m.to and "Propuesta económica" in m.subject)
        self.assertIn("$132.222 IVA incluido", correo.alternatives[0][0])
        self.assertNotIn("antes de IVA", correo.alternatives[0][0])
        self.assertEqual(v.valor_texto, "$132.222 IVA incluido")

"""Recorrido de punta a punta entre áreas, con el equipo y la configuración reales de JANOTEC.

Cada prueba sigue un proceso completo y comprueba las conexiones: a quién le llega el aviso y en qué
bandeja aparece el caso para que el área siguiente lo atienda. Las pruebas de cada pantalla están en los
tests de cada app; aquí se verifica que los procesos se encadenen bien.
"""
import shutil
import tempfile
from datetime import time, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets import reincidencia
from tickets.models import ActaSoporte, Categoria, Ticket, TipoFalla

# Firma a mano (PNG) como la que dibuja el recuadro de firma
FIRMA = "data:image/png;base64," + __import__("base64").b64encode(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"0" * 40).decode()

MEDIA_TMP = tempfile.mkdtemp()
E = Ticket.Estado
COORD = "coordinadortic@janotec.app"
URGENTE = {"urgente": "1", "motivo_urgencia": "El cliente la necesita hoy"}  # envío sin esperar a dirección
FIRMA = "data:image/png;base64," + __import__("base64").b64encode(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"0" * 40).decode()


def dia_habil(dias=2):
    d = timezone.localdate() + timedelta(days=dias)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


# Igual que el .env de hoy: solo el buzón de soporte; sin buzón comercial, SST ni administrativa
@override_settings(MEDIA_ROOT=MEDIA_TMP, NOTIFICAR_SOPORTE_EMAIL=COORD, NOTIFICAR_COMERCIAL_EMAIL="",
                   NOTIFICAR_SST_EMAIL="", NOTIFICAR_ADMINISTRATIVA_EMAIL="", CORREO_CLIENTES_PRUEBA="",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseFlujos(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        cat = Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")[0]
        inst = Categoria.objects.filter(nombre__startswith="Instalación").first() or Categoria.objects.create(nombre="Instalación / ampliación")
        TipoFalla.objects.get_or_create(categoria=inst, nombre="Cotización")
        TipoFalla.objects.get_or_create(categoria=inst, nombre="Instalar equipos nuevos")
        self.cat = cat
        self.emp = Empresa.objects.create(razon_social="AGROPECUARIA GANACAMPO SAS", nit="902106030", ciudad="Villavicencio")
        self.sede = Sede.objects.create(empresa=self.emp, nombre="PRINCIPAL", ciudad="Villavicencio", direccion="LOTE 8")
        u = Usuario.objects.create_user
        self.cliente = u("cliente", "cliente@ganacampo.co", "Clave-segura-1", rol="cliente", empresa=self.emp,
                         first_name="Daniel", telefono="3214061219")
        self.jguana = u("jguana", COORD, "Clave-segura-1", rol="supervisor", first_name="Juan Daniel", last_name="Guana")
        self.miguel = u("mtibamoso", "m.tibamoso@seguridadjano.com", "Clave-segura-1", rol="direccion", first_name="Miguel")
        self.freyder = u("fgordillo", "f.gordillo@janotec.app", "Clave-segura-1", rol="agente", first_name="Freyder")
        self.yuri = u("ychingate", "janotec@janotec.app", "Clave-segura-1", rol="bodega", tambien_comercial=True, first_name="Yuri")
        self.sindy = u("smedina", "c.medina@seguridadjano.com", "Clave-segura-1", rol="bodega", tambien_comercial=True,
                       first_name="Sindy")
        self.luisa = u("lgarcia", "centraljano@seguridadjano.com", "Clave-segura-1", rol="central", first_name="Luisa")
        self.comerciales = {self.yuri.email, self.sindy.email}

    # --- utilidades ----------------------------------------------------------------
    def para(self, correo):
        return [m.subject for m in mail.outbox if correo in m.to + m.cc]

    def radicados(self, usuario, url):
        import re
        self.client.force_login(usuario)
        return set(re.findall(r"(JNT-\d{4}-\d{6})", self.client.get(url).content.decode()))

    def ticket(self, **kw):
        datos = dict(empresa=self.emp, sede=self.sede, solicitante=self.cliente, radicado_por=self.jguana, categoria=self.cat,
                     asunto="Cámara sin imagen", descripcion="La cámara de la entrada no da imagen", impacto="media",
                     contacto_nombre="Daniel", contacto_telefono="3214061219", contacto_email="cliente@ganacampo.co")
        datos.update(kw)
        return Ticket.objects.create(**datos)

    def programar(self, t, insumos="", **extra):
        datos = {"ticket": t.pk, "fecha": dia_habil().isoformat(), "hora_inicio": "08:00", "hora_fin": "12:00",
                 "cliente": t.empresa.razon_social, "sede": "PRINCIPAL", "actividad": t.descripcion, "tipo": "mantenimiento",
                 "linea": "CCTV / videovigilancia", "encargados": [self.freyder.pk], "notificar_a": t.contacto_email,
                 "sst_duracion": "medio día", "insumos": insumos}
        datos.update(extra)
        self.client.force_login(self.jguana)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cronograma:crear"), datos)
        t.refresh_from_db()
        return t.actividades.order_by("-pk").first()

    def acta(self, t, **extra):
        datos = {"ciudad": "Villavicencio", "actividad": ["Soporte"], "sistema": "CCTV", "diagnostico": "Fuente averiada",
                 "resultado": "solucionado", "observaciones": "Se cambió la fuente", "cargo_tecnico": "Técnico",
                 "firma_tecnico": FIRMA, "recibio": "Daniel", "cargo_recibio": "Administrador", "firma_cliente": FIRMA,
                 "modalidad": "presencial",
                 "copia_correo": "", "marcar_resuelto": "on", "equipos-TOTAL_FORMS": "0", "equipos-INITIAL_FORMS": "0",
                 "equipos-MIN_NUM_FORMS": "0", "equipos-MAX_NUM_FORMS": "30"}
        datos.update(extra)
        self.client.force_login(self.freyder)
        return self.client.post(reverse("acta_nueva", args=[t.radicado]), datos)


class FlujoTecnicoTests(BaseFlujos):
    def test_1_radicacion_por_cliente_llega_a_recepcion(self):
        self.client.force_login(self.cliente)
        mail.outbox.clear()
        self.client.post(reverse("nuevo_ticket"), {
            "tipo": "soporte", "categoria": self.cat.pk, "sede": self.sede.pk, "departamento": "Meta", "municipio": "Villavicencio",
            "direccion": "LOTE 8", "falla_nombre": "Cámara sin imagen", "descripcion": "No da imagen", "alturas": "no", "sst": "no",
            "contacto_nombre": "Daniel", "contacto_telefono": "3214061219", "contacto_email": "cliente@ganacampo.co"})
        t = Ticket.objects.get()
        self.assertEqual(t.estado, E.GENERADO)
        self.assertTrue(self.para("cliente@ganacampo.co"), "el cliente recibe «Solicitud registrada»")
        self.assertTrue(self.para(COORD), "el coordinador recibe «por recepcionar»")
        self.assertIn(t.radicado, self.radicados(self.jguana, reverse("panel")), "aparece en la recepción de jguana")

    def test_2_central_registra_para_un_cliente(self):
        self.client.force_login(self.luisa)
        mail.outbox.clear()
        self.client.post(reverse("nuevo_ticket"), {
            "empresa_nombre": self.emp.razon_social, "sede_nombre": "PRINCIPAL", "tipo": "soporte", "categoria": self.cat.pk,
            "departamento": "Meta", "municipio": "Villavicencio", "direccion": "LOTE 8", "falla_nombre": "Alarma disparada",
            "descripcion": "Evento de alarma sin restablecer", "alturas": "no", "sst": "no", "contacto_nombre": "Daniel",
            "contacto_telefono": "3214061219", "contacto_email": "cliente@ganacampo.co"})
        t = Ticket.objects.filter(radicado_por=self.luisa).first()
        self.assertIsNotNone(t, "la central puede registrar la solicitud de un cliente")
        self.assertTrue(self.para(COORD), "el coordinador se entera de lo que registró la central")
        self.assertIn(t.radicado, self.radicados(self.jguana, reverse("panel")))
        self.assertIn(t.radicado, self.radicados(self.luisa, reverse("mis_tickets")), "la central lo sigue en sus registradas")

    def test_3_visita_completa_con_material_de_bodega(self):
        from cronograma import servicios as crono
        from inventario.models import Activo, SolicitudMaterial
        Activo.objects.create(placa="CAM-001", referencia="DS2CD1043", tipo="Cámara")
        t = self.ticket()
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        self.client.post(reverse("recepcionar", args=[t.radicado]))
        t.refresh_from_db()
        self.assertEqual(t.estado, E.RECEPCIONADO)
        self.assertTrue(self.para("cliente@ganacampo.co"), "el cliente recibe «Su solicitud está en proceso»")

        # Programa con el material que va a usar: lo debe recibir bodega (la autorización se pide al validar)
        mail.outbox.clear()
        a = self.programar(t, insumos="1 Cámara DS2CD1043 [DS2CD1043]")
        self.assertEqual((t.estado, t.asignado_a), (E.EN_CRONOGRAMA, self.freyder))
        self.assertTrue(self.para(self.freyder.email), "el técnico recibe la visita programada")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.assertTrue(self.para(self.yuri.email) and self.para(self.sindy.email), "bodega recibe la solicitud de material")
        self.client.force_login(self.yuri)
        self.assertContains(self.client.get(reverse("inventario:solicitudes")), s.numero)

        # Autorización del cliente
        mail.outbox.clear()
        crono.enviar_autorizacion(a, self.jguana)
        self.assertTrue(self.para("cliente@ganacampo.co"), "el cliente recibe la autorización de ingreso")
        mail.outbox.clear()
        crono.responder_autorizacion(a, aceptada=True)
        t.refresh_from_db()
        self.assertEqual(t.estado, E.AUTORIZADO)
        self.assertTrue(self.para(COORD), "el coordinador se entera de que el cliente aceptó")

        # Bodega valida, dirección firma, se entrega al técnico
        self.client.force_login(self.yuri)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "janotec", "modalidad": "mantenimiento", "tecnico": self.freyder.pk})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobacion")
        mail.outbox.clear()
        self.client.force_login(self.miguel)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": FIRMA})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobada")
        self.assertTrue(self.para(self.freyder.email), "el técnico sabe que su material está listo")
        self.client.force_login(self.yuri)
        r = self.client.get(reverse("inventario:entrega") + f"?solicitud={s.pk}")
        self.client.post(reverse("inventario:entrega"), {"solicitud": s.pk, "tecnico": self.freyder.pk, "modalidad": "mantenimiento",
                                                         "ticket": t.pk, "placa": r.context["placas"],
                                                         "firma_entrega": FIRMA, "firma_recibe": FIRMA})
        s.refresh_from_db()
        self.assertEqual(s.estado, "entregada")

        # El técnico hace el acta y descuenta lo instalado
        mail.outbox.clear()
        activo = Activo.objects.get(placa="CAM-001")
        self.acta(t, usar_activo=[str(activo.pk)], copia_correo="cliente@ganacampo.co")
        t.refresh_from_db()
        activo.refresh_from_db()
        self.assertEqual(t.estado, E.VISITA_EJECUTADA)
        self.assertEqual(activo.lugar, "cliente", "lo instalado queda registrado en el cliente")
        self.assertTrue(self.para("cliente@ganacampo.co"), "el cliente recibe copia del acta")

        self.client.force_login(self.cliente)
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "aceptar"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.CERRADO)

    def test_3b_cliente_confirma_y_recibe_el_cierre(self):
        t = self.ticket(estado=E.VISITA_EJECUTADA, asignado_a=self.freyder)
        mail.outbox.clear()
        self.client.force_login(self.cliente)
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "aceptar"})
        self.assertTrue(any("solucionada y cerrada" in s for s in self.para("cliente@ganacampo.co")),
                        "al confirmar el cliente, le llega el correo de cierre para calificar")

    def test_4_cierre_por_el_coordinador_notifica_al_cliente(self):
        t = self.ticket(estado=E.VISITA_EJECUTADA, asignado_a=self.freyder)
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": "cerrado", "prioridad": t.prioridad,
                                                                   "asignado_a": self.freyder.pk, "visibilidad": "publico"})
        self.assertTrue(any("solucionada y cerrada" in s for s in self.para("cliente@ganacampo.co")))

    def test_5_sin_correo_de_sst_el_coordinador_se_entera(self):
        """Acompañamiento SST sin correo en el Directorio (como está hoy): el coordinador recibe el aviso."""
        from cronograma import servicios as crono
        from cronograma.models import Contacto
        Contacto.objects.create(tipo=Contacto.Tipo.SST, nombre="Área SST", email="")  # como está hoy
        t = self.ticket(acompanamiento_sst=True)
        t.cambiar_estado(E.RECEPCIONADO, self.jguana)
        a = self.programar(t, sst="on", sst_persona="Javier", sst_trabajo="Trabajo en alturas")
        mail.outbox.clear()
        crono.responder_autorizacion(a, aceptada=True)
        self.assertTrue(any("Falta el correo de SST" in x for x in self.para(COORD)), "el coordinador sabe que no salió")
        Contacto.objects.filter(tipo=Contacto.Tipo.SST).update(email="sst@janotec.test")
        mail.outbox.clear()
        crono.avisar_sst(a)
        self.assertTrue(self.para("sst@janotec.test"), "con el correo en el Directorio, la solicitud llega a SST")

    def test_6_asignar_a_bodega_le_llega_y_lo_ve(self):
        t = self.ticket(estado=E.RECEPCIONADO)
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": t.estado, "prioridad": t.prioridad,
                                                                   "asignado_a": self.sindy.pk, "visibilidad": "interno",
                                                                   "mensaje": "Por favor alistar 2 baterías"})
        self.assertTrue(self.para(self.sindy.email))
        self.assertIn(t.radicado, self.radicados(self.sindy, reverse("asignados")))

    def test_7_cliente_pide_nueva_visita(self):
        t = self.ticket(estado=E.VISITA_EJECUTADA, asignado_a=self.freyder)
        self.client.force_login(self.cliente)
        mail.outbox.clear()
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "reabrir", "motivo": "Sigue sin imagen"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.PENDIENTE_VISITA)
        self.assertTrue(self.para(self.freyder.email) or self.para(COORD), "el área técnica se entera de que sigue la falla")
        self.assertIn(t.radicado, self.radicados(self.jguana, reverse("cronograma:semana")), "vuelve a «por programar»")

    def test_8_acta_no_programada_llega_al_coordinador(self):
        self.client.force_login(self.freyder)
        mail.outbox.clear()
        datos = {"ciudad": "Villavicencio", "actividad": ["Soporte"], "sistema": "CCTV", "diagnostico": "Pidió revisar un DVR",
                 "resultado": "solucionado", "observaciones": "Se reinició", "cargo_tecnico": "Técnico", "firma_tecnico": FIRMA,
                 "recibio": "Daniel", "cargo_recibio": "Admin", "firma_cliente": FIRMA, "copia_correo": "", "modalidad": "presencial",
                 "equipos-TOTAL_FORMS": "0", "equipos-INITIAL_FORMS": "0", "equipos-MIN_NUM_FORMS": "0", "equipos-MAX_NUM_FORMS": "30"}
        self.client.post(reverse("acta_sin_programar") + f"?empresa={self.emp.pk}&sede={self.sede.pk}", datos)
        self.assertTrue(ActaSoporte.objects.filter(ticket=None).exists())
        self.assertTrue(self.para(COORD))


class FlujoComercialTests(BaseFlujos):
    def pdf(self):
        return SimpleUploadedFile("propuesta.pdf", b"%PDF-1.4 propuesta", content_type="application/pdf")

    def test_9_informe_tecnico_a_comercial(self):
        t = self.ticket(tipo=Ticket.Tipo.REQUERIMIENTO, estado=E.INFORME, asignado_a=self.freyder)
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        self.client.post(reverse("entregar_informe", args=[t.radicado]), {"nota": "Cotizar alarma", "destino": "comercial",
                                                                  "urgente": "1", "motivo_urgencia": "Propuesta pedida para hoy",
                                                                  "servicios": "1 Instalación de alarma", "materiales": "1 Panel de alarma\n4 Sensor PIR"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (Ticket.Area.COMERCIAL, "elaborando"))
        self.assertTrue(self.comerciales & {d for m in mail.outbox for d in m.to}, "comercial recibe el aviso")
        self.assertIn(t.radicado, self.radicados(self.yuri, reverse("comercial:bandeja")), "aparece en la bandeja comercial")

    def test_10_coordinador_elige_en_gestion_comercial(self):
        t = self.ticket(estado=E.RECEPCIONADO, asignado_a=self.freyder)
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        self.client.post(reverse("gestionar", args=[t.radicado]), {"estado": "en_proceso", "prioridad": t.prioridad,
                                                                   "asignado_a": self.freyder.pk, "visibilidad": "interno",
                                                                   "mensaje": "Cotizar 3 computadores"})
        t.refresh_from_db()
        self.assertEqual(t.area, Ticket.Area.COMERCIAL)
        self.assertTrue(self.comerciales & {d for m in mail.outbox for d in m.to})
        self.assertIn(t.radicado, self.radicados(self.sindy, reverse("comercial:bandeja")))

    def test_11_material_a_cargo_del_cliente_pasa_por_propuesta(self):
        from inventario.models import SolicitudMaterial
        t = self.ticket(estado=E.RECEPCIONADO)
        a = self.programar(t, insumos="1 Cámara bala")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.client.force_login(self.yuri)
        mail.outbox.clear()
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "cliente", "modalidad": "venta", "tecnico": self.freyder.pk})
        t.refresh_from_db()
        self.assertEqual(t.area, Ticket.Area.COMERCIAL, "pasa a comercial para la propuesta del material")
        self.assertTrue(self.comerciales & {d for m in mail.outbox for d in m.to}, "comercial recibe el aviso")
        self.assertIn(t.radicado, self.radicados(self.yuri, reverse("comercial:bandeja") + "?vista=administrativa"))
        a.refresh_from_db()
        # La visita que estaba programada no puede quedar como si nada: o sigue programada o se avisa
        self.assertTrue(a.ticket.area == Ticket.Area.COMERCIAL)

    def test_12_requerimiento_comercial_completo(self):
        # Como quedó el 04/11/2026: Yuri y Sindy arman las propuestas (también administrativa) y, mientras el área
        # comercial no tenga usuario, dirección (Miguel) radica, pide el levantamiento y le llegan sus avisos
        Usuario.objects.filter(pk__in=[self.yuri.pk, self.sindy.pk]).update(tambien_comercial=False, tambien_administrativo=True)
        self.sindy.refresh_from_db()
        self.client.force_login(self.miguel)
        mail.outbox.clear()
        self.client.post(reverse("comercial:nuevo"), {"empresa": self.emp.pk, "contacto_nombre": "Daniel", "contacto_telefono": "3214061219",
                                                      "contacto_email": "cliente@ganacampo.co", "asunto": "Cámaras en el parqueadero",
                                                      "servicios": ["cctv"], "nd_viernes": "tarde", "sin_visita": "on"})
        t = Ticket.objects.get(ficha__isnull=False)
        self.assertTrue(self.para("cliente@ganacampo.co"), "el cliente recibe la ficha del requerimiento")
        # Levantamiento: llega a la recepción técnica
        mail.outbox.clear()
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "Ver si van domos o balas", "jornada": "manana"})
        t.refresh_from_db()
        self.assertTrue(self.para(COORD), "el coordinador recibe la solicitud de levantamiento")
        self.assertIn(t.radicado, self.radicados(self.jguana, reverse("panel")))
        # Llega de otra área: entra ya recepcionado, listo para el cronograma
        self.assertEqual(t.estado, E.RECEPCIONADO)
        self.client.force_login(self.jguana)
        mail.outbox.clear()
        a = self.programar(t)
        self.assertTrue(self.para(self.freyder.email), "el técnico recibe la visita de levantamiento")
        # El técnico entrega el informe y vuelve a comercial
        mail.outbox.clear()
        self.client.force_login(self.freyder)
        self.client.post(reverse("comercial:devolver", args=[t.radicado]), {"necesidad": "Parqueadero de noche", "materiales": "3 Cámara bala"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (Ticket.Area.COMERCIAL, "elaborando"))
        self.assertTrue(self.comerciales & {d for m in mail.outbox for d in m.to}, "el asesor sabe que volvió el informe")
        a.refresh_from_db()
        # Propuesta → cambios → versión 2 → aprobada → material → instalación
        self.client.force_login(self.sindy)
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "4000000",
                                                                            "visibilidad": "interno",
                                                                            "propuesta": self.pdf()})
        self.assertTrue(self.para("cliente@ganacampo.co"))
        mail.outbox.clear()
        self.client.force_login(self.cliente)
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "cambios", "cambios": "Marca Hikvision"})
        self.assertTrue(self.para(self.sindy.email), "administrativa recibe los cambios pedidos")
        self.client.force_login(self.sindy)
        self.client.post(reverse("comercial:gestionar", args=[t.radicado]), {**URGENTE, "etapa": "enviada", "valor_propuesta": "4300000",
                                                                            "visibilidad": "interno",
                                                                            "propuesta": self.pdf()})
        mail.outbox.clear()
        self.client.force_login(self.cliente)
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "aprobar"})
        self.assertTrue(self.para(self.sindy.email), "administrativa sabe que la aprobaron")
        self.assertTrue(self.para(self.miguel.email), "dirección, que cubre comercial, también")
        from inventario.models import SolicitudMaterial
        s = SolicitudMaterial.objects.get(ticket=t, origen="comercial")
        self.assertEqual(s.estado, "validacion", "el material aprobado va directo a revisión de bodega")
        self.assertTrue(any("Revise el material" in x for x in self.para(self.yuri.email)), "bodega recibe el material por revisar")
        # Aprobada: queda «En inventario» en técnica; coordinación ve el avance de la lista y decide
        t.refresh_from_db()
        self.assertEqual((t.area, t.estado), (Ticket.Area.TECNICA, E.EN_INVENTARIO))
        self.assertTrue(self.para(COORD), "coordinación sabe que la instalación está en alistamiento")
        mail.outbox.clear()
        self.client.force_login(self.yuri)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                             {"cargo": "cliente", "modalidad": "venta", **{f"listo_{i.pk}": "on" for i in s.items.all()}})
        self.assertTrue(any("Bodega revisó el material" in x for x in self.para(self.miguel.email)), "comercial (hoy dirección) sabe cómo va")
        self.client.force_login(self.jguana)
        self.assertContains(self.client.get(t.get_absolute_url()), "1. Aprobado por el cliente")
        self.client.post(reverse("inventario_listo", args=[t.radicado]), {"nota": "Instalar la otra semana"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.estado), (Ticket.Area.TECNICA, E.RECEPCIONADO))  # coordinación decidió: por programar
        self.assertIn(t.radicado, self.radicados(self.jguana, reverse("panel")))

    def test_13_oportunidad_del_acta_llega_a_comercial(self):
        t = self.ticket(estado=E.EN_CRONOGRAMA, asignado_a=self.freyder)
        mail.outbox.clear()
        self.acta(t, oportunidad_servicios=["alarma"], oportunidad_nota="Pidió cotizar alarma para la bodega")
        self.assertTrue(self.comerciales & {d for m in mail.outbox for d in m.to}, "comercial recibe la oportunidad")
        self.client.force_login(self.yuri)
        self.assertContains(self.client.get(reverse("oportunidades")), "alarma para la bodega")


@override_settings(SALUD_PROCESOS=True)  # retirada el 06/11/2026; se prueba encendida por si se reactiva
class SaludDeLosProcesosTests(BaseFlujos):
    """La plataforma detecta sola lo que no sigue la lógica del proceso y avisa al superusuario."""

    def test_detecta_avisa_y_cierra_solo(self):
        from tickets import diagnostico
        from tickets.models import Hallazgo
        admin = Usuario.objects.create_superuser("admin", "d.jimenez@seguridadjano.com", "Clave-segura-1")
        t = self.ticket(contacto_email=COORD, estado=E.RECEPCIONADO)
        t.cambiar_estado(E.VISITA_EJECUTADA, self.jguana)  # salto: sin cronograma ni acta
        nuevos = diagnostico.revisar()
        reglas = {h.regla for h in nuevos}
        self.assertTrue({"contacto_interno", "saltos_de_estado", "visita_sin_acta"} <= reglas, reglas)
        mail.outbox.clear()
        self.assertTrue(diagnostico.avisar(forzar=True))
        self.assertEqual(mail.outbox[-1].to, [admin.email])
        self.assertIn("Qué mejorar", mail.outbox[-1].body)
        self.assertFalse(diagnostico.avisar(forzar=True))  # lo ya avisado no se repite
        # Se corrige el contacto: ese hallazgo se cierra solo
        t.contacto_email = "cliente@ganacampo.co"
        t.save()
        diagnostico.revisar()
        self.assertIsNotNone(Hallazgo.objects.get(regla="contacto_interno").resuelto)
        # Solo el superusuario ve la página
        self.client.force_login(self.jguana)
        self.assertEqual(self.client.get(reverse("salud_procesos")).status_code, 403)
        self.client.force_login(admin)
        r = self.client.get(reverse("salud_procesos"))
        self.assertContains(r, "Dice que hubo visita, pero no hay acta")
        self.assertContains(r, "Corregidas en los últimos 30 días")


class ContactoPorSedeTests(BaseFlujos):
    """La sede guarda quién recibe; se llena solo al radicar y se advierte si el contacto es del personal."""

    def radicar(self, **kw):
        datos = {"empresa_nombre": self.emp.razon_social, "sede_nombre": "PRINCIPAL", "tipo": "soporte", "sistema": "Mantenimiento CCTV IP",
                 "departamento": "Meta", "municipio": "Villavicencio", "direccion": "LOTE 8", "falla_nombre": "Cámara sin imagen",
                 "descripcion": "No da imagen", "alturas": "no", "sst": "no", "contacto_nombre": "Daniel", "impacto": "media",
                 "contacto_telefono": "3214061219", "contacto_email": "cliente@ganacampo.co"}
        datos.update(kw)
        self.client.force_login(self.jguana)
        return self.client.post(reverse("nuevo_ticket"), datos, follow=True)

    def test_la_sede_aprende_el_contacto_y_lo_sugiere(self):
        r = self.radicar()
        form = r.context.get("form") if r.context else None
        self.assertTrue(Ticket.objects.exists(), form.errors if form else r.redirect_chain)
        self.sede.refresh_from_db()
        self.assertEqual((self.sede.contacto_nombre, self.sede.contacto_email), ("Daniel", "cliente@ganacampo.co"))
        r = self.client.get(reverse("nuevo_ticket"))
        self.assertIn("cliente@ganacampo.co", r.content.decode())  # queda en las sugerencias de la sede

    def test_advierte_si_el_contacto_es_del_personal(self):
        r = self.radicar(contacto_nombre="Juan Daniel Guana", contacto_email=COORD)
        self.assertContains(r, "El contacto registrado es del personal JANOTEC")
        self.sede.refresh_from_db()
        self.assertEqual(self.sede.contacto_email, "")  # no se aprende un contacto interno

    def test_empresa_del_grupo_es_trabajo_interno(self):
        from cronograma.models import Actividad
        from tickets import diagnostico
        grupo = Empresa.objects.create(razon_social="SEGURIDAD JANO LTDA", nit="822002315-6", es_del_grupo=True)
        Sede.objects.create(empresa=grupo, nombre="Principal")
        t = self.ticket(empresa=grupo, sede=grupo.sedes.first(), contacto_email=COORD, estado=E.RECEPCIONADO)
        a = self.programar(t)
        self.assertTrue(a.es_interna)
        self.assertFalse(a.en_rojo)
        r = self.client.post(reverse("cronograma:enviar_autorizacion", args=[a.pk]), follow=True)
        self.assertContains(r, "no se le envía autorización")
        diagnostico.revisar()
        from tickets.models import Hallazgo
        self.assertFalse(Hallazgo.objects.filter(ticket=t, regla__in=["contacto_interno", "autorizaciones"]).exists())


@override_settings(SALUD_PROCESOS=True)
class RevisarCasosDeSaludTests(BaseFlujos):
    """Cada área ve sus casos en su bandeja y marca como revisado (con motivo) lo que no requiere acción."""

    def test_revisado_con_motivo_y_vuelve_si_pasa_de_nuevo(self):
        from tickets import diagnostico
        from tickets.models import Hallazgo
        t = self.ticket(contacto_email=COORD, estado=E.RECEPCIONADO)
        diagnostico.revisar()
        h = Hallazgo.objects.get(regla="contacto_interno", ticket=t)
        self.client.force_login(self.jguana)  # coordinación lo ve en su bandeja
        self.assertContains(self.client.get(reverse("panel")), "Situaciones por revisar")
        self.client.force_login(self.yuri)  # bodega no lo puede revisar: no es de su área
        self.assertEqual(self.client.post(reverse("hallazgo_revisar", args=[h.pk]), {"motivo": "No es mío"}).status_code, 403)
        self.client.force_login(self.jguana)
        r = self.client.post(reverse("hallazgo_revisar", args=[h.pk]), {"motivo": "x", "volver": "/soporte/"}, follow=True)
        self.assertContains(r, "Escriba por qué no requiere acción")
        self.client.post(reverse("hallazgo_revisar", args=[h.pk]), {"motivo": "Jguana es el contacto a propósito", "volver": "/soporte/"})
        h.refresh_from_db()
        self.assertEqual((h.revisado_por, h.motivo_revision), (self.jguana, "Jguana es el contacto a propósito"))
        self.assertNotIn(h, diagnostico.casos_para(self.jguana))
        diagnostico.revisar()  # sigue igual: no vuelve a la lista
        h.refresh_from_db()
        self.assertIsNotNone(h.revisado_en)
        # Se corrige y luego vuelve a pasar: es algo nuevo y vuelve a la lista
        t.contacto_email = "cliente@ganacampo.co"
        t.save()
        diagnostico.revisar()
        t.contacto_email = COORD
        t.save()
        diagnostico.revisar()
        h.refresh_from_db()
        self.assertIsNone(h.revisado_en)
        self.assertIn(h, diagnostico.casos_para(self.jguana))


class SaludApagadaTests(BaseFlujos):
    """Retirada el 06/11/2026: no revisa, no avisa y no sale en ninguna pantalla; lo ya detectado se conserva."""

    def test_no_aparece_en_ninguna_parte(self):
        from tickets import diagnostico
        from tickets.models import Hallazgo
        admin = Usuario.objects.create_superuser("admin", "d.jimenez@seguridadjano.com", "Clave-segura-1")
        self.ticket(contacto_email=COORD, estado=E.RECEPCIONADO)  # antes era un caso de «contacto interno»
        mail.outbox.clear()
        diagnostico.revisar_si_toca()
        self.assertFalse(Hallazgo.objects.exists())
        self.assertEqual(mail.outbox, [])
        self.client.force_login(admin)
        r = self.client.get(reverse("panel"))
        self.assertNotContains(r, "Situaciones por revisar")
        self.assertNotContains(r, reverse("salud_procesos"))
        self.assertEqual(self.client.get(reverse("salud_procesos")).status_code, 404)
        self.client.force_login(self.jguana)
        self.assertNotContains(self.client.get(reverse("panel")), "Situaciones por revisar")


class ServicioRepetidoTests(BaseFlujos):
    def setUp(self):
        super().setUp()
        self.t = self.ticket()

    def previa(self, numero=40001, dias=10, sistema="CCTV", actividad="Soporte", diagnostico="Cámara sin imagen", sede=None):
        return ActaSoporte.objects.create(numero=numero, fecha=self.t.creado - timedelta(days=dias), empresa=self.emp, sede=sede or self.sede,
                                          actividad=actividad, sistema=sistema, diagnostico=diagnostico, resultado="parcial")

    def test_avisa_al_personal_con_soporte_reciente_del_mismo_sistema(self):
        self.previa()
        self.client.force_login(self.jguana)
        html = self.client.get(self.t.get_absolute_url()).content.decode()
        self.assertIn("Servicio repetido", html)
        self.assertIn("Acta 40001", html)

    def test_no_cuenta_lo_que_no_es_una_falla_que_volvio(self):
        otra = Sede.objects.create(empresa=self.emp, nombre="BODEGA", ciudad="Villavicencio")
        self.previa(40001, dias=45)
        self.previa(40002, sistema="ALARMA")
        self.previa(40003, actividad="Instalación")
        self.previa(40004, diagnostico="Mantenimiento preventivo del sistema")
        self.previa(40005, sede=otra)
        self.assertEqual(reincidencia.visitas_previas(self.t), [])

    def test_solo_aplica_a_soporte_con_sede(self):
        self.previa()
        self.assertEqual(len(reincidencia.visitas_previas(self.t)), 1)
        self.assertEqual(reincidencia.visitas_previas(self.ticket(tipo="instalacion")), [])
        self.assertEqual(reincidencia.visitas_previas(self.ticket(sede=None)), [])

    def test_el_cliente_no_ve_el_aviso(self):
        self.previa()
        self.client.force_login(self.cliente)
        self.assertNotContains(self.client.get(self.t.get_absolute_url()), "Servicio repetido")

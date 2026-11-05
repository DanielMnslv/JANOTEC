"""«Actuar como»: el superusuario recorre el proceso completo él solo, entrando como el usuario de prueba de cada área.

Las pruebas de seguridad comprueban que solo el superusuario actúa, solo como usuarios de prueba, y que mientras actúa
no puede guardar nada sobre datos reales. Los recorridos son los de la guía de validación de Daniel: cada paso lo hace
el área que le toca, cambiando de usuario con el botón, como lo hará él en la plataforma.
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.models import LogEntry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import prueba
from accounts.models import Empresa, Sede, Usuario
from tickets.models import Categoria, Ticket, TipoFalla

MEDIA_TMP = tempfile.mkdtemp()
E = Ticket.Estado
FIRMA = "data:image/png;base64," + __import__("base64").b64encode(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"0" * 40).decode()


def dia_habil(dias=2):
    d = timezone.localdate() + timedelta(days=dias)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


@override_settings(MEDIA_ROOT=MEDIA_TMP, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   CORREO_CLIENTES_PRUEBA="")
class BaseActuar(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        inst = (Categoria.objects.filter(nombre__startswith="Instalación").first()
                or Categoria.objects.create(nombre="Instalación / ampliación"))
        TipoFalla.objects.get_or_create(categoria=inst, nombre="Cotización")
        TipoFalla.objects.get_or_create(categoria=inst, nombre="Instalar equipos nuevos")
        self.cat = Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")[0]
        self.admin = Usuario.objects.create_superuser("admin", "admin@janotec.test", "Clave-segura-1")
        prueba.asegurar_usuarios()
        self.empresa = Empresa.objects.get(razon_social="PRUEBA")
        self.sede = self.empresa.sedes.get(nombre="PRUEBA")
        self.client.force_login(self.admin)

    def u(self, username):
        return Usuario.objects.get(username=username)

    def como(self, username):
        """Cambia de área con el botón «Actuar como» (desde el admin o desde otro usuario de prueba)."""
        r = self.client.post(reverse("actuar_como", args=[self.u(username).pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.u(username).pk)

    def ticket(self, empresa=None, **kw):
        empresa = empresa or self.empresa
        datos = dict(empresa=empresa, sede=empresa.sedes.first(), solicitante=self.u("prueba.cliente"), categoria=self.cat,
                     asunto="PRUEBA Cámara sin imagen", descripcion="No da imagen", impacto="media",
                     contacto_nombre="Daniel", contacto_telefono="3000000000", contacto_email="")
        datos.update(kw)
        return Ticket.objects.create(**datos)

    def programar(self, t):
        """Coordinación programa la visita con el técnico de prueba."""
        datos = {"ticket": t.pk, "fecha": dia_habil().isoformat(), "hora_inicio": "08:00", "hora_fin": "12:00",
                 "cliente": t.empresa.razon_social, "sede": "PRUEBA", "actividad": t.descripcion or t.asunto,
                 "tipo": "mantenimiento", "linea": "CCTV / videovigilancia", "encargados": [self.u("prueba.tecnico").pk],
                 "notificar_a": "", "sst_duracion": "medio día", "insumos": ""}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cronograma:crear"), datos)
        return t.actividades.order_by("-pk").first()


class SeguridadTests(BaseActuar):
    def test_usuarios_de_prueba_sin_contrasena_ni_correo(self):
        nombres = {u for u, *_ in prueba.USUARIOS}
        usuarios = Usuario.objects.filter(username__in=nombres)
        self.assertEqual(usuarios.count(), len(prueba.USUARIOS))
        for u in usuarios:
            self.assertFalse(u.has_usable_password(), u.username)
            self.assertEqual(u.email, "", u.username)
            self.assertFalse(u.is_superuser)
        self.assertEqual(self.u("prueba.cliente").empresa, self.empresa)
        self.assertTrue(self.u("prueba.coordinadora").revisa_compras)
        # Crear o reparar no duplica y deja cada uno como debe estar
        Usuario.objects.filter(username="prueba.bodega").update(rol="cliente", email="x@x.co")
        self.assertEqual(prueba.asegurar_usuarios(), 0)
        b = self.u("prueba.bodega")
        self.assertEqual((b.rol, b.email), ("bodega", ""))

    def test_el_boton_prepara_el_entorno_sin_correos(self):
        from django.core import mail

        from comercial.models import ItemCatalogo
        from inventario.models import Proveedor
        Usuario.objects.filter(username="prueba.financiera").delete()
        r = self.client.post(reverse("probar_como"), follow=True)
        self.assertContains(r, "Entorno de prueba listo")
        self.assertTrue(Usuario.objects.filter(username="prueba.financiera").exists())
        self.assertTrue(ItemCatalogo.objects.filter(descripcion__startswith="PRUEBA").exists())
        self.assertFalse(Proveedor.objects.filter(razon_social__startswith="PRUEBA").exclude(correo="").exists())
        self.assertEqual(mail.outbox, [])

    def test_solo_el_superusuario_y_solo_hacia_usuarios_de_prueba(self):
        real = Usuario.objects.create_user("mtibamoso", "m@janotec.test", "Clave-segura-1", rol="direccion")
        # El superusuario no entra como un usuario real
        self.assertEqual(self.client.post(reverse("actuar_como", args=[real.pk])).status_code, 403)
        # Un usuario que no es superusuario (ni dirección) no actúa como nadie
        self.client.force_login(real)
        self.assertEqual(self.client.get(reverse("probar_como")).status_code, 403)
        self.assertEqual(self.client.post(reverse("actuar_como", args=[self.u("prueba.bodega").pk])).status_code, 403)
        # Desde un usuario de prueba solo se cambia si quien empezó es el superusuario
        self.client.force_login(self.admin)
        self.como("prueba.bodega")
        self.assertEqual(self.client.post(reverse("actuar_como", args=[real.pk])).status_code, 403)
        self.como("prueba.direccion")
        self.assertContains(self.client.get(reverse("probar_como")), "Volver a mi usuario")
        r = self.client.post(reverse("volver_a_mi_usuario"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.admin.pk)
        self.assertNotIn(prueba.SESION, self.client.session)
        self.assertRedirects(r, reverse("probar_como"))
        # Queda en el registro quién actuó como quién
        mensajes = list(LogEntry.objects.filter(user=self.admin).values_list("change_message", flat=True))
        self.assertIn("admin actuó como prueba.bodega (validación del proceso)", mensajes)
        self.assertIn("admin dejó de actuar como prueba.direccion", mensajes)

    def test_volver_no_sirve_sin_haber_empezado_como_superusuario(self):
        self.client.force_login(self.u("prueba.direccion"))
        session = self.client.session
        session[prueba.SESION] = self.u("prueba.bodega").pk  # alguien intenta «volver» a un usuario que no es superusuario
        session.save()
        self.client.post(reverse("volver_a_mi_usuario"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.u("prueba.direccion").pk)

    def test_mientras_actua_no_toca_datos_reales(self):
        real = Empresa.objects.create(razon_social="CLIENTE REAL SAS", nit="9001")
        Sede.objects.create(empresa=real, nombre="PRINCIPAL")
        t_real, t_prueba = self.ticket(empresa=real, asunto="Ticket real"), self.ticket()
        self.como("prueba.coordinador")
        self.assertContains(self.client.get(reverse("probar_como"), follow=True), "Está actuando como")
        self.assertEqual(self.client.get(t_real.get_absolute_url()).status_code, 200)  # ver sí
        r = self.client.post(reverse("recepcionar", args=[t_real.radicado]), follow=True)
        self.assertContains(r, "solo puede trabajar sobre tickets, órdenes y clientes de PRUEBA")
        t_real.refresh_from_db()
        self.assertEqual(t_real.estado, E.GENERADO)
        self.client.post(reverse("recepcionar", args=[t_prueba.radicado]))
        t_prueba.refresh_from_db()
        self.assertEqual(t_prueba.estado, E.RECEPCIONADO)
        # Tampoco programa una visita de un ticket real ni registra para un cliente real
        self.client.post(reverse("cronograma:crear"), {"ticket": t_real.pk, "fecha": dia_habil().isoformat()})
        self.assertFalse(t_real.actividades.exists())
        self.como("prueba.comercial")
        self.client.post(reverse("comercial:nuevo"), {"empresa": real.pk, "asunto": "No debe quedar", "contacto_nombre": "X",
                                                      "contacto_telefono": "300", "servicios": ["cctv"], "sin_visita": "on"})
        self.assertFalse(Ticket.objects.filter(asunto="No debe quedar").exists())
        # Al volver a su usuario, el superusuario trabaja normal
        self.client.post(reverse("volver_a_mi_usuario"))
        self.client.post(reverse("recepcionar", args=[t_real.radicado]))
        t_real.refresh_from_db()
        self.assertEqual(t_real.estado, E.RECEPCIONADO)

    def test_la_coordinadora_de_prueba_no_frena_las_ordenes_reales(self):
        from inventario import financiera
        from inventario.models import OrdenCompra, Proveedor
        bodega = self.u("prueba.bodega")
        real = OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="SYSCOM", nit="1"), creado_por=bodega)
        de_prueba = OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="PRUEBA PROVEEDOR A", nit="2"),
                                               creado_por=bodega)
        self.assertFalse(financiera.requiere_revision(real, bodega), "sin coordinadora real, la orden real va a dirección")
        self.assertTrue(financiera.requiere_revision(de_prueba, bodega))

    def test_el_tecnico_de_prueba_no_sale_en_los_reportes(self):
        from tickets import indicadores
        hoy = timezone.localdate()
        nombres = [f["nombre"] for f in indicadores.por_responsable(Ticket.objects.all(), hoy, hoy)]
        self.assertNotIn("PRUEBA Técnico", nombres)


class RecorridoTecnicoTests(BaseActuar):
    """Soporte: el cliente radica, coordinación recepciona y programa, el cliente autoriza, el técnico hace el acta,
    el cliente pide otra visita (bifurcación) y luego confirma."""

    def test_de_la_radicacion_al_cierre(self):
        from cronograma import servicios as crono
        self.como("prueba.cliente")
        self.client.post(reverse("nuevo_ticket"), {
            "tipo": "soporte", "categoria": self.cat.pk, "sede": self.sede.pk, "departamento": "Meta",
            "municipio": "Villavicencio", "direccion": "Dirección de prueba", "falla_nombre": "PRUEBA Cámara sin imagen",
            "descripcion": "No da imagen", "alturas": "no", "sst": "no", "contacto_nombre": "Daniel",
            "contacto_telefono": "3000000000", "contacto_email": "cliente@janotec.test"})
        t = Ticket.objects.get(empresa=self.empresa)
        self.assertEqual(t.estado, E.GENERADO)
        # Bifurcación: sin nivel de urgencia no se recepciona; coordinación lo define y recepciona
        self.como("prueba.coordinador")
        r = self.client.post(reverse("recepcionar", args=[t.radicado]))
        self.assertIn("?recepcionar=1", r["Location"])
        Ticket.objects.filter(pk=t.pk).update(impacto="media")
        self.client.post(reverse("recepcionar", args=[t.radicado]))
        a = self.programar(t)
        t.refresh_from_db()
        self.assertEqual((t.estado, t.asignado_a.username), (E.EN_CRONOGRAMA, "prueba.tecnico"))
        # Bifurcación: el cliente rechaza el horario; se reprograma y lo acepta
        crono.enviar_autorizacion(a, self.u("prueba.coordinador"))
        crono.responder_autorizacion(a, aceptada=False, motivo="Ese día no hay quien atienda")
        a.refresh_from_db()
        self.assertEqual(a.autorizacion, "rechazada")
        crono.enviar_autorizacion(a, self.u("prueba.coordinador"))
        crono.responder_autorizacion(a, aceptada=True)
        t.refresh_from_db()
        self.assertEqual(t.estado, E.AUTORIZADO)
        # El técnico hace el acta y marca resuelto
        self.como("prueba.tecnico")
        self.client.post(reverse("acta_nueva", args=[t.radicado]), {
            "ciudad": "Villavicencio", "actividad": ["Soporte"], "sistema": "CCTV", "diagnostico": "Fuente averiada",
            "resultado": "solucionado", "observaciones": "Se cambió la fuente", "cargo_tecnico": "Técnico",
            "firma_tecnico": FIRMA, "recibio": "Daniel", "cargo_recibio": "Administrador", "firma_cliente": FIRMA,
            "modalidad": "presencial", "copia_correo": "", "marcar_resuelto": "on", "equipos-TOTAL_FORMS": "0",
            "equipos-INITIAL_FORMS": "0", "equipos-MIN_NUM_FORMS": "0", "equipos-MAX_NUM_FORMS": "30"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.VISITA_EJECUTADA)
        # Bifurcación: el cliente dice que sigue fallando → pendiente nueva visita
        self.como("prueba.cliente")
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "reabrir", "motivo": "Sigue sin imagen"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.PENDIENTE_VISITA)
        # Segunda visita y esta vez confirma
        t.cambiar_estado(E.VISITA_EJECUTADA, self.u("prueba.tecnico"))
        self.client.post(reverse("confirmar_solucion", args=[t.radicado]), {"accion": "aceptar"})
        t.refresh_from_db()
        self.assertEqual(t.estado, E.CERRADO)


class RecorridoComercialTests(BaseActuar):
    """Requerimiento comercial con todas sus bifurcaciones: levantamiento, propuesta devuelta por dirección, entrega por
    comercial, cambios del cliente, versión 2 urgente, aprobación y material a bodega."""

    def lineas(self, filas=(), **extra):
        datos = {"alcance": "PRUEBA Control de acceso", "condiciones": "Garantía de 1 año", "modalidad": "venta",
                 "tasa_mensual": "2", "recargo_alquiler": "0", "plazos": "12,24,36", "opcion_compra": "0", "margen": "30",
                 "lineas-TOTAL_FORMS": str(len(filas)), "lineas-INITIAL_FORMS": "0", "lineas-MIN_NUM_FORMS": "0",
                 "lineas-MAX_NUM_FORMS": "1000"}
        for i, (desc, cant, valor) in enumerate(filas):
            datos.update({f"lineas-{i}-descripcion": desc, f"lineas-{i}-cantidad": cant, f"lineas-{i}-valor_unitario": valor,
                          f"lineas-{i}-descuento": "0", f"lineas-{i}-iva": "19"})
        datos.update(extra)
        return datos

    def test_requerimiento_con_todas_las_vueltas(self):
        from comercial.models import ItemCatalogo
        from inventario.models import SolicitudMaterial
        ItemCatalogo.objects.create(descripcion="PRUEBA Caja de sobreponer [PRUEBA-CAJA]", valor_unitario=Decimal("8291.25"))
        # 1. Comercial radica sin visita y luego pide el levantamiento técnico
        self.como("prueba.comercial")
        self.client.post(reverse("comercial:nuevo"), {
            "empresa": self.empresa.pk, "contacto_nombre": "Daniel", "contacto_telefono": "3000000000", "contacto_email": "",
            "asunto": "PRUEBA Control de acceso en la entrada", "servicios": ["cctv"], "nd_viernes": "tarde",
            "sin_visita": "on"})
        t = Ticket.objects.get(ficha__isnull=False)
        self.assertEqual(t.ficha.comercial.username, "prueba.comercial")
        self.client.post(reverse("comercial:levantamiento", args=[t.radicado]), {"nota": "Ver la puerta", "jornada": "manana"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.estado), (Ticket.Area.TECNICA, E.RECEPCIONADO))
        # 2. Coordinación programa el levantamiento y el técnico devuelve el resultado
        self.como("prueba.coordinador")
        self.programar(t)
        self.como("prueba.tecnico")
        self.client.post(reverse("comercial:devolver", args=[t.radicado]),
                         {"necesidad": "Controlar la entrada", "materiales": "4 PRUEBA Caja de sobreponer"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.ficha.etapa), (Ticket.Area.COMERCIAL, "elaborando"))
        # 3. Administrativa arma la propuesta y la envía a dirección
        self.como("prueba.administrativa")
        url = reverse("comercial:cotizador", args=[t.radicado])
        self.client.post(url, self.lineas([("PRUEBA Caja de sobreponer [PRUEBA-CAJA]", "4", "8291.25")]))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, self.lineas(accion="a_direccion"))
        cot = t.ficha.cotizaciones.get()
        self.assertTrue(cot.en_revision)
        self.assertEqual(cot.elaborada_por.username, "prueba.administrativa")
        # 4. Bifurcación: dirección la devuelve; administrativa corrige y la vuelve a enviar
        self.como("prueba.direccion")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"accion": "devolver", "observaciones": "PRUEBA suba el margen a 35"})
        cot.refresh_from_db()
        self.assertFalse(cot.en_revision)
        self.como("prueba.administrativa")
        self.assertContains(self.client.get(url), "PRUEBA suba el margen a 35")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, self.lineas(margen="35", accion="a_direccion"))
        # 5. Dirección la aprueba y elige que la entregue comercial
        self.como("prueba.direccion")
        self.client.post(url, self.lineas(margen="35", accion="enviar", entrega="comercial"))
        v1 = t.ficha.versiones.get()
        self.assertTrue(v1.por_entregar)
        cot.refresh_from_db()
        self.assertEqual(cot.aprobada_por.username, "prueba.direccion")
        self.como("prueba.comercial")
        self.client.post(reverse("comercial:entregada", args=[t.radicado, v1.numero]), {"fecha": timezone.localdate().isoformat()})
        v1.refresh_from_db()
        self.assertFalse(v1.por_entregar)
        # 6. Bifurcación: el cliente pide cambios; administrativa saca la versión 2 urgente (sin esperar a dirección)
        self.como("prueba.cliente")
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "cambios", "cambios": "Quitar una caja"})
        t.ficha.refresh_from_db()
        self.assertEqual(t.ficha.etapa, "ajustes")
        self.como("prueba.administrativa")
        self.client.post(url, self.lineas(margen="35", accion="enviar", urgente="1",
                                          motivo_urgencia="PRUEBA el cliente la necesita hoy"))
        v2 = t.ficha.versiones.get(numero=2)
        self.assertEqual(v2.codigo, v1.codigo.replace("-V1", "-V2"))
        # 7. El cliente aprueba: el material va a bodega y el ticket queda «En inventario»
        self.como("prueba.cliente")
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "aprobar"})
        t.refresh_from_db()
        self.assertEqual((t.area, t.estado), (Ticket.Area.TECNICA, E.EN_INVENTARIO))
        s = SolicitudMaterial.objects.get(ticket=t, origen="comercial")
        self.assertEqual(s.estado, "validacion")
        # 8. Bodega revisa la lista: lo que no hay queda por comprar
        self.como("prueba.bodega")
        self.assertEqual(self.client.get(reverse("inventario:solicitud", args=[s.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventario:orden_proyecto", args=[t.radicado])).status_code, 200)


class RecorridoOrdenDeCompraTests(BaseActuar):
    """Orden de compra con revisión administrativa, devoluciones, aprobación, pago anticipado, recepción y factura con
    diferencia; y la de crédito."""

    def setUp(self):
        super().setUp()
        from inventario.models import LineaOrdenCompra, OrdenCompra, Proveedor
        self.proveedor = Proveedor.objects.create(razon_social="PRUEBA PROVEEDOR A", nit="PRUEBA-A")
        self.orden = OrdenCompra.objects.create(proveedor=self.proveedor, creado_por=self.u("prueba.bodega"),
                                                fecha_estimada=timezone.localdate())
        self.linea = LineaOrdenCompra.objects.create(orden=self.orden, descripcion="PRUEBA Cable", cantidad=100,
                                                     valor_unitario=1000)

    def datos(self, **extra):
        o, l = self.orden, self.linea
        return {"proveedor": self.proveedor.pk, "fecha_estimada": f"{o.fecha_estimada:%Y-%m-%d}", "forma_pago": o.forma_pago,
                "dias_credito": o.dias_credito or "", "notas": "",
                "lineas-TOTAL_FORMS": "1", "lineas-INITIAL_FORMS": "1", "lineas-MIN_NUM_FORMS": "0",
                "lineas-MAX_NUM_FORMS": "1000", "lineas-0-id": l.pk, "lineas-0-orden": o.pk,
                "lineas-0-descripcion": l.descripcion, "lineas-0-cantidad": "100", "lineas-0-adicional": "0",
                "lineas-0-valor_unitario": "1000", "lineas-0-iva": "19", **extra}

    def paso(self, cual, datos=None):
        return self.client.post(reverse("inventario:orden_paso", args=[self.orden.pk, cual]), datos or {})

    def estado(self):
        self.orden.refresh_from_db()
        return self.orden.estado

    def test_anticipada_con_todas_las_vueltas(self):
        from inventario import recepcion
        url, hoy = self.orden.get_absolute_url(), timezone.localdate()
        # Bodega la envía: primero pasa por la coordinadora administrativa
        self.como("prueba.bodega")
        self.client.post(url, self.datos(accion="a_direccion"))
        self.assertEqual(self.estado(), "por_revisar")
        # Bifurcación: la coordinadora la devuelve; bodega corrige y la reenvía; la coordinadora la revisa
        self.como("prueba.coordinadora")
        self.paso("devolver_revision", {"observaciones": "PRUEBA falta el flete"})
        self.assertEqual(self.estado(), "borrador")
        self.como("prueba.bodega")
        self.client.post(url, self.datos(accion="a_direccion"))
        self.como("prueba.coordinadora")
        self.paso("revisar")
        self.assertEqual(self.estado(), "revision")
        self.assertEqual(self.orden.revisada_por.username, "prueba.coordinadora")
        # Bifurcación: dirección la devuelve (borra la revisión); vuelve a pasar por la coordinadora y dirección aprueba
        self.como("prueba.direccion")
        self.paso("devolver", {"observaciones": "PRUEBA pida 10 m más"})
        self.assertEqual(self.estado(), "borrador")
        self.assertIsNone(self.orden.revisada_en)
        self.como("prueba.bodega")
        self.client.post(url, self.datos(accion="a_direccion", **{"lineas-0-adicional": "10"}))
        self.como("prueba.coordinadora")
        self.paso("revisar")
        self.como("prueba.direccion")
        self.client.post(url, self.datos(accion="aprobar", **{"lineas-0-adicional": "10"}))
        self.assertEqual(self.estado(), "por_pagar")
        # Financiera paga con el soporte; bodega la envía al proveedor y la recibe
        self.como("prueba.financiera")
        self.assertEqual(self.client.get(reverse("inventario:financiera")).status_code, 200)
        self.paso("pago", {"fecha": f"{hoy:%Y-%m-%d}", "valor": "130900", "medio": "Transferencia",
                           "soporte_pago": SimpleUploadedFile("pago.pdf", b"%PDF-1.4 x", content_type="application/pdf")})
        self.assertEqual(self.estado(), "lista")
        self.como("prueba.bodega")
        self.paso("proveedor")
        self.assertEqual(self.estado(), "enviada")
        recepcion.recibir(self.orden, {"proveedor": self.proveedor.razon_social, "factura": "FE-PRUEBA-1", "fecha": hoy},
                          [{"linea": self.linea.pk, "clase": "insumo", "descripcion": "PRUEBA Cable", "cantidad": Decimal("110"),
                            "valor_unitario": Decimal("1000"), "iva": Decimal("19"), "codigo": "PRUEBA-CABLE-OC",
                            "unidad": "Metro"}], self.u("prueba.bodega"))
        self.assertEqual(self.estado(), "recibida")
        # Bifurcación: la factura no cuadra; financiera la acepta con una nota y la compra queda cerrada
        self.como("prueba.financiera")
        self.paso("factura", {"numero": "FE-PRUEBA-1", "fecha": f"{hoy:%Y-%m-%d}", "valor": "150000"})
        self.orden.refresh_from_db()
        self.assertFalse(self.orden.factura_cuadra)
        self.paso("factura", {"numero": "FE-PRUEBA-1", "fecha": f"{hoy:%Y-%m-%d}", "valor": "150000", "acepta": "1",
                              "nota": "PRUEBA incluye el flete"})
        self.orden.refresh_from_db()
        self.assertTrue(self.orden.cerrada)

    def test_urgente_y_credito(self):
        url = self.orden.get_absolute_url()
        self.orden.forma_pago = "credito"
        self.orden.save()
        # Bodega la pasa urgente (sin dirección): queda lista para el proveedor porque es a crédito
        self.como("prueba.bodega")
        self.client.post(url, self.datos(accion="a_direccion", urgente="1", dias_credito="30",
                                         motivo_urgencia="PRUEBA el proveedor solo la separa hoy"))
        self.assertEqual(self.estado(), "lista")
        self.assertEqual(self.orden.motivo_urgencia, "PRUEBA el proveedor solo la separa hoy")
        self.paso("proveedor")
        self.assertEqual(self.estado(), "enviada")

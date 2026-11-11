"""Pruebas de «Mi día»: cada área abre en sus pendientes y dirección en un solo «Por aprobar»."""
from datetime import time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from cronograma.models import Actividad
from inventario.models import OrdenCompra, Proveedor
from tickets.models import Categoria, InformeBorrador, Ticket


class Base(TestCase):
    """Usuarios de cada área, un cliente y la forma de crear tickets."""

    def setUp(self):
        self.cat = Categoria.objects.get_or_create(nombre="Mantenimiento CCTV IP")[0]
        self.emp = Empresa.objects.create(razon_social="AGROPECUARIA GANACAMPO SAS", nit="902106030", ciudad="Villavicencio")
        self.sede = Sede.objects.create(empresa=self.emp, nombre="PRINCIPAL", ciudad="Villavicencio", direccion="LOTE 8")
        u = Usuario.objects.create_user
        self.cliente = u("cliente", "cliente@ganacampo.co", "Clave-segura-1", rol="cliente", empresa=self.emp)
        self.coord = u("jguana", "coord@janotec.test", "Clave-segura-1", rol="supervisor", first_name="Juan")
        self.miguel = u("mtibamoso", "miguel@janotec.test", "Clave-segura-1", rol="direccion", first_name="Miguel")
        self.tecnico = u("fgordillo", "freyder@janotec.test", "Clave-segura-1", rol="agente", first_name="Freyder")
        self.bodega = u("bodega", "bodega@janotec.test", "Clave-segura-1", rol="bodega")
        self.comercial = u("comercial", "comercial@janotec.test", "Clave-segura-1", rol="comercial")
        self.admva = u("admva", "admva@janotec.test", "Clave-segura-1", rol="administrativo")
        self.finan = u("finan", "finan@janotec.test", "Clave-segura-1", rol="financiera")
        self.central = u("central", "central@janotec.test", "Clave-segura-1", rol="central")

    def ticket(self, **kw):
        datos = dict(empresa=self.emp, sede=self.sede, solicitante=self.cliente, radicado_por=self.coord, categoria=self.cat,
                     asunto="Cámara sin imagen", descripcion="La cámara de la entrada no da imagen", impacto="media",
                     contacto_nombre="Daniel", contacto_telefono="3214061219", contacto_email="cliente@ganacampo.co")
        datos.update(kw)
        return Ticket.objects.create(**datos)

    def ver(self, usuario):
        self.client.force_login(usuario)
        r = self.client.get(reverse("mi_dia"))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()


class MiDiaTests(Base):
    """Qué ve cada área al entrar y que nadie vea lo que no le toca."""

    def test_cada_area_abre_en_mi_dia_y_el_cliente_en_sus_solicitudes(self):
        for usuario in (self.coord, self.miguel, self.tecnico, self.bodega, self.comercial, self.admva, self.finan):
            self.client.force_login(usuario)
            self.assertRedirects(self.client.get(reverse("inicio")), reverse("mi_dia"), msg_prefix=usuario.username)
        for usuario in (self.cliente, self.central):
            self.client.force_login(usuario)
            self.assertRedirects(self.client.get(reverse("inicio")), reverse("mis_tickets"), msg_prefix=usuario.username)
            self.assertRedirects(self.client.get(reverse("mi_dia")), reverse("inicio"), target_status_code=302)

    def test_direccion_ve_un_solo_por_aprobar(self):
        t = self.ticket(estado=Ticket.Estado.INFORME)
        InformeBorrador.objects.create(ticket=t, estado=InformeBorrador.Estado.REVISION, destinatario="Señores", asunto="Informe",
                                       introduccion="x", actividades="x", conclusiones="x")
        oc = OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="SYSCOM COLOMBIA", nit="900"),
                                        estado=OrdenCompra.Estado.REVISION)
        self.ticket()  # uno nuevo por recepcionar: es de coordinación, no de dirección
        html = self.ver(self.miguel)
        self.assertIn("<h1 class=\"h3 page-title mb-1\">Por aprobar</h1>", html)
        self.assertIn(f"Informe {t.radicado}", html)
        self.assertIn(f"Orden de compra {oc.numero}", html)
        self.assertIn(reverse("inventario:orden", args=[oc.pk]), html)
        self.assertNotIn("Por recepcionar", html)
        self.assertIn('title="Esperan la firma de dirección">2</span>', html)  # la etiqueta del menú

    def test_coordinacion_ve_lo_suyo_y_no_lo_de_direccion(self):
        nuevo = self.ticket()
        programable = self.ticket(estado=Ticket.Estado.RECEPCIONADO, asignado_a=self.tecnico)
        OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="GVS", nit="901"), estado=OrdenCompra.Estado.REVISION)
        html = self.ver(self.coord)
        self.assertIn(nuevo.radicado, html)
        self.assertIn(reverse("recepcionar", args=[nuevo.radicado]), html)
        self.assertIn(programable.radicado, html)
        self.assertIn("Programar visita", html)
        self.assertNotIn("Orden de compra", html)

    def test_cada_pendiente_trae_el_contexto_corto_de_la_solicitud(self):
        """Como en recepción: además del ticket y la empresa, el asunto, el tipo y el sistema."""
        self.ticket(asunto="Agregar nuevamente el NVR a la central de monitoreo para poder visualizar las cámaras",
                    tipo=Ticket.Tipo.SOPORTE)
        html = self.ver(self.coord)
        self.assertIn("Agregar nuevamente el NVR a la central de monitoreo", html)
        self.assertIn('<i class="bi bi-tools"></i> Servicio técnico · Mantenimiento CCTV IP', html)

    def test_visita_pasada_sin_acta_le_aparece_al_tecnico_y_al_coordinador(self):
        t = self.ticket(estado=Ticket.Estado.EN_CRONOGRAMA, asignado_a=self.tecnico)
        a = Actividad.objects.create(ticket=t, fecha=timezone.localdate() - timedelta(days=2), hora_inicio=time(8), hora_fin=time(12),
                                     cliente=self.emp.razon_social, actividad="Revisar cámara")
        a.encargados.add(self.tecnico)
        hoy = self.ticket(estado=Ticket.Estado.AUTORIZADO, asignado_a=self.tecnico)
        b = Actividad.objects.create(ticket=hoy, fecha=timezone.localdate(), hora_inicio=time(14), hora_fin=time(16),
                                     cliente=self.emp.razon_social, actividad="Mantenimiento")
        b.encargados.add(self.tecnico)
        html = self.ver(self.tecnico)
        self.assertIn("Acta por hacer", html)
        self.assertIn(reverse("acta_nueva", args=[t.radicado]), html)
        self.assertIn(f"Hoy 14:00 · {hoy.radicado}", html)
        self.assertIn(t.radicado, self.ver(self.coord))

    def test_financiera_ve_lo_que_debe_pagar(self):
        oc = OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="SYSCOM", nit="900"),
                                        estado=OrdenCompra.Estado.POR_PAGAR)
        html = self.ver(self.finan)
        self.assertIn(oc.numero, html)
        self.assertIn("Registrar pago", html)

    def test_sin_pendientes_dice_todo_al_dia_y_las_tarjetas_en_cero_se_apagan(self):
        html = self.ver(self.finan)
        self.assertIn("Todo al día", html)
        self.assertIn('class="kpi cero"', html)

    def test_el_usuario_de_prueba_solo_ve_lo_de_prueba(self):
        from accounts.prueba import asegurar_usuarios, empresa_prueba
        asegurar_usuarios()
        real = self.ticket()
        prueba = self.ticket(empresa=empresa_prueba(), sede=empresa_prueba().sedes.first())
        html = self.ver(Usuario.objects.get(username="prueba.coordinador"))
        self.assertIn(prueba.radicado, html)
        self.assertNotIn(real.radicado, html)


class FiltrosYSiguientePasoTests(Base):
    """Fase 3: filtros como etiquetas con su número y la columna «Siguiente paso» en las tablas."""

    def test_panel_tecnico_con_etiquetas_y_siguiente_paso(self):
        nuevo = self.ticket()
        self.ticket(estado=Ticket.Estado.RECEPCIONADO, asignado_a=self.tecnico)
        self.client.force_login(self.coord)
        html = self.client.get(reverse("panel")).content.decode()
        self.assertIn('Por recepcionar <span class="n">1</span>', html)
        self.assertIn('Por programar <span class="n">1</span>', html)
        self.assertIn("Siguiente paso", html)
        self.assertIn(f'data-confirmar-recepcion="{nuevo.radicado}"', html)
        self.assertIn(">Programar <i", html)
        # La etiqueta filtra y conserva la búsqueda
        r = self.client.get(reverse("panel") + "?estado=por_programar&q=Cámara")
        self.assertNotIn(nuevo.radicado, r.content.decode())
        self.assertIn("«Cámara»", r.content.decode())  # filtro activo con su «×»

    def test_el_tecnico_no_ve_botones_de_coordinacion(self):
        t = self.ticket(estado=Ticket.Estado.RECEPCIONADO, asignado_a=self.tecnico)
        self.client.force_login(self.tecnico)
        html = self.client.get(reverse("panel") + "?estado=abiertos").content.decode()
        self.assertIn(t.radicado, html)
        self.assertNotIn(">Programar <i", html)
        self.assertIn("Programar la visita", html)

    def test_ordenes_de_compra_con_siguiente_paso(self):
        OrdenCompra.objects.create(proveedor=Proveedor.objects.create(razon_social="SYSCOM", nit="900"),
                                   estado=OrdenCompra.Estado.POR_PAGAR)
        self.client.force_login(self.miguel)
        html = self.client.get(reverse("inventario:ordenes")).content.decode()
        self.assertIn('Abiertas <span class="n">1</span>', html)
        self.assertIn("Pago de contado", html)

    def test_bandeja_comercial_con_etiquetas(self):
        self.client.force_login(self.comercial)
        r = self.client.get(reverse("comercial:bandeja"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Solo míos")
        self.assertContains(r, 'A cargo de comercial <span class="n">0</span>')


class CelularTests(Base):
    """Fase 4: la barra inferior del celular solo para técnicos, coordinación y bodega."""

    def test_barra_inferior_por_area(self):
        self.client.force_login(self.tecnico)
        html = self.client.get(reverse("mi_dia")).content.decode()
        self.assertIn('class="barra-inferior', html)
        self.assertIn(reverse("inventario:mi_material"), html.split('class="barra-inferior')[1])
        self.client.force_login(self.bodega)
        barra = self.client.get(reverse("mi_dia")).content.decode().split('class="barra-inferior')[1]
        self.assertIn(reverse("inventario:ordenes"), barra)
        self.assertNotIn(reverse("cronograma:semana"), barra)
        for usuario in (self.cliente, self.finan):
            self.client.force_login(usuario)
            r = self.client.get(reverse("inicio"), follow=True)
            self.assertNotIn('class="barra-inferior', r.content.decode(), usuario.username)


class InformesCoordinacionTests(Base):
    """Coordinación ve cada informe con su siguiente paso; el PDF lleva la firma centrada del coordinador."""

    def borrador(self, t, estado, **kw):
        return InformeBorrador.objects.create(ticket=t, estado=estado, destinatario="Señores", asunto="Informe",
                                              introduccion="x", actividades="x", conclusiones="x", **kw)

    def test_cada_informe_con_su_paso(self):
        sin = self.ticket(estado=Ticket.Estado.INFORME, asunto="Sin borrador")
        por_enviar = self.ticket(estado=Ticket.Estado.INFORME, asunto="Por enviar")
        self.borrador(por_enviar, InformeBorrador.Estado.BORRADOR, creado_por=self.coord)
        en_direccion = self.ticket(estado=Ticket.Estado.INFORME, asunto="Con dirección")
        self.borrador(en_direccion, InformeBorrador.Estado.REVISION)
        aprobado = self.ticket(estado=Ticket.Estado.INFORME, asunto="Aprobado")
        self.borrador(aprobado, InformeBorrador.Estado.APROBADO, aprobado_por=self.miguel, aprobado_en=timezone.now())
        html = self.ver(self.coord)
        self.assertIn("Informes técnicos", html)
        for t, boton in ((sin, "Generar borrador"), (por_enviar, "Revisar y enviar a dirección"),
                         (en_direccion, "Ver informe"), (aprobado, "Entregar informe")):
            self.assertIn(t.radicado, html)
            self.assertIn(boton, html)
        self.assertIn("Aprobado por Miguel", html)

    def test_la_pagina_del_informe_muestra_el_paso_arriba(self):
        t = self.ticket(estado=Ticket.Estado.INFORME)
        self.borrador(t, InformeBorrador.Estado.BORRADOR, creado_por=self.coord)
        self.client.force_login(self.coord)
        html = self.client.get(reverse("informe_borrador", args=[t.radicado])).content.decode()
        self.assertIn("Le toca a usted: revise el texto y oprima «Enviar a dirección»", html)
        self.assertIn('form="form-informe"', html)
        InformeBorrador.objects.filter(ticket=t).update(estado=InformeBorrador.Estado.REVISION)
        self.client.force_login(self.miguel)
        html = self.client.get(reverse("informe_borrador", args=[t.radicado])).content.decode()
        self.assertIn("Le toca a usted: revíselo y apruébelo", html)
        self.assertIn("Aprobar y pasar a limpio", html)

    def test_firma_del_coordinador_centrada(self):
        from tickets import informe

        t = self.ticket(estado=Ticket.Estado.INFORME)
        b = self.borrador(t, InformeBorrador.Estado.BORRADOR, creado_por=self.miguel)
        self.coord.last_name, self.coord.cargo = "Guana", "Coordinador TIC"
        self.coord.save()
        self.assertEqual(informe.firmante(b), self.coord)  # aunque lo haya generado dirección, firma el coordinador
        tabla = informe._bloque_firma(self.coord, 400)
        textos = " ".join(getattr(c[0], "text", str(c[0])) for c in tabla._cellvalues)
        self.assertIn("Juan Guana", textos)
        self.assertIn("Coordinador TIC", textos)
        self.assertEqual(tabla.hAlign, "CENTER")
        nombre, contenido = informe.pdf(b)
        self.assertTrue(contenido.startswith(b"%PDF"))

    def test_con_firma_digital_va_la_imagen_tal_cual(self):
        import shutil
        import tempfile
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from PIL import Image
        from reportlab.platypus import Image as ImagenPDF

        from tickets import informe

        carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, carpeta, ignore_errors=True)
        png = BytesIO()
        Image.new("RGBA", (400, 200), (255, 255, 255, 0)).save(png, "PNG")
        with override_settings(MEDIA_ROOT=carpeta):
            self.coord.firma = SimpleUploadedFile("firma.png", png.getvalue(), content_type="image/png")
            self.coord.save()
            bloque = informe._bloque_firma(self.coord, 400)
        self.assertIsInstance(bloque, ImagenPDF)  # la imagen completa, sin texto agregado
        self.assertEqual(bloque.hAlign, "CENTER")
        self.assertAlmostEqual(bloque.drawWidth / bloque.drawHeight, 2, places=2)  # sin deformarla

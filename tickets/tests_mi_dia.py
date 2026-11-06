"""Pruebas de «Mi día»: cada área abre en sus pendientes y dirección en un solo «Por aprobar»."""
from datetime import time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from cronograma.models import Actividad
from inventario.models import OrdenCompra, Proveedor
from tickets.models import Categoria, InformeBorrador, Ticket


class MiDiaTests(TestCase):
    """Qué ve cada área al entrar y que nadie vea lo que no le toca."""

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

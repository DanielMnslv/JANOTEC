"""Usuarios de prueba por área y «Actuar como»: el superusuario recorre todo el proceso él solo.

Para validar el proceso de principio a fin hacen falta personas distintas: quien arma la orden de compra no puede ser
quien la aprueba, y dirección que envía algo lo aprueba de una vez (así no se podrían probar las devoluciones). Por eso
hay un usuario de prueba por área (``prueba.comercial``, ``prueba.direccion``, ``prueba.tecnico``…) y el superusuario
entra como cualquiera de ellos con «Actuar como», sin contraseñas, y vuelve a su usuario con un clic.

Reglas de seguridad:
    * Solo un superusuario activo puede actuar como otro, y solo como un usuario de prueba (nombre ``prueba.…``).
    * Los usuarios de prueba no tienen contraseña (no se puede iniciar sesión con ellos) ni correo: no reciben avisos
      y no cambian a quién le llegan los avisos reales (los avisos se buscan entre quienes tienen correo).
    * Mientras actúa como otro, la plataforma no deja guardar nada sobre tickets, solicitudes, órdenes ni clientes que
      no sean de PRUEBA: los datos reales no se tocan.
    * Cada cambio de usuario queda en el registro de Configuración (quién actuó como quién y cuándo).
"""
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

PREFIJO = "prueba."
# Clave de sesión donde se guarda el superusuario que está actuando como otro
SESION = "actuar_como_original"

# (usuario, nombre que se ve, rol, opciones adicionales, qué hace en el proceso)
USUARIOS = [
    ("prueba.cliente", "Cliente", "cliente", {},
     "El cliente de la empresa PRUEBA: radica solicitudes, pide cotizaciones, aprueba, pide cambios o rechaza la "
     "propuesta, autoriza visitas, confirma la solución y califica."),
    ("prueba.central", "Central de atención", "central", {},
     "Recibe solicitudes por teléfono y las registra a nombre del cliente."),
    ("prueba.comercial", "Comercial", "comercial", {},
     "Radica requerimientos, pide el levantamiento técnico, entrega la propuesta, registra la respuesta del cliente."),
    ("prueba.coordinador", "Coordinador técnico", "supervisor", {},
     "Hace de Juan Daniel Guana: recepciona, programa visitas, arma el informe técnico, autoriza la salida de material."),
    ("prueba.tecnico", "Técnico", "agente", {},
     "Va a la visita: devuelve el levantamiento, recibe el material y diligencia el acta."),
    ("prueba.administrativa", "Administrativa", "administrativo", {},
     "Arma la propuesta en el cotizador, el catálogo y la cotización con proveedores."),
    ("prueba.coordinadora", "Coordinadora administrativa", "administrativo", {"revisa_compras": True},
     "Hace de Yuri: revisa las órdenes de compra antes de dirección (Revisó)."),
    ("prueba.bodega", "Bodega", "bodega", {},
     "Valida el material, arma las órdenes de compra (Realizó), recibe la mercancía y entrega al técnico."),
    ("prueba.direccion", "Dirección", "direccion", {},
     "Hace de Miguel: aprueba o devuelve informes, propuestas y órdenes de compra; firma la salida de material."),
    ("prueba.financiera", "Financiera", "financiera", {},
     "Paga las órdenes de compra (de contado o con anticipo y su saldo) y valida la factura contra la orden."),
]
OPCIONES = ("tambien_comercial", "tambien_administrativo", "tambien_financiera", "tambien_tecnico", "revisa_compras")


def q_de_prueba(prefijo=""):
    """Filtro de los usuarios de prueba (``prefijo`` para filtrar por una relación, p. ej. «asignado_a__»)."""
    return Q(**{f"{prefijo}username__startswith": PREFIJO})


def es_de_prueba(usuario):
    """¿Es uno de los usuarios de prueba de las áreas?"""
    return bool(usuario and getattr(usuario, "username", "").startswith(PREFIJO) and not usuario.is_superuser)


def es_nombre_prueba(texto):
    """La empresa, el proveedor o el ítem es de prueba si su nombre empieza por «PRUEBA»."""
    return (texto or "").strip().upper().startswith("PRUEBA")


def empresa_prueba():
    """La empresa PRUEBA (la crea si no existe), con su sede PRUEBA."""
    from accounts.models import Empresa, Sede

    empresa = Empresa.objects.filter(razon_social="PRUEBA").order_by("pk").first()
    if empresa is None:
        empresa = Empresa.objects.create(razon_social="PRUEBA", ciudad="Villavicencio")
    if not empresa.sedes.filter(nombre="PRUEBA").exists():
        Sede.objects.create(empresa=empresa, nombre="PRUEBA", ciudad="Villavicencio", direccion="Dirección de prueba")
    return empresa


def asegurar_usuarios():
    """Crea los usuarios de prueba que falten y deja los existentes como deben estar. Devuelve cuántos creó."""
    from accounts.models import Usuario

    empresa = empresa_prueba()
    creados = 0
    for username, nombre, rol, opciones, _que in USUARIOS:
        u = Usuario.objects.filter(username=username).first()
        if u is None:
            u = Usuario(username=username)
            creados += 1
        u.first_name, u.last_name = "PRUEBA", nombre
        u.rol, u.email, u.is_active, u.is_superuser = rol, "", True, False
        u.empresa = empresa if rol == "cliente" else None
        for campo in OPCIONES:
            setattr(u, campo, bool(opciones.get(campo)))
        u.set_unusable_password()  # nadie inicia sesión con ellos: solo se entra con «Actuar como»
        u.save()
    return creados


def usuarios():
    """Los usuarios de prueba en el orden del proceso, con lo que hace cada uno."""
    from accounts.models import Usuario

    existentes = {u.username: u for u in Usuario.objects.filter(q_de_prueba(), is_active=True)}
    return [(existentes.get(username), nombre, que) for username, nombre, _r, _o, que in USUARIOS]


def original(request):
    """El superusuario que está actuando como otro (None si no está actuando)."""
    from accounts.models import Usuario

    pk = request.session.get(SESION)
    if not pk:
        return None
    return Usuario.objects.filter(pk=pk, is_active=True, is_superuser=True).first()


def registrar(autor, usuario, mensaje):
    """Deja en el registro de Configuración quién actuó como quién."""
    LogEntry.objects.create(user_id=autor.pk, content_type_id=ContentType.objects.get_for_model(usuario).pk,
                            object_id=str(usuario.pk), object_repr=str(usuario)[:200], action_flag=CHANGE,
                            change_message=mensaje)


# --- Qué se puede guardar mientras se actúa como otro ----------------------------------------------
def objeto_es_de_prueba(nombre_vista, kwargs, datos):
    """¿El guardado que se intenta toca solo cosas de PRUEBA? Revisa el ticket, la solicitud de material, la orden de
    compra, la actividad o el cliente de la dirección (y la empresa que viene en el formulario)."""
    from accounts.models import Empresa
    from tickets.models import Ticket

    def ticket_ok(ticket):
        return ticket is None or es_nombre_prueba(ticket.empresa.razon_social)

    if kwargs.get("radicado"):
        t = Ticket.objects.filter(radicado=str(kwargs["radicado"]).upper()).select_related("empresa").first()
        if t is not None and not ticket_ok(t):
            return False
    pk = kwargs.get("pk")
    if pk and nombre_vista.startswith(("inventario:solicitud", )):
        from inventario.models import SolicitudMaterial
        s = SolicitudMaterial.objects.filter(pk=pk).select_related("ticket__empresa").first()
        if s is not None and not ticket_ok(s.ticket):
            return False
    if pk and nombre_vista.startswith(("inventario:orden", )):
        from inventario.models import OrdenCompra
        o = OrdenCompra.objects.filter(pk=pk).select_related("proveedor", "ticket__empresa").first()
        if o is not None and not (es_nombre_prueba(o.proveedor.razon_social) and ticket_ok(o.ticket)):
            return False
    if pk and nombre_vista == "resolver_devolucion":
        from tickets.models import Devolucion
        d = Devolucion.objects.filter(pk=pk).select_related("ticket__empresa").first()
        if d is not None and not ticket_ok(d.ticket):
            return False
    if pk and nombre_vista.startswith("cronograma:"):
        from cronograma.models import Actividad
        a = Actividad.objects.filter(pk=pk).select_related("ticket__empresa").first()
        if a is not None and not (ticket_ok(a.ticket) if a.ticket_id else es_nombre_prueba(a.cliente)):
            return False
    for clave in ("empresa_pk", ) + (("pk", ) if nombre_vista in ("inventario:cliente", "cliente_detalle",
                                                                  "cliente_editar_admin", "cliente_eliminar") else ()):
        if kwargs.get(clave):
            e = Empresa.objects.filter(pk=kwargs[clave]).first()
            if e is not None and not es_nombre_prueba(e.razon_social):
                return False
    ticket = datos.get("ticket")
    if ticket and str(ticket).isdigit():
        t = Ticket.objects.filter(pk=ticket).select_related("empresa").first()
        if t is not None and not ticket_ok(t):
            return False
    empresa = datos.get("empresa")
    if empresa and str(empresa).isdigit():
        e = Empresa.objects.filter(pk=empresa).first()
        if e is not None and not es_nombre_prueba(e.razon_social):
            return False
    return True

from decimal import Decimal

from django import template

register = template.Library()


def _miles(n):
    return f"{n:,}".replace(",", ".")


@register.filter
def pesos(valor):
    """1574274.8 → $1.574.275"""
    if valor in (None, ""):
        return "—"
    return "$" + _miles(int(Decimal(valor).quantize(Decimal("1"))))


@register.filter
def cant(valor):
    """Decimal('38.00') → 38 · Decimal('2.50') → 2,5"""
    if valor in (None, ""):
        return "—"
    d = Decimal(valor).normalize()
    entero, _, decimales = f"{d:f}".partition(".")
    signo = "-" if entero.startswith("-") else ""
    texto = signo + _miles(abs(int(entero or 0)))
    return f"{texto},{decimales}" if decimales else texto


@register.simple_tag
def alistamiento(ticket):
    """Las listas de alistamiento del ticket (una por solicitud de material que no se canceló)."""
    from inventario import solicitudes as sol
    return [sol.lista_alistamiento(s) for s in ticket.solicitudes_material.exclude(estado="cancelada").order_by("creado")]


@register.simple_tag
def puede_firmar(solicitud, usuario):
    from inventario import solicitudes as sol
    return sol.roles_que_puede_firmar(solicitud, usuario)


@register.simple_tag
def cuadre_ticket(ticket):
    """Entregado = instalado + devuelto, por material y técnico."""
    from inventario import solicitudes as sol
    return sol.cuadre(ticket)


# El menú de bodega, en un solo lugar: lo usan el menú de arriba y el encabezado de cada pantalla del inventario
MENU_BODEGA = [
    ("Mi trabajo", "bi-inboxes", [
        ("Tickets en bodega", "inventario:bandeja", "", "bi-inboxes"),
        ("Solicitudes de material", "inventario:solicitudes", "", "bi-clipboard-check"),
        ("Por entregar", "inventario:panel", "?ver=entregas", "bi-truck"),
    ]),
    ("Inventario", "bi-boxes", [
        ("Inventario: todos los productos", "inventario:productos", "", "bi-grid-3x3-gap"),
        ("Etiquetas de placas (QR)", "inventario:etiquetas", "", "bi-qr-code"),
        ("Herramientas", "inventario:herramientas", "", "bi-tools"),
        ("Préstamos y alquileres", "inventario:prestamos", "", "bi-arrow-left-right"),
        ("Por reponer (stock mínimo)", "inventario:productos", "?bajos=1", "bi-exclamation-triangle"),
    ]),
    ("Compras", "bi-cart", [
        ("Órdenes de compra", "inventario:ordenes", "", "bi-receipt"),
        ("Listado de compras (facturas)", "inventario:facturas", "", "bi-journal-text"),
        ("Ingreso desde la factura (sin orden)", "inventario:ingreso_factura", "", "bi-file-earmark-arrow-down"),
        ("Compra suelta (sin orden)", "inventario:compra", "", "bi-cart-plus"),
        ("Proveedores", "inventario:contactos", "?tipo=proveedores", "bi-truck-front"),
    ]),
    ("Movimientos", "bi-arrow-left-right", [
        ("Entregar a técnico", "inventario:entrega", "", "bi-box-arrow-right"),
        ("Material con técnicos y devoluciones", "inventario:panel", "?ver=tecnicos", "bi-box-arrow-in-left"),
        ("Garantías y devoluciones al proveedor", "inventario:garantias", "", "bi-shield-exclamation"),
    ]),
    ("Configuración", "bi-gear", [
        ("Marcas", "inventario:catalogo", "marcas", "bi-tag"),
        ("Categorías", "inventario:catalogo", "categorias", "bi-diagram-3"),
        ("Clientes", "inventario:contactos", "", "bi-building"),
        ("Funcionarios", "inventario:funcionarios", "", "bi-people"),
    ]),
]


@register.simple_tag
def menu_bodega():
    """[{titulo, icono, opciones: [{nombre, url, icono}]}]; «Marcas»/«Categorías» llevan el tipo en la ruta."""
    from django.urls import reverse

    grupos = []
    for titulo, icono, opciones in MENU_BODEGA:
        lista = []
        for nombre, ruta, extra, ic in opciones:
            url = reverse(ruta, args=[extra]) if ruta == "inventario:catalogo" else reverse(ruta) + extra
            lista.append({"nombre": nombre, "url": url, "icono": ic})
        grupos.append({"titulo": titulo, "icono": icono, "opciones": lista})
    return grupos

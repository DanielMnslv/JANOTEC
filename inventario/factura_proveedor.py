"""Dar ingreso a bodega desde la factura del proveedor: se sube el PDF (o el XML/ZIP de la factura electrónica), la
plataforma llena las filas con los equipos y materiales que trae, la persona las revisa y todo entra de una vez.

Se usa en dos lugares:

* **Recibir una orden de compra**: los ítems de la factura se ponen en las líneas de la orden que les corresponden
  (`lectura_factura.emparejar`) y la factura queda como soporte de la orden.
* **Ingreso desde la factura** (compra sin orden): cada ítem es una fila nueva (`lectura_factura.sugerir_fila`).

La lectura nunca guarda nada: solo llena el formulario. Entre la lectura y el ingreso el archivo queda en la carpeta
temporal (`temporales`).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files import File
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse

from . import lectura_factura, recepcion, servicios, temporales
from .models import Activo, CodigoActivo, Insumo, MovimientoInsumo, Proveedor
from .views import _liberar_compras, gestion_requerida

EXTENSIONES = ("pdf", "xml", "zip")


def leer_subida(request, campo="factura_archivo"):
    """Lee la factura subida. Devuelve (factura, token, extensión) o None (con el mensaje de error ya puesto)."""
    archivo = request.FILES.get(campo)
    extension = (archivo.name.rsplit(".", 1)[-1].lower() if archivo and "." in archivo.name else "")
    if not archivo or extension not in EXTENSIONES:
        messages.error(request, "Suba la factura en PDF, o el XML o ZIP de la factura electrónica que manda el proveedor.")
        return None
    if archivo.size > 15 * 1024 * 1024:
        messages.error(request, "La factura pesa más de 15 MB.")
        return None
    token, ruta = temporales.guardar(archivo, "factura", extension)
    try:
        with open(ruta, "rb") as f:
            factura = lectura_factura.leer(f, archivo.name)
    except lectura_factura.FacturaIlegible as e:
        ruta.unlink(missing_ok=True)
        messages.error(request, f"No se pudo leer la factura: {e}")
        return None
    except Exception:  # noqa: BLE001 - archivo raro: se avisa en lugar de fallar
        ruta.unlink(missing_ok=True)
        messages.error(request, "No se pudo leer la factura. Regístrela a mano o súbala en XML/ZIP.")
        return None
    return factura, token, extension


def proveedor_de(factura):
    """El nombre del proveedor como está en la plataforma (por NIT o por nombre); si no está, como viene en la factura."""
    from .importar_compras import clave_nombre, nit_limpio

    for p in Proveedor.objects.exclude(nit=""):
        if factura.nit and nit_limpio(p.nit) == factura.nit:
            return p.razon_social
    clave = clave_nombre(factura.proveedor)
    for p in Proveedor.objects.all():
        if clave and clave_nombre(p.razon_social) == clave:
            return p.razon_social
    return factura.proveedor[:200]


def ya_ingresada(numero, proveedor):
    """¿Ya entró a bodega algo con este número de factura y este proveedor? Para no ingresarla dos veces."""
    if not numero:
        return False
    filtro = Q(factura__iexact=numero) & Q(proveedor__iexact=proveedor)
    return Activo.objects.filter(filtro).exists() or MovimientoInsumo.objects.filter(filtro).exists()


def cabecera_inicial(factura):
    return {"factura": factura.numero[:60], "fecha": factura.fecha or servicios.timezone.localdate(),
            "proveedor": proveedor_de(factura)}


def soporte_temporal(request):
    """La factura leída antes, para guardarla como soporte si no subieron otra al confirmar. None si no hay."""
    extension = request.POST.get("factura_ext", "")
    ruta = temporales.ruta(request.POST.get("factura_token", ""), "factura", extension) if extension in EXTENSIONES else None
    return ruta if ruta and ruta.exists() else None


def contexto_lectura(factura, token, extension):
    return {"lectura": factura, "factura_token": token, "factura_ext": extension}


def datalists():
    return {"proveedores": Proveedor.objects.values_list("razon_social", flat=True),
            "insumos": Insumo.objects.filter(activo=True), "codigos": CodigoActivo.objects.all()}


@login_required
@gestion_requerida
def ingreso_factura(request):
    """Compra sin orden: se sube la factura, se revisan las filas que trae y todo entra a bodega de una vez."""
    contexto = {"orden": None, **datalists()}
    if request.method == "POST" and request.POST.get("accion") == "leer":
        leida = leer_subida(request)
        if leida:
            factura, token, extension = leida
            inicial = cabecera_inicial(factura)
            filas = [lectura_factura.sugerir_fila(i, factura.tasa_iva) for i in factura.items]
            filas += [{"clase": "insumo", "iva": factura.tasa_iva}] * 2
            contexto.update(contexto_lectura(factura, token, extension),
                            cabecera=recepcion.RecepcionForm(initial=inicial),
                            filas=recepcion.FilasRecepcion(initial=filas, prefix="filas"),
                            repetida=ya_ingresada(inicial["factura"], inicial["proveedor"]))
        return render(request, "inventario/orden_recibir.html", contexto)
    if request.method == "POST":
        cabecera = recepcion.RecepcionForm(request.POST, request.FILES)
        filas = recepcion.FilasRecepcion(request.POST, prefix="filas")
        if cabecera.is_valid() and filas.is_valid():
            datos = [f.cleaned_data for f in filas.forms if f.cleaned_data and not f.vacia]
            if not any(d.get("clase") != "no" and d.get("cantidad") for d in datos):
                messages.error(request, "No marcó nada como recibido.")
            else:
                insumos, placas = recepcion.recibir(None, cabecera.cleaned_data, datos, request.user)
                ruta = soporte_temporal(request)
                if ruta:
                    ruta.unlink(missing_ok=True)
                _liberar_compras(request)
                partes = ([f"insumos: {', '.join(insumos)}"] if insumos else []) + ([f"equipos: {', '.join(placas)}"] if placas else [])
                messages.success(request, f"Factura {cabecera.cleaned_data['factura']} ingresada a bodega. Entraron " + "; ".join(partes) + ".")
                if placas:
                    return redirect(f"{reverse('inventario:etiquetas')}?placas={','.join(placas)}")
                return redirect("inventario:productos")
        else:
            messages.error(request, "Revise las filas marcadas.")
        contexto.update(cabecera=cabecera, filas=filas, factura_token=request.POST.get("factura_token", ""),
                        factura_ext=request.POST.get("factura_ext", ""))
    return render(request, "inventario/orden_recibir.html", contexto)


def adjuntar_soporte(request, cabecera_datos):
    """Si no subieron la factura en el formulario pero se leyó antes, esa queda como soporte de la orden."""
    if cabecera_datos.get("soporte"):
        return cabecera_datos["soporte"], None
    ruta = soporte_temporal(request)
    if not ruta:
        return None, None
    nombre = f"factura_{(cabecera_datos.get('factura') or 'proveedor').replace('/', '-')}.{ruta.suffix.lstrip('.')}"
    return File(open(ruta, "rb"), name=nombre), ruta

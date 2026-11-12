"""Proveedores: quién nos atiende en cada uno, qué marcas maneja y qué nos ha vendido.

Lo vendido no se digita: sale de las compras registradas, que guardan el proveedor como texto y a veces escrito distinto
(«AS SECURITY  SAS», «as security sas»), por eso se compara normalizado. Las marcas son las que el encargado indica que
maneja el proveedor más las que ya nos ha vendido.
"""
from collections import Counter, defaultdict
from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from tickets.forms import BootstrapMixin

from .models import Activo, AsesorProveedor, CompraHistorica, MovimientoActivo, MovimientoInsumo, Proveedor, normalizar
from .productos import _clave, _registro
from .views import gestion_requerida, inventario_requerido

SIN_MARCA = {"", "GENERICO", "SIN MARCA"}
COMPRA = MovimientoActivo.Tipo.COMPRA


def _compras_materiales():
    return MovimientoInsumo.objects.filter(tipo=COMPRA).exclude(proveedor="")


def _textos_de(proveedor):
    """Cómo quedó escrito este proveedor en las compras."""
    nombre = normalizar(proveedor.razon_social)
    escritos = set(Activo.objects.exclude(proveedor="").order_by().values_list("proveedor", flat=True).distinct())
    escritos |= set(_compras_materiales().order_by().values_list("proveedor", flat=True).distinct())
    return [t for t in escritos if normalizar(t) == nombre]


def compras_de(proveedor):
    """Lo que nos ha vendido: equipos por modelo y materiales, con la última compra de cada uno."""
    textos = _textos_de(proveedor)
    equipos, materiales = {}, {}
    for a in Activo.objects.filter(proveedor__in=textos).order_by("fecha_compra", "id"):
        e = equipos.setdefault(_clave(a.referencia), {"referencia": a.referencia.strip(), "marca": a.marca, "descripcion": a.descripcion,
                                                      "unidades": 0, "ultima": None, "valor": None})
        e["unidades"] += 1
        if a.fecha_compra:
            e.update(ultima=a.fecha_compra, valor=a.valor)
    for m in _compras_materiales().filter(proveedor__in=textos).select_related("insumo").order_by("fecha", "id"):
        f = materiales.setdefault(m.insumo_id, {"insumo": m.insumo, "cantidad": 0, "ultima": None, "valor": None})
        f["cantidad"] += m.cantidad
        f.update(ultima=m.fecha, valor=m.valor_unitario)
    return (sorted(equipos.values(), key=lambda e: (-e["unidades"], e["referencia"])),
            sorted(materiales.values(), key=lambda f: f["insumo"].descripcion.lower()))


def marcas_de(proveedor, equipos, materiales):
    """Las marcas que maneja: indicadas por el encargado y vendidas según las compras, sin repetir."""
    marcas = {normalizar(m.nombre): {"nombre": m.nombre, "indicada": True, "vendida": False} for m in proveedor.marcas.all()}
    for nombre in [e["marca"] for e in equipos] + [f["insumo"].marca for f in materiales]:
        clave = normalizar(nombre)
        if clave not in SIN_MARCA:
            marcas.setdefault(clave, {"nombre": nombre.strip(), "indicada": False, "vendida": True})["vendida"] = True
    return sorted(marcas.values(), key=lambda m: m["nombre"].lower())


def listado(q=""):
    """Los proveedores con sus compras y marcas. Se busca por nombre, NIT, asesor o marca («¿quién vende Hikvision?»)."""
    compras, vendidas = Counter(), defaultdict(set)
    for texto, marca in Activo.objects.exclude(proveedor="").order_by().values_list("proveedor", "marca"):
        compras[normalizar(texto)] += 1
        vendidas[normalizar(texto)].add(marca.strip())
    for texto, marca in _compras_materiales().order_by().values_list("proveedor", "insumo__marca"):
        compras[normalizar(texto)] += 1
        vendidas[normalizar(texto)].add(marca.strip())
    buscado, lista = normalizar(q), []
    del_excel = Counter(CompraHistorica.objects.exclude(proveedor=None).values_list("proveedor_id", flat=True))
    for p in Proveedor.objects.prefetch_related("asesores", "marcas"):
        clave = normalizar(p.razon_social)
        unidas = {normalizar(m): m for m in vendidas[clave]}
        unidas.update({normalizar(m.nombre): m.nombre for m in p.marcas.all()})
        p.marcas_vista = sorted(m for k, m in unidas.items() if k not in SIN_MARCA)
        lugares = [clave, p.nit] + [normalizar(a.nombre) for a in p.asesores.all()] + list(unidas)
        if not buscado or any(buscado in (x or "") for x in lugares):
            lista.append((p, compras[clave] + del_excel[p.pk]))
    return sorted(lista, key=lambda x: (-x[1], x[0].razon_social))[:800]


def _quienes_venden(ventas):
    """{texto del proveedor: última compra} → con su ficha y su asesor, lo más reciente primero."""
    fichas = {normalizar(p.razon_social): p for p in Proveedor.objects.prefetch_related("asesores")}
    filas = [{"nombre": texto, "proveedor": fichas.get(normalizar(texto)), "ultima": ultima} for texto, ultima in ventas.items()]
    return sorted(filas, key=lambda f: f["ultima"] or date.min, reverse=True)


def de_unidades(unidades):
    ventas = {}
    for a in sorted(unidades, key=lambda a: a.fecha_compra or date.min):
        if a.proveedor.strip():
            ventas[a.proveedor.strip()] = a.fecha_compra
    return _quienes_venden(ventas)


def de_insumo(insumo):
    ventas = {}
    for m in _compras_materiales().filter(insumo=insumo).order_by("fecha", "id"):
        ventas[m.proveedor.strip()] = m.fecha
    return _quienes_venden(ventas)


class AsesorForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = AsesorProveedor
        fields = ["nombre", "cargo", "celular", "correo", "atiende", "activo"]
        help_texts = {"atiende": "Si el proveedor asigna uno por línea. Ej.: CCTV Hikvision, cableado.",
                      "activo": "Desmárquelo cuando el proveedor asigne a otra persona: sigue en el historial de la ficha."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


@login_required
@inventario_requerido
def proveedor(request, pk):
    p = get_object_or_404(Proveedor.objects.prefetch_related("asesores", "marcas"), pk=pk)
    equipos, materiales = compras_de(p)
    contexto = {"p": p, "equipos": equipos, "materiales": materiales, "marcas": marcas_de(p, equipos, materiales),
                "ordenes": p.ordenes.order_by("-creado")[:5]}
    if ve_compras_del_excel(request.user):
        contexto.update(historial_de(p, todas=request.GET.get("historial") == "todo"))
    return render(request, "inventario/proveedor.html", contexto)


def ve_compras_del_excel(usuario):
    """El historial trae valores y pagos: lo ven bodega, dirección y financiera, no los técnicos."""
    return usuario.gestiona_inventario or usuario.ve_financiera


def historial_de(proveedor, todas=False):
    """Las compras del Excel «Compras y facturas» a este proveedor, con lo comprado y lo que figura sin pagar."""
    compras = list(proveedor.compras_historicas.all())
    total = sum((c.valor or 0) for c in compras)
    return {"historial": compras if todas else compras[:30], "historial_total": total, "historial_n": len(compras),
            "historial_pagadas": sum(1 for c in compras if c.fecha_pago)}


@login_required
@gestion_requerida
def asesor_form(request, proveedor_pk, pk=None):
    p = get_object_or_404(Proveedor, pk=proveedor_pk)
    asesor = get_object_or_404(AsesorProveedor, pk=pk, proveedor=p) if pk else AsesorProveedor(proveedor=p)
    form = AsesorForm(request.POST or None, instance=asesor)
    if request.method == "POST" and form.is_valid():
        guardado = form.save()
        _registro(request.user, guardado, CHANGE if pk else ADDITION, f"Asesor de {p}: {', '.join(form.changed_data)}")
        messages.success(request, f"Se guardó el asesor {guardado.nombre}.")
        return redirect("inventario:proveedor", p.pk)
    return render(request, "inventario/asesor_form.html", {"form": form, "p": p, "asesor": asesor if pk else None})


TEMPORALES = "janotec_importaciones"


def _ruta_temporal(token):
    import tempfile
    from pathlib import Path

    carpeta = Path(tempfile.gettempdir()) / TEMPORALES
    carpeta.mkdir(exist_ok=True)
    return carpeta / f"compras_{token}.xlsx"


@login_required
def importar_compras(request):
    """Sube el Excel «Compras y facturas»: primero muestra qué haría y solo guarda cuando se confirma.

    Entre la revisión y la confirmación el archivo queda en la carpeta temporal del servidor (no en media, que es
    pública), y se borra al importar.
    """
    import re
    import uuid

    from django.core.exceptions import PermissionDenied

    from . import importar_compras as imp

    if not ve_compras_del_excel(request.user):
        raise PermissionDenied
    ultima = CompraHistorica.objects.select_related("importado_por").order_by("-importado_en").first()
    contexto = {"ultima": ultima, "total_anterior": CompraHistorica.objects.count()}
    if request.method == "POST" and request.POST.get("accion") == "importar":
        token = request.POST.get("token", "")
        ruta = _ruta_temporal(token) if re.fullmatch(r"[0-9a-f]{32}", token) else None
        if not ruta or not ruta.exists():
            messages.error(request, "El archivo revisado ya no está. Súbalo otra vez.")
            return redirect("inventario:importar_compras")
        try:
            resumen = imp.aplicar(imp.leer(ruta), request.user)
        finally:
            ruta.unlink(missing_ok=True)
        messages.success(request, f"Listo: {resumen['compras']} compras en el historial, {len(resumen['nuevos'])} "
                                  f"proveedores nuevos y {len(resumen['con_nit'])} con el NIT completado.")
        return redirect(f"{reverse('inventario:contactos')}?tipo=proveedores")
    if request.method == "POST":
        archivo = request.FILES.get("archivo")
        if not archivo or not archivo.name.lower().endswith(".xlsx"):
            messages.error(request, "Suba el Excel en formato .xlsx (en Google Sheets: Archivo → Descargar → Microsoft Excel).")
        elif archivo.size > 15 * 1024 * 1024:
            messages.error(request, "El archivo pesa más de 15 MB.")
        else:
            token = uuid.uuid4().hex
            ruta = _ruta_temporal(token)
            with open(ruta, "wb") as destino:
                for trozo in archivo.chunks():
                    destino.write(trozo)
            try:
                datos = imp.leer(ruta)
            except Exception:  # archivo dañado o que no es un libro de Excel
                ruta.unlink(missing_ok=True)
                messages.error(request, "No se pudo leer el archivo. Revise que sea el Excel de compras en .xlsx.")
            else:
                if not datos.filas:
                    ruta.unlink(missing_ok=True)
                    messages.error(request, "No se encontró ninguna hoja con las columnas NIT y RAZÓN SOCIAL.")
                else:
                    contexto.update(plan=imp.plan(datos), token=token, nombre_archivo=archivo.name)
    return render(request, "inventario/importar_compras.html", contexto)

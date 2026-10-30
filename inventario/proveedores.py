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

from tickets.forms import BootstrapMixin

from .models import Activo, AsesorProveedor, MovimientoActivo, MovimientoInsumo, Proveedor, normalizar
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
    for p in Proveedor.objects.prefetch_related("asesores", "marcas"):
        clave = normalizar(p.razon_social)
        unidas = {normalizar(m): m for m in vendidas[clave]}
        unidas.update({normalizar(m.nombre): m.nombre for m in p.marcas.all()})
        p.marcas_vista = sorted(m for k, m in unidas.items() if k not in SIN_MARCA)
        lugares = [clave, p.nit] + [normalizar(a.nombre) for a in p.asesores.all()] + list(unidas)
        if not buscado or any(buscado in (x or "") for x in lugares):
            lista.append((p, compras[clave]))
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
    return render(request, "inventario/proveedor.html", {
        "p": p, "equipos": equipos, "materiales": materiales, "marcas": marcas_de(p, equipos, materiales),
        "ordenes": p.ordenes.order_by("-creado")[:5]})


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

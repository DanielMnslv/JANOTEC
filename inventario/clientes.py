"""Clientes vistos desde el inventario: quién es el encargado, qué sedes tienen, dónde quedan y qué equipos hay en cada una.

Los municipios que faltan no se llenan solos: las actas traen errores («CAMARAS» como ciudad), así que se sugieren y el
encargado los confirma al guardar la sede. Las actas también dicen quién recibió por última vez en cada sede.
"""
from collections import Counter, defaultdict

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from accounts.models import Empresa, EncargadoCliente, Sede
from tickets.forms import BootstrapMixin
from tickets.models import ActaSoporte, Ticket

from .models import Activo, Lugar, normalizar
from .productos import _registro
from .views import gestion_requerida, inventario_requerido

# Quien firmó por JANOTEC o una prueba no es el contacto del cliente
NO_ES_CONTACTO = ("GUANA", "PRUEBA")


def _instalados():
    return Activo.objects.filter(lugar=Lugar.CLIENTE, empresa__isnull=False)


def listado(q=""):
    """Los clientes con sus equipos instalados. Se busca por nombre, NIT, encargado, sede o municipio."""
    instalados = Counter(_instalados().order_by().values_list("empresa_id", flat=True))
    buscado, lista = normalizar(q), []
    for e in Empresa.objects.filter(es_prospecto=False).prefetch_related("encargados", "sedes").order_by("razon_social"):
        e.municipios = sorted({s.ciudad.strip() for s in e.sedes.all() if s.ciudad.strip()})
        lugares = [normalizar(e.razon_social), e.nit or "", normalizar(e.ciudad)] + [normalizar(a.nombre) for a in e.encargados.all()]
        for s in e.sedes.all():
            lugares += [normalizar(s.nombre), normalizar(s.ciudad)]
        if not buscado or any(buscado in x for x in lugares):
            lista.append((e, instalados.get(e.pk, 0)))
    return lista


def _pistas(empresa):
    """{sede: {municipios escritos, último contacto}} según las actas y los tickets de esa sede."""
    municipios, recibio = defaultdict(Counter), {}
    filas = (ActaSoporte.objects.filter(empresa=empresa, sede__isnull=False).order_by("fecha")
             .values_list("sede_id", "ciudad", "recibio", "cargo_recibio"))
    for sede, ciudad, nombre, cargo in filas:
        if ciudad.strip():
            municipios[sede][ciudad.strip()] += 1
        if nombre.strip() and not any(x in normalizar(nombre) for x in NO_ES_CONTACTO):
            recibio[sede] = (nombre.strip().title(), cargo.strip())
    for sede, ciudad in Ticket.objects.filter(empresa=empresa, sede__isnull=False).exclude(municipio="").order_by().values_list("sede_id", "municipio"):
        municipios[sede][ciudad.strip()] += 1
    return municipios, recibio


def _municipio_sugerido(conteo):
    return conteo.most_common(1)[0][0] if conteo else ""


def sedes_de(empresa):
    municipios, recibio = _pistas(empresa)
    equipos = Counter(_instalados().filter(empresa=empresa).order_by().values_list("sede_id", flat=True))
    sedes = list(empresa.sedes.all())
    for s in sedes:
        s.sugerido = "" if s.ciudad else _municipio_sugerido(municipios.get(s.pk))
        s.recibio = recibio.get(s.pk)
        s.equipos = equipos.get(s.pk, 0)
    return sedes, equipos.get(None, 0)


def equipos_de(empresa):
    """Lo instalado en el cliente, por sede y modelo."""
    grupos = {}
    for a in _instalados().filter(empresa=empresa).select_related("sede"):
        clave = (a.sede_id, a.referencia.strip().upper())
        g = grupos.setdefault(clave, {"sede": a.sede, "referencia": a.referencia.strip(), "marca": a.marca,
                                      "descripcion": a.descripcion, "unidades": 0})
        g["unidades"] += 1
    return sorted(grupos.values(), key=lambda g: (g["sede"].nombre if g["sede"] else "~", g["referencia"]))


class EncargadoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = EncargadoCliente
        fields = ["nombre", "cargo", "celular", "correo", "notas", "activo"]
        widgets = {"notas": forms.Textarea(attrs={"rows": 2})}
        help_texts = {"activo": "Desmárquelo cuando el cliente cambie de encargado: sigue en el historial de la ficha."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


class SedeForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Sede
        fields = ["nombre", "departamento", "ciudad", "direccion", "contacto_nombre", "contacto_cargo", "contacto_telefono",
                  "contacto_email", "activa"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ciudad"].help_text = "Donde queda de verdad. Ej.: EDS Colono queda en Granada."
        self._aplicar_estilos()

    def clean_nombre(self):
        nombre = self.cleaned_data["nombre"].strip()
        if Sede.objects.filter(empresa=self.instance.empresa, nombre__iexact=nombre).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este cliente ya tiene una sede con ese nombre.")
        return nombre


@login_required
@inventario_requerido
def cliente(request, pk):
    e = get_object_or_404(Empresa.objects.prefetch_related("encargados", "sedes"), pk=pk, es_prospecto=False)
    sedes, sin_sede = sedes_de(e)
    return render(request, "inventario/cliente.html", {
        "e": e, "sedes": sedes, "sin_sede": sin_sede, "equipos": equipos_de(e),
        "sin_municipio": sum(1 for s in sedes if not s.ciudad)})


@login_required
@gestion_requerida
def encargado_form(request, empresa_pk, pk=None):
    e = get_object_or_404(Empresa, pk=empresa_pk)
    encargado = get_object_or_404(EncargadoCliente, pk=pk, empresa=e) if pk else EncargadoCliente(empresa=e)
    form = EncargadoForm(request.POST or None, instance=encargado)
    if request.method == "POST" and form.is_valid():
        guardado = form.save()
        _registro(request.user, guardado, CHANGE if pk else ADDITION, f"Encargado de {e}: {', '.join(form.changed_data)}")
        messages.success(request, f"Se guardó el encargado {guardado.nombre}.")
        return redirect("inventario:cliente", e.pk)
    return render(request, "inventario/cliente_dato_form.html", {
        "form": form, "e": e, "titulo": encargado.nombre if pk else f"¿Quién es el encargado en {e.razon_social}?"})


@login_required
@gestion_requerida
def sede_form(request, empresa_pk, pk=None):
    e = get_object_or_404(Empresa, pk=empresa_pk)
    sede = get_object_or_404(Sede, pk=pk, empresa=e) if pk else Sede(empresa=e)
    inicial = {}
    if pk and not sede.ciudad:  # se sugiere lo de las actas: queda consignado solo cuando el encargado guarda
        inicial["ciudad"] = _municipio_sugerido(_pistas(e)[0].get(sede.pk))
    form = SedeForm(request.POST or None, instance=sede, initial=inicial)
    if request.method == "POST" and form.is_valid():
        guardada = form.save()
        _registro(request.user, guardada, CHANGE if pk else ADDITION, f"Sede de {e}: {', '.join(form.changed_data)}")
        messages.success(request, f"Se guardó la sede {guardada.nombre}.")
        return redirect("inventario:cliente", e.pk)
    return render(request, "inventario/cliente_dato_form.html", {
        "form": form, "e": e, "titulo": sede.nombre if pk else f"Nueva sede de {e.razon_social}",
        "sugerido": inicial.get("ciudad")})

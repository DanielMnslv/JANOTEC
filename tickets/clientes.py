"""Clientes y sus sedes, administrados por el área técnica (Guana).

Es el listado que alimenta el campo autocompletado de cliente y sede al radicar o programar un ticket:
lo que se crea aquí aparece de inmediato; lo inactivo deja de sugerirse pero conserva su historial.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import Empresa, Sede

from .forms import BootstrapMixin
from .permisos import supervisor_requerido


class EmpresaForm(BootstrapMixin, forms.ModelForm):
    """Datos del cliente (empresa). La razón social no se repite y el NIT vacío queda como «sin NIT»."""
    class Meta:
        model = Empresa
        fields = ["razon_social", "nit", "sector", "ciudad", "direccion", "telefono", "email_contacto", "es_del_grupo", "activa"]
        labels = {"activa": "Activo (se sugiere al radicar y programar tickets)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()

    def clean_razon_social(self):
        """Quita espacios de más y evita crear dos clientes con el mismo nombre."""
        nombre = " ".join(self.cleaned_data["razon_social"].split())
        otro = Empresa.objects.filter(razon_social__iexact=nombre).exclude(pk=self.instance.pk).first()
        if otro:
            raise forms.ValidationError(f"Ya existe el cliente «{otro}».")
        return nombre

    def clean_nit(self):
        """NIT sin espacios; vacío se guarda como nulo (varios clientes pueden no tenerlo)."""
        return (self.cleaned_data.get("nit") or "").strip() or None


class SedeForm(BootstrapMixin, forms.ModelForm):
    """Datos de una sede del cliente, con su contacto. El nombre no se repite dentro del mismo cliente."""
    class Meta:
        model = Sede
        fields = ["nombre", "departamento", "ciudad", "direccion", "abonado", "contacto_nombre", "contacto_cargo",
                  "contacto_telefono", "contacto_email", "activa"]
        labels = {"activa": "Activa"}

    def __init__(self, *args, empresa=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.empresa = empresa
        self._aplicar_estilos()

    def clean_nombre(self):
        """Quita espacios de más y evita dos sedes con el mismo nombre en el cliente."""
        nombre = " ".join(self.cleaned_data["nombre"].split())
        if self.empresa.sedes.filter(nombre__iexact=nombre).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este cliente ya tiene una sede con ese nombre.")
        return nombre


def _usos(objeto, ignorar=()):
    """Registros que dependen del cliente o la sede (tickets, actas, usuarios, equipos…): con ellos no se elimina."""
    usos = []
    for rel in objeto._meta.related_objects:
        nombre = rel.get_accessor_name()
        if not nombre or nombre in ignorar or rel.related_name == "+":
            continue
        n = getattr(objeto, nombre).count()
        if n:
            usos.append(f"{n} {rel.related_model._meta.verbose_name_plural if n != 1 else rel.related_model._meta.verbose_name}")
    return usos


@login_required
@supervisor_requerido
def clientes(request):
    """Listado de clientes con sus sedes activas y número de tickets; filtra por estado y busca por nombre,
    NIT, ciudad o sede. Los prospectos de comercial no aparecen.
    """
    q = request.GET.get("q", "").strip()
    estado = request.GET.get("estado", "activos")
    lista = Empresa.objects.filter(es_prospecto=False).annotate(
        n_sedes=Count("sedes", filter=Q(sedes__activa=True), distinct=True),
        n_tickets=Count("tickets", distinct=True),
    )
    if estado == "activos":
        lista = lista.filter(activa=True)
    elif estado == "inactivos":
        lista = lista.filter(activa=False)
    if q:
        lista = lista.filter(Q(razon_social__icontains=q) | Q(nit__icontains=q) | Q(ciudad__icontains=q)
                             | Q(sedes__nombre__icontains=q)).distinct()
    return render(request, "tickets/clientes.html", {"lista": lista.order_by("razon_social"), "q": q, "estado": estado})


@login_required
@supervisor_requerido
def cliente_form(request, pk=None):
    """Crear o editar un cliente; al crearlo lleva a su ficha para agregarle las sedes."""
    empresa = get_object_or_404(Empresa, pk=pk) if pk else None
    form = EmpresaForm(request.POST or None, instance=empresa)
    if request.method == "POST" and form.is_valid():
        nuevo = empresa is None
        empresa = form.save()
        messages.success(request, f"Cliente «{empresa}» {'creado: agréguele sus sedes' if nuevo else 'actualizado'}.")
        return redirect("cliente_detalle", pk=empresa.pk)
    return render(request, "tickets/cliente_form.html", {"form": form, "empresa": empresa})


@login_required
@supervisor_requerido
def cliente_detalle(request, pk):
    """Ficha del cliente con sus sedes; el mismo formulario agrega una sede nueva o edita la elegida (?sede=)."""
    empresa = get_object_or_404(Empresa, pk=pk)
    # Con ?sede=<id> (o el campo «sede» al guardar) el formulario edita esa sede; sin él, agrega una nueva
    id_sede = request.POST.get("sede") or request.GET.get("sede")
    sede_editada = get_object_or_404(Sede, pk=id_sede, empresa=empresa) if id_sede else None
    if request.method == "POST":
        form = SedeForm(request.POST, instance=sede_editada, empresa=empresa)
        if form.is_valid():
            sede = form.save(commit=False)
            sede.empresa = empresa
            sede.save()
            messages.success(request, f"Sede «{sede.nombre}» {'actualizada' if sede_editada else 'agregada'}.")
            return redirect("cliente_detalle", pk=empresa.pk)
    else:
        form = SedeForm(instance=sede_editada, empresa=empresa,
                        initial=None if sede_editada else {"ciudad": empresa.ciudad})
    sedes = empresa.sedes.annotate(n_tickets=Count("tickets")).order_by("-activa", "nombre")
    return render(request, "tickets/cliente_admin.html", {
        "empresa": empresa, "sedes": sedes, "form": form, "sede_editada": sede_editada,
    })


@login_required
@supervisor_requerido
@require_POST
def cliente_eliminar(request, pk):
    """Elimina el cliente si no tiene historial; si lo tiene, solo lo deja inactivo para conservarlo."""
    empresa = get_object_or_404(Empresa, pk=pk)
    usos = _usos(empresa, ignorar=("sedes",)) + [u for s in empresa.sedes.all() for u in _usos(s)]
    if usos:
        empresa.activa = False
        empresa.save(update_fields=["activa"])
        messages.warning(request, f"«{empresa}» tiene historial ({', '.join(usos[:4])}): no se elimina, quedó inactivo "
                                  "y ya no se sugiere al radicar.")
        return redirect("cliente_detalle", pk=empresa.pk)
    nombre = str(empresa)
    empresa.delete()
    messages.success(request, f"Cliente «{nombre}» eliminado.")
    return redirect("clientes")


@login_required
@supervisor_requerido
@require_POST
def sede_eliminar(request, pk):
    """Elimina la sede si no tiene historial; si lo tiene, solo la deja inactiva."""
    sede = get_object_or_404(Sede, pk=pk)
    usos = _usos(sede)
    if usos:
        sede.activa = False
        sede.save(update_fields=["activa"])
        messages.warning(request, f"La sede «{sede.nombre}» tiene historial ({', '.join(usos[:4])}): quedó inactiva.")
    else:
        sede.delete()
        messages.success(request, f"Sede «{sede.nombre}» eliminada.")
    return redirect("cliente_detalle", pk=sede.empresa_id)

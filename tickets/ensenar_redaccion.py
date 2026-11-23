"""«Ejemplos de redacción»: así se le enseña al botón «Mejorar redacción» cómo quiere JANOTEC sus textos (23/11/2026).

No se reentrena el modelo: cada ejemplo (lo que se escribió y cómo debía quedar) se guarda en EjemploRedaccion y,
antes de cada mejora, Claude recibe los ejemplos activos del mismo tipo de texto (tickets/redaccion.py) e imita su
estructura, su tono y su presentación. Los administra coordinación, dirección y el área administrativa.
"""

from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import EjemploRedaccion


def puede_ensenar(usuario):
    """Coordinación, dirección y administrativa (propuestas): quienes saben cómo debe quedar un texto."""
    return usuario.is_authenticated and (usuario.es_supervisor or usuario.es_administrativo)


def _ensenar_requerido(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not puede_ensenar(request.user):
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


class EjemploForm(forms.ModelForm):
    """Formulario del ejemplo: los dos textos grandes, lado a lado en pantalla ancha."""

    class Meta:
        model = EjemploRedaccion
        fields = ["titulo", "aplica", "original", "mejorado", "activo"]
        widgets = {
            "original": forms.Textarea(attrs={"rows": 18, "data-redaccion": "no",
                                              "placeholder": "Pegue el texto tal como lo escribió la persona."}),
            "mejorado": forms.Textarea(attrs={"rows": 18, "data-redaccion": "no",
                                              "placeholder": "Pegue o escriba cómo debía quedar: estructura, títulos, viñetas…"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nombre, campo in self.fields.items():
            clase = {"activo": "form-check-input", "aplica": "form-select"}.get(nombre, "form-control")
            campo.widget.attrs["class"] = clase
        self.fields["original"].widget.attrs["class"] += " font-monospace small"
        self.fields["mejorado"].widget.attrs["class"] += " font-monospace small"


@login_required
@_ensenar_requerido
def lista(request, pk=None):
    """La lista de ejemplos y el formulario para crear uno nuevo o editar el elegido."""
    ejemplo = get_object_or_404(EjemploRedaccion, pk=pk) if pk else EjemploRedaccion()
    form = EjemploForm(request.POST or None, instance=ejemplo)
    if request.method == "POST" and form.is_valid():
        guardado = form.save(commit=False)
        if not guardado.creado_por_id:
            guardado.creado_por = request.user
        guardado.save()
        messages.success(request, f"Se guardó el ejemplo «{guardado.titulo}». Desde ya, «Mejorar redacción» lo tiene "
                                  "en cuenta" + ("." if guardado.activo else " cuando lo active."))
        return redirect("redaccion_ejemplos")
    return render(request, "tickets/redaccion_ejemplos.html", {
        "form": form, "ejemplo": ejemplo, "ejemplos": EjemploRedaccion.objects.select_related("creado_por")})


@login_required
@_ensenar_requerido
@require_POST
def eliminar(request, pk):
    """Borra el ejemplo: deja de mostrarse a Claude en la siguiente mejora."""
    ejemplo = get_object_or_404(EjemploRedaccion, pk=pk)
    ejemplo.delete()
    messages.success(request, f"Se eliminó el ejemplo «{ejemplo.titulo}».")
    return redirect("redaccion_ejemplos")

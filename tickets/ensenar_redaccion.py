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
    from . import aprendizaje

    todos = EjemploRedaccion.objects.select_related("creado_por")
    try:
        aprendidos = aprendizaje.historial()
    except Exception:  # la página sirve igual aunque no se pueda leer el historial
        aprendidos = []
    return render(request, "tickets/redaccion_ejemplos.html", {
        "form": form, "ejemplo": ejemplo,
        "por_revisar": [e for e in todos if e.origen == EjemploRedaccion.Origen.CORRECCION],
        "ejemplos": [e for e in todos if e.origen != EjemploRedaccion.Origen.CORRECCION],
        "aprendidos": {g: sum(1 for d in aprendidos if d["grupo"] == g)
                       for g in ("diagnostico", "resultado", "informe", "propuesta")}})


CORRECCIONES_POR_DIA = 30  # por persona: evita llenar la lista de candidatos


@login_required
@require_POST
def correccion(request):
    """Alguien usó la propuesta de Claude y la corrigió antes de guardar: su versión queda como ejemplo candidato.

    Lo manda el navegador al enviar el formulario (static/js/redaccion.js). Queda inactivo, marcado «Por revisar»,
    hasta que coordinación, dirección o administrativa lo aprueba en «Ejemplos de redacción». No responde nada útil:
    el formulario sigue su curso aunque esto falle.
    """
    from django.core.cache import cache
    from django.http import HttpResponse
    from django.utils import timezone

    from . import redaccion

    if not request.user.es_interno:
        raise PermissionDenied
    original = (request.POST.get("original") or "").strip()
    propuesta = (request.POST.get("propuesta") or "").strip()
    final = (request.POST.get("final") or "").strip()
    if not (original and propuesta and final) or final == propuesta or len(final) > 12000 or len(original) > 8000:
        return HttpResponse(status=204)
    clave = f"redaccion:correcciones:{request.user.pk}:{timezone.localdate():%Y%m%d}"
    if cache.get(clave, 0) >= CORRECCIONES_POR_DIA:
        return HttpResponse(status=204)
    cache.set(clave, cache.get(clave, 0) + 1, 60 * 60 * 26)
    campo = redaccion._limpio(request.POST.get("campo"), 60) or "texto"
    grupos = redaccion.aplica_para(request.POST.get("tipo", ""), campo, request.POST.get("pagina", ""), original)
    aplica = grupos[0] if grupos[0] in EjemploRedaccion.Aplica.values else EjemploRedaccion.Aplica.TODOS
    EjemploRedaccion.objects.update_or_create(
        original=original, origen=EjemploRedaccion.Origen.CORRECCION,
        defaults={"titulo": f"Corrección · {campo}"[:120], "aplica": aplica, "mejorado": final, "activo": False,
                  "creado_por": request.user})
    return HttpResponse(status=204)


@login_required
@_ensenar_requerido
@require_POST
def aprobar(request, pk):
    """Aprueba una corrección candidata: queda activa y Claude la usa desde la siguiente mejora."""
    ejemplo = get_object_or_404(EjemploRedaccion, pk=pk)
    ejemplo.activo = True
    ejemplo.origen = EjemploRedaccion.Origen.MANUAL
    ejemplo.save(update_fields=["activo", "origen", "actualizado"])
    messages.success(request, f"Aprobado: «{ejemplo.titulo}» ya se usa como ejemplo.")
    return redirect("redaccion_ejemplos")


@login_required
@_ensenar_requerido
@require_POST
def eliminar(request, pk):
    """Borra el ejemplo: deja de mostrarse a Claude en la siguiente mejora."""
    ejemplo = get_object_or_404(EjemploRedaccion, pk=pk)
    ejemplo.delete()
    messages.success(request, f"Se eliminó el ejemplo «{ejemplo.titulo}».")
    return redirect("redaccion_ejemplos")

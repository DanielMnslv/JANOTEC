"""Viáticos para el personal que va a las sedes de los clientes (alimentación, transporte, gasolina u otros).

Los gestiona por completo el área financiera. Se piden de dos formas:

* **Al programar en el cronograma**: casillas «Viáticos SST/SGI» y «Viáticos técnicos» con los conceptos. Al guardar la
  actividad se crea (o se actualiza) la solicitud y financiera recibe el correo. Si se desmarcan, la solicitud
  pendiente se cancela y también se le avisa.
* **Desde el ticket**: botón «Solicitar viáticos», para desplazamientos que no están en el cronograma.

Financiera los ve en «Viáticos» (y en su «Mi día»), los entrega y marca la solicitud como gestionada con el valor y una
nota; el soporte es opcional.
"""
import logging
from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from tickets.models import Ticket
from tickets.permisos import personal_requerido

from .models import SolicitudViaticos

log = logging.getLogger(__name__)
CONCEPTOS = [c for c, _ in SolicitudViaticos.CONCEPTOS]
E = SolicitudViaticos.Estado


# --- Aviso a financiera -------------------------------------------------------------------
def avisar(solicitud, motivo="nueva"):
    """Correo al área financiera: solicitud nueva, cambiada o cancelada. Si falla queda en el registro."""
    from inventario.financiera import _correo, correos_financiera

    titulos = {"nueva": "Se requieren viáticos", "cambio": "Cambiaron los viáticos", "cancelada": "Ya no se requieren los viáticos"}
    radicado = solicitud.ticket.radicado if solicitud.ticket_id else ""
    asunto = f"{titulos[motivo]} · {solicitud.numero}" + (f" · {radicado}" if radicado else "") + f" · {solicitud.destino}"
    cuerpo = "\n".join([
        f"{titulos[motivo]} para el desplazamiento del {solicitud.fecha:%d/%m/%Y}.", "",
        f"Cliente y sede: {solicitud.destino}",
        *([f"Ticket: {radicado}"] if radicado else []),
        f"Para: {solicitud.para_texto}",
        *([f"Quiénes van: {solicitud.personas}"] if solicitud.personas else []),
        f"Conceptos: {solicitud.conceptos_texto}",
        f"Solicitó: {solicitud.solicitada_por.get_full_name() if solicitud.solicitada_por else '—'}", "",
        "Cuando los entregue, márquela como gestionada en la plataforma:",
        f"{settings.SITIO_URL.rstrip('/')}{solicitud.get_absolute_url()}",
    ])
    return _correo(asunto, cuerpo, correos_financiera())


# --- Desde el cronograma ------------------------------------------------------------------
def campos_actividad(solicitud):
    """Valores para las casillas del formulario de la actividad (para editarla)."""
    if not solicitud or solicitud.estado == E.CANCELADA:
        return {"via_sst": False, "via_tecnicos": False, "via_detalle": "", **{f"via_{c}": False for c in CONCEPTOS}}
    return {"via_sst": solicitud.para_sst, "via_tecnicos": solicitud.para_tecnicos, "via_detalle": solicitud.detalle,
            **{f"via_{c}": getattr(solicitud, c) for c in CONCEPTOS}}


def desde_actividad(actividad, datos, usuario):
    """Crea, actualiza o cancela la solicitud de viáticos de la actividad según las casillas, y avisa a financiera.
    Devuelve la solicitud (o None si no hay)."""
    from .models import Actividad

    solicitud = SolicitudViaticos.objects.filter(actividad=actividad).first()
    pide = bool(datos.get("via_sst") or datos.get("via_tecnicos"))
    if not pide:
        if solicitud and solicitud.estado == E.PENDIENTE:
            solicitud.estado = E.CANCELADA
            solicitud.save(update_fields=["estado"])
            avisar(solicitud, "cancelada")
        Actividad.objects.filter(pk=actividad.pk).update(viaticos=False)
        actividad.viaticos = False
        return solicitud
    personas = ", ".join(u.get_full_name() or u.username for u in actividad.tecnicos())
    if datos.get("via_sst") and actividad.sst_persona:
        personas = ", ".join(x for x in (personas, f"{actividad.sst_persona} (SST)") if x)
    valores = {"fecha": actividad.fecha, "destino": " · ".join(x for x in (actividad.cliente, actividad.sede) if x)[:300],
               "ticket_id": actividad.ticket_id, "para_sst": bool(datos.get("via_sst")),
               "para_tecnicos": bool(datos.get("via_tecnicos")), "personas": personas[:500],
               "detalle": (datos.get("via_detalle") or "")[:300], **{c: bool(datos.get(f"via_{c}")) for c in CONCEPTOS}}
    Actividad.objects.filter(pk=actividad.pk).update(viaticos=True)
    actividad.viaticos = True
    if not solicitud or solicitud.estado == E.CANCELADA:
        if solicitud:  # se había cancelado y la vuelven a pedir
            for k, v in valores.items():
                setattr(solicitud, k, v)
            solicitud.estado, solicitud.solicitada_por = E.PENDIENTE, usuario
            solicitud.save()
        else:
            solicitud = SolicitudViaticos.objects.create(actividad=actividad, solicitada_por=usuario, **valores)
        avisar(solicitud, "nueva")
        return solicitud
    cambio = any(getattr(solicitud, k) != v for k, v in valores.items() if k != "personas")
    for k, v in valores.items():
        setattr(solicitud, k, v)
    solicitud.save()
    if cambio:
        avisar(solicitud, "cambio")
    return solicitud


# --- Formularios --------------------------------------------------------------------------
class SolicitudViaticosForm(forms.ModelForm):
    """Solicitud de viáticos desde el ticket (o sin ticket)."""

    class Meta:
        model = SolicitudViaticos
        fields = ["fecha", "destino", "para_sst", "para_tecnicos", "personas", "alimentacion", "transporte", "gasolina",
                  "otros", "detalle"]
        widgets = {"fecha": forms.DateInput(attrs={"type": "date", "class": "form-control"}, format="%Y-%m-%d"),
                   "destino": forms.TextInput(attrs={"class": "form-control"}),
                   "personas": forms.TextInput(attrs={"class": "form-control", "placeholder": "Nombres del personal que va"}),
                   "detalle": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ej.: peajes, hospedaje una noche"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for c in ["para_sst", "para_tecnicos", *CONCEPTOS]:
            self.fields[c].widget.attrs["class"] = "form-check-input"

    def clean(self):
        datos = super().clean()
        if not (datos.get("para_sst") or datos.get("para_tecnicos")):
            raise forms.ValidationError("Marque para quién son: personal SST/SGI, personal técnico o los dos.")
        if not any(datos.get(c) for c in CONCEPTOS):
            raise forms.ValidationError("Marque al menos un concepto: alimentación, transporte, gasolina u otros.")
        if datos.get("otros") and not datos.get("detalle"):
            self.add_error("detalle", "Diga cuáles son los otros conceptos.")
        return datos


# --- Vistas -------------------------------------------------------------------------------
def _puede_ver(usuario):
    return usuario.ve_financiera or usuario.es_supervisor


@login_required
def lista(request):
    """Las solicitudes de viáticos: financiera las gestiona; coordinación las consulta."""
    if not _puede_ver(request.user):
        raise PermissionDenied
    base = SolicitudViaticos.objects.select_related("ticket", "solicitada_por", "gestionada_por", "actividad")
    return render(request, "cronograma/viaticos.html", {
        "pendientes": base.filter(estado=E.PENDIENTE).order_by("fecha", "pk"),
        "gestionadas": base.exclude(estado=E.PENDIENTE).order_by("-fecha", "-pk")[:40],
        "gestiona": request.user.ve_financiera, "hoy": timezone.localdate(),
    })


@login_required
@personal_requerido
def nueva(request):
    """Pedir viáticos desde un ticket (?ticket=<radicado>) o para un desplazamiento suelto."""
    ticket = None
    if request.GET.get("ticket") or request.POST.get("ticket"):
        ticket = get_object_or_404(Ticket.objects.select_related("empresa", "sede"),
                                   radicado=request.GET.get("ticket") or request.POST.get("ticket"))
    inicial = {"fecha": timezone.localdate(), "para_tecnicos": True}
    if ticket:
        inicial["destino"] = " · ".join(str(x) for x in (ticket.empresa, ticket.sede) if x)
    form = SolicitudViaticosForm(request.POST or None, initial=inicial)
    if request.method == "POST" and form.is_valid():
        solicitud = form.save(commit=False)
        solicitud.ticket, solicitud.solicitada_por = ticket, request.user
        solicitud.save()
        enviado = avisar(solicitud, "nueva")
        messages.success(request, f"Solicitud {solicitud.numero} enviada a financiera." if enviado else
                         f"Solicitud {solicitud.numero} registrada; el correo a financiera no salió, pero la ve en su bandeja.")
        return redirect(ticket.get_absolute_url() if ticket else "cronograma:viaticos")
    previas = ticket.viaticos.exclude(estado=E.CANCELADA).order_by("-fecha") if ticket else []
    return render(request, "cronograma/viaticos_nueva.html", {"form": form, "ticket": ticket, "previas": previas})


@login_required
@require_POST
def gestionar(request, pk):
    """Financiera marca la solicitud como gestionada (valor entregado, nota y soporte opcional)."""
    if not request.user.ve_financiera:
        raise PermissionDenied
    solicitud = get_object_or_404(SolicitudViaticos, pk=pk, estado=E.PENDIENTE)
    try:
        valor = Decimal(request.POST.get("valor_entregado", "").replace(".", "").replace(",", ".").replace("$", "").strip() or "0")
    except InvalidOperation:
        messages.error(request, "El valor entregado no es un número.")
        return redirect("cronograma:viaticos")
    solicitud.valor_entregado = valor or None
    solicitud.nota_financiera = request.POST.get("nota_financiera", "").strip()[:300]
    if request.FILES.get("soporte"):
        solicitud.soporte = request.FILES["soporte"]
    solicitud.estado, solicitud.gestionada_por, solicitud.gestionada_en = E.GESTIONADA, request.user, timezone.now()
    solicitud.save()
    messages.success(request, f"{solicitud.numero} marcada como gestionada.")
    return redirect("cronograma:viaticos")


@login_required
def soporte(request, pk):
    """El soporte que subió financiera."""
    solicitud = get_object_or_404(SolicitudViaticos, pk=pk)
    if not _puede_ver(request.user) or not solicitud.soporte:
        raise Http404
    try:
        return FileResponse(solicitud.soporte.open("rb"), as_attachment=False)
    except FileNotFoundError:
        raise Http404("El soporte ya no está disponible.")

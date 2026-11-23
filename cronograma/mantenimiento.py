"""Plan de mantenimiento preventivo por sede y sistema (12/11/2026; desde el 23/11/2026 se maneja en «Clientes y sedes»).

* Cada plan es un sistema de una sede (CCTV, alarma, control de acceso…) con su frecuencia (mensual, trimestral,
  semestral o anual), el último mantenimiento realizado y la fecha del próximo.
* Unos días antes (``settings.MANTENIMIENTO_DIAS_ANTES``, 15 por defecto) la plataforma **avisa** a coordinación por
  correo y en «Mi día»; ya no crea el ticket sola. Coordinación lo crea con «Programar mantenimiento» en la ficha de la
  sede: el ticket lleva la lista de los equipos instalados de ese sistema y el técnico hace el acta sobre él.
* Al programarlo, la siguiente fecha se corre según la frecuencia. Mientras el ticket siga abierto no se crea otro.
* El último mantenimiento sale de la fecha del acta del ticket (o del que se anotó a mano antes de la plataforma).
* El contrato es opcional (número, vigencia y valor por visita); con el contrato vencido el ticket lo advierte.

El aviso se revisa de dos formas, como el cierre automático:

- ``python manage.py generar_mantenimientos``, programado una vez al día en el servidor;
- ``revisar_si_toca()``, que llaman la Recepción y «Clientes y sedes» como red de seguridad.
"""
import calendar
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import EjecucionMantenimiento, PlanMantenimiento

INTERVALO_SEGUNDOS = 600


def dias_antes():
    """Con cuántos días de anticipación se crea el ticket."""
    return int(getattr(settings, "MANTENIMIENTO_DIAS_ANTES", 15))


def sumar_meses(dia, meses):
    """La misma fecha ``meses`` después (el 31 pasa al último día del mes si no existe)."""
    total = dia.month - 1 + meses
    anio, mes = dia.year + total // 12, total % 12 + 1
    return dia.replace(year=anio, month=mes, day=min(dia.day, calendar.monthrange(anio, mes)[1]))


# ---------------------------------------------------------------------------------------------------------------------
# Generación de los tickets
# ---------------------------------------------------------------------------------------------------------------------
def _categoria(plan):
    """El servicio del ticket: «Mantenimiento CCTV IP» para CCTV; «Mantenimiento preventivo» para lo demás."""
    from tickets.models import Categoria

    if plan.sistema == PlanMantenimiento.Sistema.CCTV:
        cat = Categoria.objects.filter(nombre__iexact="Mantenimiento CCTV IP").first()
        if cat:
            return cat
    cat = Categoria.objects.filter(nombre__iexact="Mantenimiento preventivo").first()
    return cat or Categoria.objects.create(nombre="Mantenimiento preventivo")


def _solicitante(plan):
    """Quién queda como solicitante: quien creó el plan o, si ya no está, el coordinador técnico."""
    from accounts.models import Usuario
    from accounts.prueba import q_de_prueba

    if plan.creado_por and plan.creado_por.is_active:
        return plan.creado_por
    coordinadores = Usuario.objects.filter(is_active=True, rol=Usuario.Rol.SUPERVISOR).exclude(q_de_prueba()).order_by("pk")
    return coordinadores.first() or Usuario.objects.filter(is_active=True, is_superuser=True).order_by("pk").first()


def equipos_del_plan(plan):
    """Los equipos instalados (en servicio) del sistema del plan en esa sede."""
    from .models import EquipoInstalado

    return (EquipoInstalado.objects.filter(sede=plan.sede, sistema=plan.sistema)
            .exclude(estado=EquipoInstalado.Estado.RETIRADO).order_by("ubicacion", "tipo"))


def ultimo(plan):
    """Fecha del último mantenimiento: el acta más reciente de sus tickets o la que se anotó a mano (None si no hay)."""
    from tickets.models import ActaSoporte

    tickets = plan.ejecuciones.exclude(ticket=None).values("ticket")
    acta = ActaSoporte.objects.filter(ticket__in=tickets).order_by("-fecha").values_list("fecha", flat=True).first()
    fechas = [f for f in (plan.ultimo_realizado, acta and timezone.localtime(acta).date()) if f]
    return max(fechas, default=None)


def descripcion(plan, fecha):
    """El texto del ticket: qué se mantiene, cada cuánto, la fecha, el contrato y los equipos a revisar."""
    lineas = [f"Mantenimiento preventivo {plan.get_frecuencia_display().lower()} de {plan.get_sistema_display()} "
              f"previsto para el {fecha:%d/%m/%Y}."]
    if plan.detalle:
        lineas.append(f"Incluye: {plan.detalle}")
    equipos = list(equipos_del_plan(plan))
    if equipos:
        lineas.append(f"Equipos instalados a revisar ({len(equipos)}):")
        lineas += [f"- {e}" + (f" (serial {e.serial})" if e.serial else "") for e in equipos]
    if plan.con_contrato:
        texto = f"Cubierto por contrato{f' {plan.contrato}' if plan.contrato else ''}"
        if plan.contrato_vence:
            texto += f", vigente hasta el {plan.contrato_vence:%d/%m/%Y}"
        lineas.append(texto + ".")
        if plan.contrato_vencido:
            lineas.append("ATENCIÓN: el contrato está vencido; confirme con el cliente antes de programar la visita.")
    else:
        lineas.append("Sin contrato: se cobra por visita" + (f" (${plan.valor:,.0f} antes de IVA)".replace(",", ".")
                                                             if plan.valor else "") + ".")
    if plan.observaciones:
        lineas.append(plan.observaciones)
    lineas.append("Programado desde el plan de mantenimiento de la sede.")
    return "\n".join(lineas)


def crear_ticket(plan, fecha, autor=None):
    """Crea el ticket de mantenimiento del plan para esa fecha y la ejecución que los une. Devuelve el ticket."""
    from tickets.models import Seguimiento, Ticket

    sede = plan.sede
    with transaction.atomic():
        t = Ticket(empresa=sede.empresa, sede=sede, solicitante=autor or _solicitante(plan), radicado_por=autor,
                   tipo=Ticket.Tipo.MANTENIMIENTO, area=Ticket.Area.TECNICA, categoria=_categoria(plan),
                   asunto=f"Mantenimiento preventivo de {plan.get_sistema_display()} · {sede.nombre}"[:200],
                   descripcion=descripcion(plan, fecha), departamento=sede.departamento, municipio=sede.ciudad,
                   direccion=sede.direccion, contacto_nombre=sede.contacto_nombre, contacto_telefono=sede.contacto_telefono,
                   contacto_email=sede.contacto_email)
        t.asignar_prioridad_inicial()
        t.save()
        quien = f" por {autor}" if autor else " automáticamente"
        Seguimiento.objects.create(ticket=t, autor=autor, tipo=Seguimiento.Tipo.CREACION, estado_nuevo=Ticket.Estado.GENERADO,
                                   mensaje=f"Mantenimiento preventivo generado{quien} por el plan de la sede "
                                           f"({plan.get_frecuencia_display().lower()}, previsto el {fecha:%d/%m/%Y}).")
        EjecucionMantenimiento.objects.create(plan=plan, fecha_prevista=fecha, ticket=t)
    return t


def programar(plan, autor=None):
    """Crea el ticket del próximo mantenimiento del plan y corre la fecha siguiente según la frecuencia.
    Devuelve el ticket, o None si ya hay uno abierto."""
    if plan.en_curso:
        return None
    fecha = plan.proxima
    ticket = crear_ticket(plan, fecha, autor=autor)
    while plan.proxima <= max(fecha, timezone.localdate()):
        plan.proxima = sumar_meses(plan.proxima, plan.frecuencia)
    plan.save(update_fields=["proxima", "actualizado"])
    return ticket


def por_programar(hoy=None):
    """Los planes activos que vencen dentro del aviso (o ya vencieron) y aún no tienen ticket abierto."""
    hoy = hoy or timezone.localdate()
    planes = (PlanMantenimiento.objects.filter(activo=True, proxima__lte=hoy + timedelta(days=dias_antes()),
                                               sede__activa=True, sede__empresa__activa=True)
              .select_related("sede__empresa").order_by("proxima"))
    return [p for p in planes if not p.en_curso]


def avisar(hoy=None):
    """Avisa a coordinación, en un solo correo, los mantenimientos que llegan a los ``dias_antes()`` días y aún no se
    avisaron para esa fecha. No crea tickets: coordinación los programa desde la ficha de la sede. Devuelve los planes."""
    from accounts.prueba import es_nombre_prueba
    from tickets import notificaciones

    nuevos = [p for p in por_programar(hoy) if p.aviso_para != p.proxima]
    if not nuevos:
        return []
    reales = [p for p in nuevos if not es_nombre_prueba(p.sede.empresa.razon_social)]
    if reales:
        sitio = getattr(settings, "SITIO_URL", "").rstrip("/")
        lineas = [f"Estos mantenimientos preventivos vencen en los próximos {dias_antes()} días (o ya vencieron) y "
                  "todavía no tienen ticket:", ""]
        for p in reales:
            lineas.append(f"- {p.proxima:%d/%m/%Y} · {p.sede.empresa} · {p.sede.nombre} · {p.get_sistema_display()} "
                          f"({p.get_frecuencia_display().lower()})")
            lineas.append(f"  Programar: {sitio}/soporte/sedes/{p.sede_id}/")
        lineas += ["", "Al tocar «Programar mantenimiento» en la ficha de la sede se crea el ticket con sus equipos."]
        notificaciones._enviar(f"Mantenimientos por programar ({len(reales)})", "\n".join(lineas), correos_coordinacion())
    for p in nuevos:
        p.aviso_para = p.proxima
        p.save(update_fields=["aviso_para"])
    return nuevos


def correos_coordinacion():
    """Los correos de la coordinación técnica (sin usuarios de prueba); si no hay, el buzón de soporte."""
    from accounts.models import Usuario
    from accounts.prueba import q_de_prueba

    correos = list(Usuario.objects.filter(is_active=True, rol=Usuario.Rol.SUPERVISOR).exclude(q_de_prueba())
                   .exclude(email="").values_list("email", flat=True))
    return correos or [getattr(settings, "NOTIFICAR_SOPORTE_EMAIL", "")]


def revisar_si_toca():
    """Revisa los avisos si no se ha hecho en los últimos 10 minutos (red de seguridad del comando diario)."""
    if cache.add("cronograma:mantenimientos", True, INTERVALO_SEGUNDOS):
        avisar()


# ---------------------------------------------------------------------------------------------------------------------
# Formulario
# ---------------------------------------------------------------------------------------------------------------------
class PlanForm(forms.ModelForm):
    """Alta y edición de un plan."""

    class Meta:
        model = PlanMantenimiento
        fields = ["sede", "sistema", "detalle", "frecuencia", "proxima", "con_contrato", "contrato", "contrato_vence",
                  "valor", "observaciones", "activo"]
        widgets = {"proxima": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "contrato_vence": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "observaciones": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import Sede

        if "sede" in self.fields:  # en la ficha de la sede ya va dada
            sedes = Sede.objects.filter(activa=True, empresa__activa=True).select_related("empresa").order_by(
                "empresa__razon_social", "nombre")
            if self.instance.pk:
                sedes = sedes | Sede.objects.filter(pk=self.instance.sede_id)
            self.fields["sede"].queryset = sedes
            self.fields["sede"].label_from_instance = lambda s: f"{s.empresa} · {s.nombre}"
        for nombre, campo in self.fields.items():
            if isinstance(campo.widget, forms.CheckboxInput):
                campo.widget.attrs["class"] = "form-check-input"
            elif isinstance(campo.widget, forms.Select):
                campo.widget.attrs["class"] = "form-select"
            else:
                campo.widget.attrs["class"] = "form-control"

    def clean(self):
        d = super().clean()
        if d.get("con_contrato") and not (d.get("contrato") or d.get("contrato_vence")):
            self.add_error("contrato", "Indique el número del contrato o hasta cuándo va.")
        return d


# ---------------------------------------------------------------------------------------------------------------------
# Vistas antiguas: desde el 23/11/2026 todo se maneja en «Clientes y sedes» (tickets/clientes.py); se conservan las
# direcciones para que los enlaces y correos viejos sigan llegando al lugar correcto.
# ---------------------------------------------------------------------------------------------------------------------
def _exigir_supervisor(request):
    if not request.user.es_supervisor:
        raise PermissionDenied


@login_required
def lista(request):
    """Lleva a «Clientes y sedes» filtrado por los clientes con mantenimiento."""
    _exigir_supervisor(request)
    return redirect(f"{reverse('clientes')}?mantenimiento=con")


@login_required
def editar(request, pk=None):
    """Lleva al plan dentro de la ficha de la sede (o a «Clientes y sedes» si no se dio la sede)."""
    _exigir_supervisor(request)
    if pk:
        plan = get_object_or_404(PlanMantenimiento, pk=pk)
        return redirect("sede_plan_editar", sede_pk=plan.sede_id, pk=plan.pk)
    if (request.GET.get("sede") or "").isdigit():
        return redirect("sede_plan_nuevo", sede_pk=request.GET["sede"])
    return redirect("clientes")


@login_required
def detalle(request, pk):
    """Lleva a la ficha de la sede del plan."""
    _exigir_supervisor(request)
    plan = get_object_or_404(PlanMantenimiento, pk=pk)
    return redirect("sede_detalle", pk=plan.sede_id)

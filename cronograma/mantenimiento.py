"""Plan de mantenimiento preventivo por sede (pedido por la coordinación TIC, 12/11/2026).

* Cada plan es un sistema de una sede (CCTV, alarma, control de acceso…) con su frecuencia y la fecha del próximo.
* Unos días antes de esa fecha (``settings.MANTENIMIENTO_DIAS_ANTES``, 15 por defecto) la plataforma **crea sola** el
  ticket de mantenimiento: llega a «Recepción» del área técnica para asignarlo y programarlo en el cronograma.
  Al cliente no le llega correo por esto; se le avisa como siempre cuando se programa la visita.
* La siguiente fecha se corre según la frecuencia. Mientras el ticket anterior siga abierto no se crea otro: el plan
  se marca atrasado.
* El contrato es opcional (número, vigencia y valor por visita); con el contrato vencido el ticket lo advierte.

Se ejecuta de dos formas, como el cierre automático:

- ``python manage.py generar_mantenimientos``, programado una vez al día en el servidor;
- ``revisar_si_toca()``, que llaman la Recepción y la lista de planes como red de seguridad.
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


def descripcion(plan, fecha):
    """El texto del ticket: qué se mantiene, cada cuánto, la fecha y el contrato."""
    lineas = [f"Mantenimiento preventivo {plan.get_frecuencia_display().lower()} de {plan.get_sistema_display()} "
              f"previsto para el {fecha:%d/%m/%Y}."]
    if plan.detalle:
        lineas.append(f"Incluye: {plan.detalle}")
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
    lineas.append("Generado automáticamente por el plan de mantenimiento de la sede.")
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


def generar(hoy=None):
    """Crea los tickets de los planes cuya fecha está a ``dias_antes()`` o menos y corre su próxima fecha.
    No crea otro si el ticket anterior del plan sigue abierto. Devuelve la lista de tickets creados."""
    hoy = hoy or timezone.localdate()
    limite = hoy + timedelta(days=dias_antes())
    creados = []
    planes = (PlanMantenimiento.objects.filter(activo=True, proxima__lte=limite, sede__activa=True, sede__empresa__activa=True)
              .select_related("sede__empresa", "creado_por"))
    for plan in planes:
        if plan.en_curso:
            continue
        fecha = plan.proxima
        creados.append(crear_ticket(plan, fecha))
        while plan.proxima <= max(fecha, hoy):
            plan.proxima = sumar_meses(plan.proxima, plan.frecuencia)
        plan.save(update_fields=["proxima", "actualizado"])
    return creados


def revisar_si_toca():
    """Genera los mantenimientos si no se ha hecho en los últimos 10 minutos (red de seguridad del comando diario)."""
    if cache.add("cronograma:mantenimientos", True, INTERVALO_SEGUNDOS):
        generar()


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
# Vistas (coordinación técnica y dirección)
# ---------------------------------------------------------------------------------------------------------------------
def _exigir_supervisor(request):
    if not request.user.es_supervisor:
        raise PermissionDenied


@login_required
def lista(request):
    """Los planes: atrasados, con ticket en curso y próximos, con filtro por cliente y sistema."""
    _exigir_supervisor(request)
    revisar_si_toca()
    hoy = timezone.localdate()
    planes = PlanMantenimiento.objects.select_related("sede__empresa").prefetch_related("ejecuciones__ticket")
    q = (request.GET.get("q") or "").strip()
    if q:
        planes = planes.filter(Q(sede__empresa__razon_social__icontains=q) | Q(sede__nombre__icontains=q)
                               | Q(detalle__icontains=q))
    sistema = request.GET.get("sistema") or ""
    if sistema:
        planes = planes.filter(sistema=sistema)
    if request.GET.get("inactivos") != "1":
        planes = planes.filter(activo=True)

    filas = []
    for p in planes:
        ejecuciones = sorted(p.ejecuciones.all(), key=lambda e: e.fecha_prevista, reverse=True)
        en_curso = next((e for e in ejecuciones if e.ticket and e.ticket.estado not in ("cerrado", "cancelado")), None)
        ultima = next((e for e in ejecuciones if e.ticket and e.ticket.estado == "cerrado"), None)
        atrasado = bool(en_curso and en_curso.fecha_prevista < hoy) or (p.activo and not en_curso and p.proxima < hoy)
        filas.append({"plan": p, "en_curso": en_curso, "ultima": ultima, "atrasado": atrasado})
    resumen = {
        "planes": len(filas), "sedes": len({f["plan"].sede_id for f in filas}),
        "en_curso": sum(1 for f in filas if f["en_curso"]), "atrasados": sum(1 for f in filas if f["atrasado"]),
        "mes": sum(1 for f in filas if f["plan"].activo and f["plan"].proxima <= sumar_meses(hoy, 1)),
    }
    return render(request, "cronograma/mantenimiento_lista.html", {
        "filas": filas, "resumen": resumen, "q": q, "sistema": sistema, "sistemas": PlanMantenimiento.Sistema.choices,
        "inactivos": request.GET.get("inactivos") == "1", "dias_antes": dias_antes(),
    })


@login_required
def editar(request, pk=None):
    """Crear (con ?sede= para elegirla de una vez) o editar un plan."""
    _exigir_supervisor(request)
    plan = get_object_or_404(PlanMantenimiento, pk=pk) if pk else None
    inicial = {"sede": request.GET.get("sede")} if not plan and request.GET.get("sede") else None
    form = PlanForm(request.POST or None, instance=plan, initial=inicial)
    if request.method == "POST" and form.is_valid():
        nuevo = form.save(commit=False)
        if not nuevo.pk:
            nuevo.creado_por = request.user
        nuevo.save()
        messages.success(request, f"Plan de mantenimiento guardado: {nuevo}. Próximo: {nuevo.proxima:%d/%m/%Y}.")
        return redirect("cronograma:mantenimiento_detalle", pk=nuevo.pk)
    return render(request, "cronograma/mantenimiento_form.html", {"form": form, "plan": plan})


@login_required
def detalle(request, pk):
    """El plan, sus mantenimientos generados y los equipos que la plataforma conoce en la sede.
    POST «generar»: crea ya el ticket del próximo mantenimiento (sin esperar la fecha)."""
    _exigir_supervisor(request)
    plan = get_object_or_404(PlanMantenimiento.objects.select_related("sede__empresa", "creado_por"), pk=pk)
    if request.method == "POST" and request.POST.get("accion") == "generar":
        if plan.en_curso:
            messages.warning(request, f"Ya hay un mantenimiento en curso: {plan.en_curso.ticket.radicado}.")
        else:
            fecha = plan.proxima
            t = crear_ticket(plan, fecha, autor=request.user)
            plan.proxima = sumar_meses(max(fecha, timezone.localdate()), plan.frecuencia)
            plan.save(update_fields=["proxima", "actualizado"])
            messages.success(request, f"Se creó el ticket {t.radicado}. El siguiente mantenimiento quedó para el "
                                      f"{plan.proxima:%d/%m/%Y}.")
        return redirect("cronograma:mantenimiento_detalle", pk=plan.pk)
    from tickets import historial_cliente

    return render(request, "cronograma/mantenimiento_detalle.html", {
        "plan": plan, "ejecuciones": plan.ejecuciones.select_related("ticket", "ticket__asignado_a"),
        "en_curso": plan.en_curso, "historial": historial_cliente.resumen(plan.sede.empresa, plan.sede),
        "otros": plan.sede.planes_mantenimiento.exclude(pk=plan.pk), "dias_antes": dias_antes(),
    })

"""Clientes y sedes: el módulo principal de los clientes (unificado el 23/11/2026).

Reúne lo que antes estaba en tres lugares (Gestión → Clientes, la ficha de cliente del inventario y «Mantenimientos»):

* **Listado de clientes** con sus sedes, los equipos instalados y el estado del mantenimiento (al día, próximo en
  15 días, vencido, programado o sin mantenimiento). Arriba, los mantenimientos por programar.
* **Ficha del cliente**: datos, encargados y sedes (cada una con su estado). Alimenta el autocompletado de cliente y
  sede al radicar o programar un ticket: lo inactivo deja de sugerirse pero conserva su historial.
* **Ficha de la sede**: su mantenimiento por sistema (frecuencia, último realizado, próximo y el botón «Programar
  mantenimiento», que crea el ticket con los equipos), los equipos instalados por sistema y el historial de visitas.
* **Equipo instalado**: alta ordenada (sistema, equipo, marca, modelo, serial, ubicación, foto, características,
  de quién es) y su hoja de vida con cada acta en que se intervino.

Ven el módulo el personal técnico y quienes gestionan el inventario; los datos del cliente y de las sedes los editan
coordinación, dirección y bodega; los equipos instalados y los planes de mantenimiento, solo coordinación y dirección.
"""
from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Empresa, Sede

from .forms import BootstrapMixin

# Orden de gravedad del semáforo de mantenimiento (el peor manda en la sede y en el cliente)
GRAVEDAD = {"vencido": 0, "proximo": 1, "programado": 2, "al_dia": 3}
ESTADOS = {
    "vencido": ("Vencido", "danger"),
    "proximo": ("Próximo (15 días)", "warning"),
    "programado": ("Programado", "info"),
    "al_dia": ("Al día", "success"),
    "sin": ("Sin mantenimiento", "secondary"),
}


# ---------------------------------------------------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------------------------------------------------
def puede_ver(usuario):
    """Personal técnico, coordinación, dirección y quien usa el inventario."""
    return usuario.es_personal or usuario.ve_inventario


def puede_editar_datos(usuario):
    """Datos del cliente, sedes y encargados: coordinación, dirección y bodega."""
    return usuario.es_supervisor or usuario.gestiona_inventario


def puede_editar_equipos(usuario):
    """Equipos instalados y planes de mantenimiento: coordinación técnica y dirección."""
    return usuario.es_supervisor


def _exigir(prueba):
    def decorador(vista):
        @wraps(vista)
        def envoltura(request, *args, **kwargs):
            if not prueba(request.user):
                raise PermissionDenied
            return vista(request, *args, **kwargs)
        return envoltura
    return decorador


ver_requerido = _exigir(puede_ver)
datos_requerido = _exigir(puede_editar_datos)
equipos_requerido = _exigir(puede_editar_equipos)


# ---------------------------------------------------------------------------------------------------------------------
# Formularios
# ---------------------------------------------------------------------------------------------------------------------
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
        self.fields["ciudad"].help_text = "Donde queda de verdad. Ej.: EDS Colono queda en Granada."
        self._aplicar_estilos()

    def clean_nombre(self):
        """Quita espacios de más y evita dos sedes con el mismo nombre en el cliente."""
        nombre = " ".join(self.cleaned_data["nombre"].split())
        if self.empresa.sedes.filter(nombre__iexact=nombre).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este cliente ya tiene una sede con ese nombre.")
        return nombre


def _equipo_form_class():
    from cronograma.models import EquipoInstalado

    class EquipoForm(BootstrapMixin, forms.ModelForm):
        """Alta ordenada de un equipo instalado: sistema, qué es, marca y modelo, serial, dónde está, de quién es."""
        class Meta:
            model = EquipoInstalado
            fields = ["sistema", "tipo", "marca", "modelo", "serial", "ubicacion", "propiedad", "placa", "estado",
                      "fecha_instalacion", "garantia_hasta", "caracteristicas", "foto", "observaciones"]
            widgets = {
                "fecha_instalacion": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                "garantia_hasta": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                "caracteristicas": forms.Textarea(attrs={"rows": 3, "placeholder": "Ej.: 4 MP, IR 30 m, IP 192.168.1.64, PoE"}),
                "observaciones": forms.Textarea(attrs={"rows": 2}),
                "foto": forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
            }

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._aplicar_estilos()

        def clean(self):
            d = super().clean()
            if d.get("propiedad") and d["propiedad"] != EquipoInstalado.Propiedad.CLIENTE and not (d.get("placa") or "").strip():
                self.add_error("placa", "Los equipos de JANOTEC llevan su placa de inventario.")
            for campo in ("tipo", "marca", "modelo", "serial", "ubicacion", "placa"):
                if d.get(campo):
                    d[campo] = " ".join(d[campo].split())
            return d

    return EquipoForm


def _plan_form_class():
    from cronograma.mantenimiento import PlanForm

    class PlanSedeForm(PlanForm):
        """El plan de un sistema de la sede (la sede ya va dada)."""
        class Meta(PlanForm.Meta):
            fields = ["sistema", "frecuencia", "ultimo_realizado", "proxima", "detalle", "con_contrato", "contrato",
                      "contrato_vence", "valor", "observaciones", "activo"]
            widgets = {**PlanForm.Meta.widgets,
                       "ultimo_realizado": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

        def __init__(self, *args, sede=None, **kwargs):
            self.sede = sede
            super().__init__(*args, **kwargs)
            self.fields.pop("sede", None)

        def clean(self):
            d = super().clean()
            sistema = d.get("sistema")
            if sistema and self.sede.planes_mantenimiento.filter(sistema=sistema).exclude(pk=self.instance.pk).exists():
                self.add_error("sistema", "Esta sede ya tiene un plan para ese sistema: edítelo.")
            return d

    return PlanSedeForm


# ---------------------------------------------------------------------------------------------------------------------
# Estado del mantenimiento
# ---------------------------------------------------------------------------------------------------------------------
def estado_de(planes):
    """El peor estado de una lista de planes activos ("sin" si no hay ninguno)."""
    estados = [p.estado for p in planes if p.activo]
    return min(estados, key=lambda e: GRAVEDAD.get(e, 9)) if estados else "sin"


def _planes_por_sede(sedes_ids):
    from cronograma.models import PlanMantenimiento

    por_sede = {}
    for p in (PlanMantenimiento.objects.filter(sede_id__in=sedes_ids, activo=True)
              .prefetch_related("ejecuciones__ticket")):
        por_sede.setdefault(p.sede_id, []).append(p)
    return por_sede


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


# ---------------------------------------------------------------------------------------------------------------------
# Listado y ficha del cliente
# ---------------------------------------------------------------------------------------------------------------------
@login_required
@ver_requerido
def clientes(request):
    """Clientes con sus sedes, equipos instalados y estado del mantenimiento; arriba, lo que hay que programar.
    Filtra por estado del cliente y del mantenimiento y busca por nombre, NIT, ciudad o sede."""
    from cronograma import mantenimiento
    from cronograma.models import EquipoInstalado

    mantenimiento.revisar_si_toca()
    q = request.GET.get("q", "").strip()
    estado = request.GET.get("estado", "activos")
    mant = request.GET.get("mantenimiento", "")
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
    lista = list(lista.order_by("razon_social"))

    sedes = dict(Sede.objects.filter(empresa__in=lista, activa=True).values_list("pk", "empresa_id"))
    planes = _planes_por_sede(list(sedes))
    equipos = dict(EquipoInstalado.objects.filter(sede__empresa__in=lista).exclude(estado=EquipoInstalado.Estado.RETIRADO)
                   .values("sede__empresa").annotate(n=Count("id")).values_list("sede__empresa", "n"))
    por_empresa = {}
    for sede_id, empresa_id in sedes.items():
        por_empresa.setdefault(empresa_id, []).extend(planes.get(sede_id, []))
    filas = []
    for e in lista:
        e.planes = por_empresa.get(e.pk, [])
        e.estado_mant = estado_de(e.planes)
        e.n_equipos = equipos.get(e.pk, 0)
        e.proxima = min((p.proxima for p in e.planes), default=None)
        if mant == "por_programar" and e.estado_mant not in ("vencido", "proximo"):
            continue
        if mant == "con" and e.estado_mant == "sin":
            continue
        if mant == "sin" and e.estado_mant != "sin":
            continue
        filas.append(e)
    return render(request, "tickets/clientes.html", {
        "lista": filas, "q": q, "estado": estado, "mant": mant, "ESTADOS": ESTADOS,
        "por_programar": mantenimiento.por_programar(), "dias_antes": mantenimiento.dias_antes(),
        "puede_editar": puede_editar_datos(request.user),
    })


@login_required
@datos_requerido
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
@ver_requerido
def cliente_detalle(request, pk):
    """Ficha del cliente: datos, encargados y sedes con su estado de mantenimiento. El mismo formulario agrega una
    sede nueva o edita la elegida (?sede=)."""
    from cronograma.models import EquipoInstalado

    empresa = get_object_or_404(Empresa.objects.prefetch_related("encargados"), pk=pk, es_prospecto=False)
    editar = puede_editar_datos(request.user)
    id_sede = request.POST.get("sede") or request.GET.get("sede")
    sede_editada = get_object_or_404(Sede, pk=id_sede, empresa=empresa) if id_sede else None
    form = None
    if editar:
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
    elif request.method == "POST":
        raise PermissionDenied
    sedes = list(empresa.sedes.annotate(n_tickets=Count("tickets", distinct=True)).order_by("-activa", "nombre"))
    planes = _planes_por_sede([s.pk for s in sedes])
    equipos = dict(EquipoInstalado.objects.filter(sede__in=sedes).exclude(estado=EquipoInstalado.Estado.RETIRADO)
                   .values("sede").annotate(n=Count("id")).values_list("sede", "n"))
    from inventario.clientes import _municipio_sugerido, _pistas, equipos_de

    municipios, recibio = _pistas(empresa)  # lo que dicen las actas: municipio escrito y quién recibió la última
    for s in sedes:
        s.planes = planes.get(s.pk, [])
        s.estado_mant = estado_de(s.planes)
        s.n_equipos = equipos.get(s.pk, 0)
        s.sugerido = "" if s.ciudad else _municipio_sugerido(municipios.get(s.pk))
        s.recibio = recibio.get(s.pk)

    return render(request, "tickets/cliente_admin.html", {
        "empresa": empresa, "sedes": sedes, "de_inventario": equipos_de(empresa), "form": form, "sede_editada": sede_editada, "ESTADOS": ESTADOS,
        "sin_municipio": sum(1 for s in sedes if not s.ciudad),
        "puede_editar": editar, "estado_cliente": estado_de([p for s in sedes if s.activa for p in s.planes]),
    })


@login_required
@datos_requerido
def encargado_form(request, empresa_pk, pk=None):
    """Agregar o editar el encargado del cliente (la persona a cargo de JANOTEC allá). Si cambia, el anterior se
    desmarca y queda en el historial de la ficha."""
    from accounts.models import EncargadoCliente
    from inventario.clientes import EncargadoForm

    empresa = get_object_or_404(Empresa, pk=empresa_pk)
    encargado = get_object_or_404(EncargadoCliente, pk=pk, empresa=empresa) if pk else EncargadoCliente(empresa=empresa)
    form = EncargadoForm(request.POST or None, instance=encargado)
    if request.method == "POST" and form.is_valid():
        guardado = form.save()
        messages.success(request, f"Se guardó el encargado {guardado.nombre}.")
        return redirect("cliente_detalle", pk=empresa.pk)
    return render(request, "tickets/cliente_dato_form.html", {
        "form": form, "empresa": empresa,
        "titulo": encargado.nombre if pk else f"¿Quién es el encargado en {empresa.razon_social}?"})


@login_required
@datos_requerido
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
@datos_requerido
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


# ---------------------------------------------------------------------------------------------------------------------
# Ficha de la sede: mantenimiento por sistema, equipos instalados e historial
# ---------------------------------------------------------------------------------------------------------------------
@login_required
@ver_requerido
def sede_detalle(request, pk):
    """La sede con su mantenimiento por sistema, sus equipos instalados agrupados por sistema y el historial.
    POST «programar» (coordinación): crea el ticket del próximo mantenimiento del plan con sus equipos."""
    from cronograma import mantenimiento
    from cronograma.models import EquipoInstalado, PlanMantenimiento

    from . import historial_cliente

    sede = get_object_or_404(Sede.objects.select_related("empresa"), pk=pk)
    if request.method == "POST":
        if not puede_editar_equipos(request.user):
            raise PermissionDenied
        plan = get_object_or_404(PlanMantenimiento, pk=request.POST.get("plan"), sede=sede)
        if request.POST.get("accion") == "programar":
            ticket = mantenimiento.programar(plan, autor=request.user)
            if ticket is None:
                messages.warning(request, f"Ya hay un mantenimiento en curso: {plan.en_curso.ticket.radicado}.")
            else:
                messages.success(request, f"Se creó el ticket {ticket.radicado} del mantenimiento de "
                                          f"{plan.get_sistema_display()}; quedó en «Recepcionar». El siguiente queda para "
                                          f"el {plan.proxima:%d/%m/%Y}.")
        return redirect("sede_detalle", pk=sede.pk)

    planes = list(sede.planes_mantenimiento.prefetch_related("ejecuciones__ticket").order_by("-activo", "proxima"))
    for p in planes:
        p.ultimo = mantenimiento.ultimo(p)
        p.curso = p.en_curso
    equipos = list(sede.equipos_instalados.order_by("sistema", "ubicacion", "tipo"))
    ver_retirados = request.GET.get("retirados") == "1"
    grupos = {}
    nombres = dict(PlanMantenimiento.Sistema.choices)
    for e in equipos:
        if e.estado == EquipoInstalado.Estado.RETIRADO and not ver_retirados:
            continue
        grupos.setdefault(e.sistema, {"nombre": nombres.get(e.sistema, e.sistema), "equipos": []})["equipos"].append(e)
    return render(request, "tickets/sede_detalle.html", {
        "sede": sede, "planes": planes, "grupos": list(grupos.values()), "ESTADOS": ESTADOS,
        "estado_sede": estado_de(planes), "dias_antes": mantenimiento.dias_antes(),
        "retirados": sum(1 for e in equipos if e.estado == EquipoInstalado.Estado.RETIRADO), "ver_retirados": ver_retirados,
        "en_servicio": sum(1 for e in equipos if e.estado != EquipoInstalado.Estado.RETIRADO),
        "historial": historial_cliente.resumen(sede.empresa, sede),
        "puede_editar": puede_editar_equipos(request.user), "puede_editar_datos": puede_editar_datos(request.user),
    })


@login_required
@equipos_requerido
def plan_form(request, sede_pk, pk=None):
    """Agregar o editar el mantenimiento de un sistema de la sede (frecuencia, último realizado y próximo)."""
    from cronograma import mantenimiento
    from cronograma.models import PlanMantenimiento

    sede = get_object_or_404(Sede.objects.select_related("empresa"), pk=sede_pk)
    plan = get_object_or_404(PlanMantenimiento, pk=pk, sede=sede) if pk else PlanMantenimiento(sede=sede)
    sistema = request.GET.get("sistema")
    form = _plan_form_class()(request.POST or None, instance=plan, sede=sede,
                              initial={"sistema": sistema} if sistema and not pk else None)
    if request.method == "POST" and form.is_valid():
        nuevo = form.save(commit=False)
        nuevo.sede = sede
        if not nuevo.pk:
            nuevo.creado_por = request.user
        nuevo.save()
        messages.success(request, f"Mantenimiento de {nuevo.get_sistema_display()} guardado: "
                                  f"{nuevo.get_frecuencia_display().lower()}, el próximo el {nuevo.proxima:%d/%m/%Y}.")
        return redirect("sede_detalle", pk=sede.pk)
    return render(request, "tickets/sede_plan_form.html", {"form": form, "sede": sede, "plan": plan if pk else None,
                                                           "dias_antes": mantenimiento.dias_antes()})


@login_required
@equipos_requerido
def equipo_form(request, sede_pk=None, pk=None):
    """Agregar (en la sede) o editar un equipo instalado."""
    from cronograma.models import EquipoInstalado

    if pk:
        equipo = get_object_or_404(EquipoInstalado.objects.select_related("sede__empresa"), pk=pk)
        sede = equipo.sede
    else:
        sede = get_object_or_404(Sede.objects.select_related("empresa"), pk=sede_pk)
        equipo = EquipoInstalado(sede=sede, sistema=request.GET.get("sistema") or "")
    form = _equipo_form_class()(request.POST or None, request.FILES or None, instance=equipo)
    if request.method == "POST" and form.is_valid():
        guardado = form.save(commit=False)
        if not guardado.pk:
            guardado.creado_por = request.user
        guardado.save()
        messages.success(request, f"Equipo {'agregado' if not pk else 'actualizado'}: {guardado}.")
        if request.POST.get("otro") and not pk:
            return redirect(f"{request.path}?sistema={guardado.sistema}")
        return redirect("sede_detalle", pk=sede.pk)
    from cronograma.models import EquipoInstalado as EI

    sugerencias = (EI.objects.exclude(tipo="").values_list("tipo", flat=True).distinct().order_by("tipo")[:200])
    marcas = EI.objects.exclude(marca="").values_list("marca", flat=True).distinct().order_by("marca")[:200]
    return render(request, "tickets/equipo_form.html", {"form": form, "sede": sede, "equipo": equipo if pk else None,
                                                        "sugerencias": sugerencias, "marcas": marcas})


@login_required
@ver_requerido
def equipo_detalle(request, pk):
    """Hoja de vida del equipo: sus datos y cada acta en que se intervino, de la más reciente a la más antigua."""
    from cronograma.models import EquipoInstalado

    equipo = get_object_or_404(EquipoInstalado.objects.select_related("sede__empresa", "creado_por"), pk=pk)
    registros = (equipo.registros_acta.select_related("acta", "acta__ticket").prefetch_related("fotos")
                 .order_by("-acta__fecha"))
    return render(request, "tickets/equipo_detalle.html", {
        "e": equipo, "registros": registros, "puede_editar": puede_editar_equipos(request.user),
        "hoy": timezone.localdate(),
    })


@login_required
@datos_requerido
def importar_clientes(request):
    """Sube la relación de clientes y sedes en Excel: primero muestra qué haría y solo guarda cuando se confirma.

    Crea lo que falta y completa lo vacío sin pisar lo escrito (ver ``importar_clientes``). Los equipos (panel de
    alarma, videovigilancia) solo se cargan si quien importa puede editar equipos: coordinación técnica o dirección.
    """
    from inventario import temporales

    from . import importar_clientes as imp

    con_equipos = puede_editar_equipos(request.user)
    contexto = {"con_equipos": con_equipos, "total_clientes": Empresa.objects.filter(es_prospecto=False).count(),
                "total_sedes": Sede.objects.count()}
    if request.method == "POST" and request.POST.get("accion") == "importar":
        ruta = temporales.ruta(request.POST.get("token", ""), "clientes", "xlsx")
        if not ruta or not ruta.exists():
            messages.error(request, "El archivo revisado ya no está. Súbalo otra vez.")
            return redirect("importar_clientes")
        try:
            r = imp.aplicar(imp.leer(ruta), request.user, con_equipos)
        finally:
            ruta.unlink(missing_ok=True)
        messages.success(request, f"Listo: {len(r['nuevos_clientes'])} clientes y {len(r['nuevas_sedes'])} sedes nuevas, "
                                  f"{len(r['sedes_completar'])} sedes completadas y {r['equipos']} equipos cargados.")
        return redirect("clientes")
    if request.method == "POST":
        archivo = request.FILES.get("archivo")
        if not archivo or not archivo.name.lower().endswith(".xlsx"):
            messages.error(request, "Suba la relación en formato .xlsx (en Google Sheets: Archivo → Descargar → Microsoft Excel).")
        elif archivo.size > 5 * 1024 * 1024:
            messages.error(request, "El archivo pesa más de 5 MB.")
        else:
            token, ruta = temporales.guardar(archivo, "clientes", "xlsx")
            try:
                filas = imp.leer(ruta)
            except Exception:  # archivo dañado o que no es un libro de Excel
                filas = None
            if not filas:
                ruta.unlink(missing_ok=True)
                messages.error(request, "No se encontró ninguna hoja con las columnas CLIENTE y SEDE."
                               if filas is not None else "No se pudo leer el archivo. Revise que sea un Excel .xlsx.")
            else:
                contexto.update(plan=imp.plan(filas, con_equipos), token=token, nombre_archivo=archivo.name)
    return render(request, "tickets/importar_clientes.html", contexto)

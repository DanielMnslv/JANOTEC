"""Productos: el inventario visto como un catálogo, como el cronograma ve los tickets.

- Equipos (con serial): se agrupan por modelo (referencia). Cada modelo dice cuántas unidades hay y dónde, y al
  abrirlo se ve cada unidad con su placa y su serial.
- Materiales (sin serial, por cantidad): cada insumo con lo que hay en bodega y con los técnicos.

Por defecto se ve lo que JANOTEC tiene (en bodega, con técnicos, en mantenimiento o garantía); lo vendido, alquilado,
prestado o dado de baja se oculta y se muestra con «Ver también lo que ya no está».
"""
from collections import Counter, defaultdict
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.admin.models import CHANGE, DELETION, LogEntry
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from tickets.forms import BootstrapMixin

from . import servicios
from .models import Activo, Insumo, MinimoModelo, MovimientoActivo, MovimientoInsumo
from .views import gestion_requerida

E = Activo.Estado
EN_JANOTEC = [E.DISPONIBLE, E.EN_TECNICO, E.ASIGNADO, E.MANTENIMIENTO, E.GARANTIA]
CON_CLIENTES = [E.ALQUILADO, E.PRESTADO]
YA_NO_ESTAN = [E.VENDIDO, E.BAJA, E.NO_ENCONTRADO]
MINIMO_POR_DEFECTO = 1  # todo producto se repone cuando en bodega queda menos de 1; el encargado lo cambia al ingresarlo o modificarlo


def _clave(referencia):
    return " ".join((referencia or "").upper().split())


def _registro(usuario, objeto, accion, mensaje):
    """Deja constancia del cambio en el historial de administración (quién, cuándo y qué)."""
    LogEntry.objects.create(user=usuario, content_type=ContentType.objects.get_for_model(objeto), object_id=str(objeto.pk),
                            object_repr=str(objeto)[:200], action_flag=accion, change_message=mensaje[:1000])


def filas_equipos(q="", todos=False):
    """Una fila por modelo de equipo: cuántas unidades hay y dónde, y su stock mínimo."""
    from .models import MinimoModelo

    activos = Activo.objects.all()
    if q:
        activos = activos.filter(Q(referencia__icontains=q) | Q(descripcion__icontains=q) | Q(marca__icontains=q)
                                 | Q(placa__icontains=q) | Q(serial__icontains=q) | Q(tipo__icontains=q))
    modelos = {}
    for a in activos.only("referencia", "marca", "descripcion", "categoria", "tipo", "estado", "valor", "iva"):
        m = modelos.setdefault(_clave(a.referencia), {"referencia": a.referencia.strip(), "marcas": Counter(),
                                                      "descripciones": Counter(), "tipo": a.tipo, "categoria": a.categoria,
                                                      "estados": Counter(), "valores": []})
        m["marcas"][a.marca] += 1
        m["descripciones"][a.descripcion] += 1
        m["estados"][a.estado] += 1
        if a.valor:
            m["valores"].append(a.valor * (1 + (a.iva or Decimal(0)) / 100))
    minimos = {_clave(r): n for r, n in MinimoModelo.objects.values_list("referencia", "minimo")}
    filas = []
    for clave, m in modelos.items():
        e = m["estados"]
        f = {"tipo": "equipo", "clave": m["referencia"], "codigo": m["referencia"] or "(sin modelo)",
             "marca": m["marcas"].most_common(1)[0][0], "descripcion": m["descripciones"].most_common(1)[0][0],
             "grupo": " · ".join(x for x in (m["categoria"], m["tipo"]) if x), "unidad": "unidades",
             "bodega": e[E.DISPONIBLE], "tecnicos": e[E.EN_TECNICO] + e[E.ASIGNADO] + e[E.MANTENIMIENTO] + e[E.GARANTIA],
             "clientes": sum(e[x] for x in CON_CLIENTES), "fuera": sum(e[x] for x in YA_NO_ESTAN),
             "minimo": minimos.get(clave, MINIMO_POR_DEFECTO), "activo": True, "categoria": m["categoria"], "subtipo": m["tipo"],
             "costo": sum(m["valores"]) / len(m["valores"]) if m["valores"] else None}
        f["en_janotec"] = f["bodega"] + f["tecnicos"]
        if todos or q or f["en_janotec"] or clave in minimos:  # lo agotado del todo solo sale si se le fijó un mínimo
            filas.append(f)
    return filas, len(modelos)


def filas_materiales(q="", todos=False):
    """Una fila por material (insumo): lo que hay en bodega y con los técnicos, y su stock mínimo."""
    insumos = Insumo.objects.all() if todos else Insumo.objects.filter(activo=True)
    if q:
        insumos = insumos.filter(Q(codigo__icontains=q) | Q(descripcion__icontains=q) | Q(marca__icontains=q))
    saldos = servicios.saldos_insumos()
    costos = servicios.costo_promedio()
    filas, total = [], 0
    for i in insumos:
        total += 1
        s = saldos.get(i.pk)
        bodega = s["bodega"] if s else Decimal(0)
        tecnicos = sum((v for v in s["tecnicos"].values() if v > 0), Decimal(0)) if s else Decimal(0)
        if todos or q or bodega or tecnicos or i.stock_minimo:
            filas.append({"tipo": "material", "clave": i.codigo, "codigo": i.codigo, "marca": i.marca, "descripcion": i.descripcion,
                          "grupo": "", "unidad": i.get_unidad_display().lower(), "bodega": bodega, "tecnicos": tecnicos,
                          "clientes": None, "fuera": None, "minimo": i.stock_minimo, "activo": i.activo, "insumo": i,
                          "categoria": "", "subtipo": "", "costo": costos.get(i.pk) or None})
    return filas, total


@login_required
@gestion_requerida
def productos(request):
    """El panel del inventario: todos los productos (equipos y materiales) con sus botones, como en cualquier inventario."""
    if request.method == "POST":
        return _accion_producto(request)
    tipo = request.GET.get("tipo") if request.GET.get("tipo") in ("equipos", "materiales") else "todos"
    q = request.GET.get("q", "").strip()
    todos = request.GET.get("todos") == "1"
    solo_bajos = request.GET.get("bajos") == "1"
    filas, total = [], 0
    if tipo in ("todos", "equipos"):
        f, n = filas_equipos(q, todos)
        filas, total = filas + f, total + n
    if tipo in ("todos", "materiales"):
        f, n = filas_materiales(q, todos)
        filas, total = filas + f, total + n
    for f in filas:
        f["bajo"] = bool(f["minimo"]) and f["bodega"] < f["minimo"]
    bajos = sum(1 for f in filas if f["bajo"])
    if solo_bajos:
        filas = [f for f in filas if f["bajo"]]
    filas.sort(key=lambda f: (not f["bajo"], f["descripcion"].upper()))
    return render(request, "inventario/productos.html", {
        "filas": filas, "tipo": tipo, "q": q, "todos": todos, "solo_bajos": solo_bajos, "bajos": bajos,
        "ocultos": total - len(filas) if not (q or solo_bajos) else 0,
        "equipos": sum(1 for f in filas if f["tipo"] == "equipo"), "materiales": sum(1 for f in filas if f["tipo"] == "material"),
    })


def _volver(request):
    volver = request.POST.get("volver", "")
    return redirect(volver if volver.startswith("/") and not volver.startswith("//") else reverse("inventario:productos"))


def _accion_producto(request):
    """Desde la fila del producto: fijar su stock mínimo o eliminarlo (solo si se registró por error)."""
    from .models import MinimoModelo

    accion, tipo, clave = request.POST.get("accion"), request.POST.get("tipo"), request.POST.get("clave", "").strip()
    if accion == "minimo":
        try:
            valor = Decimal((request.POST.get("minimo") or "0").replace(",", "."))
            if valor < 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            messages.error(request, "Escriba el stock mínimo como un número.")
            return _volver(request)
        if tipo == "equipo":
            MinimoModelo.objects.update_or_create(referencia=clave, defaults={"minimo": int(valor)})
        else:
            Insumo.objects.filter(codigo=clave).update(stock_minimo=valor)
        messages.success(request, f"Stock mínimo de {clave}: {valor.normalize():f}.")
    elif accion == "eliminar" and tipo == "material":
        insumo = get_object_or_404(Insumo, codigo=clave)
        if MovimientoInsumo.objects.filter(insumo=insumo).exists():
            messages.error(request, f"{clave} ya tiene movimientos y su historia se conserva. Si ya no se usa, "
                                    "desmarque «Se sigue usando» en «Modificar».")
        else:
            _registro(request.user, insumo, DELETION, f"Eliminó el material {insumo.codigo} · {insumo.descripcion}")
            insumo.delete()
            messages.success(request, f"{clave} eliminado.")
    elif accion == "eliminar" and tipo == "equipo":
        unidades = [a for a in Activo.objects.all() if _clave(a.referencia) == _clave(clave)]
        bloqueadas = [a.placa for a in unidades if not se_puede_eliminar(a)[0]]
        if not unidades or bloqueadas:
            messages.error(request, f"El modelo {clave} tiene unidades con historia ({', '.join(bloqueadas[:5])}): no se borra. "
                                    "Dé de baja las unidades que ya no estén.")
        else:
            with transaction.atomic():
                for a in unidades:
                    _registro(request.user, a, DELETION, f"Eliminó {a.placa} (modelo {clave}, registrado por error)")
                    MovimientoActivo.objects.filter(activo=a).delete()
                    a.delete()
            messages.success(request, f"Modelo {clave} eliminado con sus {len(unidades)} unidad(es).")
    else:
        raise Http404
    return _volver(request)


class InsumoNuevoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Insumo
        fields = ["codigo", "descripcion", "unidad", "marca", "stock_minimo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["codigo"].help_text = "Corto y sin espacios, ej.: CAN-20X12. Así lo encuentra al entregar o comprar."
        self._aplicar_estilos()

    def clean_codigo(self):
        codigo = self.cleaned_data["codigo"].strip().upper().replace(" ", "-")
        if Insumo.objects.filter(codigo=codigo).exists():
            raise forms.ValidationError(f"Ya existe el material {codigo}.")
        return codigo


@login_required
@gestion_requerida
def insumo_nuevo(request):
    """Crear un material en el catálogo (sin existencias: entran con la compra)."""
    form = InsumoNuevoForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        insumo = form.save()
        messages.success(request, f"Material {insumo.codigo} creado. Sus existencias entran al registrar la compra.")
        return redirect(f"{reverse('inventario:productos')}?tipo=materiales&q={insumo.codigo}")
    return render(request, "inventario/insumo_nuevo.html", {"form": form})


class ModeloForm(BootstrapMixin, forms.Form):
    """Lo común a todas las unidades de un modelo: se corrige una vez y queda igual en todas."""

    referencia = forms.CharField(label="Modelo / referencia", max_length=80)
    marca = forms.CharField(max_length=80, required=False)
    descripcion = forms.CharField(label="Descripción", max_length=300)
    categoria = forms.CharField(label="Categoría", max_length=40, required=False)
    tipo = forms.CharField(max_length=80, required=False)
    minimo = forms.IntegerField(label="Stock mínimo", min_value=0, initial=MINIMO_POR_DEFECTO, required=False,
                                help_text="Se avisa para reponer cuando en bodega quedan menos unidades de este modelo.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


@login_required
@gestion_requerida
def modelo(request):
    """Un modelo con todas sus unidades (placa y serial), y la corrección de sus datos para todas a la vez."""
    clave = _clave(request.GET.get("ref") or request.POST.get("ref"))
    unidades = [a for a in Activo.objects.select_related("tecnico", "empresa", "sede").order_by("placa")
                if _clave(a.referencia) == clave] if clave else []
    if not unidades:
        raise Http404
    base = unidades[0]
    form = ModeloForm(request.POST or None, initial={
        "referencia": base.referencia, "marca": Counter(a.marca for a in unidades).most_common(1)[0][0],
        "descripcion": Counter(a.descripcion for a in unidades).most_common(1)[0][0], "categoria": base.categoria,
        "tipo": base.tipo, "minimo": next((m.minimo for m in MinimoModelo.objects.all() if _clave(m.referencia) == clave),
                                         MINIMO_POR_DEFECTO)})
    if request.method == "POST" and form.is_valid():
        minimo = form.cleaned_data.pop("minimo")
        d = {k: v.strip() for k, v in form.cleaned_data.items()}
        with transaction.atomic():
            anterior = MinimoModelo.objects.filter(referencia__iexact=base.referencia.strip()).first()
            if minimo is None and anterior:  # sin cambiarlo: se conserva (y sigue al modelo si se le corrigió el nombre)
                minimo = anterior.minimo
            MinimoModelo.objects.filter(referencia__iexact=base.referencia.strip()).delete()
            if minimo is not None:
                MinimoModelo.objects.update_or_create(referencia=d["referencia"], defaults={"minimo": minimo})
            for a in unidades:
                for campo, valor in d.items():
                    setattr(a, campo, valor)
                a.save(update_fields=list(d))
            _registro(request.user, base, CHANGE, f"Corrigió el modelo {clave} en {len(unidades)} unidad(es): {d}")
        messages.success(request, f"Modelo actualizado en sus {len(unidades)} unidades.")
        return redirect(f"{reverse('inventario:modelo')}?ref={d['referencia']}")
    todos = request.GET.get("todos") == "1"
    visibles = [a for a in unidades if todos or a.estado not in YA_NO_ESTAN + CON_CLIENTES]
    conteo = Counter(a.estado for a in unidades)
    from .proveedores import de_unidades
    return render(request, "inventario/modelo.html", {
        "vendedores": de_unidades(unidades), "base": base, "form": form, "unidades": visibles, "todos": todos, "total": len(unidades),
        "conteo": [(E(e).label, n, Activo(estado=e).color_estado) for e, n in conteo.most_common()],
        "ocultas": len(unidades) - len(visibles),
    })


class ActivoForm(BootstrapMixin, forms.ModelForm):
    """Corregir los datos de una unidad. El estado y dónde está no se cambian aquí: eso lo hacen los movimientos."""

    class Meta:
        model = Activo
        fields = ["serial", "referencia", "marca", "descripcion", "categoria", "tipo", "subtipo", "fecha_compra", "proveedor",
                  "factura", "valor", "iva", "observaciones"]
        widgets = {"fecha_compra": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), "descripcion": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()

    def clean_serial(self):
        serial = (self.cleaned_data.get("serial") or "").strip()
        if serial and Activo.objects.filter(serial=serial).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"Ya hay otro equipo con el serial {serial}.")
        return serial


def se_puede_eliminar(activo):
    """Solo lo registrado por error: sigue en bodega y no tiene más historia que su ingreso."""
    if activo.estado != E.DISPONIBLE:
        return False, "Solo se elimina un equipo que está en bodega. Si salió, se vendió o se dañó, use «Dar de baja»."
    if MovimientoActivo.objects.filter(activo=activo).exclude(tipo=MovimientoActivo.Tipo.COMPRA).exists() \
            or activo.garantias.exists():
        return False, "Ya tiene movimientos (entregas, devoluciones, garantías): su historia se conserva. Use «Dar de baja»."
    return True, ""


@login_required
@gestion_requerida
def activo_editar(request, placa):
    activo = get_object_or_404(Activo, placa=placa)
    form = ActivoForm(request.POST or None, instance=activo)
    if request.method == "POST" and form.is_valid():
        cambios = ", ".join(form.changed_data)
        form.save()
        if cambios:
            _registro(request.user, activo, CHANGE, f"Corrigió: {cambios}")
        messages.success(request, f"{activo.placa} actualizado." if cambios else "No había cambios.")
        return redirect(activo)
    puede, motivo = se_puede_eliminar(activo)
    return render(request, "inventario/activo_editar.html", {"activo": activo, "form": form, "puede_eliminar": puede,
                                                              "motivo_no_eliminar": motivo})


@login_required
@gestion_requerida
@require_POST
def activo_eliminar(request, placa):
    activo = get_object_or_404(Activo, placa=placa)
    puede, motivo = se_puede_eliminar(activo)
    if not puede:
        messages.error(request, motivo)
        return redirect(activo)
    ref = activo.referencia
    with transaction.atomic():
        _registro(request.user, activo, DELETION, f"Eliminó {activo.placa} (serial {activo.serial or '—'}, {activo.descripcion})")
        MovimientoActivo.objects.filter(activo=activo).delete()
        activo.delete()
    messages.success(request, f"{placa} eliminado del inventario (estaba registrado por error).")
    return redirect(f"{reverse('inventario:modelo')}?ref={ref}" if Activo.objects.filter(referencia=ref).exists()
                    else reverse("inventario:productos"))


class InsumoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Insumo
        fields = ["descripcion", "unidad", "marca", "stock_minimo", "activo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["activo"].label = "Se sigue usando (sale en las listas)"
        self._aplicar_estilos()


@login_required
@gestion_requerida
def insumo_editar(request, codigo):
    insumo = get_object_or_404(Insumo, codigo=codigo)
    if request.method == "POST" and request.POST.get("accion") == "eliminar":
        if MovimientoInsumo.objects.filter(insumo=insumo).exists():
            messages.error(request, "Ya tiene movimientos (compras, entregas): su historia se conserva. Desactívelo en su lugar.")
            return redirect("inventario:insumo_editar", codigo)
        _registro(request.user, insumo, DELETION, f"Eliminó el material {insumo.codigo} · {insumo.descripcion}")
        insumo.delete()
        messages.success(request, f"{codigo} eliminado.")
        return redirect(f"{reverse('inventario:productos')}?tipo=materiales")
    form = InsumoForm(request.POST or None, instance=insumo)
    if request.method == "POST" and form.is_valid():
        if form.changed_data:
            form.save()
            _registro(request.user, insumo, CHANGE, f"Corrigió: {', '.join(form.changed_data)}")
        messages.success(request, f"{insumo.codigo} actualizado.")
        return redirect(insumo)
    return render(request, "inventario/insumo_editar.html", {
        "insumo": insumo, "form": form, "puede_eliminar": not MovimientoInsumo.objects.filter(insumo=insumo).exists()})

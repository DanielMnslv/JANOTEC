"""Herramientas de trabajo por técnico y su auditoría (las mismas reglas de Inventario_Global).

- Cada técnico tiene sus herramientas asignadas: salen y vuelven sin firmas; cada cambio de responsable queda
  en el historial de entregas.
- La auditoría revisa TODAS las herramientas del técnico (menos las dadas de baja). Lo «registrado» se toma de
  la base, no de la pantalla. «Disponible» cuenta como «En uso». Si algo no está conforme hay que explicarlo
  (nota de 5 caracteres o más) y solo una herramienta dañada se puede dar de baja.
- La auditoría es permanente: no se edita ni se elimina.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AuditoriaHerramientas, DetalleAuditoria, EntregaHerramienta, Herramienta

H = Herramienta.Estado
EE = DetalleAuditoria.EstadoEncontrado
RD = DetalleAuditoria.Resultado
NOTA_MINIMA = 5


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else ""


def _registrar_entrega(herramienta, tipo, tecnico, tecnico_nombre, usuario):
    EntregaHerramienta.objects.create(herramienta=herramienta, herramienta_nombre=herramienta.nombre, tipo=tipo,
                                      tecnico=tecnico, tecnico_nombre=tecnico_nombre or _nombre(tecnico),
                                      registrado_por=_nombre(usuario))


@transaction.atomic
def guardar(herramienta, usuario, responsable_anterior=None):
    """Crear o editar: si cambió el responsable, queda en el historial de entregas."""
    nuevo = herramienta.pk is None
    if herramienta.tecnico and herramienta.estado == H.DISPONIBLE:
        herramienta.estado = H.EN_USO
    if not herramienta.tecnico and herramienta.estado == H.EN_USO:
        herramienta.estado = H.DISPONIBLE
    herramienta.save()
    cambio = (herramienta.tecnico_id != responsable_anterior) if not nuevo else herramienta.tecnico_id is not None
    if cambio and herramienta.tecnico_id:
        _registrar_entrega(herramienta, EntregaHerramienta.Tipo.ENTREGA, herramienta.tecnico, "", usuario)
    elif cambio and responsable_anterior:
        from accounts.models import Usuario
        anterior = Usuario.objects.filter(pk=responsable_anterior).first()
        _registrar_entrega(herramienta, EntregaHerramienta.Tipo.DEVOLUCION, anterior, "", usuario)
    return herramienta


@transaction.atomic
def entregar(herramienta, tecnico, usuario):
    if herramienta.estado == H.BAJA:
        raise ValidationError(f"«{herramienta.nombre}» está dada de baja.")
    anterior = herramienta.tecnico_id
    herramienta.tecnico, herramienta.tecnico_nombre = tecnico, ""
    return guardar(herramienta, usuario, responsable_anterior=anterior)


@transaction.atomic
def devolver(herramienta, usuario):
    anterior = herramienta.tecnico_id
    herramienta.tecnico, herramienta.tecnico_nombre = None, ""
    return guardar(herramienta, usuario, responsable_anterior=anterior)


def por_auditar(tecnico):
    """Lo registrado del técnico (todo menos lo dado de baja), en el orden de la auditoría."""
    return list(Herramienta.objects.filter(tecnico=tecnico).exclude(estado=H.BAJA).order_by("nombre", "pk"))


@transaction.atomic
def registrar_auditoria(tecnico, auditor, encontrado, nota_general="", ip=""):
    """`encontrado` = {id de herramienta: {"cantidad": int, "estado": EE, "nota": str, "baja": bool}}."""
    registradas = por_auditar(tecnico)
    if not registradas:
        raise ValidationError("Este técnico no tiene herramientas asignadas para auditar.")
    errores, filas, bajas = [], [], []
    novedades = unidades_registradas = unidades_encontradas = 0
    for h in registradas:
        dato = encontrado.get(h.pk)
        if dato is None:
            errores.append(f"Falta revisar «{h.nombre}»: la auditoría debe incluir todas las herramientas del técnico.")
            continue
        estado = dato.get("estado")
        if estado not in EE.values:
            errores.append(f"Elija el estado encontrado de «{h.nombre}».")
            continue
        try:
            cantidad = int(dato.get("cantidad"))
        except (TypeError, ValueError):
            cantidad = -1
        if cantidad < 0:
            errores.append(f"La cantidad encontrada de «{h.nombre}» debe ser un número entero mayor o igual a cero.")
            continue
        nota = (dato.get("nota") or "").strip()[:500]
        baja = bool(dato.get("baja"))
        estado_registrado = EE.EN_USO if h.estado == H.DISPONIBLE else h.estado
        if baja and estado != EE.DANADA:
            errores.append(f"Solo se puede dar de baja una herramienta dañada («{h.nombre}»).")
            continue
        if estado == EE.NO_ENCONTRADA:
            cantidad = 0
        if baja:
            resultado = RD.DADA_DE_BAJA
        elif cantidad == 0:
            resultado = RD.NO_ENCONTRADA
        elif cantidad != h.cantidad or estado != estado_registrado:
            resultado = RD.CON_NOVEDAD
        else:
            resultado = RD.CONFORME
        if resultado != RD.CONFORME and len(nota) < NOTA_MINIMA:
            errores.append(f"Explique en la nota qué pasó con «{h.nombre}» (mínimo {NOTA_MINIMA} caracteres).")
            continue
        novedades += resultado != RD.CONFORME
        unidades_registradas += h.cantidad
        unidades_encontradas += cantidad
        filas.append(DetalleAuditoria(herramienta=h, herramienta_nombre=h.nombre, marca=h.marca, serial=h.serial,
                                      cantidad_registrada=h.cantidad, cantidad_encontrada=cantidad,
                                      estado_registrado=h.estado, estado_encontrado=estado, resultado=resultado, nota=nota))
        if baja:
            bajas.append(h)
    if errores:
        raise ValidationError(errores)
    auditoria = AuditoriaHerramientas.objects.create(
        tecnico=tecnico, tecnico_nombre=_nombre(tecnico), auditor=auditor, auditor_nombre=_nombre(auditor),
        resultado=AuditoriaHerramientas.Resultado.CON_NOVEDADES if novedades else AuditoriaHerramientas.Resultado.CONFORME,
        total_herramientas=len(filas), unidades_registradas=unidades_registradas, unidades_encontradas=unidades_encontradas,
        novedades=novedades, nota_general=(nota_general or "").strip()[:1000], ip=(ip or "")[:45])
    for fila in filas:
        fila.auditoria = auditoria
    DetalleAuditoria.objects.bulk_create(filas)
    # Las dadas de baja salen del inventario del técnico en la misma operación
    Herramienta.objects.filter(pk__in=[h.pk for h in bajas], tecnico=tecnico).update(estado=H.BAJA)
    return auditoria

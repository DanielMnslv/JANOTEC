"""Catálogos centrales de marcas y categorías (como marcas y categorias_activo de Inventario_Global).

Los equipos guardan la marca o la categoría como texto; el catálogo define la escritura oficial. Renombrar o
unificar cambia el texto en todos los registros que la usan, así «HIKVISION», «Hikvision» y «Hikvisión» quedan
como una sola marca.
"""
from collections import Counter, defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Activo, CategoriaActivo, CodigoActivo, Herramienta, Insumo, Marca, normalizar

CATALOGOS = {
    "marcas": {"modelo": Marca, "titulo": "Marcas", "singular": "marca", "largo": 120,
               "campos": [(Activo, "marca"), (Insumo, "marca"), (Herramienta, "marca"), (CodigoActivo, "marca")]},
    "categorias": {"modelo": CategoriaActivo, "titulo": "Categorías de activos", "singular": "categoría", "largo": 60,
                   "campos": [(Activo, "categoria")]},
}


def _usos(tipo):
    """{clave normalizada: Counter({escritura: registros})} de todo lo que usa el catálogo."""
    usos = defaultdict(Counter)
    for modelo, campo in CATALOGOS[tipo]["campos"]:
        for valor, n in Counter(modelo.objects.values_list(campo, flat=True)).items():
            if valor and valor.strip():
                usos[normalizar(valor)][valor] += n
    return usos


def filas(tipo):
    """Cada entrada del catálogo con cuántos registros la usan y las escrituras distintas que hay que unificar."""
    usos = _usos(tipo)
    salida = []
    claves = set()
    for item in CATALOGOS[tipo]["modelo"].objects.all():
        clave = normalizar(item.nombre)
        claves.add(clave)
        escrituras = usos.get(clave, Counter())
        salida.append({"item": item, "usos": sum(escrituras.values()),
                       "variantes": sorted((v, n) for v, n in escrituras.items() if v != item.nombre)})
    sueltas = [{"nombre": c.most_common(1)[0][0], "usos": sum(c.values())} for k, c in usos.items() if k not in claves]
    return salida, sorted(sueltas, key=lambda s: -s["usos"])


def _cambiar_texto(tipo, clave, nuevo):
    """Reemplaza en todos los registros cada escritura cuya clave normalizada es `clave`."""
    cambiados = 0
    for modelo, campo in CATALOGOS[tipo]["campos"]:
        for valor in set(modelo.objects.values_list(campo, flat=True)):
            if valor and normalizar(valor) == clave and valor != nuevo:
                cambiados += modelo.objects.filter(**{campo: valor}).update(**{campo: nuevo})
    return cambiados


def _buscar(tipo, nombre, excluir=None):
    clave = normalizar(nombre)
    for item in CATALOGOS[tipo]["modelo"].objects.all():
        if normalizar(item.nombre) == clave and item.pk != getattr(excluir, "pk", None):
            return item
    return None


@transaction.atomic
def crear(tipo, nombre):
    nombre = " ".join((nombre or "").split())[:CATALOGOS[tipo]["largo"]]
    if not nombre:
        raise ValidationError("Escriba el nombre.")
    existente = _buscar(tipo, nombre)
    if existente:
        raise ValidationError(f"Ya existe como «{existente.nombre}».")
    return CATALOGOS[tipo]["modelo"].objects.create(nombre=nombre)


@transaction.atomic
def renombrar(tipo, item, nuevo):
    """Cambia la escritura oficial y la de todos los registros. Si coincide con otra entrada, se unen."""
    nuevo = " ".join((nuevo or "").split())[:CATALOGOS[tipo]["largo"]]
    if not nuevo:
        raise ValidationError("Escriba el nuevo nombre.")
    otra = _buscar(tipo, nuevo, excluir=item)
    cambiados = _cambiar_texto(tipo, normalizar(item.nombre), nuevo)
    if otra:
        cambiados += _cambiar_texto(tipo, normalizar(otra.nombre), nuevo)
        otra.delete()
    item.nombre = nuevo
    item.save(update_fields=["nombre"])
    return cambiados


@transaction.atomic
def unificar(tipo, item):
    """Deja con la escritura oficial los registros escritos distinto (Hikvision → Hikvisión)."""
    return _cambiar_texto(tipo, normalizar(item.nombre), item.nombre)


@transaction.atomic
def eliminar(tipo, item):
    usos = sum(_usos(tipo).get(normalizar(item.nombre), Counter()).values())
    if usos:
        raise ValidationError(f"«{item.nombre}» se usa en {usos} registro(s): renómbrela o únala con otra en vez de eliminarla.")
    item.delete()


def canonica(tipo, texto):
    """Lo que escribió el usuario → la escritura del catálogo (y si es nueva, la agrega)."""
    texto = " ".join((texto or "").split())[:CATALOGOS[tipo]["largo"]]
    if not texto:
        return ""
    existente = _buscar(tipo, texto)
    if existente:
        return existente.nombre
    CATALOGOS[tipo]["modelo"].objects.create(nombre=texto)
    return texto

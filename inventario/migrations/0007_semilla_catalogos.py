"""Llena los catálogos de marcas y categorías con lo que ya se usa (una entrada por nombre, la escritura más usada)."""
import re
import unicodedata
from collections import Counter

from django.db import migrations


def _clave(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]", " ", texto)).strip().upper()


def _mas_usada(valores):
    grupos = {}
    for valor, n in valores.items():
        valor = (valor or "").strip()
        if valor:
            grupos.setdefault(_clave(valor), Counter())[valor] += n
    return [c.most_common(1)[0][0] for c in grupos.values()]


def sembrar(apps, schema_editor):
    marcas = Counter()
    for modelo in ("Activo", "Insumo", "Herramienta", "CodigoActivo"):
        marcas.update(apps.get_model("inventario", modelo).objects.values_list("marca", flat=True))
    Marca = apps.get_model("inventario", "Marca")
    for nombre in _mas_usada(marcas):
        Marca.objects.get_or_create(nombre=nombre[:120])
    categorias = Counter(apps.get_model("inventario", "Activo").objects.values_list("categoria", flat=True))
    categorias.update({"REDES": 0})  # existía en LISTAS del inventario anterior
    Categoria = apps.get_model("inventario", "CategoriaActivo")
    for nombre in _mas_usada({**categorias, "REDES": 1}):
        Categoria.objects.get_or_create(nombre=nombre[:60])


class Migration(migrations.Migration):
    dependencies = [("inventario", "0006_categoriaactivo_marca_proveedor_correo_and_more")]
    operations = [migrations.RunPython(sembrar, migrations.RunPython.noop)]

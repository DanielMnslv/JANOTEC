"""Equipos que el técnico encuentra instalados en la sede (23/11/2026).

Además de lo que interviene, usa o retira, en el acta el técnico registra lo que ya estaba instalado en la sede
(«Equipos que ya estaban instalados»). Al registrar el acta, cada fila pasa a los equipos instalados de la sede
(cronograma.EquipoInstalado), con su hoja de vida: así cualquier otro técnico, coordinación o comercial sabe qué hay
en cada sede antes de ir.

- Si son varios iguales (cantidad 4, sin serial), se carga uno por uno: cada equipo tiene su propia hoja de vida.
- Si trae serial y ese serial ya está en la sede, no se duplica: se completan los datos que falten.
- Cada equipo queda enlazado al acta donde se reportó (registros_acta), con el técnico y la fecha.
"""

import re

from django.utils import timezone

MAXIMO_POR_FILA = 30  # tope de equipos que se cargan de una sola fila (cantidad)

# El sistema del acta (ActaSoporte.SISTEMAS) en los sistemas de la sede (PlanMantenimiento.Sistema)
SISTEMA_DEL_ACTA = {"CCTV": "cctv", "ALARMA": "alarma", "CONTROL DE ACCESO": "acceso", "REDES": "red",
                    "ENERGIA SOLAR": "fotovoltaica", "RADIO": "radio"}


def unidades(cantidad, serial=""):
    """Cuántos equipos representa la fila: la cantidad escrita (1 si no es un número), uno solo si trae serial."""
    if serial:
        return 1
    numero = re.match(r"\s*(\d+)", cantidad or "")
    return max(1, min(int(numero.group(1)), MAXIMO_POR_FILA)) if numero else 1


def cargar_en_sede(equipo_acta, sede, tecnico, fotos=()):
    """Carga en la sede el equipo que el técnico encontró instalado. Devuelve cuántos equipos quedaron cargados."""
    from cronograma.models import EquipoInstalado

    acta = equipo_acta.acta
    sistema = equipo_acta.sistema_equipo or SISTEMA_DEL_ACTA.get((acta.sistema or "").upper(), "otro")
    estado = equipo_acta.estado_equipo if equipo_acta.estado_equipo in EquipoInstalado.Estado.values else "operativo"
    nota = (f"Reportado por {tecnico.get_full_name() or tecnico.username} en el acta N° {acta.numero} "
            f"({timezone.localtime(acta.fecha):%d/%m/%Y}).")
    datos = {"sistema": sistema, "tipo": equipo_acta.equipo[:100], "marca": equipo_acta.marca[:80],
             "modelo": equipo_acta.referencia[:100], "ubicacion": equipo_acta.ubicacion[:150],
             "caracteristicas": equipo_acta.observaciones, "placa": equipo_acta.placa[:40], "estado": estado}

    serial = (equipo_acta.serial or "").strip()
    if serial:
        existente = (EquipoInstalado.objects.filter(sede=sede, serial__iexact=serial)
                     .exclude(estado=EquipoInstalado.Estado.RETIRADO).first())
        if existente:  # ya estaba cargado: se completan los datos vacíos y se actualiza el estado
            for campo, valor in datos.items():
                if valor and (campo == "estado" or not getattr(existente, campo)):
                    setattr(existente, campo, valor)
            existente.save()
            equipo_acta.equipo_instalado = existente
            equipo_acta.save(update_fields=["equipo_instalado"])
            return 0

    creados = []
    for _ in range(unidades(equipo_acta.cantidad, serial)):
        creados.append(EquipoInstalado.objects.create(sede=sede, serial=serial[:100], observaciones=nota,
                                                      creado_por=tecnico, **datos))
    if fotos:  # la primera foto queda como foto del equipo (de cada uno, si son varios iguales)
        foto = fotos[0]
        for instalado in creados:
            try:
                foto.seek(0)
                instalado.foto.save(foto.name, foto, save=True)
            except (OSError, ValueError):
                break  # la foto ya quedó en el acta; sin foto en la hoja de vida no pasa nada
    equipo_acta.equipo_instalado = creados[0]
    equipo_acta.save(update_fields=["equipo_instalado"])
    return len(creados)

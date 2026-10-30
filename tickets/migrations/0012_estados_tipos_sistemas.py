"""Pasa los tickets al flujo nuevo (generado → recepcionado → en cronograma → autorizado → visita ejecutada)
y deja como «sistemas a intervenir» los cinco del documento de flujo de JANOTEC."""
from django.db import migrations

ESTADOS = {"radicado": "generado", "en_revision": "recepcionado", "asignado": "recepcionado",
           "en_espera": "recepcionado", "resuelto": "visita_ejecutada"}
TIPOS = {"peticion": "requerimiento", "queja": "soporte", "reclamo": "soporte", "sugerencia": "soporte",
         "felicitacion": "soporte"}
RENOMBRAR = {"Alarmas": "Alarma de intrusión", "Equipos informáticos y redes": "Redes de comunicaciones"}
SISTEMAS = [("CCTV / Videovigilancia", "Cámaras, grabadores (DVR/NVR), acceso remoto"),
            ("Alarma de intrusión", "Paneles, sensores, sirenas, falsas alarmas"),
            ("Redes de comunicaciones", "Switches, puntos de acceso, cableado, enlaces"),
            ("Energía solar", "Paneles, inversores, baterías, sistemas fotovoltaicos"),
            ("Control de acceso", "Lectores, biométricos, tarjetas, cerraduras electromagnéticas")]


def adelante(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Seguimiento = apps.get_model("tickets", "Seguimiento")
    Categoria = apps.get_model("tickets", "Categoria")
    for viejo, nuevo in ESTADOS.items():
        Ticket.objects.filter(estado=viejo).update(estado=nuevo)
        Seguimiento.objects.filter(estado_anterior=viejo).update(estado_anterior=nuevo)
        Seguimiento.objects.filter(estado_nuevo=viejo).update(estado_nuevo=nuevo)
    for viejo, nuevo in TIPOS.items():
        Ticket.objects.filter(tipo=viejo).update(tipo=nuevo)
    # Quién lo gestionó: hasta ahora solo radicaba el cliente
    for t in Ticket.objects.filter(radicado_por__isnull=True):
        t.radicado_por_id = t.solicitante_id
        t.save(update_fields=["radicado_por"])
    for viejo, nuevo in RENOMBRAR.items():
        if not Categoria.objects.filter(nombre=nuevo).exists():
            Categoria.objects.filter(nombre=viejo).update(nombre=nuevo)
    nombres = []
    for orden, (nombre, descripcion) in enumerate(SISTEMAS, start=1):
        cat, _ = Categoria.objects.get_or_create(nombre=nombre, defaults={"descripcion": descripcion})
        cat.orden, cat.activa, cat.descripcion = orden, True, descripcion
        cat.save()
        nombres.append(nombre)
    # El resto no se borra (hay tickets y actas que lo usan): solo deja de ofrecerse al radicar
    Categoria.objects.exclude(nombre__in=nombres).update(activa=False)


def atras(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    for viejo, nuevo in ESTADOS.items():
        Ticket.objects.filter(estado=nuevo).update(estado=viejo)
    for estado in ("en_cronograma", "autorizado", "pendiente_visita"):
        Ticket.objects.filter(estado=estado).update(estado="asignado")


class Migration(migrations.Migration):
    dependencies = [("tickets", "0011_ticket_acompanamiento_sst_ticket_departamento_and_more")]

    operations = [migrations.RunPython(adelante, atras)]

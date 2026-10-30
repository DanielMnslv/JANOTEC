"""Sistemas a intervenir: los mismos tipos de servicio de janotecsas.netlify.app (unificación de plataformas)."""
from django.db import migrations

RENOMBRAR = {"CCTV / Videovigilancia": "Mantenimiento CCTV IP", "Redes de comunicaciones": "Fibra óptica / red",
             "Energía solar": "Energía fotovoltaica"}
SISTEMAS = [("Mantenimiento CCTV IP", "Cámaras sin imagen, grabadores (DVR/NVR), acceso remoto, revisión"),
            ("Instalación CCTV IP", "Cámaras nuevas, ampliación del sistema, traslados"),
            ("Control de acceso", "Lectores, biométricos, tarjetas, cerraduras, talanqueras"),
            ("Alarma de intrusión", "Paneles, sensores, sirenas, falsas alarmas"),
            ("Fibra óptica / red", "Switches, puntos de acceso, cableado, enlaces de fibra"),
            ("Energía fotovoltaica", "Paneles solares, inversores, baterías"),
            ("Radiocomunicaciones", "Radios, repetidoras, antenas"),
            ("Diagnóstico técnico", "Cuando no se sabe qué sistema falla: un técnico lo revisa")]


def adelante(apps, schema_editor):
    Categoria = apps.get_model("tickets", "Categoria")
    for viejo, nuevo in RENOMBRAR.items():
        if not Categoria.objects.filter(nombre=nuevo).exists():
            Categoria.objects.filter(nombre=viejo).update(nombre=nuevo)
    nombres = []
    for orden, (nombre, descripcion) in enumerate(SISTEMAS, start=1):
        cat, _ = Categoria.objects.get_or_create(nombre=nombre, defaults={"descripcion": descripcion})
        cat.orden, cat.activa, cat.descripcion = orden, True, descripcion
        cat.save()
        nombres.append(nombre)
    Categoria.objects.exclude(nombre__in=nombres).update(activa=False)


def atras(apps, schema_editor):
    Categoria = apps.get_model("tickets", "Categoria")
    for viejo, nuevo in RENOMBRAR.items():
        Categoria.objects.filter(nombre=nuevo).update(nombre=viejo)


class Migration(migrations.Migration):
    dependencies = [("tickets", "0014_ticket_confirmado_en_ticket_confirmado_por_and_more")]

    operations = [migrations.RunPython(adelante, atras)]

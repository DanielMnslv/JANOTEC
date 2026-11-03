"""Registro de la app «tickets» en Django."""

from django.apps import AppConfig


class TicketsConfig(AppConfig):
    """Configuración de la app de tickets de soporte."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "tickets"
    verbose_name = "Tickets de soporte"

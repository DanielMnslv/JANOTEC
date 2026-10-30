"""Permisos del área administrativa en Configuración (admin de Django).

Los supervisores no son superusuarios: entran a Configuración con estos permisos.
Pueden crear y editar, pero no borrar empresas ni usuarios (se desactivan), para
no perder el historial de tickets y actas.
"""
GRUPO_AREA_ADMINISTRATIVA = "Área administrativa"

PERMISOS = {
    "accounts": {"empresa": ["view", "add", "change"], "sede": ["view", "add", "change", "delete"],
                 "usuario": ["view", "add", "change"]},
    "tickets": {"categoria": ["view", "add", "change"], "tipofalla": ["view", "add", "change", "delete"],
                "actasoporte": ["view", "add", "change"], "equipoacta": ["view", "add", "change", "delete"],
                "ticket": ["view"], "seguimiento": ["view"], "firmaacta": ["view"], "fotoequipo": ["view"],
                "oportunidadcomercial": ["view", "add", "change"],
                "adjunto": ["view"]},
    "comercial": {"fichacomercial": ["view", "change"]},
    "cronograma": {"actividad": ["view", "add", "change", "delete"], "documentopersonal": ["view", "add", "change"]},
}


def asegurar_grupo(**kwargs):
    """Crea o actualiza el grupo con sus permisos (se ejecuta después de cada migrate)."""
    from django.contrib.auth.models import Group, Permission

    grupo, _ = Group.objects.get_or_create(name=GRUPO_AREA_ADMINISTRATIVA)
    codigos = [(app, f"{accion}_{modelo}") for app, modelos in PERMISOS.items()
               for modelo, acciones in modelos.items() for accion in acciones]
    permisos = [p for p in Permission.objects.filter(content_type__app_label__in=PERMISOS.keys())
                if (p.content_type.app_label, p.codename) in codigos]
    grupo.permissions.set(permisos)
    return grupo


def sincronizar_usuario(usuario):
    """El rol manda: supervisor = acceso a Configuración con los permisos del grupo."""
    from django.contrib.auth.models import Group

    grupo = Group.objects.filter(name=GRUPO_AREA_ADMINISTRATIVA).first() or asegurar_grupo()
    if usuario.rol in (usuario.Rol.SUPERVISOR, usuario.Rol.DIRECCION) and usuario.is_active:
        usuario.groups.add(grupo)
    else:
        usuario.groups.remove(grupo)

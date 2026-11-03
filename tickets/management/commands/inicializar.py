from django.core.management.base import BaseCommand

from tickets.models import Categoria, TipoFalla

# Sistemas a intervenir (documento de flujo de JANOTEC). Las categorías anteriores se conservan
# inactivas por los tickets y actas viejos; «Instalación / ampliación» la usa el área comercial.
CATEGORIAS = [
    ("Mantenimiento CCTV IP", "Cámaras sin imagen, grabadores (DVR/NVR), acceso remoto, revisión"),
    ("Instalación CCTV IP", "Cámaras nuevas, ampliación del sistema, traslados"),
    ("Control de acceso", "Lectores, biométricos, tarjetas, cerraduras, talanqueras"),
    ("Alarma de intrusión", "Paneles, sensores, sirenas, falsas alarmas"),
    ("Fibra óptica / red", "Switches, puntos de acceso, cableado, enlaces de fibra"),
    ("Energía fotovoltaica", "Paneles solares, inversores, baterías"),
    ("Radiocomunicaciones", "Radios, repetidoras, antenas"),
    ("Diagnóstico técnico", "Cuando no se sabe qué sistema falla: un técnico lo revisa"),
]
CATEGORIAS_INTERNAS = [("Instalación / ampliación", "Nuevos equipos, traslados, desmontes (área comercial)")]

# Fallas o necesidades por servicio, en el lenguaje del cliente.
# Basadas en lo que más se repite en las actas de soporte 2024-2026. Se editan en Configuración.
OTRA = "Otra falla (la describo abajo)"
FALLAS = {
    "Mantenimiento CCTV IP": [
        "Una o varias cámaras sin imagen",
        "Imagen borrosa, oscura o con interferencia",
        "El grabador (DVR/NVR) no graba o no guarda video",
        "Disco duro lleno o con falla",
        "No puedo ver las cámaras desde el celular o a distancia",
        "Fecha y hora incorrectas en las grabaciones",
        "Necesito copia de una grabación",
        "Cámara movida, golpeada o dañada",
        "Cableado o conectores dañados",
        "Equipos apagados o sin energía (fuente, UPS)",
        OTRA,
    ],
    "Alarma de intrusión": [
        "Falsas alarmas o activaciones constantes",
        "Una zona o sensor no funciona",
        "Teclado o panel con falla",
        "Batería baja o el sistema se apaga sin energía",
        "Botón de pánico no funciona",
        "Sirena no suena o no deja de sonar",
        "Puerta o contacto magnético con falla",
        "Olvidé la clave o necesito cambiar usuarios",
        OTRA,
    ],
    "Control de acceso": [
        "El lector o biométrico no reconoce",
        "La puerta o electroimán no abre o no cierra",
        "Registrar o retirar personal",
        "Talanquera con falla",
        OTRA,
    ],
    "Fibra óptica / red": [
        "Sin conexión de red en los equipos de seguridad",
        "Switch o punto de acceso con falla",
        "UPS con falla o batería agotada",
        "Cableado de red dañado",
        OTRA,
    ],
    "Mantenimiento preventivo": [
        "Programar mantenimiento",
        "Reprogramar un mantenimiento",
        "Revisión general de los equipos",
        OTRA,
    ],
    "Instalación / ampliación": [
        "Instalar equipos nuevos",
        "Ampliar el sistema (más cámaras, sensores…)",
        "Trasladar equipos de lugar",
        "Desmontar o retirar equipos",
        "Cotización",
        OTRA,
    ],
    "Facturación y contratos": [
        "Factura o cobro",
        "Contrato u otrosí",
        "Certificados",
        OTRA,
    ],
}


class Command(BaseCommand):
    help = "Crea las categorías y fallas base de tickets (se puede ejecutar varias veces; no borra lo editado)."

    def handle(self, *args, **opts):
        for orden, (nombre, desc) in enumerate(CATEGORIAS + CATEGORIAS_INTERNAS, start=1):
            _, creada = Categoria.objects.get_or_create(
                nombre=nombre, defaults={"descripcion": desc, "orden": orden, "activa": orden <= len(CATEGORIAS)}
            )
            if creada:
                self.stdout.write(f"  + {nombre}")
        nuevas = 0
        for nombre_cat, fallas in FALLAS.items():
            categoria = Categoria.objects.filter(nombre=nombre_cat).first()
            if not categoria:
                continue
            for orden, falla in enumerate(fallas, start=1):
                _, creada = TipoFalla.objects.get_or_create(
                    categoria=categoria, nombre=falla, defaults={"orden": 99 if falla == OTRA else orden}
                )
                nuevas += creada
        self.stdout.write(self.style.SUCCESS(f"Categorías listas. Fallas nuevas: {nuevas}."))

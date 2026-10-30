"""Carga empresas, sedes, personal y actas de soporte desde «Datos maestros JANOTEC.xlsx».

Se puede ejecutar las veces que se quiera: crea lo que falta y actualiza lo existente
(empresas por «clave», sedes por empresa + nombre, personal por usuario, actas por número).
Nunca borra datos ni cambia contraseñas.

    python manage.py importar_maestros "datos_iniciales/Datos maestros JANOTEC.xlsx"
    python manage.py importar_maestros archivo.xlsx --simular   # muestra qué haría, sin guardar
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.models import ActaSoporte, EquipoActa

RESULTADOS = {
    "solucionado": ActaSoporte.Resultado.SOLUCIONADO,
    "solucionado parcialmente": ActaSoporte.Resultado.PARCIAL,
    "recomendaciones": ActaSoporte.Resultado.RECOMENDACIONES,
}


class Simulacion(Exception):
    """Se lanza al final de --simular para deshacer todo."""


def si(valor):
    return str(valor or "").strip().upper() in ("SI", "SÍ", "TRUE", "1", "X")


def texto(valor, largo=None):
    t = "" if valor is None else str(valor).strip()
    return t[:largo] if largo else t


def decimal(valor):
    try:
        return Decimal(str(valor).strip()) if str(valor or "").strip() else None
    except InvalidOperation:
        return None


class Command(BaseCommand):
    help = "Importa los datos maestros (empresas, sedes, personal, actas) desde el Excel consolidado."

    def add_arguments(self, parser):
        parser.add_argument("archivo", help="Ruta del Excel «Datos maestros JANOTEC.xlsx»")
        parser.add_argument("--simular", action="store_true", help="Muestra lo que haría sin guardar nada")

    def handle(self, archivo, simular=False, **opts):
        try:
            import openpyxl
        except ImportError as e:
            raise CommandError("Falta openpyxl: pip install -r requirements.txt") from e
        try:
            libro = openpyxl.load_workbook(archivo, read_only=True, data_only=True)
        except FileNotFoundError as e:
            raise CommandError(f"No existe el archivo: {archivo}") from e
        for hoja in ("Empresas", "Sedes", "Personal", "Actas", "Equipos en actas"):
            if hoja not in libro.sheetnames:
                raise CommandError(f"Al Excel le falta la hoja «{hoja}».")

        def filas(nombre):
            it = libro[nombre].iter_rows(values_only=True)
            cab = [texto(c).lower() for c in next(it)]
            for f in it:
                if any(v not in (None, "") for v in f):
                    yield dict(zip(cab, f))

        self.conteo = {}
        try:
            with transaction.atomic():
                empresas = self._empresas(filas("Empresas"))
                sedes = self._sedes(filas("Sedes"), empresas)
                personal = self._personal(filas("Personal"))
                actas = self._actas(filas("Actas"), empresas, sedes, personal)
                self._equipos(filas("Equipos en actas"), actas)
                if simular:
                    raise Simulacion
        except Simulacion:
            self.stdout.write(self.style.WARNING("SIMULACIÓN: no se guardó nada."))

        for clave, (creados, actualizados) in self.conteo.items():
            self.stdout.write(f"  {clave:<10} creados: {creados:>5}   actualizados: {actualizados:>5}")
        if not simular:
            self.stdout.write(self.style.SUCCESS("Datos maestros importados."))

    # ------------------------------------------------------------------
    def _contar(self, clave, creado):
        c, a = self.conteo.get(clave, (0, 0))
        self.conteo[clave] = (c + 1, a) if creado else (c, a + 1)

    def _empresas(self, filas):
        mapa = {}
        for f in filas:
            codigo = texto(f.get("clave"), 40)
            if not codigo:
                continue
            datos = {
                "razon_social": texto(f.get("razón social"), 200),
                "nit": texto(f.get("nit"), 30) or None,
                "ciudad": texto(f.get("ciudad"), 100),
                "sector": Empresa.Sector.PUBLICO if "blic" in texto(f.get("sector")).lower() else Empresa.Sector.PRIVADO,
                "activa": si(f.get("activa")),
            }
            empresa = Empresa.objects.filter(codigo=codigo).first()
            if empresa is None and datos["nit"]:
                empresa = Empresa.objects.filter(nit=datos["nit"]).first()  # ya existía, creada a mano
            creado = empresa is None
            if creado:
                empresa = Empresa(codigo=codigo)
            empresa.codigo = codigo
            for k, v in datos.items():
                setattr(empresa, k, v)
            empresa.save()
            mapa[codigo] = empresa
            self._contar("empresas", creado)
        return mapa

    def _sedes(self, filas, empresas):
        mapa = {}
        for f in filas:
            empresa = empresas.get(texto(f.get("clave empresa")))
            nombre = texto(f.get("sede"), 150)
            if not empresa or not nombre:
                continue
            sede, creado = Sede.objects.update_or_create(
                empresa=empresa, nombre=nombre,
                defaults={"ciudad": texto(f.get("ciudad"), 100), "direccion": texto(f.get("dirección"), 200),
                          "abonado": texto(f.get("abonado alarma"), 20)},
            )
            mapa[(empresa.codigo, nombre)] = sede
            self._contar("sedes", creado)
        return mapa

    def _personal(self, filas):
        mapa = {}
        for f in filas:
            usuario = texto(f.get("usuario"), 150)
            if not usuario:
                continue
            rol_texto = texto(f.get("rol (agente/supervisor)")).lower()
            rol = (Usuario.Rol.DIRECCION if rol_texto.startswith("direc") else
                   Usuario.Rol.SUPERVISOR if rol_texto.startswith("super") else Usuario.Rol.AGENTE)
            u = Usuario.objects.filter(username=usuario).first()
            creado = u is None
            if creado:
                u = Usuario(username=usuario)
                u.set_unusable_password()  # cada persona define la suya con «¿Olvidó su contraseña?»
            u.first_name = texto(f.get("nombres"), 150)
            u.last_name = texto(f.get("apellidos"), 150)
            u.cargo = texto(f.get("cargo"), 100)
            u.email = texto(f.get("correo"), 254) or u.email
            u.rol = rol
            u.is_staff = rol in (Usuario.Rol.SUPERVISOR, Usuario.Rol.DIRECCION)
            u.is_active = si(f.get("activo"))
            u.save()
            mapa[usuario] = u
            self._contar("personal", creado)
        return mapa

    def _actas(self, filas, empresas, sedes, personal):
        mapa = {}
        zona = timezone.get_current_timezone()
        for f in filas:
            try:
                numero = int(str(f.get("n° acta")).strip())
            except (TypeError, ValueError):
                continue
            empresa = empresas.get(texto(f.get("clave empresa")))
            if not empresa:
                continue
            fecha = f.get("fecha")
            if not isinstance(fecha, datetime):
                try:
                    fecha = datetime.fromisoformat(texto(fecha))
                except ValueError:
                    continue
            if timezone.is_naive(fecha):
                fecha = timezone.make_aware(fecha, zona)
            enlace = texto(f.get("enlace imágenes"), 300)
            acta, creado = ActaSoporte.objects.update_or_create(
                numero=numero,
                defaults={
                    "fecha": fecha, "empresa": empresa,
                    "sede": sedes.get((empresa.codigo, texto(f.get("sede (listado)"), 150))),
                    "sede_texto": texto(f.get("sede escrita"), 200), "ciudad": texto(f.get("ciudad"), 100),
                    "tecnico": personal.get(texto(f.get("técnico"))),
                    "tecnico_texto": texto(f.get("técnico escrito") or f.get("técnico"), 150),
                    "actividad": texto(f.get("actividad"), 100), "sistema": texto(f.get("sistema"), 60),
                    "diagnostico": texto(f.get("diagnóstico")),
                    "resultado": RESULTADOS.get(texto(f.get("resultado")).lower(), ""),
                    "observaciones": texto(f.get("observaciones")),
                    "recibio": texto(f.get("recibió (cliente)"), 150), "cargo_recibio": texto(f.get("cargo"), 150),
                    "latitud": decimal(f.get("latitud")), "longitud": decimal(f.get("longitud")),
                    "enlace_imagenes": enlace if enlace.startswith("http") else "",
                },
            )
            mapa[numero] = acta
            self._contar("actas", creado)
        return mapa

    def _equipos(self, filas, actas):
        por_acta = {}
        for f in filas:
            try:
                numero = int(str(f.get("n° acta")).strip())
            except (TypeError, ValueError):
                continue
            if numero in actas:
                por_acta.setdefault(numero, []).append(f)
        # Los equipos de cada acta se reemplazan completos: así una corrección en el Excel no duplica filas
        EquipoActa.objects.filter(acta__numero__in=por_acta.keys()).delete()
        nuevos = [
            EquipoActa(acta=actas[n], equipo=texto(f.get("equipo"), 150), marca=texto(f.get("marca"), 100),
                       serial=texto(f.get("serial"), 100), cantidad=texto(f.get("cantidad"), 20),
                       observaciones=texto(f.get("observaciones")))
            for n, lista in por_acta.items() for f in lista
        ]
        EquipoActa.objects.bulk_create(nuevos)
        self.conteo["equipos"] = (len(nuevos), 0)

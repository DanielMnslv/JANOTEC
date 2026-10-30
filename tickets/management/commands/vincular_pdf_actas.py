"""Vincula a cada acta de la plataforma su PDF original, tomado del comprimido de la carpeta de clientes.

El número del acta va en el nombre del archivo: «30268 GASES DEL LLANO.pdf», «ACTA SOPORTE-23198 - CAÑOS NEGROS.pdf».
Por defecto solo muestra lo que haría. Con --aplicar copia los PDF.
Las actas en que el cliente del archivo no se parece al del acta quedan por fuera (se listan para revisarlas),
salvo que se pasen sus números en --incluir.

    python manage.py vincular_pdf_actas "C:\\ruta\\3. CLIENTES.zip"
    python manage.py vincular_pdf_actas "C:\\ruta\\3. CLIENTES.zip" --aplicar
"""
import re
import unicodedata
import zipfile

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from tickets.models import ActaSoporte

PALABRAS_VACIAS = {"SEDE", "ACTA", "ACTAS", "SOPORTE", "TECNICO", "PRIVADO", "PUBLICO", "CLIENTES", "EMPRESA", "SERVICIOS",
                   "PUBLICOS", "2024", "2025", "2026", "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
                   "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE", "CORRESPONDENCIA", "ENVIADA", "VILLAVICENCIO"}
SINONIMOS = {"MOCREDITO": "MOTOCREDITO", "CUSIANAGAS": "CUSIANA", "INCOEQUIPOS": "INCODEQUIPOS", "ALCALDIA": "MUNICIPIO"}


def palabras(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper().replace("MOTO CREDITO", "MOTOCREDITO").replace("JANO TEC", "JANOTEC")
    return {SINONIMOS.get(w, w) for w in re.findall(r"[A-Z0-9]{4,}", t)} - PALABRAS_VACIAS


def numero_de(nombre):
    m = re.match(r"^\s*(\d{5})\b", nombre) or re.search(r"ACTA[^0-9]{0,30}?(\d{5})\b", nombre, re.I)
    return int(m.group(1)) if m else None


class Command(BaseCommand):
    help = "Vincula el PDF original a las actas, desde el comprimido de la carpeta de clientes (por número de acta)."

    def add_arguments(self, parser):
        parser.add_argument("comprimido")
        parser.add_argument("--aplicar", action="store_true", help="Copiar los PDF (sin esto solo muestra lo que haría).")
        parser.add_argument("--incluir", nargs="*", type=int, default=[], help="Números dudosos que sí se vinculan.")
        parser.add_argument("--reemplazar", action="store_true", help="Reemplazar el PDF de las actas que ya tienen uno.")

    def handle(self, comprimido, aplicar, incluir, reemplazar, **kw):
        z = zipfile.ZipFile(comprimido)
        archivos = {}
        for i in z.infolist():
            nombre = i.filename.split("/")[-1]
            ruta = i.filename.upper()
            if not nombre.lower().endswith(".pdf") or "ACTA" not in ruta or "ENTREGA" in ruta:
                continue
            n = numero_de(nombre)
            if n:
                archivos.setdefault(n, []).append(i)
        actas = {a.numero: a for a in ActaSoporte.objects.filter(numero__in=archivos).select_related("empresa", "sede")}
        listas, dudosas, ya = [], [], 0
        for n, opciones in sorted(archivos.items()):
            a = actas.get(n)
            if a is None:
                continue
            if a.pdf_original and not reemplazar:
                ya += 1
                continue
            # Preferir la copia de «05. Acta de Soporte»; si no, la más pesada (la escaneada completa)
            elegido = max(opciones, key=lambda i: ("05. ACTA DE SOPORTE" in i.filename.upper(), i.file_size))
            rutas = " ".join(i.filename for i in opciones)
            coincide = palabras(f"{a.empresa} {a.lugar} {a.sede_texto}") & palabras(rutas)
            if not coincide and n not in incluir:
                dudosas.append((a, elegido))
                continue
            listas.append((a, elegido))
        sin_acta = sorted(set(archivos) - set(actas))
        self.stdout.write(f"PDF de actas en el comprimido: {len(archivos)} números")
        self.stdout.write(f"Se vinculan: {len(listas)} ({round(sum(i.file_size for _, i in listas) / 1e6)} MB)"
                          + (f" · ya tenían PDF: {ya}" if ya else ""))
        self.stdout.write(f"Sin acta en la plataforma (no se tocan): {len(sin_acta)} {sin_acta[:20]}")
        self.stdout.write(f"Dudosas, el cliente no coincide (quedan por fuera): {len(dudosas)}")
        for a, i in dudosas:
            self.stdout.write(f"   {a.numero} · plataforma: {a.empresa} / {a.lugar} · archivo: {i.filename.split('/', 1)[-1][-80:]}")
        if not aplicar:
            self.stdout.write(self.style.WARNING("Solo revisión: no se copió nada. Use --aplicar para vincular."))
            return
        hechas = malas = 0
        for a, i in listas:
            contenido = z.read(i)
            if not contenido.startswith(b"%PDF"):
                malas += 1
                self.stdout.write(f"   {a.numero}: el archivo no es un PDF válido, se omite")
                continue
            a.pdf_original.save(f"acta_{a.numero}.pdf", ContentFile(contenido), save=False)
            a.save(update_fields=["pdf_original"])
            hechas += 1
        self.stdout.write(self.style.SUCCESS(f"Listo: {hechas} actas con su PDF original" + (f" · {malas} no eran PDF" if malas else "")))

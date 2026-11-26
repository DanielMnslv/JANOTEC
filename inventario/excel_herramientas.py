from io import BytesIO

from django.db.models import OuterRef, Subquery
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .herramientas import evidencias_vigentes
from .models import EntregaHerramienta, EvidenciaHerramienta


def herramientas_excel(herramientas, fecha, responsable):
    """Exporta las asignaciones del responsable con columnas editables y filtros."""
    ultima = evidencias_vigentes().filter(herramienta_id=OuterRef("pk")).values("pk")[:1]
    entrega = EntregaHerramienta.objects.filter(
        herramienta_id=OuterRef("pk"), tecnico_id=OuterRef("tecnico_id"), tipo=EntregaHerramienta.Tipo.ENTREGA,
    ).values("fecha")[:1]
    registros = list(herramientas.annotate(evidencia_id=Subquery(ultima), fecha_entrega=Subquery(entrega)))
    evidencias = EvidenciaHerramienta.objects.defer("foto").in_bulk(
        [registro.evidencia_id for registro in registros if registro.evidencia_id],
    )

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Herramientas"
    hoja.append(["Herramientas de trabajo"])
    hoja["A1"].font = Font(bold=True, size=16)
    hoja.append([f"Responsable: {responsable}"])
    hoja.append([f"Corte: {fecha:%d/%m/%Y %H:%M} · {len(registros)} registros · "
                 f"{sum(registro.cantidad for registro in registros)} unidades"])
    for fila in range(1, 4):
        hoja.merge_cells(start_row=fila, start_column=1, end_row=fila, end_column=10)
    hoja.append(["Herramienta", "Marca", "Serial", "Cantidad", "Estado", "Valor total (COP)",
                 "Descripción", "Condición observada", "Fecha de evidencia", "Fecha de entrega"])
    for registro in registros:
        evidencia = evidencias.get(registro.evidencia_id)
        hoja.append([
            registro.nombre, registro.marca, registro.serial, registro.cantidad, registro.get_estado_display(),
            registro.valor, registro.descripcion,
            evidencia.get_condicion_display() if evidencia else "Pendiente de evidencia",
            timezone.localtime(evidencia.fecha).replace(tzinfo=None) if evidencia else None,
            timezone.localtime(registro.fecha_entrega).replace(tzinfo=None) if registro.fecha_entrega else None,
        ])
        fila = hoja[hoja.max_row]
        for celda in fila:
            if isinstance(celda.value, str):
                celda.data_type = "s"
        fila[5].number_format = "#,##0.00"
        fila[6].alignment = Alignment(wrap_text=True)
        fila[8].number_format = "dd/mm/yyyy hh:mm"
        fila[9].number_format = "dd/mm/yyyy hh:mm"
    for celda in hoja[4]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="394052")
    for numero, ancho in enumerate((43, 20, 22, 12, 19, 21, 55, 26, 22, 22), 1):
        hoja.column_dimensions[get_column_letter(numero)].width = ancho
    hoja.auto_filter.ref = f"A4:J{hoja.max_row}"
    hoja.freeze_panes = "B5"

    salida = BytesIO()
    libro.save(salida)
    return salida.getvalue()

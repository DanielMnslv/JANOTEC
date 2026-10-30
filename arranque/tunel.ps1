# Tunel publico de la plataforma JANOTEC (Cloudflare, sin cuenta).
# - Si el tunel se cae o deja de responder, lo reinicia solo.
# - Cada vez que cambia la direccion, actualiza SITIO_URL en .env (los correos llevan el enlace correcto)
#   y la deja escrita en ENLACE_ACTUAL.txt para compartirla.
$Host.UI.RawUI.WindowTitle = "JANOTEC - Tunel publico"
$proyecto = Split-Path -Parent $PSScriptRoot
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$envFile = Join-Path $proyecto ".env"
$settings = Join-Path $proyecto "config\settings.py"
$enlaceTxt = Join-Path $proyecto "ENLACE_ACTUAL.txt"
$utf8 = New-Object System.Text.UTF8Encoding $false

function Escribir($texto, $color = "Gray") { Write-Host "$(Get-Date -Format 'dd/MM HH:mm')  $texto" -ForegroundColor $color }

function Actualizar-Enlace($url) {
    $lineas = [IO.File]::ReadAllLines($envFile, $utf8) | ForEach-Object { if ($_ -like "SITIO_URL=*") { "SITIO_URL=$url" } else { $_ } }
    [IO.File]::WriteAllLines($envFile, [string[]]$lineas, $utf8)
    (Get-Item $settings).LastWriteTime = Get-Date   # el servidor recarga la configuracion con el enlace nuevo
    [IO.File]::WriteAllText($enlaceTxt, "$url`r`n", $utf8)
}

function Responde($url) {
    try { (Invoke-WebRequest "$url/consulta/" -UseBasicParsing -TimeoutSec 20).StatusCode -eq 200 } catch { $false }
}

# Un solo tunel a la vez: cierra los que hayan quedado abiertos
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

while ($true) {
    # Registro nuevo en cada arranque: si el anterior queda bloqueado, nunca se toma un enlace viejo
    Remove-Item (Join-Path $env:TEMP "janotec_tunel*.log") -ErrorAction SilentlyContinue
    $log = Join-Path $env:TEMP "janotec_tunel_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
    Escribir "Abriendo el tunel..."
    $p = Start-Process $cloudflared -ArgumentList "tunnel", "--url", "http://localhost:8000", "--logfile", "`"$log`"" -PassThru -WindowStyle Hidden
    $url = $null
    for ($i = 0; $i -lt 90 -and -not $url -and -not $p.HasExited; $i++) {
        Start-Sleep 1
        if (Test-Path $log) {
            $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1
            if ($m) { $url = $m.Matches[0].Value }
        }
    }
    if ($url) {
        Actualizar-Enlace $url
        Escribir "PLATAFORMA EN LINEA: $url" "Green"
        Escribir "(la direccion tambien queda en ENLACE_ACTUAL.txt; no cierre esta ventana)"
        $fallas = 0
        while (-not $p.HasExited -and $fallas -lt 3) {
            Start-Sleep 60
            if (-not (Responde "http://127.0.0.1:8000")) { continue }   # el servidor esta reiniciando: no es culpa del tunel
            if (Responde $url) { $fallas = 0 } else { $fallas++; Escribir "El enlace no responde ($fallas de 3)" "Yellow" }
        }
    } else {
        Escribir "No se obtuvo la direccion del tunel (revise la conexion a internet)." "Yellow"
    }
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Escribir "El tunel se cayo: se reinicia en 10 segundos..." "Yellow"
    Start-Sleep 10
}

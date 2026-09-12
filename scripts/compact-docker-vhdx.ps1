# Zkomprimuje VHDX Docker Desktopu (D:\Programy\Docker\DockerDesktopWSL\disk\docker_data.vhdx).
# VHDX po smazání dat uvnitř VM sám nezmenší — místo se vrátí až kompakcí.
# SPUSTIT JAKO SPRÁVCE (diskpart to vyžaduje). Docker Desktop musí být vypnutý.
#
#   pwsh -File scripts/compact-docker-vhdx.ps1        (#1127; 12. 9. 2026: 70,9 → 24,9 GB)
#
# Kdy: na alert `disk_low` z docker-cleanup.ps1 nebo po větším úklidu image.
# Trh musí být zavřený — Docker (a tím sběr dat) stojí ~2–5 minut.
[CmdletBinding()]
param(
    # Cesta k datovému disku Docker Desktopu (Settings → Resources → Disk image location)
    [string]$Vhdx = 'D:\Programy\Docker\DockerDesktopWSL\disk\docker_data.vhdx'
)
$vhdx = $Vhdx
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Spusť jako správce (diskpart vyžaduje elevaci).'
}
if (-not (Test-Path $vhdx)) { throw "VHDX nenalezen: $vhdx" }

Write-Host "Zastavuji Docker Desktop a WSL..."
Get-Process 'Docker Desktop', com.docker.backend -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5
wsl --shutdown
Start-Sleep -Seconds 5

$before = (Get-Item $vhdx).Length / 1GB
Write-Host ("VHDX před: {0:N1} GB" -f $before)

$script = @"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
$tmp = Join-Path $env:TEMP 'compact-vhdx.txt'
Set-Content -Path $tmp -Value $script -Encoding ascii
diskpart /s $tmp
Remove-Item $tmp -ErrorAction SilentlyContinue

$after = (Get-Item $vhdx).Length / 1GB
Write-Host ("VHDX po:   {0:N1} GB (uvolněno {1:N1} GB)" -f $after, ($before - $after))

Write-Host "Startuji Docker Desktop..."
Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'

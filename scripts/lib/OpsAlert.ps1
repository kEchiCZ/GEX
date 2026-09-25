# Provozní upozornění MIMO Docker (#1279) — pro skripty, které Docker zastavují
# (kompaktace VHDX): když stack po nich nenaběhne, zvonek aplikace ani push z API
# nejdou poslat, protože API běží v Dockeru. Dva kanály:
#   1) Telegram, když jsou v prostředí nebo v .env repa GEXLENS_PUSH_TELEGRAM_TOKEN
#      a GEXLENS_PUSH_TELEGRAM_CHAT_ID (tytéž klíče jako push z API; ops kategorie
#      neomezují tiché hodiny, api/push_telegram.py). Token se nikdy nevypisuje.
#   2) Okno na ploše přihlášeného uživatele (msg.exe) — vydrží do rána, i když
#      Telegram nastavený není.
# Selhání kanálu skript neshodí; do logu jde jen typ chyby, ne URL s tokenem.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1'); Send-OpsAlert 'Docker nenaběhl…'

function Get-OpsAlertSetting([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value.Trim() }
    $envFile = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding utf8) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") {
            $raw = $matches[1].Trim().Trim('"').Trim("'")
            if ($raw) { return $raw }
        }
    }
    return $null
}

function Send-OpsAlert {
    param([Parameter(Mandatory)][string]$Message)
    Write-Warning $Message  # prefix „UPOZORNĚNÍ:" přidá pwsh sám
    $text = "⚠️ GEXLens`n$Message"

    $token = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_TOKEN'
    $chat = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_CHAT_ID'
    if ($token -and $chat) {
        $body = @{ chat_id = $chat; text = $text; disable_web_page_preview = $true } | ConvertTo-Json -Compress
        $sent = $false
        foreach ($attempt in 1..3) {
            try {
                Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
                    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                    -TimeoutSec 10 | Out-Null
                $sent = $true
                break
            } catch {
                # Výjimka nese URL i s tokenem — do logu jen stavový kód / typ
                $status = $_.Exception.Response.StatusCode.value__
                Write-Warning ("Telegram pokus {0}/3 selhal: {1}" -f $attempt, ($status ? "HTTP $status" : $_.Exception.GetType().Name))
                if ($status -ge 400 -and $status -lt 500) { break }
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
        if ($sent) { Write-Host 'Upozornění odesláno na Telegram.' }
    } else {
        Write-Host 'Telegram není nastavený (GEXLENS_PUSH_TELEGRAM_TOKEN/_CHAT_ID) — upozornění jen na ploše.'
    }

    # Okno na ploše: /TIME v sekundách, 12 h = do rána
    try {
        & "$env:SystemRoot\System32\msg.exe" $env:USERNAME /TIME:43200 "GEXLens: $Message" 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Warning "msg.exe skončil kódem $LASTEXITCODE — okno se nezobrazilo." }
    } catch { Write-Warning "Okno na ploše se nepodařilo zobrazit: $($_.Exception.GetType().Name)" }
}

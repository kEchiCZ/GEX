# Provozní upozornění MIMO Docker (#1279) — pro skripty, které Docker zastavují
# (kompaktace VHDX, rollback deploye): když stack po nich nenaběhne, zvonek
# aplikace ani push z API nejdou poslat, protože API běží v Dockeru. Kanály:
#   1) okno na ploše přihlášeného uživatele (msg.exe, vydrží 12 h) — vždy, první;
#   2) Telegram, když jsou v prostředí nebo v .env repa GEXLENS_PUSH_TELEGRAM_TOKEN
#      a GEXLENS_PUSH_TELEGRAM_CHAT_ID (tytéž klíče jako push z API; ops kategorie
#      neomezují tiché hodiny, api/push_telegram.py).
# Token se nevypisuje ani s -Verbose; do logu jde jen stavový kód / typ chyby.
# Žádná chyba kanálu nevyhodí výjimku ven.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1'); Send-OpsAlert 'Docker nenaběhl…'

function Get-OpsAlertSetting([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value.Trim() }
    $envFile = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) '.env'
    if (-not (Test-Path $envFile)) { return $null }
    # Jako dotenv v Compose: volitelný `export`, uvozovky, komentář za hodnotou
    # bez uvozovek; při duplicitě platí poslední výskyt
    $found = $null
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding utf8) {
        if ($line -match "^\s*(?:export\s+)?$([regex]::Escape($Name))\s*=\s*(.*)$") {
            $raw = $matches[1].Trim()
            if ($raw -match '^"([^"]*)"' -or $raw -match "^'([^']*)'") { $raw = $matches[1] }
            else { $raw = ($raw -replace '\s+#.*$', '').Trim() }
            $found = $raw
        }
    }
    if ($found) { return $found }
    return $null
}

function Send-OpsAlert {
    param([Parameter(Mandatory)][string]$Message)
    Write-Warning $Message  # prefix „UPOZORNĚNÍ:" přidá pwsh sám

    # Okno na ploše první — nezávisí na síti ani konfiguraci; /TIME v s, 12 h = do rána
    try {
        $msgOut = & "$env:SystemRoot\System32\msg.exe" $env:USERNAME /TIME:43200 "GEXLens: $Message" 2>&1
        if ($LASTEXITCODE -ne 0) { Write-Warning "msg.exe skončil kódem ${LASTEXITCODE}: $msgOut" }
    } catch { Write-Warning "Okno na ploše se nepodařilo zobrazit: $($_.Exception.GetType().Name)" }

    try {
        $token = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_TOKEN'
        $chat = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_CHAT_ID'
    } catch {
        Write-Warning "Nastavení Telegramu nejde načíst: $($_.Exception.GetType().Name)"
        return
    }
    if (-not ($token -and $chat)) {
        Write-Host 'Telegram není nastavený (GEXLENS_PUSH_TELEGRAM_TOKEN/_CHAT_ID) — upozornění jen na ploše.'
        return
    }
    $body = @{ chat_id = $chat; text = "⚠️ GEXLens`n$Message"; disable_web_page_preview = $true } | ConvertTo-Json -Compress
    foreach ($attempt in 1..3) {
        try {
            # -Verbose:$false: verbose stream Invoke-RestMethod vypisuje celou URL i s tokenem
            Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
                -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                -TimeoutSec 10 -Verbose:$false -Debug:$false | Out-Null
            Write-Host 'Upozornění odesláno na Telegram.'
            return
        } catch {
            # Výjimka může nést URL s tokenem — do logu jen stavový kód / typ
            $status = if ($_.Exception -is [Microsoft.PowerShell.Commands.HttpResponseException]) { [int]$_.Exception.Response.StatusCode } else { 0 }
            $reason = if ($status) { "HTTP $status" } else { $_.Exception.GetType().Name }
            Write-Warning "Telegram pokus $attempt/3 selhal: $reason"
            if ($status -ge 400 -and $status -lt 500) { return }  # token / chat id — opakovat nemá smysl
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

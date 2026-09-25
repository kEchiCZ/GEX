# Provozní upozornění MIMO Docker (#1279, #1284) — pro skripty, které Docker zastavují
# (kompaktace VHDX, rollback deploye): když stack po nich nenaběhne, zvonek
# aplikace ani push z API nejdou poslat, protože API běží v Dockeru. Kanály:
#   1) výstup skriptu (Write-Warning) → log úlohy v data/logs/ — vždy;
#   2) Telegram, když ho povoluje Nastavení → Notifikace a jsou v prostředí nebo
#      v .env repa GEXLENS_PUSH_TELEGRAM_TOKEN a GEXLENS_PUSH_TELEGRAM_CHAT_ID
#      (tytéž klíče jako push z API).
# Okno na ploše (msg.exe) je od #1284 zrušené (rozhodnutí uživatele 25. 9. 2026):
# vypnutý nebo nenastavený Telegram = upozornění zůstane jen v logu.
#
# Přepínače (#1284): hlavní vypínač a přepínač druhu (-Topic, klíč z PUSH_TOPICS
# v api/src/gexlens_api/push_telegram.py, např. 'maintenance') se čtou z GET
# /push/status — efektivní stav včetně výchozích hodnot a dědění, jediná kopie
# té logiky je v Pythonu. Skript, který Docker zastavuje, si stav načte PŘEDEM
# (Get-OpsAlertPolicy) a předá ho přes -Policy. Když API neodpoví nebo přepínač
# nezná, Telegram se pošle (fail-open): nedostupné API je samo provozní problém.
# Tiché hodiny ani denní strop skript neřeší — přepínače údržby jsou provozní
# (kategorie ops), tichými hodinami by prošly i v API.
# Token se nevypisuje ani s -Verbose; do logu jde jen stavový kód / typ chyby.
# Žádná chyba kanálu nevyhodí výjimku ven.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')
#           $policy = Get-OpsAlertPolicy          # dokud API běží
#           Send-OpsAlert 'Docker nenaběhl…' -Topic 'maintenance' -Policy $policy

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

# Efektivní nastavení Telegramu z API: @{ Enabled = hlavní vypínač; Topics = @{ klíč = bool } }.
# $null = API neodpovědělo nebo vrátilo cizí tvar → volající pošle Telegram (fail-open).
function Get-OpsAlertPolicy {
    param(
        # API produkce na hostu (compose.yml: 127.0.0.1:8010); dev = http://127.0.0.1:8011
        [string]$ApiBase = 'http://127.0.0.1:8010',
        [int]$TimeoutSec = 5
    )
    try {
        $status = Invoke-RestMethod -Uri "$ApiBase/push/status" -TimeoutSec $TimeoutSec -Verbose:$false -Debug:$false
    } catch {
        Write-Warning "Nastavení notifikací z $ApiBase/push/status nejde načíst ($($_.Exception.GetType().Name)) — upozornění půjde na Telegram bez ohledu na přepínače."
        return $null
    }
    if ($status.enabled -isnot [bool] -or $null -eq $status.groups) {
        Write-Warning 'GET /push/status nevrátil hlavní vypínač a přepínače (starší API?) — upozornění půjde na Telegram bez ohledu na přepínače.'
        return $null
    }
    $topics = @{}
    foreach ($group in $status.groups) {
        foreach ($item in $group.topics) { $topics[[string]$item.key] = [bool]$item.enabled }
    }
    [pscustomobject]@{ Enabled = [bool]$status.enabled; Topics = $topics }
}

# Rozhodnutí podle nastavení: Send = poslat na Telegram, Reason = věta do logu
function Resolve-OpsAlertTelegram([string]$Topic, $Policy) {
    $send = $true
    if ($null -eq $Policy) {
        $reason = 'nastavení neznámé (API neodpovědělo) → posílá se (fail-open)'
    } elseif (-not $Policy.Enabled) {
        $send = $false
        $reason = 'vypnuto hlavním vypínačem v Nastavení → Notifikace'
    } elseif (-not $Policy.Topics.ContainsKey($Topic)) {
        $reason = "přepínač '$Topic' API nezná → posílá se (fail-open)"
    } elseif ($Policy.Topics[$Topic]) {
        $reason = "přepínač '$Topic' zapnutý"
    } else {
        $send = $false
        $reason = "přepínač '$Topic' vypnutý v Nastavení → Notifikace"
    }
    [pscustomobject]@{ Send = $send; Reason = "Telegram: $reason" }
}

function Send-OpsAlert {
    param(
        [Parameter(Mandatory)][string]$Message,
        # Přepínač v Settings → Notifikace (klíč z PUSH_TOPICS, test_push_telegram.py
        # kontroluje, že existuje)
        [Parameter(Mandatory)][string]$Topic,
        # Výsledek Get-OpsAlertPolicy načtený předem ($null = API tehdy neodpovědělo);
        # bez parametru si ho funkce načte sama — jen když API ještě běží
        $Policy
    )
    Write-Warning $Message  # prefix „UPOZORNĚNÍ:" přidá pwsh sám; v logu zůstane vždy

    if (-not $PSBoundParameters.ContainsKey('Policy')) { $Policy = Get-OpsAlertPolicy }
    $decision = Resolve-OpsAlertTelegram $Topic $Policy
    Write-Host $decision.Reason
    if (-not $decision.Send) { Write-Host 'Upozornění zůstává jen v logu.'; return }

    try {
        $token = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_TOKEN'
        $chat = Get-OpsAlertSetting 'GEXLENS_PUSH_TELEGRAM_CHAT_ID'
    } catch {
        Write-Warning "Nastavení Telegramu nejde načíst: $($_.Exception.GetType().Name) — upozornění jen v logu."
        return
    }
    if (-not ($token -and $chat)) {
        Write-Host 'Telegram není nastavený (GEXLENS_PUSH_TELEGRAM_TOKEN/_CHAT_ID) — upozornění jen v logu.'
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
            if ($status -ge 400 -and $status -lt 500) { break }  # token / chat id — opakovat nemá smysl
            if ($attempt -lt 3) { Start-Sleep -Seconds (2 * $attempt) }
        }
    }
    Write-Warning 'Upozornění se na Telegram nepodařilo odeslat — zůstává jen v logu.'
}

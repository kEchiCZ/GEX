# Vygeneruje z Markdown manuálů: HTML wiki pro aplikaci (frontend/public/manual)
# a PDF verze (docs/manual/*.pdf). Vyžaduje Node (npx marked) a Microsoft Edge.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$manualDir = Join-Path $root "docs\manual"
$wikiDir = Join-Path $root "frontend\public\manual"
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge nenalezen (potřebný pro PDF export)" }

$css = @"
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; max-width: 860px;
         margin: 0 auto; padding: 24px 32px; color: #1c2430; line-height: 1.55; }
  h1 { border-bottom: 3px solid #14b8a6; padding-bottom: 8px; }
  h2 { border-bottom: 1px solid #d6dde6; padding-bottom: 4px; margin-top: 2em; }
  code { background: #eef1f5; padding: 1px 5px; border-radius: 4px; font-size: 0.92em; }
  pre { background: #f5f7fa; border: 1px solid #d6dde6; border-radius: 8px;
        padding: 12px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  th, td { border: 1px solid #c9d2dd; padding: 6px 10px; text-align: left;
           vertical-align: top; }
  th { background: #eef1f5; }
  img { max-width: 100%; border: 1px solid #c9d2dd; border-radius: 6px; margin: 8px 0; }
  blockquote { border-left: 4px solid #14b8a6; margin: 12px 0; padding: 4px 14px;
               background: #f0faf8; }
  a { color: #0d8f85; }
  @media print { body { max-width: none; } h2 { page-break-after: avoid; } }
</style>
"@

function Build-Manual([string]$mdName, [string]$title, [bool]$toWiki) {
    $md = Join-Path $manualDir $mdName
    $bodyFile = Join-Path $env:TEMP "gexlens-manual-body.html"
    npx --yes marked --gfm -i $md -o $bodyFile
    $body = Get-Content $bodyFile -Raw -Encoding UTF8
    $html = "<!doctype html><html lang=`"cs`"><head><meta charset=`"utf-8`">" +
            "<title>$title</title>$css</head><body>$body</body></html>"

    $htmlPath = Join-Path $manualDir ($mdName -replace "\.md$", ".html")
    [IO.File]::WriteAllText($htmlPath, $html, [Text.UTF8Encoding]::new($false))

    $pdfPath = $htmlPath -replace "\.html$", ".pdf"
    # Cesty s mezerami + stderr Edge: nejspolehlivější je cmd /c s URL-enkódovanou cestou
    $pdfTemp = Join-Path $env:TEMP "gexlens-manual.pdf"
    Remove-Item $pdfTemp -ErrorAction SilentlyContinue
    $url = "file:///" + (($htmlPath -replace "\\", "/") -replace " ", "%20")
    cmd /c "`"$edge`" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf=`"$pdfTemp`" `"$url`" 2>nul" | Out-Null
    if (-not (Test-Path $pdfTemp)) { throw "PDF se nevytvorilo: $pdfPath" }
    Move-Item $pdfTemp $pdfPath -Force
    Write-Host "PDF: $pdfPath"

    if ($toWiki) {
        New-Item -ItemType Directory -Force (Join-Path $wikiDir "img") | Out-Null
        Copy-Item $htmlPath (Join-Path $wikiDir "index.html") -Force
        Copy-Item (Join-Path $manualDir "img\*") (Join-Path $wikiDir "img") -Force
        Write-Host "Wiki: $wikiDir\index.html"
    }
    Remove-Item $htmlPath, $bodyFile -ErrorAction SilentlyContinue
}

Build-Manual -mdName "UZIVATELSKY-MANUAL.md" -title "GEXLens - Uzivatelsky manual" -toWiki $true
Build-Manual -mdName "ADMIN-MANUAL.md" -title "GEXLens - Manual pro spravce a vyvojare" -toWiki $false
Write-Host "Hotovo."

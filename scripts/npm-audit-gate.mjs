// Brána npm audit (CI job `security` a `scripts/security-scan.ps1`, #1837).
//
// Vstup: výstup `npm audit --json` ve frontendu. Blokují advisory high a critical jako
// `npm audit --audit-level=high`, kromě advisory s dočasnou výjimkou do data. Výjimka je jen
// pro advisory bez opravené verze upstream. Po datu neplatí a advisory zase blokuje. Výjimka,
// kterou už žádné advisory nepotřebuje, dá varování: smazat ji.
//
// Použití: node scripts/npm-audit-gate.mjs audit.json [YYYY-MM-DD]
// Návratový kód: 0 = projde, 1 = blokuje (nebo nečitelný výstup), 2 = endpoint auditu
// neodpověděl (volající zkusí znovu).

import { readFileSync } from 'node:fs'

/** Advisory → poslední den výjimky (UTC). Každá s issue, které ji odstraní. */
export const EXCEPTIONS = {
  // picomatch * (high, ReDoS) přes fdir/tinyglobby → vite a vitest, jen dev; opravená verze
  // není, `npm audit fix --force` = downgrade vitest 5 → 3.1.4 (vlastník 18. 11.: B, #1837)
  'GHSA-fc5q-39xc-vjpf': '2026-12-31',
}

const SEVERE = new Set(['high', 'critical'])

/** Vyhodnocení reportu; čistá funkce kvůli testu. */
export function gate(report, today, exceptions = EXCEPTIONS) {
  if (report.error) {
    return { code: 2, lines: [`npm audit endpoint: ${report.error.summary ?? report.error.code}`] }
  }
  if (!report.vulnerabilities || typeof report.vulnerabilities !== 'object') {
    return { code: 1, lines: ['::error::npm audit: výstup nemá `vulnerabilities`'] }
  }
  const advisories = new Map()
  for (const vulnerability of Object.values(report.vulnerabilities)) {
    for (const via of vulnerability.via ?? []) {
      if (via && typeof via === 'object' && typeof via.url === 'string') {
        advisories.set(via.url.split('/').pop(), via)
      }
    }
  }
  const lines = []
  let blocking = 0
  for (const [id, via] of [...advisories].sort(([a], [b]) => a.localeCompare(b))) {
    const until = exceptions[id]
    const severe = SEVERE.has(via.severity)
    const excepted = severe && until !== undefined && today <= until
    const note = excepted ? ` — výjimka do ${until} (#1837)` : ''
    lines.push(`${via.severity}\t${id}\t${via.name}: ${via.title}${note}`)
    if (severe && until !== undefined && today > until) {
      lines.push(`::warning::výjimka npm audit ${id} vypršela ${until} — advisory zase blokuje`)
    }
    if (severe && !excepted) blocking += 1
  }
  for (const id of Object.keys(exceptions)) {
    if (!advisories.has(id)) {
      lines.push(`::warning::výjimka npm audit ${id} už není potřeba — smazat (#1837)`)
    }
  }
  if (blocking > 0) {
    lines.push(`::error::npm audit: ${blocking} advisory high/critical bez výjimky`)
    return { code: 1, lines }
  }
  return { code: 0, lines }
}

if (process.argv[1]?.endsWith('npm-audit-gate.mjs')) {
  const [, , file, today = new Date().toISOString().slice(0, 10)] = process.argv
  let report
  try {
    report = JSON.parse(readFileSync(file, 'utf8'))
  } catch (error) {
    console.log(`::error::npm audit: výstup ${file} není JSON (${error.message})`)
    process.exit(1)
  }
  const { code, lines } = gate(report, today)
  for (const line of lines) console.log(line)
  process.exit(code)
}

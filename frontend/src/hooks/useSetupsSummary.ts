/** Serverový souhrn setupů (#1319): celá historie, UI jen vykresluje.

Výsledek je svázaný se symboly dotazu — po přepnutí symbolu se nikdy neukáže
souhrn jiného instrumentu, jen „načítá se" do odpovědi. Změna voleb nad TÝMIŽ
symboly (mechanika, simulace) drží poslední souhrn s `stale: true`, dokud
nedorazí nový: přepínač i bloky zůstanou v DOM (fokus, žádný skok stránky)
a UI je jen ztlumí. `refreshKey` vynutí nové načtení (WS setups.*, nová
seance), `pollMs` periodické. */
import { useEffect, useState } from 'react'
import { fetchSetupsSummary } from '../api/setups'
import type { SetupsSummary } from '../api/setups'

export interface SummaryState {
  summary: SetupsSummary | null
  /** true = server souhrn nedodal (API/DB) — ukázat chybu, nic nedopočítávat. */
  failed: boolean
  /** true = souhrn patří předchozím volbám týchž symbolů, nový se načítá. */
  stale: boolean
}

export function useSetupsSummary(
  symbols: string[],
  options: {
    allVersions?: boolean
    simulation?: { accountUsd: number; riskPct: number }
    refreshKey?: unknown
    pollMs?: number
  } = {},
): SummaryState {
  const symbolsKey = symbols.join(',')
  const allVersions = options.allVersions ?? false
  const accountUsd = options.simulation?.accountUsd ?? 0
  const riskPct = options.simulation?.riskPct ?? 0
  const key = `${symbolsKey}|${allVersions}|${accountUsd}|${riskPct}`
  const [state, setState] = useState<{
    key: string
    symbolsKey: string
    summary: SetupsSummary | null
    failed: boolean
  }>({ key: '', symbolsKey: '', summary: null, failed: false })
  const { refreshKey, pollMs } = options

  useEffect(() => {
    if (symbolsKey === '') return
    let cancelled = false
    const load = () => {
      void fetchSetupsSummary(symbolsKey.split(','), {
        allVersions,
        simulation: accountUsd > 0 && riskPct > 0 ? { accountUsd, riskPct } : undefined,
      }).then((summary) => {
        if (!cancelled) setState({ key, symbolsKey, summary, failed: summary === null })
      })
    }
    load()
    const timer = pollMs ? window.setInterval(load, pollMs) : undefined
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [key, symbolsKey, allVersions, accountUsd, riskPct, refreshKey, pollMs])

  if (state.key === key) return { summary: state.summary, failed: state.failed, stale: false }
  // Tytéž symboly, jiné volby: poslední známý stav ztlumeně do odpovědi
  if (state.symbolsKey === symbolsKey) {
    return { summary: state.summary, failed: state.failed, stale: true }
  }
  return { summary: null, failed: false, stale: false }
}

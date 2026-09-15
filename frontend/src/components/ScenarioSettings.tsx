/** Settings → Scénáře (#1173): obsazení disku snímky + ruční úklid.
Nad 1 GB přijde alert do zvonku; mazání dělá jen uživatel — řádky s výsledky
zůstávají, mizí jen PNG. */
import { useEffect, useState } from 'react'
import { cleanupScenarioImages, fetchScenarioDisk } from '../api/scenarios'
import type { ScenarioDisk } from '../api/scenarios'

function fmtBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`
  return `${Math.round(bytes / 1e3)} kB`
}

export function ScenarioSettings() {
  const [disk, setDisk] = useState<ScenarioDisk | null>(null)
  const [days, setDays] = useState(30)
  const [message, setMessage] = useState<string | null>(null)
  const reload = () => {
    void fetchScenarioDisk().then(setDisk)
  }
  useEffect(() => {
    let cancelled = false
    void fetchScenarioDisk().then((result) => {
      if (!cancelled) setDisk(result)
    })
    return () => {
      cancelled = true
    }
  }, [])
  return (
    <section aria-label="Scénáře">
      <h2>Scénáře dne</h2>
      <p className="muted" data-testid="scenario-disk">
        {disk === null
          ? 'Obsazení disku se načítá…'
          : `Snímky: ${disk.images} souborů, ${fmtBytes(disk.bytes)} z ${fmtBytes(disk.limit_bytes)}` +
            (disk.over_limit ? ' — NAD LIMITEM, pročisti' : '') +
            ` · scénářů celkem ${disk.scenarios}`}
      </p>
      <label>
        Smazat snímky starší než (dní)
        <input
          type="number"
          min={1}
          max={3650}
          value={days}
          aria-label="Stáří snímků k úklidu (dní)"
          onChange={(event) => setDays(Math.max(1, Number(event.target.value) || 1))}
        />
      </label>
      <button
        type="button"
        className="chip"
        onClick={() => {
          // Nevratné mazání souborů — potvrzení; řádky s výsledky zůstávají
          if (!window.confirm(`Smazat snímky scénářů starší než ${days} dní? Výsledky zůstanou.`))
            return
          void cleanupScenarioImages(days).then((result) => {
            setMessage(
              result
                ? `Smazáno ${result.removed} snímků, uvolněno ${fmtBytes(result.freed_bytes)}`
                : 'Úklid selhal',
            )
            reload()
          })
        }}
      >
        Pročistit snímky
      </button>
      {message && <p className="muted">{message}</p>}
    </section>
  )
}

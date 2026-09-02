/** Banner rekonstruovaného úseku (#617) — zavíratelný s pamětí (#977).

Doplněná minuta nese cenu a objem, ale ŽÁDNÝ tok — CumΔ z ní spočítat nejde.
Kdyby splynula s měřenou, uživatel by četl díru jako naměřený klid, proto se
rekonstrukce hlásí. Ale banner překrývá graf, takže musí jít zavřít — a po
refreshi se pro TUTÉŽ rekonstrukci neukazovat znovu. Zavření se persistuje
jako otisk množiny doplněných minut (ADR-0007): změní-li se (jiný den, další
doplněné minuty), banner se znovu ukáže — nová informace se nesmí ztratit.
*/
import { minuteLabel } from '../replay/useDayData'
import { usePersistentState } from '../state/persist'
import type { Revive } from '../state/persist'

/** Otisk rekonstrukce: symbol, den, počet a krajní minuty. Stačí na to, aby
se jiná množina doplněných minut poznala; celý seznam by zbytečně bobtnal. */
function reconstructionFingerprint(
  symbol: string,
  date: string,
  reconstructedIso: string[],
): string {
  if (reconstructedIso.length === 0) return ''
  const first = reconstructedIso[0]
  const last = reconstructedIso[reconstructedIso.length - 1]
  return `${symbol}|${date}|${reconstructedIso.length}|${first}|${last}`
}

/** Reviver otisku: jen krátký řetězec, cokoli jiného spadne na „nezavřeno". */
const fingerprintRevive: Revive<string> = (value, fallback) =>
  typeof value === 'string' && value.length <= 200 ? value : fallback

export function ReconstructedBanner({
  symbol,
  date,
  reconstructedIso,
}: {
  symbol: string
  date: string
  /** Seřazené ISO časy doplněných minut (`DayData.reconstructedIso`). */
  reconstructedIso: string[]
}) {
  const fingerprint = reconstructionFingerprint(symbol, date, reconstructedIso)
  const [dismissed, setDismissed] = usePersistentState<string>(
    'reconstructedBannerDismissed',
    '',
    fingerprintRevive,
  )
  if (fingerprint === '' || dismissed === fingerprint) return null
  const first = reconstructedIso[0]
  const last = reconstructedIso[reconstructedIso.length - 1]
  return (
    <div
      className="stale-banner stale-banner-closable"
      role="status"
      data-testid="reconstructed-banner"
    >
      <span>
        {`Rekonstruováno ${reconstructedIso.length} min z dxFeed ` +
          `(${minuteLabel(first)}–${minuteLabel(last)}) ` +
          '— cena a objem doplněné po pozdním startu, CumΔ za ten úsek chybí.'}
      </span>
      <button
        type="button"
        className="stale-banner-close"
        aria-label="Zavřít upozornění na rekonstrukci"
        title="Zavřít — znovu se ukáže jen při nové rekonstrukci"
        onClick={() => setDismissed(fingerprint)}
      >
        ×
      </button>
    </div>
  )
}

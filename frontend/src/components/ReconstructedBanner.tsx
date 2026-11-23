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

/** Druh úseku z tastytrade: rekonstrukce svíček po startu (#617), nebo bar
z tisků za výpadku barů IBKR (#1772). Každý má vlastní text a paměť zavření. */
export type TastyBarsKind = 'reconstructed' | 'tasty'

const KIND_STORAGE: Record<TastyBarsKind, string> = {
  reconstructed: 'reconstructedBannerDismissed',
  tasty: 'tastyBarsBannerDismissed',
}

function bannerText(kind: TastyBarsKind, count: number, range: string): string {
  if (kind === 'tasty') {
    return (
      `Z tastytrade ${count} min (${range}) — real-time bary IBKR nechodily, ` +
      'svíčky, setupy i paper šly z tisků front future (záložní zdroj).'
    )
  }
  return (
    `Rekonstruováno ${count} min z dxFeed (${range}) ` +
    '— cena a objem doplněné po pozdním startu, CumΔ za ten úsek chybí.'
  )
}

export function ReconstructedBanner({
  symbol,
  date,
  reconstructedIso,
  kind = 'reconstructed',
}: {
  symbol: string
  date: string
  /** Seřazené ISO časy minut úseku (`DayData.reconstructedIso` / `tastyBarsIso`). */
  reconstructedIso: string[]
  kind?: TastyBarsKind
}) {
  const fingerprint = reconstructionFingerprint(symbol, date, reconstructedIso)
  const [dismissed, setDismissed] = usePersistentState<string>(
    KIND_STORAGE[kind],
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
      data-testid={kind === 'tasty' ? 'tasty-bars-banner' : 'reconstructed-banner'}
    >
      <span>
        {bannerText(kind, reconstructedIso.length, `${minuteLabel(first)}–${minuteLabel(last)}`)}
      </span>
      <button
        type="button"
        className="stale-banner-close"
        aria-label={
          kind === 'tasty'
            ? 'Zavřít upozornění na bary z tastytrade'
            : 'Zavřít upozornění na rekonstrukci'
        }
        title="Zavřít — znovu se ukáže jen při novém úseku"
        onClick={() => setDismissed(fingerprint)}
      >
        ×
      </button>
    </div>
  )
}

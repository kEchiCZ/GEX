/** Karta „Ranní checklist“ v Briefingu (#1241): šest bodů z `morningChecklist`
— útes gammy minulé seance, vyšší TF trend, nejbližší zeď s dominancí,
gap vůči PDC a flipu, tendence/Max Pain podle času do expirace, riziko.
Poučení z 21. 9. 2026: všechno bylo v datech, jen roztroušené po panelech. */
import type { CheckItem, CheckStatus } from '../instrument/morningcheck'

const GLYPH: Record<CheckStatus, string> = { go: '▲', watch: '●', calm: '○', na: '—' }
const TITLE: Record<CheckStatus, string> = {
  go: 'signál pro dnešek',
  watch: 'sledovat',
  calm: 'v normálu',
  na: 'bez dat',
}

export function MorningChecklistCard({ items }: { items: CheckItem[] }) {
  return (
    <section className="briefing-card briefing-checklist" aria-label="Ranní checklist">
      <h3>☑ Ranní checklist</h3>
      <ol className="checklist">
        {items.map((item) => (
          <li
            key={item.key}
            className={`checklist-item checklist-${item.status}`}
            data-testid={`check-${item.key}`}
          >
            <span className="checklist-glyph" title={TITLE[item.status]}>
              {GLYPH[item.status]}
            </span>
            <span className="checklist-body">
              <b>{item.label}:</b> {item.value}
              <span className="muted checklist-action">{item.action}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}

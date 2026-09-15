/** Tlačítko „Vysvětlit" + text (#1126 3d): karta v News i dialog markeru v grafu.

Stav per zpráva žije v komponentě; text přijde z API (cache na serveru navždy),
chyba se ukáže místo textu. Druhé kliknutí text sbalí, další ho znovu rozbalí
bez dalšího volání (server odpoví z cache). */
import { useState } from 'react'
import { explainNews } from '../api/news'

type ExplainState =
  | { state: 'idle'; text?: string }
  | { state: 'loading' }
  | { state: 'done'; text: string }
  | { state: 'error'; error: string }

export function NewsExplain({ eventId, title }: { eventId: number; title: string }) {
  const [explain, setExplain] = useState<ExplainState>({ state: 'idle' })
  const ask = () => {
    if (explain.state === 'loading') return
    if (explain.state === 'done') {
      setExplain({ state: 'idle', text: explain.text }) // sbalit, text si pamatujeme
      return
    }
    if (explain.state === 'idle' && explain.text) {
      setExplain({ state: 'done', text: explain.text })
      return
    }
    setExplain({ state: 'loading' })
    void explainNews(eventId).then((result) =>
      setExplain(
        result.ok
          ? { state: 'done', text: result.explanation.text }
          : { state: 'error', error: result.error },
      ),
    )
  }
  return (
    <div className="news-explain-row">
      <button
        type="button"
        className={
          explain.state === 'done' ? 'chip active news-explain-button' : 'chip news-explain-button'
        }
        aria-label={`Vysvětlit zprávu: ${title}`}
        title="Co ta zpráva je, kdo za ní stojí a proč může hýbat ES/NQ — vysvětlení z Gemini (česky, 2–4 věty). Bez předpovědi směru; do SentIndexu ani signálů neteče. Jednou vysvětlená zpráva se pamatuje."
        disabled={explain.state === 'loading'}
        onClick={ask}
      >
        {explain.state === 'loading' ? 'Vysvětluji…' : 'Vysvětlit'}
      </button>
      {explain.state === 'done' && (
        <p className="news-explain-text" data-testid={`news-explain-${eventId}`}>
          {explain.text}
        </p>
      )}
      {explain.state === 'error' && (
        <p className="muted news-explain-text" data-testid={`news-explain-${eventId}`}>
          Vysvětlení není k dispozici: {explain.error}
        </p>
      )}
    </div>
  )
}

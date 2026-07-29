/** Legenda grafu (#346): ukázka každého prvku + co znamená a jak na něj cena reaguje.

Obsah je **data**, ne JSX — ukázky se kreslí generickým `LegendSwatch` ze
stejných barevných konstant, jaké používá graf (`LEVEL_COLORS`, `SETUP_COLORS`,
`callColor`/`putColor`). Legenda se tak nemůže rozejít s tím, co uživatel
opravdu vidí; kdyby byly barvy opsané, tichý rozchod by nikdo neodhalil.

Vysvětlení záměrně říká i **jak na úroveň cena reaguje** — samotný název
(„Gamma Flip") traderovi nepomůže, pokud neví, co od něj čekat.
*/
import { useEffect } from 'react'
import { LEVEL_COLORS, SECONDARY_WALL_DASH, SETUP_COLORS } from '../heatmap/overlays'
import { callColor, putColor } from '../heatmap/color'

const UP_COLOR = '#3ecf8e'
const DOWN_COLOR = '#f0616d'

function rgba(color: [number, number, number, number]): string {
  return `rgba(${color[0]},${color[1]},${color[2]},${(color[3] / 255).toFixed(2)})`
}

/** Jak se ukázka nakreslí. Odpovídá tomu, co kreslí canvas heatmapy. */
export type Swatch =
  /** Vodorovná čára úrovně. */
  | { kind: 'line'; color: string; dash?: number[]; width?: number; opacity?: number }
  /** Svislá čára (news marker, seance). */
  | { kind: 'vline'; color: string; width?: number; dash?: number[] }
  /** Barevný přechod buněk heatmapy od nuly k maximu. */
  | { kind: 'ramp'; to: string }
  /** Divergentní rampa signed vrstev (záporné ← 0 → kladné). */
  | { kind: 'diverging' }
  /** Svíčky ceny. */
  | { kind: 'candles' }
  /** Plocha panelu nad/pod nulou. */
  | { kind: 'area'; positive: string; negative: string }
  /** Sloupce panelu. */
  | { kind: 'bars'; color: string; second?: string }

export interface LegendItem {
  name: string
  swatch: Swatch
  /** Co to je. */
  what: string
  /** Jak na to cena reaguje / jak to číst. Prázdné u čistě popisných prvků. */
  how?: string
}

export interface LegendSection {
  title: string
  note?: string
  items: LegendItem[]
}

export const LEGEND_SECTIONS: LegendSection[] = [
  {
    title: 'Úrovně v hlavním grafu',
    note: 'Vodorovné čáry přes celou šířku. Popisek nad čarou nese název a aktuální cenu, u zdí i jejich dominanci v procentech.',
    items: [
      {
        name: 'Max Pain',
        swatch: { kind: 'line', color: LEVEL_COLORS.max_pain },
        what: 'Strike, na kterém by při expiraci vypršelo bez hodnoty nejvíc otevřených opcí — tedy cena, kde drtivá většina držitelů opcí prodělá nejvíc.',
        how: 'Čím blíž expiraci, tím silněji k němu bývá cena tažena (pinning). V poslední hodině obchodování se kolem něj často „zasekne“. Není to předpověď, spíš gravitace — proti silné zprávě neobstojí.',
      },
      {
        name: 'Gamma Flip',
        swatch: { kind: 'line', color: LEVEL_COLORS.flip, dash: [6, 5] },
        what: 'Úroveň, kde kumulativní NetGEX mění znaménko — hranice mezi kladnou a zápornou gamma dealerů.',
        how: 'NAD ní jsou dealeři v kladné gamma a pohyb tlumí: prodávají do růstu, kupují do poklesu, takže trh má sklon k návratu k průměru a menším rozpětím. POD ní je gamma záporná a dealeři pohyb zesilují — trendy jedou dál, volatilita roste, propady zrychlují. Průraz této úrovně mění charakter dne.',
      },
      {
        name: 'Call zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.call_wall },
        what: 'Strike nad spotem s největší koncentrací call gamma. Popisek nese i dominanci — jak velký podíl síly call strany zeď drží.',
        how: 'Působí jako odpor: při přiblížení dealeři prodávají a růst brzdí. Když ji cena prorazí a udrží se nad ní, hedging se obrátí a pohyb se často zrychlí (gamma squeeze).',
      },
      {
        name: 'Put zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.put_wall },
        what: 'Strike pod spotem s největší koncentrací put gamma.',
        how: 'Působí jako podpora — poklesy se u ní obvykle zastaví. Průraz dolů ale bere trhu záchytný bod a otevírá prostor k rychlému pádu k další úrovni.',
      },
      {
        name: '2. call zeď / 2. put zeď',
        swatch: {
          kind: 'line',
          color: LEVEL_COLORS.call_wall_2,
          dash: [...SECONDARY_WALL_DASH],
        },
        what: 'Druhá nejsilnější zeď na dané straně. Kreslí se tečkovaně a poloprůhledně, ať ji nespleteš s primární.',
        how: 'Ukazuje, kam se pozicing přesune, když primární zeď padne — tedy nejbližší další místo, kde pohyb nejspíš narazí. Zobrazí se jen když existuje; některé dny na jedné straně žádná druhá zeď není.',
      },
      {
        name: 'Těžiště',
        swatch: { kind: 'line', color: LEVEL_COLORS.centroid, dash: [6, 5] },
        what: 'Vážený střed opčního pozicingu — kde leží masa otevřených kontraktů.',
        how: 'Orientační bod, kolem kterého se pozicing rozkládá. Cena daleko od těžiště znamená, že se trh vzdálil od hlavní koncentrace pozic.',
      },
      {
        name: 'Slabá zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.call_wall, dash: [2, 3], opacity: 0.4 },
        what: 'Úsek zdi, kde dominance klesla pod 15 % — kreslí se ztlumeně a tečkovaně.',
        how: 'Zeď je v tu chvíli roztříštěná mezi víc strikes. Neber ji jako spolehlivou podporu ani odpor.',
      },
    ],
  },
  {
    title: 'Navržený setup',
    note: 'Objeví se, jen když detektor najde příležitost. Ke grafu patří karta s popisem a poměrem rizika.',
    items: [
      {
        name: 'Vstup',
        swatch: { kind: 'line', color: SETUP_COLORS.entry, dash: [6, 5] },
        what: 'Cena, na které setup počítá se vstupem do pozice.',
      },
      {
        name: 'Cíl',
        swatch: { kind: 'line', color: SETUP_COLORS.target, dash: [6, 5] },
        what: 'Cílová cena setupu. Zásah cílem setup uzavírá jako úspěšný.',
      },
      {
        name: 'Stop',
        swatch: { kind: 'line', color: SETUP_COLORS.stop, dash: [6, 5] },
        what: 'Ochranná úroveň. Zásah stopem setup uzavírá se ztrátou — vyhodnocuje se vždy dřív než cíl, aby výsledky nelhaly.',
      },
    ],
  },
  {
    title: 'Heatmapa',
    note: 'Barva buňky = velikost hodnoty na daném striku a v dané minutě. Co se měří, určuje přepínač Mode (OI, Vol, VEX…), jak se to škáluje, přepínač Scale.',
    items: [
      {
        name: 'Call vrstva',
        swatch: { kind: 'ramp', to: rgba(callColor(1)) },
        what: 'Zelenomodrá. Sytost roste s velikostí hodnoty na call straně.',
        how: 'Souvislý sytý pruh přes několik minut je zeď — místo, kde je koncentrovaný pozicing. Náhlé rozsvícení nového striku znamená čerstvý tok.',
      },
      {
        name: 'Put vrstva',
        swatch: { kind: 'ramp', to: rgba(putColor(1)) },
        what: 'Červená. Totéž pro put stranu.',
      },
      {
        name: 'Vrstva ±',
        swatch: { kind: 'diverging' },
        what: 'Módy se znaménkem (Vol ±, OI±All, VEX ±) kreslí převahu: zeleně tam, kde vede call strana, červeně kde put.',
        how: 'Rychle ukáže, která strana na striku dominuje, místo aby se obě sčítaly.',
      },
      {
        name: 'Stará data',
        swatch: { kind: 'ramp', to: 'rgba(150,150,150,0.85)' },
        what: 'Buňka odbarvená do šeda a zprůhledněná — kotace je starší než 5 minut.',
        how: 'Není to prázdno, ale neaktuálnost. Nestav na takovém striku rozhodnutí.',
      },
      {
        name: 'Projekce',
        swatch: { kind: 'ramp', to: 'rgba(20,200,170,0.45)' },
        what: 'Plocha vpravo za svislým předělem „projekce →“. Poslední naměřený sloupec protažený do konce seance, kreslený se sníženou sytostí.',
        how: 'Ukazuje, kde by zdi ležely, kdyby se pozicing dál neměnil. Není to měření ani předpověď — jen prodloužení současného stavu.',
      },
    ],
  },
  {
    title: 'Cena',
    items: [
      {
        name: 'Svíčky',
        swatch: { kind: 'candles' },
        what: 'Minutové OHLC nad heatmapou; zelená roste, červená klesá. Přepínačem Cena lze přepnout na spojitou linku.',
        how: 'Poslední svíčka je rozdělaná a dokresluje se živě, proto se během minuty mění.',
      },
    ],
  },
  {
    title: 'Značky v grafu',
    items: [
      {
        name: 'Zpráva',
        swatch: { kind: 'vline', color: 'rgba(20,184,166,0.95)', width: 2 },
        what: 'Svislá čára v minutě, kdy zpráva vyšla, s ikonou kategorie. Zelenomodrá je pozitivní, červená negativní, šedá neutrální nebo teprve plánovaná.',
        how: 'Jas a tloušťka odpovídají důležitosti — okrajová zpráva je sotva vidět, FOMC křičí. Pohyb těsně po silné značce je reakce na zprávu, ne na pozicing.',
      },
      {
        name: 'Seance',
        swatch: { kind: 'vline', color: 'rgba(125,133,150,0.8)', dash: [4, 4] },
        what: 'Otevření a zavření hlavních trhů (Sydney, Šanghaj, Frankfurt, Londýn, US) s popiskem.',
        how: 'Likvidita a charakter pohybu se na těchto hranicích mění — zejména US open bývá zlom dne.',
      },
    ],
  },
  {
    title: 'Panely pod grafem',
    note: 'Sdílejí časovou osu s hlavním grafem a crosshair — hodnota pod kurzorem se ukazuje vpravo. Které panely jsou vidět, řídí přepínače nad grafem.',
    items: [
      {
        name: 'Vol',
        swatch: { kind: 'bars', color: 'rgba(125,133,150,0.8)' },
        what: 'Zobchodovaný objem futures za minutu.',
        how: 'Pohyb na vysokém objemu má váhu, tentýž pohyb na nízkém je spíš šum.',
      },
      {
        name: 'Opt Vol',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        what: 'Opční volume rozdělené na call (zeleně) a put (červeně).',
        how: 'Skok na jedné straně ukazuje, kam vstupuje čerstvý opční tok — často předchází pohybu zdí.',
      },
      {
        name: 'Δ Flow C/P',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        what: 'Delta-vážený opční tok per strana — objem přepočtený na skutečnou směrovou expozici.',
        how: 'Na rozdíl od holého volume rozliší, jestli tok tlačí trh nahoru, nebo dolů.',
      },
      {
        name: 'Cum Δ',
        swatch: { kind: 'area', positive: UP_COLOR, negative: DOWN_COLOR },
        what: 'Kumulativní delta futures — plocha nad nulou zeleně, pod nulou červeně.',
        how: 'Ukazuje, jestli den táhli agresivní kupci, nebo prodejci. Rozchod s cenou (cena roste, Cum Δ klesá) je varovný signál slábnoucího pohybu.',
      },
      {
        name: 'Sentiment',
        swatch: { kind: 'area', positive: UP_COLOR, negative: DOWN_COLOR },
        what: 'Index nálady ze zpracovaných zpráv, spojitá řada s exponenciálním dozníváním.',
        how: 'Kladné pásmo je risk-on, záporné risk-off. Čti jako kontext k pohybu, ne jako vstupní signál.',
      },
    ],
  },
  {
    title: 'Profil vpravo',
    items: [
      {
        name: 'Vol + OI',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        what: 'Vodorovné pruhy per strike — otevřené kontrakty a objem, call vpravo, put vlevo. Sdílí osu Y s grafem, takže pruh je vždy v úrovni svého striku.',
        how: 'Nejdelší pruhy jsou zdi, které v grafu vidíš jako vodorovné čáry.',
      },
      {
        name: 'GEX křivka',
        swatch: { kind: 'line', color: LEVEL_COLORS.centroid, width: 2 },
        what: 'Modelovaný profil NetGEX přes cenové pásmo, zapnutelný chipem GEX.',
        how: 'Průchod nulou je gamma flip. Vpravo od nuly kladná gamma (tlumení), vlevo záporná (zesilování).',
      },
    ],
  },
]

const SWATCH_W = 62
const SWATCH_H = 18

/** Ukázka prvku — kreslí se stejnými barvami, jaké používá graf. */
export function LegendSwatch({ swatch }: { swatch: Swatch }) {
  const midY = SWATCH_H / 2
  const common = { width: SWATCH_W, height: SWATCH_H, className: 'legend-swatch' }
  if (swatch.kind === 'line') {
    return (
      <svg {...common} role="presentation">
        <line
          x1={2}
          y1={midY}
          x2={SWATCH_W - 2}
          y2={midY}
          stroke={swatch.color}
          strokeWidth={swatch.width ?? 1.5}
          strokeDasharray={swatch.dash?.join(' ')}
          opacity={swatch.opacity ?? 1}
        />
      </svg>
    )
  }
  if (swatch.kind === 'vline') {
    return (
      <svg {...common} role="presentation">
        <line
          x1={SWATCH_W / 2}
          y1={1}
          x2={SWATCH_W / 2}
          y2={SWATCH_H - 1}
          stroke={swatch.color}
          strokeWidth={swatch.width ?? 1.5}
          strokeDasharray={swatch.dash?.join(' ')}
        />
      </svg>
    )
  }
  if (swatch.kind === 'ramp' || swatch.kind === 'diverging') {
    const id = `ramp-${swatch.kind === 'ramp' ? swatch.to : 'div'}`.replace(/[^a-z0-9]/gi, '')
    return (
      <svg {...common} role="presentation">
        <defs>
          <linearGradient id={id} x1="0" x2="1">
            {swatch.kind === 'ramp' ? (
              <>
                <stop offset="0%" stopColor={swatch.to} stopOpacity={0} />
                <stop offset="100%" stopColor={swatch.to} stopOpacity={1} />
              </>
            ) : (
              <>
                <stop offset="0%" stopColor={rgba(putColor(1))} />
                <stop offset="50%" stopColor="rgba(0,0,0,0)" />
                <stop offset="100%" stopColor={rgba(callColor(1))} />
              </>
            )}
          </linearGradient>
        </defs>
        <rect x={1} y={1} width={SWATCH_W - 2} height={SWATCH_H - 2} fill={`url(#${id})`} />
      </svg>
    )
  }
  if (swatch.kind === 'candles') {
    return (
      <svg {...common} role="presentation">
        {[
          { x: 14, color: UP_COLOR, top: 4, bottom: 13 },
          { x: 31, color: DOWN_COLOR, top: 6, bottom: 15 },
          { x: 48, color: UP_COLOR, top: 3, bottom: 11 },
        ].map((candle) => (
          <g key={candle.x} stroke={candle.color} fill={candle.color}>
            <line x1={candle.x} y1={1} x2={candle.x} y2={SWATCH_H - 1} strokeWidth={1} />
            <rect
              x={candle.x - 3}
              y={candle.top}
              width={6}
              height={candle.bottom - candle.top}
              stroke="none"
            />
          </g>
        ))}
      </svg>
    )
  }
  if (swatch.kind === 'area') {
    return (
      <svg {...common} role="presentation">
        <polygon points={`2,${midY} 20,4 34,7 46,${midY}`} fill={swatch.positive} opacity={0.8} />
        <polygon
          points={`46,${midY} 52,13 58,11 ${SWATCH_W - 2},${midY}`}
          fill={swatch.negative}
          opacity={0.8}
        />
        <line x1={2} y1={midY} x2={SWATCH_W - 2} y2={midY} stroke="var(--border)" />
      </svg>
    )
  }
  return (
    <svg {...common} role="presentation">
      {[3, 12, 21, 30, 39, 48].map((x, index) => {
        const isSecond = swatch.second !== undefined && index % 2 === 1
        const height = [10, 6, 14, 8, 12, 5][index]
        return (
          <rect
            key={x}
            x={x}
            y={SWATCH_H - 1 - height}
            width={6}
            height={height}
            fill={isSecond ? swatch.second : swatch.color}
          />
        )
      })}
    </svg>
  )
}

/** Modální legenda. Zavírá se křížkem, klávesou Esc i klikem mimo obsah. */
export function Legend({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="legend-backdrop" onClick={onClose} role="presentation">
      <div
        className="legend-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Legenda grafu"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="legend-header">
          <h2>Legenda grafu</h2>
          <button className="legend-close" onClick={onClose} aria-label="Zavřít legendu">
            ×
          </button>
        </header>
        <div className="legend-body">
          {LEGEND_SECTIONS.map((section) => (
            <section key={section.title} className="legend-section">
              <h3>{section.title}</h3>
              {section.note && <p className="muted legend-note">{section.note}</p>}
              <ul>
                {section.items.map((item) => (
                  <li key={item.name} className="legend-item">
                    <LegendSwatch swatch={item.swatch} />
                    <div className="legend-text">
                      <strong>{item.name}</strong>
                      <p>{item.what}</p>
                      {item.how && <p className="legend-how">{item.how}</p>}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}

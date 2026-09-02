/** Globální stav aplikace: pipeline status z WS, view, téma, alerty, přepínače. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { LiveSocket } from '../api/ws'
import type { Coverage } from '../instrument/coverage'
import { API_BASE, WS_URL } from '../config'
import type { GexRegimeState } from '../instrument/regime'
import type { SettleWatchInfo } from '../instrument/settlewatch'
import { sessionDateIso } from '../instrument/tz'
import { GEX_UNITS } from '../heatmap/units'
import type { GexUnits } from '../heatmap/units'
import { FORWARD_RANGES } from '../heatmap/dailyforward'
import type { ForwardRange } from '../heatmap/dailyforward'
import { clampedNumber, enumMap, mergedBooleans, oneOf, shortString, usePersistentState } from './persist' // prettier-ignore

export interface PipelineStatus {
  engine: string
  connection?: string
  port?: number
  greeks_complete?: number
  greeks_total?: number
  repair_count?: number
  /** OI pokrytí aktivních řetězů (#664): archiv IBKR / tasty fill / bez hodnoty. */
  oi_present?: number
  oi_filled?: number
  oi_missing?: number
  lines_utilization?: number
  /** Chyby subskripce market data (#417, #772): kumulativ od startu, okno
      60 min, omilostněné přechody seance a poslední záznamy k dohledání. */
  subscription_errors?: number
  subscription_errors_60m?: number
  subscription_errors_excused?: number
  subscription_error_recent?: { ts: string; contract: string; symbol: string }[]
  disk_usage_bytes?: number
  disk_limit_bytes?: number
  last_tick_ts?: string
  news_available?: boolean
  /** Připojený IBKR účet (#446) — „DU1234567 (paper)"; paper flag zvlášť pro barvu. */
  account?: string
  account_paper?: boolean | null
  /** Křížová kontrola IBKR × tasty (#517 A). Klíč CHYBÍ, když shadow neběží —
      „neměří se" je jiný stav než `ok` a UI je nesmí splácnout dohromady. */
  feed_crosscheck?: 'ok' | 'ibkr_suspect' | 'tasty_suspect' | 'quiet' | 'insufficient'
  feed_crosscheck_detail?: string
  feed_crosscheck_ibkr_dead_share?: number
  feed_crosscheck_contracts?: number
  /** Zdroj opčního řetězu a ceny podkladu (#614). Klíč CHYBÍ, když ochrana
      neběží — stejná logika jako u křížové kontroly: „nechrání se" není totéž
      co „chrání se a je vše v pořádku". */
  chain_source?: 'ibkr' | 'tasty'
  spot_source?: 'ibkr' | 'tasty' | 'none'
  /** Jak dlouho chybí IBKR spojení v sekundách (#770). Klíč CHYBÍ, když
      spojení drží — nepřítomnost = nic k hlášení. */
  connection_offline_for_s?: number
  /** Stav tastytrade větve (#706). Klíče CHYBÍ, když větev neběží — to je
      jiný stav než „běží a je odpojená". */
  tasty_connected?: boolean
  tasty_reconnects?: number
  tasty_symbols?: number
  tasty_quotes?: number
  tasty_greeks?: number
  tasty_oi?: number
  tasty_trades?: number
  tasty_trades_recorded?: number
  tasty_last_event_ts?: string
  /** Greeks validátor (#614): podíl párů nad měřeným prahem per podklad. */
  tasty_greeks_mismatch?: Record<string, number>
  /** Extended expirace z tasty (#616): per symbol seznam expirací, jejichž
      zdrojem je tastytrade (BS greeks z kotací, bez objemů/flows). */
  tasty_extended_expiries?: Record<string, string[]>
  /** Aktivní ad-hoc pohledy přes tasty (#521 C) — hlavička jimi značí zdroj. */
  tasty_adhoc?: string[]
  updated_at?: number | null
}

export interface AlertMessage {
  kind: string
  symbol: string
  message: string
  ts: number
  /** Setup alerty (#186): 'created' → proklik na graf, 'closed' → na Setupy. */
  event?: string
}

export interface Toggles {
  dynGex: boolean
  /** Sekundární zeď (ADR-0008, #92): dvě rovnocenné koncentrace jako dvě linie. */
  secondaryWall: boolean
  gexLevels: boolean
  /** GEX žebřík (#244): významné striky jako barevné úrovně („parametry"). */
  ladder: boolean
  /** Flow-adjusted levels (ADR-0011, #222): flip/walls z OI odhadu ranní OI + tok. */
  flowAdjusted: boolean
  sessions: boolean
  vol: boolean
  optVol: boolean
  delta: boolean
  deltaFlow: boolean
  /** Panel Evo OI (#573): vývoj celkového call/put OI. */
  evoOi: boolean
  volOiDelta: boolean
  /** Projekce heatmapy do settle (ADR-0006) — jen intraday. */
  projection: boolean
  news: boolean
  /** Vrstva setupů (#399): karta aktivního setupu + entry/cíl/stop linie.
  Řídí jen zobrazení v grafu — detektor i stránka Setupy běží vždy. */
  setups: boolean
}

export type AppView =
  'chart' | 'dashboard' | 'chain' | 'setups' | 'briefing' | 'journal' | 'news' | 'stats' | 'settings' // prettier-ignore
export type Theme = 'dark' | 'light'

/** Režim zobrazení signálů (#295, SPEC 6.1/S9): výpočet běží vždy, tohle řídí jen UI. */
export type SignalMode = 'off' | 'news' | 'combined'
export const SIGNAL_MODES: readonly SignalMode[] = ['off', 'news', 'combined']

/** Podkladová plocha heatmapy (#242 → dropdown #204): jedna aktivní, ne checkboxy. */
export type UnderlayPlane = 'off' | 'gex' | 'charm' | 'vanna'
export const UNDERLAY_PLANES: readonly UnderlayPlane[] = ['off', 'gex', 'charm', 'vanna']

/** Filtr news markerů v grafu (#408): všechny, nebo jen významné (importance ≥ 2). */
export type NewsMarkerFilter = 'all' | 'important'
export const NEWS_MARKER_FILTERS: readonly NewsMarkerFilter[] = ['all', 'important']

/** Zdroj OI pro heatmapu a Dyn GEX (#232 fáze 2): měřený ranní archiv, nebo
flow-adjusted odhad OI_est = OI + α·net (ADR-0011). Default VŽDY měřené —
FA je opt-in a UI ho vždy značí jako odhad. Persistuje se per symbol. */
export type OiSource = 'measured' | 'fa'
export const OI_SOURCES: readonly OiSource[] = ['measured', 'fa']

/** Poslední cena + denní změna (hlavička; plní MainContent z denních dat). */
export interface PriceInfo {
  last: number | null
  changePct: number | null
}

/** GEX režim badge (#209; plní MainContent z živých levels + Dyn GEX profilu). */
export interface RegimeInfo {
  state: GexRegimeState | null
  measuredFlip: number | null
  dynamicFlip: number | null
  /** Režim odvozen ze znaménka profilu bez flipu (#864) — tooltip to přizná. */
  fromProfileSign?: boolean
}

/** Intraday timeframy — agregace 1m dat do košů (SPEC 7.1, TradingView sada). */
export const INTERVALS = [
  '1m',
  '2m',
  '3m',
  '5m',
  '10m',
  '15m',
  '30m',
  '45m',
  '1h',
  '2h',
  '3h',
  '4h',
  '1d',
] as const
export type Interval = (typeof INTERVALS)[number]

export const INTERVAL_MINUTES: Record<Interval, number> = {
  '1m': 1,
  '2m': 2,
  '3m': 3,
  '5m': 5,
  '10m': 10,
  '15m': 15,
  '30m': 30,
  '45m': 45,
  '1h': 60,
  '2h': 120,
  '3h': 180,
  '4h': 240,
  '1d': 1440,
}

interface AppState {
  status: PipelineStatus
  symbol: string
  /** Přepnutí aktivního tickeru (z watchlistu v sidebaru). */
  setSymbol: (symbol: string) => void
  /** Nedávno zobrazené symboly (#521) — sekce v sidebaru, persistováno. */
  recentSymbols: string[]
  /** Vyřadí symbol z Nedávných (#987) — vyhledaný symbol jinak nešel odebrat. */
  forgetRecentSymbol: (symbol: string) => void
  expiries: string[]
  /** Trading classes per expirace z OI archivu (#513, SPEC 3.2); prázdné pole = neznámé. */
  expiryClasses: Record<string, string[]>
  selectedExpiry: string | null
  setSelectedExpiry: (expiry: string) => void
  /** Doskok na starší seanci, protože pro vybranou expiraci nejsou data (#946).
  Null = ukazuje se to, co uživatel/výchozí volba vybrala. */
  expiryFallback: { requested: string; shown: string } | null
  timeframe: 'intraday' | 'daily'
  setTimeframe: (value: 'intraday' | 'daily') => void
  interval: Interval
  setInterval: (value: Interval) => void
  toggles: Toggles
  setToggle: (key: keyof Toggles, value: boolean) => void
  /** Dropdown OFF/NEWS/COMBINED vedle News checkboxu (#295, SPEC 9.0). */
  signalMode: SignalMode
  setSignalMode: (mode: SignalMode) => void
  /** Dropdown podkladové plochy Off/Dyn GEX/Charm/Vanna (#204). */
  underlayPlane: UnderlayPlane
  setUnderlayPlane: (plane: UnderlayPlane) => void
  /** Rozsah Forward GEX projekce v Daily (#572): settle / +1 den / týden. */
  forwardRange: ForwardRange
  setForwardRange: (range: ForwardRange) => void
  /** Filtr news markerů Vše/Významné vedle News checkboxu (#408). */
  newsMarkerFilter: NewsMarkerFilter
  setNewsMarkerFilter: (filter: NewsMarkerFilter) => void
  /** Jednotka Dyn ploch a GEX křivky (#569): $/1 % (výchozí) váží P²/100, $/bod je surové pole enginu. */
  gexUnits: GexUnits
  setGexUnits: (units: GexUnits) => void
  /** Zdroj OI aktivního symbolu (#232): měřené / FA odhad; persist per symbol. */
  oiSource: OiSource
  setOiSource: (source: OiSource) => void
  view: AppView
  setView: (view: AppView) => void
  /** Rychlý vstup do deníku (#673): předvyplněný okamžik (✎/Shift+klik);
      briefing (#674) předvyplní i text ranního plánu. */
  journalDraft: { tsRef: string; text?: string } | null
  setJournalDraft: (draft: { tsRef: string; text?: string } | null) => void
  /** Traders mode (#627 bod 5): zapíná trading vrstvy — teď značky deníku
      v ose (#673); postupně referenční úrovně (#678). Default vypnuto. */
  tradersMode: boolean
  setTradersMode: (enabled: boolean) => void
  /** Kalkulačka pozice (#679): velikost účtu a riziko na obchod — jen
      v prohlížeči, na server se nikdy neposílá. */
  riskAccountUsd: number
  setRiskAccountUsd: (value: number) => void
  riskPct: number
  setRiskPct: (value: number) => void
  theme: Theme
  setTheme: (theme: Theme) => void
  alerts: AlertMessage[]
  unreadAlerts: number
  markAlertsRead: () => void
  consoleLog: string[]
  priceInfo: PriceInfo
  setPriceInfo: (info: PriceInfo) => void
  regimeInfo: RegimeInfo
  setRegimeInfo: (info: RegimeInfo) => void
  /** Settle watch (#603): klíčová úroveň dne + odstup — plní MainContent, čte hlavička. */
  settleWatch: SettleWatchInfo | null
  setSettleWatch: (info: SettleWatchInfo | null) => void
  /** Pokrytí OHLC barů zobrazeného dne (#470) — hlásí graf, čte hlavička. */
  ohlcCoverage: Coverage | null
  setOhlcCoverage: (coverage: Coverage | null) => void
  /** Verze setupů — WS kanál setups.* ji zvedá, konzumenti přenačítají REST. */
  setupsVersion: number
  /** Sdílený WS klient — živý append intraday grafu (useDayData). */
  socket: LiveSocket
}

const AppStateContext = createContext<AppState | null>(null)

const LOG_LIMIT = 200
const ALERTS_LIMIT = 50

const VIEWS: readonly AppView[] = [
  'chart',
  'dashboard',
  'chain',
  'setups',
  'briefing',
  'journal',
  'news',
  'stats',
  'settings',
]

/** Výchozí expirace: nejbližší expirace ≥ den seance (dnešní 0DTE řetěz tím
dostane přednost sám), a teprve když žádná budoucí není, nejnovější proběhlá.

Původní pravidlo „jinak `at(-1)`" počítalo s tím, že seznam nese jen proběhlé
expirace, kde poslední = nejnovější minulá. Po zapojení tastytrade extended
expirací (#610) sahá `/instruments/{symbol}/expiries` daleko DOPŘEDU, takže
`at(-1)` začalo znamenat „nejvzdálenější budoucí" — měsíční řetěz měsíc dopředu,
pro který se nic nesbírá. O víkendech a svátcích, kdy den seance mezi
expiracemi není, tím aplikace tiše spadla na demo data (#945).

Kotva je den seance (#512), ne kalendářní UTC den: nedělní večer už patří
pondělní ose. `sessionDate` je parametrem kvůli deterministickým testům. */
export function defaultExpiry(
  expiries: string[],
  sessionDate: string = sessionDateIso().replaceAll('-', ''),
): string | null {
  if (expiries.length === 0) return null
  const sorted = [...expiries].sort()
  return sorted.find((expiry) => expiry >= sessionDate) ?? sorted.at(-1) ?? null
}

/** Expirace k otevření: vybraná, nebo — když pro ni nejsou uložená žádná data —
expirace posledního dne, pro který data JSOU (#946).

Mimo obchodování (sobota, neděle do otevření Globexu, svátky) nemá nejbližší
budoucí expirace ještě ani minutu dat, takže by aplikace nabídla demo dataset.
Uživatel v takové chvíli chce vidět poslední odobchodovanou seanci — otevřít
appku v sobotu a nevidět páteček je proti očekávání.

Nedělní večer se řeší sám: po otevření Globexu (17:00 CT) už data pro aktuální
seanci existují, takže se fallback nepoužije a otevře se živý den. Stejně tak
v seanci — tam se tahle větev nikdy neuplatní.

`days` je vzestupný seznam z `/instruments/{symbol}/days`; poslední záznam je
nejnovější den s daty. Prázdný seznam = necháváme vybranou expiraci (engine
teprve začíná sbírat, hláška o tom v grafu nelže). */
export function expiryWithData(
  selected: string | null,
  days: { date: string; expiry: string }[],
): string | null {
  if (selected === null || days.length === 0) return selected
  if (days.some((day) => day.expiry === selected)) return selected
  return days[days.length - 1]?.expiry ?? selected
}

/** Deep-link: počáteční obrazovka a téma z URL (?view=dashboard&theme=light).

Téma z URL má přednost před uloženou volbou (ADR-0007) — automatizované
snímky musí dostat deterministický vzhled. */
function initialFromUrl(): { view: AppView; theme: Theme | null } {
  const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null
  const view = params?.get('view')
  const theme = params?.get('theme')
  return {
    view: VIEWS.includes(view as AppView) ? (view as AppView) : 'chart',
    theme: theme === 'light' ? 'light' : theme === 'dark' ? 'dark' : null,
  }
}

/** Výchozí stav přepínačů (persistuje se jako celek, ADR-0007). */
const DEFAULT_TOGGLES: Toggles = {
  dynGex: true,
  secondaryWall: true,
  gexLevels: true,
  ladder: false,
  flowAdjusted: false,
  sessions: false,
  vol: true,
  optVol: true,
  delta: true,
  deltaFlow: false,
  evoOi: false,
  volOiDelta: true,
  projection: true,
  news: false,
  setups: true,
}

export function AppStateProvider({
  children,
  socket,
  symbol: initialSymbol = 'ES',
}: {
  children: ReactNode
  /** Testovatelnost: injektovaný LiveSocket místo výchozího. */
  socket?: LiveSocket
  symbol?: string
}) {
  const [status, setStatus] = useState<PipelineStatus>({ engine: 'offline' })
  // Jediná stabilní instance WS klienta — sdílená přes context (useDayData ji odebírá)
  const [live] = useState(() => socket ?? new LiveSocket(WS_URL))
  // Poslední volby uživatele přežívají refresh (ADR-0007, #167); URL má přednost
  const [symbol, setSymbol] = usePersistentState('symbol', initialSymbol, shortString())
  // Nedávné (#521): posledních 8 zobrazených symbolů, aktivní první
  const [recentSymbols, setRecentSymbols] = usePersistentState<string[]>(
    'recentSymbols',
    [],
    (value, fallback) =>
      Array.isArray(value)
        ? value.filter((item): item is string => typeof item === 'string').slice(0, 8)
        : fallback,
  )
  useEffect(() => {
    setRecentSymbols((previous) =>
      [symbol, ...previous.filter((item) => item !== symbol)].slice(0, 8),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setter z usePersistentState je stabilní
  }, [symbol])
  const forgetRecentSymbol = useCallback(
    (item: string) => setRecentSymbols((previous) => previous.filter((row) => row !== item)),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setter z usePersistentState je stabilní
    [],
  )
  const [expiries, setExpiries] = useState<string[]>([])
  const [expiryClasses, setExpiryClasses] = useState<Record<string, string[]>>({})
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null)
  const [expiryFallback, setExpiryFallback] = useState<{
    requested: string
    shown: string
  } | null>(null)
  const [timeframe, setTimeframe] = usePersistentState<'intraday' | 'daily'>(
    'timeframe',
    'intraday',
    oneOf(['intraday', 'daily']),
  )
  const [interval, setInterval] = usePersistentState<Interval>('interval', '1m', oneOf(INTERVALS))
  const [view, setView] = useState<AppView>(() => initialFromUrl().view)
  // Rychlý vstup do deníku (#673) — jen v paměti, nepersistuje se
  const [journalDraft, setJournalDraft] = useState<{ tsRef: string; text?: string } | null>(null)
  // Traders mode (#627 bod 5): přepínač trading vrstev; když se osvědčí,
  // přepínač zmizí a vrstvy budou standard
  const [tradersMode, setTradersMode] = usePersistentState<boolean>(
    'tradersMode',
    false,
    (value, fallback) => (typeof value === 'boolean' ? value : fallback),
  )
  // Kalkulačka pozice (#679): default 5 000 $ (báze P/L statistik #191) a 1 %
  const [riskAccountUsd, setRiskAccountUsd] = usePersistentState<number>(
    'riskAccountUsd',
    5000,
    clampedNumber(0, 100_000_000),
  )
  const [riskPct, setRiskPct] = usePersistentState<number>('riskPct', 1, clampedNumber(0, 100))
  const [theme, setTheme] = usePersistentState<Theme>(
    'theme',
    'dark',
    oneOf(['dark', 'light']),
    initialFromUrl().theme,
  )
  const [alerts, setAlerts] = useState<AlertMessage[]>([])
  const [unreadAlerts, setUnreadAlerts] = useState(0)
  const [consoleLog, setConsoleLog] = useState<string[]>([])
  const [setupsVersion, setSetupsVersion] = useState(0)
  const [priceInfo, setPriceInfoState] = useState<PriceInfo>({ last: null, changePct: null })
  // Bail-out na stejné hodnoty — pojistka proti render smyčce při nestabilních identitách
  const setPriceInfo = useCallback((info: PriceInfo) => {
    setPriceInfoState((previous) =>
      previous.last === info.last && previous.changePct === info.changePct ? previous : info,
    )
  }, [])
  const [ohlcCoverage, setOhlcCoverageState] = useState<Coverage | null>(null)
  // Bail-out na stejné hodnoty jako u priceInfo — graf hlásí pokrytí při každé nové minutě
  const setOhlcCoverage = useCallback((coverage: Coverage | null) => {
    setOhlcCoverageState((previous) =>
      previous?.covered === coverage?.covered && previous?.expected === coverage?.expected
        ? previous
        : coverage,
    )
  }, [])
  const [regimeInfo, setRegimeInfoState] = useState<RegimeInfo>({
    state: null,
    measuredFlip: null,
    dynamicFlip: null,
  })
  // Stejný bail-out jako priceInfo — pojistka proti render smyčce (#78)
  const setRegimeInfo = useCallback((info: RegimeInfo) => {
    setRegimeInfoState((previous) =>
      previous.state === info.state &&
      previous.measuredFlip === info.measuredFlip &&
      previous.dynamicFlip === info.dynamicFlip &&
      previous.fromProfileSign === info.fromProfileSign
        ? previous
        : info,
    )
  }, [])
  // Settle watch (#603) — týž bail-out vzor proti render smyčce
  const [settleWatch, setSettleWatchState] = useState<SettleWatchInfo | null>(null)
  const setSettleWatch = useCallback((info: SettleWatchInfo | null) => {
    setSettleWatchState((previous) =>
      previous?.name === info?.name &&
      previous?.level === info?.level &&
      previous?.distance === info?.distance &&
      previous?.weak === info?.weak
        ? previous
        : info,
    )
  }, [])
  const [toggles, setToggles] = usePersistentState<Toggles>(
    'toggles',
    DEFAULT_TOGGLES,
    mergedBooleans<Toggles>(),
  )
  // Režim signálů je string, do `Toggles` (jen booleany) nepatří
  const [signalMode, setSignalMode] = usePersistentState<SignalMode>(
    'signalMode',
    'off',
    oneOf(SIGNAL_MODES),
  )
  // Dřív checkbox dynGexField (#242) — dropdown ploch ho nahrazuje (#204)
  // Default `gex` (#837): modelované gamma pole je hlavní přidaná hodnota
  // nástroje — jako `off` zůstávalo schované za dropdownem a uživatel viděl
  // jen měřené módy s konstantní projekcí
  const [underlayPlane, setUnderlayPlane] = usePersistentState<UnderlayPlane>(
    'underlayPlane',
    'gex',
    oneOf(UNDERLAY_PLANES),
  )
  const [newsMarkerFilter, setNewsMarkerFilter] = usePersistentState<NewsMarkerFilter>(
    'newsMarkerFilter',
    'all',
    oneOf(NEWS_MARKER_FILTERS),
  )
  // Rozsah Forward GEX projekce (#572): default celý týden — přepínač filtruje
  const [forwardRange, setForwardRange] = usePersistentState<ForwardRange>(
    'forwardRange',
    'week',
    oneOf(FORWARD_RANGES),
  )
  // Jednotka Dyn ploch (#569): default $/1 % (referenční čtení); engine ukládá $/bod
  const [gexUnits, setGexUnits] = usePersistentState<GexUnits>(
    'gexUnits',
    'per_percent',
    oneOf(GEX_UNITS),
  )
  // Zdroj OI per symbol (#232) — vzor chartYRange mapy: klíč = symbol,
  // chybějící záznam = default měřené (FA je opt-in)
  const [oiSourceMap, setOiSourceMap] = usePersistentState<Record<string, OiSource>>(
    'oiSourceBySymbol',
    {},
    enumMap(OI_SOURCES),
  )
  const oiSource: OiSource = oiSourceMap[symbol] ?? 'measured'
  const setOiSource = useCallback(
    (source: OiSource) => setOiSourceMap((previous) => ({ ...previous, [symbol]: source })),
    [symbol, setOiSourceMap],
  )

  const appendLog = useCallback((line: string) => {
    const stamp = new Date().toLocaleTimeString()
    setConsoleLog((previous) => [...previous.slice(-(LOG_LIMIT - 1)), `[${stamp}] ${line}`])
  }, [])

  useEffect(() => {
    live.subscribe('status', (data) => {
      setStatus(data as unknown as PipelineStatus)
      const record = data as Record<string, unknown>
      appendLog(
        `status: engine=${String(record.engine)} connection=${String(record.connection ?? '—')}`,
      )
    })
    live.subscribe('alerts', (data) => {
      const alert = data as unknown as AlertMessage
      setAlerts((previous) => [...previous.slice(-(ALERTS_LIMIT - 1)), alert])
      setUnreadAlerts((previous) => previous + 1)
      appendLog(`alert [${alert.kind}] ${alert.message}`)
    })
    live.subscribe('setups.*', () => {
      setSetupsVersion((previous) => previous + 1)
    })
    // Odmítnutá subskripce nebo chyba protokolu musí být vidět (#948) — dřív
    // se zahazovala a `alerts` i `status` 24 dní tiše nechodily
    const offNotice = live.onNotice((text) => appendLog(text))
    live.connect()
    return () => {
      offNotice()
      live.close()
    }
  }, [live, appendLog])

  // Počáteční stav pipeline hned z REST — WS push chodí až s dalším cyklem enginu (~60 s)
  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/status`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: PipelineStatus | null) => {
        if (!cancelled && payload) setStatus(payload)
      })
      .catch(() => {
        // API neběží — zůstává offline stav
      })
    return () => {
      cancelled = true
    }
  }, [])

  const [expiryRetry, setExpiryRetry] = useState(0)
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    // Čerstvě přidaný ticker nemusí mít ještě data — bez expirací zkoušet à 30 s
    const scheduleRetry = () => {
      timer = setTimeout(() => setExpiryRetry((n) => n + 1), 30_000)
    }
    fetch(`${API_BASE}/instruments/${symbol}/expiries`)
      .then((response) => (response.ok ? response.json() : { expiries: [] }))
      .then(
        (payload: {
          expiries: string[]
          detail?: { date: string; trading_classes: string[] }[]
        }) => {
          if (cancelled) return
          setExpiries(payload.expiries)
          // Trading classes do mapy (#513); starší API bez `detail` = prázdno
          setExpiryClasses(
            Object.fromEntries(
              (payload.detail ?? []).map((row) => [row.date, row.trading_classes]),
            ),
          )
          const candidate = defaultExpiry(payload.expiries)
          setSelectedExpiry(candidate)
          if (payload.expiries.length === 0) scheduleRetry()
          // Mimo obchodování nemá vybraná expirace data (#946) — doskočit na
          // poslední den, který je má. Jen dotaz navíc, výběr se opraví, až
          // odpověď dorazí; v seanci se větev neuplatní.
          if (candidate !== null) {
            fetch(`${API_BASE}/instruments/${symbol}/days`)
              .then((response) => (response.ok ? response.json() : { days: [] }))
              .then((daysPayload: { days: { date: string; expiry: string }[] }) => {
                if (cancelled) return
                const withData = expiryWithData(candidate, daysPayload.days ?? [])
                // Doskok MUSÍ být vidět (#946): o víkendu je čekaný, ale ve všední
                // den znamená výpadek sběru — a tichá záměna dne za starší je pak
                // horší než demo, které aspoň křičelo, že něco chybí
                if (withData !== candidate && withData !== null) {
                  setSelectedExpiry(withData)
                  setExpiryFallback({ requested: candidate, shown: withData })
                } else {
                  setExpiryFallback(null)
                }
              })
              .catch(() => {
                // /days nedostupné — zůstává vybraná expirace, graf řekne pravdu
              })
          }
        },
      )
      .catch(() => {
        // API neběží — hlavička ukáže placeholder, status bar offline stav
        if (!cancelled) {
          setExpiries([])
          setExpiryClasses({})
          setSelectedExpiry(null)
          setExpiryFallback(null)
          scheduleRetry()
        }
      })
    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [symbol, expiryRetry])

  const value = useMemo<AppState>(
    () => ({
      status,
      symbol,
      setSymbol,
      recentSymbols,
      forgetRecentSymbol,
      expiries,
      expiryClasses,
      selectedExpiry,
      // Ruční volba expirace doskok ruší — od té chvíle uživatel ví, na co kouká
      setSelectedExpiry: (expiry: string) => {
        setExpiryFallback(null)
        setSelectedExpiry(expiry)
      },
      expiryFallback,
      timeframe,
      setTimeframe,
      interval,
      setInterval,
      toggles,
      setToggle: (key, val) => setToggles((prev) => ({ ...prev, [key]: val })),
      signalMode,
      setSignalMode,
      underlayPlane,
      setUnderlayPlane,
      forwardRange,
      setForwardRange,
      newsMarkerFilter,
      setNewsMarkerFilter,
      gexUnits,
      setGexUnits,
      oiSource,
      setOiSource,
      view,
      setView,
      journalDraft,
      setJournalDraft,
      tradersMode,
      setTradersMode,
      riskAccountUsd,
      setRiskAccountUsd,
      riskPct,
      setRiskPct,
      theme,
      setTheme,
      alerts,
      unreadAlerts,
      markAlertsRead: () => setUnreadAlerts(0),
      consoleLog,
      priceInfo,
      setPriceInfo,
      regimeInfo,
      setRegimeInfo,
      settleWatch,
      setSettleWatch,
      ohlcCoverage,
      setOhlcCoverage,
      setupsVersion,
      socket: live,
    }),
    [
      status,
      symbol,
      // Settery z usePersistentState jsou stabilní useState settery — lint to
      // přes vlastní hook nevidí, proto jsou v deps (nic nepřepočítávají)
      setSymbol,
      setTimeframe,
      setInterval,
      setTheme,
      setToggles,
      signalMode,
      setSignalMode,
      underlayPlane,
      setUnderlayPlane,
      forwardRange,
      setForwardRange,
      newsMarkerFilter,
      setNewsMarkerFilter,
      gexUnits,
      setGexUnits,
      oiSource,
      setOiSource,
      recentSymbols,
      forgetRecentSymbol,
      expiries,
      expiryClasses,
      selectedExpiry,
      expiryFallback,
      timeframe,
      interval,
      toggles,
      view,
      journalDraft,
      tradersMode,
      riskAccountUsd,
      riskPct,
      theme,
      alerts,
      unreadAlerts,
      consoleLog,
      priceInfo,
      setPriceInfo,
      regimeInfo,
      setRegimeInfo,
      settleWatch,
      setSettleWatch,
      ohlcCoverage,
      setOhlcCoverage,
      setupsVersion,
      live,
    ],
  )

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>
}

export function useAppState(): AppState {
  const state = useContext(AppStateContext)
  if (state === null) {
    throw new Error('useAppState musí být uvnitř AppStateProvider')
  }
  return state
}

/** Scénář dne (#1173, #1126 bod 3a): založení se snímkem, seznam, track record, úklid.

Scénář vzniká jen dopředu — na živém dni, s termínem dnes nebo v budoucnu;
vyhodnocení dělá engine po settle termínu. Cíle se odvozují z geometrie
anotace (obraty + koncový bod), uživatel je před uložením může upravit. */
import { API_BASE } from '../config'
import type { AnnotationPayload } from '../annotations/model'

export interface ScenarioPathPoint {
  ts: string
  price: number
}

export interface ScenarioResult {
  hit1: boolean
  hit2: boolean | null
  order_ok: boolean | null
  hit1_ts: string | null
  hit2_ts: string | null
  max_dev_pts: number | null
  max_dev_em: number | null
  bars: number
  verdict: 'hit' | 'partial' | 'miss'
}

export interface Scenario {
  id: number
  symbol: string
  day: string
  created_at: string
  deadline: string
  deadline_ts: string
  entry: number
  targets: number[]
  path: ScenarioPathPoint[]
  annotation_id: number | null
  note: string | null
  has_image: boolean
  image_bytes: number
  evaluated_at: string | null
  result: ScenarioResult | null
}

export interface ScenarioStats {
  symbol: string | null
  preliminary: boolean
  n: number
  hit1: number
  hit1_rate: number | null
  n_second: number
  hit2: number
  hit2_rate: number | null
  n_order: number
  order_ok: number
  order_rate: number | null
  median_dev_em: number | null
}

export interface ScenarioDisk {
  bytes: number
  scenarios: number
  images: number
  limit_bytes: number
  over_limit: boolean
}

export interface ScenarioCreateIn {
  symbol: string
  entry: number
  path: ScenarioPathPoint[]
  targets: number[] | null
  deadline: string | null
  annotation_id: number | null
  note: string | null
  image_png_base64: string | null
}

/** Cíle z geometrie: obraty cesty + koncový bod, drobné chvění se slučuje
(zrcadlo `compute/scenario.targets_from_path`). */
export function targetsFromPath(path: ScenarioPathPoint[], entry: number, limit = 3): number[] {
  if (path.length === 0) return []
  const prices = [...path].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts)).map((p) => p.price)
  const tolerance = Math.abs(entry) * 0.0005
  const targets: number[] = []
  let direction = 0
  let extreme = prices[0]
  for (let index = 1; index < prices.length; index += 1) {
    const price = prices[index]
    const delta = price - extreme
    if (Math.abs(delta) <= tolerance) continue
    const step = delta > 0 ? 1 : -1
    const turned = direction !== 0 && step !== direction
    if (
      turned &&
      (targets.length === 0 || Math.abs(extreme - targets[targets.length - 1]) > tolerance)
    ) {
      targets.push(extreme)
    }
    direction = step
    extreme = price
  }
  const end = prices[prices.length - 1]
  if (targets.length === 0 || Math.abs(end - targets[targets.length - 1]) > tolerance)
    targets.push(end)
  return targets.slice(0, limit).map((value) => Math.round(value * 100) / 100)
}

/** Body anotace (minuta osy × strike) → absolutní čas × cena. */
export function pathFromAnnotation(
  payload: AnnotationPayload,
  minutesIso: string[],
): ScenarioPathPoint[] {
  if (minutesIso.length === 0) return []
  const start = Date.parse(minutesIso[0])
  if (Number.isNaN(start)) return []
  return payload.points.map((point) => ({
    ts: new Date(start + point.minute * 60_000).toISOString(),
    price: Math.round(point.strike * 100) / 100,
  }))
}

export async function createScenario(
  body: ScenarioCreateIn,
): Promise<{ ok: true; scenario: Scenario } | { ok: false; error: string }> {
  try {
    const response = await fetch(`${API_BASE}/scenarios`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const payload = (await response.json()) as { detail?: unknown }
        if (typeof payload.detail === 'string') detail = payload.detail
      } catch {
        // bez JSON těla
      }
      return { ok: false, error: detail }
    }
    return { ok: true, scenario: (await response.json()) as Scenario }
  } catch {
    return { ok: false, error: 'API nedostupné' }
  }
}

export async function fetchScenarios(symbol?: string, limit = 50): Promise<Scenario[]> {
  try {
    const query = new URLSearchParams({ limit: String(limit) })
    if (symbol) query.set('symbol', symbol)
    const response = await fetch(`${API_BASE}/scenarios?${query.toString()}`)
    if (!response.ok) return []
    const payload = (await response.json()) as { scenarios?: unknown }
    // Cizí tvar (mock, starší API) = prázdno, ne pád komponenty
    return Array.isArray(payload.scenarios) ? (payload.scenarios as Scenario[]) : []
  } catch {
    return []
  }
}

export async function fetchScenarioStats(symbol?: string): Promise<ScenarioStats | null> {
  try {
    const query = symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''
    const response = await fetch(`${API_BASE}/scenarios/stats${query}`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<ScenarioStats>
    return typeof payload.n === 'number' ? (payload as ScenarioStats) : null
  } catch {
    return null
  }
}

export async function fetchScenarioDisk(): Promise<ScenarioDisk | null> {
  try {
    const response = await fetch(`${API_BASE}/scenarios/disk`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<ScenarioDisk>
    return typeof payload.bytes === 'number' ? (payload as ScenarioDisk) : null
  } catch {
    return null
  }
}

export async function cleanupScenarioImages(
  olderThanDays: number,
): Promise<{ removed: number; freed_bytes: number } | null> {
  try {
    const response = await fetch(`${API_BASE}/scenarios/images?older_than_days=${olderThanDays}`, {
      method: 'DELETE',
    })
    if (!response.ok) return null
    return (await response.json()) as { removed: number; freed_bytes: number }
  } catch {
    return null
  }
}

export function scenarioImageUrl(id: number): string {
  return `${API_BASE}/scenarios/${id}/image`
}

export function verdictLabel(result: ScenarioResult | null): string {
  if (!result) return 'čeká na termín'
  if (result.verdict === 'hit') return 'trefa'
  if (result.verdict === 'partial') return 'částečně'
  return 'mimo'
}

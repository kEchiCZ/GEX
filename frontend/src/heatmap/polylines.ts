/** Polylinie kontur (#1222): úsečky marching squares napojené do souvislých
křivek a vyhlazené — místo mozaiky tisíců oddělených úseček po buňkách.

Napojení jde přes kvantované koncové body (klíč = zaokrouhlené souřadnice
buněk), takže úsečky téže izolinie ze sousedních čtverců na sebe navazují
přesně; otevřené křivky začínají v koncovém bodě, který má jediného souseda,
uzavřené (ostrov) kdekoli. Vyhlazení = Chaikin (corner cutting), u uzavřené
křivky přes celý obvod. Kódování do plochého Float32Array pro přenos z
workeru (transferable). */
import type { Segment } from './contours'

export interface Polyline {
  /** Body [x0, y0, x1, y1, …] v souřadnicích buněk. */
  points: number[]
  closed: boolean
}

const QUANT = 1000

function keyOf(x: number, y: number): string {
  return `${Math.round(x * QUANT)}|${Math.round(y * QUANT)}`
}

/** Spojí úsečky do polylinií podle sdílených koncových bodů. */
export function joinSegments(segments: Segment[]): Polyline[] {
  if (segments.length === 0) return []
  // Graf: klíč bodu → indexy úseček, které v něm končí
  const adjacency = new Map<string, number[]>()
  const endpoints: [string, string][] = []
  segments.forEach(([x1, y1, x2, y2], index) => {
    const a = keyOf(x1, y1)
    const b = keyOf(x2, y2)
    endpoints.push([a, b])
    for (const key of [a, b]) {
      const list = adjacency.get(key)
      if (list) list.push(index)
      else adjacency.set(key, [index])
    }
  })
  const used = new Uint8Array(segments.length)
  const polylines: Polyline[] = []

  const walk = (startIndex: number, startKey: string): { keys: string[]; closed: boolean } => {
    const keys = [startKey]
    let index = startIndex
    let key = startKey
    for (;;) {
      used[index] = 1
      const [a, b] = endpoints[index]
      const next = a === key ? b : a
      keys.push(next)
      const candidates = adjacency.get(next) ?? []
      const following = candidates.find((candidate) => !used[candidate])
      if (following === undefined) {
        return { keys, closed: next === startKey }
      }
      index = following
      key = next
    }
  }

  const coordsOf = (key: string): [number, number] => {
    const [x, y] = key.split('|').map(Number)
    return [x / QUANT, y / QUANT]
  }
  const emit = (keys: string[], closed: boolean) => {
    const points: number[] = []
    const last = closed ? keys.length - 1 : keys.length
    for (let index = 0; index < last; index += 1) {
      const [x, y] = coordsOf(keys[index])
      points.push(x, y)
    }
    if (points.length >= 4) polylines.push({ points, closed })
  }

  // Nejdřív otevřené křivky (z bodů s jediným sousedem), pak zbylé ostrovy
  for (const [key, list] of adjacency) {
    if (list.length !== 1) continue
    const index = list[0]
    if (used[index]) continue
    const { keys, closed } = walk(index, key)
    emit(keys, closed)
  }
  for (let index = 0; index < segments.length; index += 1) {
    if (used[index]) continue
    const { keys, closed } = walk(index, endpoints[index][0])
    emit(keys, closed)
  }
  return polylines
}

/** Chaikinovo vyhlazení (corner cutting) — `iterations` průchodů. */
export function chaikin(points: number[], closed: boolean, iterations = 2): number[] {
  let current = points
  for (let pass = 0; pass < iterations; pass += 1) {
    const count = current.length / 2
    if (count < 3) return current
    const next: number[] = []
    const limit = closed ? count : count - 1
    if (!closed) next.push(current[0], current[1])
    for (let index = 0; index < limit; index += 1) {
      const j = (index + 1) % count
      const x0 = current[index * 2]
      const y0 = current[index * 2 + 1]
      const x1 = current[j * 2]
      const y1 = current[j * 2 + 1]
      next.push(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1)
      next.push(0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)
    }
    if (!closed) next.push(current[current.length - 2], current[current.length - 1])
    current = next
  }
  return current
}

export function smoothPolylines(polylines: Polyline[], iterations = 2): Polyline[] {
  return polylines.map((polyline) => ({
    points: chaikin(polyline.points, polyline.closed, iterations),
    closed: polyline.closed,
  }))
}

/** Kódování pro worker: [n, (počet bodů, closed, x, y, …)…]. */
export function encodePolylines(polylines: Polyline[]): Float32Array {
  let size = 1
  for (const polyline of polylines) size += 2 + polyline.points.length
  const flat = new Float32Array(size)
  flat[0] = polylines.length
  let offset = 1
  for (const polyline of polylines) {
    flat[offset] = polyline.points.length / 2
    flat[offset + 1] = polyline.closed ? 1 : 0
    flat.set(polyline.points, offset + 2)
    offset += 2 + polyline.points.length
  }
  return flat
}

export function decodePolylines(flat: Float32Array): Polyline[] {
  if (flat.length === 0) return []
  const count = flat[0]
  const polylines: Polyline[] = []
  let offset = 1
  for (let index = 0; index < count; index += 1) {
    const points = flat[offset]
    const closed = flat[offset + 1] === 1
    const values = Array.from(flat.subarray(offset + 2, offset + 2 + points * 2))
    polylines.push({ points: values, closed })
    offset += 2 + points * 2
  }
  return polylines
}

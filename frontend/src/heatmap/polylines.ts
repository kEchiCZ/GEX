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

/** Spojí úsečky do polylinií podle sdílených koncových bodů.

Klíč kvantovaného bodu je jedno číslo (#1420). Dřív to byl řetězec `x|y`
a jeho stavba, hashování a zpětné parsování stály u 10 tisíc úseček desítky
ms. Kódování je prosté: složka se posune o minimum v datech a y se vejde pod
násobitel řádku, takže se spojí přesně tytéž body jako s řetězci. Složka NaN
(pole s NaN) má vlastní hodnotu jako dřív „NaN“ v řetězci. */
export function joinSegments(segments: Segment[]): Polyline[] {
  if (segments.length === 0) return []
  // Kvantované složky koncových bodů [x1, y1, x2, y2] a jejich rozsah
  const quantized = new Float64Array(segments.length * 4)
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index]
    for (let part = 0; part < 4; part += 1) {
      const value = Math.round(segment[part] * QUANT)
      quantized[index * 4 + part] = value
      if (Number.isNaN(value)) continue
      // Marching squares nekonečnou souřadnici nevyrobí (interpolace dá nejvýš NaN)
      if (!Number.isFinite(value)) throw new Error(`joinSegments: nekonečná souřadnice úsečky ${index}`) // prettier-ignore
      if (part % 2 === 0) {
        minX = Math.min(minX, value)
        maxX = Math.max(maxX, value)
      } else {
        minY = Math.min(minY, value)
        maxY = Math.max(maxY, value)
      }
    }
  }
  // Složka jen s NaN: prázdný rozsah, NaN dostane hodnotu 0
  if (minX > maxX) [minX, maxX] = [0, -1]
  if (minY > maxY) [minY, maxY] = [0, -1]
  const nanColumn = maxX - minX + 1
  const nanRow = maxY - minY + 1
  const rowStride = nanRow + 1
  if ((nanColumn + 1) * rowStride > Number.MAX_SAFE_INTEGER) {
    throw new Error('joinSegments: rozsah souřadnic přesahuje přesná celá čísla')
  }
  const keyOf = (x: number, y: number): number =>
    (Number.isNaN(x) ? nanColumn : x - minX) * rowStride + (Number.isNaN(y) ? nanRow : y - minY)

  // Graf: klíč bodu → indexy úseček, které v něm končí
  const adjacency = new Map<number, number[]>()
  const endpoints = new Float64Array(segments.length * 2)
  const link = (key: number, index: number) => {
    const list = adjacency.get(key)
    if (list) list.push(index)
    else adjacency.set(key, [index])
  }
  for (let index = 0; index < segments.length; index += 1) {
    const base = index * 4
    const a = keyOf(quantized[base], quantized[base + 1])
    const b = keyOf(quantized[base + 2], quantized[base + 3])
    endpoints[index * 2] = a
    endpoints[index * 2 + 1] = b
    link(a, index)
    link(b, index)
  }
  const used = new Uint8Array(segments.length)
  const polylines: Polyline[] = []

  const walk = (startIndex: number, startKey: number): { keys: number[]; closed: boolean } => {
    const keys = [startKey]
    let index = startIndex
    let key = startKey
    for (;;) {
      used[index] = 1
      const a = endpoints[index * 2]
      const next = a === key ? endpoints[index * 2 + 1] : a
      keys.push(next)
      let following = -1
      for (const candidate of adjacency.get(next) ?? []) {
        if (!used[candidate]) {
          following = candidate
          break
        }
      }
      if (following < 0) return { keys, closed: next === startKey }
      index = following
      key = next
    }
  }

  const emit = (keys: number[], closed: boolean) => {
    const points: number[] = []
    const last = closed ? keys.length - 1 : keys.length
    for (let index = 0; index < last; index += 1) {
      const key = keys[index]
      // Celočíselné dělení klíče, s korekcí zaokrouhlení podílu u velkých klíčů
      let column = Math.floor(key / rowStride)
      let row = key - column * rowStride
      if (row < 0) [column, row] = [column - 1, row + rowStride]
      else if (row >= rowStride) [column, row] = [column + 1, row - rowStride]
      points.push(
        column === nanColumn ? NaN : (column + minX) / QUANT,
        row === nanRow ? NaN : (row + minY) / QUANT,
      )
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
    const { keys, closed } = walk(index, endpoints[index * 2])
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

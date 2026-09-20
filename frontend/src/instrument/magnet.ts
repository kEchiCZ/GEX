/** Magnet úrovně (#1223): kam dnešní positioning tlačí (negativní gamma) nebo
lepí (pozitivní gamma) cenu a kdy ten tlak zmizí (expirace řetězu).

Jen z existujících řad: put/call wall, flip, těžiště GEX (`centroid`) a GEX
režim (#209). Není to signál ani setup — jedna věta nad grafem místo
skládání z panelů. Bez potřebné úrovně (žádná zeď pod cenou…) vrací null a
chip se nekreslí; tichý odhad je horší než nic. */
import type { GexRegimeState } from './regime'

export interface MagnetInput {
  spot: number | null
  regime: GexRegimeState | null
  flip: number | null
  putWall: number | null
  callWall: number | null
  /** Těžiště GEX (levels.centroid) — pin v pozitivním režimu. */
  centroid: number | null
}

export type MagnetKind = 'push' | 'pin' | 'edge'
export type MagnetSource = 'put_wall' | 'call_wall' | 'centroid' | 'flip'

export interface Magnet {
  kind: MagnetKind
  source: MagnetSource
  level: number
  /** level − spot v bodech (záporné = pod cenou). */
  distance: number
}

/** Pásmo kolem flipu, kde režim není jednoznačný a magnet je flip sám (body). */
export const EDGE_BAND_POINTS = 5

const finite = (value: number | null): value is number => value !== null && Number.isFinite(value)

export function magnetLevel(input: MagnetInput): Magnet | null {
  const { spot, regime, flip, putWall, callWall, centroid } = input
  if (!finite(spot) || regime === null) return null
  if (finite(flip) && Math.abs(spot - flip) <= EDGE_BAND_POINTS) {
    return { kind: 'edge', source: 'flip', level: flip, distance: flip - spot }
  }
  if (regime === 'flipzone') {
    return finite(flip)
      ? { kind: 'edge', source: 'flip', level: flip, distance: flip - spot }
      : null
  }
  if (regime === 'negative') {
    // Dealeři hedgují ve směru pohybu — cena zrychluje k zóně největší
    // negativní gammy pod sebou; nad cenou tlačí call wall dolů jen slabě
    if (finite(putWall) && putWall < spot) {
      return { kind: 'push', source: 'put_wall', level: putWall, distance: putWall - spot }
    }
    if (finite(callWall) && callWall > spot) {
      return { kind: 'push', source: 'call_wall', level: callWall, distance: callWall - spot }
    }
    return null
  }
  // Pozitivní gamma: lepení k těžišti kladné gammy (pinning)
  if (finite(centroid)) {
    return { kind: 'pin', source: 'centroid', level: centroid, distance: centroid - spot }
  }
  if (finite(flip)) {
    return { kind: 'pin', source: 'flip', level: flip, distance: flip - spot }
  }
  return null
}

export const MAGNET_SOURCE_LABELS: Record<MagnetSource, string> = {
  put_wall: 'put wall',
  call_wall: 'call wall',
  centroid: 'těžiště GEX',
  flip: 'flip',
}

export function magnetGlyph(magnet: Magnet): string {
  return magnet.kind === 'pin' ? '📌' : magnet.kind === 'edge' ? '⚖️' : '🧲'
}

function formatPoints(distance: number): string {
  const rounded = Math.round(distance * 100) / 100
  const sign = rounded > 0 ? '+' : rounded < 0 ? '−' : '±'
  return `${sign}${Math.abs(rounded)} b`
}

/** Krátký text chipu: „tlačí k 7600 (put wall, −13 b) · zmizí za 13 h 40 m“. */
export function magnetChipText(magnet: Magnet, countdown: string | null): string {
  const verb = magnet.kind === 'push' ? 'tlačí k' : magnet.kind === 'pin' ? 'lepí k' : 'na hraně'
  const where = `${verb} ${magnet.level.toFixed(magnet.level % 1 === 0 ? 0 : 2)}`
  const detail = `${MAGNET_SOURCE_LABELS[magnet.source]}, ${formatPoints(magnet.distance)}`
  const until = countdown ? ` · zmizí ${countdown.replace(/^≈ /, '')}` : ''
  return `${where} (${detail})${until}`
}

/** Věta do Briefingu: režim + magnet + expirace. */
export function magnetSentence(
  magnet: Magnet,
  regime: GexRegimeState | null,
  countdown: string | null,
): string {
  const head =
    magnet.kind === 'push'
      ? `Negativní gamma tlačí cenu k ${magnet.level.toFixed(0)} (${MAGNET_SOURCE_LABELS[magnet.source]}, ${formatPoints(magnet.distance)})`
      : magnet.kind === 'pin'
        ? `Pozitivní gamma lepí cenu k ${magnet.level.toFixed(0)} (${MAGNET_SOURCE_LABELS[magnet.source]}, ${formatPoints(magnet.distance)})`
        : `Cena je na hraně režimu u flipu ${magnet.level.toFixed(0)} (${formatPoints(magnet.distance)})`
  const tail = countdown
    ? `; tento tlak zmizí s expirací řetězu ${countdown}.`
    : regime === null
      ? '.'
      : '; platí do expirace řetězu.'
  return head + tail
}

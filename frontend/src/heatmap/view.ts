/** Transformace pohledu grafu (TradingView styl): nezávislý zoom os X/Y, pan, reset. */

export interface ViewTransform {
  offsetX: number
  offsetY: number
  zoomX: number
  zoomY: number
}

export const DEFAULT_VIEW: ViewTransform = { offsetX: 0, offsetY: 0, zoomX: 1, zoomY: 1 }

/** Meze zoomu: <1 = smrštění (více kontextu), >1 = roztažení (větší svíčky). */
export const ZOOM_MIN = 0.2
export const ZOOM_MAX = 32

/** Strop základní šířky koše v px — málo dat se neroztahuje na celou šířku (TradingView). */
export const BUCKET_MAX_PX = 12
/** Odsazení posledního koše od pravého okraje ve výchozím pohledu. */
export const TIME_RIGHT_MARGIN_PX = 60

/** Základní šířka jednoho time-bucketu v px (zoomX = 1): fit-to-width, nejvýš BUCKET_MAX_PX.

Plný den dat vyplní šířku jako dřív; po startu s pár minutami dostane svíčka
fixní šířku místo roztažení přes celý canvas.
*/
export function baseBucketPx(minutes: number, canvasWidth: number): number {
  return Math.min(canvasWidth / Math.max(1, minutes), BUCKET_MAX_PX)
}

/** Výchozí offsetX: data užší než canvas se ukotví k pravému okraji s odsazením. */
export function homeOffsetX(minutes: number, canvasWidth: number): number {
  const plotWidth = minutes * baseBucketPx(minutes, canvasWidth)
  return Math.max(0, canvasWidth - TIME_RIGHT_MARGIN_PX - plotWidth)
}

function clampZoom(value: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, value))
}

/** Zoom jedné osy kolem kotvy v pixelech — bod pod kotvou zůstává na místě. */
export function zoomAxis(
  view: ViewTransform,
  axis: 'x' | 'y',
  factor: number,
  anchorPx: number,
): ViewTransform {
  if (axis === 'x') {
    const zoomX = clampZoom(view.zoomX * factor)
    const applied = zoomX / view.zoomX
    return { ...view, zoomX, offsetX: anchorPx - (anchorPx - view.offsetX) * applied }
  }
  const zoomY = clampZoom(view.zoomY * factor)
  const applied = zoomY / view.zoomY
  return { ...view, zoomY, offsetY: anchorPx - (anchorPx - view.offsetY) * applied }
}

/** Zoom obou os najednou (kolečko nad plochou grafu) kolem bodu kurzoru. */
export function zoomBoth(
  view: ViewTransform,
  factor: number,
  anchorX: number,
  anchorY: number,
): ViewTransform {
  return zoomAxis(zoomAxis(view, 'x', factor, anchorX), 'y', factor, anchorY)
}

/** Výchozí pohled dne: osa Y napasovaná na cenové pásmo (TradingView auto-fit).

Denní obálka strikes je široká (stovky bodů), zatímco cena se hýbe v úzkém
pásmu — bez fitu jsou svíčky zploštělé. Fit namapuje [min low, max high]
s odsazením na 10–90 % výšky canvasu; heatmapa zůstává zarovnaná (jen zoomY
a offsetY, osa X se nemění).
*/
export function fitPriceView(
  strikes: number[],
  priceLow: number | null,
  priceHigh: number | null,
  canvasHeight = 640,
): ViewTransform {
  if (strikes.length < 2 || priceLow === null || priceHigh === null || priceHigh < priceLow) {
    return DEFAULT_VIEW
  }
  const step = canvasHeight / strikes.length
  const rowOf = (value: number): number => {
    // Interpolovaná pozice hodnoty v ose strikes (index 0 = nejnižší strike)
    const last = strikes.length - 1
    if (value <= strikes[0]) return 0
    if (value >= strikes[last]) return last
    for (let index = 0; index < last; index += 1) {
      if (value >= strikes[index] && value <= strikes[index + 1]) {
        return index + (value - strikes[index]) / (strikes[index + 1] - strikes[index])
      }
    }
    return last
  }
  const yTop = (strikes.length - 1 - rowOf(priceHigh) + 0.5) * step
  const yBottom = (strikes.length - 1 - rowOf(priceLow) + 0.5) * step
  const span = yBottom - yTop
  if (span <= 0) return DEFAULT_VIEW
  const zoomY = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, (0.8 * canvasHeight) / span))
  return { ...DEFAULT_VIEW, zoomY, offsetY: 0.1 * canvasHeight - yTop * zoomY }
}

/** Velikost plátna v logických CSS px. */
export interface CanvasSize {
  width: number
  height: number
}

/** Kompenzace zoomu při změně velikosti plátna — obsah grafu se nehýbe (#171).

Základní měřítko os je odvozené z rozměrů plátna (`baseBucketPx(minutes, width)`,
`height / strikeCount`), takže roztažení panelů či okna by obsah proporčně
přeškálovalo. Kompenzovaný zoom drží součin base × zoom konstantní: pixelové
pozice buněk zůstávají (kotva levý horní roh, offsety se nemění) a změna
rozměru jen odkryje/skryje kus plochy — TradingView chování. Záměrně bez
ořezu na ZOOM_MIN/MAX: kompenzace není uživatelský zoom, ale změna základu. */
export function compensateView(
  view: ViewTransform,
  minutes: number,
  previous: CanvasSize,
  next: CanvasSize,
): ViewTransform {
  if (previous.width <= 0 || previous.height <= 0 || next.width <= 0 || next.height <= 0) {
    return view
  }
  const factorX = baseBucketPx(minutes, previous.width) / baseBucketPx(minutes, next.width)
  const factorY = previous.height / next.height
  if (factorX === 1 && factorY === 1) return view
  return { ...view, zoomX: view.zoomX * factorX, zoomY: view.zoomY * factorY }
}

/** Zóna interakce podle pozice kurzoru: levý pruh = osa Y, spodní pruh = osa X. */
export type AxisZone = 'x' | 'y' | null

export const AXIS_Y_WIDTH = 48
export const AXIS_X_HEIGHT = 22

export function axisZoneAt(x: number, y: number, height: number): AxisZone {
  if (y > height - AXIS_X_HEIGHT) return 'x'
  if (x < AXIS_Y_WIDTH) return 'y'
  return null
}

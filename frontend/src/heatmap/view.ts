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

/** Zóna interakce podle pozice kurzoru: levý pruh = osa Y, spodní pruh = osa X. */
export type AxisZone = 'x' | 'y' | null

export const AXIS_Y_WIDTH = 48
export const AXIS_X_HEIGHT = 22

export function axisZoneAt(x: number, y: number, height: number): AxisZone {
  if (y > height - AXIS_X_HEIGHT) return 'x'
  if (x < AXIS_Y_WIDTH) return 'y'
  return null
}

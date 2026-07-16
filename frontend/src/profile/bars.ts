/** Geometrie skládaných pruhů strike profilu (SPEC 4.6/7.3) — čisté helpery. */

export interface ProfileRow {
  strike: number
  callVolComponent: number
  callOiComponent: number
  putVolComponent: number
  putOiComponent: number
  /** Surové hodnoty pro tooltip. */
  callVolume: number
  putVolume: number
  callOi: number
  putOi: number
  distanceFromSpot: number
}

export interface BarGeometry {
  strike: number
  /** Šířky segmentů v px: call jde doprava od symetrické osy, put doleva. */
  callVolWidth: number
  callOiWidth: number
  putVolWidth: number
  putOiWidth: number
}

/** Šířky pruhů: normalizace největší celkovou stranou, zoom násobí, ořez na halfWidth. */
export function barGeometry(rows: ProfileRow[], halfWidth: number, zoom: number): BarGeometry[] {
  const maxSide = Math.max(
    1e-9,
    ...rows.map((row) =>
      Math.max(
        row.callVolComponent + row.callOiComponent,
        row.putVolComponent + row.putOiComponent,
      ),
    ),
  )
  const scale = (halfWidth / maxSide) * zoom
  const clamp = (value: number) => Math.min(halfWidth, value * scale)
  return rows.map((row) => {
    const callVolWidth = clamp(row.callVolComponent)
    const putVolWidth = clamp(row.putVolComponent)
    return {
      strike: row.strike,
      callVolWidth,
      callOiWidth: Math.min(halfWidth - callVolWidth, row.callOiComponent * scale),
      putVolWidth,
      putOiWidth: Math.min(halfWidth - putVolWidth, row.putOiComponent * scale),
    }
  })
}

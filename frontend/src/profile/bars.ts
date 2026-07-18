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
  /** Změna OI proti předchozímu archivovanému dni (null = srovnání není k dispozici). */
  callOiChange?: number | null
  putOiChange?: number | null
}

export interface BarGeometry {
  strike: number
  /** Šířky segmentů v px: call jde doprava od symetrické osy, put doleva. */
  callVolWidth: number
  callOiWidth: number
  putVolWidth: number
  putOiWidth: number
}

/** Největší celková strana (Vol+OI komponenty) — základ měřítka pruhů i osy. */
export function maxComponentSide(rows: ProfileRow[]): number {
  return Math.max(
    1e-9,
    ...rows.map((row) =>
      Math.max(
        row.callVolComponent + row.callOiComponent,
        row.putVolComponent + row.putOiComponent,
      ),
    ),
  )
}

/** Kompaktní formát množství pro osu profilu (1 234 → „1.2k"). */
export function formatAmount(value: number): string {
  if (value >= 1000) {
    const k = value / 1000
    return `${k >= 10 ? Math.round(k) : Math.round(k * 10) / 10}k`
  }
  return String(Math.round(value))
}

/** Šířky pruhů: normalizace největší celkovou stranou, zoom násobí, ořez na halfWidth. */
export function barGeometry(rows: ProfileRow[], halfWidth: number, zoom: number): BarGeometry[] {
  const maxSide = maxComponentSide(rows)
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

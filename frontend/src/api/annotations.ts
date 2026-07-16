/** REST klient /annotations (SPEC 7.4, API z issue #21). */
import { API_BASE } from '../config'
import type { AnnotationPayload, StoredAnnotation } from '../annotations/model'

interface AnnotationRow {
  id: number
  symbol: string
  day: string
  payload: AnnotationPayload
}

export async function listAnnotations(symbol: string, date: string): Promise<StoredAnnotation[]> {
  const response = await fetch(`${API_BASE}/annotations?symbol=${symbol}&date=${date}`)
  if (!response.ok) {
    throw new Error(`Načtení anotací selhalo: HTTP ${response.status}`)
  }
  const payload = (await response.json()) as { annotations: AnnotationRow[] }
  return payload.annotations.map((row) => ({ id: row.id, payload: row.payload }))
}

export async function createAnnotation(
  symbol: string,
  date: string,
  payload: AnnotationPayload,
): Promise<StoredAnnotation> {
  const response = await fetch(`${API_BASE}/annotations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, day: date, payload }),
  })
  if (!response.ok) {
    throw new Error(`Uložení anotace selhalo: HTTP ${response.status}`)
  }
  const row = (await response.json()) as AnnotationRow
  return { id: row.id, payload: row.payload }
}

export async function deleteAnnotation(id: number): Promise<void> {
  const response = await fetch(`${API_BASE}/annotations/${id}`, { method: 'DELETE' })
  if (!response.ok && response.status !== 404) {
    throw new Error(`Smazání anotace selhalo: HTTP ${response.status}`)
  }
}

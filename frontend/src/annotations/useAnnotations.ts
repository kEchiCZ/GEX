/** Stav anotací per instrument + den (SPEC 7.4): načtení, kreslení, mazání. */
import { useCallback, useEffect, useState } from 'react'
import { createAnnotation, deleteAnnotation, listAnnotations } from '../api/annotations'
import type { AnnotationPayload, StoredAnnotation } from './model'

export interface AnnotationsState {
  annotations: StoredAnnotation[]
  create: (payload: AnnotationPayload) => Promise<void>
  erase: (id: number) => Promise<void>
}

export function useAnnotations(symbol: string, date: string): AnnotationsState {
  const [annotations, setAnnotations] = useState<StoredAnnotation[]>([])

  useEffect(() => {
    let cancelled = false
    listAnnotations(symbol, date)
      .then((loaded) => {
        if (!cancelled) setAnnotations(loaded)
      })
      .catch(() => {
        // API neběží — kreslení funguje jen lokálně do reloadu
        if (!cancelled) setAnnotations([])
      })
    return () => {
      cancelled = true
    }
  }, [symbol, date])

  const create = useCallback(
    async (payload: AnnotationPayload) => {
      try {
        const stored = await createAnnotation(symbol, date, payload)
        setAnnotations((previous) => [...previous, stored])
      } catch {
        // Bez API aspoň lokálně (záporné id = neuložená)
        setAnnotations((previous) => [...previous, { id: -Date.now(), payload }])
      }
    },
    [symbol, date],
  )

  const erase = useCallback(async (id: number) => {
    setAnnotations((previous) => previous.filter((annotation) => annotation.id !== id))
    if (id > 0) {
      await deleteAnnotation(id).catch(() => {
        // Server o anotaci přijde při příštím načtení; lokálně už je pryč
      })
    }
  }, [])

  return { annotations, create, erase }
}

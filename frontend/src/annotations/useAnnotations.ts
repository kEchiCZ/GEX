/** Stav anotací per instrument + den (SPEC 7.4): načtení, kreslení, mazání, přesun. */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createAnnotation,
  deleteAnnotation,
  listAnnotations,
  updateAnnotation,
} from '../api/annotations'
import type { AnnotationPayload, StoredAnnotation } from './model'

export interface AnnotationsState {
  annotations: StoredAnnotation[]
  create: (payload: AnnotationPayload) => Promise<void>
  erase: (id: number) => Promise<void>
  /** Přesun tažením (#589): nový payload téže anotace, `id` se nemění. */
  move: (id: number, payload: AnnotationPayload) => Promise<void>
}

export function useAnnotations(symbol: string, date: string): AnnotationsState {
  const [annotations, setAnnotations] = useState<StoredAnnotation[]>([])
  // Zrcadlo stavu pro čtení mimo setState updater (návrat po neúspěšném přesunu)
  const annotationsRef = useRef<StoredAnnotation[]>([])
  useEffect(() => {
    annotationsRef.current = annotations
  }, [annotations])

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

  const move = useCallback(async (id: number, payload: AnnotationPayload) => {
    const replace = (next: AnnotationPayload) =>
      setAnnotations((previous) =>
        previous.map((annotation) => (annotation.id === id ? { id, payload: next } : annotation)),
      )
    // Původní pozice ze zrcadla, ne z updateru — ten musí zůstat čistý (#143)
    const original = annotationsRef.current.find((annotation) => annotation.id === id)?.payload
    replace(payload)
    if (id < 0) return // neuložená anotace (API neběží) — jen lokálně
    try {
      await updateAnnotation(id, payload)
    } catch {
      // Uložení selhalo → vrátit původní pozici. Nechat ji přesunutou jen lokálně
      // by lhalo: po reloadu by anotace skočila zpátky a nebylo by proč.
      if (original) replace(original)
    }
  }, [])

  return { annotations, create, erase, move }
}

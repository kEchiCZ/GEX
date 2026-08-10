/** Stav anotací per instrument + den (SPEC 7.4): načtení, kreslení, mazání, přesun, undo/redo.

Historie (#590) drží operace, ne snapshoty plochy: každý záznam ví, jak se sám vrátit
(vytvoření ↔ smazání, přesun ↔ původní pozice). Nová operace zahodí redo zásobník.

Pozor na `id`: undo vytvoření anotaci SMAŽE, redo ji vytvoří znovu a server přidělí NOVÉ
`id`. Historie proto po re-vytvoření staré `id` ve svých záznamech přemapuje, jinak by další
undo mířilo na anotaci, která už neexistuje.
*/
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createAnnotation,
  deleteAnnotation,
  listAnnotations,
  updateAnnotation,
} from '../api/annotations'
import type { AnnotationPayload, StoredAnnotation } from './model'

/** Jedna vratná operace nad plochou. */
type HistoryEntry =
  | { kind: 'create'; id: number; payload: AnnotationPayload }
  | { kind: 'erase'; id: number; payload: AnnotationPayload }
  | { kind: 'move'; id: number; before: AnnotationPayload; after: AnnotationPayload }

export interface AnnotationsState {
  annotations: StoredAnnotation[]
  create: (payload: AnnotationPayload) => Promise<void>
  erase: (id: number) => Promise<void>
  /** Přesun tažením (#589): nový payload téže anotace, `id` se nemění. */
  move: (id: number, payload: AnnotationPayload) => Promise<void>
  /** Zpět / vpřed (#590); `canUndo`/`canRedo` řídí disabled stav tlačítek. */
  undo: () => Promise<void>
  redo: () => Promise<void>
  canUndo: boolean
  canRedo: boolean
}

export function useAnnotations(symbol: string, date: string): AnnotationsState {
  const [annotations, setAnnotations] = useState<StoredAnnotation[]>([])
  // Zrcadlo stavu pro čtení mimo setState updater (updater musí zůstat čistý, #143)
  const annotationsRef = useRef<StoredAnnotation[]>([])
  useEffect(() => {
    annotationsRef.current = annotations
  }, [annotations])

  // Zásobníky historie žijí v refech (operace na ně sahají hned po sobě), do stavu
  // se propisují jen jejich hloubky — víc React vědět nepotřebuje
  const undoRef = useRef<HistoryEntry[]>([])
  const redoRef = useRef<HistoryEntry[]>([])
  const [depth, setDepth] = useState({ undo: 0, redo: 0 })
  const syncDepth = useCallback(() => {
    setDepth({ undo: undoRef.current.length, redo: redoRef.current.length })
  }, [])

  useEffect(() => {
    let cancelled = false
    // Jiný instrument/den = jiná plocha; historie té staré by mířila mimo
    undoRef.current = []
    redoRef.current = []
    setDepth({ undo: 0, redo: 0 })
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

  const payloadOf = useCallback(
    (id: number): AnnotationPayload | undefined =>
      annotationsRef.current.find((annotation) => annotation.id === id)?.payload,
    [],
  )

  /** Přepíše `id` v obou zásobnících — po re-vytvoření anotace (redo/undo mazání). */
  const remapId = useCallback((from: number, to: number) => {
    const remap = (entries: HistoryEntry[]): HistoryEntry[] =>
      entries.map((entry) => (entry.id === from ? { ...entry, id: to } : entry))
    undoRef.current = remap(undoRef.current)
    redoRef.current = remap(redoRef.current)
  }, [])

  // ── Primitiva bez historie ────────────────────────────────────────

  const applyCreate = useCallback(
    async (payload: AnnotationPayload): Promise<number> => {
      try {
        const stored = await createAnnotation(symbol, date, payload)
        setAnnotations((previous) => [...previous, stored])
        return stored.id
      } catch {
        // Bez API aspoň lokálně (záporné id = neuložená)
        const id = -Date.now()
        setAnnotations((previous) => [...previous, { id, payload }])
        return id
      }
    },
    [symbol, date],
  )

  const applyErase = useCallback(async (id: number) => {
    setAnnotations((previous) => previous.filter((annotation) => annotation.id !== id))
    if (id > 0) {
      await deleteAnnotation(id).catch(() => {
        // Server o anotaci přijde při příštím načtení; lokálně už je pryč
      })
    }
  }, [])

  /** `false` = uložení selhalo a pozice se vrátila (do historie takový krok nepatří). */
  const applyMove = useCallback(
    async (id: number, payload: AnnotationPayload): Promise<boolean> => {
      const replace = (next: AnnotationPayload) =>
        setAnnotations((previous) =>
          previous.map((annotation) => (annotation.id === id ? { id, payload: next } : annotation)),
        )
      const original = payloadOf(id)
      replace(payload)
      if (id < 0) return true // neuložená anotace (API neběží) — jen lokálně
      try {
        await updateAnnotation(id, payload)
        return true
      } catch {
        // Uložení selhalo → vrátit původní pozici. Nechat ji přesunutou jen lokálně
        // by lhalo: po reloadu by anotace skočila zpátky a nebylo by proč.
        if (original) replace(original)
        return false
      }
    },
    [payloadOf],
  )

  /** Nová uživatelská operace: na undo zásobník, redo se zahazuje. */
  const record = useCallback(
    (entry: HistoryEntry) => {
      undoRef.current = [...undoRef.current, entry]
      redoRef.current = []
      syncDepth()
    },
    [syncDepth],
  )

  // ── Veřejné operace ───────────────────────────────────────────────

  const create = useCallback(
    async (payload: AnnotationPayload) => {
      const id = await applyCreate(payload)
      record({ kind: 'create', id, payload })
    },
    [applyCreate, record],
  )

  const erase = useCallback(
    async (id: number) => {
      const payload = payloadOf(id)
      await applyErase(id)
      if (payload) record({ kind: 'erase', id, payload })
    },
    [applyErase, payloadOf, record],
  )

  const move = useCallback(
    async (id: number, payload: AnnotationPayload) => {
      const before = payloadOf(id)
      const stored = await applyMove(id, payload)
      if (before && stored) record({ kind: 'move', id, before, after: payload })
    },
    [applyMove, payloadOf, record],
  )

  const undo = useCallback(async () => {
    const entry = undoRef.current.at(-1)
    if (!entry) return
    undoRef.current = undoRef.current.slice(0, -1)
    redoRef.current = [...redoRef.current, entry]
    syncDepth()
    if (entry.kind === 'create') {
      await applyErase(entry.id)
    } else if (entry.kind === 'erase') {
      const id = await applyCreate(entry.payload)
      if (id !== entry.id) remapId(entry.id, id)
    } else {
      await applyMove(entry.id, entry.before)
    }
    syncDepth()
  }, [applyCreate, applyErase, applyMove, remapId, syncDepth])

  const redo = useCallback(async () => {
    const entry = redoRef.current.at(-1)
    if (!entry) return
    redoRef.current = redoRef.current.slice(0, -1)
    undoRef.current = [...undoRef.current, entry]
    syncDepth()
    if (entry.kind === 'create') {
      const id = await applyCreate(entry.payload)
      if (id !== entry.id) remapId(entry.id, id)
    } else if (entry.kind === 'erase') {
      await applyErase(entry.id)
    } else {
      await applyMove(entry.id, entry.after)
    }
    syncDepth()
  }, [applyCreate, applyErase, applyMove, remapId, syncDepth])

  return {
    annotations,
    create,
    erase,
    move,
    undo,
    redo,
    canUndo: depth.undo > 0,
    canRedo: depth.redo > 0,
  }
}

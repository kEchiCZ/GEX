/** Stohování cenovek úrovní (#238): dvě pojmenované úrovně na témže striku
(např. Max Pain = call zeď) by si popisky překreslily a jeden by nešel číst.
Kolidující štítky se odsunou pod sebe jako popisky seancí (#193) — čistá
funkce nad svislými boxy, bez canvasu. */

export interface LabelBox {
  /** Horní hrana boxu v px (souřadnice canvasu, roste dolů). */
  y: number
  height: number
}

/** Vrátí nové horní hrany boxů tak, aby se nepřekrývaly.

- Pořadí shora dolů = pořadí podle vstupního `y` (při shodě pořadí vstupu),
  takže štítek výše položené úrovně zůstává nad štítkem nižší.
- Box se posouvá jen DOLŮ a jen o nejmenší nutný kus — nekolidující štítky
  se nehnou z místa (štítek sedí u své čáry).
- Výsledek je indexovaný jako vstup. */
export function stackLabelRows(boxes: readonly LabelBox[], gap = 0): number[] {
  const order = boxes
    .map((box, index) => ({ box, index }))
    .sort((a, b) => a.box.y - b.box.y || a.index - b.index)
  const result: number[] = new Array<number>(boxes.length).fill(0)
  let cursor = -Infinity
  for (const { box, index } of order) {
    const top = Math.max(box.y, cursor)
    result[index] = top
    cursor = top + box.height + gap
  }
  return result
}

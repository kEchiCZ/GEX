/** Vitest setup: jsdom polyfilly + izolace localStorage. */
import { beforeEach } from 'vitest'

// Persistence UI voleb (ADR-0007) jinak prosakuje mezi testy v jednom souboru —
// jsdom drží localStorage po celý běh souboru
beforeEach(() => window.localStorage.clear())

// jsdom neimplementuje PointerEvent — bez něj fireEvent.pointerMove nenese souřadnice
if (typeof window !== 'undefined' && window.PointerEvent === undefined) {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number
    constructor(type: string, init: MouseEventInit & { pointerId?: number } = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 0
    }
  }
  Object.defineProperty(window, 'PointerEvent', { value: PointerEventPolyfill })
}

// setPointerCapture v jsdom chybí
if (typeof Element !== 'undefined' && !Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}

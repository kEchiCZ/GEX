/** Web worker pro kontury (#493): blur + marching squares mimo main thread.

Zpráva dovnitř: { id, buffer (transferable), width, height, mode }.
Zpráva ven: { id, buffer } — segmenty jako plochý Float32Array (transferable).
*/
import { computeContourSegments, segmentsToFlat } from './contourCompute'
import type { ContoursMode } from './contours'

interface ContourRequest {
  id: number
  buffer: ArrayBuffer
  width: number
  height: number
  mode: ContoursMode
}

self.addEventListener('message', (event: MessageEvent<ContourRequest>) => {
  const { id, buffer, width, height, mode } = event.data
  const segments = computeContourSegments(new Float32Array(buffer), width, height, mode)
  const flat = segmentsToFlat(segments)
  // postMessage workeru: druhý argument = transfer list (typ z DOM lib sedí)
  self.postMessage({ id, buffer: flat.buffer }, { transfer: [flat.buffer] })
})

/** Web worker pro kontury (#493, #1222): blur + marching squares + napojení
do polylinií mimo main thread.

Zpráva dovnitř: { id, buffer (transferable), width, height, mode }.
Zpráva ven: { id, buffer } — polylinie kódované do Float32Array (transferable).
*/
import { computeContourPolylines } from './contourCompute'
import type { ContoursMode } from './contours'
import { encodePolylines } from './polylines'

interface ContourRequest {
  id: number
  buffer: ArrayBuffer
  width: number
  height: number
  mode: ContoursMode
}

self.addEventListener('message', (event: MessageEvent<ContourRequest>) => {
  const { id, buffer, width, height, mode } = event.data
  const polylines = computeContourPolylines(new Float32Array(buffer), width, height, mode)
  const flat = encodePolylines(polylines)
  // postMessage workeru: druhý argument = transfer list (typ z DOM lib sedí)
  self.postMessage({ id, buffer: flat.buffer }, { transfer: [flat.buffer] })
})

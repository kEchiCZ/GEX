/** Web worker pro kontury (#493, #1222): blur + marching squares + napojení
do polylinií mimo main thread.

Zpráva dovnitř: { id, buffer (transferable), width, height, modes }.
Zpráva ven: { id, buffers } — polylinie každého módu kódované do Float32Array
(transferable), v pořadí `modes`. Hladiny i flip jdou jednou úlohou nad
sdíleným blurem (#1420).
*/
import { computeContourPolylinesForModes } from './contourCompute'
import type { ContoursMode } from './contours'
import { encodePolylines } from './polylines'

interface ContourRequest {
  id: number
  buffer: ArrayBuffer
  width: number
  height: number
  modes: ContoursMode[]
}

self.addEventListener('message', (event: MessageEvent<ContourRequest>) => {
  const { id, buffer, width, height, modes } = event.data
  const sets = computeContourPolylinesForModes(new Float32Array(buffer), width, height, modes)
  const buffers = sets.map((polylines) => encodePolylines(polylines).buffer)
  // postMessage workeru: druhý argument = transfer list (typ z DOM lib sedí)
  self.postMessage({ id, buffers }, { transfer: buffers })
})

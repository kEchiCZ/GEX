/** ISO datum běžící obchodní seance (Globex, #512), které se samo překlopí.

Po 17:00 CT běží seance zítřka. Kontrola 1×/min (#508): pohled otevřený přes
hranici seance se překlopí na nový den, místo aby do reloadu ukazoval
včerejšek. Re-render jen při změně dne. */
import { useEffect, useState } from 'react'
import { sessionDateIso } from '../instrument/tz'

export function useSessionDate(checkMs: number = 60_000): string {
  const [today, setToday] = useState(() => sessionDateIso())
  useEffect(() => {
    const timer = window.setInterval(() => {
      const current = sessionDateIso()
      setToday((previous) => (previous === current ? previous : current))
    }, checkMs)
    return () => window.clearInterval(timer)
  }, [checkMs])
  return today
}

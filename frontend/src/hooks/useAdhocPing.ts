/** Držení ad-hoc pohledu naživu (#521 C): symbol mimo watchlist se pinguje
à 1 min — engine bez prodloužení pohled po ~3 min uklidí a subskripce
vypadnou (AC uvolnění kapacity). */
import { useEffect } from 'react'
import { requestAdhoc } from '../components/SymbolSearch'

export function useAdhocPing(symbol: string, watched: boolean): void {
  useEffect(() => {
    if (watched || symbol === '') return
    void requestAdhoc(symbol)
    const timer = window.setInterval(() => void requestAdhoc(symbol), 60_000)
    return () => window.clearInterval(timer)
  }, [symbol, watched])
}

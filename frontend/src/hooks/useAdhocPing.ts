/** Držení ad-hoc pohledu naživu (#521 C): symbol mimo watchlist se pinguje
à 1 min — engine bez prodloužení pohled po ~3 min uklidí a subskripce
vypadnou (AC uvolnění kapacity). */
import { useEffect } from 'react'
import { requestAdhoc } from '../components/SymbolSearch'

/** `watched === null` = watchlist ještě není načtený — nepingovat: první
render po restartu API měl prázdný watchlist, ping založil ad-hoc pohled
pro symbol s plnou pipeline a chip „ad-hoc · tastytrade" visel nad IBKR daty (4. 9.). */
export function useAdhocPing(symbol: string, watched: boolean | null): void {
  useEffect(() => {
    if (watched !== false || symbol === '') return
    void requestAdhoc(symbol)
    const timer = window.setInterval(() => void requestAdhoc(symbol), 60_000)
    return () => window.clearInterval(timer)
  }, [symbol, watched])
}

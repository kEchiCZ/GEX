"""CVD podkladu — kumulativní objemová delta futures (#829).

Doplněk k `cumdelta.py`: ten měří **opční** delta tok (kolik delty se protočilo
přes opce), tohle měří tok v **podkladu** (agresivní nákupy − prodeje futures
kontraktů). Dvě různé věci, které spolu nemusí korelovat — a právě jejich
rozchod je informace, kterou ani jedna řada sama nedá.

Zdroj je dxFeed `TimeAndSale` na streamer symbolu front futures, který engine
už odebírá kvůli fallbacku spotu (#614). Event nese `aggressorSide` přímo od
burzy, takže odpadá Lee–Ready odhad — na rozdíl od opční větve tu nic
neklasifikujeme, jen sčítáme. Surové printy se NEUKLÁDAJÍ (podklad jich má
miliony za den, viz `trades_recorder`); drží se jen minutový agregát.

Kotva je táž jako u CumΔ: open Globex seance (#638), aby obě řady seděly na
tutéž osu obchodního dne a daly se číst proti sobě. Tisk se do kumulativu
přičte až uzávěrkou minuty (#1603, vzor stínu `tasty/cumdelta_dx`): roll
seance tak tisky po openu nezahodí a po restartu se kumulativ naváže z flow
partice (`restore_cum`).
"""

import datetime as dt
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Hodnoty `aggressorSide` z dxFeed → znaménko toku
_AGGRESSOR_SIGN = {"BUY": 1.0, "SELL": -1.0}


@dataclass(frozen=True)
class CvdRow:
    """Minutový bod řady CVD podkladu."""

    ts_min: dt.datetime
    #: Čistý objem minuty (buy − sell, v kontraktech)
    cvd_delta: float
    #: Kumulativ od open seance
    cvd: float
    #: Podíl printů s vyplněným agresorem (0–1); nízká hodnota = řada je slepá
    aggressor_share: float | None


class FuturesCvdTracker:
    """Denní agregátor CVD podkladu přes dxFeed TimeAndSale.

    Sleduje jen registrované streamer symboly (front futures per instrument),
    ostatní eventy ignoruje — týmž callbackem tečou i všechny opční printy.
    """

    def __init__(self) -> None:
        #: streamer symbol → instrument (např. "/ESU6:XCME" → "ES")
        self._streamers: dict[str, str] = {}
        self._cum: dict[str, float] = {}
        self._minute: dict[str, float] = {}
        self._trades: dict[str, int] = {}
        self._with_aggressor: dict[str, int] = {}
        self._session_date: dt.date | None = None

    def register(self, symbol: str, streamer: str) -> None:
        """Naváže streamer front futures na instrument; opakované volání je no-op.

        Streamer se mění s rolováním kontraktu — starý se odregistruje, aby po
        rollu nepřitékal tok expirujícího kontraktu do nové řady.
        """
        if self._streamers.get(streamer) == symbol:
            return
        for existing, owner in list(self._streamers.items()):
            if owner == symbol and existing != streamer:
                del self._streamers[existing]
                logger.info("CVD podkladu %s: roll streameru %s → %s", symbol, existing, streamer)
        self._streamers[streamer] = symbol

    def on_event(self, event_type: str, values: list[object]) -> None:
        """EventCallback DxLinkStreamu — pořadí polí dle `tasty.stream.EVENT_FIELDS`."""
        if event_type != "TimeAndSale" or not values:
            return
        symbol = self._streamers.get(str(values[0]))
        if symbol is None:
            return  # opční print nebo neregistrovaný symbol
        self._trades[symbol] = self._trades.get(symbol, 0) + 1
        size = _number(values[3]) if len(values) > 3 else None
        aggressor = values[4] if len(values) > 4 else None
        sign = _AGGRESSOR_SIGN.get(aggressor.upper()) if isinstance(aggressor, str) else None
        if sign is None or size is None:
            return  # print bez agresora se do toku nepočítá (ale je v `trades`)
        self._with_aggressor[symbol] = self._with_aggressor.get(symbol, 0) + 1
        # Jen do minuty — do kumulativu až `close_minute` (#1603)
        self._minute[symbol] = self._minute.get(symbol, 0.0) + sign * size

    def close_minute(self, symbol: str, ts_min: dt.datetime) -> CvdRow:
        """Uzavře minutu: přičte ji do kumulativu, vrátí bod řady a vynuluje minutu."""
        trades = self._trades.get(symbol, 0)
        share = (self._with_aggressor.get(symbol, 0) / trades) if trades else None
        minute = self._minute.get(symbol, 0.0)
        self._cum[symbol] = self._cum.get(symbol, 0.0) + minute
        row = CvdRow(
            ts_min=ts_min,
            cvd_delta=minute,
            cvd=self._cum[symbol],
            aggressor_share=share,
        )
        self._minute[symbol] = 0.0
        self._trades[symbol] = 0
        self._with_aggressor[symbol] = 0
        return row

    def roll_session(self, session_date: dt.date) -> bool:
        """Reset kumulativu na hranici Globex seance (#638) — stejná kotva jako CumΔ.

        Maže jen kumulativy (#1603): rozdělaná minuta nese tisky od openu, které
        přišly před prvním cyklem nové seance (callback streamu běží mimo
        minutový cyklus), a ty patří do první minuty nové seance. Volá ho
        primární runtime prvního instrumentu, takže reset platí i pro ostatní.
        První volání po startu jen zafixuje seanci BEZ resetu — úroveň běžící
        seance naváže `restore_cum`.
        """
        if self._session_date == session_date:
            return False
        first = self._session_date is None
        self._session_date = session_date
        if first:
            return False
        self._cum.clear()
        return True

    def has_level(self, symbol: str) -> bool:
        """Má instrument v běžící seanci úroveň (uzavřená minuta nebo navázání)?"""
        return symbol in self._cum

    def restore_cum(self, symbol: str, base: float) -> bool:
        """Naváže kumulativ z flow partice po restartu uprostřed seance (#638, #1603).

        Jen instrument, který v seanci ještě úroveň nemá — pipeline založená
        znovu uprostřed seance (roll expirace v settle, návrat do watchlistu)
        sdílí tracker a základ by jinak přičetla podruhé. Vrací, zda navázal.
        """
        if symbol in self._cum:
            return False
        self._cum[symbol] = base
        return True

    def is_tracking(self, symbol: str) -> bool:
        """Má instrument registrovaný streamer? Bez něj je řada jen prázdná."""
        return symbol in self._streamers.values()

    def status_fields(self) -> dict[str, object]:
        """Diagnostika do /status: co který instrument sleduje a kolik toho vidí.

        Bez tohohle nejde odlišit tři různé stavy, které v parquet vypadají
        stejně (CVD = 0): streamer není registrovaný, registrovaný je ale
        printy nechodí, nebo chodí bez `aggressorSide`.
        """
        per_symbol: dict[str, object] = {}
        for streamer, symbol in self._streamers.items():
            trades = self._trades.get(symbol, 0)
            per_symbol[symbol] = {
                "streamer": streamer,
                "trades_minute": trades,
                "with_aggressor_minute": self._with_aggressor.get(symbol, 0),
                # Živá úroveň: uzavřené minuty + rozdělaná (do kumulativu jde
                # až `close_minute`, #1603)
                "cum": round(self._cum.get(symbol, 0.0) + self._minute.get(symbol, 0.0), 1),
            }
        return {"futures_cvd": per_symbol}


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN → None

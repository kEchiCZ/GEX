"""OI zdi — hladiny z denního open interestu (#851).

Doplněk ke gamma zdím z `levels.py`, ne jejich náhrada. Rozdíl je v tom, co
každá veličina znamená:

* **gamma zeď** (`call_wall`/`put_wall`) je maximum NetGEX profilu, tedy místo,
  kde dealeři hedgují NEJVÍC PRÁVĚ TEĎ — působí okamžitě přes tok,
* **OI zeď** je maximum otevřeného zájmu; chová se spíš jako magnet k expiraci
  a bod, kde se pozice zavírají.

Proč vůbec: gamma profil se počítá jen nad IBKR obálkou striků (±200 b, strop
100 market data lines), kdežto denní OI máme díky tastytrade přes celý řetěz
(#828). Masa OTM putů tak leží mimo dosah gamma zdí — a trader podle zdi
umisťuje stop, takže zeď na špatném místě stojí peníze přímo.

Záměrně se tu **nic nedopočítává**: žádná IV, žádná gamma, žádný model.
Odhadnout gammu z extrapolované volatility (varianty A/B v #851) by vyrobilo
čáru, která vypadá jako měřená, ale stojí na dohadu o skew — a to zrovna
v křídlech, kde je skew nejnelineárnější. Otevřený zájem je tvrdé číslo
od burzy; když ho nemáme, hladina prostě není.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OiWalls:
    """Hladiny z denního OI; None = na dané straně není žádné OI."""

    call: float | None
    put: float | None
    #: Podíl zdi na celkovém OI své strany (0–1) — koncentrovaný profil ~1,
    #: rozprostřený ~1/N. Nízká hodnota znamená, že „zeď" je jen nejvyšší
    #: z mnoha srovnatelných striků a nemá cenu ji číst jako úroveň.
    call_share: float | None = None
    put_share: float | None = None


def compute_oi_walls(oi_by_strike_right: Mapping[tuple[float, str], float], spot: float) -> OiWalls:
    """Strike s největším OI nad spotem (call) a pod ním (put).

    Strany se počítají zvlášť a každá jen ze svých kontraktů: call zeď z call
    OI nad spotem, put zeď z put OI pod ním. Míchat obě strany na jednom
    striku by smazalo právě tu informaci, kvůli které se hladina kreslí.

    Remízu láme nižší strike (deterministicky, stejně jako `max_pain_strike`).
    """
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for (strike, right), oi in oi_by_strike_right.items():
        if oi <= 0.0:
            continue  # nula ani záporná hodnota zeď netvoří
        if right == "C" and strike > spot:
            calls[strike] = calls.get(strike, 0.0) + oi
        elif right == "P" and strike < spot:
            puts[strike] = puts.get(strike, 0.0) + oi

    call_wall, call_share = _peak(calls)
    put_wall, put_share = _peak(puts)
    return OiWalls(call=call_wall, put=put_wall, call_share=call_share, put_share=put_share)


def _peak(side: Mapping[float, float]) -> tuple[float | None, float | None]:
    """Strike s největším OI + jeho podíl na součtu strany."""
    if not side:
        return None, None
    total = sum(side.values())
    if total <= 0.0:
        return None, None
    # Remíza → nižší strike (sorted zajistí determinismus)
    best = max(sorted(side), key=lambda strike: side[strike])
    return best, side[best] / total

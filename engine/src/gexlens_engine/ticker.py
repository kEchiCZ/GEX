"""Ticker instrumentu (#1191, ADR-0041): kořen produktu, nebo konkrétní kontrakt.

`ES` = kořen — engine sleduje front kontrakt podle roll pravidla (ADR-0039,
`GEXLENS_FRONT_ROLL_DAYS`). `ESU6` = pinovaný kontrakt (kód měsíce + poslední
číslice roku) — engine sleduje přesně tento futures kontrakt a jeho opční
řetěz, dokud neexpiruje. Pinovaný kontrakt automatický NAHRAZUJE (stejný
počet pipeline a market data lines, ADR-0001), neběží vedle něj.

Ticker je klíč všeho, co pipeline ukládá a publikuje (partice, OI archiv,
setupy, deník, kanály WS). Na kořen se převádí JEN na hranici IBKR
(`Future(root)`, `FuturesOption(root, …)`, `reqSecDefOptParams(root, …)`),
tastytrade (`product-code=root`, `/futures-option-chains/root`) a v UI
(lidský název produktu) — jedno místo převodu, žádná další větev v enginu.
"""

import re
from dataclasses import dataclass

#: Kódy měsíců CME (F–Z); kvartální cyklus indexů je H/M/U/Z, energie měsíční
MONTH_CODES = "FGHJKMNQUVXZ"
_TICKER_RE = re.compile(rf"^(?P<root>[A-Z0-9]{{1,4}})(?P<month>[{MONTH_CODES}])(?P<year>\d)$")
_ROOT_RE = re.compile(r"^[A-Z0-9]{1,6}$")


@dataclass(frozen=True)
class Ticker:
    """Rozložený ticker: `root` = produkt (ES), `contract` = kód kontraktu (U6) nebo None."""

    root: str
    contract: str | None

    @property
    def pinned(self) -> bool:
        return self.contract is not None

    @property
    def symbol(self) -> str:
        return f"{self.root}{self.contract}" if self.contract else self.root

    @property
    def local_symbol(self) -> str | None:
        """TWS lokální symbol pinovaného kontraktu (`ESU6`); None u kořene."""
        return self.symbol if self.contract else None


def parse_ticker(raw: str) -> Ticker:
    """`ES` → kořen, `ESU6` → kontrakt U6; jinak ValueError (validace watchlistu)."""
    text = raw.strip().upper()
    if not text:
        raise ValueError("prázdný ticker")
    match = _TICKER_RE.match(text)
    if match and len(text) >= 3:
        return Ticker(root=match.group("root"), contract=match.group("month") + match.group("year"))
    if _ROOT_RE.match(text):
        return Ticker(root=text, contract=None)
    raise ValueError(f"neplatný ticker {raw!r} — čekám kořen (ES) nebo kontrakt (ESZ6)")


#: CME produkty, které aplikace zná jako futures (podklad = futures kontrakt,
#: opce = FOP). Cokoli jiného je akcie / ETF / index (#206): podklad = symbol
#: sám, opce z `/option-chains/{symbol}/nested` (OPRA přes tastytrade).
CME_FUTURES_ROOTS: frozenset[str] = frozenset(
    {
        "ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM",
        "CL", "NG", "GC", "SI", "HG", "ZB", "ZN", "ZF", "6E", "6J", "ZC", "ZS", "ZW",
    }
)  # fmt: skip


def is_futures(symbol: str) -> bool:
    """Futures produkt (CME) vs. akcie/ETF/index (#206) — podle kořene tickeru."""
    return symbol_root(symbol) in CME_FUTURES_ROOTS


def symbol_root(symbol: str) -> str:
    """Kořen produktu z tickeru; nečitelný ticker vrací beze změny (uppercase)."""
    try:
        return parse_ticker(symbol).root
    except ValueError:
        return symbol.strip().upper()


def pinned_contract(symbol: str) -> str | None:
    """TWS lokální symbol pinovaného kontraktu (`ESU6`), None u kořene/nečitelného."""
    try:
        return parse_ticker(symbol).local_symbol
    except ValueError:
        return None

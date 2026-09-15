"""Push notifikace mimo prohlížeč přes Telegram bota (#1175, #1126 bod 3b).

Zvonek žije jen v otevřeném tabu; uživatel chce alerty na mobil. Rozhodnutí
15. 9. 2026: Telegram (klienta má, bot = jeden HTTPS POST, žádná knihovna).
Signal nemá bot API, ntfy/Web Push by chtěly další aplikaci nebo hosting.

Jedna fronta, dva výstupy: zdroj je TENTÝŽ kanál `alerts`, který plní
zvonek (engine i news-engine ho posílají přes `/internal/publish`, provozní
alerty API přes `AlertEngine`) — `LiveHub` zavolá posluchače, tady se
rozhodne a pošle. Žádná nová rozhodovací logika: co nezvoní, neodejde.

Pravidla:
- Kategorie podle `kind` (setup / ops / news / info), každá s přepínačem
  v serverových nastaveních (Settings → Notifikace); `info` default vypnuto.
- Setup jen `event=created` a s confidence ≥ práh (env), uzavření ne.
- Tiché hodiny (env, default 23:00–06:00 Europe/Prague) — provozní alerty
  jdou i v noci (pád enginu nepočká), ostatní ne.
- Dedup per (kind, symbol, klíč zprávy) po 10 min, denní strop zpráv.
- Token a chat id jen z `.env`; hlavička ani log token nikdy nenesou,
  URL s tokenem se do logu nepíše (Telegram ho má v cestě).
- Odeslání běží v daemon vlákně mimo publish — WS klienti nečekají na síť.
"""

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_S = 5.0
RETRIES = 3
DEDUP_WINDOW_S = 600.0
MESSAGE_MAX_CHARS = 900

#: Kategorie push zpráv; klíče serverových nastavení = `push_telegram_<kategorie>`
PUSH_CATEGORIES: tuple[str, ...] = ("setup", "ops", "news", "info")
PUSH_SETTING_KEYS: frozenset[str] = frozenset(f"push_telegram_{c}" for c in PUSH_CATEGORIES)
PUSH_DEFAULTS: dict[str, bool] = {"setup": True, "ops": True, "news": True, "info": False}

#: Provozní druhy: výpadky a degradace — jdou i v tichých hodinách
OPS_KINDS: frozenset[str] = frozenset(
    {
        "disconnect",
        "disk_limit",
        "disk_space",
        "connection_stall",
        "degraded_start",
        "competing_session",
        "feed_backup_dead",
        "instrument_error",
        "subscription_error",
        "greeks_stalled",
        "greeks_suspect",
        "strikes_stalled",
        "oi_refresh_failed",
        "oi_missing",
        "spot_fallback",
        "chain_fallback",
        "broker",
        "setup_degraded",
        "scenario_disk",
    }
)
NEWS_KINDS: frozenset[str] = frozenset({"news_anomaly", "vol_concentration"})


def category_of(kind: str) -> str:
    # Výsledek scénáře dne (#1173) patří k obchodním věcem uživatele — s setupy
    if kind in ("setup", "scenario_result", "scenario_created"):
        return "setup"
    if kind in OPS_KINDS:
        return "ops"
    if kind in NEWS_KINDS:
        return "news"
    return "info"


def parse_quiet_hours(spec: str) -> tuple[dt.time, dt.time] | None:
    """`HH:MM-HH:MM` v lokálním čase; prázdné/nesmyslné = bez tichých hodin."""
    try:
        start_text, end_text = spec.strip().split("-")
        start = dt.time.fromisoformat(start_text.strip())
        end = dt.time.fromisoformat(end_text.strip())
    except ValueError:
        return None
    return (start, end)


def in_quiet_hours(now_local: dt.time, window: tuple[dt.time, dt.time] | None) -> bool:
    """Okno přes půlnoc (23:00–06:00) i v rámci dne (12:00–13:00)."""
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now_local < end
    return now_local >= start or now_local < end


@dataclass(frozen=True)
class PushOptions:
    token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    quiet_hours: str = "23:00-06:00"
    daily_cap: int = 200
    setup_min_confidence: float = 0.0
    timezone: str = "Europe/Prague"

    @property
    def configured(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())

    @classmethod
    def from_settings(cls, settings: Any) -> "PushOptions":
        return cls(
            token=str(settings.push_telegram_token or "").strip(),
            chat_id=str(settings.push_telegram_chat_id or "").strip(),
            quiet_hours=str(settings.push_quiet_hours or ""),
            daily_cap=int(settings.push_daily_cap),
            setup_min_confidence=float(settings.push_setup_min_confidence),
        )


def format_message(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind", "alert"))
    symbol = str(payload.get("symbol") or "").strip()
    text = str(payload.get("message") or "").strip()
    prefix = {"setup": "🎯", "ops": "⚠️", "news": "📰"}.get(category_of(kind), "ℹ️")
    head = f"{prefix} GEXLens"
    if symbol and symbol != "*":
        head += f" · {symbol}"
    body = f"{head}\n{text}" if text else f"{head}\n{kind}"
    return body[:MESSAGE_MAX_CHARS]


class TelegramPush:
    """Rozhodne, jestli alert odejde na Telegram, a pošle ho (mimo publish vlákno)."""

    def __init__(
        self,
        options: PushOptions,
        settings_reader: Callable[[], dict[str, Any]],
        *,
        post: Callable[..., httpx.Response] | None = None,
        clock: Callable[[], float] = time.time,
        run_in_thread: bool = True,
    ) -> None:
        self._options = options
        self._settings_reader = settings_reader
        self._post = post
        self._clock = clock
        self._run_in_thread = run_in_thread
        self._quiet = parse_quiet_hours(options.quiet_hours)
        self._tz = ZoneInfo(options.timezone)
        self._lock = threading.Lock()
        self._recent: dict[tuple[str, str, str], float] = {}
        self._day: dt.date | None = None
        self._sent_today = 0
        self.last_error: str | None = None
        self.last_sent_at: float | None = None

    # ── stav pro /push/status ─────────────────────────────────────
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": self._options.configured,
                "quiet_hours": self._options.quiet_hours,
                "daily_cap": self._options.daily_cap,
                "sent_today": self._sent_today if self._day == self._today() else 0,
                "last_sent_at": self.last_sent_at,
                "last_error": self.last_error,
                "categories": self._enabled_categories(),
            }

    def _today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self._clock(), self._tz).date()

    def _enabled_categories(self) -> dict[str, bool]:
        try:
            stored = self._settings_reader()
        except Exception:  # noqa: BLE001 — DB výpadek nesmí shodit publish
            logger.exception("Push: čtení nastavení selhalo — beru defaulty")
            stored = {}
        result: dict[str, bool] = {}
        for category in PUSH_CATEGORIES:
            value = stored.get(f"push_telegram_{category}")
            result[category] = PUSH_DEFAULTS[category] if not isinstance(value, bool) else value
        return result

    # ── rozhodnutí ────────────────────────────────────────────────
    def decide(self, payload: dict[str, Any]) -> str | None:
        """Vrátí důvod, proč alert NEODEJDE, nebo None (odejde). Čisté rozhodnutí."""
        if not self._options.configured:
            return "bez tokenu/chat id"
        kind = str(payload.get("kind") or "")
        if not kind:
            return "bez kind"
        category = category_of(kind)
        if kind == "setup":
            if payload.get("event") not in (None, "created"):
                return "setup: jen vznik"
            confidence = payload.get("confidence")
            if (
                isinstance(confidence, int | float)
                and float(confidence) < self._options.setup_min_confidence
            ):
                return f"setup: confidence {confidence} < práh"
        if not self._enabled_categories().get(category, False):
            return f"kategorie {category} vypnuta"
        now = self._clock()
        local = dt.datetime.fromtimestamp(now, self._tz)
        if category != "ops" and in_quiet_hours(local.time(), self._quiet):
            return "tiché hodiny"
        key = (kind, str(payload.get("symbol") or ""), str(payload.get("message") or "")[:80])
        with self._lock:
            last = self._recent.get(key)
            if last is not None and now - last < DEDUP_WINDOW_S:
                return "duplicita do 10 min"
            today = local.date()
            if self._day != today:
                self._day = today
                self._sent_today = 0
            if self._options.daily_cap > 0 and self._sent_today >= self._options.daily_cap:
                return "denní strop"
            # Rezervace: počítá se hned, ať souběžné alerty strop nepřekročí
            self._recent[key] = now
            self._sent_today += 1
            if len(self._recent) > 500:
                cutoff = now - DEDUP_WINDOW_S
                self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        return None

    # ── odeslání ──────────────────────────────────────────────────
    def handle(self, payload: dict[str, Any]) -> None:
        """Posluchač `LiveHub` (kanál alerts): rozhodne synchronně, pošle mimo vlákno."""
        reason = self.decide(payload)
        if reason is not None:
            logger.debug("Push přeskočen (%s): %s", reason, payload.get("kind"))
            return
        text = format_message(payload)
        if self._run_in_thread:
            threading.Thread(target=self._send, args=(text,), daemon=True).start()
        else:
            self._send(text)

    def _send(self, text: str) -> bool:
        post = self._post or httpx.post
        url = TELEGRAM_URL.format(token=self._options.token)
        body = {"chat_id": self._options.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, RETRIES + 1):
            try:
                response = post(url, json=body, timeout=REQUEST_TIMEOUT_S)
            except httpx.HTTPError as exc:
                self.last_error = f"{type(exc).__name__}"
                logger.warning(
                    "Push Telegram pokus %d/%d: %s", attempt, RETRIES, type(exc).__name__
                )
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            if response.status_code == 200:
                with self._lock:
                    self.last_sent_at = self._clock()
                    self.last_error = None
                return True
            # 4xx = chyba konfigurace (token, chat id) — opakovat nemá smysl
            self.last_error = f"HTTP {response.status_code}"
            logger.error(
                "Push Telegram odmítnut: HTTP %d %.200s", response.status_code, response.text
            )
            if 400 <= response.status_code < 500:
                return False
            time.sleep(min(2.0 * attempt, 5.0))
        return False

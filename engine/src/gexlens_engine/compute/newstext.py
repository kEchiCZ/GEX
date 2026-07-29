"""Normalizace titulku a dedup klíč zpráv (SPEC 3.3) — sdílené enginem i news-engine.

Žije v enginu, protože `news-engine` na engine závisí (ne naopak) a broker
headlines z ticku 292 zachytává právě engine (#291, Tier D). Dvě implementace
téhož hashe by znamenaly, že tatáž story přijatá dvěma cestami skončí
v databázi dvakrát.
"""

import datetime as dt
import hashlib
import re
import unicodedata

# Slova bez významu pro shodu titulků — dedup je ignoruje, ať „Fed holds rates"
# a „The Fed holds rates" splynou
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Titulek na kanonický tvar: bez diakritiky, interpunkce a stopslov.

    Titulek složený jen ze stopslov se **nesmí** zredukovat na prázdný řetězec —
    kolidoval by se všemi ostatními.
    """
    folded = unicodedata.normalize("NFKD", title.casefold())
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = _SPACES.sub(" ", _NON_WORD.sub(" ", ascii_only)).strip().split(" ")
    kept = [w for w in words if w and w not in _STOPWORDS]
    return " ".join(kept) if kept else " ".join(words)


def dedup_hash(title: str, ts_event: dt.datetime) -> str:
    """Klíč pro idempotentní zápis: normalizovaný titulek + den události.

    Den v klíči musí být: `news_events.dedup_hash` je UNIQUE, takže samotný
    titulek by znamenal, že se opakující se událost nikdy nezapíše podruhé
    (měsíční „Core PCE", „Fed holds rates").

    Hrubost na den je záměr — tatáž story z více zdrojů v jeden den má splynout
    (cross-source merge). Případ přes půlnoc řeší rolling-window dedup před
    zápisem; tenhle hash je poslední pojistka proti opakovanému fetchi.
    """
    key = f"{normalize_title(title)}|{ts_event.date().isoformat()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# Limit sloupce `news_events.source_uid` (storage/sentiment.py) — sdílená
# konstanta, ať normalizace a schéma nemůžou rozejít
SOURCE_UID_MAX_LENGTH = 128


def normalize_source_uid(uid: str | None) -> str | None:
    """Uid delší než sloupec → deterministický SHA-256 hex (#380).

    RSS guid/link nebo FF `country|title|date` můžou limit 128 znaků přerůst
    a shodit celou dávku zápisu (StringDataRightTruncation). Hash drží
    identitu stabilní; prosté oříznutí by mohlo kolidovat mezi dvěma dlouhými
    uid se stejným prefixem.
    """
    if uid is None or len(uid) <= SOURCE_UID_MAX_LENGTH:
        return uid
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()

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

# Plné znění článku (#743) chodí od zdrojů jako HTML. Značky se strhávají hned
# při normalizaci — do DB ani do rysů modelu nepatří. `script`/`style` se musí
# vyhodit VČETNĚ obsahu, jinak by v textu zůstal kód a CSS.
_HTML_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(#\d+|#x[0-9a-f]+|[a-z]+);", re.I)

#: Kolik znaků plného textu se posílá modelu jako „první odstavec". Lead nese
#: většinu signálu; celý článek (~350 slov) by při dnešní velikosti korpusu
#: přidal hlavně boilerplate a přeučoval (#740 fáze 1, #743).
LEAD_CHARS = 400

#: Kolik znaků článku se vůbec UKLÁDÁ (#744). Průměrný článek má ~3,5 kB a
#: backfill za dva roky by tak zabral ~177 MB — nad rozpočtem, který na to
#: máme. Model přitom čte jen prvních LEAD_CHARS, takže 1 500 drží
#: čtyřnásobnou rezervu pro pozdější rozšíření a objem srazí na třetinu.
#: Ořezává se konzistentně živý stream i backfill, ať korpus není nesourodý.
BODY_MAX_CHARS = 1500


def strip_html(raw: str) -> str:
    """HTML článku → čistý text (#743).

    Vlastní implementace místo knihovny: potřebujeme setřít značky a entity,
    ne parsovat dokument, a další závislost v obraze kvůli tomuhle nestojí za
    to. `script` a `style` padají i s obsahem — bez toho by v textu zůstal
    JavaScript a CSS, což by modelu dodalo tisíce nesmyslných rysů.
    """
    if not raw:
        return ""
    text = _HTML_BLOCK.sub(" ", raw)
    text = _HTML_TAG.sub(" ", text)
    text = _HTML_ENTITY.sub(
        lambda match: {
            "amp": "&",
            "lt": "<",
            "gt": ">",
            "quot": '"',
            "apos": "'",
            "nbsp": " ",
        }.get(match.group(1).lower(), " "),
        text,
    )
    return _SPACES.sub(" ", text).strip()


def clip_body(text: str, *, limit: int = BODY_MAX_CHARS) -> str:
    """Ořez článku na ukládanou délku (#744) — na hranici věty, ne slova.

    Sdílí logiku s `lead_paragraph`, jen s jiným limitem: obojí je „vezmi
    smysluplný začátek textu", liší se jen tím, kolik ho je potřeba.
    """
    return lead_paragraph(text, limit=limit)


def lead_paragraph(body: str | None, *, limit: int = LEAD_CHARS) -> str:
    """První odstavec článku pro rysy modelu — ne celý text (#740, #743).

    Řez na hranici věty, ne uprostřed slova: uříznutá věta by vyrobila
    n-gramy, které v žádném jiném článku nevzniknou.
    """
    if not body:
        return ""
    text = body.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1].strip() if end > limit // 2 else cut.rsplit(" ", 1)[0].strip()


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

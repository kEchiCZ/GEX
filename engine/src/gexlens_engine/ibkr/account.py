"""Rozpoznání připojeného IBKR účtu (issue #446).

Aplikace nikde neukazovala, k jakému účtu je připojená. Uživatel si přepnul TWS
na paper, port změnil v Settings (které ho ale engine nečetl) a z hlavičky
usoudil, že jede naživo — štítek „Live" tam přitom znamená živá data vs. replay,
ne typ účtu.

IBKR paper účty mají prefix `DU` (a `DF` pro financial advisor paper), živé `U`.
Rozlišení podle prefixu je jediné, co jde získat bez dalšího dotazu na server.
"""

from dataclasses import dataclass

PAPER_PREFIXES = ("DU", "DF")


@dataclass(frozen=True)
class AccountInfo:
    """Připojený účet pro stavovou lištu a Settings."""

    #: Čísla účtů z `ib.managedAccounts()`; prázdné = TWS je zatím neposlala
    accounts: tuple[str, ...]
    paper: bool | None  #: None = nelze určit (bez účtů)

    @property
    def label(self) -> str:
        if not self.accounts:
            return "neznámý účet"
        kind = "paper" if self.paper else "živý"
        return f"{', '.join(mask_account(name) for name in self.accounts)} ({kind})"


def mask_account(name: str) -> str:
    """Číslo účtu pro veřejný stav — zůstane prefix a poslední tři znaky.

    Štítek jde do `/status`, který je bez autentizace (#542 M7). Rozlišit dva
    účty od sebe stačí koncovka; celé číslo je zbytečný identifikátor.
    """
    if len(name) <= 5:
        return name
    prefix = "DU" if name.upper().startswith(PAPER_PREFIXES) else name[:1]
    return f"{prefix}***{name[-3:]}"


def classify_accounts(accounts: object) -> AccountInfo:
    """AccountInfo z `ib.managedAccounts()`.

    Účet se bere jako paper, jen když jsou paper VŠECHNY — smíšený seznam by
    jinak mohl tvrdit „paper", zatímco data tečou z živého účtu.
    """
    if not isinstance(accounts, (list, tuple)):
        return AccountInfo(accounts=(), paper=None)
    names = tuple(str(item).strip() for item in accounts if str(item).strip())
    if not names:
        return AccountInfo(accounts=(), paper=None)
    paper = all(name.upper().startswith(PAPER_PREFIXES) for name in names)
    return AccountInfo(accounts=names, paper=paper)

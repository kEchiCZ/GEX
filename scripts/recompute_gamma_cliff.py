"""Přepočet tabulky gamma_cliff po změně metody měření (#576 fáze 1 fix).

Hrubá gamma z profilu místo |NetGEX| řetězu — historické řádky se přepíší,
metriky následující seance zůstávají. Spouští se z hostitele proti compose PG
(port 55432), data z `data/`:

    uv run python scripts/recompute_gamma_cliff.py \n        --db "postgresql+psycopg://gexlens:…@127.0.0.1:55432/gexlens" --data-dir data

Heslo nikdy nevypisuje ani neloguje.
"""

import argparse
import datetime as dt
import logging
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.gammacliff import GammaCliffCollector
from gexlens_engine.storage.gammacliff_store import GammaCliffRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, help="SQLAlchemy URL databáze (heslo se nevypisuje)")
    parser.add_argument("--data-dir", default="data", type=Path)
    parser.add_argument("--symbols", nargs="*", default=["ES", "NQ"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine = create_engine(args.db)
    repository = GammaCliffRepository(engine)
    repository.ensure_schema()
    now = dt.datetime.now(dt.UTC)
    for symbol in args.symbols:
        collector = GammaCliffCollector(
            symbol=symbol, repository=repository, db=engine, data_dir=args.data_dir
        )
        collector.recompute(now)


if __name__ == "__main__":
    main()

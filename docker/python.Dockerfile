# Sdílený image pro engine, API i news-engine (uv workspace, Python 3.12).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# Python bufferuje stdout po ~8 kB, když nejde do terminálu (#771). Za provozu
# to není poznat — engine chrlí řádky a buffer se plní za sekundy. Vyplave to
# až v poruše, kdy je výstupu málo: 18. 8. byl při osmihodinovém výpadku
# `docker logs` u enginu i API ÚPLNĚ prázdný, takže po příčině nezůstala stopa
# a běžící proces vypadal jako zaseklý. Patří to do image, ne do compose —
# prod i dev stavějí z tohoto Dockerfilu a nesmí se to rozejít.
# PYTHONFAULTHANDLER vypíše zásobník při tvrdém pádu interpretu; dump
# zaseknutého procesu za běhu umí `gexlens_engine.diagnostics` přes SIGUSR1.
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

# pg_dump pro zálohu DB z UI (#439). Musí být verze 16 kvůli serveru
# `postgres:16` — klient 15 z bookworm dump odmítne („server version mismatch"),
# proto oficiální PGDG repozitář místo distribučního balíku.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY engine/pyproject.toml engine/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY news-engine/pyproject.toml news-engine/pyproject.toml
COPY engine/src engine/src
COPY api/src api/src
COPY news-engine/src news-engine/src

RUN uv sync --all-packages --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Neprivilegovaný běh (#542): API je jediná služba dosažitelná zvenčí a má RW
# bind mount ./data — kompromitace pod rootem by znamenala zápis na hostitelský
# disk. Přepnutí na UID 10001 dělá entrypoint až po srovnání vlastnictví dat;
# `USER` v Dockerfile nestačí, podadresáře z dřívějška patří rootu.
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/gexlens gexlens \
    && mkdir -p /app/data \
    && chown -R gexlens:gexlens /app/data
COPY docker/entrypoint.sh /usr/local/bin/gexlens-entrypoint
RUN chmod +x /usr/local/bin/gexlens-entrypoint
ENV HOME=/home/gexlens
ENTRYPOINT ["/usr/local/bin/gexlens-entrypoint"]

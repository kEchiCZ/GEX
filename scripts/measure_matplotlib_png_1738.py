"""Paměť a čas serverového PNG přes matplotlib Agg — podklad ADR-0062 K1 (#1738).

Graf 1200 × 700 px s čarou o 400 bodech, pěti úrovněmi a dvěma zónami (řádově jako graf
plánu QQQ). Měří RSS procesu před importem, po importu a po kreslení. matplotlib ani psutil
nejsou v závislostech repa, proto běží v dočasném prostředí:
    uv run --no-project --python 3.12 --with matplotlib --with psutil \
        python scripts/measure_matplotlib_png_1738.py
2. 11. 2026 (Windows, matplotlib 3.11.2): +54 MB RSS, import 1,4 s (první běh 8,5 s kvůli
cache fontů), kreslení 0,22 s, PNG 40 kB.

`--render` (F4b #1744) změří navíc skutečný podproces kreslení `gexlens_api.chart_render`
přes `plan_chart.render_png`: dvakrát po sobě (první v novém prostředí staví cache fontů),
čas celého podprocesu včetně importu, špičku RSS podprocesu (`getrusage`, jen Linux) a PNG.
Potřebuje repo na PYTHONPATH (`engine/src`, `api/src`, `news-engine/src`), v kontejneru
z image `gex-python` s doinstalovaným matplotlib a psutil.
"""

import io
import math
import sys
import time

import psutil

proc = psutil.Process()
rss0 = proc.memory_info().rss
t0 = time.perf_counter()
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

rss_import = proc.memory_info().rss
t_import = time.perf_counter() - t0

xs = list(range(400))
ys = [696 + 3 * math.sin(i / 40) - i / 60 for i in xs]
t1 = time.perf_counter()
fig, ax = plt.subplots(figsize=(12, 7), dpi=100)
ax.plot(xs, ys, lw=1.2)
for level, name in ((697.5, "flip"), (698.2, "ONH"), (692.1, "TP1"), (690.7, "TP2"), (699.7, "SL")):
    ax.axhline(level, lw=0.8, ls="--")
    ax.text(401, level, name, va="center")
ax.axhspan(697.0, 697.5, alpha=0.2)
ax.axhspan(699.4, 699.7, alpha=0.2)
ax.set_title("QQQ plán — test")
buf = io.BytesIO()
fig.savefig(buf, format="png")
plt.close(fig)
t_render = time.perf_counter() - t1
rss_render = proc.memory_info().rss
print(
    f"RSS start {rss0 / 1e6:.0f} MB, po importu {rss_import / 1e6:.0f} MB "
    f"(+{(rss_import - rss0) / 1e6:.0f}), po kreslení {rss_render / 1e6:.0f} MB "
    f"(+{(rss_render - rss0) / 1e6:.0f}); import {t_import:.2f} s, kreslení {t_render:.2f} s, "
    f"PNG {len(buf.getvalue()) / 1e3:.0f} kB, matplotlib {matplotlib.__version__}"
)

if "--render" in sys.argv[1:]:
    import datetime as dt
    import resource

    from gexlens_api.plan_chart import render_png

    start = dt.datetime(2026, 11, 2, 14, 0, tzinfo=dt.UTC)
    # Popis řádově jako graf plánu QQQ: 400 minut ceny, cesta, 12 úrovní, 9 řádků poznámek
    spec = {
        "title": "Plán QQQ 2. 11. · SHORT → put 1DTE · plán z 9:31 ET (měření #1744)",
        "subtitle": "verdikt: obchodovat",
        "tz": "Europe/Prague",
        "time_label": "čas Praha",
        "x_min": start.isoformat(),
        "x_max": (start + dt.timedelta(hours=7, minutes=15)).isoformat(),
        "price": {
            "label": "QQQ 1m",
            "points": [
                [(start + dt.timedelta(minutes=i)).isoformat(), y]
                for i, y in zip(xs, ys, strict=True)
            ],
        },
        "path": {
            "label": "cesta scénáře",
            "points": [
                [(start + dt.timedelta(minutes=31)).isoformat(), 696.0],
                [(start + dt.timedelta(hours=2, minutes=30)).isoformat(), 692.0],
                [(start + dt.timedelta(hours=7)).isoformat(), 690.6],
            ],
        },
        "lines": [{"y": 690.0 + i * 1.3, "label": f"úroveň {i}", "role": "level"} for i in range(9)]
        + [
            {"y": 698.2, "label": "vstup A od flip 698,21", "role": "entry"},
            {"y": 699.7, "label": "SL 699,66", "role": "stop"},
            {"y": 692.0, "label": "TP1 691,99", "role": "target"},
        ],
        "vlines": [{"x": (start + dt.timedelta(hours=6, minutes=45)).isoformat(), "label": "exit"}],
        "markers": [],
        "notes": [f"řádek poznámky {i} " + "x" * 120 for i in range(9)],
    }
    for attempt in ("první", "druhé"):
        t2 = time.perf_counter()
        png = render_png(spec)
        wall = time.perf_counter() - t2
        width = int.from_bytes(png[16:20], "big")
        height = int.from_bytes(png[20:24], "big")
        print(
            f"render_png {attempt}: {wall:.2f} s, PNG {len(png) / 1e3:.0f} kB, "
            f"{width} × {height} px"
        )
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    rss_parent = proc.memory_info().rss / 1e6
    print(f"špička RSS podprocesu {peak:.0f} MB; RSS tohoto procesu {rss_parent:.0f} MB")

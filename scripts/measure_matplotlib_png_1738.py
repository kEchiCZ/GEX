"""Paměť a čas serverového PNG přes matplotlib Agg — podklad ADR-0062 K1 (#1738).

Graf 1200 × 700 px s čarou o 400 bodech, pěti úrovněmi a dvěma zónami (řádově jako graf
plánu QQQ). Měří RSS procesu před importem, po importu a po kreslení. matplotlib ani psutil
nejsou v závislostech repa, proto běží v dočasném prostředí:
    uv run --no-project --python 3.12 --with matplotlib --with psutil \
        python scripts/measure_matplotlib_png_1738.py
2. 11. 2026 (Windows, matplotlib 3.11.2): +54 MB RSS, import 1,4 s (první běh 8,5 s kvůli
cache fontů), kreslení 0,22 s, PNG 40 kB. V kontejneru přeměří F4b (#1744).
"""

import io
import math
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

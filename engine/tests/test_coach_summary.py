"""Shrnutí kouče (#1201): věty z čísel, „na co si dát pozor", chování bez dat."""

from gexlens_engine.compute.coach_summary import coach_summary

WEEKLY = {
    "n": 12,
    "total_r": 2.1,
    "win_rate": 0.5,
    "score": 78,
    "flags": {"revenge": {"n": 3, "cost_r": -2.4}, "no_setup": {"n": 1, "cost_r": 0.0}},
    "rules": [
        {
            "kind": "revenge",
            "advice": "Po stopu 15 minut pauza — žádný nový vstup na stejný symbol.",
        },
        {"kind": "no_setup", "advice": "Vstup jen na setup z playbooku."},
    ],
}
HOURS = {
    "trades": {
        "segments": {
            "dopoledne": {"label": "RTH dopoledne", "n": 21, "avg_r": 0.5},
            "open30": {"label": "US open +30", "n": 25, "avg_r": -0.4},
        },
        "best_segment": "dopoledne",
        "worst_segment": "open30",
    },
    "setups": {
        "segments": {"power": {"label": "Power hour", "n": 34, "avg_r": -0.3}},
        "best_segment": None,
        "worst_segment": "power",
    },
}
SETUPS = {
    "n": 388,
    "days": 60,
    "total_r": 11.2,
    "recommendations": [
        {
            "kind": "avoid",
            "text": "Neobchodovat wall_bounce v okně Power hour: Ø -0.30 R, "
            "úspěšnost 20 % (LB 10 %), n=34, Σ -10.2 R.",
        },
        {
            "kind": "focus",
            "text": "Soustředit se na failed_break v okně RTH dopoledne: Ø +0.40 R, "
            "úspěšnost 40 % (LB 25 %), n=35, Σ +14.0 R.",
        },
    ],
}


def test_shrnuti_sklada_vety_a_pozor() -> None:
    summary = coach_summary(WEEKLY, HOURS, SETUPS)
    lines = summary["lines"]
    assert lines[0] == "Tento týden 12 obchodů, +2.10 R, úspěšnost 50 %, disciplína 78/100."
    assert lines[1] == "Nejdražší chyba: vstup krátce po stopu (revenge) (3×, -2.40 R)."
    assert lines[2].startswith(
        "Tvoje obchody: nejlepší okno RTH dopoledne (+0.50 R, n=21), nejhorší US open +30"
    )
    assert lines[3] == "Setupy detektoru: nejhorší Power hour (-0.30 R, n=34)."
    assert lines[4].startswith("Setupy: Neobchodovat wall_bounce v okně Power hour")
    assert "soustředit se na failed_break" in lines[4]
    watch = summary["watch"]
    assert watch[0].startswith("Po stopu 15 minut pauza")
    assert "Neobchodovat v okně US open +30." in watch
    assert any(w.startswith("Neobchodovat wall_bounce v okně Power hour") for w in watch)
    assert len(watch) <= 4


def test_shrnuti_bez_dat() -> None:
    summary = coach_summary(None, None, {"n": 5, "days": 60, "total_r": 0.4, "recommendations": []})
    assert summary["lines"][0] == "Tento týden zatím žádný obchod v deníku."
    assert "zatím žádná kombinace" in summary["lines"][1]
    assert summary["watch"] == []
    assert coach_summary({"n": 0}, {}, None)["lines"] == [
        "Tento týden zatím žádný obchod v deníku."
    ]

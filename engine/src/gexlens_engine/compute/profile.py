"""Strike profil (SPEC 4.6): kombinace volume a OI složky s |Δ| vahou, C − P orientace.

Profile(K) = [volC·|ΔC| + w·OIC·|ΔC|] − [volP·|ΔP| + w·OIP·|ΔP|]

Výstup nese složky odděleně (skládané pruhy v UI: Vol vs. OI Δ odstínem) a
net hodnotu dle varianty dropdownu `Vol + OI Δ` (jen Vol / jen OI Δ / kombinace).
Call kladně (doprava, teal), put záporně (doleva, červená). Čistá funkce bez I/O.
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass


class ProfileVariant(enum.Enum):
    VOL = "vol"
    OI_DELTA = "oi_delta"
    COMBINED = "combined"


@dataclass(frozen=True)
class ProfileInput:
    """Vstup pro jeden kontrakt: volume, OI a Δ z posledního platného snapshotu."""

    strike: float
    right: str  # C | P
    volume: float
    oi: float
    delta: float


@dataclass(frozen=True)
class StrikeProfile:
    """Profil jednoho striku: složky pro skládané pruhy + net dle varianty + tooltip data."""

    strike: float
    call_vol_component: float  # volC · |ΔC|
    call_oi_component: float  # w · OIC · |ΔC|
    put_vol_component: float  # volP · |ΔP|
    put_oi_component: float  # w · OIP · |ΔP|
    net: float  # call složky − put složky dle varianty
    call_volume: float  # surové hodnoty pro tooltip (SPEC 4.6)
    put_volume: float
    call_oi: float
    put_oi: float
    distance_from_spot: float


def compute_profile(
    inputs: Sequence[ProfileInput],
    variant: ProfileVariant,
    spot: float,
    *,
    oi_weight: float = 1.0,
) -> list[StrikeProfile]:
    """Spočte strike profil pro danou variantu (čistá funkce, SPEC 4.6)."""
    calls: dict[float, ProfileInput] = {}
    puts: dict[float, ProfileInput] = {}
    for item in inputs:
        if item.right == "C":
            calls[item.strike] = item
        elif item.right == "P":
            puts[item.strike] = item
        else:
            raise ValueError(f"Neplatná strana opce: {item.right!r} (očekávám C/P)")

    profiles: list[StrikeProfile] = []
    for strike in sorted(set(calls) | set(puts)):
        call = calls.get(strike)
        put = puts.get(strike)
        call_abs_delta = abs(call.delta) if call else 0.0
        put_abs_delta = abs(put.delta) if put else 0.0

        call_vol = (call.volume if call else 0.0) * call_abs_delta
        call_oi = oi_weight * (call.oi if call else 0.0) * call_abs_delta
        put_vol = (put.volume if put else 0.0) * put_abs_delta
        put_oi = oi_weight * (put.oi if put else 0.0) * put_abs_delta

        if variant is ProfileVariant.VOL:
            net = call_vol - put_vol
        elif variant is ProfileVariant.OI_DELTA:
            net = call_oi - put_oi
        else:
            net = (call_vol + call_oi) - (put_vol + put_oi)

        profiles.append(
            StrikeProfile(
                strike=strike,
                call_vol_component=call_vol,
                call_oi_component=call_oi,
                put_vol_component=put_vol,
                put_oi_component=put_oi,
                net=net,
                call_volume=call.volume if call else 0.0,
                put_volume=put.volume if put else 0.0,
                call_oi=call.oi if call else 0.0,
                put_oi=put.oi if put else 0.0,
                distance_from_spot=strike - spot,
            )
        )
    return profiles

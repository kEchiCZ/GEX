#!/usr/bin/env bash
# Squash-merge PR až po ZELENÉM CI, ukotveno na konkrétní commit SHA.
#
# Proč skript a ne jednořádkový `gh pr checks --watch && gh pr merge`:
#
# 1. `gh pr checks --watch` vrací exit 0 i při failu — řetěz s `&&` tedy
#    mergne červený PR (stalo se u #105 a #424).
# 2. `gh pr checks` hned po `gh pr create` hlásí „no checks reported" a
#    `--watch` skončí okamžitě → merge bez CI.
# 3. Hned po `git push` do existující větve `gh pr checks` chvíli ukazuje
#    běhy PŘEDCHOZÍHO commitu, pak krátce nic. Čekání na „nějaké checky"
#    proto může projít na starém výsledku nebo spadnout do prázdného okna.
#
# Řešení: ptát se GitHubu na check-runs KONKRÉTNÍHO SHA, počkat na jejich
# dokončení a merge pustit jen tehdy, když všechny skončily `success`.
#
# Bez závislosti na samostatném `jq` — `gh api --jq` má filtr zabudovaný.
set -euo pipefail

pr="${1:?použití: merge-when-green.sh <číslo PR> [timeout_s]}"
timeout_s="${2:-1800}"
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
sha=$(gh pr view "$pr" --json headRefOid --jq .headRefOid)
echo "PR #$pr @ ${sha:0:8} v $repo"

deadline=$(( $(date +%s) + timeout_s ))
while :; do
    # Jeden řádek na check: název <tab> status <tab> conclusion
    runs=$(gh api "repos/$repo/commits/$sha/check-runs" \
        --jq '.check_runs[] | "\(.name)\t\(.status)\t\(.conclusion // "-")"' || true)
    total=$(printf '%s' "$runs" | grep -c . || true)
    pending=$(printf '%s' "$runs" | grep -cv $'\tcompleted\t' || true)

    if [ "$total" -gt 0 ] && [ "$pending" -eq 0 ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "TIMEOUT po ${timeout_s}s — $total checků, z toho $pending nedoběhlo:"
        printf '%s\n' "$runs"
        exit 2
    fi
    sleep 20
done

printf '%s\n' "$runs" | sed 's/^/  /'
failed=$(printf '%s' "$runs" | grep -cv $'\tsuccess$' || true)
if [ "$failed" -ne 0 ]; then
    echo "CI NENÍ zelené ($failed z $total) — NEMERGUJU"
    exit 1
fi

echo "CI zelené ($total checků) — merguju"
gh pr merge "$pr" --squash --delete-branch

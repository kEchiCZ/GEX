#!/usr/bin/env bash
# Založí privátní repo gexlens na GitHubu, nahraje labels, milestones a issues.
# Použití:  GITHUB_TOKEN=ghp_xxx ./scripts/bootstrap_repo.sh [nazev-repa]
# Token: fine-grained PAT s právy Administration:write (vytvoření repa),
#        Contents:write, Issues:write — jen pro tento repozitář/účet, krátká expirace.
set -euo pipefail

REPO="${1:-gexlens}"
API="https://api.github.com"
AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json")
DIR="$(cd "$(dirname "$0")/.." && pwd)"

OWNER=$(curl -sf "${AUTH[@]}" "$API/user" | python3 -c "import sys,json;print(json.load(sys.stdin)['login'])")
echo "Účet: $OWNER"

# 1) Repo (privátní)
curl -sf "${AUTH[@]}" -X POST "$API/user/repos" \
  -d "{\"name\":\"$REPO\",\"private\":true,\"description\":\"GEX/OI options positioning visualizer nad IBKR (ROHOR Studio)\",\"has_issues\":true,\"auto_init\":true}" >/dev/null \
  && echo "Repo $OWNER/$REPO založeno" || echo "Repo možná existuje — pokračuji"

# 2) Labels
for L in "epic:engine|1d76db" "epic:storage|0e8a16" "epic:compute|5319e7" "epic:api|fbca04" \
         "epic:frontend|d93f0b" "epic:ops|c2e0c6" "epic:quality|bfdadc" \
         "prio:P0|b60205" "prio:P1|e99695" "needs-decision|f9d0c4"; do
  NAME="${L%%|*}"; COLOR="${L##*|}"
  curl -s "${AUTH[@]}" -X POST "$API/repos/$OWNER/$REPO/labels" \
    -d "{\"name\":\"$NAME\",\"color\":\"$COLOR\"}" >/dev/null || true
done
echo "Labels OK"

# 3) Milestones (pořadí = číslo)
declare -A MS
for M in "M1 Datová vrstva" "M2 Výpočty" "M3 API" "M4 Frontend" "M5 Provozní celek"; do
  NUM=$(curl -s "${AUTH[@]}" -X POST "$API/repos/$OWNER/$REPO/milestones" \
    -d "{\"title\":\"$M\"}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('number',''))")
  if [ -z "$NUM" ]; then
    NUM=$(curl -s "${AUTH[@]}" "$API/repos/$OWNER/$REPO/milestones?state=all" | \
      python3 -c "import sys,json;ms=json.load(sys.stdin);print(next(m['number'] for m in ms if m['title']=='$M'))")
  fi
  MS["$M"]=$NUM
done
echo "Milestones OK"

# 4) Issues z issues.json (v pořadí souboru)
python3 - "$DIR/scripts/issues.json" <<'PY'
import json, os, sys, urllib.request

issues = json.load(open(sys.argv[1]))
token = os.environ["GITHUB_TOKEN"]
api = "https://api.github.com"

def req(method, path, data=None):
    r = urllib.request.Request(api + path, method=method,
        data=json.dumps(data).encode() if data else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))

owner = req("GET", "/user")["login"]
repo = os.environ.get("REPO", "gexlens")
ms = {m["title"]: m["number"] for m in req("GET", f"/repos/{owner}/{repo}/milestones?state=all")}

for i, it in enumerate(issues, 1):
    res = req("POST", f"/repos/{owner}/{repo}/issues", {
        "title": it["title"], "body": it["body"],
        "labels": it["labels"], "milestone": ms[it["milestone"]]})
    print(f"#{res['number']:>3} {it['title']}")
PY
echo "Hotovo: https://github.com/$OWNER/$REPO/issues"

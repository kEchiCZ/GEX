#!/usr/bin/env bash
# Založí VM gex-server v Oracle Cloud Always Free a opakuje pokus, dokud není kapacita A1 (#1802).
#
# Konzole 20. 11. 2026 hlásila „Out of capacity“ pro VM.Standard.A1.Flex ve všech třech AD
# Frankfurtu, proto skript zkouší AD dokola s pauzou. Hodnoty účtu bere jen z ~/.oci/config
# (profil OCI_CLI_PROFILE) a z proměnných prostředí; v repu nic z účtu není (repo je veřejné).
#
# Co dělá (idempotentně, druhé spuštění nic nového nezaloží):
#   1. existuje-li gex-server, jen vypíše jeho veřejnou IPv4 a skončí 0 (síť nemění, takže
#      po smazání ingress 22 ho zpět nepřidá);
#   2. síť: VCN gex-vcn, internet gateway gex-igw, výchozí route table 0.0.0.0/0 → IGW,
#      veřejná podsíť gex-subnet; výchozí security list VCN = jen TCP 22 z adresy PC /32
#      (dočasně, do Tailscale SSH) a egress vše;
#   3. před KAŽDÝM pokusem ověří, že v tenancy není jiná instance, boot ani block volume
#      (Always Free = 2 OCPU / 12 GB / 200 GB celkem; další zdroj by čerpal kredit nebo peníze),
#      jinak skončí chybou;
#   4. launch 2 OCPU / 12 GB, nejnovější Canonical Ubuntu 24.04 aarch64, boot 200 GB,
#      veřejná IPv4, klíč z ~/.ssh/id_ed25519.pub. „Out of host capacity“ = další AD po pauze,
#      429 = delší pauza, výpadek spojení = další pokus (kontrola z bodu 1 odhalí instanci,
#      kdyby přesto vznikla), jiná chyba (autorizace, limit, parametr) = konec s chybou.
#
# Použití (PC, Git Bash; OCI CLI: `uv tool install oci-cli`, API klíč: ADMIN-MANUAL kap. 14):
#   scripts/server/oci-launch-retry.sh
# Proměnné prostředí:
#   OCI_CLI_CONFIG_FILE, OCI_CLI_PROFILE  config a profil (čte je i OCI CLI), výchozí ~/.oci/config, DEFAULT
#   OCI_COMPARTMENT_ID      kompartment pro síť a VM, výchozí kořen tenancy
#   GEX_SSH_PUBKEY          veřejný klíč, výchozí ~/.ssh/id_ed25519.pub
#   GEX_SSH_CIDR            zdroj ingress 22, výchozí veřejná IPv4 PC /32 (checkip.amazonaws.com)
#   GEX_OCI_PAUSE           pauza po pokusu v s, výchozí 40 → každá ze 3 AD zhruba po 2 min
#   GEX_OCI_PAUSE_THROTTLE  pauza po 429 v s, výchozí 300
#   GEX_OCI_LOG             log s časem každého pokusu, výchozí ~/.oci/oci-launch-retry.log
#                           (OCID a IP sem patří, do repa ani issues ne)

# JMESPath literály v --query jsou záměrně v backtickách uvnitř jednoduchých uvozovek.
# shellcheck disable=SC2016
set -euo pipefail

readonly NAME=gex-server SHAPE=VM.Standard.A1.Flex OCPUS=2 MEM_GB=12 BOOT_GB=200
readonly VCN_NAME=gex-vcn IGW_NAME=gex-igw SUBNET_NAME=gex-subnet
readonly VCN_CIDR=10.0.0.0/16 SUBNET_CIDR=10.0.0.0/24
readonly RUNNING_TIMEOUT=1800

CONFIG_FILE=${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}
PROFILE=${OCI_CLI_PROFILE:-DEFAULT}
PUBKEY=${GEX_SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}
PAUSE=${GEX_OCI_PAUSE:-40}
PAUSE_THROTTLE=${GEX_OCI_PAUSE_THROTTLE:-300}
LOG_FILE=${GEX_OCI_LOG:-$HOME/.oci/oci-launch-retry.log}
LOCK_DIR=$LOG_FILE.lock

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*)
    # OCI CLI na Windows ověřuje práva klíče přes powershell.exe 5.1. Zděděný PSModulePath
    # z PowerShellu 7 mu rozbije Get-Acl (CouldNotAutoloadMatchingModule) a stderr každého
    # volání zaplní chybou; bez něj si 5.1 sestaví vlastní cestu modulů a kontrola projde.
    unset PSModulePath
    ;;
esac
# OCI CLI 3.96 na Pythonu 3.12+ hlásí SyntaxWarning z vlastního kódu: šum ve stderr,
# ze kterého se čte kód chyby.
export PYTHONWARNINGS=ignore::SyntaxWarning

OUT='' ERR=''
TENANCY='' COMPARTMENT='' PUBKEY_ARG='' SSH_CIDR=''
VCN_ID='' IGW_ID='' RT_ID='' SL_ID='' SUBNET_ID='' IMAGE_ID='' INSTANCE_ID=''
ADS=()

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" | tee -a "$LOG_FILE"; }
die() {
  log "CHYBA: $*"
  exit 1
}

# Zavolá OCI CLI bez vlastního opakování: stdout do OUT (null = prázdný), stderr do ERR.
oci_call() {
  local errf rc=0
  errf=$(mktemp)
  OUT=$(oci --no-retry "$@" 2>"$errf" | tr -d '\r') || rc=$?
  ERR=$(tr -d '\r' <"$errf")
  rm -f "$errf"
  [[ $OUT == null ]] && OUT=''
  return "$rc"
}

# Kód, zpráva a HTTP status chyby OCI; bez ServiceError první řádky stderr.
err_summary() {
  local s
  s=$(grep -oE '"(code|message|status)": [^,]*' <<<"$ERR" | tr '\n' ' ') || true
  [[ -n $s ]] || s=$(head -n 3 <<<"$ERR" | tr '\n' ' ')
  printf '%s' "$s"
}

# Chyba spojení (OCI neodpovědělo), ne odmítnutí služby ani chyba parametru.
is_transport_error() {
  ! grep -q 'ServiceError' <<<"$ERR" &&
    grep -qiE 'RequestException|ConnectionError|ConnectTimeout|ReadTimeout|timed out|Max retries exceeded|NameResolution|getaddrinfo|Connection (aborted|reset|refused)' <<<"$ERR"
}

# Čtecí dotaz: výpadek spojení se opakuje, odmítnutí služby je konec.
oci_read() {
  until oci_call "$@"; do
    is_transport_error || die "OCI odmítlo dotaz '${1:-} ${2:-} ${3:-}': $(err_summary)"
    log "Výpadek spojení s OCI (${1:-} ${2:-} ${3:-}), další pokus za ${PAUSE} s: $(err_summary)"
    sleep "$PAUSE"
  done
}

# Zakládá nebo mění zdroj: bez opakování, nejasný výsledek by mohl zdroj zdvojit.
oci_write() {
  oci_call "$@" || die "OCI '${1:-} ${2:-} ${3:-}' selhalo: $(err_summary)"
}

# Hodnota klíče z profilu $PROFILE v $CONFIG_FILE.
cfg_value() {
  awk -v want="[$PROFILE]" -v key="$1" '
    { sub(/\r$/, "") }
    /^[[:space:]]*\[/ { in_profile = ($0 == want); next }
    in_profile {
      k = $0; sub(/[[:space:]]*=.*/, "", k); sub(/^[[:space:]]+/, "", k)
      if (k == key) { v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); print v; exit }
    }' "$CONFIG_FILE"
}

preflight() {
  mkdir -p "$(dirname "$LOG_FILE")"
  command -v oci >/dev/null || die "chybí OCI CLI (uv tool install oci-cli)"
  [[ -r $CONFIG_FILE ]] || die "chybí $CONFIG_FILE (API klíč z konzole, ADMIN-MANUAL kap. 14)"
  TENANCY=$(cfg_value tenancy)
  [[ $TENANCY == ocid1.tenancy.* ]] || die "v $CONFIG_FILE [$PROFILE] chybí tenancy"
  COMPARTMENT=${OCI_COMPARTMENT_ID:-$TENANCY}
  [[ -r $PUBKEY ]] || die "chybí veřejný klíč $PUBKEY"
  grep -qE '^ssh-(ed25519|rsa) ' "$PUBKEY" || die "$PUBKEY nevypadá jako veřejný SSH klíč"
  PUBKEY_ARG=$PUBKEY
  if command -v cygpath >/dev/null; then PUBKEY_ARG=$(cygpath -m "$PUBKEY"); fi
  [[ $PAUSE =~ ^[0-9]+$ && $PAUSE_THROTTLE =~ ^[0-9]+$ ]] ||
    die "GEX_OCI_PAUSE a GEX_OCI_PAUSE_THROTTLE musí být celé sekundy"
}

# Jediný běh naráz: dva souběžné běhy by mohly založit dvě instance.
acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    local pid
    pid=$(cat "$LOCK_DIR/pid" 2>/dev/null) || true
    if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then die "skript už běží (PID $pid, zámek $LOCK_DIR)"; fi
    log "Odstraňuji zámek po ukončeném běhu (PID ${pid:-?})"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || die "nezaložím zámek $LOCK_DIR"
  fi
  echo $$ >"$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
  trap 'log "Přerušeno signálem"; exit 130' INT TERM
}

detect_ssh_cidr() {
  if [[ -n ${GEX_SSH_CIDR:-} ]]; then
    SSH_CIDR=$GEX_SSH_CIDR
  else
    local ip
    ip=$(curl -fsS --max-time 15 https://checkip.amazonaws.com | tr -d '\r\n ') ||
      die "nezjistím veřejnou IPv4 PC (nastav GEX_SSH_CIDR=x.x.x.x/32)"
    SSH_CIDR=$ip/32
  fi
  [[ $SSH_CIDR =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || die "GEX_SSH_CIDR musí být jedna IPv4 /32, je '$SSH_CIDR'"
}

ensure_network() {
  oci_read network vcn list --compartment-id "$COMPARTMENT" --display-name "$VCN_NAME" \
    --lifecycle-state AVAILABLE --all --query 'data[0].id' --raw-output
  VCN_ID=$OUT
  if [[ -z $VCN_ID ]]; then
    oci_write network vcn create --compartment-id "$COMPARTMENT" --display-name "$VCN_NAME" \
      --cidr-blocks "[\"$VCN_CIDR\"]" --dns-label gexvcn --wait-for-state AVAILABLE \
      --query 'data.id' --raw-output
    VCN_ID=$OUT
    log "Založena VCN $VCN_NAME ($VCN_CIDR)"
  fi
  [[ $VCN_ID == ocid1.vcn.* ]] || die "neočekávané id VCN: $VCN_ID"

  oci_read network vcn get --vcn-id "$VCN_ID" --query 'data."default-route-table-id"' --raw-output
  RT_ID=$OUT
  oci_read network vcn get --vcn-id "$VCN_ID" --query 'data."default-security-list-id"' --raw-output
  SL_ID=$OUT
  [[ $RT_ID == ocid1.routetable.* && $SL_ID == ocid1.securitylist.* ]] ||
    die "VCN $VCN_NAME nemá výchozí route table nebo security list"

  oci_read network internet-gateway list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
    --display-name "$IGW_NAME" --lifecycle-state AVAILABLE --all --query 'data[0].id' --raw-output
  IGW_ID=$OUT
  if [[ -z $IGW_ID ]]; then
    oci_write network internet-gateway create --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
      --display-name "$IGW_NAME" --is-enabled true --wait-for-state AVAILABLE \
      --query 'data.id' --raw-output
    IGW_ID=$OUT
    log "Založena internet gateway $IGW_NAME"
  fi
  [[ $IGW_ID == ocid1.internetgateway.* ]] || die "neočekávané id internet gateway: $IGW_ID"

  oci_read network route-table get --rt-id "$RT_ID" \
    --query 'join(`","`, data."route-rules"[].join(`" "`, [destination, "network-entity-id"]))' --raw-output
  if [[ $OUT != "0.0.0.0/0 $IGW_ID" ]]; then
    oci_write network route-table update --rt-id "$RT_ID" --force \
      --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW_ID\"}]" \
      --query 'data.id' --raw-output
    log "Výchozí route table: 0.0.0.0/0 → $IGW_NAME"
  fi

  local have_in have_out
  oci_read network security-list get --security-list-id "$SL_ID" \
    --query 'join(`","`, data."ingress-security-rules"[].join(`" "`, [protocol, source, to_string("tcp-options"."destination-port-range".min), to_string("tcp-options"."destination-port-range".max)]))' \
    --raw-output
  have_in=$OUT
  oci_read network security-list get --security-list-id "$SL_ID" \
    --query 'join(`","`, data."egress-security-rules"[].join(`" "`, [protocol, destination]))' --raw-output
  have_out=$OUT
  if [[ $have_in != "6 $SSH_CIDR 22 22" || $have_out != "all 0.0.0.0/0" ]]; then
    oci_write network security-list update --security-list-id "$SL_ID" --force \
      --ingress-security-rules "[{\"protocol\":\"6\",\"source\":\"$SSH_CIDR\",\"sourceType\":\"CIDR_BLOCK\",\"isStateless\":false,\"tcpOptions\":{\"destinationPortRange\":{\"min\":22,\"max\":22}},\"description\":\"docasne SSH z PC do Tailscale SSH (#1802)\"}]" \
      --egress-security-rules '[{"protocol":"all","destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK","isStateless":false}]' \
      --query 'data.id' --raw-output
    log "Security list: ingress jen TCP 22 z $SSH_CIDR, egress vše"
  fi

  oci_read network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
    --display-name "$SUBNET_NAME" --lifecycle-state AVAILABLE --all --query 'data[0].id' --raw-output
  SUBNET_ID=$OUT
  if [[ -z $SUBNET_ID ]]; then
    oci_write network subnet create --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
      --display-name "$SUBNET_NAME" --cidr-block "$SUBNET_CIDR" --dns-label gexsub \
      --route-table-id "$RT_ID" --security-list-ids "[\"$SL_ID\"]" --prohibit-public-ip-on-vnic false \
      --wait-for-state AVAILABLE --query 'data.id' --raw-output
    SUBNET_ID=$OUT
    log "Založena veřejná podsíť $SUBNET_NAME ($SUBNET_CIDR)"
  else
    oci_read network subnet get --subnet-id "$SUBNET_ID" --query 'data."prohibit-public-ip-on-vnic"' --raw-output
    [[ $OUT == false ]] || die "podsíť $SUBNET_NAME zakazuje veřejnou IPv4"
  fi
  [[ $SUBNET_ID == ocid1.subnet.* ]] || die "neočekávané id podsítě: $SUBNET_ID"
}

find_image() {
  oci_read compute image list --compartment-id "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
    --operating-system-version 24.04 --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC --all \
    --query 'data[?contains("display-name", `"aarch64"`)] | [0] | join(`" "`, [id, "display-name"])' \
    --raw-output
  IMAGE_ID=${OUT%% *}
  [[ $IMAGE_ID == ocid1.image.* ]] || die "nenašel jsem image Canonical Ubuntu 24.04 aarch64 pro $SHAPE"
  log "Image: ${OUT#* }"
}

load_ads() {
  oci_read iam availability-domain list --compartment-id "$TENANCY" --query 'join(`" "`, data[].name)' --raw-output
  read -r -a ADS <<<"$OUT"
  ((${#ADS[@]} > 0)) || die "region nemá žádnou availability domain"
  log "AD: ${ADS[*]##*:}"
}

# INSTANCE_ID = gex-server mimo TERMINATED v kompartmentu (výpis instancí je hned konzistentní).
existing_instance() {
  oci_read compute instance list --compartment-id "$COMPARTMENT" --display-name "$NAME" --all \
    --query 'data[?"lifecycle-state" != `"TERMINATED"`] | [0].id' --raw-output
  INSTANCE_ID=$OUT
  [[ -n $INSTANCE_ID ]]
}

# Always Free = 2 OCPU / 12 GB / 200 GB celkem: s jinou instancí nebo svazkem v tenancy nic nezakládat.
guard_free_tier() {
  oci_read search resource structured-search \
    --query-text "query instance, bootvolume, volume resources where lifecycleState != 'TERMINATED'" \
    --query 'join(`", "`, data.items[].join(`" "`, ["resource-type", "display-name", "lifecycle-state"]))' \
    --raw-output
  [[ -z $OUT ]] || die "v tenancy už existuje: $OUT. Další instance nebo svazek by překročil Always Free (2 OCPU / 12 GB / 200 GB), nic nezakládám"
  oci_read compute instance list --compartment-id "$COMPARTMENT" --all \
    --query 'join(`", "`, data[?"lifecycle-state" != `"TERMINATED"`].join(`" "`, ["display-name", "lifecycle-state"]))' \
    --raw-output
  [[ -z $OUT ]] || die "v kompartmentu už existuje instance: $OUT, nic nezakládám"
}

report_instance() {
  local state waited=0
  while :; do
    oci_read compute instance get --instance-id "$INSTANCE_ID" --query 'data."lifecycle-state"' --raw-output
    state=$OUT
    [[ $state == RUNNING ]] && break
    [[ $state == PROVISIONING || $state == STARTING ]] || die "instance $NAME je ve stavu $state"
    ((waited < RUNNING_TIMEOUT)) || die "instance $NAME ani po ${RUNNING_TIMEOUT} s neběží (stav $state)"
    log "Instance $NAME ve stavu $state, čekám na RUNNING"
    sleep 20
    waited=$((waited + 20))
  done
  oci_read compute instance list-vnics --instance-id "$INSTANCE_ID" --query 'data[0]."public-ip"' --raw-output
  [[ -n $OUT ]] || die "instance $NAME běží, ale nemá veřejnou IPv4"
  log "HOTOVO: $NAME běží, veřejná IPv4 $OUT. Ověř: ssh ubuntu@$OUT uname -m (čeká se aarch64)"
}

launch_loop() {
  local i=0 attempt=0 ad short
  while :; do
    if existing_instance; then
      report_instance
      return 0
    fi
    guard_free_tier
    ad=${ADS[i % ${#ADS[@]}]}
    short=${ad##*:}
    attempt=$((attempt + 1))
    i=$((i + 1))
    log "Pokus $attempt: $short"
    if oci_call compute instance launch --compartment-id "$COMPARTMENT" --availability-domain "$ad" \
      --display-name "$NAME" --hostname-label "$NAME" --shape "$SHAPE" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
      --image-id "$IMAGE_ID" --boot-volume-size-in-gbs "$BOOT_GB" \
      --subnet-id "$SUBNET_ID" --assign-public-ip true --ssh-authorized-keys-file "$PUBKEY_ARG" \
      --query 'data.id' --raw-output; then
      INSTANCE_ID=$OUT
      log "Instance $NAME založena v $short"
      report_instance
      return 0
    fi
    if grep -qiE 'out of (host )?capacity' <<<"$ERR"; then
      log "  $short: Out of host capacity, další AD za ${PAUSE} s"
      sleep "$PAUSE"
    elif grep -qE '"status": 429|TooManyRequests' <<<"$ERR"; then
      log "  $short: 429 TooManyRequests, pauza ${PAUSE_THROTTLE} s"
      sleep "$PAUSE_THROTTLE"
    elif is_transport_error; then
      log "  $short: výpadek spojení ($(err_summary)), další pokus za ${PAUSE} s"
      sleep "$PAUSE"
    else
      die "launch v $short selhal: $(err_summary)"
    fi
  done
}

main() {
  preflight
  acquire_lock
  log "Start: $NAME $SHAPE $OCPUS OCPU / $MEM_GB GB, boot $BOOT_GB GB, profil $PROFILE"
  if existing_instance; then
    report_instance
    exit 0
  fi
  detect_ssh_cidr
  ensure_network
  find_image
  load_ads
  launch_loop
}

main "$@"

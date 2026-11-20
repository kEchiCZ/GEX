#!/usr/bin/env bash
# Bootstrap Linux serveru GEXLens (#1802, ADR-0063): Ubuntu 24.04, amd64 i arm64, idempotentní.
# Stejný skript pro Oracle Cloud A1 i plán B (jiný poskytovatel): firewall volí podle toho,
# co image má (iptables image Oracle se zachovají, jinde UFW), cesty žádného poskytovatele.
#
# Spuštění (jako root):
#   první běh z PC přes dočasné veřejné SSH:
#     ssh ubuntu@<IP> 'sudo bash -s' < scripts/server/bootstrap.sh
#   další běhy přes Tailscale SSH (po `tailscale up`):
#     ssh ubuntu@gex-server sudo bash /srv/gexlens/scripts/server/bootstrap.sh
#   zavření veřejného SSH, až Tailscale SSH z PC funguje (před vložením tajemství):
#     ssh ubuntu@gex-server sudo bash /srv/gexlens/scripts/server/bootstrap.sh --close-public-ssh
#
# Každý krok mění systém jen při rozdílu proti cíli a hlásí „ZMĚNA“; na konci počet změn
# a kroky pro vlastníka. Druhý běh bez nových balíků v archivu = 0 změn.
#
# Co dělá:
#   - apt full-upgrade, pásmo Europe/Prague, hostname, zram swap (zstd, 25 % RAM);
#   - unattended-upgrades bez rebootu a bez Dockeru (balíky Dockeru na blacklistu, needrestart
#     Docker ani containerd nerestartuje; Docker se aktualizuje jen ručně v pauze Globexu);
#   - journald strop 500 MB, Docker Engine + compose plugin z download.docker.com, log limity;
#   - uživatel gex ve skupině docker (přístup jen přes Tailscale SSH, žádný klíč);
#   - Tailscale z pkgs.tailscale.com; po přihlášení vlastníka Tailscale SSH a
#     `tailscale serve` HTTPS 443 → 127.0.0.1:8080 (nginx stacku);
#   - firewall: na veřejném rozhraní nic dovnitř (do zavření jen dočasné 22), DOCKER-USER zahodí
#     vše z veřejného rozhraní do kontejnerů (past řetězce DOCKER před UFW, #542);
#   - sshd bez hesel; uživatel restic jen pro sftp z PC (#1805, #1851): port 2222 jen v tailnetu
#     (port 22 adresy tailnetu obsluhuje Tailscale SSH), chroot /srv/restic, bez shellu a docker;
#   - PowerShell 7 a uv (tarball s ověřeným sha256, stejné verze jako PC);
#   - /srv/gexlens: git clone, data/ pro uid 10001 (#542), .env 600 a secrets/ 700 prázdné
#     (hodnoty vkládá vlastník), data/local/instance.json s rolí prohlížení (epocha 0).
# Co nedělá: tajemství, `tailscale up` (přihlášení vlastníka), stack a IB Gateway (F6 #1803).
#
# Proměnné prostředí:
#   GEX_HOSTNAME          jméno stroje a uzlu v tailnetu, výchozí gex-server
#   GEX_ROOT              adresář repa, výchozí /srv/gexlens
#   GEX_REPO_URL          výchozí https://github.com/kEchiCZ/GEX.git (veřejné repo)
#   GEX_SSH_CIDR          jen UFW (plán B) do zavření veřejného SSH: zdroj dočasného 22, x.x.x.x/32
#   GEX_RESTIC_PUBKEY     veřejný klíč PC pro uživatele restic (bez něj zůstane stávající soubor)
#   GEX_TEMP_KEY_COMMENT  komentář dočasného klíče PC, který --close-public-ssh odebere, výchozí gex-pc
set -euo pipefail

readonly HOSTNAME_WANT=${GEX_HOSTNAME:-gex-server}
readonly ROOT=${GEX_ROOT:-/srv/gexlens}
readonly REPO_URL=${GEX_REPO_URL:-https://github.com/kEchiCZ/GEX.git}
readonly TEMP_KEY_COMMENT=${GEX_TEMP_KEY_COMMENT:-gex-pc}
readonly TZ_WANT=Europe/Prague
readonly APP_UID=10001 # uživatel kontejnerů engine, api a news-engine (#542)
readonly RESTIC_USER=restic RESTIC_CHROOT=/srv/restic RESTIC_HOME=/var/lib/restic-sftp RESTIC_PORT=2222
readonly SERVE_TARGET=http://127.0.0.1:8080
readonly STATE_DIR=/etc/gexlens
readonly CLOSED_MARKER=$STATE_DIR/public-ssh-closed
readonly PWSH_VERSION=7.6.9 UV_VERSION=0.11.26

CHANGES=0
TODO=()
APT_UPDATED=0
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
ARCH='' CODENAME='' PUB_IF='' FIREWALL='' TS_STATE='' TS_DNS=''
CLOSE_PUBLIC_SSH=0

log() { printf '[bootstrap] %s\n' "$*"; }
die() {
  log "CHYBA: $*"
  exit 1
}
changed() {
  CHANGES=$((CHANGES + 1))
  log "ZMĚNA: $*"
}
todo() { TODO+=("$*"); }

# Zapíše stdin do souboru jen při rozdílu obsahu, práv nebo vlastníka. 0 = změněno, 1 = beze změny.
put_file() {
  local path=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f $path ]] && cmp -s "$tmp" "$path" && [[ $(stat -c '%a %U:%G' "$path") == "$mode $owner" ]]; then
    rm -f "$tmp"
    return 1
  fi
  install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$path"
  rm -f "$tmp"
  changed "$path"
}

# Adresář s právy a vlastníkem (bez rekurze: data se nepřevlastňují).
ensure_dir() {
  local path=$1 mode=$2 owner=$3
  if [[ -d $path && $(stat -c '%a %U:%G' "$path") == "$mode $owner" ]]; then return 0; fi
  install -d -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$path"
  changed "adresář $path ($mode $owner)"
}

apt_update() {
  ((APT_UPDATED)) && return 0
  apt-get update -q >/dev/null
  APT_UPDATED=1
}

ensure_pkgs() {
  local missing=() p
  for p in "$@"; do
    # formát dpkg-query, ne proměnná shellu
    # shellcheck disable=SC2016
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
  done
  ((${#missing[@]})) || return 0
  apt_update
  apt-get install "${APT_OPTS[@]}" "${missing[@]}" >/dev/null
  changed "balíky ${missing[*]}"
}

preflight() {
  ((EUID == 0)) || die "spusť jako root (sudo)"
  # shellcheck source=/dev/null
  . /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die "podporované je jen Ubuntu 24.04 (je ${ID:-?} ${VERSION_ID:-?})"
  CODENAME=${VERSION_CODENAME:-}
  [[ -n $CODENAME ]] || die "/etc/os-release bez VERSION_CODENAME"
  ARCH=$(dpkg --print-architecture)
  [[ $ARCH == amd64 || $ARCH == arm64 ]] || die "nepodporovaná architektura $ARCH"
  PUB_IF=$(ip -4 route show default | awk '{print $5; exit}')
  [[ -n $PUB_IF ]] || die "nenašel jsem veřejné rozhraní (výchozí route IPv4)"
  if [[ -f /etc/iptables/rules.v4 ]] && grep -q 'InstanceServices' /etc/iptables/rules.v4; then
    FIREWALL=oracle-iptables
  else
    FIREWALL=ufw
  fi
  [[ -f $CLOSED_MARKER ]] && CLOSE_PUBLIC_SSH=1
  log "Ubuntu $VERSION_ID $ARCH, veřejné rozhraní $PUB_IF, firewall $FIREWALL"
}

stage_upgrade() {
  apt_update
  local n
  n=$(apt-get -s full-upgrade | grep -c '^Inst ') || true
  if ((n > 0)); then
    apt-get full-upgrade "${APT_OPTS[@]}" >/dev/null
    changed "apt full-upgrade ($n balíků)"
  fi
  [[ -f /var/run/reboot-required ]] && todo "restart serveru kvůli aktualizaci (sudo reboot) — mimo otevřený trh, jen bez běžícího sběru"
  return 0
}

stage_base() {
  ensure_pkgs ca-certificates curl gnupg git python3 zram-tools unattended-upgrades needrestart libicu74
  if [[ $(timedatectl show -p Timezone --value) != "$TZ_WANT" ]]; then
    timedatectl set-timezone "$TZ_WANT"
    changed "pásmo $TZ_WANT"
  fi
  if [[ $(hostnamectl hostname) != "$HOSTNAME_WANT" ]]; then
    hostnamectl set-hostname "$HOSTNAME_WANT"
    changed "hostname $HOSTNAME_WANT"
  fi
  modprobe -q zram || die "jádro $(uname -r) nemá modul zram (zram-tools by nefungoval)"
  if put_file /etc/default/zram-tools 644 root:root <<'EOF'; then
# GEXLens bootstrap (#1802): swap v RAM se zstd, 25 % paměti (kryje špičky RTH, ADMIN-MANUAL kap. 14)
ALGO=zstd
PERCENT=25
PRIORITY=100
EOF
    systemctl restart zramswap
  fi
  systemctl is-active --quiet zramswap || die "zramswap neběží (journalctl -u zramswap)"
}

stage_updates_policy() {
  put_file /etc/apt/apt.conf.d/52gexlens-unattended-upgrades 644 root:root <<'EOF' || true
// GEXLens bootstrap (#1802): bez automatického rebootu; Docker jen ručně v pauze Globexu
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Package-Blacklist {
    "docker-ce";
    "docker-ce-cli";
    "docker-ce-rootless-extras";
    "docker-buildx-plugin";
    "docker-compose-plugin";
    "containerd.io";
};
EOF
  put_file /etc/needrestart/conf.d/50-gexlens.conf 644 root:root <<'EOF' || true
# GEXLens bootstrap (#1802): Docker a containerd po aktualizaci knihoven nerestartovat
# (restart Dockeru = výpadek sběru); restart jen ručně v pauze Globexu.
$nrconf{override_rc}{qr(^docker)} = 0;
$nrconf{override_rc}{qr(^containerd)} = 0;
EOF
  if put_file /etc/systemd/journald.conf.d/50-gexlens.conf 644 root:root <<'EOF'; then
# GEXLens bootstrap (#1802): strop journalu
[Journal]
SystemMaxUse=500M
EOF
    systemctl restart systemd-journald
  fi
}

stage_docker() {
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod 644 /etc/apt/keyrings/docker.asc
    changed "klíč repa Docker"
  fi
  if put_file /etc/apt/sources.list.d/docker.list 644 root:root <<EOF; then
deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable
EOF
    APT_UPDATED=0
  fi
  ensure_pkgs docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  if put_file /etc/docker/daemon.json 644 root:root <<'EOF'; then
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
EOF
    if [[ -z $(docker ps -q 2>/dev/null) ]]; then
      systemctl restart docker
    else
      todo "restart Dockeru kvůli /etc/docker/daemon.json ručně v pauze Globexu (běží kontejnery)"
    fi
  fi
  systemctl is-enabled --quiet docker || {
    systemctl enable --now docker
    changed "docker enable"
  }
}

stage_user() {
  if ! id gex >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --groups docker gex
    changed "uživatel gex (skupina docker)"
  elif ! id -nG gex | tr ' ' '\n' | grep -qx docker; then
    usermod -aG docker gex
    changed "gex do skupiny docker"
  fi
}

ts_refresh() {
  read -r TS_STATE TS_DNS < <(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
print(d.get("BackendState") or "Unknown", ((d.get("Self") or {}).get("DNSName") or "-").rstrip("."))
') || true
  TS_STATE=${TS_STATE:-Unknown}
}

stage_tailscale() {
  if [[ ! -f /usr/share/keyrings/tailscale-archive-keyring.gpg ]]; then
    curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$CODENAME.noarmor.gpg" \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    chmod 644 /usr/share/keyrings/tailscale-archive-keyring.gpg
    changed "klíč repa Tailscale"
  fi
  if put_file /etc/apt/sources.list.d/tailscale.list 644 root:root <<EOF; then
deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu $CODENAME main
EOF
    APT_UPDATED=0
  fi
  ensure_pkgs tailscale
  systemctl is-enabled --quiet tailscaled || {
    systemctl enable --now tailscaled
    changed "tailscaled enable"
  }
  ts_refresh
  if [[ $TS_STATE != Running ]]; then
    todo "vlastník: sudo tailscale up --ssh --hostname=$HOSTNAME_WANT (přihlášení v prohlížeči), pak bootstrap znovu"
    return 0
  fi
  local run_ssh
  run_ssh=$(tailscale debug prefs | python3 -c 'import json, sys; print(json.load(sys.stdin).get("RunSSH", False))')
  if [[ $run_ssh != True ]]; then
    tailscale set --ssh
    changed "Tailscale SSH zapnuto"
  fi
  if ! tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
target = sys.argv[1]
text = sys.stdin.read().strip()
web = (json.loads(text) if text else {}).get("Web") or {}
ok = any(h.get("Proxy") == target for site in web.values() for h in (site.get("Handlers") or {}).values())
sys.exit(0 if ok else 1)
' "$SERVE_TARGET"; then
    if timeout 60 tailscale serve --bg --https=443 "$SERVE_TARGET" >/dev/null; then
      changed "tailscale serve https://$TS_DNS → $SERVE_TARGET"
    else
      todo "vlastník: v admin konzoli Tailscale zapnout MagicDNS a HTTPS Certificates, pak bootstrap znovu (tailscale serve selhal)"
    fi
  fi
}

# DOCKER-USER: z veřejného rozhraní do kontejnerů jen odpovědi na spojení zevnitř (#542).
docker_user_live() {
  local want have
  want=$(printf '%s\n' "-N DOCKER-USER" \
    "-A DOCKER-USER -i $PUB_IF -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN" \
    "-A DOCKER-USER -i $PUB_IF -j DROP" \
    "-A DOCKER-USER -j RETURN")
  iptables -N DOCKER-USER 2>/dev/null || true
  have=$(iptables -S DOCKER-USER)
  [[ $have == "$want" ]] && return 0
  iptables -F DOCKER-USER
  iptables -A DOCKER-USER -i "$PUB_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  iptables -A DOCKER-USER -i "$PUB_IF" -j DROP
  iptables -A DOCKER-USER -j RETURN
  changed "iptables DOCKER-USER (živě)"
}

stage_firewall() {
  if [[ $FIREWALL == oracle-iptables ]]; then
    # Image Oracle: INPUT končí REJECT a pouští jen 22 a služby 169.254.0.0/16; UFW se nepoužívá
    # (Oracle ho na svých image nedoporučuje). Pravidla pro boot nese /etc/iptables/rules.v4.
    local rules=/etc/iptables/rules.v4 tmp
    if ! grep -q '^-A DOCKER-USER -i .* -j DROP' "$rules"; then
      tmp=$(mktemp)
      awk -v ifc="$PUB_IF" '
        /^\*filter/ { print; print ":DOCKER-USER - [0:0]"; in_filter = 1; next }
        in_filter && /^COMMIT/ {
          print "-A DOCKER-USER -i " ifc " -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
          print "-A DOCKER-USER -i " ifc " -j DROP"
          print "-A DOCKER-USER -j RETURN"
          in_filter = 0
        }
        { print }' "$rules" >"$tmp"
      cat "$tmp" >"$rules" # přepis obsahu, práva a vlastník souboru zůstanou
      rm -f "$tmp"
      changed "$rules: DOCKER-USER"
    fi
    docker_user_live
    return 0
  fi

  # Jiný poskytovatel (plán B): UFW nepouští nic dovnitř kromě tailnetu.
  ensure_pkgs ufw
  local status
  status=$(ufw status verbose)
  if ! grep -q 'Default: deny (incoming), allow (outgoing)' <<<"$status"; then
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    changed "ufw výchozí deny incoming / allow outgoing"
  fi
  if ! grep -q 'Anywhere on tailscale0' <<<"$status"; then
    ufw allow in on tailscale0 >/dev/null
    changed "ufw allow in on tailscale0"
  fi
  if ((!CLOSE_PUBLIC_SSH)) && ! grep -q 'gex-temp-ssh' <<<"$(ufw status)"; then
    [[ ${GEX_SSH_CIDR:-} =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] ||
      die "UFW bez Tailscale SSH: nastav GEX_SSH_CIDR=<IP PC>/32, jinak by zapnutí UFW zavřelo SSH"
    ufw allow from "$GEX_SSH_CIDR" to any port 22 proto tcp comment gex-temp-ssh >/dev/null
    changed "ufw dočasně 22 z $GEX_SSH_CIDR"
  fi
  local after_changed=0
  if ! grep -q 'DOCKER-USER' /etc/ufw/after.rules; then
    cat >>/etc/ufw/after.rules <<EOF

# GEXLens bootstrap (#1802): z veřejného rozhraní do kontejnerů jen odpovědi (past DOCKER před UFW, #542)
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -i $PUB_IF -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -i $PUB_IF -j DROP
-A DOCKER-USER -j RETURN
COMMIT
EOF
    changed "/etc/ufw/after.rules: DOCKER-USER"
    after_changed=1
  fi
  if ! grep -q '^Status: active' <<<"$(ufw status)"; then
    ufw --force enable >/dev/null
    changed "ufw enable"
  elif ((after_changed)); then
    ufw reload >/dev/null
  fi
  docker_user_live
}

stage_sshd() {
  if ! id "$RESTIC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$RESTIC_HOME" --create-home --shell /usr/sbin/nologin "$RESTIC_USER"
    changed "uživatel $RESTIC_USER (jen sftp)"
  fi
  ensure_dir "$RESTIC_CHROOT" 755 root:root # chroot musí patřit rootovi a nesmí být zapisovatelný
  ensure_dir "$RESTIC_CHROOT/repo" 700 "$RESTIC_USER:$RESTIC_USER"
  ensure_dir "$RESTIC_HOME/.ssh" 700 "$RESTIC_USER:$RESTIC_USER"
  if [[ -n ${GEX_RESTIC_PUBKEY:-} ]]; then
    [[ $GEX_RESTIC_PUBKEY =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+( .*)?$ ]] || die "GEX_RESTIC_PUBKEY není veřejný SSH klíč"
    printf 'restrict %s\n' "$GEX_RESTIC_PUBKEY" | put_file "$RESTIC_HOME/.ssh/authorized_keys" 600 "$RESTIC_USER:$RESTIC_USER" || true
  elif [[ ! -f $RESTIC_HOME/.ssh/authorized_keys ]]; then
    : | put_file "$RESTIC_HOME/.ssh/authorized_keys" 600 "$RESTIC_USER:$RESTIC_USER" || true
    todo "klíč PC pro uživatele $RESTIC_USER: bootstrap s GEX_RESTIC_PUBKEY='<veřejný klíč>' (#1804, #1851)"
  fi

  if put_file /etc/ssh/sshd_config.d/10-gexlens.conf 644 root:root <<EOF; then
# GEXLens bootstrap (#1802) — spravuje scripts/server/bootstrap.sh, ruční úpravy přepíše.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
# 22: veřejné SSH jen dočasně do Tailscale SSH (na adrese tailnetu port 22 obsluhuje tailscaled).
# $RESTIC_PORT: sftp pro restic z PC; veřejné rozhraní ho nepustí firewall, v tailnetu projde.
Port 22
Port $RESTIC_PORT
Match LocalPort $RESTIC_PORT
    AllowUsers $RESTIC_USER
Match User $RESTIC_USER
    ForceCommand internal-sftp
    ChrootDirectory $RESTIC_CHROOT
    AuthenticationMethods publickey
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
EOF
    sshd -t || die "sshd -t odmítl konfiguraci (/etc/ssh/sshd_config.d/10-gexlens.conf)"
    if systemctl is-active --quiet ssh.socket; then
      # Ubuntu 24.04: porty z sshd_config převezme ssh.socket až po daemon-reload (generátor)
      systemctl daemon-reload
      systemctl restart ssh.socket
    fi
    systemctl try-reload-or-restart ssh.service
  fi
  ss -ltnH "sport = :$RESTIC_PORT" | grep -q LISTEN || die "sshd neposlouchá na portu $RESTIC_PORT"
}

install_tarball() { # url sha256 cílový adresář
  local url=$1 sha=$2 dest=$3 tmp
  tmp=$(mktemp)
  curl -fsSL "$url" -o "$tmp"
  echo "$sha  $tmp" | sha256sum -c --quiet - || die "nesedí sha256 $url"
  rm -rf "$dest"
  install -d -m 755 "$dest"
  tar -xzf "$tmp" -C "$dest"
  rm -f "$tmp"
}

stage_tools() {
  local pa psha ua usha have
  case $ARCH in
    amd64)
      pa=x64 psha=1da6d6d6035d651f20e70ddee7c4c15ac6ab8b9fb40ee92240d91263f28a884b
      ua=x86_64 usha=6426a73c3837e6e2483ee344cbc00f36394d179afcba6183cb77437e67db4af0
      ;;
    arm64)
      pa=arm64 psha=02eb9991af447c7e7fe881acde195961afd397c40b8eb1c2b3856b84ec2d85f2
      ua=aarch64 usha=befa1a59c91e96eb601b0fd9a97c03dd666f17baba644b2b4db9c59a767e387e
      ;;
  esac
  # proměnná PowerShellu, ne shellu
  # shellcheck disable=SC2016
  have=$(/usr/bin/pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>/dev/null) || true
  if [[ $have != "$PWSH_VERSION" ]]; then
    install_tarball "https://github.com/PowerShell/PowerShell/releases/download/v$PWSH_VERSION/powershell-$PWSH_VERSION-linux-$pa.tar.gz" \
      "$psha" /opt/microsoft/powershell/7
    chmod 755 /opt/microsoft/powershell/7/pwsh
    ln -sfn /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh
    changed "PowerShell $PWSH_VERSION"
  fi
  have=$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $2}') || true
  if [[ $have != "$UV_VERSION" ]]; then
    local tmpdir
    tmpdir=$(mktemp -d)
    install_tarball "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$ua-unknown-linux-gnu.tar.gz" \
      "$usha" "$tmpdir/uv"
    install -m 755 "$tmpdir/uv/uv-$ua-unknown-linux-gnu/uv" "$tmpdir/uv/uv-$ua-unknown-linux-gnu/uvx" /usr/local/bin/
    rm -rf "$tmpdir"
    changed "uv $UV_VERSION"
  fi
}

stage_repo() {
  ensure_dir "$ROOT" 755 gex:gex
  if [[ ! -d $ROOT/.git ]]; then
    [[ -z $(ls -A "$ROOT") ]] || die "$ROOT není prázdný a není to git repo"
    sudo -u gex git clone -q "$REPO_URL" "$ROOT"
    changed "git clone $REPO_URL → $ROOT"
  fi
  ensure_dir "$ROOT/data" 755 "$APP_UID:$APP_UID"
  ensure_dir "$ROOT/data/local" 755 "$APP_UID:$APP_UID"
  ensure_dir "$ROOT/secrets" 700 gex:gex
  if [[ ! -f $ROOT/.env ]]; then
    : | put_file "$ROOT/.env" 600 gex:gex || true
  elif [[ $(stat -c '%a %U:%G' "$ROOT/.env") != "600 gex:gex" ]]; then
    chown gex:gex "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    changed "$ROOT/.env práva 600 gex:gex"
  fi
  # Stav stroje (#1847): server začíná v prohlížení, PC bez souboru zůstává aktivní (ADR-0063 bod 3).
  if [[ ! -f $ROOT/data/local/instance.json ]]; then
    printf '{"role": "viewer", "epoch": 0, "changed_at": "%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" |
      put_file "$ROOT/data/local/instance.json" 644 "$APP_UID:$APP_UID" || true
  fi
  # Adresa UI v tailnetu (kap. 14); hodnotu nastavenou ručně nepřepisuje, obsah .env nevypisuje.
  if [[ $TS_STATE == Running && $TS_DNS != - ]] && ! grep -q '^GEXLENS_ALLOWED_ORIGINS=' "$ROOT/.env"; then
    [[ ! -s $ROOT/.env || $(tail -c 1 "$ROOT/.env" | od -An -c | tr -d ' ') == '\n' ]] || echo >>"$ROOT/.env"
    echo "GEXLENS_ALLOWED_ORIGINS=https://$TS_DNS" >>"$ROOT/.env"
    changed ".env: GEXLENS_ALLOWED_ORIGINS=https://$TS_DNS"
  fi
}

# Po ověřeném Tailscale SSH z PC: dočasný klíč PC pryč a veřejné 22 zavřít (Rozhodnuto 19. 11., #1802).
close_public_ssh() {
  ts_refresh
  [[ $TS_STATE == Running ]] || die "Tailscale neběží, veřejné SSH nezavírám (byl by to jediný přístup)"
  [[ $(tailscale debug prefs | python3 -c 'import json, sys; print(json.load(sys.stdin).get("RunSSH", False))') == True ]] ||
    die "Tailscale SSH není zapnuté, veřejné SSH nezavírám"
  local f
  for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [[ -f $f ]] || continue
    if grep -q " $TEMP_KEY_COMMENT\$" "$f"; then
      sed -i "/ $TEMP_KEY_COMMENT\$/d" "$f"
      changed "$f: odebrán dočasný klíč $TEMP_KEY_COMMENT"
    fi
  done
  if [[ $FIREWALL == oracle-iptables ]]; then
    local rules=/etc/iptables/rules.v4 line tmp
    local -a rule
    while IFS= read -r line; do
      read -r -a rule <<<"${line#-A }"
      if iptables -C "${rule[@]}" 2>/dev/null; then iptables -D "${rule[@]}"; fi
      tmp=$(mktemp)
      grep -vxF -- "$line" "$rules" >"$tmp" || true
      cat "$tmp" >"$rules" # přepis obsahu, práva a vlastník souboru zůstanou
      rm -f "$tmp"
      changed "iptables: zavřeno $line"
    done < <(grep -E '^-A INPUT .*--dport 22 .*-j ACCEPT$' "$rules" || true)
  else
    local num
    while num=$(ufw status numbered | awk -F'[][]' '/gex-temp-ssh/ {gsub(/ /, "", $2); print $2; exit}') && [[ -n $num ]]; do
      ufw --force delete "$num" >/dev/null
      changed "ufw: smazáno dočasné 22"
    done
  fi
  if [[ ! -f $CLOSED_MARKER ]]; then
    install -d -m 755 "$STATE_DIR"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$CLOSED_MARKER"
    changed "$CLOSED_MARKER"
  fi
  CLOSE_PUBLIC_SSH=1
  todo "Oracle: smazat ingress 22 v Security Listu VCN (konzole nebo OCI CLI), pak nmap -Pn zvenku = 0 otevřených TCP portů"
  todo "PC: smazat soubory dočasného klíče ~/.ssh/id_ed25519 a id_ed25519.pub (Rozhodnuto 19. 11.)"
}

main() {
  exec </dev/null # nic se neptá; při běhu přes `bash -s` nesmí příkazy číst zbytek skriptu
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  case ${1:-} in
    '') ;;
    --close-public-ssh) CLOSE_PUBLIC_SSH=2 ;;
    -h | --help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *) die "neznámý parametr $1 (jen --close-public-ssh)" ;;
  esac
  local close_requested=$((CLOSE_PUBLIC_SSH == 2))
  CLOSE_PUBLIC_SSH=0
  preflight
  stage_upgrade
  stage_base
  stage_updates_policy
  stage_docker
  stage_user
  stage_tailscale
  stage_firewall
  stage_sshd
  stage_tools
  stage_repo
  ((close_requested)) && close_public_ssh
  ((CLOSE_PUBLIC_SSH)) || todo "po ověření Tailscale SSH z PC: bootstrap --close-public-ssh (před vložením tajemství)"
  log "Hotovo: změn $CHANGES"
  local t
  for t in "${TODO[@]}"; do log "ZBÝVÁ: $t"; done
}

main "$@"

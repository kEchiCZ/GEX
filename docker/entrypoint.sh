#!/bin/sh
# Spouštěč Python kontejnerů: srovná vlastnictví dat a zahodí práva (#542).
#
# Procesy mají běžet neprivilegovaně, ale `./data` je bind mount, jehož
# podadresáře vznikly pod rootem — prosté `USER` v Dockerfile znamená, že engine
# do nich nesmí zapsat (PermissionError z pyarrow při zápisu partice) a sběr dat
# se tiše zastaví. Docker Desktop práva bind mountu NEemuluje tak, jak by se
# z chování kořene mountu zdálo.
#
# Kontejner proto startuje jako root, opraví jen soubory s cizím vlastníkem
# a hned se přepne na UID 10001. Kdo image spustí rovnou pod neprivilegovaným
# uživatelem (`user:` v compose), projde rovnou na exec — chown by stejně selhal.
set -e

DATA_DIR="${GEXLENS_DATA_DIR:-/app/data}"
RUN_UID=10001
RUN_GID=10001

if [ "$(id -u)" = "0" ]; then
    if [ -d "$DATA_DIR" ]; then
        # Jen odchylky: rekurzivní chown celého archivu by při každém startu
        # zbytečně přepisoval desítky tisíc souborů
        find "$DATA_DIR" \( ! -user "$RUN_UID" -o ! -group "$RUN_GID" \) \
            -exec chown "$RUN_UID:$RUN_GID" {} + 2>/dev/null || true
    fi
    exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups "$@"
fi

exec "$@"

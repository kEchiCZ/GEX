"""Diagnostika běžícího procesu (#771): dump zásobníků na signál.

Motivace je konkrétní. Při osmihodinovém výpadku 18. 8. (#770) engine na první
pohled zamrzl — v logu nic, CPU u nuly. Zjistit, na čem visí, nešlo: `py-spy` se
do image nedá doinstalovat (venv je bez pipu) a jediné, co zbylo, byl restart,
který stopu smazal. Registrace níže dovolí vytáhnout zásobník VŠECH vláken
bez ukončení procesu:

    docker kill -s USR1 gex-engine-1 && docker logs --tail 200 gex-engine-1

Dump jde do stderr, tedy do `docker logs` (nebufferovaně, viz PYTHONUNBUFFERED
v docker/python.Dockerfile). Proces běží dál — na rozdíl od SIGABRT, který sice
`PYTHONFAULTHANDLER` taky vypíše, ale zabije ho.

SIGUSR1 na Windows neexistuje; tam se registrace tiše přeskočí (engine se pouští
i lokálně mimo Docker).
"""

import faulthandler
import logging
import signal
import sys

logger = logging.getLogger(__name__)

#: Signál pro dump. USR1 je vyhrazený pro aplikace a nic jiného ho neposílá.
STACK_DUMP_SIGNAL = "SIGUSR1"


def install_stack_dump() -> bool:
    """Zaregistruje dump zásobníků na SIGUSR1. Vrací True, pokud se to povedlo.

    Selhání není důvod nespustit engine — diagnostika je pomůcka, ne provoz.
    """
    sig = getattr(signal, STACK_DUMP_SIGNAL, None)
    if sig is None:  # Windows
        logger.info("Dump zásobníku přes %s není na této platformě k dispozici", STACK_DUMP_SIGNAL)
        return False
    try:
        faulthandler.register(sig, file=sys.stderr, all_threads=True, chain=False)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "Dump zásobníku přes %s se nepodařilo zaregistrovat: %s", STACK_DUMP_SIGNAL, exc
        )
        return False
    logger.info(
        "Zaseknutý proces jde rozebrat bez restartu: `docker kill -s USR1 <kontejner>` "
        "vypíše zásobník všech vláken do logu (#771)"
    )
    return True

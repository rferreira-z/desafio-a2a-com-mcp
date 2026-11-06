"""Log do agente no stderr. Nunca registra o requestState."""

from __future__ import annotations

import sys
from datetime import datetime


def log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} {texto}", file=sys.stderr, flush=True)

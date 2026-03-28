from __future__ import annotations

import threading
import time

from .config import settings
from .services import refresh_all


class SchedulerThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            refresh_all(force=False)
            self._stop_event.wait(settings.调度间隔秒)

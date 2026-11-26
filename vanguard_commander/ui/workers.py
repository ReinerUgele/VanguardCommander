# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Hintergrundaufgaben: blockierende Netzwerkarbeit läuft nie im GUI-Thread."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal


def error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


class Task(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)
    progress = pyqtSignal(object)

    def __init__(self, fn: Callable, args: tuple, kwargs: dict):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 – wird in der Oberfläche angezeigt
            self.failed.emit(exc)
            return
        self.succeeded.emit(result)


class TaskPool(QObject):
    """Hält Referenzen auf laufende Threads, damit Qt sie nicht vorzeitig zerstört."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: set[Task] = set()

    def run(self, fn: Callable, *args: Any, on_ok: Callable | None = None, on_fail: Callable | None = None,
            on_progress: Callable | None = None, with_task: bool = False, **kwargs: Any) -> Task:
        task = Task(fn, args, kwargs)
        if with_task:
            task.kwargs["task"] = task
        if on_ok is not None:
            task.succeeded.connect(on_ok)
        if on_fail is not None:
            task.failed.connect(on_fail)
        if on_progress is not None:
            task.progress.connect(on_progress)
        task.finished.connect(lambda t=task: self._tasks.discard(t))
        self._tasks.add(task)
        task.start()
        return task

    def busy(self) -> bool:
        return any(t.isRunning() for t in self._tasks)

    def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()

    def wait_all(self, ms: int = 3000) -> None:
        for t in list(self._tasks):
            t.wait(ms)

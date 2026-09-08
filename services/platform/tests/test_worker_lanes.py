from __future__ import annotations

from types import SimpleNamespace

from flowweave.bootstrap.settings import Settings
from flowweave.bootstrap.worker import (
    _ALL_TASK_TYPES,
    _DELIVERY_TASK_TYPES,
    _MAINTENANCE_TASK_TYPES,
    _RUNTIME_TASK_TYPES,
    TaskWorker,
)
from flowweave.modules.tasks.application.handlers import HANDLERS


def test_worker_lanes_cover_each_handler_once_with_bounded_total_concurrency() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=4))

    lanes = worker._lane_specs()
    assert sum(slots for _name, _task_types, slots in lanes) == 4
    assert set().union(*(task_types for _name, task_types, _slots in lanes)) == _ALL_TASK_TYPES
    assert _ALL_TASK_TYPES == frozenset(HANDLERS)
    assert not (_RUNTIME_TASK_TYPES & _DELIVERY_TASK_TYPES)
    assert not (_RUNTIME_TASK_TYPES & _MAINTENANCE_TASK_TYPES)
    assert not (_DELIVERY_TASK_TYPES & _MAINTENANCE_TASK_TYPES)


def test_single_worker_uses_one_generic_lane() -> None:
    worker = object.__new__(TaskWorker)
    worker.container = SimpleNamespace(settings=Settings(worker_concurrency=1))

    assert worker._lane_specs() == (("all", _ALL_TASK_TYPES, 1),)

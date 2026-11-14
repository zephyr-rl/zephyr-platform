"""Module (feature package) contract and registry.

A module declares its per-store settings schema and the marketplace capabilities it needs, and
registers its job handlers, event subscriptions, periodic tasks and API routes through a
``ModuleContext``. Each store switches every module on or off independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from fastapi import APIRouter
from pydantic import BaseModel

from app.connectors.base import Capability
from app.core.events import EventBus
from app.core.jobs import JobRegistry


@dataclass(frozen=True)
class PeriodicTask:
    """The scheduler enqueues ``job_type`` every ``interval`` (for each store with the module on
    when ``per_store`` is true)."""

    name: str
    job_type: str
    interval: timedelta
    per_store: bool = True
    module_key: str | None = None


@dataclass
class ModuleContext:
    jobs: JobRegistry
    events: EventBus
    periodic: list[PeriodicTask] = field(default_factory=list)
    routers: list[APIRouter] = field(default_factory=list)


class Module(Protocol):
    key: str
    name: str
    description: str
    settings_schema: type[BaseModel]
    requires: frozenset[Capability]
    default_enabled: bool

    def register(self, ctx: ModuleContext) -> None: ...


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, Module] = {}

    def add(self, module: Module) -> None:
        if module.key in self._modules:
            raise ValueError(f"Module {module.key!r} already registered")
        self._modules[module.key] = module

    def get(self, key: str) -> Module:
        try:
            return self._modules[key]
        except KeyError:
            raise KeyError(f"Unknown module {key!r}") from None

    def all(self) -> list[Module]:
        return list(self._modules.values())

    def register_all(self, ctx: ModuleContext) -> None:
        for module in self._modules.values():
            module.register(ctx)


def default_settings(module: Module) -> dict[str, Any]:
    return module.settings_schema().model_dump(mode="json")

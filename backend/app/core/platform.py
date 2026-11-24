"""Wires the core services together. Every process (api, worker, scheduler) builds one."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.connectors import fake
from app.connectors.base import ConnectorRegistry
from app.connectors.eldorado import connector as eldorado
from app.core import models  # noqa: F401  (every table must be mapped in every process)
from app.core.config import Settings
from app.core.crypto import Fingerprinter, SecretBox
from app.core.db import Database
from app.core.events import EventBus
from app.core.jobs import JobRegistry
from app.core.modules import ModuleContext, ModuleRegistry
from app.modules.catalog import build_module_registry


@dataclass
class Platform:
    settings: Settings
    db: Database
    secrets: SecretBox
    fingerprints: Fingerprinter
    connectors: ConnectorRegistry
    modules: ModuleRegistry
    jobs: JobRegistry
    events: EventBus
    module_context: ModuleContext = field(init=False)

    def __post_init__(self) -> None:
        self.module_context = ModuleContext(jobs=self.jobs, events=self.events)
        self.modules.register_all(self.module_context)
        # Imported here: notifications use the platform type.
        from app.notifications import service as notifications

        notifications.register(self)
        from app.tools.rocketpass import service as rocketpass

        rocketpass.register(self)


def build_connector_registry() -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register(eldorado.INFO, lambda creds: eldorado.EldoradoConnector(creds))
    registry.register(fake.INFO, lambda creds: fake.FakeConnector(creds))
    return registry


def build_platform(settings: Settings) -> Platform:
    return Platform(
        settings=settings,
        db=Database(settings),
        secrets=SecretBox(settings.secret_keys.get_secret_value()),
        fingerprints=Fingerprinter(settings.fingerprint_key.get_secret_value()),
        connectors=build_connector_registry(),
        modules=build_module_registry(),
        jobs=JobRegistry(),
        events=EventBus(),
    )

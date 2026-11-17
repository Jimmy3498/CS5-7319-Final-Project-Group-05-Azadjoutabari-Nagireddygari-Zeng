from pathlib import Path

from shared.infrastructure.sqlite import SQLiteStore
from shared.wiring import compose_services

from .application import LayeredApplication
from .gateway import LayeredGateway


def build(data_root: Path | None = None) -> LayeredGateway:
    root = data_root or Path(__file__).resolve().parents[1] / ".local" / "layered"
    store = SQLiteStore(root / "application.sqlite3")
    store.initialize()
    store.recover_interrupted_turns()
    services = compose_services(root)
    services.gameplay.recover_completed_results()
    services.reporting.catch_up()
    return LayeredGateway(LayeredApplication(services))

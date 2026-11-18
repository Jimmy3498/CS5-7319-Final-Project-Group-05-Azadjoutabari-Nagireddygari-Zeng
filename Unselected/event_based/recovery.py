from shared.domain.services import Services
from shared.models.common import RequestContext

from .bus import InProcessBus
from .messages import Message, Topic


class CompletionRecovery:
    def __init__(self, services: Services, bus: InProcessBus) -> None:
        self.services, self.bus = services, bus

    def recover(self) -> None:
        """Republish committed identifiers only; provider work is never replayed."""
        gameplay = self.services.gameplay
        sessions = getattr(gameplay, "sessions", None)
        if sessions is None:
            return
        for notice in sessions.pending_notifications():
            self.bus.publish(
                Message(
                    notice.event_id,
                    notice.operation_id,
                    notice.operation_id,
                    None,
                    Topic.TURN_COMMITTED,
                    RequestContext("completion-recovery", None, notice.operation_id),
                    notice=notice,
                )
            )

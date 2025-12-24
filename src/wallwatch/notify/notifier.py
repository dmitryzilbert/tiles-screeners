from __future__ import annotations

from dataclasses import dataclass

from wallwatch.state.models import Alert


class Notifier:
    def notify(self, alert: Alert) -> None:
        raise NotImplementedError


@dataclass
class ConsoleNotifier(Notifier):
    def notify(self, alert: Alert) -> None:
        parts = [
            f"event={alert.event}",
            f"instrument={alert.instrument_id}",
            f"side={alert.side}",
            f"price={alert.price}",
            f"size={alert.size}",
            f"ratio={alert.ratio:.2f}",
            f"v_ref={alert.v_ref:.2f}",
            f"distance_ticks={alert.distance_ticks}",
            f"dwell={alert.dwell_seconds:.1f}s",
            f"executed_at_wall={alert.executed_at_wall:.2f}",
            f"cancel_share={alert.cancel_share:.2f}",
            f"reasons={','.join(alert.reasons)}",
        ]
        print(" ".join(parts))


class TelegramNotifier(Notifier):
    def notify(self, alert: Alert) -> None:
        raise NotImplementedError("Telegram notifier is not implemented yet.")


class SlackNotifier(Notifier):
    def notify(self, alert: Alert) -> None:
        raise NotImplementedError("Slack notifier is not implemented yet.")

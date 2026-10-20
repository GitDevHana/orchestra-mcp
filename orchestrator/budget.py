from __future__ import annotations

from .providers import estimate_tokens


class TokenGuard:
    """Monotonic stop guard over reported usage and streaming text estimates.

    A provider may reveal billable usage late. This stops on observation, not at
    an unknowable upstream billing instant. A final response/usage cannot lower
    an already observed peak and silently reopen the budget.
    """

    def __init__(self, slot, prompt: str):
        self.slot = slot
        self.prompt_tokens = estimate_tokens(prompt)
        self.output = ""
        self.extra_output_tokens = 0
        self.reported: int | None = None
        self.peak = max(slot.guard_count, slot.accounted + self.prompt_tokens)

    @property
    def estimated(self) -> int:
        return self.prompt_tokens + estimate_tokens(self.output) + self.extra_output_tokens

    @property
    def reached(self) -> bool:
        return self.slot.spec.max_tokens is not None and self.peak >= self.slot.spec.max_tokens

    def observe(self, event: dict) -> None:
        kind = event["kind"]
        if kind == "text":
            self.output += event["text"]
        elif kind == "final_text":
            # Aggregate results often repeat streamed text. Do not count twice
            # and never reduce the prior high-water mark.
            if estimate_tokens(event["text"]) > estimate_tokens(self.output):
                self.output = event["text"]
        elif kind == "token_output":
            self.extra_output_tokens += max(0, int(event["estimated_tokens"]))
        elif kind == "meter":
            self.reported = max(self.reported or 0, int(event["tokens"]))
        self.peak = max(self.peak, self.slot.accounted + self.estimated,
                        self.slot.accounted + (self.reported or 0))

    def usage(self, *, stopped=False) -> dict:
        charged = self.reported if self.reported is not None else self.estimated
        return {"accounted_tokens": self.slot.accounted + charged,
                "guard_tokens": self.peak,
                "guard_basis": "maximum of observed provider usage and text estimate; monotonic high-water mark",
                "reported_tokens": self.slot.reported + (self.reported or 0),
                "estimated_tokens": self.slot.estimated + (self.estimated if self.reported is None else 0),
                "last_turn_basis": "provider_reported" if self.reported is not None else "estimated",
                "max_tokens": self.slot.spec.max_tokens,
                "limit_reached": self.reached,
                "stopped": stopped,
                "strict_provider_cap": False}

    def commit(self) -> dict:
        usage = self.usage(stopped=self.reached)
        self.slot.accounted = usage["accounted_tokens"]
        self.slot.reported = usage["reported_tokens"]
        self.slot.estimated = usage["estimated_tokens"]
        self.slot.guard_count = self.peak
        return usage

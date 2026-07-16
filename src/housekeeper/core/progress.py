from dataclasses import dataclass


@dataclass
class Progress:
    processed: int = 0
    total: int | None = None
    errors: int = 0

    @property
    def fraction(self) -> float | None:
        return self.processed / self.total if self.total else None

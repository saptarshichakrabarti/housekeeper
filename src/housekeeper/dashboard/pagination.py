import base64
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Cursor:
    sort_value: str
    entry_id: int

    def encode(self) -> str:
        return base64.urlsafe_b64encode(json.dumps(self.__dict__, sort_keys=True).encode()).decode()

    @classmethod
    def decode(cls, value: str | None):
        if not value:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(value.encode()))
            return cls(str(data["sort_value"]), int(data["entry_id"]))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pagination cursor") from exc

from pydantic import ValidationError

from .models import Signal


class SignalParseError(ValueError):
    pass


def parse_signal(payload: dict) -> Signal:
    try:
        return Signal.model_validate(payload)
    except ValidationError as e:
        raise SignalParseError(str(e)) from e

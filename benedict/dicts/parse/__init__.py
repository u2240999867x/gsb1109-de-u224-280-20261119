from benedict.dicts.parse import parse_util
from benedict.dicts.parse.parse_dict import ParseDict, SettlementRecord


def __getattr__(name: str) -> object:
    if name == "SettlementRecord":
        return SettlementRecord
    from benedict import settlement

    try:
        return getattr(settlement, name)
    except AttributeError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc


__all__ = ["ParseDict", "parse_util", "SettlementRecord"]

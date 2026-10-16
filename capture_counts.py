"""Episode-count contract shared by configuration and capture planning; no devices."""
from decimal import Decimal, InvalidOperation

# Technical JSON/JavaScript precision guard, not a collection-size business limit.
MAX_EPISODES = 2**53 - 1


def episode_count(value, name="条数"):
    """Accept positive integral JSON numbers / numeric form strings without rounding."""
    error = f"{name} 应为正整数"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(error)
    try:
        count = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(error) from None
    if not count.is_finite() or count < 1:
        raise ValueError(error)
    if count > MAX_EPISODES:
        raise ValueError(f"{name} 超出系统可精确表示的整数范围")
    if count != count.to_integral_value():
        raise ValueError(error)
    return int(count)

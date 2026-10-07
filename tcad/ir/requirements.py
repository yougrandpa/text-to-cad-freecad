"""Source checks for model-authored numeric requirements."""
from __future__ import annotations

import re


def source_numbers_match(expr) -> bool:
    """Require declared numeric values to occur in the quoted user span.

    Derived dimensions remain assumptions; this is a provenance check, not a
    proof that the expression captures the meaning of the request.
    """
    source = expr.source_text
    numbers = {float(n) for n in re.findall(r"[-+]?\d+(?:\.\d+)?", source)}
    for word, number in (("一个", 1), ("两个", 2), ("三个", 3), ("四个", 4), ("五个", 5)):
        if word in source:
            numbers.add(float(number))

    def values(value):
        if isinstance(value, dict):
            return [n for v in value.values() for n in values(v)]
        if isinstance(value, list):
            return [n for v in value for n in values(v)]
        return [float(value)] if isinstance(value, (int, float)) and not isinstance(value, bool) else []

    expected = values(expr.value)
    return bool(expected) and all(n in numbers for n in expected)

"""Aggregate reported provider usage without discarding partially reported calls."""

from collections.abc import Iterable


def sum_usage(records: Iterable[dict]) -> tuple[dict, int, float]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    missing, count, cost = 0, 0, 0.0
    for usage in records:
        count += 1
        counts = {key: usage.get(key) for key in totals}
        valid = {key: isinstance(value, int) and not isinstance(value, bool) for key, value in counts.items()}
        if not valid["total_tokens"] and valid["prompt_tokens"] and valid["completion_tokens"]:
            counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
            valid["total_tokens"] = True
        missing += not valid["total_tokens"]
        for key, value in counts.items():
            if valid[key]:
                totals[key] += value
        amount = usage.get("cost")
        if isinstance(amount, int | float) and not isinstance(amount, bool):
            cost += float(amount)
    return ({} if count and missing == count else totals), missing, cost

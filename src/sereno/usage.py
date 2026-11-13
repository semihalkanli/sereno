"""Aggregate reported provider usage without discarding partially reported calls.

A usage block reports its charge as `cost` (OpenRouter's cash cost) or, on the opencode Go API, as
`usage.opencode.allowance` against the subscription. Both are read here, so a provider that reports the allowance is
never unpriced; which one a record carries stays visible in the raw usage block and in the record's billing marker.
"""

from collections.abc import Iterable


def allowance_of(usage: object) -> float | None:
    """The subscription allowance a usage block reports (`usage.opencode.allowance`), or None."""
    opencode = usage.get("opencode") if isinstance(usage, dict) else None
    value = opencode.get("allowance") if isinstance(opencode, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def billing_source_of(body: object) -> str | None:
    """Where the charge comes from, as the API names it: the opencode Go API reports it as
    `usage.opencode.billing_source` (for example "go"); a top-level or usage-level field is read too."""
    if not isinstance(body, dict):
        return None
    raw_usage = body.get("usage")
    usage: dict = raw_usage if isinstance(raw_usage, dict) else {}
    raw_opencode = usage.get("opencode")
    opencode: dict = raw_opencode if isinstance(raw_opencode, dict) else {}
    for value in (opencode.get("billing_source"), usage.get("billing_source"), body.get("billing_source")):
        if isinstance(value, str) and value:
            return value
    return None


def usage_charge(usage: object) -> float | None:
    """The money-equivalent charge a usage block reports: the opencode allowance when there is one (the Go API also
    sends `cost: 0`, since nothing is charged in cash), else its `cost`."""
    allowance = allowance_of(usage)
    if allowance is not None:
        return allowance
    amount = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(amount, int | float) and not isinstance(amount, bool):
        return float(amount)
    return None


def sum_usage(records: Iterable[dict]) -> tuple[dict, int, float]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    missing, count, cost = 0, 0, 0.0
    for usage in records:
        count += 1
        counts = {key: usage.get(key) for key in totals}
        valid = {key: isinstance(value, int) and not isinstance(value, bool) for key, value in counts.items()}
        prompt, completion = counts["prompt_tokens"], counts["completion_tokens"]
        if (
            not valid["total_tokens"]
            and isinstance(prompt, int)
            and isinstance(completion, int)
            and valid["prompt_tokens"]
            and valid["completion_tokens"]
        ):
            counts["total_tokens"] = prompt + completion
            valid["total_tokens"] = True
        missing += not valid["total_tokens"]
        for key, value in counts.items():
            if valid[key] and isinstance(value, int):
                totals[key] += value
        amount = usage_charge(usage)
        if amount is not None:
            cost += amount
    return ({} if count and missing == count else totals), missing, cost

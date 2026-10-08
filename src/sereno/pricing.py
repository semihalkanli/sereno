"""Official Anthropic API prices and the cost of one Messages API response from its usage block.

Dependency-free, so the cost hook in scripts/cost_hook can load this file by path under any interpreter. Prices
were copied from https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-08.
"""

# USD per million tokens: input, output, cache read multiplier, and for prompts over the threshold the long-prompt
# input and output prices. Cache writes cost 1.25x (5 minutes) or 2x (1 hour) the input price of the same tier.
ANTHROPIC_PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "read": 0.1, "long_over": 100_000, "long": (0.50, 2.50)},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "read": 0.05},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "read": 0.05},
}
# Pinning inference to the US (inference_geo "us") multiplies every token price.
US_INFERENCE = 1.1


def anthropic_cost(model, usage: dict) -> float | None:
    """The cost of one Messages API response from its usage block; None for a model without a price."""
    price = ANTHROPIC_PRICES.get(model)
    if price is None:
        return None
    uncached = usage.get("input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    hour = split.get("ephemeral_1h_input_tokens") or 0
    minutes = split.get("ephemeral_5m_input_tokens", written - hour) or 0
    input_price, output_price = price["input"], price["output"]
    if "long_over" in price and uncached + read + written > price["long_over"]:
        input_price, output_price = price["long"]
    total = (
        input_price * (uncached + 1.25 * minutes + 2 * hour + price["read"] * read)
        + output_price * (usage.get("output_tokens") or 0)
    ) / 1_000_000
    return total * (US_INFERENCE if usage.get("inference_geo") == "us" else 1)

from sereno.usage import billing_source_of, sum_usage, usage_charge

# The usage block the opencode Go API returned on 2026-11-14: a zero cash cost beside the allowance it charged.
GO_USAGE = {
    "prompt_tokens": 45,
    "completion_tokens": 19,
    "cost": 0,
    "opencode": {"billing_source": "go", "list_cost": 1.8e-05, "allowance": 1.1e-05},
}


def test_the_go_api_charge_is_its_allowance_not_its_zero_cash_cost():
    assert usage_charge(GO_USAGE) == 1.1e-05
    assert sum_usage([GO_USAGE, {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}])[2] == 0.5 + 1.1e-05


def test_a_cash_cost_without_an_allowance_is_the_charge():
    assert usage_charge({"cost": 0.25}) == 0.25
    assert usage_charge({"prompt_tokens": 3}) is None


def test_the_billing_source_is_read_where_the_go_api_reports_it():
    assert billing_source_of({"usage": GO_USAGE}) == "go"
    assert billing_source_of({"billing_source": "top", "usage": {}}) == "top"
    assert billing_source_of({"usage": {"cost": 1}}) is None
    assert billing_source_of(None) is None

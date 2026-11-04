import json
from datetime import date
from io import BytesIO
from urllib.error import HTTPError

import pytest

from purchase_planning.explanations import explain, gateway
from purchase_planning.facts import PlanFacts, Recommendation


@pytest.mark.parametrize("change", [
    {}, {"sku_id": "SKU-2"}, {"code": "invented"}, {"text": "Покрытие запаса — 1,6 мес."},
    {"text": "Покрытие запаса — 1,5 мес. (SKU-2)"},
    {"text": "Наценка за 12 месяцев — 3,0%."},
])
def test_only_grounded_clauses_are_published(change):
    cutoff = date(2026, 8, 13)
    facts = PlanFacts(cutoff, cutoff, (
        Recommendation("SKU-1", "SUP-1", 1234, coverage_months=1.5, stock_until=date(2026, 9, 28), markup_pct_12m=30.5),
    ))

    def reply(_context):
        return json.dumps({"summary": [], "rows": [
            {"sku_id": "SKU-1", "code": "order", "text": "К заказу 1 234 шт."},
            {"sku_id": "SKU-1", "code": "markup", "text": "Наценка за 12 месяцев — 30,5%."},
            {"sku_id": "SKU-1", "code": "coverage", "text": "Покрытие запаса — 1,5 мес.", **change},
        ]})

    result = explain(facts, [], reply)
    if change:
        assert result is None
    else:
        assert result is not None
        assert result.rows["SKU-1"] == (
            "К заказу 1 234 шт. Наценка за 12 месяцев — 30,5%. Покрытие запаса — 1,5 мес."
        )


@pytest.mark.parametrize("failure", [None, "timeout", "http", "bad_json", "truncated"])
def test_gateway_contract_and_failure_fallback(monkeypatch, failure):
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    monkeypatch.delenv("EXPLABS_MODEL", raising=False)
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (Recommendation("SKU-1", "SUP-1", 10),))
    content = '{"summary":[],"rows":[{"sku_id":"SKU-1","code":"order","text":"К заказу 10 шт."}]}'

    def reply(request, *, timeout):
        assert request.full_url == "https://api.experientiallabs.ai/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer test-key"
        assert timeout > 0
        body = json.loads(request.data)
        assert body["model"] == "claude-sonnet-5.5"
        context = json.loads(body["messages"][-1]["content"])
        assert context == {"summary": {}, "rows": {"SKU-1": {"order": "К заказу 10 шт."}}}
        if failure == "timeout":
            raise TimeoutError
        if failure == "http":
            raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
        if failure == "bad_json":
            return BytesIO(b"<html>Bad gateway</html>")
        envelope = {"choices": [{
            "finish_reason": "length" if failure else "stop", "message": {"content": content},
        }]}
        return BytesIO(json.dumps(envelope).encode("utf-8"))

    monkeypatch.setattr("purchase_planning.explanations.urlopen", reply)
    result = explain(facts, [], gateway())
    if failure:
        assert result is None
    else:
        assert result is not None and result.rows == {"SKU-1": "К заказу 10 шт."}


def test_stub_and_missing_key_never_call_the_gateway(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Вызов шлюза без ключа или в CI")

    monkeypatch.setattr("purchase_planning.explanations.urlopen", unexpected_call)
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    assert gateway(stub=True)({}) is None
    monkeypatch.delenv("EXPLABS_API_KEY")
    assert gateway()({}) is None

import json
from dataclasses import asdict
from datetime import date
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest

from purchase_planning.comparison import Driver
from purchase_planning.explanations import Reply, explain, gateway
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
        Recommendation("SKU-OTHER", "SUP-1", 20),
    ))

    def reply(_context, _max_tokens):
        return Reply(content=json.dumps({"rows": [
            {"sku_id": "SKU-1", "code": "order", "text": "К заказу 1 234 шт."},
            {"sku_id": "SKU-1", "code": "markup", "text": "Наценка за 12 месяцев — 30,5%."},
            {"sku_id": "SKU-1", "code": "coverage", "text": "Покрытие запаса — 1,5 мес.", **change},
            {"sku_id": "SKU-OTHER", "code": "order", "text": "К заказу 21 шт."},
        ]}))

    result, stats = explain(facts, [], reply)
    expected = "К заказу 1 234 шт. Наценка за 12 месяцев — 30,5%."
    if not change:
        expected += " Покрытие запаса — 1,5 мес."
    assert result.rows == {"SKU-1": expected}
    assert stats.explained_skus == 1
    assert stats.discarded_skus == 1
    assert stats.discarded_by_reason == {"validation": 1}
    assert stats.rejected_items == 1 + int(bool(change))


@pytest.mark.parametrize(("failure", "override"), [
    (None, None), (None, "custom-model"), ("timeout", None), ("http_503", None),
    ("invalid_json", None), ("truncated", None), ("validation", None),
])
def test_gateway_contract_and_failure_fallback(monkeypatch, failure, override):
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    monkeypatch.delenv("EXPLABS_MODEL", raising=False)
    if override:
        monkeypatch.setenv("EXPLABS_MODEL", override)
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (Recommendation("SKU-1", "SUP-1", 10),))
    content = '{"rows":[{"sku_id":"SKU-1","code":"order","text":"К заказу 10 шт."}]}'

    def reply(request, *, timeout):
        assert request.full_url == "https://api.experientiallabs.ai/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer test-key"
        assert timeout > 0
        body = json.loads(request.data)
        assert body["model"] == (override or "claude-sonnet-5")
        assert body["max_tokens"] > 0
        assert body["response_format"] == {"type": "json_object"}
        context = json.loads(body["messages"][-1]["content"])
        assert context == {"rows": {"SKU-1": {"order": "К заказу 10 шт."}}}
        if failure == "timeout":
            raise TimeoutError
        if failure == "http_503":
            raise HTTPError(request.full_url, 503, "test-key", {}, None)
        envelope = {"choices": [{
            "finish_reason": "length" if failure == "truncated" else "stop",
            "message": {"content": {
                "invalid_json": "{bad test-key " + json.dumps(context, ensure_ascii=False) + " " + "я" * 300,
                "validation": '{"rows":[null]}',
            }.get(failure, f"Пояснение:\n```json\n{content}\n```\nГотово.")},
        }]}
        return BytesIO(json.dumps(envelope).encode("utf-8"))

    monkeypatch.setattr("purchase_planning.explanations.urlopen", reply)
    result, stats = explain(facts, [], gateway())
    if failure:
        assert result.rows == {}
        assert stats.discarded_by_reason == {failure: 1}
        assert "test-key" not in stats.message()
    else:
        assert result.rows == {"SKU-1": "К заказу 10 шт."}
        assert stats.discarded_skus == 0
    if failure == "invalid_json":
        assert stats.first_invalid_response == ("{bad [скрыто] [скрыто] " + "я" * 300)[:300]
    else:
        assert stats.first_invalid_response is None


def _http_429(request, code, error_type, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = {"error": {"code": code, "type": error_type, "message": "Служебный текст test-key"}}
    return HTTPError(request.full_url, 429, "Too Many Requests", headers, BytesIO(json.dumps(body).encode("utf-8")))


def test_gateway_quota_stops_all_batches_without_retry(monkeypatch):
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (Recommendation("A", "SUP-1", 10), Recommendation("B", "SUP-2", 20)))
    calls = []

    def exhausted(request, *, timeout):
        calls.append(request)
        raise _http_429(request, "insufficient_quota", "quota", retry_after=1)

    monkeypatch.setattr("purchase_planning.explanations.urlopen", exhausted)
    monkeypatch.setattr("purchase_planning.explanations.sleep", lambda _: pytest.fail("Повтор при исчерпанной квоте"))
    result, stats = explain(facts, [], gateway())

    assert len(calls) == 1
    assert result.rows == {}
    assert stats.discarded_by_reason == {"llm_budget": 2}
    assert stats.stop_reason == "исчерпан бюджет LLM"
    assert asdict(stats)["http_429_errors"] == [{"code": "insufficient_quota", "type": "quota"}]
    assert "test-key" not in json.dumps(asdict(stats))


@pytest.mark.parametrize(("retry_after", "failures", "delays", "expected_failure"), [
    (7, 1, [7], None),
    (None, 5, [2, 4, 8, 16], "http_429"),
    (600, 1, [], "time_budget"),
])
def test_gateway_rate_limit_retries_within_time_budget(monkeypatch, retry_after, failures, delays, expected_failure):
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    calls, waits = [], []

    def reply(request, *, timeout):
        calls.append(request)
        if len(calls) <= failures:
            raise _http_429(request, "rate_limit_exceeded", "rate_limit", retry_after)
        return BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')

    monkeypatch.setattr("purchase_planning.explanations.urlopen", reply)
    monkeypatch.setattr("purchase_planning.explanations.sleep", waits.append)
    complete = gateway()
    result = complete({"rows": {}}, 512)

    assert len(calls) == len(delays) + 1
    assert waits == delays
    assert result.failure == expected_failure
    assert asdict(result.http_429_errors[0]) == {"code": "rate_limit_exceeded", "type": "rate_limit"}
    if expected_failure is None:
        assert result.content == "{}"
    if expected_failure == "time_budget":
        assert complete({"rows": {}}, 512).failure == "time_budget"
        assert len(calls) == 1


def test_only_largest_orders_are_explained():
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (
        Recommendation("SMALL", "SUP-1", 5),
        Recommendation("MEDIUM", "SUP-1", 30),
        Recommendation("LARGE", "SUP-2", 24, order_multiple=20),
    ))

    def reply(context, _max_tokens):
        assert set(context["rows"]) == {"LARGE"}
        return Reply(content='{"rows":[{"sku_id":"LARGE","code":"order","text":"К заказу 40 шт."}]}')

    result, stats = explain(facts, [], reply, max_skus=1)

    assert result.rows == {"LARGE": "К заказу 40 шт."}
    assert stats.discarded_skus == 0


def test_stub_and_missing_key_never_call_the_gateway(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Вызов шлюза без ключа или в CI")

    monkeypatch.setattr("purchase_planning.explanations.urlopen", unexpected_call)
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    assert gateway(stub=True)({}, 512).failure == "stub"
    monkeypatch.delenv("EXPLABS_API_KEY")
    assert gateway()({}, 512).failure == "no_key"


def test_truncated_batch_retries_each_half_once(monkeypatch):
    monkeypatch.setenv("EXPLABS_API_KEY", "test-key")
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, tuple(Recommendation(f"SKU-{i}", "SUP-1", 10) for i in range(4)))
    calls = []

    def reply(request, *, timeout):
        body = json.loads(request.data)
        rows = json.loads(body["messages"][-1]["content"])["rows"]
        calls.append((list(rows), body["max_tokens"]))
        truncated = "SKU-0" in rows
        items = [{"sku_id": sku, "code": "order", "text": clauses["order"]} for sku, clauses in rows.items()]
        return BytesIO(json.dumps({"choices": [{
            "finish_reason": "length" if truncated else "stop",
            "message": {"content": "{incomplete" if truncated else json.dumps({"rows": items})},
        }]}).encode("utf-8"))

    monkeypatch.setattr("purchase_planning.explanations.urlopen", reply)
    result, stats = explain(facts, [], gateway())
    assert [skus for skus, _ in calls] == [["SKU-0", "SKU-1", "SKU-2", "SKU-3"], ["SKU-0", "SKU-1"], ["SKU-2", "SKU-3"]]
    assert calls[0][1] > calls[1][1] == calls[2][1] > 0
    assert set(result.rows) == {"SKU-2", "SKU-3"}
    assert stats.discarded_by_reason == {"truncated": 2}
    assert stats.truncated_responses == 2


def test_batches_stay_with_supplier_and_summary_is_separate():
    day = date(2026, 8, 13)
    facts = PlanFacts(day, day, (
        *(Recommendation(f"A-{i}", "SUP-1", 10) for i in range(41)),
        Recommendation("B-1", "SUP-2", 20),
    ))
    changes = [{
        "sku_id": "B-1", "order_qty": (10, 20), "segment": (None, None),
        "stockout_risk": (False, False), "drivers": [Driver.SALES], "flags": [],
    }]
    calls = []

    def reply(context, max_tokens):
        if summary := context.get("summary"):
            calls.append(("summary", len(summary)))
            return Reply(content=json.dumps({"summary": [
                {"code": "increased", "text": "Количество к заказу выросло у 1 SKU."},
                {"code": "sales", "text": "Неподтверждённая фраза."},
            ]}))
        rows = context["rows"]
        calls.append((next(iter(rows))[0], len(rows)))
        if "A-40" in rows:
            return Reply(content="{invalid")
        return Reply(content=json.dumps({"rows": [
            {"sku_id": sku, "code": "order", "text": clauses["order"]} for sku, clauses in rows.items()
        ]}))

    result, stats = explain(facts, changes, reply)
    assert calls == [("summary", 2), ("A", 40), ("A", 1), ("B", 1)]
    assert result.summary == "Количество к заказу выросло у 1 SKU."
    assert result.rows["B-1"] == "К заказу 20 шт."
    assert stats.explained_skus == 41
    assert stats.discarded_by_reason == {"invalid_json": 1}
    assert stats.rejected_items == 1

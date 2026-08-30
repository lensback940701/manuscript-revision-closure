from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch


from standalone.pricing import (
    ExchangeRateQuote,
    PriceQuote,
    PriceRefreshError,
    _parse_ecb_usd_cny,
    _parse_deepseek,
    _parse_gemini,
    _parse_kimi,
    calculate_task_cost,
    price_with_fallback,
)


class PricingTests(unittest.TestCase):
    def test_kimi_exact_model_row_parser(self) -> None:
        html = """<table><tr><th>Model</th><th>Unit</th><th>Hit</th><th>Miss</th><th>Output</th></tr>
        <tr><td>kimi-k2.6</td><td>1M tokens</td><td>¥1.10</td><td>¥6.50</td><td>¥27.00</td></tr></table>"""
        self.assertEqual((1.1, 6.5, 27.0), _parse_kimi(html, "kimi-k2.6"))
        with self.assertRaises(PriceRefreshError):
            _parse_kimi(html, "kimi-k3")
        markdown = '["kimi-k2.6", "1M tokens", "¥1.10", "¥6.50", "¥27.00"]'
        self.assertEqual((1.1, 6.5, 27.0), _parse_kimi(markdown, "kimi-k2.6"))
        k27 = '\n'.join(
            [
                '["kimi-k2.7-code", "1M tokens", "¥1.30", "¥6.50", "¥27.00"]',
                '["kimi-k2.7-code-highspeed", "1M tokens", "¥2.60", "¥13.00", "¥54.00"]',
            ]
        )
        self.assertEqual((2.6, 13.0, 54.0), _parse_kimi(k27, "kimi-k2.7-code-highspeed"))

    def test_deepseek_column_parser_does_not_cross_models(self) -> None:
        html = """<table>
        <tr><th>MODEL</th><th>deepseek-v4-flash</th><th>deepseek-v4-pro</th></tr>
        <tr><td>百万tokens输入（缓存命中）</td><td>0.05元</td><td>0.15元</td></tr>
        <tr><td>百万tokens输入（缓存未命中）</td><td>1.5元</td><td>4.5元</td></tr>
        <tr><td>百万tokens输出</td><td>4.5元</td><td>13.5元</td></tr></table>"""
        self.assertEqual((0.15, 4.5, 13.5), _parse_deepseek(html, "deepseek-v4-pro"))

    def test_deepseek_current_peak_and_off_peak_rows_are_time_selected(self) -> None:
        html = """<table>
        <tr><th>MODEL</th><th>deepseek-v4-flash</th><th>deepseek-v4-pro</th></tr>
        <tr><td>价格</td><td>百万tokens输入（缓存命中）</td><td>空闲时段</td><td>0.05元</td><td>0.15元</td></tr>
        <tr><td>高峰时段</td><td>0.10元</td><td>0.30元</td></tr>
        <tr><td>百万tokens输入（缓存未命中）</td><td>空闲时段</td><td>1.5元</td><td>4.5元</td></tr>
        <tr><td>高峰时段</td><td>3.0元</td><td>9.0元</td></tr>
        <tr><td>百万tokens输出</td><td>空闲时段</td><td>4.5元</td><td>13.5元</td></tr>
        <tr><td>高峰时段</td><td>9.0元</td><td>27.0元</td></tr></table>"""
        peak = datetime(2026, 8, 27, 2, tzinfo=timezone.utc)
        off_peak = datetime(2026, 8, 27, 5, tzinfo=timezone.utc)
        self.assertEqual((0.3, 9.0, 27.0), _parse_deepseek(html, "deepseek-v4-pro", at=peak))
        self.assertEqual((0.15, 4.5, 13.5), _parse_deepseek(html, "deepseek-v4-pro", at=off_peak))

    def test_ecb_cross_rate_and_dual_currency_cost(self) -> None:
        xml = """<Envelope><Cube><Cube time="2026-08-24"><Cube currency="USD" rate="1.1664"/>
        <Cube currency="CNY" rate="7.8414"/></Cube></Cube></Envelope>"""
        rate_date, usd_to_cny = _parse_ecb_usd_cny(xml)
        self.assertEqual("2026-08-24", rate_date)
        self.assertAlmostEqual(7.8414 / 1.1664, usd_to_cny)
        fx = ExchangeRateQuote(usd_to_cny, "https://example.invalid/fx", "test", rate_date, "now", "test")
        quote = PriceQuote("test", "model", 1.0, 1.0, 1.0, "CNY", "https://example.invalid", "test", "now", "today", "test")
        result = calculate_task_cost(quote, [{"prompt_tokens": 500000, "completion_tokens": 500000}], fx)
        self.assertEqual(1.0, result["total_estimated_cost_cny"])
        self.assertAlmostEqual(1.0 / usd_to_cny, result["total_estimated_cost_usd"], places=8)

    def test_gemini_parser_uses_target_standard_table_only(self) -> None:
        html = """<h2>Gemini 3.7 Flash</h2><h3>Standard</h3><table>
        <tr><th></th><th>Free Tier</th><th>Paid Tier</th></tr>
        <tr><td>Input price</td><td>Free</td><td>$0.75 through December 31, 2026. $1.50 starting January 1, 2027.</td></tr>
        <tr><td>Output price (including thinking tokens)</td><td>Free</td><td>$3.75 through December 31, 2026. $7.50 starting January 1, 2027.</td></tr>
        <tr><td>Context caching price</td><td>Free</td><td>$0.075 through December 31, 2026. $0.15 starting January 1, 2027.</td></tr>
        </table><h3>Batch</h3><table><tr><td>Input price</td><td>$99</td></tr></table>
        <h2>Gemini 3.5 Flash</h2><h3>Standard</h3><table><tr><td>Input price</td><td>$1.50</td></tr><tr><td>Output price</td><td>$9.00</td></tr></table>"""
        self.assertEqual((0.075, 0.75, 3.75), _parse_gemini(html, "gemini-3.7-flash"))

    def test_cost_uses_actual_cache_split_and_does_not_double_count_reasoning(self) -> None:
        quote = PriceQuote(
            provider="test",
            model="test-model",
            input_cache_hit_per_million=0.1,
            input_cache_miss_per_million=1.0,
            output_per_million=2.0,
            currency="USD",
            source_url="https://example.invalid",
            source_status="test",
            retrieved_at="2026-08-27T00:00:00Z",
            price_as_of="2026-08-27",
            note="测试",
        )
        result = calculate_task_cost(
            quote,
            [
                {
                    "prompt_tokens": 1000,
                    "prompt_cache_hit_tokens": 400,
                    "prompt_cache_miss_tokens": 600,
                    "completion_tokens": 500,
                    "reasoning_tokens": 200,
                }
            ],
        )
        self.assertEqual(0.00164, result["total_estimated_cost"])
        self.assertEqual(200, result["calls"][0]["reasoning_tokens_reported"])

    def test_live_failure_is_explicit_snapshot_not_fake_live_price(self) -> None:
        with patch("standalone.pricing.refresh_price", side_effect=PriceRefreshError("offline")):
            quote = price_with_fallback("deepseek", "deepseek-v4-pro")
        self.assertIsNotNone(quote)
        self.assertEqual("bundled_snapshot_fallback", quote.source_status)
        self.assertIn("不得视为最新价格", quote.note)
        with patch("standalone.pricing.refresh_price", side_effect=PriceRefreshError("offline")):
            self.assertIsNone(price_with_fallback("gemini", "unknown-model"))

    def test_missing_usage_is_not_reported_as_zero_cost(self) -> None:
        quote = PriceQuote(
            provider="test",
            model="test-model",
            input_cache_hit_per_million=None,
            input_cache_miss_per_million=1.0,
            output_per_million=2.0,
            currency="USD",
            source_url="https://example.invalid",
            source_status="test",
            retrieved_at="2026-08-27T00:00:00Z",
            price_as_of="2026-08-27",
            note="测试",
        )
        result = calculate_task_cost(quote, [{}])
        self.assertEqual(
            "estimated_known_usage_subtotal_with_unknown_potential_charge",
            result["status"],
        )
        self.assertEqual(0.0, result["known_usage_estimated_subtotal"])
        self.assertEqual(1, result["unknown_potential_charge_attempt_count"])
        self.assertFalse(result["calls"][0]["usage_complete"])
        self.assertEqual("UNKNOWN_POTENTIAL_CHARGE", result["calls"][0]["billing_status"])
        self.assertIsNone(result["calls"][0]["prompt_tokens"])

    def test_complete_usage_receipt_count_is_independent_of_price_availability(self) -> None:
        result = calculate_task_cost(
            None,
            [{"prompt_tokens": 123, "completion_tokens": 17, "total_tokens": 140}],
        )
        self.assertEqual("price_unavailable", result["status"])
        self.assertEqual(1, result["physical_request_attempt_count"])
        self.assertEqual(1, result["usage_receipt_count"])
        self.assertEqual(0, result["unknown_potential_charge_attempt_count"])
        self.assertTrue(result["calls"][0]["usage_complete"])
        self.assertEqual("KNOWN_USAGE", result["calls"][0]["billing_status"])


if __name__ == "__main__":
    unittest.main()

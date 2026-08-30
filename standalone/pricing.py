"""Official-price refresh and transparent token-cost estimates."""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree

from . import __version__


PRICE_CATALOG_VERSION = "mrc-official-pricing-catalog-2.0"
SNAPSHOT_DATE = "2026-08-27"
GEMINI_PRICE_URL = "https://ai.google.dev/gemini-api/docs/pricing?hl=en"
DEEPSEEK_PRICE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
KIMI_PRICE_URLS = {
    "kimi-k3": "https://platform.kimi.com/docs/pricing/chat-k3",
    "kimi-k2.7-code": "https://platform.kimi.com/docs/pricing/chat-k27-code",
    "kimi-k2.7-code-highspeed": "https://platform.kimi.com/docs/pricing/chat-k27-code",
    "kimi-k2.6": "https://platform.kimi.com/docs/pricing/chat-k26",
}
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ALLOWED_PRICE_HOSTS = frozenset(
    {"ai.google.dev", "api-docs.deepseek.com", "platform.kimi.com", "www.ecb.europa.eu"}
)


@dataclass(frozen=True, slots=True)
class PriceQuote:
    provider: str
    model: str
    input_cache_hit_per_million: float | None
    input_cache_miss_per_million: float
    output_per_million: float
    currency: str
    source_url: str
    source_status: str
    retrieved_at: str
    price_as_of: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "currency": self.currency,
            "input_cache_hit_per_million": self.input_cache_hit_per_million,
            "input_cache_miss_per_million": self.input_cache_miss_per_million,
            "output_per_million": self.output_per_million,
            "source_url": self.source_url,
            "source_status": self.source_status,
            "retrieved_at": self.retrieved_at,
            "price_as_of": self.price_as_of,
            "note": self.note,
            "catalog_version": PRICE_CATALOG_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ExchangeRateQuote:
    usd_to_cny: float
    source_url: str
    source_status: str
    rate_date: str
    retrieved_at: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "usd_to_cny": self.usd_to_cny,
            "source_url": self.source_url,
            "source_status": self.source_status,
            "rate_date": self.rate_date,
            "retrieved_at": self.retrieved_at,
            "note": self.note,
        }


SNAPSHOTS: dict[tuple[str, str], tuple[float | None, float, float, str, str]] = {
    ("kimi", "kimi-k3"): (2.00, 20.00, 100.00, KIMI_PRICE_URLS["kimi-k3"], "CNY"),
    ("kimi", "kimi-k2.7-code"): (1.30, 6.50, 27.00, KIMI_PRICE_URLS["kimi-k2.7-code"], "CNY"),
    ("kimi", "kimi-k2.7-code-highspeed"): (
        2.60,
        13.00,
        54.00,
        KIMI_PRICE_URLS["kimi-k2.7-code-highspeed"],
        "CNY",
    ),
    ("kimi", "kimi-k2.6"): (1.10, 6.50, 27.00, KIMI_PRICE_URLS["kimi-k2.6"], "CNY"),
    ("gemini", "gemini-3.7-flash"): (0.075, 0.75, 3.75, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3.6-flash"): (0.075, 0.75, 3.75, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3.5-flash"): (0.15, 1.50, 9.00, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3.5-flash-lite"): (0.03, 0.30, 2.50, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3.1-flash-lite"): (0.025, 0.25, 1.50, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3.1-pro-preview"): (0.20, 2.00, 12.00, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-3-flash-preview"): (0.05, 0.50, 3.00, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-2.5-pro"): (0.125, 1.25, 10.00, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-2.5-flash"): (0.01, 0.50, 10.00, GEMINI_PRICE_URL, "USD"),
    ("gemini", "gemini-2.5-flash-lite"): (0.03, 0.30, 2.50, GEMINI_PRICE_URL, "USD"),
}

DEEPSEEK_SNAPSHOT_RATES: dict[str, dict[str, tuple[float, float, float]]] = {
    "deepseek-v4-flash": {
        "off-peak": (0.05, 1.50, 4.50),
        "peak": (0.10, 3.00, 9.00),
    },
    "deepseek-v4-pro": {
        "off-peak": (0.15, 4.50, 13.50),
        "peak": (0.30, 9.00, 27.00),
    },
    "deepseek-v4-flash-vision-exp": {
        "off-peak": (0.05, 1.50, 4.50),
        "peak": (0.10, 3.00, 9.00),
    },
}


class PriceRefreshError(RuntimeError):
    """Raised when an official page cannot produce one unambiguous price."""


class ExchangeRateRefreshError(RuntimeError):
    """Raised when the official daily USD/CNY reference rate is unavailable."""


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str, list[str] | None]] = []
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._in_cell = False
        self._cell_text: list[str] = []
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h2", "h3"}:
            self._heading = tag
            self._heading_text = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._in_cell = True
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h2", "h3"} and self._heading == tag:
            self.events.append((tag, _clean(" ".join(self._heading_text)), None))
            self._heading = None
        elif tag in {"td", "th"} and self._in_cell and self._row is not None:
            self._row.append(_clean(" ".join(self._cell_text)))
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.events.append(("row", "", self._row))
            self._row = None


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _dollars(value: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", value)]


def _yuan(value: str) -> list[float]:
    matches = re.findall(
        r"(?:¥|￥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)|([0-9][0-9,]*(?:\.[0-9]+)?)\s*元",
        value,
    )
    return [float((prefixed or suffixed).replace(",", "")) for prefixed, suffixed in matches]


def _fetch_html(url: str, timeout_seconds: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": f"manuscript-revision-closure-standalone/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            final_host = (urllib.parse.urlsplit(response.geturl()).hostname or "").casefold()
            if final_host not in ALLOWED_PRICE_HOSTS:
                raise PriceRefreshError("official pricing page redirected outside the allowlist")
            raw = response.read(4 * 1024 * 1024 + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise PriceRefreshError("official pricing page request failed") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise PriceRefreshError("official pricing page exceeds 4 MiB")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PriceRefreshError("official pricing page is not valid UTF-8") from exc


def _parse_kimi(html: str, model: str) -> tuple[float, float, float]:
    parser = _StructureParser()
    parser.feed(html)
    for kind, _text, row in parser.events:
        if kind == "row" and row and row[0].casefold() == model.casefold() and len(row) >= 5:
            values = [_yuan(cell) for cell in row[2:5]]
            if all(len(items) == 1 for items in values):
                return values[0][0], values[1][0], values[2][0]
    # Mintlify exposes an official Markdown alternate containing the source
    # DocTable expression.  Parse one exact model row, never a fuzzy match.
    normalized = html.replace('<>{"¥"}', "¥ ").replace("</>", "")
    for line in normalized.splitlines():
        if f'["{model}"' not in line:
            continue
        values = _yuan(line)
        if len(values) == 3:
            return values[0], values[1], values[2]
    raise PriceRefreshError("official Kimi table did not contain one exact model row")


def _deepseek_tier(at: datetime | None = None) -> str:
    current = at or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    peak = current.weekday() < 5 and (1 <= current.hour < 4 or 6 <= current.hour < 10)
    return "peak" if peak else "off-peak"


def _parse_deepseek(html: str, model: str, *, at: datetime | None = None) -> tuple[float, float, float]:
    parser = _StructureParser()
    parser.feed(html)
    rows = [row for kind, _text, row in parser.events if kind == "row" and row]
    header_index = -1
    column = -1
    for index, row in enumerate(rows):
        lowered = [cell.casefold() for cell in row]
        if model.casefold() in lowered:
            header_index = index
            column = lowered.index(model.casefold())
            break
    if header_index < 0:
        raise PriceRefreshError("official DeepSeek table did not contain the model")
    model_offset = column - 1
    if model_offset < 0:
        raise PriceRefreshError("official DeepSeek model column was invalid")
    found: dict[str, dict[str, float]] = {"hit": {}, "miss": {}, "output": {}}
    current_metric = ""
    for row in rows[header_index + 1 :]:
        label = " ".join(row[:2]).casefold()
        if "cache hit" in label or ("缓存命中" in label and "未命中" not in label):
            current_metric = "hit"
        elif "cache miss" in label or "缓存未命中" in label:
            current_metric = "miss"
        elif ("output" in label and "token" in label) or "tokens输出" in label:
            current_metric = "output"
        if not current_metric:
            continue
        tier = "standard"
        for cell in row[:3]:
            candidate = cell.casefold()
            if candidate in {"peak", "高峰时段"}:
                tier = "peak"
                break
            if candidate in {"off-peak", "空闲时段"}:
                tier = "off-peak"
                break
        prices = [value for cell in row for value in _yuan(cell)]
        if model_offset < len(prices):
            found[current_metric][tier] = prices[model_offset]
    selected_tier = _deepseek_tier(at)
    selected: list[float] = []
    for metric in ("hit", "miss", "output"):
        rates = found[metric]
        if selected_tier in rates:
            selected.append(rates[selected_tier])
        elif "standard" in rates:
            selected.append(rates["standard"])
        else:
            raise PriceRefreshError("official DeepSeek price fields were incomplete")
    if len(selected) != 3:
        raise PriceRefreshError("official DeepSeek price fields were incomplete")
    return selected[0], selected[1], selected[2]


def _gemini_heading(model: str) -> str:
    return model.removeprefix("gemini-").replace("-", " ").casefold()


def _select_current_gemini_price(values: list[float]) -> float:
    if not values:
        raise PriceRefreshError("official Gemini price cell had no USD value")
    if len(values) == 1:
        return values[0]
    return values[0] if date.today() <= date(2026, 12, 31) else values[-1]


def _parse_gemini(html: str, model: str) -> tuple[float | None, float, float]:
    parser = _StructureParser()
    parser.feed(html)
    target = _gemini_heading(model)
    inside_model = False
    inside_standard = False
    rates: dict[str, float] = {}
    for kind, text, row in parser.events:
        if kind == "h2":
            normalized = text.casefold().replace("gemini ", "", 1)
            inside_model = target in normalized or normalized in target
            inside_standard = False
            if rates and not inside_model:
                break
        elif inside_model and kind == "h3":
            inside_standard = text.casefold().startswith("standard")
        elif inside_model and inside_standard and kind == "row" and row and len(row) >= 2:
            label = row[0].casefold()
            paid_cell = row[-1]
            values = _dollars(paid_cell)
            if "input price" in label and "cache" not in label:
                rates["miss"] = _select_current_gemini_price(values)
            elif "output price" in label:
                rates["output"] = _select_current_gemini_price(values)
            elif "context caching price" in label and values:
                rates["hit"] = _select_current_gemini_price(values)
    if not {"miss", "output"}.issubset(rates):
        raise PriceRefreshError("official Gemini standard price fields were incomplete")
    return rates.get("hit"), rates["miss"], rates["output"]


_CACHE: dict[tuple[str, ...], tuple[float, PriceQuote]] = {}
_CACHE_LOCK = threading.Lock()


def _source_url(provider: str, model: str) -> str:
    if provider == "gemini":
        return GEMINI_PRICE_URL
    if provider == "deepseek":
        return DEEPSEEK_PRICE_URL
    return KIMI_PRICE_URLS.get(model, "https://platform.kimi.com/docs/pricing/chat")


def refresh_price(provider: str, model: str, *, timeout_seconds: float = 10.0) -> PriceQuote:
    key = (provider.casefold().strip(), model.strip())
    provider_name, model_id = key
    tier = _deepseek_tier() if provider_name == "deepseek" else "standard"
    cache_key = (*key, tier)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < 30 * 60:
            return cached[1]
    source_url = _source_url(provider_name, model_id)
    fetch_url = source_url + ".md" if provider_name == "kimi" and model_id in KIMI_PRICE_URLS else source_url
    html = _fetch_html(fetch_url, timeout_seconds)
    if provider_name == "gemini":
        hit, miss, output = _parse_gemini(html, model_id)
    elif provider_name == "deepseek":
        hit, miss, output = _parse_deepseek(html, model_id)
    elif provider_name == "kimi":
        hit, miss, output = _parse_kimi(html, model_id)
    else:
        raise PriceRefreshError("unknown provider pricing source")
    retrieved = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    currency = "USD" if provider_name == "gemini" else "CNY"
    note = "官方标准付费层文本 token 价格；不含税费、免费层、批量/Flex/优先级和账户折扣。"
    if provider_name == "deepseek":
        note = (
            f"官方 {tier} 档文本 token 价格；峰时为 UTC 周一至周五 01:00–04:00 与 06:00–10:00，"
            "其余为谷时；不含税费、赠送余额和账户差异。"
        )
    quote = PriceQuote(
        provider=provider_name,
        model=model_id,
        input_cache_hit_per_million=hit,
        input_cache_miss_per_million=miss,
        output_per_million=output,
        currency=currency,
        source_url=source_url,
        source_status="live_official_page",
        retrieved_at=retrieved,
        price_as_of=retrieved[:10],
        note=note,
    )
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now, quote)
    return quote


def price_with_fallback(provider: str, model: str, *, timeout_seconds: float = 10.0) -> PriceQuote | None:
    try:
        return refresh_price(provider, model, timeout_seconds=timeout_seconds)
    except PriceRefreshError as exc:
        snapshot = SNAPSHOTS.get((provider, model))
        tier = _deepseek_tier() if provider == "deepseek" else "standard"
        if provider == "deepseek" and model in DEEPSEEK_SNAPSHOT_RATES:
            hit, miss, output = DEEPSEEK_SNAPSHOT_RATES[model][tier]
            snapshot = (hit, miss, output, DEEPSEEK_PRICE_URL, "CNY")
        if snapshot is None:
            return None
        hit, miss, output, url, currency = snapshot
        return PriceQuote(
            provider=provider,
            model=model,
            input_cache_hit_per_million=hit,
            input_cache_miss_per_million=miss,
            output_per_million=output,
            currency=currency,
            source_url=url,
            source_status="bundled_snapshot_fallback",
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            price_as_of=SNAPSHOT_DATE,
            note=(
                f"官方页面实时刷新失败：{exc}。使用带日期内置参考价"
                + (f"（DeepSeek {tier} 档）" if provider == "deepseek" else "")
                + "；不得视为最新价格或最终账单。"
            ),
        )


_FX_CACHE: tuple[float, ExchangeRateQuote] | None = None
_FX_CACHE_LOCK = threading.Lock()


def _parse_ecb_usd_cny(xml_text: str) -> tuple[str, float]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ExchangeRateRefreshError("ECB exchange-rate XML is malformed") from exc
    rate_date = ""
    rates: dict[str, float] = {}
    for element in root.iter():
        if element.attrib.get("time"):
            rate_date = element.attrib["time"]
        currency = element.attrib.get("currency")
        raw_rate = element.attrib.get("rate")
        if currency in {"USD", "CNY"} and raw_rate is not None:
            try:
                rates[currency] = float(raw_rate)
            except ValueError as exc:
                raise ExchangeRateRefreshError("ECB exchange-rate XML contains a non-numeric rate") from exc
    if not rate_date or set(rates) != {"USD", "CNY"} or any(value <= 0 for value in rates.values()):
        raise ExchangeRateRefreshError("ECB exchange-rate XML lacks one complete USD/CNY reference pair")
    return rate_date, rates["CNY"] / rates["USD"]


def refresh_exchange_rate(*, timeout_seconds: float = 10.0) -> ExchangeRateQuote:
    global _FX_CACHE
    now = time.monotonic()
    with _FX_CACHE_LOCK:
        if _FX_CACHE and now - _FX_CACHE[0] < 30 * 60:
            return _FX_CACHE[1]
    xml_text = _fetch_html(ECB_FX_URL, timeout_seconds)
    rate_date, usd_to_cny = _parse_ecb_usd_cny(xml_text)
    quote = ExchangeRateQuote(
        usd_to_cny=usd_to_cny,
        source_url=ECB_FX_URL,
        source_status="live_ecb_reference_rate",
        rate_date=rate_date,
        retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        note="欧洲中央银行工作日参考汇率，仅用于双币种估算，不是银行卡、支付平台或实际结算汇率。",
    )
    with _FX_CACHE_LOCK:
        _FX_CACHE = (now, quote)
    return quote


def exchange_rate_or_none(*, timeout_seconds: float = 10.0) -> ExchangeRateQuote | None:
    try:
        return refresh_exchange_rate(timeout_seconds=timeout_seconds)
    except (ExchangeRateRefreshError, PriceRefreshError, OSError, ValueError):
        return None


def calculate_task_cost(
    quote: PriceQuote | None,
    usages: Iterable[Mapping[str, Any]],
    exchange_rate: ExchangeRateQuote | None = None,
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    total_cost = 0.0
    total_usd = 0.0
    total_cny = 0.0
    all_usage_complete = True
    unknown_potential_charge_attempts = 0
    complete_usage_receipts = 0
    for index, usage in enumerate(usages, start=1):
        prompt_raw = usage.get("prompt_tokens")
        output_raw = usage.get("completion_tokens")
        usage_complete = (
            isinstance(prompt_raw, int)
            and not isinstance(prompt_raw, bool)
            and prompt_raw >= 0
            and isinstance(output_raw, int)
            and not isinstance(output_raw, bool)
            and output_raw >= 0
        )
        all_usage_complete = all_usage_complete and usage_complete
        if not usage_complete:
            unknown_potential_charge_attempts += 1
        else:
            complete_usage_receipts += 1
        prompt = prompt_raw if usage_complete else None
        output = output_raw if usage_complete else None
        hit = int(usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens", 0)) or 0) if usage_complete else None
        explicit_miss = usage.get("prompt_cache_miss_tokens")
        miss = (
            int(explicit_miss)
            if usage_complete and isinstance(explicit_miss, int) and explicit_miss >= 0
            else max(0, prompt - hit) if usage_complete and prompt is not None and hit is not None else None
        )
        if usage_complete and (hit < 0 or miss < 0 or hit + miss > prompt):
            hit, miss = 0, prompt
        cost: float | None = None
        cost_usd: float | None = None
        cost_cny: float | None = None
        if quote is not None and usage_complete:
            hit_rate = quote.input_cache_hit_per_million
            if hit_rate is None:
                miss += hit
                hit = 0
                hit_rate = quote.input_cache_miss_per_million
            cost = (
                hit * hit_rate + miss * quote.input_cache_miss_per_million + output * quote.output_per_million
            ) / 1_000_000
            total_cost += cost
            if quote.currency == "USD":
                cost_usd = cost
                cost_cny = cost * exchange_rate.usd_to_cny if exchange_rate is not None else None
            elif quote.currency == "CNY":
                cost_cny = cost
                cost_usd = cost / exchange_rate.usd_to_cny if exchange_rate is not None else None
            if cost_usd is not None:
                total_usd += cost_usd
            if cost_cny is not None:
                total_cny += cost_cny
        calls.append(
            {
                "call_index": index,
                "prompt_tokens": prompt,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "completion_tokens": output,
                "reasoning_tokens_reported": (
                    int(usage.get("reasoning_tokens", 0) or 0) if usage_complete else None
                ),
                "usage_complete": usage_complete,
                "billing_status": "KNOWN_USAGE" if usage_complete else "UNKNOWN_POTENTIAL_CHARGE",
                "estimated_cost": round(cost, 8) if cost is not None else None,
                "estimated_cost_usd": round(cost_usd, 8) if cost_usd is not None else None,
                "estimated_cost_cny": round(cost_cny, 8) if cost_cny is not None else None,
            }
        )
    status = "estimated"
    if quote is None:
        status = "price_unavailable"
    elif not all_usage_complete:
        status = "estimated_known_usage_subtotal_with_unknown_potential_charge"
    limitations = [
        "按 API usage 返回的 token 与官方标准付费层单价估算，不是平台最终账单。",
        "免费层、税费、赠送余额、账户折扣、缓存细则及服务层级可能改变实际扣费。",
    ]
    if quote is not None and exchange_rate is None:
        limitations.append("未取得官方 USD/CNY 参考汇率；仅显示提供商原始计价币种，不伪造换算值。")
    if not all_usage_complete:
        limitations.insert(0, "至少一次 API 响应没有返回完整输入/输出 token；不得把缺失 usage 计为零费用。")
    return {
        "status": status,
        "pricing": quote.as_dict() if quote is not None else None,
        "calls": calls,
        "total_estimated_cost": round(total_cost, 8) if quote is not None else None,
        "known_usage_estimated_subtotal": round(total_cost, 8) if quote is not None else None,
        "total_estimated_cost_usd": (
            round(total_usd, 8)
            if quote is not None and (quote.currency == "USD" or exchange_rate is not None)
            else None
        ),
        "total_estimated_cost_cny": (
            round(total_cny, 8)
            if quote is not None and (quote.currency == "CNY" or exchange_rate is not None)
            else None
        ),
        "currency": quote.currency if quote is not None else None,
        "exchange_rate": exchange_rate.as_dict() if exchange_rate is not None else None,
        "physical_request_attempt_count": len(calls),
        "usage_receipt_count": complete_usage_receipts,
        "unknown_potential_charge_attempt_count": unknown_potential_charge_attempts,
        "billing_limitations": limitations,
    }

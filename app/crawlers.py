from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
import urllib.error
import urllib.request
from urllib.parse import urlparse
from typing import Any

from .config import settings


@dataclass
class CollectedPrice:
    raw_name: str
    standard_name: str
    display_name: str
    developer: str
    context_length: int
    input_price: float
    output_price: float
    currency: str = "USD"
    unit: str = "1M tokens"
    source_url: str = ""
    confidence: float = 1.0


class BaseAdapter:
    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        raise NotImplementedError


class MockAdapter(BaseAdapter):
    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        seed = int(provider_row["id"])
        rng = random.Random(seed)
        models = [
            ("gpt-4o", "GPT-4o", "OpenAI", 128000),
            ("claude-3.5-sonnet", "Claude 3.5 Sonnet", "Anthropic", 200000),
            ("gemini-1.5-pro", "Gemini 1.5 Pro", "Google", 1000000),
            ("deepseek-v3", "DeepSeek V3", "DeepSeek", 64000),
        ]
        prices: list[CollectedPrice] = []
        base_bias = 0.75 + rng.random() * 0.6
        for idx, (standard_name, display_name, developer, context_length) in enumerate(
            models, start=1
        ):
            input_price = round((0.4 + idx * 0.17) * base_bias, 4)
            output_price = round((1.0 + idx * 0.3) * base_bias, 4)
            prices.append(
                CollectedPrice(
                    raw_name=display_name,
                    standard_name=standard_name,
                    display_name=display_name,
                    developer=developer,
                    context_length=context_length,
                    input_price=input_price,
                    output_price=output_price,
                    source_url=str(provider_row["website_url"]),
                    confidence=0.92,
                )
            )
        return prices


class JsonEndpointAdapter(BaseAdapter):
    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        endpoint = provider_row["website_url"]
        request = urllib.request.Request(
            endpoint,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PriceCompareBot/1.0)",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = payload if isinstance(payload, list) else payload.get("items", [])
        collected: list[CollectedPrice] = []
        for item in items:
            collected.append(
                CollectedPrice(
                    raw_name=item["raw_name"],
                    standard_name=item["standard_name"],
                    display_name=item.get("display_name", item["standard_name"]),
                    developer=item.get("developer", ""),
                    context_length=int(item.get("context_length", 0)),
                    input_price=float(item["input_price"]),
                    output_price=float(item["output_price"]),
                    currency=item.get("currency", "USD"),
                    unit=item.get("unit", "1M tokens"),
                    source_url=item.get("source_url", endpoint),
                    confidence=float(item.get("confidence", 1.0)),
                )
            )
        return collected


def _pretty_name(slug: str) -> str:
    parts = re.split(r"[-_]+", slug)
    return " ".join(
        part.upper() if len(part) <= 3 else part.title() for part in parts if part
    )


def _parse_context_length(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"([\d.]+)\s*([KkMm])", value)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "k":
        return int(number * 1000)
    return int(number * 1000000)


class PricingApiAdapter(BaseAdapter):
    api_url = ""

    def _resolve_api_url(self, provider_row: Any) -> str:
        if self.api_url:
            return self.api_url
        website_url = str(provider_row["website_url"] or "").strip()
        if not website_url:
            return ""

        parsed = urlparse(website_url)
        if parsed.scheme and parsed.netloc:
            if "/api/" in parsed.path:
                return website_url.rstrip("/")
            return f"{parsed.scheme}://{parsed.netloc}/api/pricing"
        return website_url.rstrip("/") + "/api/pricing"

    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        endpoint = self._resolve_api_url(provider_row)
        if not endpoint:
            raise RuntimeError("站点地址不能为空")
        payload = fetch_json(endpoint)
        data = payload.get("data", []) if isinstance(payload, dict) else []
        group_ratio = (
            payload.get("group_ratio", {}) if isinstance(payload, dict) else {}
        )
        vendors = (
            {
                vendor.get("id"): vendor.get("name", "")
                for vendor in payload.get("vendors", [])
            }
            if isinstance(payload, dict)
            else {}
        )

        collected: list[CollectedPrice] = []
        for item in data:
            standard_name = str(item.get("model_name", "")).strip()
            if not standard_name:
                continue
            display_name = _pretty_name(standard_name)
            vendor_name = vendors.get(item.get("vendor_id"), "")
            selected_ratio = self._selected_group_ratio(item, group_ratio)
            nominal_input = self._nominal_input(item) * settings.模型倍率换算系数
            nominal_output = self._nominal_output(item) * settings.模型倍率换算系数
            currency = "网站币"
            unit = "倍率"
            collected.append(
                CollectedPrice(
                    raw_name=standard_name,
                    standard_name=standard_name,
                    display_name=display_name,
                    developer=vendor_name,
                    context_length=_parse_context_length(str(item.get("tags", ""))),
                    input_price=round(nominal_input * selected_ratio, 6),
                    output_price=round(nominal_output * selected_ratio, 6),
                    currency=currency,
                    unit=unit,
                    source_url=endpoint,
                    confidence=0.98,
                )
            )
        return collected

    def _nominal_input(self, item: dict[str, Any]) -> float:
        if int(item.get("quota_type", 0) or 0) == 1:
            return float(item.get("model_price", 0) or 0)
        value = item.get("model_ratio", item.get("model_price", 0))
        return float(value or 0)

    def _nominal_output(self, item: dict[str, Any]) -> float:
        if int(item.get("quota_type", 0) or 0) == 1:
            return float(item.get("model_price", 0) or 0)
        value = item.get("completion_ratio", item.get("model_price", 0))
        return float(value or 0)

    def _selected_group_ratio(
        self, item: dict[str, Any], group_ratio: dict[str, Any]
    ) -> float:
        groups = item.get("enable_groups", []) or []
        ratios: list[float] = []
        for group in groups:
            try:
                ratio = float(group_ratio.get(group, 0))
            except Exception:
                continue
            if ratio > 0:
                ratios.append(ratio)
        if not ratios:
            return 1.0
        return min(ratios)


api_adapter = PricingApiAdapter()

ADAPTERS: dict[str, BaseAdapter] = {
    "mock": MockAdapter(),
    "json": JsonEndpointAdapter(),
    "api": api_adapter,
}


def get_adapter(adapter_kind: str) -> BaseAdapter:
    return ADAPTERS.get(adapter_kind, MockAdapter())


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PriceCompareBot/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_collect(
    adapter: BaseAdapter, provider_row: Any
) -> tuple[list[CollectedPrice], str | None]:
    try:
        return adapter.collect(provider_row), None
    except urllib.error.URLError as exc:
        return [], f"网络错误：{exc}"
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)

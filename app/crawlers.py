from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import urllib.error
import urllib.request
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

    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        payload = fetch_json(
            self.api_url or provider_row["website_url"].rstrip("/") + "/api/pricing"
        )
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
                    source_url=self.api_url or provider_row["website_url"],
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


class IkunCodeApiAdapter(PricingApiAdapter):
    api_url = "https://api.ikuncode.cc/api/pricing"


class JimikuApiAdapter(PricingApiAdapter):
    api_url = "https://jimiku.com/api/pricing"


class BltcyApiAdapter(PricingApiAdapter):
    api_url = "https://api.bltcy.ai/api/pricing"


class PackyApiPricingAdapter(PricingApiAdapter):
    api_url = "https://www.packyapi.com/api/pricing"


class BrowserPricingAdapter(BaseAdapter):
    site_name = ""
    start_url = ""
    model_aliases: dict[str, str] = {}

    def collect(self, provider_row: Any) -> list[CollectedPrice]:
        page_text = self._render_page(provider_row)
        collected = self._parse_text(provider_row, page_text)
        if not collected:
            raise RuntimeError(
                f"{self.site_name or provider_row['name']} 未提取到价格信息"
            )
        return collected

    def _render_page(self, provider_row: Any) -> str:
        try:
            playwright_sync = import_module("playwright.sync_api")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("请先安装 playwright 以启用浏览器采集") from exc

        chromium = getattr(playwright_sync, "sync_playwright")
        storage_state_path = str(provider_row["login_state_path"] or "").strip()
        browser_profile_dir = str(provider_row["browser_profile_dir"] or "").strip()
        url = self.start_url or str(provider_row["website_url"])

        with chromium() as p:
            if browser_profile_dir:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=browser_profile_dir,
                    headless=True,
                    locale="zh-CN",
                )
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = p.chromium.launch(headless=True)
                context_kwargs: dict[str, Any] = {"locale": "zh-CN"}
                if storage_state_path and Path(storage_state_path).exists():
                    context_kwargs["storage_state"] = storage_state_path
                context = browser.new_context(**context_kwargs)
                page = context.new_page()

            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                text = page.locator("body").inner_text(timeout=15000)
            finally:
                context.close()
                if not browser_profile_dir:
                    browser.close()  # type: ignore[attr-defined]

        return text

    def _parse_text(self, provider_row: Any, text: str) -> list[CollectedPrice]:
        blocks = [
            block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()
        ]
        collected: list[CollectedPrice] = []
        aliases = self.model_aliases
        for block in blocks:
            lower = block.lower()
            if not any(alias in lower for alias in aliases):
                continue
            prices = self._extract_prices(block)
            if not prices:
                continue
            standard_name = self._match_standard_name(lower)
            if not standard_name:
                continue
            display_name = self._display_name(standard_name)
            collected.append(
                CollectedPrice(
                    raw_name=display_name,
                    standard_name=standard_name,
                    display_name=display_name,
                    developer=self._developer_for(standard_name),
                    context_length=self._context_length_for(standard_name),
                    input_price=prices[0],
                    output_price=prices[1] if len(prices) > 1 else prices[0],
                    currency=self._currency_for(block),
                    unit=self._unit_for(block),
                    source_url=str(provider_row["website_url"]),
                    confidence=0.78,
                )
            )
        return collected

    def _extract_prices(self, block: str) -> list[float]:
        values: list[float] = []
        for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", block):
            value = float(match.group(1))
            if 0 < value < 1000:
                values.append(value)
        return values[:2]

    def _match_standard_name(self, lower: str) -> str:
        for alias, standard_name in self.model_aliases.items():
            if alias in lower:
                return standard_name
        return ""

    def _display_name(self, standard_name: str) -> str:
        return {
            "gpt-4o": "GPT-4o",
            "claude-3.5-sonnet": "Claude 3.5 Sonnet",
            "gemini-1.5-pro": "Gemini 1.5 Pro",
            "deepseek-v3": "DeepSeek V3",
            "qwen2.5-max": "Qwen2.5-Max",
            "gpt-4.1": "GPT-4.1",
        }.get(standard_name, standard_name)

    def _developer_for(self, standard_name: str) -> str:
        return {
            "gpt-4o": "OpenAI",
            "claude-3.5-sonnet": "Anthropic",
            "gemini-1.5-pro": "Google",
            "deepseek-v3": "DeepSeek",
            "qwen2.5-max": "Alibaba",
            "gpt-4.1": "OpenAI",
        }.get(standard_name, "")

    def _context_length_for(self, standard_name: str) -> int:
        return {
            "gpt-4o": 128000,
            "claude-3.5-sonnet": 200000,
            "gemini-1.5-pro": 1000000,
            "deepseek-v3": 64000,
            "qwen2.5-max": 128000,
            "gpt-4.1": 128000,
        }.get(standard_name, 0)

    def _currency_for(self, block: str) -> str:
        if "¥" in block or "元" in block:
            return "CNY"
        return "USD"

    def _unit_for(self, block: str) -> str:
        if "1k" in block.lower() or "千" in block:
            return "1K tokens"
        return "1M tokens"


class IkunCodeAdapter(BrowserPricingAdapter):
    site_name = "IKunCode"
    start_url = "https://api.ikuncode.cc/pricing"
    model_aliases = {
        "gpt-4o": "gpt-4o",
        "claude 3.5": "claude-3.5-sonnet",
        "claude-3.5": "claude-3.5-sonnet",
        "gemini 1.5": "gemini-1.5-pro",
        "deepseek": "deepseek-v3",
        "qwen": "qwen2.5-max",
    }


class JimikuAdapter(BrowserPricingAdapter):
    site_name = "Jimiku"
    start_url = "https://jimiku.com/pricing"
    model_aliases = IkunCodeAdapter.model_aliases


class BltcyAdapter(BrowserPricingAdapter):
    site_name = "柏拉图AI"
    start_url = "https://api.bltcy.ai/models"
    model_aliases = {
        "gpt": "gpt-4.1",
        "claude": "claude-3.5-sonnet",
        "gemini": "gemini-1.5-pro",
        "deepseek": "deepseek-v3",
        "qwen": "qwen2.5-max",
    }


class PackyApiAdapter(BrowserPricingAdapter):
    site_name = "PackyAPI"
    start_url = "https://www.packyapi.com/pricing"
    model_aliases = IkunCodeAdapter.model_aliases


ADAPTERS: dict[str, BaseAdapter] = {
    "mock": MockAdapter(),
    "json": JsonEndpointAdapter(),
    "ikuncode_api": IkunCodeApiAdapter(),
    "jimiku_api": JimikuApiAdapter(),
    "bltcy_api": BltcyApiAdapter(),
    "packyapi_api": PackyApiPricingAdapter(),
    "ikuncode_browser": IkunCodeAdapter(),
    "jimiku_browser": JimikuAdapter(),
    "bltcy_browser": BltcyAdapter(),
    "packyapi_browser": PackyApiPricingAdapter(),
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

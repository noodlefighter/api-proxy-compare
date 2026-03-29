from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from .config import settings
from .crawlers import get_adapter, safe_collect
from .db import db_session, execute, fetch_all, fetch_one, json_payload, utc_now

refresh_lock = threading.Lock()


def seed_demo_data() -> None:
    now = utc_now()
    with db_session() as conn:
        providers = [
            (
                "ikuncode",
                "https://api.ikuncode.cc/pricing",
                "api",
                1.0,
                1,
                "真实站点，接口直抓",
                now,
                now,
            ),
            (
                "jimiku",
                "https://jimiku.com/pricing",
                "api",
                1.0,
                1,
                "真实站点，接口直抓",
                now,
                now,
            ),
            (
                "bltcy",
                "https://api.bltcy.ai/models",
                "api",
                1.0,
                1,
                "真实站点，接口直抓",
                now,
                now,
            ),
            (
                "packyapi",
                "https://www.packyapi.com/pricing",
                "api",
                1.0,
                1,
                "真实站点，接口直抓",
                now,
                now,
            ),
            (
                "demo-a",
                "https://example.com/proxyhub",
                "mock",
                1.0,
                0,
                "演示数据",
                now,
                now,
            ),
            (
                "demo-b",
                "https://example.com/moonrelay",
                "mock",
                1.0,
                0,
                "演示数据",
                now,
                now,
            ),
        ]
        for provider in providers:
            conn.execute(
                """
                INSERT INTO providers (name, website_url, adapter_kind, recharge_ratio, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    website_url=excluded.website_url,
                    adapter_kind=excluded.adapter_kind,
                    recharge_ratio=excluded.recharge_ratio,
                    enabled=excluded.enabled,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                provider,
            )

        conn.executemany(
            """
            INSERT INTO models (standard_name, display_name, developer, context_length, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(standard_name) DO UPDATE SET
                display_name=excluded.display_name,
                developer=excluded.developer,
                context_length=excluded.context_length,
                updated_at=excluded.updated_at
            """,
            [
                ("gpt-4o", "GPT-4o", "OpenAI", 128000, now, now),
                (
                    "claude-3.5-sonnet",
                    "Claude 3.5 Sonnet",
                    "Anthropic",
                    200000,
                    now,
                    now,
                ),
                ("gemini-1.5-pro", "Gemini 1.5 Pro", "Google", 1000000, now, now),
                ("deepseek-v3", "DeepSeek V3", "DeepSeek", 64000, now, now),
            ],
        )

        conn.execute(
            "UPDATE providers SET enabled=0 WHERE website_url LIKE 'https://example.com/%'"
        )
        conn.execute(
            """
            UPDATE providers
            SET adapter_kind='api'
            WHERE adapter_kind NOT IN ('api', 'mock', 'json')
            """
        )

    refresh_all(force=True)


def upsert_price(provider_id: int, model_id: int, data: dict[str, Any]) -> None:
    now = utc_now()
    recharge_ratio = float(data.get("recharge_ratio", 1.0) or 1.0)
    fiat_input_price = float(data["input_price"]) * recharge_ratio
    fiat_output_price = float(data["output_price"]) * recharge_ratio
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO prices (provider_id, model_id, input_price, output_price, fiat_input_price, fiat_output_price, currency, unit, source_url, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, model_id) DO UPDATE SET
                input_price=excluded.input_price,
                output_price=excluded.output_price,
                fiat_input_price=excluded.fiat_input_price,
                fiat_output_price=excluded.fiat_output_price,
                currency=excluded.currency,
                unit=excluded.unit,
                source_url=excluded.source_url,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
            """,
            (
                provider_id,
                model_id,
                data["input_price"],
                data["output_price"],
                fiat_input_price,
                fiat_output_price,
                data["currency"],
                data["unit"],
                data["source_url"],
                data["confidence"],
                now,
            ),
        )


def recompute_provider_fiat_prices(provider_id: int) -> None:
    execute(
        """
        UPDATE prices
        SET fiat_input_price = input_price * COALESCE((SELECT recharge_ratio FROM providers WHERE providers.id = prices.provider_id), 1.0),
            fiat_output_price = output_price * COALESCE((SELECT recharge_ratio FROM providers WHERE providers.id = prices.provider_id), 1.0)
        WHERE provider_id = ?
        """,
        (provider_id,),
    )


def add_raw_snapshot(provider_id: int, kind: str, payload: Any) -> None:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO raw_snapshots (provider_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (provider_id, kind, json_payload(payload), utc_now()),
        )


def log_scrape(
    provider_id: int | None,
    status: str,
    duration_ms: int,
    message: str,
    started_at: str,
    finished_at: str,
) -> None:
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO scrape_runs (provider_id, status, duration_ms, message, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (provider_id, status, duration_ms, message, started_at, finished_at),
        )


def refresh_provider(provider_row: Any) -> tuple[bool, str]:
    adapter = get_adapter(provider_row["adapter_kind"])
    started = time.perf_counter()
    started_at = utc_now()
    collected, error = safe_collect(adapter, provider_row)
    duration_ms = int((time.perf_counter() - started) * 1000)

    if error:
        log_scrape(
            provider_row["id"], "failed", duration_ms, error, started_at, utc_now()
        )
        return False, error

    add_raw_snapshot(
        provider_row["id"], "prices", [item.__dict__ for item in collected]
    )

    model_rows = fetch_all("SELECT id, standard_name FROM models")
    model_map = {row["standard_name"]: row["id"] for row in model_rows}
    for item in collected:
        model_id = model_map.get(item.standard_name)
        if model_id is None:
            with db_session() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO models (standard_name, display_name, developer, context_length, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(standard_name) DO UPDATE SET
                        display_name=excluded.display_name,
                        developer=excluded.developer,
                        context_length=excluded.context_length,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item.standard_name,
                        item.display_name,
                        item.developer,
                        item.context_length,
                        started_at,
                        started_at,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("创建模型记录失败")
                model_id = int(cursor.lastrowid)
                model_map[item.standard_name] = model_id

        model_id_int = int(model_id)
        upsert_price(
            provider_row["id"],
            model_id_int,
            {
                "input_price": item.input_price,
                "output_price": item.output_price,
                "currency": item.currency,
                "unit": item.unit,
                "source_url": item.source_url,
                "confidence": item.confidence,
            },
        )

        with db_session() as conn:
            conn.execute(
                """
                    INSERT INTO model_aliases (provider_id, model_id, raw_name, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(provider_id, raw_name) DO UPDATE SET model_id=excluded.model_id
                    """,
                (provider_row["id"], model_id_int, item.raw_name, started_at),
            )

    log_scrape(
        provider_row["id"],
        "success",
        duration_ms,
        f"抓取到 {len(collected)} 条价格",
        started_at,
        utc_now(),
    )
    return True, f"抓取到 {len(collected)} 条价格"


def refresh_all(force: bool = False) -> list[dict[str, Any]]:
    if not force and not refresh_lock.acquire(blocking=False):
        return [{"provider": None, "ok": False, "message": "刷新任务正在执行中"}]

    if force:
        refresh_lock.acquire()

    try:
        providers = fetch_all("SELECT * FROM providers WHERE enabled=1 ORDER BY id ASC")
        results = []
        for provider in providers:
            ok, message = refresh_provider(provider)
            results.append({"provider": provider["name"], "ok": ok, "message": message})
        return results
    finally:
        refresh_lock.release()


def effective_cost(
    input_price: float,
    output_price: float,
    input_weight: float = 3.0,
    output_weight: float = 1.0,
) -> float:
    return round(input_price * input_weight + output_price * output_weight, 6)


def _matches_default_model_filter(model: Any) -> bool:
    keywords = settings.模型默认过滤关键词
    if not keywords:
        return True
    haystack = " ".join(
        str(model[field] or "")
        for field in ("standard_name", "display_name", "developer")
    ).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def list_models(query: str = "", exact: bool = False):
    if query:
        if exact:
            rows = fetch_all(
                """
                SELECT * FROM models
                WHERE lower(trim(standard_name)) = lower(trim(?))
                   OR lower(trim(display_name)) = lower(trim(?))
                   OR lower(trim(developer)) = lower(trim(?))
                ORDER BY display_name ASC
                """,
                (query, query, query),
            )
        else:
            rows = fetch_all(
                """
                SELECT * FROM models
                WHERE standard_name LIKE ? OR display_name LIKE ? OR developer LIKE ?
                ORDER BY display_name ASC
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            )
    else:
        rows = fetch_all("SELECT * FROM models ORDER BY display_name ASC")

    return [row for row in rows if _matches_default_model_filter(row)]


def list_providers():
    return fetch_all("SELECT * FROM providers ORDER BY enabled DESC, name ASC")


def list_prices_for_model(model_id: int):
    return fetch_all(
        """
        SELECT prices.*, providers.name AS provider_name, providers.website_url, providers.enabled, providers.recharge_ratio,
               COALESCE(prices.fiat_input_price, prices.input_price * providers.recharge_ratio) AS fiat_input_price,
               COALESCE(prices.fiat_output_price, prices.output_price * providers.recharge_ratio) AS fiat_output_price,
               models.display_name, models.standard_name, models.developer, models.context_length
        FROM prices
        JOIN providers ON providers.id = prices.provider_id
        JOIN models ON models.id = prices.model_id
        WHERE prices.model_id = ?
        ORDER BY fiat_input_price ASC, fiat_output_price ASC
        """,
        (model_id,),
    )


def list_prices_for_provider(provider_id: int):
    return fetch_all(
        """
        SELECT prices.*, providers.name AS provider_name, providers.recharge_ratio,
               COALESCE(prices.fiat_input_price, prices.input_price * providers.recharge_ratio) AS fiat_input_price,
               COALESCE(prices.fiat_output_price, prices.output_price * providers.recharge_ratio) AS fiat_output_price,
               models.display_name, models.standard_name, models.developer, models.context_length
        FROM prices
        JOIN providers ON providers.id = prices.provider_id
        JOIN models ON models.id = prices.model_id
        WHERE prices.provider_id = ?
        ORDER BY fiat_input_price ASC, fiat_output_price ASC, models.display_name ASC
        """,
        (provider_id,),
    )


def compare_models(
    model_query: str = "",
    exact: bool = False,
    input_weight: float = 3.0,
    output_weight: float = 1.0,
):
    models = list_models(model_query, exact=exact)
    output = []
    for model in models:
        prices = list_prices_for_model(model["id"])
        for price in prices:
            output.append(
                {
                    "model": model,
                    "price": price,
                    "effective_cost": effective_cost(
                        float(price["fiat_input_price"] or 0),
                        float(price["fiat_output_price"] or 0),
                        input_weight=input_weight,
                        output_weight=output_weight,
                    ),
                }
            )
    output.sort(
        key=lambda item: (
            item["effective_cost"],
            item["price"]["fiat_input_price"],
            item["price"]["fiat_output_price"],
        )
    )
    return output


def latest_runs(limit: int = 20):
    return fetch_all(
        """
        SELECT scrape_runs.*, providers.name AS provider_name
        FROM scrape_runs
        LEFT JOIN providers ON providers.id = scrape_runs.provider_id
        ORDER BY scrape_runs.id DESC
        LIMIT ?
        """,
        (limit,),
    )


def provider_stats():
    return fetch_all(
        """
        SELECT providers.*, COUNT(prices.id) AS price_count
        FROM providers
        LEFT JOIN prices ON prices.provider_id = providers.id
        GROUP BY providers.id
        ORDER BY providers.enabled DESC, providers.name ASC
        """
    )


def model_stats():
    rows = fetch_all(
        """
        SELECT models.*, COUNT(prices.id) AS provider_count
        FROM models
        LEFT JOIN prices ON prices.model_id = models.id
        GROUP BY models.id
        ORDER BY provider_count DESC, models.display_name ASC
        """
    )
    return [row for row in rows if _matches_default_model_filter(row)]


def get_provider(provider_id: int):
    return fetch_one("SELECT * FROM providers WHERE id=?", (provider_id,))


def update_provider(provider_id: int, payload: dict[str, Any]) -> None:
    execute(
        """
        UPDATE providers
        SET website_url=?, adapter_kind=?, recharge_ratio=?, enabled=?, notes=?, updated_at=?
        WHERE id=?
        """,
        (
            payload["website_url"],
            payload["adapter_kind"],
            payload["recharge_ratio"],
            payload["enabled"],
            payload["notes"],
            utc_now(),
            provider_id,
        ),
    )
    recompute_provider_fiat_prices(provider_id)


def create_provider(payload: dict[str, Any]) -> int:
    name = str(payload.get("name", "")).strip()
    website_url = str(payload.get("website_url", "")).strip()
    if not name:
        raise ValueError("站点名称不能为空")
    if not website_url:
        raise ValueError("站点地址不能为空")

    now = utc_now()
    with db_session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO providers (name, website_url, adapter_kind, recharge_ratio, enabled, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                website_url,
                str(payload.get("adapter_kind", "api")).strip() or "api",
                float(payload.get("recharge_ratio", 1.0) or 1.0),
                1 if payload.get("enabled") else 0,
                str(payload.get("notes", "")).strip(),
                now,
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("创建站点失败")
        return int(cursor.lastrowid)


def delete_provider(provider_id: int) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        return cursor.rowcount > 0

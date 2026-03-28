from __future__ import annotations

import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import BASE_DIR, settings
from .db import fetch_one, initialize_db
from .scheduler import SchedulerThread
from .services import (
    compare_models,
    latest_runs,
    get_provider,
    list_prices_for_model,
    list_prices_for_provider,
    list_providers,
    model_stats,
    provider_stats,
    refresh_provider,
    refresh_all,
    seed_demo_data,
    update_provider,
)


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render(template_name: str, **context) -> bytes:
    template = env.get_template(template_name)
    return template.render(**context).encode("utf-8")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PriceCompareHTTP/1.0"

    def _send(
        self,
        body: bytes,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith("/static/"):
            self.serve_static(path)
            return

        if path == "/":
            q = query.get("q", [""])[0].strip()
            selected_model = query.get("model", [""])[0].strip()
            input_weight = float(query.get("in", ["3"])[0])
            output_weight = float(query.get("out", ["1"])[0])
            comparisons = compare_models(
                q or selected_model,
                input_weight=input_weight,
                output_weight=output_weight,
            )
            providers = provider_stats()
            models = model_stats()
            body = render(
                "home.html",
                title="LLM 中转站价格对比",
                q=q,
                selected_model=selected_model,
                input_weight=input_weight,
                output_weight=output_weight,
                comparisons=comparisons[:50],
                providers=providers,
                models=models[:8],
                latest_runs=latest_runs(8),
            )
            self._send(body)
            return

        if path == "/models":
            q = query.get("q", [""])[0].strip()
            exact = query.get("exact", [""])[0] in {"1", "true", "on", "yes"}
            comparisons = compare_models(q, exact=exact)
            body = render(
                "model.html",
                title="模型价格排行",
                q=q,
                exact=exact,
                comparisons=comparisons,
                default_filter=", ".join(settings.模型默认过滤关键词) or "全部",
            )
            self._send(body)
            return

        if path == "/providers":
            providers = provider_stats()
            body = render("providers.html", title="站点列表", providers=providers)
            self._send(body)
            return

        if path.startswith("/provider/") and path.endswith("/refresh"):
            try:
                provider_id = int(path.split("/")[2])
            except Exception:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            provider = get_provider(provider_id)
            if not provider:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            refresh_all(force=True)
            self._redirect(f"/provider/{provider_id}")
            return

        if path.startswith("/provider/"):
            try:
                provider_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            provider = get_provider(provider_id)
            if not provider:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            prices = list_prices_for_provider(provider_id)
            body = render(
                "provider_detail.html",
                title=f"{provider['name']} - 价格详情",
                provider=provider,
                prices=prices,
            )
            self._send(body)
            return

        if path == "/runs":
            body = render("runs.html", title="采集日志", runs=latest_runs(50))
            self._send(body)
            return

        if path == "/refresh":
            refresh_all(force=True)
            self._redirect("/")
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/provider/"):
            try:
                provider_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            form = parse_qs(raw)
            try:
                recharge_ratio = float(form.get("recharge_ratio", ["1.0"])[0])
            except ValueError:
                recharge_ratio = 1.0
            enabled = 1 if form.get("enabled") else 0
            update_provider(
                provider_id,
                {
                    "website_url": form.get("website_url", [""])[0].strip(),
                    "adapter_kind": form.get("adapter_kind", ["mock"])[0].strip(),
                    "recharge_ratio": recharge_ratio,
                    "login_state_path": form.get("login_state_path", [""])[0].strip(),
                    "browser_profile_dir": form.get("browser_profile_dir", [""])[
                        0
                    ].strip(),
                    "enabled": enabled,
                    "notes": form.get("notes", [""])[0].strip(),
                },
            )
            self._redirect(f"/provider/{provider_id}")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def serve_static(self, path: str) -> None:
        file_path = STATIC_DIR / path.removeprefix("/static/")
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = (
            mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        )
        self._send(file_path.read_bytes(), content_type=content_type)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def run_server() -> None:
    initialize_db()
    seed_demo_data()
    scheduler = SchedulerThread()
    scheduler.start()
    server = ThreadingHTTPServer((settings.主机, settings.端口), AppHandler)
    print(f"服务器已启动：http://{settings.主机}:{settings.端口}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.server_close()

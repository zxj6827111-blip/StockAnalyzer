from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_analyzer.main import _resolve_frontend_dist_dir, app


def test_dashboard_page_returns_html() -> None:
    client = TestClient(app)
    redirect = client.get("/dashboard", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers.get("location") == "/ui"

    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert "<div id=\"root\"></div>" in body
    assert "/ui/assets/" in body
    assert "<title>frontend</title>" in body


def test_frontend_dist_resolution_supports_local_vite_output(tmp_path: Path) -> None:
    local_dist = tmp_path / "frontend" / "dist"
    local_dist.mkdir(parents=True)

    assert _resolve_frontend_dist_dir(tmp_path) == local_dist


def test_frontend_dist_resolution_prefers_root_frontend_dist(tmp_path: Path) -> None:
    preferred = tmp_path / "frontend_dist"
    fallback = tmp_path / "frontend" / "dist"
    preferred.mkdir(parents=True)
    fallback.mkdir(parents=True)

    assert _resolve_frontend_dist_dir(tmp_path) == preferred

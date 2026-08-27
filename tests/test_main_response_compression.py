"""响应压缩与前端静态资源缓存头的行为测试。

看板 JSON 响应体偏大（线上 /dashboard/portfolio 约 730KB、/week5/scan/latest 约
460KB），此前既没有 gzip 也没有静态资源强缓存。这里锁定三条契约：

1. 客户端声明 gzip 时，较大的 JSON 响应会被压缩；
2. 客户端未声明 gzip 时不压缩（保证企微/飞书等不声明 Accept-Encoding 的集成不受影响）；
3. hash 化的前端构建产物带 immutable 强缓存，而 index.html 不能强缓存
   （否则前端发版后浏览器会一直拿到旧页面）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stock_analyzer import main as main_module


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main_module.app)


def test_large_json_response_is_gzipped_when_client_accepts(client: TestClient) -> None:
    response = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    # TestClient 会自动解压，确认解压后仍是合法 JSON
    assert isinstance(response.json(), dict)


def test_response_not_compressed_when_client_does_not_accept(client: TestClient) -> None:
    response = client.get("/openapi.json", headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None


def test_small_response_is_not_compressed(client: TestClient) -> None:
    # /health 响应约 580 字节，低于 minimum_size=1024，不应为其付压缩开销
    response = client.get("/health", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None
    assert response.json()["status"] == "ok"


@pytest.mark.skipif(
    main_module._frontend_assets_dir is None
    or not main_module._frontend_assets_dir.exists(),
    reason="frontend/dist/assets 未构建，跳过静态资源缓存头断言",
)
def test_hashed_ui_asset_is_immutably_cacheable(client: TestClient) -> None:
    assets_dir = main_module._frontend_assets_dir
    assert assets_dir is not None
    candidates = sorted(assets_dir.glob("*.js"))
    assert candidates, "期望 frontend/dist/assets 下存在构建产物"

    response = client.get(f"/ui/assets/{candidates[0].name}")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control


@pytest.mark.skipif(
    main_module._frontend_dist_dir is None
    or not (main_module._frontend_dist_dir / "index.html").exists(),
    reason="frontend/dist/index.html 未构建，跳过入口页缓存头断言",
)
def test_ui_entry_page_is_not_immutably_cached(client: TestClient) -> None:
    response = client.get("/ui")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "immutable" not in cache_control, (
        "index.html 强缓存会导致前端发版后浏览器一直拿旧页面"
    )

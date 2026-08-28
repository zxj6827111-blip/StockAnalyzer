"""验证 /ui 的 index.html 响应按需内联注入 window.SA_API_TOKEN（P1-1）。

背景：main.py 的 _verify_api_auth 对写操作端点做 fail-closed 鉴权
（Authorization: Bearer / X-SA-API-Key），但内置前端此前从不附带任何鉴权头，
导致 HistoricalBacktest / News / RuntimeStage / SystemOps 等页面的 POST
请求恒 401。修复方式是后端在返回 index.html 时内联注入
window.SA_API_TOKEN，前端 api.ts 的 apiPost 读取后附加请求头，零用户操作、
不落 localStorage。

只在鉴权确实生效时注入非空值（避免鉴权关闭场景下对每个访问者无意义暴露
token）；index.html 响应必须 no-store，防止浏览器缓存住注入前/旧 token
版本的页面。

用 monkeypatch.setattr 直接改 main_module._config 单例的属性（与
test_main_wecom.py 的既有做法一致），而不是 importlib.reload(main) ——
reload 会重建 main_module._config/app 等模块级单例，与其它测试文件
（同样 ``import stock_analyzer.main as main_module`` 后原地 monkeypatch
同一个 _config 对象）的假设不兼容，会在整套件跑的时候互相污染
（已实测验证：reload 方案导致 test_main_wecom.py 在同一进程内后续失败）。
"""

from __future__ import annotations

import json

from pytest import MonkeyPatch
from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


def test_index_html_injects_empty_token_when_auth_disabled(monkeypatch: MonkeyPatch) -> None:
    """鉴权关闭时仍会注入这段 <script>（前端逻辑对空字符串/undefined 一视同
    仁地不附加请求头），但值必须是空字符串，不能泄露 api_token 配置中的
    残留值——避免鉴权关闭后配置里的旧 token 无意义地暴露给每个访问者。
    """
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", False)
    monkeypatch.setattr(main_module, "_api_auth_force_enabled", lambda: False)
    client = TestClient(main_module.app)

    response = client.get("/ui")

    assert response.status_code == 200
    assert 'window.SA_API_TOKEN="";</script>' in response.text


def test_index_html_never_leaks_configured_token_when_auth_disabled(monkeypatch: MonkeyPatch) -> None:
    """核心安全断言：即使 api_token 配置里残留了一个真实 token，鉴权确实
    关闭时也绝不能把它注入进每个访问者都能看到的页面源码。
    """
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", False)
    monkeypatch.setattr(
        main_module._config.security, "api_token", "leftover-real-token-should-not-leak"
    )
    monkeypatch.setattr(main_module, "_api_auth_force_enabled", lambda: False)
    client = TestClient(main_module.app)

    response = client.get("/ui")

    assert response.status_code == 200
    assert "leftover-real-token-should-not-leak" not in response.text


def test_index_html_injects_configured_token_when_auth_enabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", True)
    monkeypatch.setattr(
        main_module._config.security, "api_token", "test-token-value-for-injection"
    )
    client = TestClient(main_module.app)

    response = client.get("/ui")

    assert response.status_code == 200
    assert 'window.SA_API_TOKEN="test-token-value-for-injection";' in response.text
    assert response.headers.get("cache-control") == "no-store"


def test_direct_index_html_request_still_gets_token_injected(monkeypatch: MonkeyPatch) -> None:
    """/ui/index.html 不能绕过注入逻辑走到裸 FileResponse 分支。"""
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", True)
    monkeypatch.setattr(main_module._config.security, "api_token", "no-bypass-token")
    client = TestClient(main_module.app)

    response = client.get("/ui/index.html")

    assert response.status_code == 200
    assert 'window.SA_API_TOKEN="no-bypass-token";' in response.text


def test_frontend_api_token_injection_enabled_follows_fail_closed_default(
    monkeypatch: MonkeyPatch,
) -> None:
    """_api_auth_force_enabled() fail-closed 依据的是"环境变量是否被显式
    设置过"；这里直接验证 _frontend_api_token_injection_enabled() 在
    "config 认为鉴权关闭，但 fail-closed 强制开启" 场景下仍返回 True
    （因为写端点实际仍然要求鉴权，前端必须带 token 才能用）。
    """
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", False)
    monkeypatch.setattr(main_module, "_api_auth_force_enabled", lambda: True)

    assert main_module._frontend_api_token_injection_enabled() is True



def test_index_html_escapes_angle_brackets_in_token(monkeypatch: MonkeyPatch) -> None:
    """token 含 `</script>` 时不能破坏页面结构（HTML 上下文转义）。

    json.dumps 只负责 JS 字符串字面量层面的转义，**不会**转义 `<`。若直接内联，
    HTML 解析器会在 token 里的 `</script>` 处提前闭合 <script> 标签，后半段
    token 变成页面里的真实 HTML 元素（XSS），同时 window.SA_API_TOKEN 赋值残缺
    导致整个前端 JS 语法错误白屏。token 来自运维自配的 .env 而非外部输入，实际
    不构成攻击面，但随机生成的 token 恰好含 `<` 就会让 UI 整体白屏、且报错只在
    浏览器控制台里，极难排查——所以这里必须转成 \\u003c。
    """
    payload = 'tok</script><img src=x onerror=alert(1)>'
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", True)
    monkeypatch.setattr(main_module._config.security, "api_token", payload)
    client = TestClient(main_module.app)

    response = client.get("/ui")

    assert response.status_code == 200
    # script 标签不能被提前闭合，token 里的标签也不能变成真实 HTML 元素
    assert "</script><img" not in response.text
    assert "<img src=x onerror=alert(1)>" not in response.text
    # `<` 必须以 \u003c 的形式出现（在 JS 字符串里等价于 `<`，HTML 解析器不识别）
    assert "window.SA_API_TOKEN=\"tok\\u003c/script>\\u003cimg src=x onerror=alert(1)>\";" in (
        response.text
    )


def test_index_html_preserves_token_value_through_escaping(monkeypatch: MonkeyPatch) -> None:
    """转义只改变字面量写法，JS 运行时解析出来必须还是原始 token。

    否则前端拿到的 X-SA-API-Key 与后端 _verify_api_auth 期望的值不一致，会从
    "恒 401" 变成更难排查的"看起来带了头但仍然 403"。这里用 json.loads 反解
    注入的字面量来模拟 JS 引擎的解析结果（\\u003c 在 JSON 与 JS 里语义一致）。
    """
    payload = 'tok</script>"quote"\\backslash'
    monkeypatch.setattr(main_module._config.security, "api_auth_enabled", True)
    monkeypatch.setattr(main_module._config.security, "api_token", payload)
    client = TestClient(main_module.app)

    response = client.get("/ui")

    prefix = "window.SA_API_TOKEN="
    start = response.text.index(prefix) + len(prefix)
    end = response.text.index(";</script>", start)
    assert json.loads(response.text[start:end]) == payload

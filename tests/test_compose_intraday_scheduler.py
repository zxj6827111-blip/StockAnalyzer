from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SERVICES = ("api", "scheduler-critical", "scheduler-heavy")
OVERLAYS = (
    "docker-compose.runtime.yml",
    "docker-compose.vendor-overlay.yml",
    "docker-compose.advisory.yml",
    "docker-compose.firstscan.yml",
    "docker-compose.learning.yml",
    "docker-compose.notifications.local.yml",
)


def _load_compose(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_base_compose_defines_only_split_scheduler_services() -> None:
    services = _load_compose("docker-compose.yml")["services"]

    assert "scheduler" not in services
    assert {"api", "scheduler-critical", "scheduler-heavy", "redis"} == set(services)

    critical = services["scheduler-critical"]
    heavy = services["scheduler-heavy"]
    assert critical["image"] == heavy["image"] == "stock-analyzer:latest"
    assert critical["command"] == heavy["command"]
    assert critical["container_name"] != heavy["container_name"]

    critical_env = critical["environment"]
    heavy_env = heavy["environment"]
    assert critical_env["SCHEDULER_GROUP"] == "critical"
    assert heavy_env["SCHEDULER_GROUP"] == "heavy"
    for key in (
        "SCHEDULER_STATE_PATH",
        "SCHEDULER_HEARTBEAT_PATH",
        "SA__SCHEDULER__LEADER_LOCK_PATH",
    ):
        assert critical_env[key] != heavy_env[key]

    for service_name in RUNTIME_SERVICES:
        service = services[service_name]
        assert service["environment"]["SA__DATA_SOURCE__INTRADAY_SUMMARY_PATH"] == (
            "/data/intraday_summary/vendor_intraday_summary.duckdb"
        )
        assert any(
            volume.endswith(":/data/intraday_summary:ro") for volume in service["volumes"]
        )


def test_all_overlays_target_the_three_runtime_services_consistently() -> None:
    for name in OVERLAYS:
        services = _load_compose(name)["services"]
        assert "scheduler" not in services, name
        assert set(RUNTIME_SERVICES).issubset(services), name

        environments = [services[item].get("environment", {}) for item in RUNTIME_SERVICES]
        volumes = [services[item].get("volumes", []) for item in RUNTIME_SERVICES]
        assert environments[0] == environments[1] == environments[2], name
        assert volumes[0] == volumes[1] == volumes[2], name


def test_vendor_overlay_requires_read_only_intraday_duckdb_for_all_services() -> None:
    services = _load_compose("docker-compose.vendor-overlay.yml")["services"]

    for service_name in RUNTIME_SERVICES:
        service = services[service_name]
        environment = service["environment"]
        assert environment["SA__DATA_SOURCE__INTRADAY_RUNTIME_MODE"] == "duckdb_required"
        assert environment["SA__DATA_SOURCE__INTRADAY_ZIP_FALLBACK_ENABLED"] == "false"
        assert (
            environment["SA__DATA_SOURCE__INTRADAY_SUMMARY_PATH"]
            == "/data/intraday_summary/vendor_intraday_summary.duckdb"
        )
        assert any(
            volume.endswith(":/data/intraday_summary:ro") for volume in service["volumes"]
        )
"""Idle queue file-policy, output-path, and checkpoint workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueFileStrategyService:
    """Manage idle queue file policies and output layout."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_effective_write_whitelist(self, task_id: str) -> list[dict[str, object]]:
        service = self._service
        merged: list[dict[str, object]] = []
        global_whitelist = list(service._config.idle_queue.write_whitelist)
        task_whitelist = service._idle_task_manifests.get(task_id, {}).get("write_whitelist", [])
        sources: list[dict[str, object]] = []
        for entry in global_whitelist:
            if isinstance(entry, dict):
                sources.append(entry)
        if isinstance(task_whitelist, list):
            for entry in task_whitelist:
                if isinstance(entry, dict):
                    sources.append(entry)
        for entry in sources:
            entry_task = str(entry.get("task", "")).strip()
            if entry_task != task_id:
                continue
            merged.append(entry)
        return merged

    def idle_path_within(self, path: Path, root: Path) -> bool:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        return resolved_path == resolved_root or resolved_root in resolved_path.parents

    def idle_whitelist_hit(self, task_id: str, path: Path, action: str) -> bool:
        service = self._service
        normalized_action = action.strip().lower()
        resolved_target = path.resolve()
        for entry in self.idle_effective_write_whitelist(task_id=task_id):
            actions = entry.get("actions", [])
            if not isinstance(actions, list):
                continue
            normalized_actions = {str(item).strip().lower() for item in actions}
            if normalized_action not in normalized_actions and "*" not in normalized_actions:
                continue
            paths = entry.get("paths", [])
            if not isinstance(paths, list):
                continue
            for raw_path in paths:
                try:
                    candidate = service._resolve_evolution_path(str(raw_path))
                    resolved_candidate = candidate.resolve()
                except OSError:
                    continue
                if (
                    resolved_target == resolved_candidate
                    or resolved_candidate in resolved_target.parents
                ):
                    return True
        return False

    def idle_forbidden_hit(self, path: Path) -> bool:
        service = self._service
        resolved_target = path.resolve()
        for raw_path in service._config.idle_queue.forbidden_write_paths:
            try:
                candidate = service._resolve_evolution_path(str(raw_path))
                resolved_candidate = candidate.resolve()
            except OSError:
                continue
            if (
                resolved_target == resolved_candidate
                or resolved_candidate in resolved_target.parents
            ):
                return True
        return False

    def idle_assert_write_allowed(self, task_id: str, path: Path, action: str) -> None:
        service = self._service
        normalized_action = action.strip().lower()
        whitelist_hit = self.idle_whitelist_hit(
            task_id=task_id,
            path=path,
            action=normalized_action,
        )
        if normalized_action != "write":
            if whitelist_hit:
                service._record_audit_event(
                    event_type="idle_queue_policy_action",
                    level="info",
                    payload={
                        "task_id": task_id,
                        "action": normalized_action,
                        "path": str(path),
                        "allowed_by": "whitelist",
                    },
                )
                return
            service._record_audit_event(
                event_type="idle_queue_policy_blocked",
                level="error",
                payload={
                    "task_id": task_id,
                    "action": normalized_action,
                    "path": str(path),
                    "reason": "non_write_operation_requires_whitelist",
                },
            )
            raise ValueError(
                f"idle operation not whitelisted for task={task_id} action={action}: {path}"
            )
        if whitelist_hit:
            return
        if self.idle_forbidden_hit(path=path):
            service._record_audit_event(
                event_type="idle_queue_policy_blocked",
                level="error",
                payload={
                    "task_id": task_id,
                    "action": normalized_action,
                    "path": str(path),
                    "reason": "forbidden_path",
                },
            )
            raise ValueError(
                f"idle write path forbidden for task={task_id} action={action}: {path}"
            )

    def idle_infer_task_id_from_output_path(self, path: Path) -> str:
        service = self._service
        root = cast(Path, service._resolve_evolution_path(service._config.idle_queue.output_root))
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
            return ""
        try:
            relative = resolved_path.relative_to(resolved_root)
        except ValueError:
            return ""
        parts = relative.parts
        if len(parts) < 2:
            return ""
        return str(parts[1]).strip()

    def idle_validate_relative_fragment(self, fragment: str, label: str) -> str:
        normalized = fragment.strip().strip("/\\")
        if not normalized:
            return ""
        candidate = Path(normalized)
        if candidate.is_absolute():
            raise ValueError(f"idle {label} must be relative: {fragment}")
        if any(part in {"..", "."} for part in candidate.parts):
            raise ValueError(f"idle {label} contains invalid traversal segment: {fragment}")
        return normalized

    def idle_output_dir(self, trade_date: str, task_id: str, subdir: str = "") -> Path:
        service = self._service
        safe_trade_date = trade_date.strip()
        if not (len(safe_trade_date) == 8 and safe_trade_date.isdigit()):
            raise ValueError(f"idle trade_date must be YYYYMMDD: {trade_date}")
        safe_task_id = task_id.strip()
        if not safe_task_id or any(
            token in safe_task_id for token in ("/", "\\", "..", ":", "\x00")
        ):
            raise ValueError(f"idle task_id contains invalid path token: {task_id}")
        root = cast(Path, service._resolve_evolution_path(service._config.idle_queue.output_root))
        base = root / safe_trade_date / safe_task_id
        safe_subdir = self.idle_validate_relative_fragment(subdir, label="subdir")
        if safe_subdir:
            base = base / safe_subdir
        resolved_root = root.resolve()
        resolved_base = base.resolve()
        if resolved_root not in resolved_base.parents and resolved_base != resolved_root:
            raise ValueError(f"idle output path escaped root: {resolved_base}")
        task_root = (root / safe_trade_date / safe_task_id).resolve()
        if resolved_base != task_root and task_root not in resolved_base.parents:
            raise ValueError(f"idle output dir escaped task root: {resolved_base}")
        self.idle_assert_write_allowed(task_id=safe_task_id, path=base, action="write")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def idle_output_path(
        self,
        trade_date: str,
        task_id: str,
        subdir: str,
        filename: str,
    ) -> Path:
        directory = self.idle_output_dir(
            trade_date=trade_date,
            task_id=task_id,
            subdir=subdir,
        )
        safe_filename = self.idle_validate_relative_fragment(filename, label="filename")
        if not safe_filename or Path(safe_filename).name != safe_filename:
            raise ValueError(f"idle filename must be a plain file name: {filename}")
        path = directory / safe_filename
        resolved_dir = directory.resolve()
        resolved_path = path.resolve()
        if resolved_dir not in resolved_path.parents and resolved_path != resolved_dir:
            raise ValueError(f"idle output path escaped task dir: {resolved_path}")
        self.idle_assert_write_allowed(task_id=task_id, path=path, action="write")
        return path

    def idle_write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        task_id = self.idle_infer_task_id_from_output_path(path)
        if task_id:
            self.idle_assert_write_allowed(task_id=task_id, path=path, action="write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def idle_write_text(self, path: Path, payload: str) -> None:
        task_id = self.idle_infer_task_id_from_output_path(path)
        if task_id:
            self.idle_assert_write_allowed(task_id=task_id, path=path, action="write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    def idle_write_checkpoint(
        self,
        task_id: str,
        trade_date: str,
        phase: str,
        now: datetime,
        extra: dict[str, object],
    ) -> None:
        directory = self.idle_output_dir(
            trade_date=trade_date,
            task_id=task_id,
            subdir="checkpoints",
        )
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        filename = f"{task_id}_ckpt_{timestamp}_{phase}.json"
        payload = {
            "task_id": task_id,
            "trade_date": trade_date,
            "phase": phase,
            "timestamp": now.isoformat(),
            "extra": extra,
        }
        self.idle_write_json(directory / filename, payload)
        self.idle_enforce_checkpoint_retention(directory=directory, task_id=task_id)

    def idle_enforce_checkpoint_retention(self, directory: Path, task_id: str) -> None:
        service = self._service
        keep = max(1, service._config.idle_queue.max_checkpoint_retention)
        items = sorted(directory.glob(f"{task_id}_ckpt_*.json"))
        if len(items) <= keep:
            return
        for stale in items[: len(items) - keep]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                continue

    def idle_find_latest_task_report(
        self,
        task_id: str,
        subdir: str,
        filename: str,
        exclude_trade_date: str,
    ) -> dict[str, object] | None:
        service = self._service
        root = service._resolve_evolution_path(service._config.idle_queue.output_root)
        pattern = f"*/{task_id}/{subdir}/{filename}" if subdir else f"*/{task_id}/{filename}"
        candidates: list[tuple[str, Path]] = []
        for path in root.glob(pattern):
            trade_date = path.parts[-4] if subdir else path.parts[-3]
            if not (len(trade_date) == 8 and trade_date.isdigit()):
                continue
            if trade_date == exclude_trade_date:
                continue
            candidates.append((trade_date, path))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        for trade_date, path in candidates:
            if path.suffix.lower() != ".json":
                return {"trade_date": trade_date, "path": str(path), "payload": {"path": str(path)}}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return {"trade_date": trade_date, "path": str(path), "payload": payload}
        return None

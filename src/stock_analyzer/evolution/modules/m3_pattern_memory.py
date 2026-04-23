"""M3 pattern memory with streaming memmap and safe snapshot deletion."""

from __future__ import annotations

import importlib.util
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class PatternAppendResult:
    """Result of pattern append operation."""

    appended: int
    total: int
    batch_size: int
    used_faiss: bool


@dataclass(frozen=True, slots=True)
class PatternSearchResult:
    """Nearest-neighbor search output."""

    indices: list[int]
    scores: list[float]


class PatternMemoryStore:
    """Pattern memory store for M3 with optional FAISS acceleration."""

    def __init__(
        self,
        base_dir: str | Path,
        vector_dim: int,
        batch_size: int = 50_000,
        delete_delay_hours: int = 24,
        vector_profile_id: str = "",
    ) -> None:
        if vector_dim <= 0:
            raise ValueError("vector_dim must be > 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        self._base_dir = Path(base_dir)
        self._vector_dim = vector_dim
        self._batch_size = batch_size
        self._delete_delay = timedelta(hours=delete_delay_hours)

        normalized_profile_id = _normalize_profile_id(vector_profile_id)
        self._vector_profile_id = normalized_profile_id
        self._store_dir = (
            self._base_dir / "profiles" / normalized_profile_id
            if normalized_profile_id
            else self._base_dir
        )
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._vectors_path = self._store_dir / "vectors.npy"
        self._meta_path = self._store_dir / "meta.json"
        self._snapshot_dir = self._store_dir / "snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_dir = self._snapshot_dir / ".delete_queue"
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)

    def append(self, vectors: NDArray[np.float64]) -> PatternAppendResult:
        """Append vectors using stream-like chunking."""
        matrix = _to_2d_array(vectors=vectors, dim=self._vector_dim)

        existing = self._load_vectors()
        chunks: list[NDArray[np.float64]] = []
        for start in range(0, matrix.shape[0], self._batch_size):
            end = min(start + self._batch_size, matrix.shape[0])
            chunks.append(matrix[start:end])
        appended = np.vstack(chunks) if chunks else np.empty((0, self._vector_dim), dtype=float)

        if existing.size == 0:
            merged = appended
        elif appended.size == 0:
            merged = existing
        else:
            merged = np.vstack([existing, appended])

        np.save(self._vectors_path, merged)
        self._write_meta(total=int(merged.shape[0]))
        return PatternAppendResult(
            appended=int(matrix.shape[0]),
            total=int(merged.shape[0]),
            batch_size=self._batch_size,
            used_faiss=_faiss_available(),
        )

    def search(self, query: NDArray[np.float64], top_k: int = 5) -> PatternSearchResult:
        """Search nearest patterns by cosine similarity."""
        if top_k <= 0:
            raise ValueError("top_k must be > 0")
        query_matrix = _to_2d_array(vectors=query, dim=self._vector_dim)
        if query_matrix.shape[0] != 1:
            raise ValueError("query must contain exactly one vector")
        vectors = self._load_vectors()
        if vectors.size == 0:
            return PatternSearchResult(indices=[], scores=[])

        q = query_matrix[0]
        q_norm = np.linalg.norm(q)
        if q_norm <= 1e-12:
            return PatternSearchResult(indices=[], scores=[])

        v_norm = np.linalg.norm(vectors, axis=1)
        denom = np.maximum(v_norm * q_norm, 1e-12)
        scores = np.dot(vectors, q) / denom

        idx = np.argsort(scores)[::-1][:top_k]
        return PatternSearchResult(
            indices=[int(item) for item in idx.tolist()],
            scores=[float(scores[item]) for item in idx.tolist()],
        )

    def count(self) -> int:
        """Return current vector count."""
        if not self._meta_path.exists():
            return 0
        payload = json.loads(self._meta_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return 0
        total = payload.get("total", 0)
        if isinstance(total, int):
            return max(0, total)
        return 0

    def create_snapshot(self, now: datetime | None = None) -> Path:
        """Create a snapshot folder for current vectors."""
        run_now = now or datetime.now(UTC)
        snapshot = self._snapshot_dir / f"snapshot_{run_now.strftime('%Y%m%d_%H%M%S')}"
        snapshot.mkdir(parents=True, exist_ok=True)
        if self._vectors_path.exists():
            shutil.copy2(self._vectors_path, snapshot / "vectors.npy")
        if self._meta_path.exists():
            shutil.copy2(self._meta_path, snapshot / "meta.json")
        return snapshot

    def safe_remove_snapshot(self, snapshot_path: str | Path, now: datetime | None = None) -> Path:
        """Mark snapshot for delayed deletion via rename."""
        run_now = now or datetime.now(UTC)
        source = Path(snapshot_path)
        if not source.exists():
            raise FileNotFoundError(str(source))
        target = self._quarantine_dir / f"{source.name}.pending.{run_now.strftime('%Y%m%d%H%M%S')}"
        suffix = 1
        while target.exists():
            target = self._quarantine_dir / (
                f"{source.name}.pending.{run_now.strftime('%Y%m%d%H%M%S')}.{suffix}"
            )
            suffix += 1
        source.rename(target)
        return target

    def purge_pending(self, now: datetime | None = None) -> list[Path]:
        """Purge pending snapshots older than delay window."""
        run_now = now or datetime.now(UTC)
        purged: list[Path] = []
        for path in self._quarantine_dir.iterdir():
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if run_now - modified < self._delete_delay:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            purged.append(path)
        return purged

    def _load_vectors(self) -> NDArray[np.float64]:
        if not self._vectors_path.exists():
            return np.empty((0, self._vector_dim), dtype=float)
        loaded = cast(NDArray[np.float64], np.load(self._vectors_path, allow_pickle=False))
        if loaded.ndim != 2 or loaded.shape[1] != self._vector_dim:
            raise ValueError("stored vectors shape mismatch")
        return loaded.astype(float, copy=False)

    def _write_meta(self, total: int) -> None:
        payload = {
            "vector_profile_id": self._vector_profile_id,
            "vector_dim": self._vector_dim,
            "batch_size": self._batch_size,
            "total": total,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._meta_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
        )


def _to_2d_array(vectors: NDArray[np.float64], dim: int) -> NDArray[np.float64]:
    matrix = np.asarray(vectors, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("vectors must be a 2D array")
    if matrix.shape[1] != dim:
        raise ValueError(f"vectors second dimension must be {dim}")
    return matrix


def _faiss_available() -> bool:
    return importlib.util.find_spec("faiss") is not None


def _normalize_profile_id(value: str) -> str:
    text = str(value).strip()
    if not text:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)

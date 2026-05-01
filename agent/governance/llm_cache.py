"""LLM cache for Phase Z v2 PR3 cluster summaries.

A small filesystem-backed key→JSON cache used by
:mod:`agent.governance.ai_cluster_processor` to skip redundant LLM
calls when the same cluster of functions is processed twice.

Storage layout
--------------
``<cache_dir>/llm_cache/cluster_summaries/<key>.json``

The directory is created lazily (``mkdir parents=True, exist_ok=True``).
Writes are atomic via ``tmpfile + os.replace`` so a crashed/aborted
write never produces a partial JSON file or leaves a ``.tmp``
sibling lingering on success.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Optional, Union


class LLMCache:
    """Filesystem-backed cache for cluster summary reports.

    Parameters
    ----------
    cache_dir : str | pathlib.Path
        Base directory that will hold the ``llm_cache/cluster_summaries``
        subtree. The directory is created lazily on first ``put``.
    """

    SUBDIR = os.path.join("llm_cache", "cluster_summaries")

    def __init__(self, cache_dir: Union[str, pathlib.Path]) -> None:
        self.cache_dir = pathlib.Path(cache_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _root(self) -> pathlib.Path:
        return self.cache_dir / "llm_cache" / "cluster_summaries"

    def _path_for(self, key: str) -> pathlib.Path:
        return self._root() / f"{key}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, key: str) -> Optional[dict]:
        """Return cached payload for *key* or ``None`` if absent/unreadable."""
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def put(self, key: str, report) -> None:
        """Atomically write *report* under *key*.

        ``report`` may be a plain dict OR any object with a ``to_dict``
        method (e.g. :class:`ClusterReport`).  Atomicity is provided by
        ``tempfile.NamedTemporaryFile`` + ``os.replace`` — on success no
        ``.tmp`` file is left behind.
        """
        if hasattr(report, "to_dict"):
            payload = report.to_dict()
        elif isinstance(report, dict):
            payload = report
        else:
            # Best-effort: rely on json's default encoder.  Surfacing the
            # TypeError here is fine — tests use dict/dataclass payloads.
            payload = report

        root = self._root()
        root.mkdir(parents=True, exist_ok=True)

        final_path = self._path_for(key)
        # Write to a temp file in the same directory, then os.replace.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(root),
            prefix=f".{key}.",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = tmp.name
        try:
            try:
                json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()
            os.replace(tmp_path, final_path)
        except Exception:
            # Best-effort cleanup of the orphan tmp on failure.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    def invalidate(self, key: str) -> bool:
        """Delete the cache entry for *key*.

        Returns ``True`` if a file was removed, ``False`` otherwise.
        """
        path = self._path_for(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

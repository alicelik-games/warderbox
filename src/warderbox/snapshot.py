"""Snapshot store for rollback support."""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Snapshot:
    """A point-in-time capture of state before an action."""

    id: str
    action_id: str
    tool_name: str
    state: Any
    timestamp: float
    restored: bool = False


class SnapshotStore:
    """In-memory store for snapshots. Supports rollback by action or by index."""

    def __init__(self, max_snapshots: int = 1000) -> None:
        self._snapshots: list[Snapshot] = []
        self._max = max_snapshots

    def capture(self, action_id: str, tool_name: str, state: Any) -> Snapshot:
        """Deep-copy the given state and store it."""
        snap = Snapshot(
            id=uuid.uuid4().hex[:12],
            action_id=action_id,
            tool_name=tool_name,
            state=copy.deepcopy(state),
            timestamp=time.time(),
        )
        self._snapshots.append(snap)
        if len(self._snapshots) > self._max:
            self._snapshots.pop(0)
        return snap

    def get(self, snapshot_id: str) -> Snapshot | None:
        for snap in self._snapshots:
            if snap.id == snapshot_id:
                return snap
        return None

    def get_latest(self) -> Snapshot | None:
        if not self._snapshots:
            return None
        return self._snapshots[-1]

    def get_by_action(self, action_id: str) -> Snapshot | None:
        for snap in reversed(self._snapshots):
            if snap.action_id == action_id:
                return snap
        return None

    def list_all(self) -> list[Snapshot]:
        return list(self._snapshots)

    @property
    def count(self) -> int:
        return len(self._snapshots)

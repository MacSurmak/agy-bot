import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from antigravity_core.config.static_config import StaticConfig


@dataclass(frozen=True, slots=True)
class AgyConversationSummary:
    conversation_id: UUID
    parent_conversation_id: str
    title: str
    preview: str
    step_count: int
    last_modified_time: str
    workspace_uris: tuple[str, ...]


class AgyHistoryReader:
    """Strictly Read-Only reader for agy's internal conversation_summaries.db and transcript.jsonl."""

    def __init__(self, static_config: StaticConfig) -> None:
        self._cfg = static_config

    @property
    def _db_path(self) -> Path:
        return self._cfg.paths.agy_home_dir / "antigravity-cli" / "conversation_summaries.db"

    def get_conversation_summary(self, conversation_id: UUID) -> AgyConversationSummary | None:
        if not self._db_path.is_file():
            return None

        uri = f"file:{self._db_path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                cursor = conn.execute(
                    """
                    SELECT conversation_id, parent_conversation_id, title, preview,
                           step_count, last_modified_time, workspace_uris
                    FROM conversation_summaries
                    WHERE conversation_id = ?
                    """,
                    (str(conversation_id),),
                )
                row = cursor.fetchone()
        except sqlite3.Error:
            return None

        if row is None:
            return None
        return self._map_row(row)

    def list_recent_root_conversations(self, limit: int) -> list[AgyConversationSummary]:
        if not self._db_path.is_file():
            return []

        uri = f"file:{self._db_path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                cursor = conn.execute(
                    """
                    SELECT conversation_id, parent_conversation_id, title, preview,
                           step_count, last_modified_time, workspace_uris
                    FROM conversation_summaries
                    WHERE parent_conversation_id = '' OR parent_conversation_id IS NULL
                    ORDER BY last_modified_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
        except sqlite3.Error:
            return []

        summaries: list[AgyConversationSummary] = []
        for row in rows:
            mapped = self._map_row(row)
            if mapped is not None:
                summaries.append(mapped)
        return summaries

    @staticmethod
    def _map_row(row: tuple[object, ...]) -> AgyConversationSummary | None:
        try:
            conv_uuid = UUID(str(row[0]))
        except ValueError:
            return None

        raw_uris = str(row[6]) if row[6] is not None else "[]"
        try:
            parsed_uris = json.loads(raw_uris)
            uris = tuple(str(u) for u in parsed_uris) if isinstance(parsed_uris, list) else ()
        except json.JSONDecodeError:
            uris = ()

        return AgyConversationSummary(
            conversation_id=conv_uuid,
            parent_conversation_id=str(row[1] or ""),
            title=str(row[2] or "Untitled Session"),
            preview=str(row[3] or ""),
            step_count=int(str(row[4] or 0)),
            last_modified_time=str(row[5] or ""),
            workspace_uris=uris,
        )

    def read_transcript_tail(self, conversation_id: UUID, max_lines: int) -> list[dict[str, object]]:
        transcript_path = (
            self._cfg.paths.agy_home_dir
            / "antigravity-cli"
            / "brain"
            / str(conversation_id)
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if not transcript_path.is_file():
            return []

        try:
            lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        entries: list[dict[str, object]] = []
        for line in lines[-max_lines:]:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    entries.append(obj)
            except json.JSONDecodeError:
                continue
        return entries

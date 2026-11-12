import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import AgyProcessError

logger = structlog.get_logger(__name__)


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
        return (
            self._cfg.paths.agy_home_dir
            / "antigravity-cli"
            / "conversation_summaries.db"
        )

    def _extract_title_from_pbtxt(self, conversation_id: UUID) -> str:
        pbtxt_path = (
            self._cfg.paths.agy_home_dir
            / "antigravity-cli"
            / "annotations"
            / f"{conversation_id}.pbtxt"
        )
        if not pbtxt_path.is_file():
            return ""
        content = pbtxt_path.read_text(encoding="utf-8", errors="replace").strip()
        if content.startswith('title:"') and content.endswith('"'):
            candidate = content[7:-1].strip()
            if candidate and candidate != "Untitled Session":
                return candidate
        return ""

    def get_conversation_summary(
        self, conversation_id: UUID
    ) -> AgyConversationSummary | None:
        pbtxt_title = self._extract_title_from_pbtxt(conversation_id)

        if not self._db_path.is_file():
            if pbtxt_title:
                return AgyConversationSummary(
                    conversation_id=conversation_id,
                    parent_conversation_id="",
                    title=pbtxt_title,
                    preview="",
                    step_count=0,
                    last_modified_time="",
                    workspace_uris=(),
                )
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
        except sqlite3.Error as exc:
            logger.error(
                "Failed to query agy conversation_summaries.db",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
            raise AgyProcessError(
                f"Failed to read conversation_summaries.db for {conversation_id}: {exc}"
            ) from exc

        if row is None:
            if pbtxt_title:
                return AgyConversationSummary(
                    conversation_id=conversation_id,
                    parent_conversation_id="",
                    title=pbtxt_title,
                    preview="",
                    step_count=0,
                    last_modified_time="",
                    workspace_uris=(),
                )
            return None

        summary = self._map_row(row)
        if (not summary.title or summary.title == "Untitled Session") and pbtxt_title:
            return replace(summary, title=pbtxt_title)
        return summary

    def list_recent_root_conversations(
        self, limit: int
    ) -> list[AgyConversationSummary]:
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
        except sqlite3.Error as exc:
            logger.error(
                "Failed to list root conversations from conversation_summaries.db",
                error=str(exc),
            )
            raise AgyProcessError(
                f"Failed to list root conversations from conversation_summaries.db: {exc}"
            ) from exc

        return [self._map_row(row) for row in rows]

    @staticmethod
    def _map_row(row: tuple[object, ...]) -> AgyConversationSummary:
        try:
            conv_uuid = UUID(str(row[0]))
        except ValueError as exc:
            raise AgyProcessError(
                f"Corrupted conversation_id UUID in conversation_summaries.db: {row[0]!r}"
            ) from exc

        raw_uris = str(row[6]) if row[6] is not None else "[]"
        try:
            parsed_uris = json.loads(raw_uris)
            uris = (
                tuple(str(u) for u in parsed_uris)
                if isinstance(parsed_uris, list)
                else ()
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Malformed workspace_uris JSON in conversation_summaries.db",
                error=str(exc),
            )
            uris = ()

        raw_title = str(row[2] or "").strip()
        raw_preview = str(row[3] or "").strip()

        # If agy CLI omitted title in SQLite, resolve from native brain task.md or first preview line
        resolved_title = raw_title
        if not resolved_title or resolved_title == "Untitled Session":
            brain_dir = Path(__file__).resolve().parents[4] / ".gemini" / "antigravity-cli" / "brain" / str(conv_uuid)
            task_file = brain_dir / "task.md"
            if task_file.is_file():
                first_task_line = task_file.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
                cleaned_task = first_task_line.lstrip("#").replace("Task:", "").strip()
                if cleaned_task:
                    resolved_title = cleaned_task
            if not resolved_title and raw_preview:
                resolved_title = raw_preview.splitlines()[0][:60].strip()

        if not resolved_title:
            resolved_title = "Untitled Session"

        return AgyConversationSummary(
            conversation_id=conv_uuid,
            parent_conversation_id=str(row[1] or ""),
            title=resolved_title,
            preview=raw_preview,
            step_count=int(str(row[4] or 0)),
            last_modified_time=str(row[5] or ""),
            workspace_uris=uris,
        )

    def read_transcript_tail(
        self, conversation_id: UUID, max_lines: int
    ) -> list[dict[str, object]]:
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
            lines = transcript_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as exc:
            raise AgyProcessError(
                f"Failed to read transcript file {transcript_path}: {exc}"
            ) from exc

        entries: list[dict[str, object]] = []
        for line in lines[-max_lines:]:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise AgyProcessError(
                    f"Malformed JSONL entry in {transcript_path}: {exc}"
                ) from exc
            if isinstance(obj, dict):
                entries.append(obj)
        return entries

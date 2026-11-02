from uuid import UUID

from telethon.tl.custom import Button

from antigravity_core.domain.enums import EffortLevel, ExecutionMode
from antigravity_core.domain.models import (
    ForumTopic,
    GoogleAccount,
    ScheduledTask,
    TopicSession,
)


def build_control_panel_keyboard() -> list[list[Button]]:
    return [
        [
            Button.inline("🔄 Refresh Dashboard", b"cp:refresh"),
            Button.inline("📊 Probe Quotas", b"cp:probe_quotas"),
        ],
        [
            Button.inline("➕ Create Topic", b"cp:new_topic"),
            Button.inline("🔑 Add / Auth Account", b"cp:auth_menu"),
        ],
        [
            Button.inline("🔀 Switch Default Account", b"cp:switch_default_acc"),
            Button.inline("🔌 Remount SSHFS Hosts", b"cp:remount_hosts"),
        ],
        [
            Button.inline("⏰ Scheduled Tasks", b"cp:tasks_menu"),
            Button.inline("⚙️ DB Settings", b"cp:settings_menu"),
        ],
        [
            Button.inline("🎟 Generate Member Invite", b"cp:create_invite"),
        ],
    ]


def build_account_auth_keyboard(accounts: list[GoogleAccount]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        rows.append(
            [
                Button.inline(
                    f"🔑 Re-Authenticate {acc.slug} ({acc.email_label})",
                    f"cp:oauth_start:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("➕ Register New Account Profile", b"cp:register_new_acc")])
    rows.append([Button.inline("⬅️ Back to Dashboard", b"cp:refresh")])
    return rows


def build_default_account_picker_keyboard(accounts: list[GoogleAccount]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        mark = "⭐ " if acc.is_default else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{acc.slug} ({acc.email_label})",
                    f"cp:set_def_acc:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("⬅️ Back to Dashboard", b"cp:refresh")])
    return rows


def build_tasks_management_keyboard(tasks: list[ScheduledTask]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for task in tasks[:10]:
        short_id = str(task.id)[:8]
        rows.append(
            [
                Button.inline(
                    f"🗑 Cancel {short_id} (Topic {task.topic_id}: {task.trigger_spec})",
                    f"cp:cancel_task:{task.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("⬅️ Back to Dashboard", b"cp:refresh")])
    return rows


def build_settings_menu_keyboard(setting_keys: list[str]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for key in setting_keys:
        rows.append(
            [Button.inline(f"✏️ Edit {key}", f"cp:edit_set:{key}".encode("ascii"))]
        )
    rows.append([Button.inline("⬅️ Back to Dashboard", b"cp:refresh")])
    return rows


def build_topic_header_keyboard(topic: ForumTopic) -> list[list[Button]]:
    tid = topic.topic_id
    mode_label = (
        "🛡 Mode: Full Write ⇄ Plan"
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else "⚡ Mode: Plan (Read-Only) ⇄ Write"
    )
    return [
        [
            Button.inline("🔄 New Session (/new)", f"ws:new:{tid}".encode("ascii")),
            Button.inline("🕰 Past Sessions", f"ws:history:{tid}".encode("ascii")),
        ],
        [
            Button.inline(mode_label, f"ws:toggle_mode:{tid}".encode("ascii")),
        ],
        [
            Button.inline("🧠 Model / Effort", f"ws:models:{tid}".encode("ascii")),
            Button.inline("📂 Workspace", f"ws:dirs:{tid}".encode("ascii")),
        ],
        [
            Button.inline("👤 Switch Account", f"ws:accounts:{tid}".encode("ascii")),
            Button.inline("🗄 Archive Topic", f"ws:archive:{tid}".encode("ascii")),
        ],
    ]


def build_live_progress_keyboard(topic_id: int) -> list[list[Button]]:
    return [
        [Button.inline("⏹ Stop Turn (/cancel)", f"ws:cancel:{topic_id}".encode("ascii"))]
    ]


def build_turn_response_keyboard(topic: ForumTopic) -> list[list[Button]]:
    tid = topic.topic_id
    toggle_label = (
        "🛡 Switch to Plan (Read-Only)"
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else "⚡ Enable Full Write"
    )
    return [
        [
            Button.inline("🔄 Fresh Session", f"ws:new:{tid}".encode("ascii")),
            Button.inline(toggle_label, f"ws:toggle_mode:{tid}".encode("ascii")),
        ]
    ]


def build_model_picker_keyboard(
    topic_id: int, models: list[str], current_model: str
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for slug in models[:10]:
        mark = "✅ " if slug == current_model else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{slug}",
                    f"ws:set_model:{topic_id}:{slug}".encode("ascii"),
                )
            ]
        )
    effort_row = [
        Button.inline(
            f"Effort: {lvl.value}",
            f"ws:set_effort:{topic_id}:{lvl.value}".encode("ascii"),
        )
        for lvl in (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.HIGH, EffortLevel.MAX)
    ]
    rows.append(effort_row)
    rows.append([Button.inline("⬅️ Close", f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_session_history_keyboard(
    topic_id: int, sessions: list[TopicSession], active_id: UUID | None
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for sess in sessions[:8]:
        mark = "🟢 " if sess.conversation_id == active_id else ""
        short = str(sess.conversation_id)[:8]
        tok_k = sess.cumulative_tokens / 1000.0
        rows.append(
            [
                Button.inline(
                    f"{mark}{short}... ({sess.turns_count} turns, {tok_k:.1f}k tok)",
                    f"ws:resume:{topic_id}:{sess.conversation_id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("⬅️ Close", f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_topic_account_picker_keyboard(
    topic_id: int, accounts: list[GoogleAccount], active_account_id: int
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        mark = "✅ " if acc.id == active_account_id else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{acc.slug} ({acc.email_label})",
                    f"ws:set_acc:{topic_id}:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("⬅️ Close", f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_workspace_dir_picker_keyboard(
    topic_id: int, choices: list[tuple[str, str]]
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for idx, (label, _path) in enumerate(choices):
        rows.append(
            [
                Button.inline(
                    label,
                    f"ws:set_dir:{topic_id}:{idx}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline("⬅️ Close", f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows

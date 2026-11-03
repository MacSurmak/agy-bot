from uuid import UUID

from telethon.tl.custom import Button

from antigravity_core.config.static_config import LimitsStaticConfig
from antigravity_core.domain.enums import EffortLevel, ExecutionMode
from antigravity_core.domain.models import (
    EventSensor,
    ForumTopic,
    GoogleAccount,
    ScheduledTask,
    TopicSession,
)
from antigravity_core.lexicon.schema import UiLexicon


def build_control_panel_keyboard(lex: UiLexicon) -> list[list[Button]]:
    return [
        [
            Button.inline(lex.btn_cp_refresh, b"cp:refresh"),
            Button.inline(lex.btn_cp_probe_quotas, b"cp:probe_quotas"),
        ],
        [
            Button.inline(lex.btn_cp_new_topic, b"cp:new_topic"),
            Button.inline(lex.btn_cp_auth_menu, b"cp:auth_menu"),
        ],
        [
            Button.inline(lex.btn_cp_switch_default_acc, b"cp:switch_default_acc"),
            Button.inline(lex.btn_cp_remount_hosts, b"cp:remount_hosts"),
        ],
        [
            Button.inline(lex.btn_cp_tasks_menu, b"cp:tasks_menu"),
            Button.inline(lex.btn_cp_settings_menu, b"cp:settings_menu"),
        ],
        [
            Button.inline(lex.btn_cp_create_invite, b"cp:create_invite"),
        ],
    ]


def build_account_auth_keyboard(
    lex: UiLexicon, accounts: list[GoogleAccount]
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_reauth_account.format(
                        slug=acc.slug, email_label=acc.email_label
                    ),
                    f"cp:oauth_start:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_cp_register_new_acc, b"cp:register_new_acc")])
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
    return rows


def build_oauth_url_keyboard(lex: UiLexicon, auth_url: str) -> list[list[Button]]:
    return [
        [Button.url(lex.btn_cp_open_oauth_url, auth_url)],
        [Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")],
    ]


def build_default_account_picker_keyboard(
    lex: UiLexicon, accounts: list[GoogleAccount]
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        mark = "⭐ " if acc.is_default else ""
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_default_acc_item.format(
                        mark=mark, slug=acc.slug, email_label=acc.email_label
                    ),
                    f"cp:set_def_acc:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
    return rows


def build_tasks_management_keyboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    tasks: list[ScheduledTask],
    sensors: list[EventSensor] | None = None,
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for task in tasks[: limits.tasks_menu_display_limit]:
        short_id = str(task.id)[: limits.uuid_short_chars]
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_cancel_task_item.format(
                        short_id=short_id,
                        topic_id=task.topic_id,
                        trigger_spec=task.trigger_spec,
                    ),
                    f"cp:cancel_task:{task.id}".encode("ascii"),
                )
            ]
        )
    for sensor in (sensors or [])[: limits.tasks_menu_display_limit]:
        short_sid = str(sensor.id)[: limits.uuid_short_chars]
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_cancel_sensor_item.format(
                        short_id=short_sid,
                        name=sensor.name,
                        topic_id=sensor.topic_id,
                    ),
                    f"cp:cancel_sensor:{sensor.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
    return rows


def build_settings_menu_keyboard(
    lex: UiLexicon, setting_keys: list[str]
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for key in setting_keys:
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_edit_setting_item.format(key=key),
                    f"cp:edit_set:{key}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
    return rows


def build_setting_options_keyboard(
    lex: UiLexicon, key: str, options: list[tuple[str, str]], current_val: str
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for label, val in options:
        mark = "✅ " if val == current_val else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{label}",
                    f"cp:set_val:{key}:{val}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:settings_menu")])
    return rows


def build_new_topic_folder_confirm_keyboard(
    lex: UiLexicon, topic_id: int
) -> list[list[Button]]:
    return [
        [
            Button.inline(
                lex.btn_ws_confirm_default_dir,
                f"ws:confirm_dir:{topic_id}".encode("ascii"),
            )
        ],
        [
            Button.inline(
                lex.btn_ws_pick_existing_dir,
                f"ws:dirs:{topic_id}".encode("ascii"),
            ),
            Button.inline(
                lex.btn_ws_enter_custom_dir,
                f"ws:custom_dir:{topic_id}".encode("ascii"),
            ),
        ],
    ]


def build_topic_header_keyboard(lex: UiLexicon, topic: ForumTopic) -> list[list[Button]]:
    tid = topic.topic_id
    mode_label = (
        lex.btn_ws_mode_full_to_plan
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.btn_ws_mode_plan_to_full
    )
    return [
        [
            Button.inline(lex.btn_ws_new_session, f"ws:new:{tid}".encode("ascii")),
            Button.inline(lex.btn_ws_past_sessions, f"ws:history:{tid}".encode("ascii")),
        ],
        [
            Button.inline(mode_label, f"ws:toggle_mode:{tid}".encode("ascii")),
        ],
        [
            Button.inline(lex.btn_ws_models, f"ws:models:{tid}".encode("ascii")),
            Button.inline(lex.btn_ws_workspace, f"ws:dirs:{tid}".encode("ascii")),
        ],
        [
            Button.inline(lex.btn_ws_switch_account, f"ws:accounts:{tid}".encode("ascii")),
            Button.inline(lex.btn_ws_archive_topic, f"ws:archive:{tid}".encode("ascii")),
        ],
    ]


def build_live_progress_keyboard(lex: UiLexicon, topic_id: int) -> list[list[Button]]:
    return [
        [Button.inline(lex.btn_ws_stop_turn, f"ws:cancel:{topic_id}".encode("ascii"))]
    ]


def build_turn_response_keyboard(lex: UiLexicon, topic: ForumTopic) -> list[list[Button]]:
    tid = topic.topic_id
    toggle_label = (
        lex.btn_ws_resp_to_plan
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.btn_ws_resp_to_write
    )
    return [
        [
            Button.inline(lex.btn_ws_fresh_session, f"ws:new:{tid}".encode("ascii")),
            Button.inline(toggle_label, f"ws:toggle_mode:{tid}".encode("ascii")),
        ]
    ]


def build_model_picker_keyboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    topic_id: int,
    models: list[str],
    current_model: str,
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for slug in models[: limits.models_menu_display_limit]:
        mark = "✅ " if slug == current_model else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{slug}",
                    f"ws:set_model:{topic_id}:{slug}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_ws_close_submenu, f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_session_history_keyboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    topic_id: int,
    sessions: list[TopicSession],
    active_id: UUID | None,
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for sess in sessions[: limits.sessions_history_display_limit]:
        mark = "🟢 " if sess.conversation_id == active_id else ""
        short = str(sess.conversation_id)[: limits.uuid_short_chars]
        tok_k = sess.cumulative_tokens / 1000.0
        rows.append(
            [
                Button.inline(
                    lex.btn_ws_session_history_item.format(
                        mark=mark,
                        short_id=short,
                        turns_count=sess.turns_count,
                        tok_k=tok_k,
                    ),
                    f"ws:resume:{topic_id}:{sess.conversation_id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_ws_close_submenu, f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_topic_account_picker_keyboard(
    lex: UiLexicon, topic_id: int, accounts: list[GoogleAccount], active_account_id: int
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for acc in accounts:
        mark = "✅ " if acc.id == active_account_id else ""
        rows.append(
            [
                Button.inline(
                    lex.btn_ws_account_picker_item.format(
                        mark=mark, slug=acc.slug, email_label=acc.email_label
                    ),
                    f"ws:set_acc:{topic_id}:{acc.id}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_ws_close_submenu, f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows


def build_workspace_dir_picker_keyboard(
    lex: UiLexicon, topic_id: int, choices: list[tuple[str, str]]
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
    rows.append([Button.inline(lex.btn_ws_close_submenu, f"ws:close_sub:{topic_id}".encode("ascii"))])
    return rows

from typing import Final
from uuid import UUID

from telethon.tl.custom import Button

from antigravity_core.config.static_config import LimitsStaticConfig
from antigravity_core.domain.enums import ExecutionMode
from antigravity_core.domain.models import (
    EventSensor,
    ForumTopic,
    GoogleAccount,
    ScheduledTask,
    TopicSession,
)
from antigravity_core.lexicon.schema import UiLexicon

_ACCOUNTS_PAGE_SIZE: Final[int] = 5


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
            Button.inline(lex.btn_cp_remount_hosts, b"cp:remount_hosts"),
            Button.inline(lex.btn_cp_tasks_menu, b"cp:tasks_menu"),
        ],
        [
            Button.inline(lex.btn_cp_settings_menu, b"cp:settings_menu"),
            Button.inline(lex.btn_cp_show_ssh_key, b"cp:show_ssh_key"),
        ],
        [
            Button.inline(lex.btn_cp_create_invite, b"cp:create_invite"),
        ],
    ]


def build_account_auth_keyboard(
    lex: UiLexicon, accounts: list[GoogleAccount]
) -> list[list[Button]]:
    return build_paginated_accounts_keyboard(lex, accounts, 0)


def build_paginated_accounts_keyboard(
    lex: UiLexicon, accounts: list[GoogleAccount], page: int
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    total = len(accounts)
    max_page = max(0, (total - 1) // _ACCOUNTS_PAGE_SIZE) if total > 0 else 0
    curr_page = max(0, min(page, max_page))
    start_idx = curr_page * _ACCOUNTS_PAGE_SIZE
    page_items = accounts[start_idx : start_idx + _ACCOUNTS_PAGE_SIZE]

    for acc in page_items:
        star = "⭐ " if acc.is_default else "👤 "
        label = (
            f"{star}{acc.email_label}"
            if "@" in acc.email_label
            else f"{star}{acc.email_label} ({acc.slug})"
        )
        rows.append(
            [
                Button.inline(
                    label,
                    f"cp:acc_view:{acc.id}".encode("ascii"),
                )
            ]
        )

    if total > _ACCOUNTS_PAGE_SIZE:
        nav_row: list[Button] = []
        if curr_page > 0:
            nav_row.append(
                Button.inline("⬅️", f"cp:auth_page:{curr_page - 1}".encode("ascii"))
            )
        nav_row.append(
            Button.inline(f"{curr_page + 1}/{max_page + 1}", b"cp:auth_noop")
        )
        if curr_page < max_page:
            nav_row.append(
                Button.inline("➡️", f"cp:auth_page:{curr_page + 1}".encode("ascii"))
            )
        rows.append(nav_row)

    rows.append([Button.inline(lex.btn_cp_add_account, b"cp:add_acc_oauth")])
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:refresh")])
    return rows


def build_single_account_manage_keyboard(
    lex: UiLexicon, account: GoogleAccount
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    if not account.is_default:
        rows.append(
            [
                Button.inline(
                    lex.btn_cp_make_default_acc,
                    f"cp:set_def_acc:{account.id}".encode("ascii"),
                )
            ]
        )
    rows.append(
        [
            Button.inline(
                lex.btn_cp_reauth_oauth,
                f"cp:oauth_start:{account.id}".encode("ascii"),
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                lex.btn_cp_delete_acc,
                f"cp:del_acc:{account.id}".encode("ascii"),
            )
        ]
    )
    rows.append([Button.inline(lex.btn_cp_back_to_accounts, b"cp:auth_menu")])
    return rows


def build_oauth_url_keyboard(lex: UiLexicon, auth_url: str) -> list[list[Button]]:
    return [
        [Button.url(lex.btn_cp_open_oauth_url, auth_url)],
        [Button.inline(lex.btn_cp_cancel_oauth, b"cp:cancel_oauth")],
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
    active_sensors = sensors if sensors is not None else []
    for sensor in active_sensors[: limits.tasks_menu_display_limit]:
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


def build_default_model_groups_keyboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    models: list[str],
    current_model: str,
) -> list[list[Button]]:
    groups = group_models_by_family(models[: limits.models_menu_display_limit])
    rows: list[list[Button]] = []
    for idx, (family_label, variants) in enumerate(groups):
        if len(variants) == 1:
            slug = variants[0]
            mark = "✅ " if slug == current_model else ""
            rows.append(
                [
                    Button.inline(
                        f"{mark}{slug}",
                        f"cp:set_val:default_model:{slug}".encode("ascii"),
                    )
                ]
            )
        else:
            has_curr = current_model in variants
            mark = "✅ " if has_curr else "📂 "
            rows.append(
                [
                    Button.inline(
                        f"{mark}{family_label} ({len(variants)})",
                        f"cp:def_mgrp:{idx}".encode("ascii"),
                    )
                ]
            )
    rows.append([Button.inline(lex.btn_cp_back_to_dashboard, b"cp:settings_menu")])
    return rows


def build_default_model_variants_keyboard(
    lex: UiLexicon,
    variants: list[str],
    current_model: str,
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for slug in variants:
        mark = "✅ " if slug == current_model else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{slug}",
                    f"cp:set_val:default_model:{slug}".encode("ascii"),
                )
            ]
        )
    rows.append([Button.inline(lex.btn_ws_back_to_model_groups, b"cp:edit_set:default_model")])
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


def build_topic_header_keyboard(
    lex: UiLexicon, topic: ForumTopic
) -> list[list[Button]]:
    tid = topic.topic_id
    mode_label = (
        lex.btn_ws_mode_full_to_plan
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.btn_ws_mode_plan_to_full
    )
    sudo_label = (
        lex.btn_ws_sudo_cb_on_to_off
        if topic.sudo_carte_blanche
        else lex.btn_ws_sudo_cb_off_to_on
    )
    return [
        [
            Button.inline(lex.btn_ws_new_session, f"ws:new:{tid}".encode("ascii")),
            Button.inline(
                lex.btn_ws_past_sessions, f"ws:history:{tid}".encode("ascii")
            ),
        ],
        [
            Button.inline(mode_label, f"ws:toggle_mode:{tid}".encode("ascii")),
        ],
        [
            Button.inline(sudo_label, f"ws:toggle_sudo:{tid}".encode("ascii")),
        ],
        [
            Button.inline(lex.btn_ws_models, f"ws:models:{tid}".encode("ascii")),
            Button.inline(
                lex.btn_ws_switch_account, f"ws:accounts:{tid}".encode("ascii")
            ),
        ],
        [
            Button.inline(
                lex.btn_ws_archive_topic, f"ws:archive:{tid}".encode("ascii")
            ),
        ],
    ]


def build_sudo_approval_keyboard(
    lex: UiLexicon, topic_id: int, request_id: str
) -> list[list[Button]]:
    return [
        [
            Button.inline(
                lex.btn_sudo_approve_once,
                f"ws:sudo_ok:{topic_id}:{request_id}".encode("ascii"),
            ),
            Button.inline(
                lex.btn_sudo_reject,
                f"ws:sudo_no:{topic_id}:{request_id}".encode("ascii"),
            ),
        ],
        [
            Button.inline(
                lex.btn_sudo_grant_carte_blanche,
                f"ws:sudo_cb:{topic_id}:{request_id}".encode("ascii"),
            ),
        ],
    ]


def build_live_progress_keyboard(lex: UiLexicon, topic_id: int) -> list[list[Button]]:
    return [
        [Button.inline(lex.btn_ws_stop_turn, f"ws:cancel:{topic_id}".encode("ascii"))]
    ]


def build_turn_response_keyboard(
    lex: UiLexicon, topic: ForumTopic
) -> list[list[Button]]:
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


_MIN_FAMILY_GROUP_SIZE: Final[int] = 2


def group_models_by_family(models: list[str]) -> list[tuple[str, list[str]]]:
    """Deterministically groups models sharing a base prefix (via .rsplit('-', 1)) without regex."""
    prefix_buckets: dict[str, list[str]] = {}
    for slug in models:
        base = slug.rsplit("-", 1)[0] if "-" in slug else slug
        if base not in prefix_buckets:
            prefix_buckets[base] = []
        prefix_buckets[base].append(slug)

    grouped: list[tuple[str, list[str]]] = []
    emitted_bases: set[str] = set()
    for slug in models:
        base = slug.rsplit("-", 1)[0] if "-" in slug else slug
        bucket = prefix_buckets[base]
        if len(bucket) >= _MIN_FAMILY_GROUP_SIZE:
            if base not in emitted_bases:
                emitted_bases.add(base)
                grouped.append((base, bucket))
        else:
            grouped.append((slug, [slug]))
    return grouped


def build_model_picker_keyboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    topic_id: int,
    models: list[str],
    current_model: str,
) -> list[list[Button]]:
    """Builds the top-level model family keyboard (1 button per family or standalone model)."""
    _ = limits
    rows: list[list[Button]] = []
    groups = group_models_by_family(models)
    for g_idx, (family_label, variants) in enumerate(groups):
        is_current = current_model in variants
        mark = "✅ " if is_current else ""
        if len(variants) == 1:
            only_slug = variants[0]
            rows.append(
                [
                    Button.inline(
                        f"{mark}🧠 {only_slug}",
                        f"ws:set_model:{topic_id}:{only_slug}".encode("utf-8"),
                    )
                ]
            )
        else:
            rows.append(
                [
                    Button.inline(
                        lex.btn_ws_model_group_item.format(
                            mark=mark,
                            family=family_label,
                            count=len(variants),
                        ),
                        f"ws:mgrp:{topic_id}:{g_idx}".encode("ascii"),
                    )
                ]
            )
    rows.append(
        [
            Button.inline(
                lex.btn_ws_back_to_topic_card,
                f"ws:back_card:{topic_id}".encode("ascii"),
            )
        ]
    )
    return rows


def build_model_variants_keyboard(
    lex: UiLexicon,
    topic_id: int,
    variants: list[str],
    current_model: str,
) -> list[list[Button]]:
    """Builds the second-level variant selector keyboard inside a chosen model family."""
    rows: list[list[Button]] = []
    for slug in variants:
        mark = "✅ " if slug == current_model else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{slug}",
                    f"ws:set_model:{topic_id}:{slug}".encode("utf-8"),
                )
            ]
        )
    rows.append(
        [
            Button.inline(
                lex.btn_ws_back_to_model_groups,
                f"ws:models:{topic_id}".encode("ascii"),
            )
        ]
    )
    return rows


def build_staged_media_keyboard(
    lex: UiLexicon, topic_id: int
) -> list[list[Button]]:
    return [
        [
            Button.inline(
                lex.btn_ws_flush_staged_media,
                f"ws:flush_files:{topic_id}".encode("ascii"),
            ),
            Button.inline(
                lex.btn_ws_clear_staged_media,
                f"ws:clear_files:{topic_id}".encode("ascii"),
            ),
        ]
    ]


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
    rows.append(
        [
            Button.inline(
                lex.btn_ws_back_to_topic_card,
                f"ws:back_card:{topic_id}".encode("ascii"),
            )
        ]
    )
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
    rows.append(
        [
            Button.inline(
                lex.btn_ws_back_to_topic_card,
                f"ws:back_card:{topic_id}".encode("ascii"),
            )
        ]
    )
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
    rows.append(
        [
            Button.inline(
                lex.btn_ws_back_to_topic_card,
                f"ws:back_card:{topic_id}".encode("ascii"),
            )
        ]
    )
    return rows

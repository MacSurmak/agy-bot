from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UiLexicon:
    """Immutable schema for all user-facing strings, templates, and button captions."""

    # Topic Header Card (formatters.py)
    topic_card_session_with_stats: str
    topic_card_session_uuid_only: str
    topic_card_session_clean: str
    topic_card_effort_suffix: str
    topic_card_mode_full_access: str
    topic_card_mode_read_only: str
    topic_card_worker_warm: str
    topic_card_worker_cold: str
    topic_card_template: str

    # New Session Divider (formatters.py)
    new_session_mode_full_access: str
    new_session_mode_read_only: str
    new_session_divider_template: str

    # Live Turn Progress & Permanent Execution Trace (formatters.py)
    live_progress_header: str
    trace_completed_header: str
    trace_page_continuation_header: str
    trace_tool_part_suffix: str
    live_progress_subagents_header: str
    live_progress_subagent_item: str
    live_progress_tools_header: str
    live_progress_tool_done_icon: str
    live_progress_tool_active_icon: str
    live_progress_tool_duration_suffix: str
    live_progress_tool_hint_suffix: str
    live_progress_tool_item: str
    trace_tool_params_label: str
    trace_tool_output_label: str
    trace_thinking_header: str
    live_progress_thinking: str
    live_progress_draft: str

    # Turn Footer (formatters.py)
    turn_footer_stats: str
    turn_footer_failover: str
    turn_footer_error_status: str

    # Control Panel Dashboard (formatters.py)
    dashboard_header: str
    dashboard_worker_running_badge: str
    dashboard_worker_warm_badge: str
    dashboard_worker_item: str
    dashboard_accounts_header: str
    dashboard_accounts_empty: str
    dashboard_account_default_badge: str
    dashboard_account_enabled_icon: str
    dashboard_account_disabled_icon: str
    dashboard_account_quota_unprobed: str
    dashboard_account_quota_probed: str
    dashboard_account_backup_ok: str
    dashboard_account_backup_missing: str
    dashboard_account_item: str
    dashboard_hosts_header: str
    dashboard_hosts_empty: str
    dashboard_host_mounted_badge: str
    dashboard_host_unmounted_badge: str
    dashboard_host_item: str
    dashboard_tasks_header: str
    dashboard_task_item: str
    dashboard_settings_summary: str

    # Workspace directory picker labels (formatters.py)
    workspace_dir_root_label: str
    workspace_dir_local_label: str
    workspace_dir_remote_label: str

    # Inline Keyboards (keyboards.py)
    btn_cp_refresh: str
    btn_cp_probe_quotas: str
    btn_cp_new_topic: str
    btn_cp_auth_menu: str
    btn_cp_switch_default_acc: str
    btn_cp_remount_hosts: str
    btn_cp_tasks_menu: str
    btn_cp_settings_menu: str
    btn_cp_create_invite: str
    btn_cp_reauth_account: str
    btn_cp_open_oauth_url: str
    btn_cp_register_new_acc: str
    btn_cp_back_to_dashboard: str
    btn_cp_default_acc_item: str
    btn_cp_cancel_task_item: str
    btn_cp_edit_setting_item: str
    btn_ws_new_session: str
    btn_ws_past_sessions: str
    btn_ws_mode_full_to_plan: str
    btn_ws_mode_plan_to_full: str
    btn_ws_models: str
    btn_ws_workspace: str
    btn_ws_switch_account: str
    btn_ws_archive_topic: str
    btn_ws_stop_turn: str
    btn_ws_fresh_session: str
    btn_ws_resp_to_plan: str
    btn_ws_resp_to_write: str
    btn_ws_effort_item: str
    btn_ws_close_submenu: str
    btn_ws_session_history_item: str
    btn_ws_account_picker_item: str

    # Control Panel Interactive Messages & Toasts (control_panel_ui.py)
    cp_reserved_notice: str
    cp_toast_probing_quotas: str
    cp_auth_menu_prompt: str
    cp_register_new_acc_prompt: str
    cp_toast_launching_oauth: str
    cp_oauth_start_failed: str
    cp_oauth_instructions: str
    cp_switch_default_acc_prompt: str
    cp_toast_default_acc_updated: str
    cp_toast_remounting_hosts: str
    cp_remount_failed: str
    cp_new_topic_prompt: str
    cp_tasks_menu_prompt: str
    cp_tasks_menu_updated: str
    cp_toast_task_canceled: str
    cp_settings_menu_header: str
    cp_settings_menu_item: str
    cp_edit_setting_prompt: str
    cp_invite_generated_msg: str
    cp_toast_invite_generated: str
    cp_new_acc_invalid_format: str
    cp_new_acc_created: str
    cp_oauth_success: str
    cp_oauth_failed: str
    cp_new_topic_default_title: str
    cp_new_topic_created: str
    cp_new_topic_failed: str
    cp_setting_updated: str
    cp_setting_update_failed: str

    # Workspace Topic Interactive Messages & Toasts (workspace_topic_ui.py)
    ws_auto_topic_title: str
    ws_folder_confirm_prompt: str
    ws_custom_dir_prompt: str
    ws_dir_confirmed_msg: str
    btn_ws_confirm_default_dir: str
    btn_ws_pick_existing_dir: str
    btn_ws_enter_custom_dir: str
    ws_turn_stopped_msg: str
    ws_no_running_turn_msg: str
    ws_starting_turn_msg: str
    ws_turn_error_msg: str
    ws_empty_response_fallback: str
    ws_toast_new_session: str
    ws_toast_mode_switched: str
    ws_toast_turn_canceled: str
    ws_toast_topic_archived: str
    ws_select_past_session_prompt: str
    ws_session_resumed_msg: str
    ws_select_model_prompt: str
    ws_model_updated_msg: str
    ws_model_unknown_msg: str
    ws_effort_updated_msg: str
    ws_select_account_prompt: str
    ws_account_switched_msg: str
    ws_select_workspace_prompt: str
    ws_workspace_updated_msg: str

    # Router Access & Invites (router.py)
    router_invite_redeemed: str
    router_invite_failed: str
    router_toast_owner_only: str
    router_toast_access_denied: str

    # Media Service Transfer Messages (mtproto_media.py)
    media_downloading_init: str
    media_downloading_progress: str
    media_download_failed: str
    media_uploading_init: str
    media_uploading_progress: str
    media_upload_failed: str

    # Batcher, Scheduler & Topic Lifecycle (batcher.py, scheduler_service.py, topic_lifecycle.py)
    batch_attached_files_header: str
    sched_report_header: str
    sched_report_empty_status: str
    sched_report_failed: str
    topic_archive_prefix: str

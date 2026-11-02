from antigravity_core.lexicon.schema import UiLexicon

EN_LEXICON = UiLexicon(
    # Topic Header Card
    topic_card_session_with_stats="`{conv_short}...` ({turns_count} turns, {tok_k:.1f}k tok.)",
    topic_card_session_uuid_only="`{conv_short}...`",
    topic_card_session_clean="🆕 Clean Session (0 tokens)",
    topic_card_effort_suffix=" (Effort: `{effort}`)",
    topic_card_mode_full_access="⚡ Full Write (`accept-edits`)",
    topic_card_mode_read_only="🛡 Read-Only (`plan`)",
    topic_card_worker_warm="🟢 Warm in RAM (TTL: {ttl_min}m)",
    topic_card_worker_cold="⚪ Cold (spawns on message, TTL: {ttl_min}m)",
    topic_card_template=(
        "📌 **Topic Passport: {title}**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💬 **Session:** {session_str}\n"
        "📂 **Workspace:** `{workspace_path}`\n"
        "🧠 **Model:** `{model_slug}`{effort_str}\n"
        "👤 **Account:** `{account_slug}` ({email_label})\n"
        "🛡 **Permissions:** {mode_str}\n"
        "📡 **Worker:** {worker_str}"
    ),
    # New Session Divider
    new_session_mode_full_access="Full Write",
    new_session_mode_read_only="Read-Only (Plan)",
    new_session_divider_template=(
        "══════════════════════════════════════════\n"
        "🆕 **NEW CLEAN SESSION (Context: 0 tokens)**\n"
        "📂 **Workspace:** `{workspace_path}`\n"
        "🧠 **Model:** `{model_slug}` | 🛡 **Mode:** `{mode_label}`\n"
        "══════════════════════════════════════════"
    ),
    # Live Turn Progress & Permanent Execution Trace
    live_progress_header=(
        "⚙️ **Executing Turn** (`{model_slug}` | `{account_slug}` | `{elapsed_sec:.1f}s`)"
    ),
    trace_completed_header=(
        "🛠 **Execution Trace** (`{model_slug}` | `{account_slug}` | `{elapsed_sec:.1f}s` | steps: `{steps_count}`)"
    ),
    live_progress_subagents_header="🤖 **Subagents:**",
    live_progress_subagent_item="• `{role}` (`{type_name}` — `{conv_short}`)",
    live_progress_tools_header="🔧 **Tools & Actions:**",
    live_progress_tool_done_icon="✅",
    live_progress_tool_active_icon="⏳",
    live_progress_tool_duration_suffix=" ({duration:.2f}s)",
    live_progress_tool_hint_suffix=" → `{hint}`",
    live_progress_tool_item="{icon} **#{step_index}** `{tool_name}`{dur}{hint}",
    trace_tool_params_label="📥 Parameters:",
    trace_tool_output_label="📤 Output:",
    trace_thinking_header="💭 **Agent Thinking:**",
    live_progress_thinking="💭 **Thinking:**\n{snippet}",
    live_progress_draft="📝 **Draft:**\n{preview}",
    # Turn Footer
    turn_footer_stats=(
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "⏱ `{duration:.1f}s` | 📊 `+{tok_k:.1f}k` tok (session: `{cum_k:.1f}k`, cache: `{cache_k:.1f}k`) | "
        "🧠 `{model_slug}` | 👤 `{account_slug}`"
    ),
    turn_footer_failover="\n🔄 *Auto-Failover:* `{from_slug}` ➔ `{to_slug}` (quota threshold)",
    turn_footer_error_status="\n⚠️ **Status `{status}`:** `{error}`",
    # Control Panel Dashboard
    dashboard_header=(
        "🛠 **ANTIGRAVITY TELEGRAM CORE — CONTROL PANEL**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 **Worker Pool:** `{warm_count}` warm in RAM | `{running_count}` actively executing"
    ),
    dashboard_worker_running_badge="🔥 RUNNING",
    dashboard_worker_warm_badge="🟢 WARM",
    dashboard_worker_item="  • Topic `{topic_id}`: {state_badge} (`{model_slug}` on `{account_slug}`)",
    dashboard_accounts_header="\n👤 **Google AI Pro Accounts & Quotas:**",
    dashboard_accounts_empty="  • ⚠️ No Google accounts registered yet. Click **[🔑 Add / Auth Account]**.",
    dashboard_account_default_badge=" ⭐ **[DEFAULT]**",
    dashboard_account_enabled_icon="🟢",
    dashboard_account_disabled_icon="🔴",
    dashboard_account_quota_unprobed="not probed yet",
    dashboard_account_quota_probed="min remaining: `{min_pct}%`",
    dashboard_account_backup_ok="🔒 Fernet Backup OK",
    dashboard_account_backup_missing="⚠️ No Token",
    dashboard_account_item="  • {status_badge} `{slug}` ({email_label}){default_badge} — {quota_str} | {backup_badge}",
    dashboard_hosts_header="\n🖥 **Remote Hosts (SSHFS `auto_cache`):**",
    dashboard_hosts_empty="  • None registered yet (use MCP `register_remote_host` or `/host-onboarding`).",
    dashboard_host_mounted_badge="🟢 Mounted",
    dashboard_host_unmounted_badge="🔴 Unmounted",
    dashboard_host_item="  • `{alias}` (`{ssh_user}@{hostname_or_ip}:{ssh_port}`) → `{local_mount_path}` [{m_badge}]",
    dashboard_tasks_header="\n⏰ **Active Scheduled Tasks:** `{count}`",
    dashboard_task_item="  • `{short_id}` [Topic `{topic_id}`] `{schedule_kind}` `{trigger_spec}` ({notify_policy})",
    dashboard_settings_summary=(
        "\n⚙️ **Default Settings:**\n"
        "  • Language: `{ui_language}` | Model: `{default_model}` | Mode: `{default_execution_mode}`"
    ),
    # Workspace directory picker labels
    workspace_dir_root_label="📁 Root Workspaces",
    workspace_dir_local_label="📂 local/{name}",
    workspace_dir_remote_label="🌐 remote/{name}",
    # Inline Keyboards
    btn_cp_refresh="🔄 Refresh Dashboard",
    btn_cp_probe_quotas="📊 Probe Quotas",
    btn_cp_new_topic="➕ Create Topic",
    btn_cp_auth_menu="🔑 Add / Auth Account",
    btn_cp_switch_default_acc="🔀 Switch Default Account",
    btn_cp_remount_hosts="🔌 Remount SSHFS Hosts",
    btn_cp_tasks_menu="⏰ Scheduled Tasks",
    btn_cp_settings_menu="⚙️ Settings",
    btn_cp_create_invite="🎟 Generate Member Invite",
    btn_cp_reauth_account="🔑 Re-Authenticate {slug} ({email_label})",
    btn_cp_open_oauth_url="🔗 Open Google Sign-In Page",
    btn_cp_register_new_acc="➕ Register New Account Profile",
    btn_cp_back_to_dashboard="⬅️ Back to Dashboard",
    btn_cp_default_acc_item="{mark}{slug} ({email_label})",
    btn_cp_cancel_task_item="🗑 Cancel {short_id} (Topic {topic_id}: {trigger_spec})",
    btn_cp_edit_setting_item="✏️ Edit {key}",
    btn_ws_new_session="🔄 New Session (/new)",
    btn_ws_past_sessions="🕰 Past Sessions",
    btn_ws_mode_full_to_plan="🛡 Mode: Full Write ⇄ Plan",
    btn_ws_mode_plan_to_full="⚡ Mode: Plan (Read-Only) ⇄ Write",
    btn_ws_models="🧠 Switch Model",
    btn_ws_workspace="📂 Workspace",
    btn_ws_switch_account="👤 Switch Account",
    btn_ws_archive_topic="🗄 Archive Topic",
    btn_ws_stop_turn="⏹ Stop Turn (/cancel)",
    btn_ws_fresh_session="🔄 Fresh Session",
    btn_ws_resp_to_plan="🛡 Switch to Plan (Read-Only)",
    btn_ws_resp_to_write="⚡ Enable Full Write",
    btn_ws_effort_item="Effort: {effort}",
    btn_ws_close_submenu="⬅️ Close",
    btn_ws_session_history_item="{mark}{short_id}... ({turns_count} turns, {tok_k:.1f}k tok)",
    btn_ws_account_picker_item="{mark}{slug} ({email_label})",
    # Control Panel Interactive Messages & Toasts
    cp_reserved_notice=(
        "ℹ️ **#Control-Panel** is reserved for system administration.\n"
        "Use the dashboard buttons above or send `/panel` to refresh."
    ),
    cp_toast_probing_quotas="Probing quotas across all accounts...",
    cp_auth_menu_prompt=(
        "🔑 **Google AI Pro Account Management**\n"
        "Select an existing account to re-authenticate via Remote SSH OAuth flow, "
        "or register a new isolated D-Bus/Keyring profile:"
    ),
    cp_register_new_acc_prompt=(
        "➕ **Register New Google Account Profile**\n"
        "Reply in this topic with `<slug> <email_label>` (for example: `acc_2 backup@gmail.com`)."
    ),
    cp_toast_launching_oauth="Launching agy login (Remote SSH OAuth Flow)...",
    cp_oauth_start_failed="❌ Failed to start OAuth flow for `{slug}`:\n`{error}`",
    cp_oauth_instructions=(
        "🔐 **OAuth Login for `{slug}` ({email_label})**\n\n"
        "1. Click the button below or copy this URL into your browser:\n`{auth_url}`\n\n"
        "2. Sign into `{email_label}`, copy the single-use verification code, "
        "and **send it as your next message in this topic**."
    ),
    cp_switch_default_acc_prompt="🔀 **Select Default Google Account for New Topics:**",
    cp_toast_default_acc_updated="Default account updated.",
    cp_toast_remounting_hosts="Verifying and mounting SSHFS hosts...",
    cp_remount_failed="❌ SSHFS mount failed for `{alias}`: `{error}`",
    cp_new_topic_prompt=(
        "➕ **Create New Workspace Topic**\n"
        "Reply with `<Topic Title> | <Workspace Path> | <PERMANENT or TASK>`\n"
        "Example:\n`🚀 Project Alpha | {workspaces_root_dir}/alpha | TASK`"
    ),
    cp_tasks_menu_prompt="⏰ **Active Scheduled Tasks ({count}):**\nClick a task below to cancel it:",
    cp_tasks_menu_updated="⏰ **Active Scheduled Tasks ({count}):**",
    cp_toast_task_canceled="Task canceled.",
    cp_settings_menu_header="⚙️ **Default Settings for New Topics:**\n",
    cp_settings_menu_item="• **{description}:** `{value_json}`",
    cp_edit_setting_prompt=(
        "✏️ **Update Setting `{key}`**\n"
        "Reply with the new JSON value (e.g. `\"ru\"`, `\"en\"`, `\"gemini-4-argon-high\"`, `900`, `1.8`)."
    ),
    cp_invite_generated_msg=(
        "🎟 **Single-Use Member Invite Generated (valid 24h):**\n"
        "Ask the user to send the following command in the forum:\n"
        "`/redeem {token}`"
    ),
    cp_toast_invite_generated="Invite generated.",
    cp_new_acc_invalid_format="❌ Format must be `<slug> <email_label>` (e.g. `acc_1 user@gmail.com`).",
    cp_new_acc_created=(
        "✅ Registered and unlocked isolated D-Bus/Keyring profile `{slug}`.\n"
        "Now open **[🔑 Add / Auth Account]** to complete Google OAuth sign-in."
    ),
    cp_oauth_success=(
        "✅ **OAuth Sign-In Complete for `{slug}`!**\n"
        "Token stored in isolated `gnome-keyring` and backed up to PostgreSQL 18 via `Fernet`."
    ),
    cp_oauth_failed="❌ **OAuth Verification Failed:**\n`{error}`",
    cp_new_topic_default_title="New Workspace",
    cp_new_topic_created="✅ Created Forum Topic **{title}** (`topic_id={topic_id}`).",
    cp_new_topic_failed="❌ Failed to create topic: `{error}`",
    cp_setting_updated="✅ Updated setting `{key}` = `{value_json}`.",
    cp_setting_update_failed="❌ Validation failed for `{key}`:\n`{error}`",
    # Workspace Topic Interactive Messages & Toasts
    ws_auto_topic_title="💬 New Chat #{topic_id}",
    ws_folder_confirm_prompt=(
        "📂 **Select Workspace Folder for Topic**\n"
        "Default assigned path:\n`{workspace_path}`\n"
        "*(folder is only created on disk when confirmed or on first prompt)*"
    ),
    ws_custom_dir_prompt=(
        "✏️ **Send the full absolute path to your project folder as your next message in this topic** "
        "(for example: `/home/macsurmak/my-project`):"
    ),
    ws_dir_confirmed_msg="✅ Workspace folder confirmed and ready: `{workspace_path}`",
    btn_ws_confirm_default_dir="✅ Keep Default Folder",
    btn_ws_pick_existing_dir="📂 Choose from Existing",
    btn_ws_enter_custom_dir="✏️ Enter Custom Path",
    ws_turn_stopped_msg="⏹ Active turn stopped.",
    ws_no_running_turn_msg="ℹ️ No running turn in this topic.",
    ws_starting_turn_msg="⏳ **Starting turn** (`{model_slug}` on `{account_slug}`)...",
    ws_turn_error_msg="❌ **Turn Error:**\n`{error}`",
    ws_empty_response_fallback="*(Completed with status `{status}`)*",
    ws_toast_new_session="Started a clean session (0 tokens).",
    ws_toast_mode_switched="Switched mode to {mode}.",
    ws_toast_turn_canceled="Turn canceled.",
    ws_toast_topic_archived="Topic archived and worker stopped.",
    ws_select_past_session_prompt="🕰 **Select a Past Session to Resume:**",
    ws_session_resumed_msg="✅ Resumed session `{conversation_id}`.",
    ws_select_model_prompt="🧠 **Select Model for Topic:**",
    ws_model_updated_msg="✅ Model updated to `{model_slug}`.",
    ws_model_unknown_msg=(
        "❌ Model `{model_slug}` is not found in `agy models`.\n"
        "Available models: {available_models}"
    ),
    ws_effort_updated_msg="✅ Thinking effort updated to `{effort}`.",
    ws_select_account_prompt="👤 **Select Google Account for this Topic:**",
    ws_account_switched_msg="✅ Switched Google Account for this topic.",
    ws_select_workspace_prompt="📂 **Select Workspace Directory:**",
    ws_workspace_updated_msg="✅ Workspace updated to `{workspace_path}`.",
    # Router Access & Invites
    router_invite_redeemed="✅ Invite token redeemed! You now have access.",
    router_invite_failed="❌ Invite redemption failed: `{error}`",
    router_toast_owner_only="Access denied: Owner only.",
    router_toast_access_denied="Access denied.",
    # Media Service Transfer Messages
    media_downloading_init="📥 Downloading `{file_name}` into `{dest_path}`...",
    media_downloading_progress="📥 Downloading `{file_name}`: **{pct:.1f}%** ({mb_cur:.1f} / {mb_tot:.1f} MB)",
    media_download_failed="❌ Failed to download `{file_name}`: `{error}`",
    media_uploading_init="📤 Uploading `{file_name}` ({size_mb:.2f} MB)...",
    media_uploading_progress="📤 Uploading `{file_name}`: **{pct:.1f}%** ({mb_cur:.1f} / {mb_tot:.1f} MB)",
    media_upload_failed="❌ Upload failed for `{file_name}`: `{error}`",
    # Batcher, Scheduler & Topic Lifecycle
    batch_attached_files_header="[Attached files saved in local inbox for inspection]:",
    sched_report_header="⏰ **Scheduled Task Report** (`{trigger_spec}`)\n━━━━━━━━━━━━━━━━━━\n",
    sched_report_empty_status="Status: `{status}`",
    sched_report_failed="❌ **Scheduled Task Failed** (`{task_id}`):\n`{error}`",
    topic_archive_prefix="🗄 [Archive] ",
)

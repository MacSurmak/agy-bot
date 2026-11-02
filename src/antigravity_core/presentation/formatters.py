from pathlib import Path

from antigravity_core.config.dynamic_settings import DynamicSettings
from antigravity_core.domain.enums import ExecutionMode, StepState, TurnStatus
from antigravity_core.domain.models import (
    ForumTopic,
    GoogleAccount,
    RemoteHost,
    ScheduledTask,
    TopicSession,
    TurnProgressEvent,
    TurnResult,
)
from antigravity_core.infrastructure.agy_runtime.worker_pool import WorkerTopicSnapshot


def render_topic_header_card(
    topic: ForumTopic,
    account: GoogleAccount,
    active_session: TopicSession | None,
    is_worker_warm: bool,
    worker_ttl_sec: int,
) -> str:
    if active_session is not None:
        conv_short = str(active_session.conversation_id)[:8]
        tok_k = active_session.cumulative_tokens / 1000.0
        session_str = f"`{conv_short}...` ({active_session.turns_count} turns, {tok_k:.1f}k tok.)"
    elif topic.active_conversation_id is not None:
        session_str = f"`{str(topic.active_conversation_id)[:8]}...`"
    else:
        session_str = "🆕 Clean Session (0 tokens)"

    effort_str = f" (Effort: `{topic.effort_level.value}`)" if topic.effort_level else ""
    if topic.execution_mode == ExecutionMode.FULL_ACCESS:
        mode_str = "⚡ Full Write (`accept-edits`)"
    else:
        mode_str = "🛡 Read-Only (`plan`)"

    ttl_min = max(1, worker_ttl_sec // 60)
    worker_str = (
        f"🟢 Warm in RAM (TTL: {ttl_min}m)"
        if is_worker_warm
        else f"⚪ Cold (spawns on message, TTL: {ttl_min}m)"
    )

    return (
        f"📌 **Topic Passport: {topic.title}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 **Session:** {session_str}\n"
        f"📂 **Workspace:** `{topic.workspace_path}`\n"
        f"🧠 **Model:** `{topic.model_slug}`{effort_str}\n"
        f"👤 **Account:** `{account.slug}` ({account.email_label})\n"
        f"🛡 **Permissions:** {mode_str}\n"
        f"📡 **Worker:** {worker_str}"
    )


def render_new_session_divider(topic: ForumTopic) -> str:
    mode_label = (
        "Full Write"
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else "Read-Only (Plan)"
    )
    return (
        "══════════════════════════════════════════\n"
        "🆕 **NEW CLEAN SESSION (Context: 0 tokens)**\n"
        f"📂 **Workspace:** `{topic.workspace_path}`\n"
        f"🧠 **Model:** `{topic.model_slug}` | 🛡 **Mode:** `{mode_label}`\n"
        "══════════════════════════════════════════"
    )


def render_live_progress(
    event: TurnProgressEvent,
    elapsed_sec: float,
    model_slug: str,
    account_slug: str,
) -> str:
    lines: list[str] = [
        f"⚙️ **Antigravity Running** (`{model_slug}` | `{account_slug}` | `{elapsed_sec:.1f}s`)",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if event.active_subagents:
        lines.append("🤖 **Active Subagents:**")
        for sub in event.active_subagents:
            lines.append(f"  • `{sub.role}` (`{sub.type_name}` — `{sub.conversation_id[:8]}`)")

    if event.active_tools:
        lines.append("🔧 **Tool Execution:**")
        for tool in event.active_tools[-6:]:
            icon = "✅" if tool.state == StepState.DONE else "⏳"
            dur = f" ({tool.duration_seconds:.2f}s)" if tool.duration_seconds is not None else ""
            hint = f" → `{tool.target_hint}`" if tool.target_hint else ""
            lines.append(f"  {icon} `{tool.tool_name}`{hint}{dur}")

    if event.thinking_snippet:
        snippet = event.thinking_snippet.strip()[-280:]
        lines.append(f"\n💭 *Thinking:* _{snippet}_")

    if event.text_accumulated.strip():
        preview = event.text_accumulated.strip()[-600:]
        lines.append(f"\n📝 **Draft:**\n{preview}")

    return "\n".join(lines)


def render_turn_footer(result: TurnResult, model_slug: str, account_slug: str) -> str:
    tok_k = result.turn_delta_tokens / 1000.0
    cum_k = result.cumulative_usage.total_tokens / 1000.0
    cache_k = result.cumulative_usage.cache_read_tokens / 1000.0

    footer = (
        f"\n\n━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{result.turn_delta_duration_sec:.1f}s` | "
        f"📊 `+{tok_k:.1f}k` tok (session: `{cum_k:.1f}k`, cache: `{cache_k:.1f}k`) | "
        f"🧠 `{model_slug}` | 👤 `{account_slug}`"
    )
    if result.account_switched_from and result.account_switched_to:
        footer += (
            f"\n🔄 *Auto-Failover:* `{result.account_switched_from}` ➔ "
            f"`{result.account_switched_to}` (quota threshold)"
        )
    if result.status != TurnStatus.SUCCESS and result.error_message:
        footer += f"\n⚠️ **Status `{result.status.value}`:** `{result.error_message[:300]}`"
    return footer


def render_control_panel_dashboard(
    accounts: list[GoogleAccount],
    hosts: list[RemoteHost],
    mount_statuses: dict[str, bool],
    pool_snapshots: list[WorkerTopicSnapshot],
    tasks: list[ScheduledTask],
    settings: DynamicSettings,
) -> str:
    warm_count = sum(1 for s in pool_snapshots if s.is_warm)
    running_count = sum(1 for s in pool_snapshots if s.is_running_turn)

    lines: list[str] = [
        "🛠 **ANTIGRAVITY TELEGRAM CORE — CONTROL PANEL**",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📡 **Worker Pool:** `{warm_count}` warm in RAM | `{running_count}` actively executing",
    ]
    for snap in pool_snapshots:
        if snap.is_warm:
            state_badge = "🔥 RUNNING" if snap.is_running_turn else "🟢 WARM"
            lines.append(
                f"  • Topic `{snap.topic_id}`: {state_badge} (`{snap.model_slug}` on `{snap.account_slug}`)"
            )

    lines.append("\n👤 **Google AI Pro Accounts & Quotas:**")
    if not accounts:
        lines.append("  • ⚠️ No Google accounts registered yet. Click **[🔑 Add / Auth Account]**.")
    for acc in accounts:
        default_badge = " ⭐ **[DEFAULT]**" if acc.is_default else ""
        status_badge = "🟢" if acc.is_enabled else "🔴"
        quota_str = "not probed yet"
        if acc.last_quota_json is not None:
            min_pct = acc.last_quota_json.get("min_remaining_pct", "?")
            quota_str = f"min remaining: `{min_pct}%`"
        backup_badge = "🔒 Fernet Backup OK" if acc.encrypted_token_backup else "⚠️ No Token"
        lines.append(
            f"  • {status_badge} `{acc.slug}` ({acc.email_label}){default_badge} — {quota_str} | {backup_badge}"
        )

    lines.append("\n🖥 **Remote Hosts (SSHFS `auto_cache`):**")
    if not hosts:
        lines.append("  • None registered yet (use MCP `register_remote_host` or `/host-onboarding`).")
    for h in hosts:
        mounted = mount_statuses.get(h.alias, False)
        m_badge = "🟢 Mounted" if mounted else "🔴 Unmounted"
        lines.append(
            f"  • `{h.alias}` (`{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}`) → `{h.local_mount_path}` [{m_badge}]"
        )

    lines.append(f"\n⏰ **Active Scheduled Tasks:** `{len(tasks)}`")
    for t in tasks[:5]:
        lines.append(
            f"  • `{str(t.id)[:8]}` [Topic `{t.topic_id}`] `{t.schedule_kind.value}` `{t.trigger_spec}` ({t.notify_policy.value})"
        )

    lines.append(
        "\n⚙️ **Dynamic Defaults (PostgreSQL 18):**\n"
        f"  • Model: `{settings.default_model}` (Effort: `{settings.default_effort}`)\n"
        f"  • Mode: `{settings.default_execution_mode.value}` | TTL: `{settings.worker_idle_ttl_sec}s`\n"
        f"  • Debounce: `{settings.debounce_window_sec}s` | Auto-Switch Quota: `< {settings.quota_auto_switch_threshold_pct}%`"
    )
    return "\n".join(lines)


def split_message_chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_idx = remaining.rfind("\n", 0, max_chars)
        if split_idx <= max_chars // 2:
            split_idx = max_chars
        chunk = remaining[:split_idx].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip("\n")

    if remaining:
        chunks.append(remaining)
    return chunks


def list_workspace_directories(workspaces_root: Path, remotes_root: Path) -> list[tuple[str, str]]:
    """Returns pairs of (label, absolute_path) for quick workspace selection."""
    choices: list[tuple[str, str]] = [
        ("📁 Root Workspaces", str(workspaces_root.resolve())),
    ]
    if workspaces_root.is_dir():
        for child in sorted(workspaces_root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                choices.append((f"📂 local/{child.name}", str(child.resolve())))

    if remotes_root.is_dir():
        for host_dir in sorted(remotes_root.iterdir()):
            if host_dir.is_dir():
                choices.append((f"🌐 remote/{host_dir.name}", str(host_dir.resolve())))
    return choices[:12]

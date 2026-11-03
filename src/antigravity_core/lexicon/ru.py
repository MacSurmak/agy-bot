from antigravity_core.lexicon.schema import UiLexicon

RU_LEXICON = UiLexicon(
    language_self_name="🇷🇺 Русский",
    # Topic Header Card
    topic_card_session_with_stats="`{conv_short}...` ({turns_count} ход., {tok_k:.1f}k ток.)",
    topic_card_session_uuid_only="`{conv_short}...`",
    topic_card_session_clean="🆕 Чистая сессия (0 токенов)",
    topic_card_effort_suffix=" (Effort: `{effort}`)",
    topic_card_mode_full_access="⚡ Полный доступ (`accept-edits`)",
    topic_card_mode_read_only="🛡 Только чтение (`plan`)",
    topic_card_worker_warm="🟢 Прогрет в RAM (TTL: {ttl_min} мин)",
    topic_card_worker_cold="⚪ Холодный (старт по сообщению, TTL: {ttl_min} мин)",
    topic_card_template=(
        "📌 **Паспорт топика: {title}**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💬 **Сессия:** {session_str}\n"
        "📂 **Папка:** `{workspace_path}`\n"
        "🧠 **Модель:** `{model_slug}`{effort_str}\n"
        "👤 **Аккаунт:** `{account_slug}` ({email_label})\n"
        "🛡 **Права агента:** {mode_str}\n"
        "📡 **Воркер:** {worker_str}"
    ),
    # New Session Divider
    new_session_mode_full_access="Полный доступ (Запись)",
    new_session_mode_read_only="Только чтение (Plan)",
    new_session_divider_template=(
        "══════════════════════════════════════════\n"
        "🆕 **НОВАЯ ЧИСТАЯ СЕССИЯ (Контекст: 0 токенов)**\n"
        "📂 **Папка:** `{workspace_path}`\n"
        "🧠 **Модель:** `{model_slug}` | 🛡 **Режим:** `{mode_label}`\n"
        "══════════════════════════════════════════"
    ),
    # Live Turn Progress & Permanent Execution Trace
    live_progress_header=(
        "⚙️ **Выполнение хода** (`{model_slug}` | `{account_slug}` | `{elapsed_sec:.1f}с`)"
    ),
    trace_completed_header=(
        "🛠 **Журнал выполнения хода** (`{model_slug}` | `{account_slug}` | `{elapsed_sec:.1f}с` | шагов: `{steps_count}`)"
    ),
    trace_page_continuation_header="🛠 Журнал выполнения хода (стр. {page_num})",
    trace_active_page_suffix="{header} [стр. {page_num}]",
    trace_tool_part_suffix=" [часть {part}/{total}]",
    trace_hidden_lines_marker="... [+{count} стр.] ...",
    trace_hidden_chars_marker="... [+{count} симв.] ...",
    live_progress_subagents_header="🤖 **Субагенты:**",
    live_progress_subagent_item="• `{role}` (`{type_name}` — `{conv_short}`)",
    live_progress_tools_header="🔧 **Инструменты и действия:**",
    live_progress_tool_done_icon="✅",
    live_progress_tool_active_icon="⏳",
    live_progress_tool_duration_suffix=" ({duration:.2f}с)",
    live_progress_tool_hint_suffix=" → `{hint}`",
    live_progress_tool_item="{icon} **#{step_index}** `{tool_name}`{dur}{hint}",
    trace_tool_params_label="📥 Параметры:",
    trace_tool_output_label="📤 Вывод:",
    trace_thinking_header="💭 **Мысли агента (Thinking):**",
    live_progress_thinking="💭 **Мысли:**\n{snippet}",
    live_progress_draft="📝 **Черновик ответа:**\n{preview}",
    # Turn & Pagination Footers
    turn_footer_stats=(
        "⏱ {duration:.1f}с  │  📊 +{tok_k:.1f}k ток (сессия: {cum_k:.1f}k, кэш: {cache_k:.1f}k)  │  "
        "🧠 {model_slug}  │  👤 {account_slug}"
    ),
    turn_footer_failover=(
        "\n🔄 *Авто-переключение аккаунта:* `{from_slug}` ➔ `{to_slug}` (исчерпан порог квоты)"
    ),
    turn_footer_error_status="\n⚠️ **Статус `{status}`:** `{error}`",
    response_chunk_bar="📄 Часть {idx} из {total}",
    sched_footer_bar="⏰ Запланированная задача  │  🧠 {model_slug}  │  👤 {account_slug}",
    sensor_footer_bar="📡 Датчик: {sensor_name}  │  🧠 {model_slug}  │  👤 {account_slug}",
    wake_badge_timer="⏰ Таймер",
    wake_badge_sensor="📡 Датчик: {sensor_name}",
    # Control Panel Dashboard
    dashboard_header=(
        "🛠 **ANTIGRAVITY TELEGRAM CORE — ПАНЕЛЬ УПРАВЛЕНИЯ**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 **Пул воркеров:** `{warm_count}` прогрето в RAM | `{running_count}` выполняют ход"
    ),
    dashboard_worker_running_badge="🔥 В РАБОТЕ",
    dashboard_worker_warm_badge="🟢 ПРОГРЕТ",
    dashboard_worker_item="  • Топик `{topic_id}`: {state_badge} (`{model_slug}` на `{account_slug}`)",
    dashboard_accounts_header="\n👤 **Аккаунты Google AI Pro и Квоты:**",
    dashboard_accounts_empty=(
        "  • ⚠️ Нет зарегистрированных аккаунтов. Нажмите **[🔑 Авторизовать / Добавить аккаунт]**."
    ),
    dashboard_account_default_badge=" ⭐ **[ОСНОВНОЙ]**",
    dashboard_account_enabled_icon="🟢",
    dashboard_account_disabled_icon="🔴",
    dashboard_account_quota_unprobed="не проверено",
    dashboard_account_quota_probed="остаток: `{min_pct}%`",
    dashboard_account_backup_ok="🔒 Бэкап Fernet OK",
    dashboard_account_backup_missing="⚠️ Нет токена",
    dashboard_account_item="  • {status_badge} `{slug}` ({email_label}){default_badge} — {quota_str} | {backup_badge}",
    dashboard_hosts_header="\n🖥 **Удаленные хосты (SSHFS `auto_cache`):**",
    dashboard_hosts_empty=(
        "  • Пока не добавлены (используйте MCP `register_remote_host` или `/host-onboarding`)."
    ),
    dashboard_host_mounted_badge="🟢 Смонтирован",
    dashboard_host_unmounted_badge="🔴 Отключен",
    dashboard_host_item="  • `{alias}` (`{ssh_user}@{hostname_or_ip}:{ssh_port}`) → `{local_mount_path}` [{m_badge}]",
    dashboard_tasks_header="\n⏰ **Активные задачи планировщика:** `{count}`",
    dashboard_task_item="  • `{short_id}` [Топик `{topic_id}`] `{schedule_kind}` `{trigger_spec}` ({notify_policy})",
    dashboard_sensors_header="\n📡 **Активные датчики (Event Sensors):** `{count}`",
    dashboard_sensor_item="  • `{short_id}` [Топик `{topic_id}`] **{name}** (`{sensor_kind}` | `{condition}` | {mode_tag})",
    dashboard_settings_summary=(
        "\n⚙️ **Настройки по умолчанию:**\n"
        "  • Язык: `{ui_language}` | Модель: `{default_model}` | Режим: `{default_execution_mode}`"
    ),
    quota_5h_label="**5ч:** {bar}",
    quota_weekly_label="**Нед:** {bar}",
    quota_gemini_prefix="    ├─ 🧠 **Gemini:** ",
    quota_claude_prefix="    └─ 🤖 **Claude/3P:** ",
    quota_reset_days_hours=" (↻ {days}д {hours}ч)",
    quota_reset_hours_mins=" (↻ {hours}ч {mins}м)",
    quota_reset_mins=" (↻ {mins}м)",
    # Workspace directory picker labels
    workspace_dir_root_label="📁 Корень рабочих папок",
    workspace_dir_local_label="📂 local/{name}",
    workspace_dir_remote_label="🌐 remote/{name}",
    # Inline Keyboards
    btn_cp_refresh="🔄 Обновить дашборд",
    btn_cp_probe_quotas="📊 Обновить квоты",
    btn_cp_new_topic="➕ Создать топик",
    btn_cp_auth_menu="🔑 Авторизовать / Добавить аккаунт",
    btn_cp_switch_default_acc="🔀 Сменить основной аккаунт",
    btn_cp_remount_hosts="🔌 Перемонтировать SSHFS",
    btn_cp_tasks_menu="⏰ Расписания и Таймеры",
    btn_cp_settings_menu="⚙️ Настройки",
    btn_cp_create_invite="🎟 Создать инвайт-токен",
    btn_cp_reauth_account="🔑 Авторизовать {slug} ({email_label})",
    btn_cp_open_oauth_url="🔗 Открыть страницу входа Google",
    btn_cp_register_new_acc="➕ Зарегистрировать новый профиль",
    btn_cp_back_to_dashboard="⬅️ Назад к дашборду",
    btn_cp_default_acc_item="{mark}{slug} ({email_label})",
    btn_cp_cancel_task_item="🗑 Таймер {short_id} (Топик {topic_id}: {trigger_spec})",
    btn_cp_cancel_sensor_item="🗑 Датчик {short_id}: {name} (Топик {topic_id})",
    btn_cp_edit_setting_item="✏️ Изменить {key}",
    btn_cp_set_language="🌐 Язык интерфейса",
    btn_cp_set_model="🧠 Модель по умолчанию",
    btn_cp_set_mode="🛡 Режим прав по умолчанию",
    setting_label_ui_language="🌐 Язык интерфейса",
    setting_label_default_model="🧠 Модель по умолчанию",
    setting_label_default_mode="🛡 Режим прав по умолчанию",
    cp_select_language_prompt="🌐 Выберите язык интерфейса",
    cp_select_mode_prompt="🛡 Выберите режим прав по умолчанию",
    mode_option_full_access="⚡ Полный доступ (Чтение и Запись)",
    mode_option_read_only="🛡 Только чтение (Режим Plan)",
    cp_toast_sensor_canceled="Датчик отключен.",
    btn_ws_new_session="🔄 Новая сессия (/new)",
    btn_ws_past_sessions="🕰 Прошлые сессии",
    btn_ws_mode_full_to_plan="🛡 Режим: Запись разрешена ⇄ Plan",
    btn_ws_mode_plan_to_full="⚡ Режим: Только чтение (Plan) ⇄ Запись",
    btn_ws_models="🧠 Сменить модель",
    btn_ws_workspace="📂 Сменить папку",
    btn_ws_switch_account="👤 Сменить аккаунт",
    btn_ws_archive_topic="🗄 В Архив",
    btn_ws_stop_turn="⏹ Остановить ход (/cancel)",
    btn_ws_fresh_session="🔄 С чистого листа",
    btn_ws_resp_to_plan="🛡 В режим Plan (Чтение)",
    btn_ws_resp_to_write="⚡ Разрешить запись",
    btn_ws_effort_item="Effort: {effort}",
    btn_ws_close_submenu="⬅️ Закрыть",
    btn_ws_session_history_item="{mark}{short_id}... ({turns_count} ход., {tok_k:.1f}k ток.)",
    btn_ws_account_picker_item="{mark}{slug} ({email_label})",
    # Control Panel Interactive Messages & Toasts
    cp_reserved_notice=(
        "ℹ️ Топик **#Control-Panel** предназначен исключительно для управления системой.\n"
        "Используйте кнопки на закрепленном дашборде или отправьте `/panel` для обновления."
    ),
    cp_toast_probing_quotas="Запрашиваю актуальные квоты по всем аккаунтам...",
    cp_auth_menu_prompt=(
        "🔑 **Управление аккаунтами Google AI Pro**\n"
        "Выберите существующий профиль для повторной OAuth-авторизации (Remote SSH Flow) "
        "или создайте новый изолированный D-Bus/Keyring профиль:"
    ),
    cp_register_new_acc_prompt=(
        "➕ **Регистрация нового профиля Google-аккаунта**\n"
        "Ответьте сообщением в формате `<slug> <email_label>` (например: `acc_2 backup@gmail.com`)."
    ),
    cp_toast_launching_oauth="Запускаю agy login (Remote SSH OAuth Flow)...",
    cp_oauth_start_failed="❌ Не удалось запустить OAuth-авторизацию для `{slug}`:\n`{error}`",
    cp_oauth_instructions=(
        "🔐 **OAuth-авторизация профиля `{slug}` ({email_label})**\n\n"
        "1. Нажмите кнопку ниже или скопируйте ссылку в браузер:\n`{auth_url}`\n\n"
        "2. Войдите в аккаунт `{email_label}`, скопируйте одноразовый код подтверждения "
        "и **отправьте его следующим сообщением в этот топик**."
    ),
    cp_switch_default_acc_prompt="🔀 **Выберите основной Google-аккаунт для новых топиков:**",
    cp_toast_default_acc_updated="Основной аккаунт обновлен.",
    cp_toast_remounting_hosts="Проверяю и монтирую удаленные хосты SSHFS...",
    cp_remount_failed="❌ Ошибка монтирования SSHFS для `{alias}`: `{error}`",
    cp_new_topic_prompt=(
        "➕ **Создание нового рабочего топика**\n"
        "Ответьте сообщением в формате:\n`<Название топика> | <Путь к папке> | <PERMANENT или TASK>`\n"
        "Пример:\n`🚀 Проект Альфа | {workspaces_root_dir}/alpha | TASK`"
    ),
    cp_tasks_menu_prompt="⏰ **Активные задачи планировщика ({count}):**\nНажмите на задачу для отмены:",
    cp_tasks_menu_updated="⏰ **Активные задачи планировщика ({count}):**",
    cp_toast_task_canceled="Задача отменена.",
    cp_settings_menu_header="⚙️ **Настройки по умолчанию для новых топиков:**\n",
    cp_settings_menu_item="• **{description}:** `{value_json}`",
    cp_edit_setting_prompt=(
        "✏️ **Изменение параметра `{key}`**\n"
        "Отправьте новое значение (например: `\"ru\"`, `\"en\"`, `\"gemini-4-argon-high\"`, `900`, `1.8`)."
    ),
    cp_invite_generated_msg=(
        "🎟 **Одноразовый инвайт-токен создан (действует 24 часа):**\n"
        "Попросите пользователя отправить в супергруппу команду:\n"
        "`/redeem {token}`"
    ),
    cp_toast_invite_generated="Инвайт-токен создан.",
    cp_new_acc_invalid_format="❌ Неверный формат. Укажите `<slug> <email_label>` (например: `acc_1 user@gmail.com`).",
    cp_new_acc_created=(
        "✅ Изолированный профиль D-Bus/Keyring `{slug}` создан и разблокирован.\n"
        "Теперь откройте **[🔑 Авторизовать / Добавить аккаунт]** для входа в Google."
    ),
    cp_oauth_success=(
        "✅ **OAuth-авторизация профиля `{slug}` успешно завершена!**\n"
        "Токен сохранен в изолированный `gnome-keyring` и зашифрован в PostgreSQL 18 ключом `Fernet`."
    ),
    cp_oauth_failed="❌ **Ошибка верификации OAuth-кода:**\n`{error}`",
    cp_new_topic_default_title="Новый топик",
    cp_new_topic_created="✅ Создан топик форума **{title}** (`topic_id={topic_id}`).",
    cp_new_topic_failed="❌ Не удалось создать топик: `{error}`",
    cp_setting_updated="✅ Параметр `{key}` обновлен: `{value_json}`.",
    cp_setting_update_failed="❌ Ошибка валидации параметра `{key}`:\n`{error}`",
    # Workspace Topic Interactive Messages & Toasts
    ws_auto_topic_title="💬 Новый диалог #{topic_id}",
    ws_folder_confirm_prompt=(
        "📂 **Выбор рабочей папки для топика**\n"
        "По умолчанию назначена папка:\n`{workspace_path}`\n"
        "*(папка будет создана только при подтверждении или первом запросе к агенту)*"
    ),
    ws_custom_dir_prompt=(
        "✏️ **Отправьте следующим сообщением в этот топик полный путь к нужной папке** "
        "(например: `/home/macsurmak/my-project`):"
    ),
    ws_dir_confirmed_msg="✅ Рабочая папка подтверждена и готова: `{workspace_path}`",
    btn_ws_confirm_default_dir="✅ Оставить по умолчанию",
    btn_ws_pick_existing_dir="📂 Выбрать из списка папок",
    btn_ws_enter_custom_dir="✏️ Ввести свой путь",
    ws_turn_stopped_msg="⏹ Выполнение текущего хода остановлено.",
    ws_no_running_turn_msg="ℹ️ В этом топике сейчас нет активного хода.",
    ws_starting_turn_msg="⏳ **Запускаю ход** (`{model_slug}` на `{account_slug}`)...",
    ws_turn_error_msg="❌ **Ошибка выполнения хода:**\n`{error}`",
    ws_empty_response_fallback="*(Ход завершен со статусом `{status}`)*",
    ws_toast_new_session="Начата чистая сессия (0 токенов).",
    ws_toast_mode_switched="Режим переключен: {mode}.",
    ws_toast_turn_canceled="Ход остановлен.",
    ws_toast_topic_archived="Топик отправлен в архив, воркер выгружен.",
    ws_select_past_session_prompt="🕰 **Выберите прошлую сессию для возврата:**",
    ws_session_resumed_msg="✅ Возобновлена сессия `{conversation_id}`.",
    ws_select_model_prompt="🧠 **Выберите модель для топика:**",
    ws_model_updated_msg="✅ Модель изменена на `{model_slug}`.",
    ws_model_unknown_msg=(
        "❌ Модель `{model_slug}` не найдена в `agy models`.\n"
        "Доступные модели: {available_models}"
    ),
    ws_effort_updated_msg="✅ Глубина рассуждений (Effort) изменена на `{effort}`.",
    ws_select_account_prompt="👤 **Выберите Google-аккаунт для этого топика:**",
    ws_account_switched_msg="✅ Google-аккаунт для топика переключен.",
    ws_select_workspace_prompt="📂 **Выберите рабочую директорию топика:**",
    ws_workspace_updated_msg="✅ Рабочая папка изменена на `{workspace_path}`.",
    ws_quota_card_header="📊 Квоты аккаунта {slug} ({email_label})",
    ws_quota_fallback_body="Остаток: `{min_pct}%`",
    ws_quota_error_msg="❌ Ошибка запроса квот: `{error}`",
    # Router Access & Invites
    router_invite_redeemed="✅ Инвайт-токен активирован! Доступ открыт.",
    router_invite_failed="❌ Ошибка активации инвайта: `{error}`",
    router_toast_owner_only="Доступ запрещен: только для владельца.",
    router_toast_access_denied="Доступ запрещен.",
    # Media Service Transfer Messages
    media_downloading_init="📥 Скачиваю `{file_name}` в `{dest_path}`...",
    media_downloading_progress="📥 Скачивание `{file_name}`: **{pct:.1f}%** ({mb_cur:.1f} / {mb_tot:.1f} МБ)",
    media_download_failed="❌ Ошибка скачивания `{file_name}`: `{error}`",
    media_uploading_init="📤 Отправляю `{file_name}` ({size_mb:.2f} МБ)...",
    media_uploading_progress="📤 Отправка `{file_name}`: **{pct:.1f}%** ({mb_cur:.1f} / {mb_tot:.1f} МБ)",
    media_upload_failed="❌ Ошибка отправки `{file_name}`: `{error}`",
    # Batcher, Scheduler & Topic Lifecycle
    batch_attached_files_header="[Прикрепленные файлы сохранены в локальный inbox]:",
    sched_report_header="⏰ **Отчет по запланированной задаче** (`{trigger_spec}`)\n━━━━━━━━━━━━━━━━━━\n",
    sched_report_empty_status="Статус: `{status}`",
    sched_report_failed="❌ **Сбой запланированной задачи** (`{task_id}`):\n`{error}`",
    sensor_reflex_never_report=(
        "⚡ **Сработал авто-рефлекс датчика `{name}`**\n"
        "• **Событие:** `{event_output}`"
    ),
    sensor_reflex_action_line="• **Команда рефлекса (`exit {rc}`):** `{action_command}`",
    sensor_reflex_handled_report=(
        "⚡ **Авто-рефлекс датчика `{name}` успешно отработал (`exit 0`)**\n"
        "• **Выполнено:** `{action_command}`"
    ),
    sensor_trigger_failed="❌ **Ошибка при срабатывании датчика `{name}`:**\n`{error}`",
    topic_archive_prefix="🗄 [Архив] ",
)

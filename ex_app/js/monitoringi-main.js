(function () {
  function boot() {
    const storedRenameTemplate = window.localStorage?.getItem("mrmRenameTemplate") || ""
    const activeRenameTemplate = storedRenameTemplate && storedRenameTemplate !== "ГБДОУ {номер} отчёт" ? storedRenameTemplate : "текст {алиас} {номер} текст"
    const allowedViews = ["dashboard", "organizations", "campaigns", "operations", "settings"]
    const storedView = window.localStorage?.getItem("mrmView") || "dashboard"
    const operationTabs = [
      ["log", "Лог"],
      ["folders", "Папки"],
      ["links", "Ссылки"],
      ["scan", "Проверка"],
      ["messages", "Рассылка"],
      ["rename", "Переименование"],
    ]
    const allowedOperationTabs = operationTabs.map(([id]) => id)
    const storedOperationTab = window.localStorage?.getItem("mrmOperationTab") || "log"
    const state = {
      view: allowedViews.includes(storedView) ? storedView : "dashboard",
      organizations: [],
      campaigns: [],
      groups: [],
      groupsLoaded: false,
      groupsLoading: false,
      recentUploads: [],
      settings: {},
      selectedCampaignId: window.localStorage?.getItem("mrmSelectedCampaignId") || "",
      editingOrganizationId: "",
      editingCampaignId: "",
      organizationFilter: "all",
      fileIds: new URLSearchParams(window.location.search).get("fileIds") || "",
      contextChecked: false,
      lastBatch: null,
      operationBatches: {},
      operationHistory: {},
      operationTab: allowedOperationTabs.includes(storedOperationTab) ? storedOperationTab : "log",
      operationState: {},
      renameTemplate: activeRenameTemplate,
    }

    function persistUiState() {
      window.localStorage?.setItem("mrmView", state.view)
      if (state.selectedCampaignId) {
        window.localStorage?.setItem("mrmSelectedCampaignId", state.selectedCampaignId)
      } else {
        window.localStorage?.removeItem("mrmSelectedCampaignId")
      }
      window.localStorage?.setItem("mrmOperationTab", state.operationTab)
    }

    const organizationTypes = [
      ["school", "Школы"],
      ["kindergarten", "Сады"],
      ["odo", "ОДО"],
      ["udod", "УДОДы"],
    ]

    const defaultOrganizationAliases = {
      school: ["ГБОУ"],
      kindergarten: ["ДОУ"],
      odo: ["ОДО"],
      udod: ["ЦФКСиЗ", "ДДЮТ", "ЦДЮТТ", "ЦППМСП", "ИМЦ"],
    }

    const views = [
      ["dashboard", "1", "Справка"],
      ["organizations", "2", "Список ОУ"],
      ["campaigns", "3", "Мониторинги"],
      ["operations", "4", "Операции"],
      ["settings", "5", "Настройки"],
    ]

    const DIRECTORY_MIME_TYPE = "httpd/unix-directory"
    const commonExtensions = ["pdf", "doc", "docx", "xls", "xlsx", "ods", "odt", "csv", "txt", "zip", "7z", "rar", "jpg", "jpeg", "png"]

    const statusLabels = {
      draft: "Черновик",
      folders_created: "Папки созданы",
      links_created: "Ссылки созданы",
      active: "Активен",
      closed: "Закрыт",
      archived: "Архив",
      pending: "Ожидает",
      folder_created: "Папка создана",
      link_created: "Ссылка создана",
      sent_manually: "Отправлено вручную",
      uploaded: "Загружено",
      accepted: "Принято",
      rejected: "Отклонено",
      missing: "Нет файла",
      preview: "Предпросмотр",
      running: "Выполняется",
      completed: "Завершено",
      completed_with_errors: "Завершено с ошибками",
      failed: "Ошибка",
      cancelled: "Отменено",
      sending: "Отправляется",
      sent: "Отправлено",
      bounced: "Не доставлено",
      success: "Успешно",
      skipped: "Пропущено",
      planned: "Запланировано",
      valid: "Принят",
      duplicate: "Дубль",
      trash: "Лишний",
      unknown: "Не распознан",
      invalid_extension: "Неверное расширение",
      invalid_name: "Неверное имя",
      move_trash: "Перемещён в Мусор",
      move_duplicate: "Перемещён в Дубли",
      rename_kept: "Переименован",
    }

    const root = document.getElementById("content") || document.getElementById("app")
    if (!root) return

    const embeddedMarker = "/apps/app_api/embedded/oo_file_collector"
    const proxyBase = window.location.pathname.includes(embeddedMarker)
      ? `${window.location.pathname.split("/apps/app_api/embedded/")[0]}/apps/app_api/proxy/oo_file_collector`
      : ""

    function parentOrWindow(path) {
      for (const scope of [window.parent, window]) {
        try {
          const value = path.reduce((current, key) => current?.[key], scope)
          if (value) return value
        } catch (_error) {
          // Cross-frame access can fail in some embedded contexts.
        }
      }
      return null
    }

    function showToast(message, kind = "info") {
      const text = String(message || "").trim()
      if (!text) return

      const ncToast = parentOrWindow(["OCP", "Toast"])
      const toastMethod = kind === "error" ? "error" : kind === "success" ? "success" : "info"
      if (ncToast && typeof ncToast[toastMethod] === "function") {
        ncToast[toastMethod](text)
        return
      }
      if (ncToast && typeof ncToast.show === "function") {
        ncToast.show(text, { type: toastMethod })
        return
      }

      const ncNotification = parentOrWindow(["OC", "Notification"])
      if (ncNotification && typeof ncNotification.showTemporary === "function") {
        ncNotification.showTemporary(text)
        return
      }

      let stack = document.querySelector(".mrm-toast-stack")
      if (!stack) {
        stack = document.createElement("div")
        stack.className = "mrm-toast-stack"
        document.body.appendChild(stack)
      }
      const toast = document.createElement("div")
      toast.className = `mrm-toast ${kind === "error" ? "is-error" : "is-success"}`
      toast.textContent = text
      stack.appendChild(toast)
      window.setTimeout(() => toast.remove(), 4200)
    }

    function setNotice(message, isError = false) {
      showToast(message, isError ? "error" : "success")
    }

    function extractError(payload, fallback) {
      const detail = payload?.detail
      if (typeof detail === "string") return detail
      if (detail?.message) return detail.message
      if (detail?.code) return detail.code
      if (Array.isArray(detail)) return detail.map((item) => item.msg || item.message || String(item)).join(", ")
      if (payload?.message) return payload.message
      return fallback
    }

    async function api(path, options = {}) {
      const response = await fetch(`${proxyBase}/api${path}`, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          ...(options.headers || {}),
        },
      })
      if (!response.ok) {
        let message = `HTTP ${response.status}`
        try {
          message = extractError(await response.json(), message)
        } catch (_error) {}
        throw new Error(message)
      }
      const type = response.headers.get("content-type") || ""
      return type.includes("application/json") ? response.json() : response.blob()
    }

    function arrayBufferToBase64(buffer) {
      const bytes = new Uint8Array(buffer)
      const chunkSize = 0x8000
      let binary = ""
      for (let index = 0; index < bytes.length; index += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunkSize))
      }
      return window.btoa(binary)
    }

    function messageBatchId(batch) {
      return batch?.message_batch_id || batch?.batch_id || batch?.id || ""
    }

    async function loadBase() {
      try {
        const [organizations, campaigns, settings, recentUploads] = await Promise.all([
          api("/organizations?active=true"),
          api("/campaigns"),
          api("/settings"),
          api("/recent-uploads?limit=10").catch(() => []),
        ])
        state.organizations = organizations
        state.campaigns = campaigns
        state.settings = settings
        state.recentUploads = recentUploads
        const previousCampaignId = state.selectedCampaignId
        if (!campaigns.some((campaign) => campaign.id === state.selectedCampaignId)) {
          state.selectedCampaignId = campaigns[0]?.id || ""
        }
        if (previousCampaignId !== state.selectedCampaignId) state.operationBatches = {}
        if (state.selectedCampaignId) {
          const [operationState, operationHistory] = await Promise.all([
            api(`/campaigns/${state.selectedCampaignId}/operation-state`).catch(() => ({})),
            api(`/campaigns/${state.selectedCampaignId}/operation-history`).catch(() => ({})),
          ])
          state.operationState = operationState
          state.operationHistory = operationHistory
        } else {
          state.operationState = {}
          state.operationHistory = {}
        }
        if (state.view === "operations" && !state.lastBatch) {
          const batchId = messageBatchId(state.operationState.message_batch)
          if (batchId) {
            state.lastBatch = await api(`/message-batches/${encodeURIComponent(batchId)}`, { method: "POST" }).catch(() => null)
          }
        }
        if (state.fileIds && !state.contextChecked) {
          state.contextChecked = true
          const context = await api(`/context-folder?file_ids=${encodeURIComponent(state.fileIds)}`)
          if (context.is_monitoring && context.campaign?.id) {
            state.selectedCampaignId = context.campaign.id
            state.view = "operations"
          } else {
            state.view = "dashboard"
            setNotice("Данный каталог не является мониторингом", true)
          }
        }
        persistUiState()
        render()
      } catch (error) {
        setNotice(error.message, true)
      }
    }

    function h(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
    }

    function typeLabel(value) {
      return organizationTypes.find(([id]) => id === value)?.[1] || String(value || "")
    }

    const uploadModeLabels = {
      campaign_root: "Одна ссылка на корень",
      org_type: "Ссылка на каждый тип ОУ",
      organization: "Ссылка для каждой организации",
    }

    const orgFolderLayoutLabels = {
      auto: "Авто",
      flat: "Организации в корне",
      by_type: "Организации внутри типов",
    }

    function uploadModeLabel(value) {
      return uploadModeLabels[value] || uploadModeLabels.organization
    }

    function orgFolderLayoutLabel(value) {
      return orgFolderLayoutLabels[value] || orgFolderLayoutLabels.auto
    }

    function campaignStructureLabel(campaign) {
      return campaign?.upload_mode === "organization" ? orgFolderLayoutLabel(campaign.org_folder_layout) : "Не используется"
    }

    function chips(values) {
      return (values || []).filter(Boolean).map((value) => `<span class="mrm-chip">${h(value)}</span>`).join("")
    }

    function compactExtensions(values) {
      const extensions = values || []
      if (!extensions.length) return "—"
      if (extensions.length <= 4) return extensions.join(", ")
      return `${extensions.slice(0, 4).join(", ")} +${extensions.length - 4}`
    }

    function campaignTitle(id) {
      return state.campaigns.find((item) => item.id === id)?.title || "мониторинг"
    }

    function syncCampaignStructureControls(form) {
      const uploadSelect = form?.elements?.upload_mode
      const layoutSelect = form?.elements?.org_folder_layout
      if (!uploadSelect || !layoutSelect) return
      const enabled = uploadSelect.value === "organization"
      layoutSelect.disabled = !enabled
      if (!enabled) layoutSelect.value = "auto"
    }

    function defaultAliasesForType(value) {
      return defaultOrganizationAliases[value] || []
    }

    function aliasText(value) {
      return (value || []).join(", ")
    }

    function isDefaultAliasText(value) {
      const normalized = String(value || "").trim()
      return Object.values(defaultOrganizationAliases).some((aliases) => aliasText(aliases) === normalized)
    }

    function formatDateTime(value) {
      if (!value) return ""
      const date = new Date(value)
      if (Number.isNaN(date.getTime())) return String(value)
      return date.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    }

    function formatSize(value) {
      const size = Number(value || 0)
      if (!size) return ""
      if (size < 1024) return `${size} Б`
      if (size < 1024 * 1024) return `${Math.round(size / 1024)} КБ`
      return `${(size / 1024 / 1024).toFixed(1)} МБ`
    }

    function orgTypeOptions(selected = "school") {
      return organizationTypes
        .map(([value, label]) => `<option value="${h(value)}" ${selected === value ? "selected" : ""}>${h(label)}</option>`)
        .join("")
    }

    function orgTypeChecks(selected = ["school", "kindergarten", "odo", "udod"]) {
      const values = new Set(selected && selected.length ? selected : organizationTypes.map(([value]) => value))
      return organizationTypes
        .map(
          ([value, label]) => `
            <label class="mrm-check">
              <input type="checkbox" name="selected_org_types" value="${h(value)}" ${values.has(value) ? "checked" : ""}>
              <span>${h(label)}</span>
            </label>
          `,
        )
        .join("")
    }

    function extensionValues(selected = ["pdf", "docx", "xlsx"]) {
      const selectedValues = (selected || []).map((item) => String(item || "").trim().toLowerCase()).filter(Boolean)
      return Array.from(new Set([...commonExtensions, ...(state.settings.default_extensions || []), ...selectedValues].map((item) => String(item || "").trim().toLowerCase()).filter(Boolean)))
    }

    function extensionTagHtml(value) {
      return `<button class="mrm-tag" type="button" data-extension-remove="${h(value)}">${h(value)} <span aria-hidden="true">×</span></button>`
    }

    function extensionInputHtml(value) {
      return `<input type="hidden" name="allowed_extensions" value="${h(value)}">`
    }

    function extensionPicker(selected = ["pdf", "docx", "xlsx"]) {
      const selectedValues = Array.from(new Set((selected || []).map((item) => String(item || "").trim().toLowerCase()).filter(Boolean)))
      const values = new Set(selectedValues)
      const extensions = extensionValues(selectedValues)
      const tags = selectedValues.length
        ? selectedValues.map(extensionTagHtml).join("")
        : `<span class="mrm-multiselect-placeholder">Выберите расширения</span>`
      return `
        <div class="mrm-multiselect" data-extension-picker>
          <div class="mrm-multiselect-control" data-extension-control tabindex="0" role="button" aria-haspopup="listbox" aria-expanded="false">
            <div class="mrm-multiselect-tags" data-extension-tags>${tags}</div>
            <span class="mrm-multiselect-arrow" aria-hidden="true"></span>
          </div>
          <div class="mrm-multiselect-menu" role="listbox">
            ${extensions
              .map(
                (value) => `
                  <button class="mrm-multiselect-option ${values.has(value) ? "is-selected" : ""}" type="button" data-extension-option="${h(value)}" aria-selected="${values.has(value) ? "true" : "false"}">
                    ${h(value)}
                  </button>
                `,
              )
              .join("")}
          </div>
          <div data-extension-inputs>${selectedValues.map(extensionInputHtml).join("")}</div>
        </div>
      `
    }

    function selectedExtensionValues(picker) {
      return Array.from(picker.querySelectorAll('input[name="allowed_extensions"]')).map((input) => input.value).filter(Boolean)
    }

    function setExtensionValues(picker, values) {
      const selectedValues = Array.from(new Set((values || []).map((item) => String(item || "").trim().toLowerCase()).filter(Boolean)))
      const selected = new Set(selectedValues)
      const tags = picker.querySelector("[data-extension-tags]")
      const inputs = picker.querySelector("[data-extension-inputs]")
      if (tags) {
        tags.innerHTML = selectedValues.length
          ? selectedValues.map(extensionTagHtml).join("")
          : `<span class="mrm-multiselect-placeholder">Выберите расширения</span>`
      }
      if (inputs) inputs.innerHTML = selectedValues.map(extensionInputHtml).join("")
      picker.querySelectorAll("[data-extension-option]").forEach((option) => {
        const active = selected.has(option.dataset.extensionOption)
        option.classList.toggle("is-selected", active)
        option.setAttribute("aria-selected", active ? "true" : "false")
      })
    }

    function toggleExtensionPicker(picker, force) {
      const open = force ?? !picker.classList.contains("is-open")
      picker.classList.toggle("is-open", open)
      picker.querySelector("[data-extension-control]")?.setAttribute("aria-expanded", open ? "true" : "false")
    }

    function closeExtensionPickers(except = null) {
      root.querySelectorAll("[data-extension-picker].is-open").forEach((picker) => {
        if (picker !== except) toggleExtensionPicker(picker, false)
      })
    }

    function groupOptions(selected) {
      const groups = Array.from(new Set([...(state.groups || []), selected].map((item) => String(item || "").trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b, "ru"))
      return [`<option value="">Не выбрана</option>`, ...groups.map((group) => `<option value="${h(group)}" ${selected === group ? "selected" : ""}>${h(group)}</option>`)].join("")
    }

    function normalizeMimeType(mimetype) {
      return String(mimetype || "").split(";", 1)[0].trim().toLowerCase()
    }

    function isPickerDirectory(node) {
      const type = String(node?.type || "").toLowerCase()
      const mime = normalizeMimeType(node?.mime || node?.mimetype || node?.mimeType || node?.contentType || "")
      return node?.is_dir === true
        || node?.isDirectory === true
        || node?.isFolder === true
        || type === "dir"
        || type === "folder"
        || mime === DIRECTORY_MIME_TYPE
        || mime === "inode/directory"
    }

    function nextcloudFilePickerBuilderFactory() {
      const factories = []
      try {
        factories.push(window.parent?.OC?.dialogs?.getFilePickerBuilder)
        factories.push(window.parent?.OCP?.Files?.getFilePickerBuilder)
        factories.push(window.parent?.getFilePickerBuilder)
      } catch (_error) {}
      factories.push(window.OC?.dialogs?.getFilePickerBuilder)
      factories.push(window.OCP?.Files?.getFilePickerBuilder)
      factories.push(window.getFilePickerBuilder)
      return factories.find((factory) => typeof factory === "function") || null
    }

    function selectedPickerPath(value) {
      const selected = Array.isArray(value) ? value[0] : value
      if (!selected) return ""
      if (typeof selected === "string") return selected.startsWith("/") ? selected : `/${selected}`
      const path = selected.path || selected.filePath || selected.filename || selected.basename || ""
      return typeof path === "string" && path ? (path.startsWith("/") ? path : `/${path}`) : ""
    }

    function setPickedFolder(value, input = null) {
      const path = selectedPickerPath(value)
      if (!path) {
        setNotice("Не удалось определить выбранную папку", true)
        return
      }
      const targetInput = input || root.querySelector('[name="parent_nc_path"]')
      if (targetInput) targetInput.value = path
      return path
    }

    function legacyDialogs() {
      for (const scope of [window.parent, window]) {
        try {
          if (scope?.OC?.dialogs?.filepicker) return scope.OC.dialogs
        } catch (_error) {}
      }
      return null
    }

    function openFolderPicker({ title = "Выберите папку", onPick = setPickedFolder } = {}) {
      const pick = (value) => {
        Promise.resolve(onPick(value)).catch((error) => setNotice(error.message, true))
      }
      const builderFactory = nextcloudFilePickerBuilderFactory()
      if (typeof builderFactory === "function") {
        try {
          const builder = builderFactory(title)
          builder.addMimeTypeFilter?.(DIRECTORY_MIME_TYPE)
          builder.setFilter?.((node) => isPickerDirectory(node))
          builder.setCanPick?.((node) => isPickerDirectory(node))
          builder.allowDirectories?.(true)
          builder.setMultiSelect?.(false)
          builder.build().pick().then(pick).catch(() => {})
          return
        } catch (error) {
          console.warn("Nextcloud folder picker builder failed:", error)
        }
      }

      const dialogs = legacyDialogs()
      if (dialogs) {
        dialogs.filepicker(title, pick, false, [DIRECTORY_MIME_TYPE], true)
        return
      }

      setNotice("API выбора папки Nextcloud недоступно", true)
    }

    function statusPill(status) {
      const ok = ["active", "uploaded", "accepted", "completed", "links_created", "success", "valid", "sent"]
      const warn = ["draft", "preview", "missing", "completed_with_errors", "folders_created", "planned", "skipped", "duplicate", "unknown", "sending", "pending"]
      const errors = ["failed", "invalid_extension", "invalid_name", "trash", "bounced"]
      const cls = ok.includes(status) ? "ok" : warn.includes(status) ? "warn" : errors.includes(status) ? "error" : ""
      return `<span class="mrm-pill ${cls}">${h(statusLabels[status] || status)}</span>`
    }

    const operationLabels = {
      create_folders: "Создать папки для всех",
      create_shares: "Ссылки для загрузки",
      delete_shares: "Удаление публичных ссылок",
      scan: "Очистка от дублей и мусора",
      scan_cleanup: "Обработка файлов проверки",
      rename: "Переименование по шаблону",
      move: "Перемещение ошибочных файлов",
      messages: "Письма организациям",
    }

    const actionLabels = {
      create_folder: "Создать папку",
      create_share: "Создать ссылку",
      move: "Переименовать/переместить",
      rename: "Переименовать",
      classify: "Проверить файл",
      copy: "Скопировать",
      delete: "Удалить",
    }

    const reasonLabels = {
      campaign_folder: "Папка мониторинга",
      organization_folder: "Папка организации",
      organization_folder_rename: "Изменился номер организации",
      public_upload_link: "Запрос файла",
      public_edit_link: "Ссылка с редактированием",
      public_share: "Публичная ссылка",
      normalized_filename: "Имя будет приведено к единому виду",
      template_rename: "Имя будет построено по заданному шаблону",
      duplicate_old_version: "Дубль, оставлена более новая версия",
      unknown_organization: "Организация не найдена в справочнике",
      invalid_extension: "Недопустимое расширение файла",
      empty_file: "Пустой файл",
      ambiguous_match: "Файл подходит нескольким организациям",
    }

    const fileActionLabels = {
      move_trash: "Перемещён в Мусор",
      move_duplicate: "Перемещён в Дубли",
      rename_kept: "Переименован",
    }

    const summaryLabels = {
      total: "Файлов проверено",
      files_checked: "Файлов учтено",
      eligible: "Можно переименовать",
      folders_to_create: "Папок проверить/создать",
      folders_to_rename: "Папок переименовать",
      share_targets: "Целей для ссылок",
      shares_to_create: "Ссылок создать",
      existing_links: "Ссылок уже есть",
      deleted: "Удалено",
      organizations_without_email: "Организаций без email",
      planned: "Запланировано",
      pending: "К отправке",
      sent: "Отправлено",
      already_ok: "Уже в порядке",
      skipped: "Пропущено",
      trash_moved: "Перемещено в Мусор",
      duplicates_moved: "Перемещено в Дубли",
      renamed: "Переименовано",
      operation_errors: "Ошибок обработки",
      success: "Успешно",
      failed: "Ошибок",
      valid: "Принято",
      duplicate: "Дублей",
      trash: "Лишних файлов",
      unknown: "Не распознано",
      invalid_extension: "Неверное расширение",
      invalid_name: "Неверное имя",
      missing: "Без файлов",
      without_email: "Без email",
    }

    function summaryLabel(key) {
      return summaryLabels[key] || String(key).replaceAll("_", " ")
    }

    function resultNote(result) {
      const status = result.status || ""
      if (result.operation_type === "scan" && status === "preview") {
        return "Файлы проверены без сохранения. Чтобы отчёт, напоминания, переименование и перемещение использовали этот результат, нажмите «Сохранить результат проверки»."
      }
      if (result.operation_type === "scan" && status === "completed") {
        return "Результат сохранён: неправильные файлы перенесены в «Мусор», дубли в «Дубли», оставшиеся актуальные файлы переименованы."
      }
      if (result.operation_type === "scan" && status === "completed_with_errors") {
        return "Результат сохранён частично. Строки с ошибками обработки нужно проверить вручную."
      }
      if (result.operation_type === "rename" && status === "preview") {
        return "Это план массового переименования по шаблону. Файлы ещё не изменены."
      }
      if (result.operation_type === "move" && status === "preview") {
        return "Это план перемещения дублей и ошибочных файлов. Файлы ещё не изменены."
      }
      if (status === "completed" || status === "completed_with_errors") {
        return "Операция выполнена. Строки со статусом «Ошибка» требуют проверки вручную."
      }
      if (result.operation_type === "messages" && status === "preview" && (result.summary?.pending || 0) > 0) {
        return "Письма подготовлены. Нажмите отправку, чтобы передать pending-письма в почтовый мост."
      }
      if (result.operation_type === "messages" && (result.summary?.pending || 0) === 0) {
        return "Нет писем, готовых к отправке. Обычно это значит, что у отсутствующих организаций не указан email или все письма уже обработаны."
      }
      return ""
    }

    function actionLabel(item, operationType) {
      if (operationType === "move") return "Переместить ошибочный файл"
      if (operationType === "rename") return "Переименовать по шаблону"
      if (item.reason === "organization_folder_rename") return "Переименовать папку"
      return actionLabels[item.action] || item.action || ""
    }

    function operationResult() {
      const result = state.lastBatch
      if (!result) {
        return `<div class="mrm-empty">Здесь появится понятный результат последней операции: что будет создано, переименовано или где возникла ошибка.</div>`
      }
      const summary = result.summary || {}
      const cards = Object.entries(summary)
        .map(([key, value]) => `<div class="mrm-result-stat"><strong>${h(value)}</strong><span>${h(summaryLabel(key))}</span></div>`)
        .join("")
      const title = operationLabels[result.operation_type] || (result.files ? "Проверка файлов" : "Результат операции")
      const note = resultNote(result)
      const rows = result.operation_type === "messages"
        ? messageRows(result.items || [])
        : result.items
          ? operationRows(result.items, result.operation_type)
          : fileRows(result.files || [])
      return `
        <div class="mrm-result">
          <div class="mrm-result-head">
            <div>
              <strong>${h(title)}</strong>
              ${result.status ? statusPill(result.status) : ""}
            </div>
            ${result.batch_id ? `<span class="mrm-hint">Пакет: ${h(result.batch_id)}</span>` : ""}
          </div>
          ${note ? `<div class="mrm-result-note">${h(note)}</div>` : ""}
          ${cards ? `<div class="mrm-result-stats">${cards}</div>` : ""}
          ${rows}
        </div>
      `
    }

    function operationRows(items, operationType) {
      if (!items.length) return `<div class="mrm-empty">По этой операции изменений нет.</div>`
      return `
        <div class="mrm-table-wrap">
          <table class="mrm-table">
            <thead><tr><th>Действие</th><th>Статус</th><th>Что изменится</th><th>Комментарий</th></tr></thead>
            <tbody>
              ${items
                .map((item) => {
                  const source = item.source_path ? `<div class="mrm-muted-line">Было: ${h(item.source_path)}</div>` : ""
                  const target = item.target_path ? `<div>${h(item.target_path)}</div>` : ""
                  const org = item.metadata_json?.organization_code ? `<div class="mrm-muted-line">ОУ: ${h(item.metadata_json.organization_code)}</div>` : ""
                  return `
                    <tr>
                      <td>${h(actionLabel(item, operationType))}</td>
                      <td>${statusPill(item.status)}</td>
                      <td class="is-wrap">${org}${source}${target}</td>
                      <td class="is-wrap">${h(item.error || reasonLabels[item.reason] || item.reason || "")}</td>
                    </tr>
                  `
                })
                .join("")}
            </tbody>
          </table>
        </div>
      `
    }

    function messageRows(items) {
      if (!items.length) return `<div class="mrm-empty">Писем для выбранного результата проверки нет.</div>`
      return `
        <div class="mrm-table-wrap">
          <table class="mrm-table">
            <thead><tr><th>Организация</th><th>Email</th><th>Статус</th><th>Тема</th><th>Комментарий</th></tr></thead>
            <tbody>
              ${items
                .map((item) => {
                  const org = item.organization_code || item.organization_name || item.organization_id || ""
                  const email = item.to_email || ""
                  return `
                    <tr>
                      <td>${h(org)}</td>
                      <td>${email ? h(email) : `<span class="mrm-muted-line">не указан</span>`}</td>
                      <td>${statusPill(item.status)}</td>
                      <td class="is-wrap">${h(item.subject || "")}</td>
                      <td class="is-wrap">${h(item.last_error || item.error || "")}</td>
                    </tr>
                  `
                })
                .join("")}
            </tbody>
          </table>
        </div>
      `
    }

    function fileRows(files) {
      if (!files.length) return `<div class="mrm-empty">Файлы не найдены.</div>`
      return `
        <div class="mrm-table-wrap">
          <table class="mrm-table">
            <thead><tr><th>Файл</th><th>Результат</th><th>ОУ</th><th>Нормальное имя</th><th>Комментарий</th></tr></thead>
            <tbody>
              ${files
                .map((file) => {
                  const action = file.file_action ? fileActionLabels[file.file_action] || file.file_action : ""
                  const target = file.target_path ? ` → ${file.target_path}` : ""
                  const operationError = file.operation_error ? `Ошибка обработки: ${file.operation_error}` : ""
                  const comment = operationError || (action ? `${action}${target}` : reasonLabels[file.reason] || file.reason || (file.organization_code ? "Организация найдена" : ""))
                  return `
                    <tr>
                      <td class="is-wrap">${h(file.path || file.original_name || "")}</td>
                      <td>${statusPill(file.classification)}</td>
                      <td class="is-wrap">${h(file.organization_code || "")}</td>
                      <td class="is-wrap">${h(file.normalized_name || "")}</td>
                      <td class="is-wrap">${h(comment)}</td>
                    </tr>
                  `
                })
                .join("")}
            </tbody>
          </table>
        </div>
      `
    }

    function shell(content, title, subtitle, actions = "") {
      const topbar = title || subtitle || actions
        ? `
          <div class="mrm-topbar">
            <div>${title ? `<h2>${h(title)}</h2>` : ""}${subtitle ? `<p>${h(subtitle)}</p>` : ""}</div>
            <div class="mrm-actions">${actions}</div>
          </div>
        `
        : ""
      return `
        <div class="mrm-shell">
          <aside class="mrm-sidebar">
            <div class="mrm-brand">
              <h1>Сбор файлов ОО</h1>
              <span>Nextcloud ExApp</span>
            </div>
            <nav class="mrm-nav">
              ${views
                .map(
                  ([id, icon, label]) => `
                    <button type="button" data-view="${id}" class="${state.view === id ? "is-active" : ""}" title="${h(label)}">
                      <span class="mrm-icon">${icon}</span><span>${h(label)}</span>
                    </button>
                  `,
                )
                .join("")}
            </nav>
          </aside>
          <main class="mrm-main">
            ${topbar}
            ${content}
          </main>
        </div>
      `
    }

    function dashboard() {
      const active = state.campaigns.filter((item) => ["active", "links_created", "folders_created"].includes(item.status)).length
      const orgsWithoutEmail = state.organizations.filter((item) => !item.email).length
      const latestUploads = state.recentUploads || []
      const nextStep = state.organizations.length === 0
        ? "Заполните список ОУ"
        : state.campaigns.length === 0
          ? "Создайте первый мониторинг"
          : "Откройте операции выбранного мониторинга"
      return shell(
        `
        <section class="mrm-grid">
          <div class="mrm-panel mrm-hero">
            <div>
              <p class="mrm-kicker">Быстрый старт</p>
              <h3>${h(nextStep)}</h3>
              <p>Приложение работает через WebDAV и OCS API от имени текущего пользователя Nextcloud. Список ОУ хранится общим для всех пользователей приложения.</p>
              ${state.fileIds ? `<p class="mrm-context">Открыто из контекстного меню Files. ID выбранного объекта: ${h(state.fileIds)}</p>` : ""}
            </div>
            <div class="mrm-actions">
              <button class="mrm-button primary" type="button" data-view="organizations">Список ОУ</button>
              <button class="mrm-button" type="button" data-view="campaigns">Создать мониторинг</button>
            </div>
          </div>
          <div class="mrm-stat"><strong>${state.campaigns.length}</strong><span>Мониторингов</span></div>
          <div class="mrm-stat"><strong>${active}</strong><span>Активных</span></div>
          <div class="mrm-stat"><strong>${state.organizations.length}</strong><span>Количество ОУ</span></div>
          <div class="mrm-stat"><strong>${orgsWithoutEmail}</strong><span>Без email</span></div>
          <div class="mrm-panel">
            <div class="mrm-panel-header"><h3>Последние загруженные файлы</h3></div>
            <div class="mrm-table-wrap">
              <table class="mrm-table">
                <thead><tr><th>Файл</th><th>Куда загружен</th><th>Папка</th><th>Когда</th></tr></thead>
                <tbody>
                  ${latestUploads
                    .map(
                      (item) => `
                        <tr>
                          <td class="is-wrap">${h(item.name)}${item.size ? `<span class="mrm-muted-inline">${h(formatSize(item.size))}</span>` : ""}</td>
                          <td>${h([item.campaign_title, item.organization_name].filter(Boolean).join(" / "))}</td>
                          <td class="is-wrap">${h(item.folder_path)}</td>
                          <td>${h(formatDateTime(item.uploaded_at))}</td>
                        </tr>
                      `,
                    )
                    .join("") || `<tr><td colspan="4">Загруженных файлов пока нет</td></tr>`}
                </tbody>
              </table>
            </div>
          </div>
        </section>
        `,
        "",
        "",
      )
    }

    function organizationRows(items) {
      return items
        .map(
          (item) => `
            <tr>
              <td>${h(typeLabel(item.type))}</td>
              <td>${h(item.code)}</td>
              <td>${h(item.email)}</td>
              <td>${h(aliasText(item.aliases))}</td>
              <td class="mrm-row-actions">
                <button class="mrm-button compact" type="button" data-edit-organization="${h(item.id)}">Изменить</button>
                <button class="mrm-button compact danger" type="button" data-delete-organization="${h(item.id)}">Удалить</button>
              </td>
            </tr>
          `,
        )
        .join("")
    }

    function organizations() {
      const editing = state.organizations.find((item) => item.id === state.editingOrganizationId)
      const filterOptions = [["all", "Все"], ...organizationTypes]
      const visibleOrganizations = state.organizationFilter === "all"
        ? state.organizations
        : state.organizations.filter((item) => item.type === state.organizationFilter)
      return shell(
        `
        <section class="mrm-grid">
          <form class="mrm-form" data-form="organization">
            <h3>${editing ? "Редактирование" : "Добавить запись"}</h3>
            <div class="mrm-fields">
              <div class="mrm-field">
                <label>Тип ОУ *</label>
                <select name="type" data-org-type-select required>${orgTypeOptions(editing?.type || "school")}</select>
              </div>
              <div class="mrm-field">
                <label>Номер *</label>
                <input name="code" value="${h(editing?.code)}" required>
              </div>
              <div class="mrm-field">
                <label>Email</label>
                <input name="email" type="email" value="${h(editing?.email)}">
              </div>
              <div class="mrm-field">
                <label>Алиасы</label>
                <input name="aliases" data-org-aliases value="${h(aliasText(editing?.aliases || defaultAliasesForType(editing?.type || "school")))}">
              </div>
              <div class="mrm-form-actions">
                <button class="mrm-button primary" type="submit">${editing ? "Сохранить" : "Добавить"}</button>
                ${editing ? `<button class="mrm-button" type="button" data-cancel-organization>Отмена</button>` : ""}
              </div>
            </div>
          </form>
          <div class="mrm-panel mrm-wide-panel">
            <div class="mrm-panel-header">
              <h3>Список ОУ</h3>
              <div class="mrm-actions">
                <label class="mrm-file-button">Импорт XLSX/CSV<input type="file" data-import-organizations accept=".csv,.xlsx"></label>
                <button class="mrm-button" type="button" data-download="organizations.xlsx">Экспорт XLSX</button>
              </div>
            </div>
            <div class="mrm-panel-body">
              <div class="mrm-list-tools">
                <div class="mrm-segmented" role="tablist" aria-label="Фильтр типа ОУ">
                  ${filterOptions
                    .map(([value, label]) => `<button class="${state.organizationFilter === value ? "is-active" : ""}" type="button" data-org-filter="${h(value)}">${h(label)}</button>`)
                    .join("")}
                </div>
                <p class="mrm-hint">Колонки для импорта: Тип ОУ, Номер, Email, Алиасы.</p>
              </div>
            </div>
            <div class="mrm-table-wrap">
              <table class="mrm-table">
                <thead><tr><th>Тип ОУ</th><th>Номер</th><th>Email</th><th>Алиасы</th><th></th></tr></thead>
                <tbody>
                  ${organizationRows(visibleOrganizations) || `<tr><td colspan="5">Записей нет</td></tr>`}
                </tbody>
              </table>
            </div>
          </div>
        </section>
        `,
        "Список ОУ",
        "Общий список образовательных организаций",
      )
    }

    function campaignRows(items) {
      return items
        .map((item) => {
          const typeChips = chips((item.selected_org_types || []).map(typeLabel))
          const settingsChips = chips([
            uploadModeLabel(item.upload_mode),
            campaignStructureLabel(item),
            compactExtensions(item.allowed_extensions || []),
          ])
          return `
            <tr>
              <td class="mrm-campaign-title-cell">
                <strong>${h(item.title)}</strong>
                <span>${h(item.campaign_folder_path)}</span>
              </td>
              <td><div class="mrm-chip-row">${typeChips || `<span class="mrm-muted-line">—</span>`}</div></td>
              <td><div class="mrm-chip-row">${settingsChips}</div></td>
              <td class="mrm-row-actions mrm-row-actions-compact">
                <button class="mrm-button compact" type="button" data-select-campaign="${h(item.id)}">Выбрать</button>
                <button class="mrm-button compact" type="button" data-edit-campaign="${h(item.id)}">Изменить</button>
                <button class="mrm-button compact" type="button" data-move-campaign="${h(item.id)}">Переместить</button>
                <button class="mrm-button compact danger" type="button" data-archive-campaign="${h(item.id)}">В архив</button>
              </td>
            </tr>
          `
        })
        .join("")
    }

    function campaigns() {
      const editing = state.campaigns.find((item) => item.id === state.editingCampaignId)
      const selectedTypes = editing?.selected_org_types || ["school"]
      const allowedExtensions = editing?.allowed_extensions || state.settings.default_extensions || ["pdf", "docx", "xlsx"]
      const uploadMode = editing?.upload_mode || "organization"
      const orgFolderLayout = editing?.org_folder_layout || "auto"
      return shell(
        `
        <section class="mrm-grid">
          <form class="mrm-form mrm-campaign-form" data-form="campaign">
            <h3>${editing ? "Редактирование" : "Мониторинг"}</h3>
            <div class="mrm-fields">
              <div class="mrm-field"><label>Название</label><input name="title" value="${h(editing?.title)}" required></div>
              <div class="mrm-field"><label>Срок сдачи</label><input name="deadline" type="date" value="${h(editing?.deadline)}"></div>
              <div class="mrm-field">
                <label>Путь</label>
                <div class="mrm-input-action">
                  <input name="parent_nc_path" value="${h(editing?.parent_nc_path || state.settings.root_path || "/Мониторинги")}">
                  <button class="mrm-button" type="button" data-pick-folder>Выбрать</button>
                </div>
              </div>
              <div class="mrm-field">
                <label>Типы ОУ</label>
                <div class="mrm-checklist">${orgTypeChecks(selectedTypes)}</div>
              </div>
              <div class="mrm-field">
                <label>Ссылки для загрузки</label>
                <select name="upload_mode">
                  ${Object.entries(uploadModeLabels).map(([value, label]) => `<option value="${h(value)}" ${uploadMode === value ? "selected" : ""}>${h(label)}</option>`).join("")}
                </select>
              </div>
              <div class="mrm-field">
                <label>Структура папок</label>
                <select name="org_folder_layout" ${uploadMode === "organization" ? "" : "disabled"}>
                  ${Object.entries(orgFolderLayoutLabels).map(([value, label]) => `<option value="${h(value)}" ${orgFolderLayout === value ? "selected" : ""}>${h(label)}</option>`).join("")}
                </select>
              </div>
              <div class="mrm-field">
                <label>Расширения</label>
                ${extensionPicker(allowedExtensions)}
              </div>
              <div class="mrm-form-actions">
                <button class="mrm-button primary" type="submit">${editing ? "Сохранить" : "Создать"}</button>
                ${editing ? `<button class="mrm-button" type="button" data-cancel-campaign>Отмена</button>` : ""}
              </div>
            </div>
          </form>
          <div class="mrm-panel mrm-wide-panel">
            <div class="mrm-panel-header">
              <h3>Мониторинги</h3>
            </div>
            <div class="mrm-table-wrap mrm-campaign-table-wrap">
              <table class="mrm-table mrm-campaign-table">
                <thead><tr><th>Мониторинг</th><th>Типы ОУ</th><th>Параметры</th><th></th></tr></thead>
                <tbody>
                  ${campaignRows(state.campaigns) || `<tr><td colspan="4">Нет мониторингов</td></tr>`}
                </tbody>
              </table>
            </div>
          </div>
        </section>
        `,
        "Мониторинги",
        "Главный каталог, в котором будут выполняться массовые операции, проверки и рассылки для ОУ из списка",
      )
    }

    function settings() {
      const rootPath = state.settings.root_path || "/Мониторинги"
      const archiveFolder = state.settings.archive_folder || ""
      const collaborationGroup = state.settings.collaboration_group || ""
      return shell(
        `
        <section class="mrm-grid">
          <form class="mrm-form" data-form="settings">
            <h3>Настройки</h3>
            <div class="mrm-fields">
              <div class="mrm-field">
                <label>Корневая папка</label>
                <div class="mrm-input-action">
                  <input name="root_path" value="${h(rootPath)}" required>
                  <button class="mrm-button" type="button" data-pick-folder>Выбрать</button>
                </div>
              </div>
              <div class="mrm-field">
                <label>Каталог архива</label>
                <div class="mrm-input-action">
                  <input name="archive_folder" value="${h(archiveFolder)}" required>
                  <button class="mrm-button" type="button" data-pick-folder>Выбрать</button>
                </div>
              </div>
              <div class="mrm-field">
                <label>Группа доступа</label>
                <div class="mrm-input-action">
                  <select name="collaboration_group" data-group-select>
                    ${groupOptions(collaborationGroup)}
                  </select>
                  <button class="mrm-button" type="button" data-load-groups>${state.groupsLoading ? "Загрузка" : "Обновить"}</button>
                </div>
              </div>
              <div class="mrm-form-actions">
                <button class="mrm-button primary" type="submit">Сохранить</button>
                <button class="mrm-button" type="button" data-test-webdav>Проверить</button>
                <button class="mrm-button" type="button" data-test-mail>Проверить почту</button>
              </div>
            </div>
          </form>
        </section>
        `,
        "Настройки",
        "Корень, архив и группа доступа",
      )
    }

    function checkSummaryText(summary = {}) {
      return `Файлов найдено: ${summary.total || 0}; Корректных: ${summary.valid || 0}; Дублей: ${summary.duplicate || 0}; Неподходящих: ${(summary.invalid_extension || 0) + (summary.invalid_name || 0) + (summary.unknown || 0)}; Не предоставили: ${summary.missing || 0}`
    }

    function hasValue(value) {
      if (Array.isArray(value)) return value.length > 0
      return value !== undefined && value !== null && value !== ""
    }

    function displayValue(value) {
      if (!hasValue(value)) return "—"
      if (Array.isArray(value)) return value.join(", ")
      return String(value)
    }

    function operationTabLabel(tab) {
      return operationTabs.find(([id]) => id === tab)?.[1] || tab
    }

    function operationTabForResult(result) {
      const type = result?.operation_type || ""
      if (type === "create_folders") return "folders"
      if (type === "create_shares" || type === "delete_shares") return "links"
      if (type === "scan" || type === "scan_cleanup") return "scan"
      if (type === "messages") return "messages"
      if (type === "rename") return "rename"
      return ""
    }

    function resultRunId(result) {
      return result?.run_id || result?.batch_id || result?.check_result_id || result?.message_batch_id || ""
    }

    function folderStructureLabel(value) {
      const labels = {
        campaign_root: "Корень мониторинга",
        flat: "Папки ОУ в корне",
        by_type: "Папки по типам ОУ",
        auto: "Авто",
      }
      return labels[value] || displayValue(value)
    }

    function runStartedAt(run) {
      return run.started_at || run.queued_at || run.created_at || run.finished_at || ""
    }

    function runDate(value) {
      const formatted = formatDateTime(value)
      return formatted || displayValue(value)
    }

    function summaryContextValue(summary, context, key) {
      if (hasValue(summary?.[key])) return summary[key]
      if (hasValue(context?.[key])) return context[key]
      return ""
    }

    function summaryRows(rows) {
      return `
        <dl class="mrm-summary-list">
          ${rows
            .map(
              (row) => `
                <div>
                  <dt>${h(row.label)}</dt>
                  <dd class="${row.long ? "is-preline" : ""}">${h(displayValue(row.value))}</dd>
                </div>
              `,
            )
            .join("")}
        </dl>
      `
    }

    function operationRunSummaryRows(tab, run) {
      const summary = run.summary || {}
      const context = state.operationHistory?.context || {}
      const value = (key) => summaryContextValue(summary, context, key)
      if (tab === "folders") {
        return [
          { label: "Операция запускалась", value: runDate(runStartedAt(run)) },
          { label: "Путь", value: value("path") },
          { label: "Создано папок", value: summary.success ?? summary.folders_to_create ?? summary.planned ?? 0 },
          { label: "Задействованные типы ОУ", value: value("organization_type_labels") },
          { label: "Структура папок", value: folderStructureLabel(value("folder_structure")) },
          { label: "Ошибок", value: summary.failed ?? summary.operation_errors ?? 0 },
        ]
      }
      if (tab === "links") {
        const isDelete = run.operation_type === "delete_shares"
        return [
          { label: "Операция запускалась", value: runDate(runStartedAt(run)) },
          { label: "Тип ссылок", value: uploadModeLabel(value("upload_mode")) },
          { label: "Срок публикации", value: runDate(value("deadline")) },
          { label: isDelete ? "Удалено ссылок" : "Создано ссылок", value: isDelete ? summary.deleted ?? summary.success ?? 0 : summary.success ?? summary.shares_to_create ?? summary.share_targets ?? 0 },
          { label: "Ошибок", value: summary.failed ?? 0 },
        ]
      }
      if (tab === "scan") {
        return [
          { label: "Операция запускалась", value: runDate(runStartedAt(run)) },
          { label: "Нормализовано файлов", value: summary.renamed ?? 0 },
          { label: "Помещено в дубли", value: summary.duplicates_moved ?? 0 },
          { label: "Помещено в мусор", value: summary.trash_moved ?? 0 },
          { label: "Не предоставлено", value: summary.missing ?? 0 },
          { label: "Не предоставившие ОУ", value: summary.missing_organization_names || summary.missing_organization_codes || "" },
        ]
      }
      if (tab === "messages") {
        return [
          { label: "Синхронизировано с проверкой файлов от", value: runDate(summary.check_result_created_at || run.check_result_created_at) },
          { label: "Не предоставлено", value: summary.missing ?? summary.planned ?? 0 },
          { label: "Не предоставившие ОУ", value: summary.missing_organization_names || summary.missing_organization_codes || "" },
          { label: "Из них без Email", value: summary.without_email ?? summary.failed ?? 0 },
          { label: "Последняя рассылка напоминаний", value: runDate(run.finished_at || run.queued_at || "") },
          { label: "Превью рассылки", value: summary.preview_text || "", long: true },
        ]
      }
      if (tab === "rename") {
        return [
          { label: "Операция запускалась", value: runDate(runStartedAt(run)) },
          { label: "Количество переименованных", value: summary.success ?? summary.renamed ?? summary.planned ?? 0 },
          { label: "Типы ОУ", value: value("organization_type_labels") },
          { label: "Указанный шаблон", value: summary.name_template || state.renameTemplate },
        ]
      }
      return []
    }

    function operationRunTitle(run) {
      const label = operationLabels[run.operation_type] || operationTabLabel(operationTabForResult(run))
      const status = run.status ? ` · ${statusLabels[run.status] || run.status}` : ""
      return `${label}${status}`
    }

    function operationRunDetails(tab, run) {
      if (run.operation_type === "messages") return messageRows(run.items || [])
      if (tab === "scan" && run.files) return fileRows(run.files || [])
      if (tab === "scan" && run.cleanup_batch?.items?.length) return operationRows(run.cleanup_batch.items, "scan_cleanup")
      if (run.items) return operationRows(run.items, run.operation_type)
      return `<div class="mrm-empty">Детальные строки для этого запуска не сохранены.</div>`
    }

    function historyRunCard(tab, run, { live = false } = {}) {
      const runId = resultRunId(run)
      return `
        <article class="mrm-run-card">
          <div class="mrm-run-head">
            <div>
              <strong>${h(live ? "Текущий предпросмотр" : operationRunTitle(run))}</strong>
              <span class="mrm-hint">${h(runId ? `run_id: ${runId}` : "")}</span>
            </div>
            ${run.status ? statusPill(run.status) : ""}
          </div>
          ${summaryRows(operationRunSummaryRows(tab, run))}
          <details>
            <summary>Вывод запуска</summary>
            ${operationRunDetails(tab, run)}
          </details>
        </article>
      `
    }

    function currentLiveRunForTab(tab) {
      if (!state.lastBatch || operationTabForResult(state.lastBatch) !== tab) return null
      return state.lastBatch
    }

    function operationHistoryPanel(tab) {
      const runs = state.operationHistory?.[tab]?.runs || []
      const live = currentLiveRunForTab(tab)
      const runIds = new Set(runs.map(resultRunId))
      const liveHtml = live && !runIds.has(resultRunId(live)) ? historyRunCard(tab, live, { live: true }) : ""
      return `
        <div class="mrm-run-list">
          <div class="mrm-result-head">
            <div><strong>История: ${h(operationTabLabel(tab))}</strong></div>
          </div>
          ${liveHtml}
          ${runs.length ? runs.map((run) => historyRunCard(tab, run)).join("") : `<div class="mrm-empty">Запусков этой операции ещё нет.</div>`}
        </div>
      `
    }

    function operationLog() {
      const log = state.operationHistory?.log || []
      if (!log.length) return `<div class="mrm-empty">Для этого мониторинга ещё нет запусков операций.</div>`
      return `
        <div class="mrm-table-wrap">
          <table class="mrm-table">
            <thead><tr><th>Когда</th><th>Операция</th><th>Кем</th><th>Статус</th><th>run_id</th></tr></thead>
            <tbody>
              ${log
                .map(
                  (item) => `
                    <tr>
                      <td>${h(runDate(item.started_at || item.created_at))}</td>
                      <td>${h(operationLabels[item.operation_type] || operationTabLabel(item.tab))}</td>
                      <td>${h(item.created_by || "—")}</td>
                      <td>${statusPill(item.status || "unknown")}</td>
                      <td class="is-wrap">${h(item.run_id || "")}</td>
                    </tr>
                  `,
                )
                .join("")}
            </tbody>
          </table>
        </div>
      `
    }

    function operationActionPanel(tab) {
      if (tab === "folders") {
        return `
          <div class="mrm-op-card">
            <span>Папки организаций</span>
            <strong>Создать папки для всех</strong>
            <p>Для выбранных ОУ будут созданы нужные папки в структуре мониторинга.</p>
            <div class="mrm-actions"><button class="mrm-button" type="button" data-op="folders-preview">Preview</button><button class="mrm-button primary" type="button" data-op="folders-apply">Создать</button></div>
          </div>
        `
      }
      if (tab === "links") {
        return `
          <div class="mrm-op-card">
            <span>Публикация</span>
            <strong>Ссылки для загрузки</strong>
            <p>Создаёт публичные ссылки по выбранному режиму мониторинга и обновляет письма организаций.</p>
            <div class="mrm-actions">
              <button class="mrm-button" type="button" data-op="shares-preview">Preview</button>
              <button class="mrm-button primary" type="button" data-op="shares-apply">Создать ссылки</button>
              <button class="mrm-button danger" type="button" data-op="shares-delete-all">Удалить все ссылки</button>
            </div>
          </div>
        `
      }
      if (tab === "scan") {
        return `
          <div class="mrm-op-card">
            <span>Проверка</span>
            <strong>Очистка от дублей и мусора</strong>
            <p>Проверяет загруженные файлы, переносит лишнее и нормализует имена актуальных файлов.</p>
            <div class="mrm-actions"><button class="mrm-button" type="button" data-op="scan-preview">Проверить файлы</button><button class="mrm-button primary" type="button" data-op="scan-apply">Сохранить результат</button></div>
          </div>
        `
      }
      if (tab === "messages") return messagesCard()
      if (tab === "rename") {
        return `
          <div class="mrm-op-card">
            <span>Массовые операции</span>
            <strong>Переименование</strong>
            <p>Меняет имена загруженных файлов по шаблону с алиасом и номером ОУ.</p>
            <div class="mrm-field">
              <label>Шаблон имени</label>
              <input data-rename-template value="${h(state.renameTemplate)}" placeholder="текст {алиас} {номер} текст">
            </div>
            <div class="mrm-actions">
              <button class="mrm-button" type="button" data-op="rename-preview">Preview</button>
              <button class="mrm-button primary" type="button" data-op="rename-apply">Переименовать файлы</button>
            </div>
          </div>
        `
      }
      return ""
    }

    function operationTabContent(tab, campaign) {
      if (!campaign) return `<div class="mrm-empty">Выберите мониторинг.</div>`
      if (tab === "log") return operationLog()
      return `
        <div class="mrm-operation-layout">
          <div>${operationActionPanel(tab)}</div>
          ${operationHistoryPanel(tab)}
        </div>
      `
    }

    function messagesCard() {
      const check = state.operationState.check_result
      const summary = state.operationState.check_summary || {}
      const batch = state.operationState.message_batch
      if (!check) {
        return `
          <div class="mrm-op-card is-disabled">
            <span>Сообщения</span>
            <strong>Сформировать письма и список не предоставивших</strong>
            <p>Сначала выполните и сохраните проверку файлов.</p>
            <div class="mrm-actions"><button class="mrm-button" type="button" disabled>Сформировать</button></div>
          </div>
        `
      }
      if (batch) {
        return `
          <div class="mrm-op-card">
            <span>Сообщения</span>
            <strong>Сообщения сформированы: ${h(batch.summary_json?.planned || batch.summary_json?.pending || 0)}</strong>
            <p>Источник: проверка от ${h(formatDateTime(check.created_at))}</p>
            <div class="mrm-actions">
              <button class="mrm-button" type="button" data-download="messages.xlsx:${h(batch.id)}">Скачать XLSX</button>
              <button class="mrm-button" type="button" data-download="messages.txt:${h(batch.id)}">Скачать TXT</button>
              <button class="mrm-button" type="button" data-op="messages-open">Открыть письма</button>
              <button class="mrm-button primary" type="button" data-op="messages-send">Подготовить и отправить</button>
            </div>
          </div>
        `
      }
      return `
        <div class="mrm-op-card">
          <span>Сообщения</span>
          <strong>Сформировать письма и список не предоставивших</strong>
          <p>Будут сформированы письма по результату проверки от ${h(formatDateTime(check.created_at))}. ${h(checkSummaryText(summary))}</p>
          <div class="mrm-actions">
            <button class="mrm-button" type="button" data-op="messages-preview">Preview</button>
            <button class="mrm-button primary" type="button" data-op="messages-prepare">Сформировать сообщения</button>
          </div>
        </div>
      `
    }

    function operations() {
      const campaign = state.campaigns.find((item) => item.id === state.selectedCampaignId)
      const activeTab = allowedOperationTabs.includes(state.operationTab) ? state.operationTab : "log"
      return shell(
        `
        <section class="mrm-grid">
          <div class="mrm-panel">
            <div class="mrm-panel-header">
              <h3>${campaign ? h(campaign.title) : "Мониторинг не выбран"}</h3>
              <select data-selected-campaign>
                ${state.campaigns.map((item) => `<option value="${h(item.id)}" ${state.selectedCampaignId === item.id ? "selected" : ""}>${h(item.title)}</option>`).join("")}
              </select>
            </div>
            <div class="mrm-panel-body">
              <div class="mrm-list-tools">
                <div class="mrm-segmented" role="tablist" aria-label="Операции мониторинга">
                  ${operationTabs
                    .map(([id, label]) => `<button class="${activeTab === id ? "is-active" : ""}" type="button" data-operation-tab="${h(id)}">${h(label)}</button>`)
                    .join("")}
                </div>
                <div class="mrm-actions">
                  <button class="mrm-button" type="button" data-download="campaign-links.xlsx">Ссылки XLSX</button>
                  <button class="mrm-button" type="button" data-download="campaign-report.xlsx">Отчёт XLSX</button>
                  <button class="mrm-button" type="button" data-download="campaign-message.txt">Текст</button>
                </div>
              </div>
              <div class="mrm-operation-view">${operationTabContent(activeTab, campaign)}</div>
            </div>
          </div>
        </section>
        `,
        "Операции",
        "Автоматическая подготовка мониторинга и сортировка файлов",
      )
    }

    function render() {
      const map = { dashboard, organizations, campaigns, operations, settings }
      const view = map[state.view] || dashboard
      root.innerHTML = view()
    }

    function jsonFromForm(form) {
      const data = {}
      for (const [key, rawValue] of new FormData(form).entries()) {
        const value = typeof rawValue === "string" ? rawValue.trim() : rawValue
        if (value === "") continue
        if (data[key] === undefined) {
          data[key] = value
        } else if (Array.isArray(data[key])) {
          data[key].push(value)
        } else {
          data[key] = [data[key], value]
        }
      }
      if (data.selected_org_types && !Array.isArray(data.selected_org_types)) data.selected_org_types = [data.selected_org_types]
      if (!data.selected_org_types && form.dataset.form === "campaign") data.selected_org_types = []
      if (form.dataset.form === "campaign" && data.upload_mode !== "organization") data.org_folder_layout = "auto"
      if (data.allowed_extensions) data.allowed_extensions = String(data.allowed_extensions).split(",").map((item) => item.trim()).filter(Boolean)
      if (!data.allowed_extensions && form.dataset.form === "campaign") data.allowed_extensions = []
      if (data.default_extensions) data.default_extensions = String(data.default_extensions).split(",").map((item) => item.trim()).filter(Boolean)
      if (form.dataset.form === "settings") data.collaboration_group = form.elements.collaboration_group?.value.trim() || ""
      if (form.dataset.form === "organization") {
        data.email = form.elements.email.value.trim() || null
        const aliases = form.elements.aliases.value.split(/[;,]/).map((item) => item.trim()).filter(Boolean)
        if (aliases.length) data.aliases = aliases
        if (data.code) data.display_name = data.code
      }
      return data
    }

    async function download(path, name) {
      if (path.includes("campaign") && !state.selectedCampaignId) {
        setNotice("Выберите мониторинг", true)
        return
      }
      const realPath = path
        .replace("campaign-mailing.csv", `campaigns/${state.selectedCampaignId}/mailing.csv`)
        .replace("campaign-mailing.xlsx", `campaigns/${state.selectedCampaignId}/mailing.xlsx`)
        .replace("campaign-links.csv", `campaigns/${state.selectedCampaignId}/links.csv`)
        .replace("campaign-links.xlsx", `campaigns/${state.selectedCampaignId}/links.xlsx`)
        .replace("campaign-report.xlsx", `campaigns/${state.selectedCampaignId}/report.xlsx`)
        .replace("campaign-message.txt", `campaigns/${state.selectedCampaignId}/message.txt`)
        .replace(/^messages\.xlsx:(.+)$/u, "message-batches/$1/xlsx")
        .replace(/^messages\.txt:(.+)$/u, "message-batches/$1/txt")
        .replace("organizations.xlsx", "organizations/export/xlsx")
      const response = await fetch(`${proxyBase}/api/${realPath}`)
      if (!response.ok) {
        let message = `HTTP ${response.status}`
        try {
          message = extractError(await response.json(), message)
        } catch (_error) {}
        throw new Error(message)
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = name || path
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    }

    async function loadGroups({ force = false } = {}) {
      if (state.groupsLoading || (state.groupsLoaded && !force)) return
      state.groupsLoading = true
      try {
        const result = await api("/settings/groups")
        state.groups = result.items || []
        state.groupsLoaded = true
      } finally {
        state.groupsLoading = false
        render()
      }
    }

    async function runOperation(op) {
      if (!state.selectedCampaignId) {
        setNotice("Выберите мониторинг", true)
        return
      }
      const id = state.selectedCampaignId
      if (op === "shares-delete-all" && !window.confirm("Удалить все публичные ссылки этого мониторинга? Письма с этими ссылками будут очищены.")) return
      const withJson = (payload) => ({ body: JSON.stringify(payload) })
      const renamePayload = () => {
        const template = (root.querySelector("[data-rename-template]")?.value || state.renameTemplate || "").trim()
        if (!template) throw new Error("Укажите шаблон имени файла. Например: текст {алиас} {номер} текст.")
        if (!template.includes("{номер}")) throw new Error("Добавьте в шаблон {номер}, чтобы сохранить номер ОУ.")
        if (!template.includes("{алиас}")) throw new Error("Добавьте в шаблон {алиас}, чтобы сохранить алиас ОУ.")
        state.renameTemplate = template
        window.localStorage?.setItem("mrmRenameTemplate", template)
        return { campaign_id: id, name_template: template }
      }
      const operations = {
        "folders-preview": {
          kind: "folders",
          phase: "preview",
          path: `/campaigns/${id}/create-folders/preview`,
          options: {},
        },
        "folders-apply": {
          kind: "folders",
          phase: "apply",
          preview: "folders-preview",
          path: (batchId) => `/campaigns/${id}/create-folders/apply?batch_id=${encodeURIComponent(batchId)}`,
          options: {},
        },
        "shares-preview": {
          kind: "shares",
          phase: "preview",
          path: `/campaigns/${id}/create-shares/preview`,
          options: {},
        },
        "shares-apply": {
          kind: "shares",
          phase: "apply",
          preview: "shares-preview",
          path: (batchId) => `/campaigns/${id}/create-shares/apply?batch_id=${encodeURIComponent(batchId)}`,
          options: {},
        },
        "shares-delete-all": {
          phase: "direct",
          path: `/campaigns/${id}/shares/delete-all`,
          options: {},
        },
        "scan-preview": {
          kind: "scan",
          phase: "direct",
          path: `/campaigns/${id}/scan/preview`,
          options: {},
        },
        "scan-apply": {
          kind: "scan",
          phase: "direct",
          path: `/campaigns/${id}/scan/apply`,
          options: {},
        },
        "messages-preview": {
          kind: "messages",
          phase: "direct",
          path: `/campaigns/${id}/messages/preview`,
          options: {},
        },
        "messages-prepare": {
          kind: "messages",
          phase: "direct",
          path: `/campaigns/${id}/messages/prepare`,
          options: {},
        },
        "messages-send": {
          kind: "messages",
          phase: "direct",
          path: () => `/message-batches/${encodeURIComponent(state.operationState.message_batch?.id || state.lastBatch?.message_batch_id || state.lastBatch?.batch_id || "")}/send`,
          options: {},
        },
        "messages-open": {
          kind: "messages",
          phase: "direct",
          path: () => `/message-batches/${encodeURIComponent(state.operationState.message_batch?.id || state.lastBatch?.message_batch_id || state.lastBatch?.batch_id || "")}`,
          options: {},
        },
        "rename-preview": {
          kind: "rename",
          phase: "preview",
          path: "/bulk/rename/preview",
          options: () => withJson(renamePayload()),
        },
        "rename-apply": {
          kind: "rename",
          phase: "apply",
          preview: "rename-preview",
          path: "/bulk/rename/apply",
          options: (batchId) => withJson({ ...renamePayload(), batch_id: batchId }),
        },
      }
      const currentMessageBatch = () => {
        if (state.lastBatch?.operation_type === "messages") return state.lastBatch
        return state.operationState.message_batch || null
      }
      const finishOperationView = async () => {
        await loadBase()
        state.view = "operations"
        persistUiState()
        render()
      }
      if (op === "messages-open") {
        const batchId = messageBatchId(currentMessageBatch())
        state.lastBatch = batchId
          ? await api(`/message-batches/${encodeURIComponent(batchId)}`, { method: "POST" })
          : await api(`/campaigns/${id}/messages/preview`, { method: "POST" })
        state.operationTab = "messages"
        setNotice("Готово")
        await finishOperationView()
        return
      }
      if (op === "messages-send") {
        let batch = currentMessageBatch()
        const completedStatuses = ["completed", "completed_with_errors", "failed", "cancelled"]
        if (!messageBatchId(batch) || completedStatuses.includes(batch.status)) {
          state.lastBatch = await api(`/campaigns/${id}/messages/prepare-send`, { method: "POST" })
          state.operationTab = "messages"
          setNotice((state.lastBatch.summary?.pending || 0) > 0 ? "Отправка запущена" : "Нет писем, готовых к отправке", (state.lastBatch.summary?.pending || 0) <= 0)
          await finishOperationView()
          return
        }
        if ((batch.summary?.pending || 0) <= 0) {
          state.lastBatch = batch
          state.operationTab = "messages"
          setNotice("Нет писем, готовых к отправке", true)
          await finishOperationView()
          return
        }
        state.lastBatch = await api(`/message-batches/${encodeURIComponent(messageBatchId(batch))}/send`, { method: "POST" })
        state.operationTab = "messages"
        setNotice("Отправка запущена")
        await finishOperationView()
        return
      }
      const runConfigured = async (operation) => {
        const path = typeof operation.path === "function" ? operation.path(state.operationBatches[operation.kind]) : operation.path
        const options = typeof operation.options === "function" ? operation.options(state.operationBatches[operation.kind]) : operation.options
        const result = await api(path, { method: "POST", ...options })
        if (result?.batch_id && operation.kind) state.operationBatches[operation.kind] = result.batch_id
        state.lastBatch = result
        return result
      }
      const operation = operations[op]
      if (!operation) return
      if (operation.phase === "apply" && !state.operationBatches[operation.kind]) {
        await runConfigured(operations[operation.preview])
      }
      const result = await runConfigured(operation)
      const resultTab = operationTabForResult(result)
      if (resultTab) state.operationTab = resultTab
      if (operation.phase === "apply") delete state.operationBatches[operation.kind]
      if (op === "shares-delete-all") delete state.operationBatches.shares
      setNotice("Готово")
      await loadBase()
      state.view = "operations"
      persistUiState()
      render()
    }

    async function moveCampaignWithPicker(campaignId) {
      openFolderPicker({
        title: "Выберите папку для перемещения мониторинга",
        onPick: async (value) => {
          const targetPath = selectedPickerPath(value)
          if (!targetPath) throw new Error("Не удалось определить выбранную папку")
          await api(`/campaigns/${encodeURIComponent(campaignId)}/move`, {
            method: "POST",
            body: JSON.stringify({ target_nc_path: targetPath }),
          })
          setNotice("Мониторинг перемещён")
          await loadBase()
          state.view = "campaigns"
          persistUiState()
          render()
        },
      })
    }

    async function archiveCampaign(campaignId) {
      const campaign = state.campaigns.find((item) => item.id === campaignId)
      const title = campaign?.title ? ` «${campaign.title}»` : ""
      if (!window.confirm(`Переместить мониторинг${title} в архив и удалить его публичные ссылки? Файлы останутся в папке архива.`)) return
      await api(`/campaigns/${encodeURIComponent(campaignId)}/archive`, { method: "POST" })
      if (state.selectedCampaignId === campaignId) {
        state.selectedCampaignId = ""
        state.operationBatches = {}
      }
      if (state.editingCampaignId === campaignId) state.editingCampaignId = ""
      setNotice("Мониторинг перемещён в архив")
      await loadBase()
      state.view = "campaigns"
      persistUiState()
      render()
    }

    root.addEventListener("click", async (event) => {
      const extensionPickerElement = event.target.closest("[data-extension-picker]")
      if (!extensionPickerElement) {
        closeExtensionPickers()
      }
      const extensionRemove = event.target.closest("[data-extension-remove]")
      if (extensionRemove && extensionPickerElement) {
        const value = extensionRemove.dataset.extensionRemove
        setExtensionValues(extensionPickerElement, selectedExtensionValues(extensionPickerElement).filter((item) => item !== value))
        toggleExtensionPicker(extensionPickerElement, true)
        return
      }
      const extensionOption = event.target.closest("[data-extension-option]")
      if (extensionOption && extensionPickerElement) {
        const value = extensionOption.dataset.extensionOption
        const selected = selectedExtensionValues(extensionPickerElement)
        const next = selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]
        setExtensionValues(extensionPickerElement, next)
        toggleExtensionPicker(extensionPickerElement, true)
        return
      }
      if (event.target.closest("[data-extension-control]") && extensionPickerElement) {
        closeExtensionPickers(extensionPickerElement)
        toggleExtensionPicker(extensionPickerElement)
        return
      }
      const target = event.target.closest("button")
      if (!target) return
      try {
        if (target.dataset.view) {
          state.view = target.dataset.view
          persistUiState()
          render()
        } else if (target.dataset.selectCampaign) {
          const title = campaignTitle(target.dataset.selectCampaign)
          if (state.selectedCampaignId !== target.dataset.selectCampaign) state.operationBatches = {}
          state.selectedCampaignId = target.dataset.selectCampaign
          state.view = "operations"
          persistUiState()
          await loadBase()
          setNotice(`Загружен ${title} мониторинг`)
        } else if (target.dataset.editOrganization) {
          state.editingOrganizationId = target.dataset.editOrganization
          state.view = "organizations"
          persistUiState()
          render()
        } else if (target.dataset.cancelOrganization !== undefined) {
          state.editingOrganizationId = ""
          render()
        } else if (target.dataset.editCampaign) {
          state.editingCampaignId = target.dataset.editCampaign
          state.view = "campaigns"
          persistUiState()
          render()
          setNotice(`Мониторинг ${campaignTitle(target.dataset.editCampaign)} загружен на редактирование`)
        } else if (target.dataset.moveCampaign) {
          await moveCampaignWithPicker(target.dataset.moveCampaign)
        } else if (target.dataset.archiveCampaign) {
          await archiveCampaign(target.dataset.archiveCampaign)
        } else if (target.dataset.cancelCampaign !== undefined) {
          const title = campaignTitle(state.editingCampaignId)
          state.editingCampaignId = ""
          render()
          setNotice(`Изменения для мониторинга ${title} отменены`, true)
        } else if (target.dataset.orgFilter) {
          state.organizationFilter = target.dataset.orgFilter
          render()
        } else if (target.dataset.operationTab) {
          state.operationTab = target.dataset.operationTab
          persistUiState()
          render()
        } else if (target.dataset.deleteOrganization) {
          if (!window.confirm("Удалить запись из общего справочника?")) return
          await api(`/organizations/${encodeURIComponent(target.dataset.deleteOrganization)}`, { method: "DELETE" })
          if (state.editingOrganizationId === target.dataset.deleteOrganization) state.editingOrganizationId = ""
          setNotice("Запись удалена")
          await loadBase()
        } else if (target.dataset.pickFolder !== undefined) {
          const input = target.closest(".mrm-input-action")?.querySelector("input")
          openFolderPicker({ onPick: (value) => setPickedFolder(value, input) })
        } else if (target.dataset.testWebdav !== undefined) {
          const result = await api("/settings/test-webdav", { method: "POST" })
          const mode = result.mode === "team_folder"
            ? "Team Folder"
            : result.mode === "group_share_fallback"
              ? "fallback с группой"
              : result.mode === "received_share"
                ? "общая папка"
                : "папка владельца"
          setNotice(`Корень доступен: ${mode}`)
        } else if (target.dataset.testMail !== undefined) {
          const result = await api("/settings/test-mail", { method: "POST" })
          setNotice(`Тестовое письмо отправлено: ${result.email}`)
        } else if (target.dataset.loadGroups !== undefined) {
          await loadGroups({ force: true })
          setNotice("Список групп обновлён")
        } else if (target.dataset.download) {
          await download(target.dataset.download)
        } else if (target.dataset.op) {
          await runOperation(target.dataset.op)
        }
      } catch (error) {
        setNotice(error.message, true)
      }
    })

    root.addEventListener("change", async (event) => {
      if (event.target.matches("[data-selected-campaign]")) {
        if (state.selectedCampaignId !== event.target.value) state.operationBatches = {}
        state.selectedCampaignId = event.target.value
        persistUiState()
        await loadBase()
        setNotice(`Загружен ${campaignTitle(event.target.value)} мониторинг`)
      }
      if (event.target.matches("[data-org-type-select]")) {
        const input = event.target.closest("form")?.querySelector("[data-org-aliases]")
        if (input && (!input.value.trim() || isDefaultAliasText(input.value))) input.value = aliasText(defaultAliasesForType(event.target.value))
      }
      if (event.target.matches('select[name="upload_mode"]')) {
        syncCampaignStructureControls(event.target.closest("form"))
      }
      if (event.target.matches("[data-import-organizations]") && event.target.files[0]) {
        const file = event.target.files[0]
        try {
          const result = await api("/organizations/import-json?apply=true&allow_update=true", {
            method: "POST",
            body: JSON.stringify({
              filename: file.name,
              content_base64: arrayBufferToBase64(await file.arrayBuffer()),
            }),
          })
          if (result.apply === false) {
            throw new Error(result.message || "Импорт не выполнен: проверьте файл и повторы в колонках «Тип ОУ» и «Номер».")
          }
          state.lastBatch = result
          setNotice(`Импорт завершён: создано ${result.created || 0}, обновлено ${result.updated || 0}`)
          await loadBase()
        } catch (error) {
          setNotice(error.message, true)
        } finally {
          event.target.value = ""
        }
      }
    })

    root.addEventListener("pointerdown", (event) => {
      if (event.target.matches("[data-group-select]")) {
        void loadGroups().catch((error) => setNotice(error.message, true))
      }
    })

    root.addEventListener("focusin", (event) => {
      if (event.target.matches("[data-group-select]")) {
        void loadGroups().catch((error) => setNotice(error.message, true))
      }
    })

    root.addEventListener("keydown", (event) => {
      const control = event.target.closest("[data-extension-control]")
      if (!control) return
      const picker = control.closest("[data-extension-picker]")
      if (!picker) return
      if (event.key === "Escape") {
        toggleExtensionPicker(picker, false)
        return
      }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault()
        closeExtensionPickers(picker)
        toggleExtensionPicker(picker)
      }
    })

    root.addEventListener("input", (event) => {
      if (event.target.matches("[data-rename-template]")) {
        state.renameTemplate = event.target.value
        state.operationBatches.rename = ""
        window.localStorage?.setItem("mrmRenameTemplate", state.renameTemplate)
      }
    })

    root.addEventListener("submit", async (event) => {
      event.preventDefault()
      const form = event.target
      let resetForm = true
      try {
        if (form.dataset.form === "organization") {
          const payload = jsonFromForm(form)
          if (state.editingOrganizationId) {
            await api(`/organizations/${encodeURIComponent(state.editingOrganizationId)}`, { method: "PUT", body: JSON.stringify(payload) })
            setNotice("Запись обновлена")
            state.editingOrganizationId = ""
          } else {
            await api("/organizations", { method: "POST", body: JSON.stringify(payload) })
            setNotice("Запись добавлена")
          }
        } else if (form.dataset.form === "campaign") {
          const payload = jsonFromForm(form)
          if (state.editingCampaignId) {
            const title = payload.title || campaignTitle(state.editingCampaignId)
            await api(`/campaigns/${encodeURIComponent(state.editingCampaignId)}`, { method: "PUT", body: JSON.stringify(payload) })
            setNotice(`Мониторинг ${title} изменён`)
            state.editingCampaignId = ""
          } else {
            await api("/campaigns", { method: "POST", body: JSON.stringify(payload) })
            setNotice("Мониторинг создан")
          }
        } else if (form.dataset.form === "settings") {
          const payload = jsonFromForm(form)
          const saved = await api("/settings", { method: "PUT", body: JSON.stringify(payload) })
          state.settings = saved
          resetForm = false
          setNotice("Настройки сохранены")
          render()
        }
        if (resetForm) form.reset()
        await loadBase()
      } catch (error) {
        setNotice(error.message, true)
      }
    })

    render()
    loadBase()
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true })
  } else {
    boot()
  }
})()

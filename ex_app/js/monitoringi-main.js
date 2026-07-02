(function () {
  function boot() {
    const state = {
      view: "dashboard",
      organizations: [],
      campaigns: [],
      settings: {},
      selectedCampaignId: "",
      editingOrganizationId: "",
      editingCampaignId: "",
      organizationFilter: "all",
      fileIds: new URLSearchParams(window.location.search).get("fileIds") || "",
      lastBatch: null,
      operationBatches: {},
      renameTemplate: window.localStorage?.getItem("mrmRenameTemplate") || "",
    }

    const organizationTypes = [
      ["school", "Школы"],
      ["kindergarten", "Сады"],
      ["odo", "ОДО"],
      ["udod", "УДОДы"],
    ]

    const views = [
      ["dashboard", "1", "Справка"],
      ["organizations", "2", "Список ОУ"],
      ["campaigns", "3", "Мониторинги"],
      ["operations", "4", "Операции"],
    ]

    const DIRECTORY_MIME_TYPE = "httpd/unix-directory"

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

    async function loadBase() {
      try {
        const [organizations, campaigns, settings] = await Promise.all([
          api("/organizations?active=true"),
          api("/campaigns"),
          api("/settings"),
        ])
        state.organizations = organizations
        state.campaigns = campaigns
        state.settings = settings
        const previousCampaignId = state.selectedCampaignId
        if (!campaigns.some((campaign) => campaign.id === state.selectedCampaignId)) {
          state.selectedCampaignId = campaigns[0]?.id || ""
        }
        if (previousCampaignId !== state.selectedCampaignId) state.operationBatches = {}
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

    function setPickedFolder(value) {
      const path = selectedPickerPath(value)
      if (!path) {
        setNotice("Не удалось определить выбранную папку", true)
        return
      }
      const input = root.querySelector('[name="parent_nc_path"]')
      if (input) input.value = path
    }

    function legacyDialogs() {
      for (const scope of [window.parent, window]) {
        try {
          if (scope?.OC?.dialogs?.filepicker) return scope.OC.dialogs
        } catch (_error) {}
      }
      return null
    }

    function openFolderPicker() {
      const builderFactory = nextcloudFilePickerBuilderFactory()
      if (typeof builderFactory === "function") {
        try {
          const builder = builderFactory("Выберите папку")
          builder.addMimeTypeFilter?.(DIRECTORY_MIME_TYPE)
          builder.setFilter?.((node) => isPickerDirectory(node))
          builder.setCanPick?.((node) => isPickerDirectory(node))
          builder.allowDirectories?.(true)
          builder.setMultiSelect?.(false)
          builder.build().pick().then(setPickedFolder).catch(() => {})
          return
        } catch (error) {
          console.warn("Nextcloud folder picker builder failed:", error)
        }
      }

      const dialogs = legacyDialogs()
      if (dialogs) {
        dialogs.filepicker("Выберите папку", setPickedFolder, false, [DIRECTORY_MIME_TYPE], true)
        return
      }

      setNotice("API выбора папки Nextcloud недоступно", true)
    }

    function statusPill(status) {
      const ok = ["active", "uploaded", "accepted", "completed", "links_created", "success", "valid"]
      const warn = ["draft", "preview", "missing", "completed_with_errors", "folders_created", "planned", "skipped", "duplicate", "unknown"]
      const errors = ["failed", "invalid_extension", "invalid_name", "trash"]
      const cls = ok.includes(status) ? "ok" : warn.includes(status) ? "warn" : errors.includes(status) ? "error" : ""
      return `<span class="mrm-pill ${cls}">${h(statusLabels[status] || status)}</span>`
    }

    const operationLabels = {
      create_folders: "Создать папки для всех",
      create_shares: "Ссылки для загрузки",
      scan: "Очистка от дублей и мусора",
      rename: "Переименование по шаблону",
      move: "Перемещение ошибочных файлов",
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
      public_upload_link: "Ссылка для загрузки файлов",
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
      shares_to_create: "Ссылок создать",
      existing_links: "Ссылок уже есть",
      organizations_without_email: "Организаций без email",
      planned: "Запланировано",
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
      const rows = result.items ? operationRows(result.items, result.operation_type) : fileRows(result.files || [])
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
      const latest = state.campaigns.slice(0, 8)
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
            <div class="mrm-panel-header"><h3>Последние мониторинги</h3></div>
            <div class="mrm-table-wrap">
              <table class="mrm-table">
                <thead><tr><th>Название</th><th>Папка</th><th>Статус</th></tr></thead>
                <tbody>
                  ${latest
                    .map(
                      (item) => `<tr><td>${h(item.title)}</td><td class="is-wrap">${h(item.campaign_folder_path)}</td><td>${statusPill(item.status)}</td></tr>`,
                    )
                    .join("") || `<tr><td colspan="3">Нет мониторингов</td></tr>`}
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
                <select name="type" required>${orgTypeOptions(editing?.type || "school")}</select>
              </div>
              <div class="mrm-field">
                <label>Номер *</label>
                <input name="code" value="${h(editing?.code)}" required>
              </div>
              <div class="mrm-field">
                <label>Email</label>
                <input name="email" type="email" value="${h(editing?.email)}">
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
                <p class="mrm-hint">Колонки для импорта: Тип ОУ, Номер, Email.</p>
              </div>
            </div>
            <div class="mrm-table-wrap">
              <table class="mrm-table">
                <thead><tr><th>Тип ОУ</th><th>Номер</th><th>Email</th><th></th></tr></thead>
                <tbody>
                  ${organizationRows(visibleOrganizations) || `<tr><td colspan="4">Записей нет</td></tr>`}
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

    function campaigns() {
      const editing = state.campaigns.find((item) => item.id === state.editingCampaignId)
      const selectedTypes = editing?.selected_org_types || ["school"]
      const allowedExtensions = (editing?.allowed_extensions || ["pdf", "docx", "xlsx"]).join(",")
      return shell(
        `
        <section class="mrm-grid">
          <form class="mrm-form" data-form="campaign">
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
              <div class="mrm-field"><label>Расширения</label><input name="allowed_extensions" value="${h(allowedExtensions)}"></div>
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
            <div class="mrm-table-wrap">
              <table class="mrm-table">
                <thead><tr><th>Название</th><th>Папка</th><th>Типы ОУ</th><th>Статус</th><th></th></tr></thead>
                <tbody>
                  ${state.campaigns
                    .map(
                      (item) => `
                        <tr>
                          <td>${h(item.title)}</td>
                          <td class="is-wrap">${h(item.campaign_folder_path)}</td>
                          <td>${h((item.selected_org_types || []).map(typeLabel).join(", "))}</td>
                          <td>${statusPill(item.status)}</td>
                          <td class="mrm-row-actions">
                            <button class="mrm-button compact" type="button" data-edit-campaign="${h(item.id)}">Изменить</button>
                            <button class="mrm-button compact" type="button" data-select-campaign="${h(item.id)}">Выбрать</button>
                          </td>
                        </tr>
                      `,
                    )
                    .join("") || `<tr><td colspan="5">Нет мониторингов</td></tr>`}
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

    function operations() {
      const campaign = state.campaigns.find((item) => item.id === state.selectedCampaignId)
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
              <div class="mrm-operation-grid">
                <div class="mrm-op-card">
                  <span>Папки организаций</span>
                  <strong>Создать папки для всех</strong>
                  <p>Для всех выбранных ОУ будет создана папка с номером в корне папки мониторинга. Если выбрано несколько типов ОУ - они будут разбиты на группы.</p>
                  <div class="mrm-actions"><button class="mrm-button" type="button" data-op="folders-preview">Preview</button><button class="mrm-button primary" type="button" data-op="folders-apply">Создать</button></div>
                </div>
                <div class="mrm-op-card">
                  <span>Публикация</span>
                  <strong>Ссылки для загрузки</strong>
                  <p>Создаёт для каждой папки ОУ публичную upload-ссылку и готовит таблицу с их перечислением. Для работы требуется создать папки в первой операции.</p>
                  <div class="mrm-actions"><button class="mrm-button" type="button" data-op="shares-preview">Preview</button><button class="mrm-button primary" type="button" data-op="shares-apply">Создать ссылки</button></div>
                </div>
                <div class="mrm-op-card">
                  <span>Проверка</span>
                  <strong>Очистка от дублей и мусора</strong>
                  <p>Переносит невалидные файлы в мусор и более старые копии в дубли, переименовывает актуальные файлы.</p>
                  <div class="mrm-actions"><button class="mrm-button" type="button" data-op="scan-preview">Проверить файлы</button><button class="mrm-button primary" type="button" data-op="scan-apply">Сохранить результат</button></div>
                </div>
                <div class="mrm-op-card">
                  <span>Массовые операции</span>
                  <strong>Переименование</strong>
                  <p>Меняйте массово имена для загруженных файлов по заданному вами шаблону.</p>
                  <div class="mrm-field">
                    <label>Шаблон имени</label>
                    <input data-rename-template value="${h(state.renameTemplate)}" placeholder="ГБДОУ {номер} отчёт">
                  </div>
                  <div class="mrm-actions">
                    <button class="mrm-button" type="button" data-op="rename-preview">Preview</button>
                    <button class="mrm-button primary" type="button" data-op="rename-apply">Переименовать файлы</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div class="mrm-panel">
            <div class="mrm-panel-header">
              <h3>Результат последней операции</h3>
              <div class="mrm-actions">
                <button class="mrm-button" type="button" data-download="campaign-links.xlsx">Ссылки XLSX</button>
                <button class="mrm-button" type="button" data-download="campaign-report.xlsx">Отчёт XLSX</button>
                <button class="mrm-button" type="button" data-download="campaign-message.txt">Текст</button>
              </div>
            </div>
            <div class="mrm-panel-body">${operationResult()}</div>
          </div>
        </section>
        `,
        "Операции",
        "Автоматическая подготовка мониторинга и сортировка файлов",
      )
    }

    function render() {
      const map = { dashboard, organizations, campaigns, operations }
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
      if (data.allowed_extensions) data.allowed_extensions = String(data.allowed_extensions).split(",").map((item) => item.trim()).filter(Boolean)
      if (data.default_extensions) data.default_extensions = String(data.default_extensions).split(",").map((item) => item.trim()).filter(Boolean)
      if (form.dataset.form === "organization") {
        data.email = form.elements.email.value.trim() || null
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

    async function runOperation(op) {
      if (!state.selectedCampaignId) {
        setNotice("Выберите мониторинг", true)
        return
      }
      const id = state.selectedCampaignId
      const withJson = (payload) => ({ body: JSON.stringify(payload) })
      const renamePayload = () => {
        const template = (root.querySelector("[data-rename-template]")?.value || state.renameTemplate || "").trim()
        if (!template) throw new Error("Укажите шаблон имени файла. Например: ГБДОУ {номер} отчёт.")
        if (!template.includes("{номер}")) throw new Error("Добавьте в шаблон {номер}, чтобы сохранить номер ОУ.")
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
          options: { body: JSON.stringify({ public_upload: true }) },
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
      await runConfigured(operation)
      if (operation.phase === "apply") delete state.operationBatches[operation.kind]
      setNotice("Готово")
      await loadBase()
      state.view = "operations"
      render()
    }

    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button")
      if (!target) return
      try {
        if (target.dataset.view) {
          state.view = target.dataset.view
          render()
        } else if (target.dataset.selectCampaign) {
          if (state.selectedCampaignId !== target.dataset.selectCampaign) state.operationBatches = {}
          state.selectedCampaignId = target.dataset.selectCampaign
          state.view = "operations"
          render()
        } else if (target.dataset.editOrganization) {
          state.editingOrganizationId = target.dataset.editOrganization
          state.view = "organizations"
          render()
        } else if (target.dataset.cancelOrganization !== undefined) {
          state.editingOrganizationId = ""
          render()
        } else if (target.dataset.editCampaign) {
          state.editingCampaignId = target.dataset.editCampaign
          state.view = "campaigns"
          render()
        } else if (target.dataset.cancelCampaign !== undefined) {
          state.editingCampaignId = ""
          render()
        } else if (target.dataset.orgFilter) {
          state.organizationFilter = target.dataset.orgFilter
          render()
        } else if (target.dataset.deleteOrganization) {
          if (!window.confirm("Удалить запись из общего справочника?")) return
          await api(`/organizations/${encodeURIComponent(target.dataset.deleteOrganization)}`, { method: "DELETE" })
          if (state.editingOrganizationId === target.dataset.deleteOrganization) state.editingOrganizationId = ""
          setNotice("Запись удалена")
          await loadBase()
        } else if (target.dataset.pickFolder !== undefined) {
          openFolderPicker()
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
        render()
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
            await api(`/campaigns/${encodeURIComponent(state.editingCampaignId)}`, { method: "PUT", body: JSON.stringify(payload) })
            setNotice("Мониторинг обновлён")
            state.editingCampaignId = ""
          } else {
            await api("/campaigns", { method: "POST", body: JSON.stringify(payload) })
            setNotice("Мониторинг создан")
          }
        }
        form.reset()
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

(function () {
  const state = {
    view: "dashboard",
    organizations: [],
    campaigns: [],
    settings: {},
    selectedCampaignId: "",
    lastBatch: null,
    notice: "",
    error: "",
  }

  const views = [
    ["dashboard", "⌂", "Обзор"],
    ["organizations", "▦", "Организации"],
    ["campaigns", "◴", "Кампании"],
    ["operations", "⇄", "Операции"],
    ["settings", "⚙", "Настройки"],
  ]

  const root = document.getElementById("app")

  async function api(path, options = {}) {
    const response = await fetch(`api${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    })
    if (!response.ok) {
      let message = `HTTP ${response.status}`
      try {
        const payload = await response.json()
        message = payload.detail?.message || payload.detail || message
      } catch (_error) {}
      throw new Error(message)
    }
    const type = response.headers.get("content-type") || ""
    return type.includes("application/json") ? response.json() : response.blob()
  }

  function setNotice(message, isError = false) {
    state.notice = isError ? "" : message
    state.error = isError ? message : ""
    render()
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
      state.selectedCampaignId = state.selectedCampaignId || campaigns[0]?.id || ""
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

  function statusPill(status) {
    const ok = ["active", "uploaded", "accepted", "completed", "links_created"]
    const warn = ["draft", "preview", "missing", "completed_with_errors"]
    const cls = ok.includes(status) ? "ok" : warn.includes(status) ? "warn" : status === "failed" ? "error" : ""
    return `<span class="mrm-pill ${cls}">${h(status)}</span>`
  }

  function shell(content, title, subtitle, actions = "") {
    return `
      <div class="mrm-shell">
        <aside class="mrm-sidebar">
          <div class="mrm-brand">
            <h1>Monitoring Request Manager</h1>
            <span>Сбор файлов ОО</span>
          </div>
          <nav class="mrm-nav">
            ${views
              .map(
                ([id, icon, label]) => `
                  <button data-view="${id}" class="${state.view === id ? "is-active" : ""}" title="${label}">
                    <span class="mrm-icon">${icon}</span><span>${label}</span>
                  </button>
                `,
              )
              .join("")}
          </nav>
        </aside>
        <main class="mrm-main">
          <div class="mrm-topbar">
            <div><h2>${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ""}</div>
            <div class="mrm-actions">${actions}</div>
          </div>
          ${state.error ? `<div class="mrm-notice error">${h(state.error)}</div>` : ""}
          ${state.notice ? `<div class="mrm-notice">${h(state.notice)}</div>` : ""}
          ${content}
        </main>
      </div>
    `
  }

  function dashboard() {
    const active = state.campaigns.filter((item) => ["active", "links_created", "folders_created"].includes(item.status)).length
    const orgsWithoutEmail = state.organizations.filter((item) => !item.email).length
    const latest = state.campaigns.slice(0, 8)
    return shell(
      `
      <section class="mrm-grid">
        <div class="mrm-stat"><strong>${state.campaigns.length}</strong><span>Кампаний</span></div>
        <div class="mrm-stat"><strong>${active}</strong><span>Активных сборов</span></div>
        <div class="mrm-stat"><strong>${state.organizations.length}</strong><span>Организаций</span></div>
        <div class="mrm-stat"><strong>${orgsWithoutEmail}</strong><span>Без email</span></div>
        <div class="mrm-panel">
          <div class="mrm-panel-header"><h3>Последние кампании</h3></div>
          <div class="mrm-table-wrap">
            <table class="mrm-table">
              <thead><tr><th>Дата</th><th>Название</th><th>Папка</th><th>Статус</th></tr></thead>
              <tbody>
                ${latest
                  .map(
                    (item) => `<tr><td>${h(item.date)}</td><td>${h(item.title)}</td><td class="is-wrap">${h(item.campaign_folder_path)}</td><td>${statusPill(item.status)}</td></tr>`,
                  )
                  .join("") || `<tr><td colspan="4">Нет кампаний</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </section>
      `,
      "Обзор",
      "Контроль кампаний, справочника и последних операций",
      `<button class="mrm-button primary" data-view="campaigns">Создать сбор</button>
       <button class="mrm-button" data-view="organizations">Справочник</button>`,
    )
  }

  function organizations() {
    return shell(
      `
      <section class="mrm-grid">
        <form class="mrm-form" data-form="organization">
          <h3>Организация</h3>
          <div class="mrm-fields">
            <div class="mrm-field"><label>Тип</label><select name="type"><option value="school">school</option><option value="kindergarten">kindergarten</option><option value="odo">odo</option><option value="udod">udod</option><option value="other">other</option></select></div>
            <div class="mrm-field"><label>Префикс</label><input name="prefix" placeholder="ГБОУ"></div>
            <div class="mrm-field"><label>Код</label><input name="code" required></div>
            <div class="mrm-field"><label>Название</label><input name="display_name" required></div>
            <div class="mrm-field"><label>Email</label><input name="email" type="email"></div>
            <div class="mrm-field"><label>Алиасы</label><input name="aliases" placeholder="ГБОУ123, ГБОУ №123"></div>
            <button class="mrm-button primary" type="submit">Сохранить</button>
          </div>
        </form>
        <div class="mrm-panel" style="grid-column: span 8">
          <div class="mrm-panel-header">
            <h3>Справочник</h3>
            <div class="mrm-actions">
              <input type="file" data-import-organizations accept=".csv,.xlsx">
              <button class="mrm-button" data-download="organizations.csv">CSV</button>
              <button class="mrm-button" data-download="organizations.xlsx">XLSX</button>
            </div>
          </div>
          <div class="mrm-table-wrap">
            <table class="mrm-table">
              <thead><tr><th>Тип</th><th>Код</th><th>Название</th><th>Email</th><th>Активна</th></tr></thead>
              <tbody>
                ${state.organizations
                  .map(
                    (item) => `<tr><td>${h(item.type)}</td><td>${h(item.code)}</td><td>${h(item.display_name)}</td><td>${h(item.email)}</td><td>${item.active ? "да" : "нет"}</td></tr>`,
                  )
                  .join("") || `<tr><td colspan="5">Справочник пуст</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </section>
      `,
      "Организации",
      "Единый справочник вместо текстовых списков",
    )
  }

  function campaigns() {
    return shell(
      `
      <section class="mrm-grid">
        <form class="mrm-form" data-form="campaign">
          <h3>Кампания</h3>
          <div class="mrm-fields">
            <div class="mrm-field"><label>Название</label><input name="title" required></div>
            <div class="mrm-field"><label>Дата</label><input name="date" type="date" required></div>
            <div class="mrm-field"><label>Срок сдачи</label><input name="deadline" type="date"></div>
            <div class="mrm-field"><label>Родительская папка</label><input name="parent_nc_path" value="${h(state.settings.root_path || "/Мониторинги")}"></div>
            <div class="mrm-field"><label>Типы организаций</label><input name="selected_org_types" value="school,kindergarten"></div>
            <div class="mrm-field"><label>Расширения</label><input name="allowed_extensions" value="pdf,docx,xlsx"></div>
            <button class="mrm-button primary" type="submit">Создать</button>
          </div>
        </form>
        <div class="mrm-panel" style="grid-column: span 8">
          <div class="mrm-panel-header">
            <h3>Кампании</h3>
            <select data-selected-campaign>
              ${state.campaigns.map((item) => `<option value="${h(item.id)}" ${state.selectedCampaignId === item.id ? "selected" : ""}>${h(item.title)}</option>`).join("")}
            </select>
          </div>
          <div class="mrm-table-wrap">
            <table class="mrm-table">
              <thead><tr><th>Дата</th><th>Название</th><th>Папка</th><th>Статус</th><th></th></tr></thead>
              <tbody>
                ${state.campaigns
                  .map(
                    (item) => `<tr><td>${h(item.date)}</td><td>${h(item.title)}</td><td class="is-wrap">${h(item.campaign_folder_path)}</td><td>${statusPill(item.status)}</td><td><button class="mrm-button" data-select-campaign="${h(item.id)}">Выбрать</button></td></tr>`,
                  )
                  .join("") || `<tr><td colspan="5">Нет кампаний</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
      </section>
      `,
      "Кампании",
      "Папки, ссылки, рассылка и проверка файлов",
    )
  }

  function operations() {
    const campaign = state.campaigns.find((item) => item.id === state.selectedCampaignId)
    return shell(
      `
      <section class="mrm-grid">
        <div class="mrm-panel">
          <div class="mrm-panel-header">
            <h3>${campaign ? h(campaign.title) : "Кампания не выбрана"}</h3>
            <select data-selected-campaign>
              ${state.campaigns.map((item) => `<option value="${h(item.id)}" ${state.selectedCampaignId === item.id ? "selected" : ""}>${h(item.title)}</option>`).join("")}
            </select>
          </div>
          <div class="mrm-panel-body">
            <div class="mrm-actions">
              <button class="mrm-button" data-op="folders-preview">Preview папок</button>
              <button class="mrm-button primary" data-op="folders-apply">Применить папки</button>
              <button class="mrm-button" data-op="shares-preview">Preview ссылок</button>
              <button class="mrm-button primary" data-op="shares-apply">Применить ссылки</button>
              <button class="mrm-button" data-op="scan-preview">Проверить</button>
              <button class="mrm-button primary" data-op="scan-apply">Сохранить проверку</button>
              <button class="mrm-button" data-op="rename-preview">Preview имён</button>
              <button class="mrm-button" data-op="move-preview">Preview перемещения</button>
            </div>
          </div>
        </div>
        <div class="mrm-panel">
          <div class="mrm-panel-header">
            <h3>Последний preview/apply</h3>
            <div class="mrm-actions">
              <button class="mrm-button" data-download="campaign-mailing.csv">Рассылка CSV</button>
              <button class="mrm-button" data-download="campaign-mailing.xlsx">Рассылка XLSX</button>
              <button class="mrm-button" data-download="campaign-report.xlsx">Отчёт XLSX</button>
              <button class="mrm-button" data-download="campaign-message.txt">Текст</button>
            </div>
          </div>
          <pre class="mrm-panel-body" style="overflow:auto; margin:0">${h(JSON.stringify(state.lastBatch || {}, null, 2))}</pre>
        </div>
      </section>
      `,
      "Операции",
      "Массовые действия выполняются только после preview",
    )
  }

  function settings() {
    return shell(
      `
      <section class="mrm-grid">
        <form class="mrm-form" style="grid-column: span 6" data-form="settings">
          <h3>Nextcloud API</h3>
          <div class="mrm-fields">
            <div class="mrm-field"><label>Base URL</label><input name="nc_base_url" value="${h(state.settings.nc_base_url || "")}" placeholder="http://nextcloud"></div>
            <div class="mrm-field"><label>Технический пользователь</label><input name="nc_user" value="${h(state.settings.nc_user || "")}"></div>
            <div class="mrm-field"><label>App password</label><input name="nc_app_password" type="password" placeholder="${state.settings.nc_app_password ? "сохранён" : ""}"></div>
            <div class="mrm-field"><label>Корневая папка</label><input name="root_path" value="${h(state.settings.root_path || "/Мониторинги")}"></div>
            <div class="mrm-field"><label>Расширения</label><input name="default_extensions" value="${h((state.settings.default_extensions || []).join(","))}"></div>
            <button class="mrm-button primary" type="submit">Сохранить</button>
          </div>
        </form>
        <div class="mrm-panel" style="grid-column: span 6">
          <div class="mrm-panel-header"><h3>Проверка подключения</h3></div>
          <div class="mrm-panel-body">
            <div class="mrm-actions">
              <button class="mrm-button" data-test="webdav">WebDAV</button>
              <button class="mrm-button" data-test="share-api">OCS Share API</button>
            </div>
          </div>
        </div>
      </section>
      `,
      "Настройки",
      "WebDAV only, OCS Share API и ограничения корневой папки",
    )
  }

  function render() {
    const map = { dashboard, organizations, campaigns, operations, settings }
    root.innerHTML = map[state.view]()
  }

  function jsonFromForm(form) {
    const data = Object.fromEntries(new FormData(form).entries())
    for (const key of Object.keys(data)) {
      if (typeof data[key] === "string" && data[key].trim() === "") {
        delete data[key]
      }
    }
    if (data.aliases) data.aliases = data.aliases.split(",").map((item) => item.trim()).filter(Boolean)
    if (data.selected_org_types) data.selected_org_types = data.selected_org_types.split(",").map((item) => item.trim()).filter(Boolean)
    if (data.allowed_extensions) data.allowed_extensions = data.allowed_extensions.split(",").map((item) => item.trim()).filter(Boolean)
    if (data.default_extensions) data.default_extensions = data.default_extensions.split(",").map((item) => item.trim()).filter(Boolean)
    return data
  }

  async function download(path, name) {
    if (path.includes("campaign") && !state.selectedCampaignId) {
      setNotice("Выберите кампанию", true)
      return
    }
    const realPath = path
      .replace("campaign-mailing.csv", `campaigns/${state.selectedCampaignId}/mailing.csv`)
      .replace("campaign-mailing.xlsx", `campaigns/${state.selectedCampaignId}/mailing.xlsx`)
      .replace("campaign-report.xlsx", `campaigns/${state.selectedCampaignId}/report.xlsx`)
      .replace("campaign-message.txt", `campaigns/${state.selectedCampaignId}/message.txt`)
      .replace("organizations.csv", "organizations/export.csv")
      .replace("organizations.xlsx", "organizations/export.xlsx")
    const blob = await api(`/${realPath}`, { headers: {} })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = name || path
    link.click()
    URL.revokeObjectURL(url)
  }

  async function runOperation(op) {
    if (!state.selectedCampaignId) {
      setNotice("Выберите кампанию", true)
      return
    }
    const id = state.selectedCampaignId
    const lastBatch = state.lastBatch?.batch_id
    const endpoints = {
      "folders-preview": [`/campaigns/${id}/create-folders/preview`, {}],
      "folders-apply": [`/campaigns/${id}/create-folders/apply?batch_id=${encodeURIComponent(lastBatch || "")}`, {}],
      "shares-preview": [`/campaigns/${id}/create-shares/preview`, {}],
      "shares-apply": [`/campaigns/${id}/create-shares/apply?batch_id=${encodeURIComponent(lastBatch || "")}`, { body: JSON.stringify({ public_upload: true }) }],
      "scan-preview": [`/campaigns/${id}/scan/preview`, {}],
      "scan-apply": [`/campaigns/${id}/scan/apply`, {}],
      "rename-preview": ["/bulk/rename/preview", { body: JSON.stringify({ campaign_id: id }) }],
      "move-preview": ["/bulk/move/preview", { body: JSON.stringify({ campaign_id: id }) }],
    }
    const [path, options] = endpoints[op]
    state.lastBatch = await api(path, { method: "POST", ...options })
    state.notice = "Готово"
    state.error = ""
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
        state.selectedCampaignId = target.dataset.selectCampaign
        state.view = "operations"
        render()
      } else if (target.dataset.download) {
        await download(target.dataset.download)
      } else if (target.dataset.op) {
        await runOperation(target.dataset.op)
      } else if (target.dataset.test) {
        const path = target.dataset.test === "webdav" ? "/settings/test-webdav" : "/settings/test-share-api"
        state.lastBatch = await api(path, { method: "POST" })
        setNotice("Подключение успешно")
      }
    } catch (error) {
      setNotice(error.message, true)
    }
  })

  root.addEventListener("change", async (event) => {
    if (event.target.matches("[data-selected-campaign]")) {
      state.selectedCampaignId = event.target.value
      render()
    }
    if (event.target.matches("[data-import-organizations]") && event.target.files[0]) {
      const form = new FormData()
      form.append("file", event.target.files[0])
      try {
        const response = await fetch("api/organizations/import?apply=true&allow_update=true", { method: "POST", body: form })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        state.lastBatch = await response.json()
        setNotice("Импорт завершён")
        await loadBase()
      } catch (error) {
        setNotice(error.message, true)
      }
    }
  })

  root.addEventListener("submit", async (event) => {
    event.preventDefault()
    const form = event.target
    try {
      if (form.dataset.form === "organization") {
        await api("/organizations", { method: "POST", body: JSON.stringify(jsonFromForm(form)) })
        setNotice("Организация сохранена")
      } else if (form.dataset.form === "campaign") {
        await api("/campaigns", { method: "POST", body: JSON.stringify(jsonFromForm(form)) })
        setNotice("Кампания создана")
      } else if (form.dataset.form === "settings") {
        await api("/settings", { method: "PUT", body: JSON.stringify(jsonFromForm(form)) })
        setNotice("Настройки сохранены")
      }
      form.reset()
      await loadBase()
    } catch (error) {
      setNotice(error.message, true)
    }
  })

  render()
  loadBase()
})()


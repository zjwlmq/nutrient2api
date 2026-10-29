"use strict";

const $ = (s, root = document) => root.querySelector(s);
const app = $("#app"), modal = $("#modal"), toastBox = $("#toast");
let csrf = null, state = null, page = "accounts", search = "", toastTimer;
const paths = {
  key: '<circle cx="15" cy="8" r="5"/><path d="m11 12-8 8v2h4v-3h3v-3l3-3"/><path d="M16 7h.01"/>',
  logs: '<path d="M14 2H5v20h14V7zM14 2v6h5M8 12h8M8 16h6M8 8h2"/>',
  settings: '<path d="m9 3 1-2h4l1 2 3 2 2 1-1 4v4l1 4-2 1-3 2-1 2h-4l-1-2-3-2-2-1 1-4v-4L4 6l2-1z"/><circle cx="12" cy="12" r="3"/>',
  user: '<circle cx="12" cy="7" r="4"/><path d="M4 22v-3a8 8 0 0 1 16 0v3"/>',
  exit: '<path d="M9 3H3v18h6M9 12h12m-4-4 4 4-4 4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
  copy: '<rect x="8" y="7" width="12" height="15" rx="2"/><path d="M16 7V2H3v15h5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  refresh: '<path d="M20 7V2m0 5h-5M4 17v5m0-5h5M4.5 8a8 8 0 0 1 13-4L20 7M4 17l2.5 3a8 8 0 0 0 13-4"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.key}</svg>`;
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const brand = () => '<div class="brand"><span class="brand-mark" aria-hidden="true">N</span><div><div class="brand-name">nutrient2api</div><small>管理控制台</small></div></div>';
const stamp = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12:false}) : "尚未检测";
const number = value => Number(value).toLocaleString("zh-CN");

function toast(message, error = false) {
  clearTimeout(toastTimer);
  toastBox.textContent = message;
  toastBox.classList.toggle("error", error);
  toastBox.hidden = false;
  toastTimer = setTimeout(() => { toastBox.hidden = true; }, 4500);
}

async function api(path, body) {
  const options = {credentials:"same-origin", headers:{}};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type":"application/json", "X-CSRF-Token":csrf || ""};
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch("/admin/api/" + path, options); }
  catch { throw new Error("无法连接服务器，请检查网络后重试"); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error("服务器返回异常，请查看服务日志"); }
  if (!response.ok) {
    if (response.status === 401 && csrf && path !== "login") {
      csrf = null;
      modal.close();
      showAuth(false);
    }
    throw new Error(typeof result.error === "string" ? result.error : result.error?.message || "操作失败，请重试");
  }
  return result;
}

async function busy(button, action, errorBox) {
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (errorBox) errorBox.hidden = true;
  try { await action(); }
  catch (error) {
    if (errorBox && errorBox.isConnected) { errorBox.textContent = error.message; errorBox.hidden = false; }
    else toast(error.message, true);
  } finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

function showAuth(setup) {
  app.innerHTML = `<main class="auth-page"><section class="auth-card">${brand()}
    <h1>${setup ? "初始化管理后台" : "欢迎回来"}</h1>
    <p class="intro">${setup ? "先设置管理员密码，Nutrient Key 可以稍后添加。" : "输入管理员密码，管理你的账户和 API。"}</p>
    <form id="auth-form">
      ${setup ? '<div class="field"><label for="setup-code">初始化码</label><input id="setup-code" name="code" required maxlength="256" autocomplete="off" spellcheck="false"><small>在服务器启动日志中找到 INITIAL SETUP CODE。</small></div>' : ""}
      <div class="field"><label for="password">${setup ? "设置管理员密码" : "管理员密码"}</label><input id="password" name="password" type="password" required ${setup ? 'minlength="12"' : ''} maxlength="256" autocomplete="${setup ? "new-password" : "current-password"}">${setup ? '<small>至少 12 个字符，请妥善保存。</small>' : ""}</div>
      ${setup ? '<div class="field"><label for="confirm-password">确认密码</label><input id="confirm-password" name="confirm" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></div>' : ""}
      <p class="form-error" id="auth-error" role="alert" hidden></p>
      <button class="primary full" type="submit">${setup ? "创建管理员并进入" : "登录控制台"}</button>
    </form>
    <p class="auth-footer">${setup ? '查看初始化码：<br><code>docker compose logs nutrient2api</code>' : '忘记密码？在服务器执行：<br><code>docker compose exec nutrient2api python -m nutrient2api reset-admin</code><br>然后刷新页面，用新的初始化码重新设置。'}</p>
  </section></main>`;
  const form = $("#auth-form");
  form.addEventListener("submit", event => {
    event.preventDefault();
    busy($("button[type=submit]", form), async () => {
      const data = Object.fromEntries(new FormData(form));
      if (setup && data.password !== data.confirm) throw new Error("两次输入的密码不一致");
      const result = await api(setup ? "setup" : "login", data);
      csrf = result.csrf;
      form.reset();
      await openConsole();
    }, $("#auth-error"));
  });
}

async function openConsole() {
  state = await api("state");
  app.innerHTML = `<aside class="sidebar">${brand()}<nav class="nav" aria-label="主导航">
    <button data-page="accounts">${icon("key")}账户管理</button>
    <button data-page="logs">${icon("logs")}调用记录</button>
    <button data-page="settings">${icon("settings")}访问设置</button>
  </nav><div class="sidebar-foot"><div class="admin-label">${icon("user")}管理员</div><button id="logout" aria-label="退出登录">${icon("exit")}退出登录</button></div></aside>
  <main class="workspace" id="content"></main>`;
  document.querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", () => {
    page = button.dataset.page;
    render();
    refresh().catch(error => toast(error.message, true));
  }));
  $("#logout").addEventListener("click", event => busy(event.currentTarget, async () => {
    await api("logout", {});
    csrf = null; state = null;
    showAuth(false);
  }));
  render();
}

async function refresh() {
  state = await api("state");
  if (csrf && $("#content")) render();
}

function accountStatus(account) {
  if (!account.enabled) return ['disabled', '已停用'];
  if (account.cooldown_until > Date.now() / 1000) return ['cooldown', account.last_status === 429 ? '限流冷却' : '认证异常'];
  if (account.last_status === 200) return ['good', '连接正常'];
  if (account.last_status) return ['bad', '检测异常'];
  return ['', '待检测'];
}

function connection() {
  return `<section class="panel connection"><h2>连接你的客户端</h2>
    <div class="connection-row"><label for="base-url">Base URL</label><div class="copy-field"><input id="base-url" readonly value="${esc(location.origin + "/v1")}"><button id="copy-url" aria-label="复制 Base URL">${icon("copy")}</button></div></div>
    <div class="connection-row"><label for="client-token">API Key</label><div class="copy-field"><input id="client-token" readonly type="password" value="hidden-client-token"><button id="copy-token">复制令牌</button></div></div>
    <p>客户端使用访问令牌，上游 Key 仅用于连接 Nutrient。</p></section>`;
}

function bindConnection() {
  $("#copy-url")?.addEventListener("click", event => busy(event.currentTarget, () => copy(location.origin + "/v1", "Base URL")));
  $("#copy-token")?.addEventListener("click", event => busy(event.currentTarget, async () => copy((await api("client-token", {})).token, "访问令牌")));
}

function render() {
  if (!state) return;
  document.querySelectorAll("[data-page]").forEach(button => {
    button.classList.toggle("active", button.dataset.page === page);
    button.setAttribute("aria-current", button.dataset.page === page ? "page" : "false");
  });
  if (page === "logs") return renderLogs();
  if (page === "settings") return renderSettings();
  const ready = state.accounts.filter(a => a.enabled && a.cooldown_until <= Date.now() / 1000).length;
  const success = state.total_requests ? Number((100 * state.total_successes / state.total_requests).toFixed(1)) + "%" : "—";
  $("#content").innerHTML = `<header class="page-heading"><div><h1>账户管理</h1><p class="subtitle">集中管理你的 Nutrient 账户和 API Key。</p></div><button class="primary" id="add-account">${icon("plus")}添加账户</button></header>
    <section class="stats" aria-label="调用概览">${[["账户总数",state.accounts.length],["参与调度",ready],["累计调用",number(state.total_requests)],["成功率",success]].map(([label,value])=>`<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`).join("")}</section>
    <section class="panel"><div class="panel-heading"><h2>账户列表</h2><label class="search">${icon("search")}<input id="search" aria-label="搜索账户" placeholder="搜索账户" value="${esc(search)}"></label></div>
      <div class="table-wrap"><table class="account-table"><thead><tr><th>账户名称</th><th>API Key</th><th>状态</th><th>调用次数</th><th>操作</th></tr></thead><tbody id="accounts-body"></tbody></table></div>
      <p class="note">新添加的账户会参与轮询。认证失败或限流时，会在输出开始前尝试其他账户。</p></section>${connection()}`;
  $("#add-account").addEventListener("click", () => editAccount());
  $("#search").addEventListener("input", event => { search = event.target.value; renderAccounts(); });
  renderAccounts(); bindConnection();
}

function renderAccounts() {
  const accounts = state.accounts.filter(a => a.name.toLowerCase().includes(search.toLowerCase()));
  $("#accounts-body").innerHTML = accounts.length ? accounts.map(account => {
    const [kind, label] = accountStatus(account);
    return `<tr><td class="name" title="${esc(account.name)}">${esc(account.name)}</td><td class="key-mask">${esc(account.key_mask)}</td>
      <td class="account-status"><span class="badge ${kind}" title="最近检测或调用：${esc(stamp(account.last_checked))}${account.last_status ? " · HTTP " + account.last_status : ""}">${label}</span></td><td class="request-count">${number(account.requests)}</td>
      <td class="action-cell"><div class="actions"><button class="link-button" data-test="${account.id}" aria-label="检测${esc(account.name)}">检测</button><button class="link-button" data-edit="${account.id}" aria-label="编辑${esc(account.name)}">编辑</button><button class="switch" role="switch" aria-checked="${account.enabled}" aria-label="启用${esc(account.name)}" data-toggle="${account.id}"></button></div></td></tr>`;
  }).join("") : `<tr><td colspan="5" class="empty"><strong>${search ? "没有找到匹配的账户" : "添加你的第一个账户"}</strong>${search ? "试试其他名称。" : "现在没有 Key 也没关系，准备好后随时回来添加。"}${search ? "" : '<br><button class="secondary" id="empty-add">添加账户</button>'}</td></tr>`;
  $("#empty-add")?.addEventListener("click", () => editAccount());
  $("#accounts-body").querySelectorAll("[data-edit]").forEach(button => button.addEventListener("click", () => editAccount(state.accounts.find(a => a.id === +button.dataset.edit))));
  $("#accounts-body").querySelectorAll("[data-test]").forEach(button => button.addEventListener("click", () => busy(button, async () => {
    const previous = button.textContent; button.textContent = "检测中…";
    try {
      const result = await api(`accounts/${button.dataset.test}/test`, {});
      await refresh(); toast(result.message, !result.ok);
    } finally { button.textContent = previous; }
  })));
  $("#accounts-body").querySelectorAll("[data-toggle]").forEach(button => button.addEventListener("click", () => busy(button, async () => {
    const account = state.accounts.find(a => a.id === +button.dataset.toggle);
    await api(`accounts/${account.id}/edit`, {name:account.name, enabled:!account.enabled});
    await refresh(); toast(account.enabled ? "账户已停用" : "账户已启用");
  })));
}

function showModal(title, content) {
  modal.innerHTML = `<div class="modal-header"><h2 id="modal-title">${esc(title)}</h2><button class="icon-button" id="close-modal" aria-label="关闭">${icon("close")}</button></div><div class="modal-body">${content}</div>`;
  $("#close-modal").addEventListener("click", () => modal.close());
  if (!modal.open) modal.showModal();
  $("input:not([readonly])", modal)?.focus();
}

function editAccount(account) {
  showModal(account ? "编辑账户" : "添加账户", `<p class="modal-intro">每个账户保存一个 Nutrient Key。保存后只显示末四位。</p><form id="account-form">
    <div class="field"><label for="account-name">账户名称</label><input id="account-name" name="name" required maxlength="80" placeholder="例如：主账户、备用账户" value="${esc(account?.name || "")}"></div>
    <div class="field"><label for="account-key">Nutrient API Key</label><input id="account-key" name="api_key" type="password" ${account ? "" : "required"} maxlength="4096" autocomplete="off" spellcheck="false" placeholder="${account ? "留空保留当前 Key" : "粘贴你的 Nutrient Key"}"><small>${account ? "当前：" + esc(account.key_mask) + "。" : ""}需要开通 AI Gateway 权限。</small></div>
    <label class="checkbox"><input type="checkbox" name="enabled" ${!account || account.enabled ? "checked" : ""}>启用此账户，参与轮询调度</label>
    <p class="form-error" id="account-error" role="alert" hidden></p><div class="modal-footer">${account ? '<button type="button" class="link-button delete" id="delete-account">删除账户</button>' : ""}<button type="button" class="secondary" id="cancel">取消</button><button class="primary" type="submit">保存账户</button></div></form>`);
  $("#cancel").addEventListener("click", () => modal.close());
  $("#delete-account")?.addEventListener("click", () => confirmAction("删除账户", `确定删除「${account.name}」？这个 Key 将停止接受新请求，已有调用记录会保留。`, "删除账户", async () => {
    await api(`accounts/${account.id}/delete`, {}); modal.close(); await refresh(); toast("账户已删除");
  }));
  $("#account-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    busy($("button[type=submit]", form), async () => {
      const data = Object.fromEntries(new FormData(form));
      data.api_key = data.api_key.trim(); data.enabled = data.enabled === "on";
      await api(account ? `accounts/${account.id}/edit` : "accounts", data);
      form.reset(); modal.close(); await refresh(); toast("账户已保存");
    }, $("#account-error"));
  });
}

function confirmAction(title, description, label, action) {
  showModal(title, `<p class="modal-intro">${esc(description)}</p><div class="modal-footer"><button class="secondary" id="cancel">取消</button><button class="danger" id="confirm">${esc(label)}</button></div>`);
  $("#cancel").addEventListener("click", () => modal.close());
  $("#confirm").addEventListener("click", event => busy(event.currentTarget, action));
}

async function copy(value, label) {
  try { await navigator.clipboard.writeText(value); toast(label + "已复制"); }
  catch {
    showModal("复制" + label, `<p class="modal-intro">当前浏览器不允许自动复制。请选中下面的内容，手动复制。</p><div class="field"><input id="manual-copy" readonly value="${esc(value)}" aria-label="${esc(label)}"></div><button class="primary full" id="copy-done">完成</button>`);
    $("#manual-copy").select();
    $("#copy-done").addEventListener("click", () => modal.close());
  }
}

function renderLogs() {
  $("#content").innerHTML = `<header class="page-heading"><div><h1>调用记录</h1><p class="subtitle">查看账户的请求结果和响应耗时。</p></div><button class="secondary refresh" id="refresh-logs">${icon("refresh")}刷新</button></header>
    <p class="logs-intro">保留最近 200 次上游调用，不保存提问或回复内容。切换账户后的重试单独计数；连通性检测不计入调用次数。</p>
    <section class="panel"><div class="table-wrap"><table><thead><tr><th>时间</th><th>账户</th><th>模型</th><th>结果</th><th>耗时</th><th>模式</th></tr></thead><tbody>${state.requests.length ? state.requests.map(item => `<tr><td class="time">${esc(stamp(item.created_at))}</td><td>${esc(item.account_name)}</td><td>${esc(item.model)}</td><td><span class="status-code ${item.status >= 300 ? "error" : ""}">${item.status}</span></td><td>${number(item.duration_ms)} ms</td><td>${item.streaming ? "流式" : "普通"}</td></tr>`).join("") : '<tr><td colspan="6" class="empty"><strong>还没有调用记录</strong>在客户端发起请求后，可以在这里查看结果。</td></tr>'}</tbody></table></div></section>`;
  $("#refresh-logs").addEventListener("click", event => busy(event.currentTarget, refresh));
}

function renderSettings() {
  $("#content").innerHTML = `<header class="page-heading"><div><h1>访问设置</h1><p class="subtitle">管理客户端连接信息和管理员密码。</p></div></header><div class="settings-grid">${connection()}
    <section class="panel settings-body"><h2>客户端访问令牌</h2><p>所有客户端使用同一个访问令牌。更换后，原令牌立即失效，需要同步更新客户端配置。</p><div class="settings-actions"><button class="secondary" id="show-token">查看令牌</button><button class="danger" id="rotate-token">更换令牌</button></div></section>
    <section class="panel settings-body"><h2>修改管理员密码</h2><p>修改后，其他已登录的会话将失效。</p><form id="password-form">
      <div class="field"><label for="old-password">当前密码</label><input id="old-password" name="old_password" type="password" required maxlength="256" autocomplete="current-password"></div>
      <div class="field"><label for="new-password">新密码</label><input id="new-password" name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password"><small>至少 12 个字符。</small></div>
      <div class="field"><label for="confirm-new-password">确认新密码</label><input id="confirm-new-password" name="confirm" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></div>
      <p class="form-error" id="password-error" role="alert" hidden></p><button class="primary" type="submit">保存新密码</button></form></section></div>`;
  bindConnection();
  $("#show-token").addEventListener("click", event => busy(event.currentTarget, async () => {
    const {token} = await api("client-token", {});
    showModal("客户端访问令牌", `<p class="modal-intro">将它填入客户端的 API Key 字段。请勿分享给无关人员。</p><div class="field"><input class="mono" readonly aria-label="访问令牌" value="${esc(token)}"></div><button class="primary full" id="modal-copy">复制令牌</button>`);
    $("#modal-copy").addEventListener("click", event => busy(event.currentTarget, () => copy(token, "访问令牌")));
  }));
  $("#rotate-token").addEventListener("click", () => confirmAction("更换访问令牌", "原令牌将立即失效，所有客户端需要重新填写。确定继续？", "确认更换", async () => {
    await api("rotate-token", {confirm:true}); modal.close(); toast("令牌已更换，请复制新令牌并更新客户端");
  }));
  $("#password-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    busy($("button[type=submit]", form), async () => {
      const data = Object.fromEntries(new FormData(form));
      if (data.password !== data.confirm) throw new Error("两次输入的新密码不一致");
      csrf = (await api("password", data)).csrf; form.reset(); toast("密码已更新，其他登录会话已失效");
    }, $("#password-error"));
  });
}

modal.addEventListener("close", () => { modal.innerHTML = ""; });
setInterval(() => {
  if (csrf && !document.hidden && !modal.open && page !== "settings" && !["INPUT", "BUTTON"].includes(document.activeElement?.tagName)) {
    refresh().catch(() => {});
  }
}, 20000);

async function boot() {
  try {
    const status = await api("status");
    csrf = status.csrf;
    if (status.authenticated) await openConsole();
    else showAuth(status.setup_required);
  } catch (error) {
    app.innerHTML = `<main class="auth-page"><section class="auth-card">${brand()}<div class="error-panel"><p>${esc(error.message)}</p><button id="retry" class="secondary">重新连接</button></div></section></main>`;
    $("#retry").addEventListener("click", boot);
  }
}
boot();

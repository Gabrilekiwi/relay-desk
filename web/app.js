"use strict";

const $ = id => document.getElementById(id);
const host = window.chrome?.webview;
const time = new Intl.DateTimeFormat("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"});
const statusText = {signed_in:"已登录", signed_out:"未登录", error:"查询失败"};
let model = {settings:{mode:"system", proxy:""}, accounts:[]};
let running = "";
let device = null;
let ready = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, callback, className) {
  const node = element("button", text, className);
  node.type = "button";
  node.onclick = callback;
  return node;
}

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").hidden = !text;
  $("status").dataset.error = String(error);
}

function show(section) {
  for (const name of ["accounts", "network"]) {
    $(name).hidden = name !== section;
    $(`show-${name}`).setAttribute("aria-pressed", String(name === section));
  }
  $("network-details").textContent = section === "network" ? "返回账号" : "查看网络";
}

function syncButtons() {
  for (const id of ["add-account", "refresh-all", "open-folder", "save-network", "run-diagnostics", "retry-network"])
    $(id).disabled = !ready || !!running;
  $("show-login").hidden = !device;
}

function request(payload) {
  if (!ready || running) return;
  running = payload.action;
  if (running === "network") {
    $("network-summary").textContent = "正在检测当前网络…";
    $("network-summary").dataset.error = "false";
    $("network-checks").hidden = true;
  } else if (running !== "state") setStatus("正在处理…");
  syncButtons();
  host.postMessage("hub:" + JSON.stringify(payload));
}

function settingsForm() {
  const form = $("network-form");
  form.elements.mode.value = model.settings.mode;
  $("proxy").value = model.settings.proxy;
  proxyVisibility();
}

function proxyVisibility() {
  const custom = $("network-form").elements.mode.value === "proxy";
  $("proxy-field").hidden = !custom;
  $("proxy").disabled = !custom;
  $("proxy").required = custom;
}

function displayTime(value) {
  const parsed = value ? new Date(value) : null;
  return parsed && Number.isFinite(parsed.valueOf()) ? time.format(parsed) : "—";
}

function quota(label, remaining, reset) {
  const row = element("div", null, "quota");
  const line = element("div", null, "quota-title");
  line.append(element("span", label), element("strong", remaining == null ? "—" : `${remaining}%`));
  row.append(line);
  if (Number.isFinite(remaining)) {
    const progress = element("progress");
    progress.value = remaining;
    progress.max = 100;
    progress.setAttribute("aria-label", label);
    row.append(progress);
  }
  if (reset) row.append(element("small", `重置 ${displayTime(reset)}`, "reset"));
  return row;
}

function accounts() {
  const cards = model.accounts.map(account => {
    const card = element("article", null, "panel");
    const header = element("header");
    header.append(element("h2", account.name, "account-title"),
      element("span", statusText[account.status] ?? "待查询", "badge"));
    const footer = element("div", null, "account-foot");
    const manage = button("管理", () => manageAccount(account));
    manage.disabled = !!running;
    manage.setAttribute("aria-label", `管理 ${account.name}`);
    footer.append(element("small", account.at ? `上次查询 ${displayTime(account.at)}` : "尚未查询", "hint"), manage);
    card.append(header, element("p", [account.email, account.plan].filter(Boolean).join(" · "), "account-meta"),
      quota("5 小时额度", account.five, account.fiveReset), quota("周额度", account.week, account.weekReset), footer);
    return card;
  });
  $("empty").hidden = cards.length !== 0;
  $("account-list").replaceChildren(...cards);
}

function openDialog(title, ...body) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(...body);
  if (!$("dialog").open) $("dialog").showModal();
}

function accountForm() {
  const form = element("form");
  const label = element("label", "账号名称", "field");
  const name = element("input");
  name.id = "account-name";
  name.type = "text";
  name.required = true;
  name.maxLength = 64;
  name.autocomplete = "off";
  label.append(name);
  const submit = element("button", "添加并登录", "primary");
  submit.type = "submit";
  form.append(label, submit);
  form.onsubmit = event => {
    event.preventDefault();
    if (!name.value.trim()) return;
    $("dialog").close();
    request({action:"add", name:name.value.trim()});
  };
  openDialog("添加账号", form);
  name.focus();
}

function manageAccount(account) {
  const actions = element("div", null, "actions");
  const invoke = action => {
    $("dialog").close();
    request({action, id:account.id});
  };
  actions.append(button("刷新此账号", () => invoke("refresh")),
    button("登录或重新登录", () => invoke("login"), "primary"),
    button("注销并删除", () => confirmRemoval(account), "danger"));
  openDialog(account.name, actions);
}

function confirmRemoval(account) {
  const cancel = button("取消", () => $("dialog").close());
  const actions = element("div", null, "actions");
  actions.append(cancel, button("确认注销并删除", () => {
    $("dialog").close();
    request({action:"remove", id:account.id, confirmed:true});
  }, "danger"));
  openDialog("注销并删除账号", element("p", `将删除「${account.name}」在 Relay Desk 中的本地账号数据。`), actions);
  cancel.focus();
}

function showDevice() {
  if (!device) return;
  const code = element("input", null, "code");
  code.id = "device-code";
  code.type = "text";
  code.value = device.code;
  code.readOnly = true;
  code.setAttribute("aria-label", "一次性设备代码");
  const actions = element("div", null, "actions");
  actions.append(button("打开 OpenAI 登录页", () => host.postMessage("hub:open-login"), "primary"),
                 button("复制代码", () => host.postMessage("hub:copy-code")));
  const note = element("p");
  note.id = "device-note";
  note.setAttribute("role", "status");
  openDialog(`登录 ${device.name}`, element("p", "在浏览器使用 OpenAI 设备登录，再输入下面的一次性代码。"),
             code, actions, note);
}

function diagnostics(data) {
  const details = $("diagnostics");
  const fields = [
    ["Python", data.python],
    ["Codex CLI", data.cli ? `${data.cli} · ${data.cliSupported ? "符合版本要求" : "版本过旧"}` : "尚未找到"],
    ["账号数", data.profileCount],
    ["网络方式", data.route === "proxy" ? "手动代理" : "本机网络"],
    ["代理配置", data.proxyConfigured ? "已检测到" : "未配置"]
  ];
  details.replaceChildren(...fields.flatMap(([name, value]) => [element("dt", name), element("dd", value)]));
  details.hidden = false;
}

function showNetworkReport(data) {
  const routes = {system_network:"本机网络", system_proxy:"已读取本机代理", manual:"手动代理"};
  const states = {reachable:"已响应", http_error:"服务返回", proxy_auth:"代理要求认证",
    proxy_rejected:"代理拒绝连接", proxy_dns:"找不到代理主机", dns:"无法解析服务地址",
    connection:"无法连接", timeout:"连接超时", tls:"TLS 验证失败",
    unavailable:"网络检测工具不可用", failed:"检测未完成"};
  const checks = Object.entries(data.checks);
  const summary = $("network-summary");
  const route = routes[data.route] + (data.proxyPort ? ` · 配置端口 ${data.proxyPort}` : "");
  const passed = checks.every(([, check]) => check.status === "reachable");
  const statuses = checks.map(([, check]) => check.status);
  let message = "请检查当前网络或代理设置，再重新检测。";
  if (statuses.every(status => status === "unavailable")) message = "无法自动检测，可继续使用当前网络。";
  else if (statuses.includes("proxy_auth")) message = "代理要求认证，请检查本机代理设置。";
  else if (statuses.includes("http_error")) message = "服务返回异常状态，请查看检测详情。";
  else if (data.route === "system_network")
    message = "检测未通过。如果代理软件未开启系统代理，可在此手动填写地址。";
  summary.dataset.error = String(!passed);
  summary.textContent = route + " · " + (passed ? "Codex 和登录服务已响应" : message);
  const names = {codex:"Codex 服务域名", login:"设备登录服务"};
  const details = $("network-checks");
  details.replaceChildren(...checks.flatMap(([target, check]) => [element("dt", names[target]),
    element("dd", states[check.status] + (check.http ? `（HTTP ${check.http}）` : ""))]));
  details.hidden = false;
}

function receive({data}) {
  if (data?.kind === "host-ready") {
    ready = true;
    request({action:"state"});
  } else if (data?.kind === "hub-result") {
    const previous = running;
    running = "";
    if (device && $("dialog").open) $("dialog").close();
    device = null;
    if (data.state) model = data.state;
    if (previous === "state" || (previous === "settings" && data.ok)) settingsForm();
    syncButtons();
    accounts();
    if (previous !== "network") setStatus(data.message || model.settingsWarning, !data.ok);
    if (previous === "network" && !data.ok) {
      $("network-summary").textContent = data.message;
      $("network-summary").dataset.error = "true";
    }
    if ((previous === "state" || previous === "settings") && data.ok) request({action:"network"});
  } else if (data?.kind === "device") {
    device = data;
    syncButtons();
    showDevice();
  } else if (data?.kind === "device-note") {
    const note = $("device-note");
    if (note) note.textContent = data.message;
  } else if (data?.kind === "diagnostic") diagnostics(data);
  else if (data?.kind === "network") showNetworkReport(data);
}

$("show-accounts").onclick = () => show("accounts");
$("show-network").onclick = () => show("network");
$("network-details").onclick = () => show($("network").hidden ? "network" : "accounts");
$("open-folder").onclick = () => request({action:"folder"});
$("refresh-all").onclick = () => request({action:"refresh"});
$("add-account").onclick = accountForm;
$("show-login").onclick = showDevice;
$("dialog-close").onclick = () => $("dialog").close();
$("run-diagnostics").onclick = () => request({action:"diagnose"});
$("retry-network").onclick = () => request({action:"network"});
$("network-form").onchange = proxyVisibility;
$("network-form").onsubmit = event => {
  event.preventDefault();
  request({action:"settings", mode:$("network-form").elements.mode.value, proxy:$("proxy").value});
};
syncButtons();
host?.addEventListener("message", receive);

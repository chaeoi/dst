"use strict";
const $ = (s) => document.querySelector(s);
const state = {
  csrf: "",
  tab: "world",
  world: null,
  mods: [],
  worldDirty: false,
  modsDirty: false,
  task: null,
  busy: false,
  logOffset: -1,
  logBusy: false,
  logVersion: 0,
};
function node(tag, text, props = {}) {
  const n = document.createElement(tag);
  if (text !== null) n.textContent = text;
  Object.assign(n, props);
  return n;
}
let toastTimer;
function toast(text, fail = false) {
  const n = $("#toast");
  n.textContent = text;
  n.classList.toggle("fail", fail);
  n.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (n.hidden = true), 5000);
}
function showLogin() {
  state.csrf = "";
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-password").value = "";
}
async function api(path, body, method) {
  const options = {
    method: method || (body === undefined ? "GET" : "POST"),
    credentials: "same-origin",
    headers: {},
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = state.csrf;
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务器返回了无法读取的响应");
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(data.error || "请求失败");
  }
  return data;
}
function dirty(which, value = true) {
  state[which + "Dirty"] = value;
  $("#" + which + "-unsaved").textContent = value ? "有尚未保存的修改" : "";
}
function unsaved() {
  return state.worldDirty || state.modsDirty;
}
function typed(text, original) {
  if (typeof original === "string") return text;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
function display(v) {
  return typeof v === "string" ? v : JSON.stringify(v);
}
function field(key, label, value, change, choices = null, hint = "") {
  const wrap = node("label", null, { className: "field" });
  wrap.append(node("span", label));
  let input;
  if (choices) {
    input = node("select", null);
    const all = [...choices];
    if (!all.some((x) => JSON.stringify(x.data) === JSON.stringify(value)))
      all.unshift({ description: "当前：" + display(value), data: value });
    for (const x of all) {
      input.append(
        node("option", x.description, { value: JSON.stringify(x.data) }),
      );
    }
    input.value = JSON.stringify(value);
    input.addEventListener("change", () => change(JSON.parse(input.value)));
  } else {
    input = node("input", null, { type: "text", value: display(value) ?? "" });
    input.addEventListener("input", () => change(typed(input.value, value)));
  }
  input.dataset.key = key;
  wrap.append(input);
  if (hint) wrap.append(node("small", hint));
  return wrap;
}
async function enter() {
  const session = await api("/api/session");
  state.csrf = session.csrf;
  $("#upload-hint").textContent =
    `支持 ZIP、tar.gz，最大 ${Math.round(session.upload_limit / 1048576)} MB。应包含服务器配置、地面和洞穴存档。`;
  $("#login").hidden = true;
  $("#app").hidden = false;
  await Promise.all([loadWorld(), loadMods(), loadArchives(), refreshStatus()]);
  resetLog();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { password: $("#login-password").value });
    await enter();
  } catch (e) {
    $("#login-error").textContent = e.message;
  } finally {
    button.disabled = false;
  }
});
$("#logout").addEventListener("click", async () => {
  if (unsaved() && !confirm("有未保存的修改，仍要退出吗？")) return;
  try {
    await api("/api/logout", {});
    dirty("world", false);
    dirty("mods", false);
    showLogin();
  } catch (e) {
    toast(e.message, true);
  }
});
document.querySelectorAll("nav button").forEach((button) =>
  button.addEventListener("click", () => {
    state.tab = button.dataset.tab;
    document
      .querySelectorAll("nav button")
      .forEach((x) => x.classList.toggle("active", x === button));
    document
      .querySelectorAll(".tab")
      .forEach((x) => (x.hidden = x.id !== "tab-" + state.tab));
    history.replaceState(null, "", "#" + state.tab);
    if (state.tab === "archives") loadArchives();
    if (state.tab === "logs") pollLog(true);
  }),
);
const phaseNames = {
  starting: "正在启动",
  running: "进程运行中",
  maintenance: "正在维护",
  stopped: "已停止",
  error: "需要处理",
};
async function refreshStatus() {
  if (!state.csrf) return;
  try {
    const s = await api("/api/status");
    state.busy = s.task?.status === "running";
    $("#phase").textContent = phaseNames[s.phase] || s.phase;
    $("#shard-status").textContent =
      `地面 ${s.shards.Master ? "●" : "○"}  洞穴 ${s.shards.Caves ? "●" : "○"}`;
    $("#dirty-notice").hidden = !s.dirty;
    const banner = $("#task-banner");
    banner.hidden = !s.error && !state.busy;
    banner.classList.toggle("fail", !!s.error && !state.busy);
    banner.textContent = state.busy ? s.task.message : s.error;
    document
      .querySelectorAll("[data-maintenance]")
      .forEach((x) => (x.disabled = state.busy));
    if (s.task && s.task.status !== "running" && state.task !== s.task.id) {
      state.task = s.task.id;
      toast(
        s.task.status === "success" ? "操作完成" : s.task.message,
        s.task.status !== "success",
      );
      await loadArchives();
      if (!state.modsDirty) await loadMods();
      if (s.task.action === "restore" && !state.worldDirty) await loadWorld();
    }
  } catch (e) {
    $("#phase").textContent = "连接中断";
  }
}
async function task(action, details = {}) {
  if (state.busy) throw new Error("已有任务正在执行");
  await api("/api/task", { action, ...details });
  await refreshStatus();
}
$("#restart").addEventListener("click", async () => {
  if (unsaved()) {
    toast("请先保存或放弃页面上尚未保存的修改", true);
    return;
  }
  if (!confirm("保存当前世界并重启服务器？")) return;
  try {
    await task("restart");
  } catch (e) {
    toast(e.message, true);
  }
});
const clusterLabels = {
  cluster_name: "服务器名称",
  cluster_description: "服务器描述",
  cluster_password: "房间密码",
  max_players: "最大人数",
  game_mode: "游戏模式",
  pvp: "玩家伤害",
  pause_when_empty: "无人时暂停",
  cluster_language: "服务器语言",
  lan_only_cluster: "仅局域网",
  offline_cluster: "离线模式",
};
const yesNo = [
  { description: "开启", data: "true" },
  { description: "关闭", data: "false" },
];
const worldLabels = {
  world_size: "世界大小",
  day: "昼夜长度",
  autumn: "秋季",
  winter: "冬季",
  spring: "春季",
  summer: "夏季",
  wildfires: "夏季自燃",
  hounds: "猎犬袭击",
  grass: "草",
  berrybush: "浆果丛",
  flint: "燧石",
  rocks: "矿石",
  earthquakes: "地震",
  bats: "蝙蝠",
};
async function loadWorld() {
  try {
    const shard = $("#world-shard").value;
    state.world = await api("/api/world?shard=" + shard);
    const wrap = $("#cluster-fields");
    wrap.replaceChildren();
    for (const [section, entries] of Object.entries(state.world.sections)) {
      for (const [key, value] of Object.entries(entries)) {
        if (!clusterLabels[key]) continue;
        let choices = null;
        if (
          [
            "pvp",
            "pause_when_empty",
            "lan_only_cluster",
            "offline_cluster",
          ].includes(key)
        )
          choices = yesNo;
        if (key === "game_mode")
          choices = [
            { description: "生存", data: "survival" },
            { description: "无尽", data: "endless" },
            { description: "荒野", data: "wilderness" },
            { description: "轻松", data: "relaxed" },
          ];
        const f = field(
          key,
          clusterLabels[key],
          value,
          (v) => {
            entries[key] = String(v);
            dirty("world");
          },
          choices,
        );
        if (key === "cluster_password")
          f.querySelector("input").type = "password";
        wrap.append(f);
      }
    }
    $("#cluster-token").value = "";
    $("#token-state").textContent = state.world.token_configured
      ? "已配置。留空保留原值。"
      : "尚未配置。填写后保存，再重启服务器。";
    renderWorld();
    dirty("world", false);
  } catch (e) {
    toast("读取世界设置失败：" + e.message, true);
  }
}
function renderWorld() {
  const wrap = $("#world-fields");
  wrap.replaceChildren();
  const data =
    state.world.level.overrides || (state.world.level.overrides = {});
  for (const [key, value] of Object.entries(data)) {
    wrap.append(
      field(
        key,
        worldLabels[key] || key,
        value,
        (v) => {
          data[key] = v;
          dirty("world");
        },
        null,
        worldLabels[key] ? key : "",
      ),
    );
  }
  if (!Object.keys(data).length)
    wrap.append(
      node("p", "使用游戏默认世界参数，可在下方增加参数。", {
        className: "subtle",
      }),
    );
}
$("#world-shard").dataset.previous = "Master";
$("#world-shard").addEventListener("change", (e) => {
  if (
    state.worldDirty &&
    !confirm("切换分片将放弃未保存的世界设置，继续吗？")
  ) {
    e.target.value = e.target.dataset.previous;
    return;
  }
  e.target.dataset.previous = e.target.value;
  loadWorld();
});
$("#cluster-token").addEventListener("input", () => dirty("world"));
$("#add-world-field").addEventListener("click", () => {
  const key = $("#new-world-key").value.trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
    toast("参数名称只能包含字母、数字和下划线", true);
    return;
  }
  const map = state.world.level.overrides;
  if (Object.hasOwn(map, key)) {
    toast("参数已经存在", true);
    return;
  }
  const text = $("#new-world-value").value;
  try {
    map[key] = JSON.parse(text);
  } catch {
    map[key] = text;
  }
  dirty("world");
  renderWorld();
  $("#new-world-key").value = "";
  $("#new-world-value").value = "";
});
$("#world-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/world", {
      shard: $("#world-shard").value,
      ...state.world,
      token: $("#cluster-token").value,
    });
    dirty("world", false);
    await loadWorld();
    await refreshStatus();
    toast("世界设置已保存，重启后生效");
  } catch (e) {
    toast(e.message, true);
  }
});
async function loadMods() {
  try {
    const data = await api("/api/mods?shard=" + $("#mods-shard").value);
    state.mods = data.mods;
    renderMods();
    dirty("mods", false);
  } catch (e) {
    toast("读取模组失败：" + e.message, true);
  }
}
function renderMods() {
  const list = $("#mod-list");
  list.replaceChildren();
  if (!state.mods.length) {
    list.append(
      node("div", "还没有服务器模组。输入创意工坊 ID 添加。", {
        className: "card empty",
      }),
    );
    return;
  }
  for (const mod of state.mods) {
    const info = mod.info || { fields: [], status: "missing" };
    const card = node("details", null, { className: "card mod-card" });
    const summary = node("summary", null);
    const title = node("div", info.name || "workshop-" + mod.id, {
      className: "mod-title",
    });
    title.append(
      node(
        "small",
        "workshop-" + mod.id + (info.version ? " · " + info.version : ""),
      ),
    );
    summary.append(
      title,
      node("span", mod.enabled ? "已启用" : "已禁用", {
        className: "badge" + (mod.enabled ? "" : " off"),
      }),
    );
    card.append(summary);
    const body = node("div", null, { className: "mod-body" });
    const controls = node("div", null, { className: "mod-controls" });
    const toggle = node("label", null, { className: "check" });
    const checkbox = node("input", null, {
      type: "checkbox",
      checked: mod.enabled,
    });
    checkbox.addEventListener("change", () => {
      mod.enabled = checkbox.checked;
      dirty("mods");
      summary.querySelector(".badge").textContent = mod.enabled
        ? "已启用"
        : "已禁用";
      summary.querySelector(".badge").classList.toggle("off", !mod.enabled);
    });
    toggle.append(checkbox, document.createTextNode("启用这个模组"));
    const remove = node("button", "移除模组", { className: "danger" });
    remove.addEventListener("click", () => {
      if (!confirm("从此分片移除 " + (info.name || mod.id) + "？保存后生效。"))
        return;
      state.mods = state.mods.filter((x) => x !== mod);
      dirty("mods");
      renderMods();
    });
    controls.append(toggle, remove);
    body.append(controls);
    if (info.status !== "ready") {
      body.append(
        node("p", info.message || "配置尚未读取", { className: "hint" }),
      );
      const retry = node("button", "下载配置", { disabled: state.busy });
      retry.dataset.maintenance = "";
      retry.addEventListener("click", () => downloadMod(mod.id));
      body.append(retry);
    }
    const grid = node("div", null, { className: "field-grid" });
    const known = new Set();
    for (const f of info.fields) {
      known.add(f.name);
      const value = Object.hasOwn(mod.options, f.name)
        ? mod.options[f.name]
        : f.default;
      if (f.options.length === 1 && !f.options[0].description) {
        grid.append(node("div", f.label, { className: "mod-group" }));
        continue;
      }
      grid.append(
        field(
          f.name,
          f.label,
          value,
          (v) => {
            mod.options[f.name] = v;
            dirty("mods");
          },
          f.options.length ? f.options : null,
          f.hover,
        ),
      );
    }
    for (const [key, value] of Object.entries(mod.options)) {
      if (!known.has(key))
        grid.append(
          field(
            key,
            key,
            value,
            (v) => {
              mod.options[key] = v;
              dirty("mods");
            },
            null,
            "存档中的已有配置值",
          ),
        );
    }
    if (
      info.status === "ready" &&
      !info.fields.length &&
      !Object.keys(mod.options).length
    )
      body.append(
        node("p", "这个模组没有开放可配置项。", { className: "subtle" }),
      );
    body.append(grid);
    card.append(body);
    list.append(card);
  }
}
$("#mods-shard").dataset.previous = "Master";
$("#mods-shard").addEventListener("change", (e) => {
  if (state.modsDirty && !confirm("切换分片将放弃未保存的模组设置，继续吗？")) {
    e.target.value = e.target.dataset.previous;
    return;
  }
  e.target.dataset.previous = e.target.value;
  loadMods();
});
async function downloadMod(id) {
  if (state.modsDirty) {
    toast("请先保存模组设置", true);
    return;
  }
  try {
    await task("download", {
      id,
      shard: $("#mods-shard").value,
      both: $("#mods-both").checked,
    });
    $("#new-mod-id").value = "";
  } catch (e) {
    toast(e.message, true);
  }
}
$("#add-mod-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = $("#new-mod-id")
    .value.trim()
    .replace(/^workshop-/, "");
  if (!/^[0-9]{1,20}$/.test(id)) {
    toast("请输入创意工坊数字 ID", true);
    return;
  }
  downloadMod(id);
});
$("#save-mods").addEventListener("click", async () => {
  try {
    await api("/api/mods", {
      shard: $("#mods-shard").value,
      mods: state.mods.map(({ id, enabled, options }) => ({
        id,
        enabled,
        options,
      })),
      both: $("#mods-both").checked,
    });
    dirty("mods", false);
    await loadMods();
    await refreshStatus();
    toast("模组设置已保存，重启后生效");
  } catch (e) {
    toast(e.message, true);
  }
});
function size(bytes) {
  return bytes < 1048576
    ? (bytes / 1024).toFixed(1) + " KB"
    : (bytes / 1048576).toFixed(1) + " MB";
}
async function loadArchives() {
  try {
    const data = await api("/api/archives");
    const wrap = $("#archive-list");
    wrap.replaceChildren();
    if (!data.archives.length) {
      wrap.append(
        node("p", "还没有存档包。创建一次备份，或上传已有存档。", {
          className: "empty",
        }),
      );
      return;
    }
    for (const archive of data.archives) {
      const row = node("div", null, { className: "archive-row" });
      const text = node("div", null);
      text.append(
        node("p", archive.label || "未填写备注", { className: "archive-name" }),
        node(
          "p",
          `${archive.source === "upload" ? "上传" : "备份"} · ${new Date(archive.created * 1000).toLocaleString()} · ${size(archive.size)}`,
          { className: "archive-detail" },
        ),
        node("p", archive.name, { className: "archive-detail" }),
      );
      const actions = node("div", null, { className: "archive-buttons" });
      const link = node("a", "下载", {
        href: "/api/download?name=" + encodeURIComponent(archive.name),
      });
      const restore = node("button", "恢复此存档", { disabled: state.busy });
      restore.dataset.maintenance = "";
      restore.addEventListener("click", async () => {
        if (unsaved()) {
          toast("请先保存或放弃尚未保存的修改", true);
          return;
        }
        if (
          !confirm(
            "恢复会替换当前世界和配置。当前存档不会自动备份；需要保留时请先手动创建备份。继续恢复吗？",
          )
        )
          return;
        try {
          await task("restore", { name: archive.name });
        } catch (e) {
          toast(e.message, true);
        }
      });
      actions.append(link, restore);
      row.append(text, actions);
      wrap.append(row);
    }
  } catch (e) {
    toast("读取存档列表失败：" + e.message, true);
  }
}
$("#refresh-archives").addEventListener("click", loadArchives);
$("#backup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await task("backup", { label: $("#backup-label").value });
    $("#backup-label").value = "";
  } catch (e) {
    toast(e.message, true);
  }
});
$("#upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const file = $("#archive-file").files[0];
  if (!file) return;
  const progress = $("#upload-progress"),
    button = $("#upload-button"),
    label = $("#upload-state");
  button.disabled = true;
  progress.hidden = false;
  progress.value = 0;
  label.textContent = "正在上传";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.setRequestHeader("X-CSRF-Token", state.csrf);
  xhr.setRequestHeader("X-Archive-Label", encodeURIComponent(file.name));
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) progress.value = (e.loaded / e.total) * 100;
    label.textContent =
      progress.value >= 100
        ? "上传完成，正在校验存档…"
        : `正在上传 ${Math.round(progress.value)}%`;
  };
  xhr.onload = () => {
    let data = {};
    try {
      data = JSON.parse(xhr.responseText);
    } catch {}
    button.disabled = false;
    if (xhr.status === 200) {
      label.textContent = "校验通过。可在下方选择恢复；当前世界未改变。";
      $("#archive-file").value = "";
      loadArchives();
      toast("存档已导入");
    } else {
      label.textContent = data.error || "上传失败";
      toast(label.textContent, true);
      if (xhr.status === 401) showLogin();
    }
  };
  xhr.onerror = () => {
    button.disabled = false;
    label.textContent = "上传中断，请检查连接后重试";
  };
  xhr.send(file);
});
function resetLog() {
  state.logOffset = -1;
  state.logVersion++;
  $("#log-view").textContent = "";
  pollLog(true);
}
$("#log-name").addEventListener("change", resetLog);
$("#reload-log").addEventListener("click", resetLog);
async function pollLog(force = false) {
  if (
    !state.csrf ||
    state.logBusy ||
    (!force && (state.tab !== "logs" || !$("#log-follow").checked))
  )
    return;
  state.logBusy = true;
  const version = state.logVersion;
  try {
    const d = await api(
      `/api/logs?name=${$("#log-name").value}&offset=${state.logOffset}`,
    );
    if (version !== state.logVersion) return;
    const view = $("#log-view"),
      stick = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
    if (d.reset) view.textContent = "";
    view.textContent += d.text;
    if (view.textContent.length > 1048576)
      view.textContent = view.textContent.slice(-524288);
    state.logOffset = d.offset;
    if (stick) view.scrollTop = view.scrollHeight;
  } catch {
  } finally {
    state.logBusy = false;
    if (version !== state.logVersion) pollLog(true);
  }
}
$("#open-password").addEventListener("click", () => {
  $("#password-form").reset();
  $("#password-error").textContent = "";
  $("#password-dialog").showModal();
});
$("#close-password").addEventListener("click", () =>
  $("#password-dialog").close(),
);
$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("#new-password").value !== $("#confirm-password").value) {
    $("#password-error").textContent = "两次输入的新密码不一致";
    return;
  }
  try {
    await api("/api/password", {
      current: $("#current-password").value,
      password: $("#new-password").value,
    });
    $("#password-dialog").close();
    showLogin();
    toast("密码已修改，请使用新密码登录");
  } catch (e) {
    $("#password-error").textContent = e.message;
  }
});
window.addEventListener("beforeunload", (e) => {
  if (unsaved()) {
    e.preventDefault();
    e.returnValue = "";
  }
});
setInterval(refreshStatus, 2000);
setInterval(() => pollLog(), 2000);
enter()
  .then(() => {
    const tab = location.hash.slice(1);
    if (["world", "mods", "archives", "logs"].includes(tab))
      $("nav button[data-tab=" + tab + "]").click();
  })
  .catch(showLogin);

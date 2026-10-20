/* astrbot_plugin_lazy_tools —— 工具组管理页（原生 JS，无构建依赖） */
(function () {
  "use strict";

  var bridge =
    window.AstrBotPluginPage ||
    window.AstrBotPluginView ||
    null;
  var state = null;
  var activeTab = "overview";
  var toastTimer = null;
  var toolFilter = { search: "", group: "", showHidden: false };

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function fmtNum(value) {
    var n = Number(value);
    if (!isFinite(n)) return "0";
    return n.toLocaleString("zh-CN");
  }
  function fmtPercent(value) {
    var n = Number(value);
    return isFinite(n) ? n.toFixed(1) + "%" : "?";
  }
  function fmtTime(ts) {
    if (!ts) return "-";
    try {
      var d = new Date(Number(ts) * 1000);
      return d.toLocaleString("zh-CN", { hour12: false });
    } catch (e) {
      return "-";
    }
  }
  function unwrap(res) {
    if (res && typeof res === "object") {
      if (res.success === true) return res.data;
      if (res.success === false) throw new Error(res.error || "请求失败");
    }
    return res;
  }
  async function apiGet(endpoint, params) {
    if (!bridge) throw new Error("Bridge SDK 未就绪");
    return unwrap(await bridge.apiGet(endpoint, params || {}));
  }
  async function apiPost(endpoint, body) {
    if (!bridge) throw new Error("Bridge SDK 未就绪");
    return unwrap(await bridge.apiPost(endpoint, body || {}));
  }
  function toast(message, isError) {
    var el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast" + (isError ? " err" : "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.className = "toast hidden";
    }, 2600);
  }
  function showError(message) {
    var el = $("#error");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
  }
  function hideError() {
    var el = $("#error");
    if (el) el.classList.add("hidden");
  }

  /* ------------------------------------------------------------------ 渲染 */
  function renderChips() {
    var el = $("#status-chips");
    if (!el || !state) return;
    var cfg = state.config || {};
    var stats = state.stats || {};
    var chips = [
      { html: "模式 <strong>" + esc(cfg.mode === "full" ? "full（全量）" : "lazy（按需）") + "</strong>", cls: cfg.mode === "lazy" ? "ok" : "warn" },
      { html: "开关 <strong>" + (cfg.enabled ? "开" : "关") + "</strong>", cls: cfg.enabled ? "" : "warn" },
      { html: "工具 <strong>" + fmtNum(stats.injected_tools || 0) + " / " + fmtNum(stats.full_tools || 0) + "</strong>" },
      { html: "schema <strong>" + fmtNum(stats.injected_tokens || 0) + "</strong> tokens" },
      { html: "省 <strong>" + fmtPercent(stats.saved_percent || 0) + "</strong>" },
      { html: "组 <strong>" + fmtNum((state.groups || []).length) + "</strong>" },
      { html: "会话 <strong>" + fmtNum((state.sessions || []).length) + "</strong>" }
    ];
    el.innerHTML = chips
      .map(function (chip) {
        return '<span class="chip ' + (chip.cls || "") + '">' + chip.html + "</span>";
      })
      .join("");
  }

  function renderStats() {
    var el = $("#stat-cards");
    if (!el || !state) return;
    var stats = state.stats || null;
    var cards = [];
    if (!stats) {
      cards.push({ label: "上次请求", value: "暂无", hint: "发一条会触发 LLM 的消息后出现" });
    } else {
      cards.push({
        label: "注入工具 / 全部",
        value: fmtNum(stats.injected_tools) + " / " + fmtNum(stats.full_tools),
        hint: "省略 " + fmtNum(stats.full_tools - stats.injected_tools) + " 个工具定义",
        cls: "accent"
      });
      cards.push({
        label: "工具 schema tokens",
        value: fmtNum(stats.injected_tokens),
        hint: "改造前 ≈ " + fmtNum(stats.full_tokens),
        cls: "accent"
      });
      cards.push({
        label: "单次节省",
        value: fmtPercent(stats.saved_percent),
        hint: "≈ " + fmtNum(stats.saved_tokens) + " tokens / 请求",
        cls: "ok"
      });
      cards.push({
        label: "上次加载组",
        value: String((stats.active_groups || []).length),
        hint: (stats.active_groups || []).join(", ") || "(无)",
      });
    }
    cards.push({
      label: "覆盖项",
      value: fmtNum(Object.keys((state.overrides || {}).tools || {}).length + Object.keys((state.overrides || {}).groups || {}).length),
      hint: "工具 + 组覆盖条数"
    });
    el.innerHTML = cards
      .map(function (card) {
        return (
          '<div class="stat ' + (card.cls || "") + '">' +
          '<div class="label">' + esc(card.label) + "</div>" +
          '<div class="value">' + esc(card.value) + "</div>" +
          '<div class="hint">' + esc(card.hint || "") + "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  function renderHistory() {
    var el = $("#stats-history");
    if (!el || !state) return;
    var history = state.stats_history || [];
    if (!history.length) {
      el.innerHTML = '<div class="hint-box">还没有请求记录。配置生效后，每次触发 LLM 都会记录一条。</div>';
      return;
    }
    el.innerHTML = history
      .map(function (item) {
        return (
          '<div class="entry">' +
          "<span>" + esc(fmtTime(item.ts)) + "</span>" +
          "<span>" + fmtNum(item.injected_tools) + "/" + fmtNum(item.full_tools) + " 工具</span>" +
          "<span>schema " + fmtNum(item.injected_tokens) + " / " + fmtNum(item.full_tokens) + "</span>" +
          "<span>省 <strong>" + fmtPercent(item.saved_percent) + "</strong></span>" +
          "<span>" + esc((item.active_groups || []).join(", ") || "无组") + "</span>" +
          "</div>"
        );
      })
      .join("");
  }

  function setValue(sel, value) {
    var el = $(sel);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  }
  function getChecked(sel) {
    var el = $(sel);
    return !!(el && el.checked);
  }
  function getValue(sel) {
    var el = $(sel);
    return el ? el.value : "";
  }
  function splitLines(text) {
    return String(text || "")
      .split(/[\n,，;；]+/)
      .map(function (item) { return item.trim(); })
      .filter(Boolean);
  }

  function renderConfig() {
    if (!state) return;
    var cfg = state.config || {};
    setValue("#cfg-enabled", cfg.enabled);
    setValue("#cfg-mode", cfg.mode || "lazy");
    setValue("#cfg-max-groups", cfg.max_active_groups);
    setValue("#cfg-max-tools", cfg.max_active_tools);
    setValue("#cfg-min-tools", cfg.min_tools_for_lazy);
    setValue("#cfg-ttl", cfg.session_ttl_seconds);
    setValue("#cfg-auto-load", cfg.auto_load);
    setValue("#cfg-auto-load-max", cfg.auto_load_max_groups);
    setValue("#cfg-debug", cfg.debug_log);
    setValue("#cfg-pin-groups", (cfg.pin_groups || []).join("\n"));
    setValue("#cfg-pin-tools", (cfg.pin_tools || []).join("\n"));
  }

  function collectConfig() {
    return {
      enabled: getChecked("#cfg-enabled"),
      mode: getValue("#cfg-mode"),
      pin_groups: splitLines(getValue("#cfg-pin-groups")),
      pin_tools: splitLines(getValue("#cfg-pin-tools")),
      max_active_groups: Number(getValue("#cfg-max-groups")) || 0,
      max_active_tools: Number(getValue("#cfg-max-tools")) || 0,
      min_tools_for_lazy: Number(getValue("#cfg-min-tools")) || 0,
      session_ttl_seconds: Number(getValue("#cfg-ttl")) || 0,
      auto_load: getChecked("#cfg-auto-load"),
      auto_load_max_groups: Number(getValue("#cfg-auto-load-max")) || 0,
      debug_log: getChecked("#cfg-debug")
    };
  }

  function renderGroups() {
    var el = $("#groups-list");
    if (!el || !state) return;
    var groups = state.groups || [];
    if (!groups.length) {
      el.innerHTML = '<div class="hint-box">当前没有工具组。可能工具集为空，或全部组被隐藏。</div>';
      return;
    }
    el.innerHTML = groups
      .map(function (group) {
        var overrides = (state.group_overrides || {})[group.id] || {};
        var pinned = (state.pinned_groups || []).indexOf(group.id) >= 0;
        var toolLines = (group.tools || [])
          .map(function (tool) {
            return (
              '<div class="tool-line">' +
              "<code>" + esc(tool.name) + "</code>" +
              '<span class="tool-auto-desc">' + esc(tool.description || tool.auto_description || "") + "</span>" +
              "</div>"
            );
          })
          .join("");
        return (
          '<div class="row" data-group="' + esc(group.id) + '">' +
          '<div class="row-head">' +
          '<span class="row-title"><code>' + esc(group.id) + "</code></span>" +
          '<span class="badge">' + fmtNum(group.tool_count) + " 工具</span>" +
          '<span class="badge muted">' + esc(group.kind || "") + "</span>" +
          (pinned ? '<span class="badge">常驻</span>' : "") +
          '<div class="tag-actions">' +
          '<button class="btn small ghost" data-act="pin">' + (pinned ? "取消常驻" : "设为常驻") + "</button>" +
          '<button class="btn small primary" data-act="save-group">保存</button>' +
          '<button class="btn small ghost" data-act="reset-group">恢复自动</button>' +
          '<button class="btn small danger" data-act="hide-group">隐藏组</button>' +
          "</div>" +
          "</div>" +
          '<div class="row-body">' +
          '<div class="row-grid">' +
          '<label class="field"><span>显示名（留空=自动：' + esc(group.auto_name) + "）</span>" +
          '<input type="text" data-field="name" value="' + esc(overrides.name || "") + '" placeholder="' + esc(group.auto_name) + '" /></label>' +
          '<label class="field"><span>组说明（模型可见；留空=自动）</span>' +
          '<textarea data-field="description" rows="2" placeholder="' + esc(group.auto_description) + '">' + esc(overrides.description || "") + "</textarea></label>" +
          "</div>" +
          '<details><summary class="muted small">成员工具（' + fmtNum(group.tool_count) + "）</summary>" + toolLines + "</details>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  function renderHiddenGroups() {
    var wrap = $("#hidden-groups-wrap");
    var el = $("#hidden-groups-list");
    if (!wrap || !el || !state) return;
    var hidden = state.hidden_groups || [];
    if (!hidden.length) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    el.innerHTML = hidden
      .map(function (groupId) {
        var ov = (state.group_overrides || {})[groupId] || {};
        return (
          '<div class="row" data-hidden-group="' + esc(groupId) + '">' +
          '<div class="row-head">' +
          '<span class="row-title"><code>' + esc(groupId) + "</code></span>" +
          '<span class="badge warn">已隐藏</span>' +
          '<div class="tag-actions">' +
          '<button class="btn small primary" data-act="unhide-group">取消隐藏</button>' +
          '<button class="btn small ghost" data-act="reset-group">恢复自动</button>' +
          "</div></div>" +
          '<div class="row-body"><div class="row-grid">' +
          '<label class="field"><span>显示名覆盖</span><input type="text" data-field="name" value="' + esc(ov.name || "") + '" /></label>' +
          '<label class="field"><span>组说明覆盖</span><textarea data-field="description" rows="2">' + esc(ov.description || "") + "</textarea></label>" +
          "</div></div></div>"
        );
      })
      .join("");
  }

  function groupOptions(selected, groupIds, autoGroup) {
    var ids = (groupIds || []).slice();
    if (selected && ids.indexOf(selected) < 0) ids.push(selected);
    ids.sort();
    var autoLabel = autoGroup ? "（自动归组 = " + autoGroup + "）" : "（自动归组）";
    var html = '<option value="">' + esc(autoLabel) + "</option>";
    ids.forEach(function (gid) {
      html +=
        '<option value="' + esc(gid) + '"' + (gid === selected ? " selected" : "") + ">" +
        esc(gid) +
        "</option>";
    });
    html += '<option value="__custom__">+ 自定义组…</option>';
    return html;
  }

  function renderTools() {
    var el = $("#tools-list");
    var filterSelect = $("#tool-group-filter");
    if (!el || !state) return;
    var groups = state.groups || [];
    var groupIds = groups.map(function (g) { return g.id; });
    if (filterSelect) {
      var current = toolFilter.group;
      filterSelect.innerHTML =
        '<option value="">全部组</option>' +
        groupIds
          .map(function (gid) {
            return '<option value="' + esc(gid) + '">' + esc(gid) + "</option>";
          })
          .join("");
      filterSelect.value = current || "";
    }

    var rows = [];
    var toolOverrides = state.tool_overrides || {};
    groups.forEach(function (group) {
      (group.tools || []).forEach(function (tool) {
        var override = toolOverrides[tool.name] || {};
        rows.push({
          name: tool.name,
          autoGroup: group.id,
          overrideGroup: override.group || "",
          effectiveGroup: override.group || group.id,
          description: override.description || "",
          autoDescription: tool.auto_description || "",
          hidden: false,
          stale: false
        });
      });
    });
    // 隐藏/残留工具
    (state.hidden_tool_info || []).forEach(function (item) {
      var override = toolOverrides[item.name] || {};
      rows.push({
        name: item.name,
        autoGroup: "",
        overrideGroup: override.group || "",
        effectiveGroup: override.group || "",
        description: override.description || "",
        autoDescription: item.auto_description || "",
        hidden: true,
        stale: !!item.stale
      });
    });

    var search = toolFilter.search.toLowerCase();
    var filtered = rows.filter(function (row) {
      if (!toolFilter.showHidden && row.hidden && !row.stale) return false;
      if (toolFilter.group && row.effectiveGroup !== toolFilter.group) return false;
      if (!search) return true;
      return (
        row.name.toLowerCase().indexOf(search) >= 0 ||
        (row.description || "").toLowerCase().indexOf(search) >= 0 ||
        (row.autoDescription || "").toLowerCase().indexOf(search) >= 0
      );
    });

    var header =
      '<div class="hint-box">共 ' + fmtNum(rows.length) + " 个工具，" +
      "当前显示 " + fmtNum(filtered.length) + " 个。工具说明修改只影响模型看到的注入版本。</div>";
    if (!filtered.length) {
      el.innerHTML = header + '<div class="hint-box">没有匹配的工具。</div>';
      return;
    }
    el.innerHTML =
      header +
      filtered
        .map(function (row) {
          var badges = "";
          if (row.hidden) badges += '<span class="badge warn">已隐藏</span>';
          if (row.stale) badges += '<span class="badge warn">已不存在</span>';
          return (
            '<div class="row" data-tool="' + esc(row.name) + '">' +
            '<div class="row-head">' +
            "<code>" + esc(row.name) + "</code>" +
            '<span class="badge muted">' + esc(row.effectiveGroup || "自动") + "</span>" +
            badges +
            "</div>" +
            '<div class="row-body">' +
            '<div class="tool-auto-desc">自动说明：' + esc(row.autoDescription || "(无)") + "</div>" +
            '<div class="row-grid">' +
            '<label class="field"><span>工具说明覆盖（留空=自动）</span>' +
            '<textarea data-field="description" rows="2">' + esc(row.description || "") + "</textarea></label>" +
            '<label class="field"><span>归组</span><select data-field="group">' +
            groupOptions(row.overrideGroup, groupIds.concat(state.hidden_groups || []), row.autoGroup) +
            "</select></label>" +
            "</div>" +
            '<div class="actions">' +
            '<label class="inline-check"><input type="checkbox" data-field="hidden"' +
            (row.hidden ? " checked" : "") + " /> 隐藏此工具</label>" +
            '<button class="btn small primary" data-act="save-tool">保存</button>' +
            '<button class="btn small ghost" data-act="reset-tool">恢复自动</button>' +
            "</div></div></div>"
          );
        })
        .join("");
  }

  function renderSessions() {
    var el = $("#sessions-list");
    if (!el || !state) return;
    var sessions = state.sessions || [];
    if (!sessions.length) {
      el.innerHTML = '<div class="hint-box">还没有会话加载记录。</div>';
      return;
    }
    el.innerHTML = sessions
      .map(function (session) {
        var groups = session.groups || [];
        var tools = session.tools || [];
        return (
          '<div class="row">' +
          '<div class="row-head">' +
          "<code>" + esc(session.umo || "(unknown)") + "</code>" +
          '<span class="muted small">最后使用 ' + esc(fmtTime(session.last_used)) + "</span>" +
          "</div>" +
          '<div class="row-body"><div class="tool-line">' +
          '<span class="muted small">已加载组：</span>' +
          (groups.length
            ? groups.map(function (gid) { return '<span class="badge">' + esc(gid) + "</span>"; }).join("")
            : '<span class="muted small">(无)</span>') +
          (tools.length ? ' <span class="muted small">+ 单工具 ' + fmtNum(tools.length) + "</span>" : "") +
          "</div></div></div>"
        );
      })
      .join("");
  }

  function renderAll() {
    renderChips();
    renderStats();
    renderConfig();
    renderGroups();
    renderHiddenGroups();
    renderTools();
    renderSessions();
    renderHistory();
  }

  /* ------------------------------------------------------------------ 操作 */
  async function loadState() {
    hideError();
    try {
      state = await apiGet("state");
      var loading = $("#loading");
      if (loading) loading.classList.add("hidden");
      renderAll();
    } catch (err) {
      var loading = $("#loading");
      if (loading) loading.classList.add("hidden");
      showError("加载失败：" + (err && err.message ? err.message : err));
    }
  }

  async function saveConfigPatch(patch, successText) {
    var config = await apiPost("config", patch);
    if (state) state.config = config;
    toast(successText || "已保存");
    await loadState();
  }

  function collectGroupPatch(row) {
    var patch = {};
    var name = $('[data-field="name"]', row);
    var desc = $('[data-field="description"]', row);
    if (name) patch.name = name.value.trim();
    if (desc) patch.description = desc.value.trim();
    return patch;
  }

  function collectToolPatch(row) {
    var patch = {};
    var desc = $('[data-field="description"]', row);
    var group = $('[data-field="group"]', row);
    var hidden = $('[data-field="hidden"]', row);
    if (desc) patch.description = desc.value.trim();
    if (group) {
      var value = group.value;
      if (value === "__custom__") {
        var custom = window.prompt("输入自定义组 id（例如 my_tools）：", "");
        value = custom ? custom.trim() : "";
      }
      patch.group = value;
    }
    if (hidden) patch.hidden = !!hidden.checked;
    return patch;
  }

  async function handleGroupAction(act, row, isHidden) {
    var groupId = isHidden
      ? row.getAttribute("data-hidden-group")
      : row.getAttribute("data-group");
    if (!groupId) return;
    if (act === "save-group") {
      var patch = collectGroupPatch(row);
      var payload = { group_updates: {} };
      payload.group_updates[groupId] = patch;
      await apiPost("overrides", payload);
      toast("已保存组 " + groupId);
      await loadState();
      return;
    }
    if (act === "reset-group") {
      var resetPayload = { reset_group_ids: [groupId] };
      await apiPost("overrides", resetPayload);
      toast("已恢复组 " + groupId + " 的自动设置");
      await loadState();
      return;
    }
    if (act === "hide-group") {
      var hidePayload = { group_updates: {} };
      hidePayload.group_updates[groupId] = { hidden: true };
      await apiPost("overrides", hidePayload);
      toast("已隐藏组 " + groupId);
      await loadState();
      return;
    }
    if (act === "unhide-group") {
      var unhidePayload = { group_updates: {} };
      unhidePayload.group_updates[groupId] = { hidden: false };
      await apiPost("overrides", unhidePayload);
      toast("已取消隐藏 " + groupId);
      await loadState();
      return;
    }
    if (act === "pin") {
      var cfg = state.config || {};
      var pins = (cfg.pin_groups || []).slice();
      var index = pins.indexOf(groupId);
      if (index >= 0) pins.splice(index, 1);
      else pins.push(groupId);
      await saveConfigPatch({ pin_groups: pins }, index >= 0 ? "已取消常驻" : "已设为常驻");
    }
  }

  async function handleToolAction(act, row) {
    var name = row.getAttribute("data-tool");
    if (!name) return;
    if (act === "save-tool") {
      var patch = collectToolPatch(row);
      var payload = { tool_updates: {} };
      payload.tool_updates[name] = patch;
      await apiPost("overrides", payload);
      toast("已保存工具 " + name);
      await loadState();
      return;
    }
    if (act === "reset-tool") {
      await apiPost("overrides", { reset_tool_names: [name] });
      toast("已恢复工具 " + name + " 的自动设置");
      await loadState();
    }
  }

  function activateTab(name) {
    activeTab = name;
    $$(".tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.getAttribute("data-tab") === name);
    });
    ["overview", "groups", "tools", "sessions"].forEach(function (panelName) {
      var panel = $("#panel-" + panelName);
      if (panel) panel.classList.toggle("hidden", panelName !== name);
    });
  }

  /* ------------------------------------------------------------------ 事件 */
  function bindStaticEvents() {
    $$(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        activateTab(tab.getAttribute("data-tab"));
      });
    });

    var refresh = $("#btn-refresh");
    if (refresh) refresh.addEventListener("click", function () { loadState(); });

    var save = $("#btn-save-config");
    if (save) {
      save.addEventListener("click", async function () {
        try {
          await saveConfigPatch(collectConfig(), "设置已保存，下一轮请求生效");
        } catch (err) {
          toast("保存失败：" + err.message, true);
        }
      });
    }

    var clearSessions = $("#btn-clear-sessions");
    if (clearSessions) {
      clearSessions.addEventListener("click", async function () {
        if (!window.confirm("确定清空所有会话已加载的工具组记忆？")) return;
        try {
          var data = await apiPost("actions/reset_sessions", {});
          toast("已清空 " + (data && data.cleared ? data.cleared : 0) + " 个会话");
          await loadState();
        } catch (err) {
          toast("清空失败：" + err.message, true);
        }
      });
    }

    var resetOverrides = $("#btn-reset-overrides");
    if (resetOverrides) {
      resetOverrides.addEventListener("click", async function () {
        if (!window.confirm("确定重置全部组/工具覆盖项？此操作不可撤销。")) return;
        try {
          await apiPost("actions/reset_overrides", {});
          toast("已重置全部覆盖项");
          await loadState();
        } catch (err) {
          toast("重置失败：" + err.message, true);
        }
      });
    }

    var exportBtn = $("#btn-export");
    if (exportBtn) {
      exportBtn.addEventListener("click", async function () {
        try {
          var bundle = await apiGet("export");
          var blob = new Blob([JSON.stringify(bundle, null, 2)], {
            type: "application/json"
          });
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = "lazy_tools_config.json";
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
        } catch (err) {
          toast("导出失败：" + err.message, true);
        }
      });
    }

    var importBtn = $("#btn-import");
    var importFile = $("#import-file");
    if (importBtn && importFile) {
      importBtn.addEventListener("click", function () { importFile.click(); });
      importFile.addEventListener("change", function () {
        var file = importFile.files && importFile.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = async function () {
          try {
            var data = JSON.parse(String(reader.result || "{}"));
            await apiPost("import", data);
            toast("导入成功");
            await loadState();
          } catch (err) {
            toast("导入失败：" + (err && err.message ? err.message : err), true);
          } finally {
            importFile.value = "";
          }
        };
        reader.readAsText(file, "utf-8");
      });
    }

    var search = $("#tool-search");
    if (search) {
      search.addEventListener("input", function () {
        toolFilter.search = search.value || "";
        renderTools();
        search.focus();
        search.setSelectionRange(search.value.length, search.value.length);
      });
    }
    var groupFilter = $("#tool-group-filter");
    if (groupFilter) {
      groupFilter.addEventListener("change", function () {
        toolFilter.group = groupFilter.value || "";
        renderTools();
      });
    }
    var showHidden = $("#show-hidden-tools");
    if (showHidden) {
      showHidden.addEventListener("change", function () {
        toolFilter.showHidden = !!showHidden.checked;
        renderTools();
      });
    }

    document.addEventListener("click", async function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("button[data-act]") : null;
      if (!btn) return;
      var act = btn.getAttribute("data-act");
      var toolRow = btn.closest("[data-tool]");
      var groupRow = btn.closest("[data-group]");
      var hiddenGroupRow = btn.closest("[data-hidden-group]");
      try {
        if (toolRow) {
          await handleToolAction(act, toolRow);
          return;
        }
        if (hiddenGroupRow) {
          await handleGroupAction(act, hiddenGroupRow, true);
          return;
        }
        if (groupRow) {
          await handleGroupAction(act, groupRow, false);
        }
      } catch (err) {
        toast("操作失败：" + (err && err.message ? err.message : err), true);
      }
    });
  }

  async function init() {
    if (!bridge) {
      showError("Bridge SDK 未就绪：请从 AstrBot WebUI 的插件页面入口打开本页。");
      return;
    }
    bindStaticEvents();
    activateTab(activeTab);
    await loadState();
  }

  init();
})();

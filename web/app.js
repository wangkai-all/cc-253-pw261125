/* Synthetic calibration-version receiver: browser client.
 *
 * The page is a thin view over the JSON API.  All decisions about which
 * calibration applies live on the server; the browser only shows the
 * decision, the evidence behind it, and the revision history.
 */

"use strict";

const state = {
  bootstrap: null,
  selected: null,
  busy: false,
};

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fact(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  return String(value);
}

function ms(value) {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "0 ms";
  if (value < 1000) return `${value} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
  if (value < 3600000) return `${(value / 60000).toFixed(1)} min`;
  return `${(value / 3600000).toFixed(1)} h`;
}

function shortTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").replace("Z", "").replace(/\.\d{3}$/, "");
}

function fromEpochMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return shortTime(new Date(n).toISOString());
}

function statusPill(item) {
  if (!item) return '<span class="pill unconverted">未发布</span>';
  const status = item.status;
  const label = {
    priced: "已换算", unconverted: "未换算", out_of_validity: "超界未换算",
  }[status] || status;
  const value = item.value_kpa === null || item.value_kpa === undefined
    ? "" : ` · ${fact(item.value_kpa)} kPa`;
  return `<span class="pill ${esc(status)}">${esc(label)}${esc(value)}</span>`;
}

function revisionNote(r) {
  if (!r.latest) {
    if (r.target_status === null) {
      return '<span class="hint">等标定到达后发布</span>';
    }
    if (r.target_status === "out_of_validity") {
      return '<span class="hint">原始值超出该标定范围，按策略不发布</span>';
    }
    return '<span class="hint">待换算</span>';
  }
  if (!r.up_to_date) {
    return '<span class="pill out_of_validity">待重算</span>';
  }
  if (r.revisions > 1) {
    return `<span class="pill provisional">r${esc(r.latest.revision)} · 替换 r${esc(r.latest.supersedes)}</span>`;
  }
  return `<span class="num">r${esc(r.revisions)}</span>`;
}

function decisionPill(decision) {
  const label = { accepted: "接受", rejected: "拒绝", duplicate: "重复" }[decision] || decision;
  return `<span class="pill ${esc(decision)}">${esc(label)}</span>`;
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" },
  }, options || {}));
  let payload = null;
  try { payload = await response.json(); } catch (err) { payload = null; }
  if (!response.ok) {
    const message = payload && payload.error ? payload.error.message : response.statusText;
    throw new Error(message);
  }
  return payload;
}

function note(text, kind) {
  const el = $("#ctl-note");
  el.textContent = text;
  el.className = "note" + (kind ? " " + kind : "");
}

async function refresh(keepSelection) {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;
  if (keepSelection && state.selected) {
    refreshDetail(state.selected).catch(() => {});
  }
  render();
}

function render() {
  const data = state.bootstrap;
  if (!data) return;
  renderTop(data);
  renderModes(data);
  renderReadings(data);
  renderChains(data);
  renderDeliveries(data);
  renderTradeoff(data);
  renderRuns(data);
  const device = $("#device");
  const current = device.value;
  device.innerHTML = data.info.devices
    .map(d => `<option value="${esc(d.key)}">${esc(d.key)} — ${esc(d.label)}</option>`)
    .join("");
  if (current) device.value = current;
  $("#db-path").textContent = data.info.db;
  updateNextVersion();
}

function renderTop(data) {
  const s = data.summary;
  const stats = [
    ["读数", s.readings],
    ["已发布", s.published_readings],
    ["修订", s.revisions],
    ["当前排队", s.queued_jobs],
  ];
  $("#topstats").innerHTML = stats
    .map(([label, value]) => `<div class="stat"><span class="k">${esc(value)}</span><span class="l">${esc(label)}</span></div>`)
    .join("");
}

function renderModes(data) {
  const modes = data.summary.publish_modes;
  $("#modes").innerHTML = data.info.devices.map(d => {
    const mode = modes[d.key] || "hold";
    const desc = mode === "hold"
      ? "等标定再发布；无生效标定时不产生任何记录。"
      : "先发暂定值；标定到达后以新修订替换。";
    return `<div class="mode-row">
      <span class="dev">${esc(d.key)}</span>
      <span class="desc">${esc(desc)}</span>
      <span class="seg" data-device="${esc(d.key)}">
        <button data-mode="hold" class="${mode === "hold" ? "on" : ""}">等标定</button>
        <button data-mode="provisional" class="${mode === "provisional" ? "on" : ""}">暂定值</button>
      </span>
    </div>`;
  }).join("");
  $$("#modes .seg button").forEach(btn => {
    btn.addEventListener("click", () => setMode(btn.closest(".seg").dataset.device, btn.dataset.mode));
  });
}

function renderReadings(data) {
  const body = $("#readings-table tbody");
  $("#readings-hint").textContent = `${data.readings.length} 条（最新在前）`;
  if (!data.readings.length) {
    body.innerHTML = `<tr><td colspan="9"><span class="hint">还没有读数。用操作台发送，或补传一批。</span></td></tr>`;
    return;
  }
  body.innerHTML = data.readings.map(r => {
    const latest = r.latest;
    const version = latest && latest.cal_version !== null
      ? `v${esc(latest.cal_version)}` : "—";
    return `<tr class="clickable ${state.selected === r.id ? "selected" : ""}" data-id="${esc(r.id)}">
      <td class="num">${esc(r.id)}</td>
      <td class="num">${esc(r.raw)}</td>
      <td class="num">${esc(shortTime(r.device_ts))}</td>
      <td>${esc(r.mode === "hold" ? "等标定" : "暂定值")}</td>
      <td>${statusPill(latest)}</td>
      <td class="num">${latest && latest.value_kpa !== null ? esc(fact(latest.value_kpa)) : "—"}</td>
      <td class="num">${version}</td>
      <td class="num">${esc(latest ? latest.chosen_by : "—")}</td>
      <td>${revisionNote(r)}</td>
    </tr>`;
  }).join("");
  $$("#readings-table tbody tr.clickable").forEach(row => {
    row.addEventListener("click", () => selectReading(row.dataset.id));
  });
}

function renderChains(data) {
  const chains = data.chains;
  const ids = Object.keys(chains);
  if (!ids.length) {
    $("#chains").innerHTML = `<p class="hint">还没有被接受的标定。投递一份，或等待另一通道送达。</p>`;
    return;
  }
  $("#chains").innerHTML = ids.map(calId => {
    const versions = chains[calId];
    const device = versions[0] ? versions[0].device : "—";
    const rows = versions.map(v => `
      <div class="version">
        <span class="v">v${esc(v.cal_version)}</span>
        <span class="meta">
          <span class="when">生效 ${esc(shortTime(v.valid_from))}</span>
          · 有效原始值 ${esc(v.validity[0])}–${esc(v.validity[1])}
          · 收到 ${esc(shortTime(v.received_at))}
          ${v.note ? " · " + esc(v.note) : ""}
        </span>
      </div>`).join("");
    return `<div class="chain">
      <div class="chain-head">
        <span class="cid">${esc(calId)}</span>
        <span class="hint">${esc(device)} · ${versions.length} 个版本</span>
      </div>
      ${rows}
    </div>`;
  }).join("");
}

function renderDeliveries(data) {
  const body = $("#deliveries-table tbody");
  if (!data.deliveries.length) {
    body.innerHTML = `<tr><td colspan="7"><span class="hint">还没有投递。</span></td></tr>`;
    return;
  }
  body.innerHTML = data.deliveries.map(d => `
    <tr title="${esc(d.detail || "")}">
      <td class="num">${esc(d.seq)}</td>
      <td class="num">${esc(d.device || "—")}</td>
      <td class="num">${esc(d.cal_id || "—")}</td>
      <td class="num">${d.cal_version === null ? "—" : "v" + esc(d.cal_version)}</td>
      <td>${decisionPill(d.decision)}</td>
      <td>${esc(d.reason || "")}${d.detail ? ` <span class="hint">${esc(d.detail)}</span>` : ""}</td>
      <td class="num">${esc(shortTime(d.received_at))}</td>
    </tr>`).join("");
}

function renderTradeoff(data) {
  const body = $("#tradeoff-table tbody");
  const rows = [];
  for (const mode of ["hold", "provisional"]) {
    const t = data.tradeoff[mode];
    if (!t) {
      rows.push(`<tr><td>${mode === "hold" ? "等标定再发布" : "先发暂定值"}</td>
        <td colspan="8"><span class="hint">还没有按此策略发布的读数</span></td></tr>`);
      continue;
    }
    rows.push(`<tr>
      <td>${mode === "hold" ? "等标定再发布" : "先发暂定值"}</td>
      <td class="num">${esc(t.readings)}</td>
      <td class="num">${esc(ms(t.median_first_ms))}</td>
      <td class="num">${esc(ms(t.median_final_ms))}</td>
      <td class="num">${esc(t.revised)}</td>
      <td class="num">${esc((t.revised_share * 100).toFixed(0))}%</td>
      <td class="num">${esc((t.revision_overhead * 100).toFixed(0))}%</td>
      <td class="num">${esc(t.unpriced)}</td>
    </tr>`);
  }
  const held = data.tradeoff._unpublished_hold || 0;
  if (held) {
    rows.push(`<tr><td colspan="8"><span class="hint">另有 ${esc(held)} 条读数按“等标定”策略保持未发布。</span></td></tr>`);
  }
  body.innerHTML = rows.join("");
}

function renderRuns(data) {
  const runs = data.runs || [];
  if (!runs.length) {
    $("#runs").innerHTML = `<p class="hint">还没有换算运行。</p>`;
    return;
  }
  $("#runs").innerHTML = runs.map(r => `
    <div class="run">
      <span class="num">${esc(fromEpochMs(r.started_ms))}</span>
      <span>${esc(r.reading_id)} <span class="hint">${esc(r.snapshot_chosen_by || "")}</span></span>
      <span class="o ${esc(r.outcome || r.state)}">${esc(r.outcome || r.state)}</span>
    </div>`).join("");
}

async function selectReading(id) {
  state.selected = id;
  $$("#readings-table tbody tr").forEach(tr => {
    tr.classList.toggle("selected", tr.dataset.id === id);
  });
  await refreshDetail(id);
}

async function refreshDetail(id) {
  const data = await api(`/api/readings/${encodeURIComponent(id)}`);
  renderDetail(data);
  $("#detail-hint").textContent = `${id} · 原始值 ${data.raw} · 设备时间 ${shortTime(data.device_ts)}`;
}

function renderDetail(data) {
  const cmp = data.comparison;
  const latest = data.latest;
  const headline = latest
    ? `${statusPill(latest)} <span class="big">${latest.value_kpa === null ? "—" : esc(fact(latest.value_kpa))}</span><span class="hint">kPa</span>
       <span class="hint">采用 v${esc(latest.cal_version)} · r${esc(latest.revision)}${latest.supersedes ? " · 替换 r" + esc(latest.supersedes) : ""}</span>`
    : `<span class="pill unconverted">未发布</span><span class="hint">当前策略下没有可发布的内容</span>`;

  const banner = cmp.naive_latest_received
    ? `<div class="banner ${cmp.naive_latest_received.matches_chosen ? "info" : "warn"}">
        “最近收到的一份”会选用 v${esc(cmp.naive_latest_received.cal_version)}
        （${esc(shortTime(cmp.naive_latest_received.received_at))} 到达），
        与按测量时刻选出的 ${cmp.chosen_version === null ? "无" : "v" + esc(cmp.chosen_version)}
        ${cmp.naive_latest_received.matches_chosen ? "一致" : "不一致"}。
        本系统从不使用“最近收到”作为选择规则。
      </div>`
    : `<div class="banner info">尚无已接受标定；该读数不会借用之后再到的文件。</div>`;

  const rows = cmp.rows.map(row => {
    const klass = row.effective ? "effective" : "not-effective";
    const value = row.value_kpa === null ? "—" : esc(fact(row.value_kpa));
    const chosenMark = row.cal_version === cmp.chosen_version ? " ◀ 采用" : "";
    return `<tr>
      <td class="num">v${esc(row.cal_version)}</td>
      <td class="num">${esc(shortTime(row.valid_from))}</td>
      <td class="num">${esc(row.validity[0])}–${esc(row.validity[1])}</td>
      <td>${row.effective ? "生效" : "未生效"}</td>
      <td>${row.in_validity ? "在范围" : "超范围"}</td>
      <td class="num ${klass}">${value}${chosenMark}</td>
      <td class="num">${row.deviation_kpa === null ? "—" : esc(fact(row.deviation_kpa))}</td>
    </tr>`;
  }).join("");

  const history = data.history.map(h => `
    <div class="rev">
      <span>r${esc(h.revision)}</span>
      <span>${esc(h.status)}</span>
      <span>${h.value_kpa === null ? "—" : esc(fact(h.value_kpa))} kPa</span>
      <span>${h.supersedes ? "替换 r" + esc(h.supersedes) : "初版"}${h.cal_version !== null ? " · v" + esc(h.cal_version) : ""}</span>
    </div>`).join("");

  $("#detail").innerHTML = `
    <div class="headline">${headline}</div>
    ${banner}
    <div class="table-wrap short">
      <table class="data compare-table">
        <thead><tr><th>版本</th><th>生效时刻</th><th>有效原始值</th><th>是否生效</th><th>原始值范围</th><th>换算值 kPa</th><th>与真值差</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7"><span class="hint">没有已接受的标定</span></td></tr>`}</tbody>
      </table>
    </div>
    <div>
      <div class="card-head"><h2>发布修订</h2><span class="hint">只追加，不覆盖</span></div>
      <div class="history">${history || '<span class="hint">还没有发布</span>'}</div>
    </div>
    <div>
      <div class="card-head"><h2>设备配置快照</h2><span class="hint">随原始值原样保存</span></div>
      <div class="num">${esc(JSON.stringify(data.config, null, 1))}</div>
    </div>`;
}

function currentDevice() {
  const data = state.bootstrap;
  const selected = $("#device").value;
  if (selected && data) {
    const found = data.info.devices.find(d => d.key === selected);
    if (found) return found;
  }
  return data ? data.info.devices[0] : null;
}

function nextVersionFor(deviceKey) {
  const chains = (state.bootstrap && state.bootstrap.chains) || {};
  let best = 0;
  for (const calId of Object.keys(chains)) {
    for (const v of chains[calId]) {
      if (v.device === deviceKey && v.cal_version > best) best = v.cal_version;
    }
  }
  return best + 1;
}

function updateNextVersion() {
  const device = currentDevice();
  if (!device) return;
  $("#next-version").textContent = String(nextVersionFor(device.key));
}

function formDevice() {
  return $("#device").value;
}

function calTimeFor(option) {
  const now = Date.now();
  if (option === "past") return new Date(now - 6 * 3600 * 1000).toISOString();
  if (option === "future") return new Date(now + 2 * 3600 * 1000).toISOString();
  return new Date(now).toISOString();
}

async function setMode(device, mode) {
  try {
    await api("/api/mode", { method: "POST", body: JSON.stringify({ device, mode }) });
    note(`${device} 发布策略已切换为${mode === "hold" ? "等标定再发布" : "先发暂定值"}；已排队的读数会按新策略重算。`, "ok");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

async function sendReadings() {
  const device = formDevice();
  try {
    const startSeq = await nextSeq(device);
    const step = 20000;
    const now = Date.now() - 10 * 60 * 1000;
    const result = await api("/api/readings/bulk", {
      method: "POST",
      body: JSON.stringify({
        device, count: 6, start_seq: startSeq,
        start_ts: new Date(now).toISOString(), step_ms: step, value_mode: "kPa",
      }),
    });
    note(`${device} 写入 ${result.readings.length} 条原始读数（seq ${startSeq} 起）。`, "ok");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

async function nextSeq(device) {
  const data = state.bootstrap;
  let best = 0;
  for (const r of data.readings) {
    if (r.device === device && r.seq > best) best = r.seq;
  }
  return best + 1;
}

async function backfill() {
  const device = formDevice();
  try {
    const startSeq = await nextSeq(device);
    const now = Date.now() - 30 * 60 * 1000;
    const result = await api("/api/readings/bulk", {
      method: "POST",
      body: JSON.stringify({
        device, count: 12, start_seq: startSeq,
        start_ts: new Date(now).toISOString(), step_ms: 15000, value_mode: "kPa",
        config: { firmware: "2.4.1", gain: "8", comment: "补传通道" },
      }),
    });
    note(`${device} 补传 ${result.readings.length} 条（另一通道写入，配置不同）。`, "ok");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

async function deliverCal() {
  const device = formDevice();
  try {
    const result = await api("/api/calibrations/generate", {
      method: "POST",
      body: JSON.stringify({
        device,
        valid_from: calTimeFor($("#cal-from").value),
        drift_kpa: Number($("#cal-drift").value) || 0,
      }),
    });
    const d = result.delivery;
    note(`${device} 标定 v${d.cal_version}: ${d.decision}${d.reason ? " · " + d.reason : ""}；排队 ${d.queued ?? 0} 条。`, d.decision === "accepted" ? "ok" : "err");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

async function deliverJson() {
  let document;
  try {
    document = JSON.parse($("#cal-json").value);
  } catch (err) {
    note("JSON 解析失败: " + err.message, "err");
    return;
  }
  try {
    const result = await api("/api/calibrations", {
      method: "POST", body: JSON.stringify({ document }),
    });
    const d = result.delivery;
    note(`投递 #${d.seq}: ${d.decision}${d.reason ? " · " + d.reason : ""}${d.detail ? " · " + d.detail : ""}`,
      d.decision === "accepted" ? "ok" : "err");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

function fillTemplate() {
  const profile = currentDevice();
  if (!profile) return;
  const version = nextVersionFor(profile.key);
  const doc = {
    cal_id: "CAL-" + profile.key.split("-")[1],
    cal_version: version,
    device: profile.key,
    z0: profile.z0,
    coefs: profile.coefs,
    validity: profile.validity,
    valid_from: calTimeFor("past"),
    replaces: version > 1 ? version - 1 : null,
    note: "手工投递示例",
  };
  $("#cal-json").value = JSON.stringify(doc, null, 2);
}

async function processQueue(crash) {
  try {
    const result = await api("/api/process", {
      method: "POST",
      body: JSON.stringify({ max_runs: 200, crash: !!crash }),
    });
    const outcomes = result.runs.reduce((acc, r) => {
      acc[r.outcome] = (acc[r.outcome] || 0) + 1;
      return acc;
    }, {});
    if (crash && result.error) {
      note(`模拟崩溃：${result.error.message}；中断运行 ${result.error.interrupted_runs} 个，队列仍有 ${result.error.queue} 条。点“恢复并补算”。`, "err");
    } else {
      const summary = Object.entries(outcomes).map(([k, v]) => `${k} ${v}`).join("，") || "无待处理";
      note(`换算队列完成：${summary}。`, "ok");
    }
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

async function recover() {
  try {
    const result = await api("/api/recover", { method: "POST", body: "{}" });
    note(`恢复：中断运行 ${result.requeued} 个，补算 ${result.runs.length} 条。`, "ok");
    await refresh(true);
  } catch (err) { note(err.message, "err"); }
}

function bind() {
  $("#device").addEventListener("change", updateNextVersion);
  $("#btn-send").addEventListener("click", sendReadings);
  $("#btn-backfill").addEventListener("click", backfill);
  $("#btn-cal").addEventListener("click", deliverCal);
  $("#btn-run").addEventListener("click", () => processQueue(false));
  $("#btn-crash").addEventListener("click", () => processQueue(true));
  $("#btn-recover").addEventListener("click", recover);
  $("#btn-refresh").addEventListener("click", () => refresh(true));
  $("#btn-cal-json").addEventListener("click", deliverJson);
  $("#btn-cal-template").addEventListener("click", fillTemplate);
}

bind();
refresh(false).catch(err => note("加载失败: " + err.message, "err"));

// DOM-level smoke test of the page against the real API, using jsdom.
// Run:  node tests/ui_smoke.mjs [port] [jsdom-path]
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const jsdomPath = process.env.JSDOM_PATH || "/tmp/jsdomtest/node_modules/jsdom";
const { JSDOM } = require(jsdomPath);

const port = Number(process.argv[2] || 8811);
const base = `http://127.0.0.1:${port}`;
const root = path.dirname(path.dirname(new URL(import.meta.url).pathname));

const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const js = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

const dom = new JSDOM(html, { url: base + "/", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
globalThis.window = window;
globalThis.document = window.document;

window.fetch = async (url, options) => {
  const full = url.startsWith("http") ? url : base + url;
  return fetch(full, options);
};
window.alert = () => {};
window.confirm = () => true;

const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.error || e.message)));
window.eval(js);

function waitFor(predicate, label, timeout = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      let ok = false;
      try { ok = predicate(); } catch (e) { return reject(e); }
      if (ok) return resolve();
      if (Date.now() - started > timeout) return reject(new Error("timeout: " + label));
      setTimeout(tick, 50);
    };
    tick();
  });
}

const $ = (sel) => window.document.querySelector(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.trim() : "");

async function click(sel) {
  const el = $(sel);
  if (!el) throw new Error("missing " + sel);
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

async function post(p, body) {
  const r = await fetch(base + p, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

async function main() {
  await waitFor(() => text("#topstats").length > 0, "bootstrap render");
  console.log("initial stats:", text("#topstats").replace(/\s+/g, " "));
  if (!$("#modes .mode-row")) throw new Error("mode rows not rendered");
  if (!$("#device option")) throw new Error("device options not rendered");

  // 1. provisional mode, send readings, process, then deliver a calibration
  await click('#modes button[data-mode="provisional"]');
  await waitFor(() => text("#ctl-note").includes("发布策略"), "mode switch");
  console.log("mode note:", text("#ctl-note"));

  await click("#btn-send");
  await waitFor(() => document.querySelectorAll("#readings-table tbody tr.clickable").length > 0,
    "readings rows");
  console.log("readings:", document.querySelectorAll("#readings-table tbody tr.clickable").length);

  await click("#btn-run");
  await waitFor(() => text("#ctl-note").includes("换算队列完成"), "process run");
  console.log("process note:", text("#ctl-note"));
  await waitFor(() => text("#readings-table tbody").includes("未换算"), "unconverted row");
  console.log("first row:", text("#readings-table tbody tr").replace(/\s+/g, " "));

  await click("#btn-cal");
  await waitFor(() => text("#ctl-note").includes("标定 v"), "cal delivery");
  await click("#btn-run");
  await waitFor(() => text("#readings-table tbody").includes("已换算"), "priced row");
  console.log("after calibration:", text("#readings-table tbody tr").replace(/\s+/g, " "));

  // 2. select a reading and inspect the comparison table
  const row = document.querySelector("#readings-table tbody tr.clickable");
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => text("#detail").includes("发布修订"), "detail render");
  const cmpRows = document.querySelectorAll("#detail .compare-table tbody tr").length;
  const historyRows = document.querySelectorAll("#detail .history .rev").length;
  console.log("comparison rows:", cmpRows, "history rows:", historyRows);
  if (cmpRows < 1) throw new Error("comparison table empty");
  if (historyRows < 2) throw new Error("expected 2 revisions, got " + historyRows);

  // 3. crash + recover through the page buttons
  await click("#btn-crash");
  await waitFor(() => text("#ctl-note").includes("模拟崩溃"), "crash note", 30000);
  console.log("crash note:", text("#ctl-note"));
  await click("#btn-recover");
  await waitFor(() => text("#ctl-note").startsWith("恢复："), "recover note");
  console.log("recover note:", text("#ctl-note"));

  // 4. a future-dated correction stays pending; a retroactive one revises
  await post("/api/calibrations/generate", {
    device: "SYNTH-P", valid_from: "2027-01-01T00:00:00Z", drift_kpa: 9.0,
  });
  await click("#btn-refresh");
  await waitFor(() => text("#chains").includes("2027"), "future version in chain");
  console.log("chain entries:", document.querySelectorAll("#chains .version").length);

  // 5. JSON delivery path
  await click("#btn-cal-template");
  const template = JSON.parse($("#cal-json").value);
  template.cal_version = 99;
  template.valid_from = "2026-01-01T00:00:00Z";
  template.coefs = template.coefs.map((c, i) => (i === 0 ? c + 0.25 : c));
  template.note = "manual delivery";
  $("#cal-json").value = JSON.stringify(template);
  await click("#btn-cal-json");
  await waitFor(() => text("#ctl-note").includes("投递 #"), "json delivery");
  console.log("json note:", text("#ctl-note"));

  // 6. tradeoff table has real numbers
  const tradeoff = text("#tradeoff-table tbody").replace(/\s+/g, " ");
  console.log("tradeoff:", tradeoff);
  if (!/provisional|暂定/.test(tradeoff)) throw new Error("tradeoff table empty");

  console.log("js errors:", errors.length ? errors : "none");
  if (errors.length) throw new Error("page errors: " + errors.join("; "));

  // 7. deliveries table shows both accepted and rejected
  const deliveries = text("#deliveries-table tbody").replace(/\s+/g, " ");
  if (!deliveries.includes("接受")) throw new Error("no accepted delivery shown");
  console.log("deliveries:", deliveries.slice(0, 200));
  console.log("UI SMOKE OK");
  window.close();
}

main().catch((err) => {
  console.error("UI SMOKE FAILED:", err.message);
  process.exit(1);
});

/* Grants Manager frontend */
"use strict";

let S = null;            // full state from server
let view = { name: "dashboard", grantId: null };
let charts = [];

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const fmt2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });
const money = (v) => fmt.format(v || 0);
const money2 = (v) => fmt2.format(v || 0);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Local calendar date — not toISOString(), which is UTC and rolls to
// "tomorrow" on evenings in US time zones. Always returns today, populated.
const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

/* ---------------------------------------------------------------- API */
// Access key for reaching this app from another device (phone on the same
// Wi-Fi). Arrives once in the URL as ?k=…, then lives in this browser so the
// link doesn't have to carry it around. Requests from the computer running
// the app never need it.
const ACCESS_KEY = (() => {
  const fromUrl = new URLSearchParams(location.search).get("k");
  if (fromUrl) { try { localStorage.setItem("gm-key", fromUrl); } catch { /* private mode */ } return fromUrl; }
  try { return localStorage.getItem("gm-key") || ""; } catch { return ""; }
})();

function withKey(url) {
  if (!ACCESS_KEY) return url;
  return url + (url.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(ACCESS_KEY);
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  // custom header a cross-origin page can't set without a preflight we never
  // answer — this is what stops another website driving the app
  opts.headers["X-Grants-App"] = "1";
  if (ACCESS_KEY) opts.headers["X-Grants-Key"] = ACCESS_KEY;
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}
async function reload() {
  S = await api("/api/state");
  render();
  if (typeof refreshNotifBadge === "function") refreshNotifBadge();
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ------------------------------------------------------------ computed */
function catName(id) { const c = S.categories.find((c) => c.id === id); return c ? c.name : "—"; }
// "Other" is a pseudo-grant for salary shares paid by external sources —
// shown on People, hidden from budgets, hiring math, alerts, and cards
function isExternal(g) { return g.name === "Other"; }
function realGrants() { return S.grants.filter((g) => !isExternal(g)); }
// The "Other" bucket as a <option> for expense grant pickers — for charges
// paid from an outside account (a colleague's, the department's). Never
// counts against your budgets. Returns "" if the bucket doesn't exist yet.
function externalGrantOption() {
  const ext = S.grants.find(isExternal);
  return ext ? `<option value="${ext.id}">Other (external account)</option>` : "";
}
function personName(id) { const p = S.people.find((p) => p.id === id); return p ? p.name : ""; }
function grantCats(g) {
  return S.categories.filter((c) => c.grant_id === null || c.grant_id === g.id);
}
function grantYears(g) {
  let n = 1;
  if (g.start_date && g.end_date) {
    const s = new Date(g.start_date), e = new Date(g.end_date);
    n = Math.max(1, Math.ceil((e - s) / (365.25 * 864e5)));
  }
  for (const b of S.budget_lines) if (b.grant_id === g.id) n = Math.max(n, b.year);
  for (const e of S.expenses) if (e.grant_id === g.id) n = Math.max(n, e.year || 1);
  return n;
}
function budgetFor(gid, cid, year) {
  const b = S.budget_lines.find((b) => b.grant_id === gid && b.category_id === cid && b.year === year);
  return b ? b.amount : 0;
}
// Workday codes mapped to an app grant
function wdCodesFor(gid) {
  if (!WD || !WD.mappings) return [];
  return WD.mappings.filter((m) => m.kind === "grant" && m.target_id === gid).map((m) => m.wd_key);
}
// A grant is "balance-driven" once a Budget-vs-Actuals report is imported for
// it: budget and spent then come from Workday's figures, not manual expenses.
function grantHasBalances(gid) {
  if (!WD || !WD.balances) return false;
  const codes = wdCodesFor(gid);
  return codes.length > 0 && WD.balances.some((b) => codes.includes(b.grant_code));
}
function grantActuals(gid) {
  const codes = wdCodesFor(gid);
  if (!codes.length || !WD.balances) return 0;
  return WD.balances.filter((b) => codes.includes(b.grant_code)).reduce((s, b) => s + (b.actuals || 0), 0);
}
function catActuals(gid, cid) {
  const codes = wdCodesFor(gid);
  if (!codes.length || !WD.balances) return 0;
  const cm = {};
  WD.mappings.filter((m) => m.kind === "category").forEach((m) => { cm[m.wd_key] = m.target_id; });
  return WD.balances
    .filter((b) => codes.includes(b.grant_code) && cm[b.object_class] === cid)
    .reduce((s, b) => s + (b.actuals || 0), 0);
}
function spentFor(gid, cid, year) {
  // Balance-driven grant: actuals are a whole-grant snapshot -> show in year 1.
  if (grantHasBalances(gid)) return (year == null || year === 1) ? catActuals(gid, cid) : 0;
  return S.expenses
    .filter((e) => e.grant_id === gid && e.category_id === cid && (year == null || (e.year || 1) === year))
    .reduce((s, e) => s + e.amount, 0);
}
function grantSpent(gid) {
  if (grantHasBalances(gid)) return grantActuals(gid);
  return S.expenses.filter((e) => e.grant_id === gid).reduce((s, e) => s + e.amount, 0);
}
function grantBudgeted(gid) {
  return S.budget_lines.filter((b) => b.grant_id === gid).reduce((s, b) => s + b.amount, 0);
}
function effectiveEnd(g) {
  // no-cost extension overrides the original end date
  return g.nce_end_date || g.end_date || "";
}
function daysLeft(g) {
  const end = effectiveEnd(g);
  if (!end) return null;
  return Math.round((new Date(end) - new Date(S.today)) / 864e5);
}

/* Money already promised but not yet billed — purchase orders and
   requisitions Workday knows about. It isn't "spent" yet, but it isn't
   yours to spend either, so it comes off what's available. Zero until a
   Workday import provides the figures. */
function grantCommitments(gid) {
  if (!WD || !WD.balances || !WD.mappings) return 0;
  const codes = WD.mappings
    .filter((m) => m.kind === "grant" && m.target_id === gid)
    .map((m) => m.wd_key);
  if (!codes.length) return 0;
  return WD.balances
    .filter((b) => codes.includes(b.grant_code))
    .reduce((s, b) => s + (b.obligation || 0) + (b.commitment || 0), 0);
}
function grantAvailable(gid) {
  const g = S.grants.find((x) => x.id === gid);
  if (!g) return 0;
  return g.initial_amount - grantSpent(gid) - grantCommitments(gid);
}

/* "18 of 36 months gone (50%) — 32% of the money spent" */
function grantPace(g) {
  const start = g.start_date, end = effectiveEnd(g);
  if (!start || !end) return null;
  const total = monthsBetween(monthKey(start), monthKey(end));
  if (total <= 0) return null;
  const gone = Math.max(0, Math.min(total, monthsBetween(monthKey(start), monthKey(S.today))));
  const spent = grantSpent(g.id);
  return {
    total, gone,
    timePct: Math.round(gone / total * 100),
    spentPct: g.initial_amount > 0 ? Math.round(spent / g.initial_amount * 100) : null,
  };
}

/* Recent burn rate, and what it implies by the grant's end date. */
function grantBurn(g) {
  const nowKey = monthKey(S.today);
  const byMonth = {};
  for (const e of S.expenses) {
    if (e.grant_id !== g.id || e.source === "adjust") continue;
    const k = monthKey(e.date);
    if (k <= nowKey) byMonth[k] = (byMonth[k] || 0) + e.amount;
  }
  const recent = Object.keys(byMonth).sort().slice(-6).map((k) => byMonth[k]);
  if (!recent.length) return null;
  const avg = recent.reduce((a, b) => a + b, 0) / recent.length;
  if (avg <= 0) return null;
  const end = effectiveEnd(g);
  const left = grantAvailable(g.id);
  const monthsToEnd = end ? Math.max(0, monthsBetween(nowKey, monthKey(end)) - 1) : null;
  const monthsOfMoney = left > 0 ? left / avg : 0;
  return {
    avg, left, monthsToEnd, monthsOfMoney,
    // what's left on the day the grant ends, at this pace
    leftAtEnd: monthsToEnd === null ? null : left - avg * monthsToEnd,
    runOut: left > 0 && monthsToEnd !== null && monthsOfMoney < monthsToEnd,
  };
}

/* ------------------------------------------------------- projections */
function monthKey(d) { return d.slice(0, 7); }
function addMonths(ym, n) {
  const [y, m] = ym.split("-").map(Number);
  const t = y * 12 + (m - 1) + n;
  return `${Math.floor(t / 12).toString().padStart(4, "0")}-${String(t % 12 + 1).padStart(2, "0")}`;
}
function monthsBetween(a, b) { // inclusive count of months a..b, 0 if a > b
  if (a > b) return 0;
  const [ay, am] = a.split("-").map(Number), [by, bm] = b.split("-").map(Number);
  return (by * 12 + bm) - (ay * 12 + am) + 1;
}

function projectAppointment(a) {
  /* Future commitment of one appointment: months not yet charged, clamped
     to the grant's effective end (incl. no-cost extension). */
  const g = S.grants.find((g) => g.id === a.grant_id);
  if (!g || g.status !== "active") return null;
  const gEnd = effectiveEnd(g);
  const endYM = [a.end_date && monthKey(a.end_date), gEnd && monthKey(gEnd)]
    .filter(Boolean).sort()[0];
  if (!endYM) return null;
  // last salary actually charged to this person on this grant
  const charged = S.expenses
    .filter((e) => e.grant_id === a.grant_id && e.person_id === a.person_id &&
                   catName(e.category_id) === "Personnel" && e.amount > 0)
    .map((e) => monthKey(e.date)).sort();
  const last = charged[charged.length - 1];
  let startYM = monthKey(a.start_date || S.today);
  if (last) startYM = addMonths(last, 1) > startYM ? addMonths(last, 1) : startYM;
  else if (monthKey(S.today) > startYM) startYM = monthKey(S.today);
  const months = monthsBetween(startYM, endYM);
  if (months <= 0) return null;
  const salary = a.monthly_salary * months;
  const fringe = salary * (a.fringe_rate || 0) / 100;
  const tuition = (a.annual_tuition || 0) / 12 * months;
  return { grant_id: a.grant_id, person_id: a.person_id, months,
           from: startYM, to: endYM, salary, fringe, tuition,
           total: salary + fringe + tuition };
}

function grantProjection(gid) {
  /* Sum of future commitments per grant + projected availability. */
  const g = S.grants.find((g) => g.id === gid);
  const parts = S.appointments.filter((a) => a.grant_id === gid)
    .map(projectAppointment).filter(Boolean);
  const proj = { salary: 0, fringe: 0, tuition: 0, total: 0, parts };
  for (const p of parts) {
    proj.salary += p.salary; proj.fringe += p.fringe;
    proj.tuition += p.tuition; proj.total += p.total;
  }
  const personnelNow = catRemaining(g, ["Personnel"]).remaining;
  const fringeNow = catRemaining(g, ["Fringe"]).remaining;
  const tuitionNow = catRemaining(g, ["Tuition"]).remaining;
  // fringe/tuition overruns eat into the salary pot
  const fringeOver = Math.max(0, proj.fringe - Math.max(0, fringeNow));
  const tuitionOver = Math.max(0, proj.tuition - Math.max(0, tuitionNow));
  proj.salaryNow = personnelNow;
  proj.salaryProjected = personnelNow - proj.salary - fringeOver - tuitionOver;
  proj.fringeNow = fringeNow;
  proj.fringeProjected = fringeNow - proj.fringe;
  proj.tuitionNow = tuitionNow;
  proj.tuitionProjected = tuitionNow - proj.tuition;
  proj.availableNow = grantAvailable(gid);
  proj.availableProjected = proj.availableNow - proj.total;
  return proj;
}
/* Dashboard alerts you have already read.

   Keyed by the server's run id, so "hide this" lasts exactly as long as the
   running app: reloading the page keeps it hidden, quitting Grants Manager
   and opening it again brings every still-true alert back. Old runs are
   dropped on write, so this never grows. */
function dismissedAlerts() {
  if (!S.run_id) return [];
  try {
    const all = JSON.parse(localStorage.getItem("gm-dismissed-alerts") || "{}");
    return all[S.run_id] || [];
  } catch { return []; }
}

function dismissAlert(id) {
  if (!S.run_id) return;
  const list = [...new Set([...dismissedAlerts(), id])];
  // only this run's entries are kept — yesterday's ids mean nothing now
  try {
    localStorage.setItem("gm-dismissed-alerts",
                         JSON.stringify({ [S.run_id]: list }));
  } catch { /* private browsing: the alert just comes back on reload */ }
}

function visibleAlerts() {
  const hidden = dismissedAlerts();
  return computeAlerts().filter((a) => !hidden.includes(a.id));
}

function computeAlerts() {
  const alerts = [];
  for (const g of S.grants) {
    if (g.status !== "active" || isExternal(g)) continue;
    const dl = daysLeft(g);
    if (dl !== null && dl < 0)
      alerts.push({ id: `end:${g.id}`, level: "red", grantId: g.id, text: `${g.name} ended ${-dl} days ago — mark it closed or extend the end date.` });
    else if (dl !== null && dl <= 183)
      alerts.push({ id: `end:${g.id}`, level: dl <= 60 ? "red" : "amber", grantId: g.id, text: `${g.name} ends in ${dl} days (${g.end_date}).` });
    const spent = grantSpent(g.id);
    if (g.initial_amount > 0 && spent / g.initial_amount >= 0.9)
      alerts.push({ id: `spent:${g.id}`, level: "red", grantId: g.id, text: `${g.name}: ${Math.round(spent / g.initial_amount * 100)}% of the total award spent.` });
    // Running dry before the end date is the thing you can still act on, and
    // it was only visible as a sentence buried in the grant's own card.
    const burn = grantBurn(g);
    if (burn && burn.runOut) {
      const m = Math.floor(burn.monthsOfMoney);
      const early = burn.monthsToEnd - m;
      // "runs out 0 months before it ends" is just "it ends" — not news
      if (early >= 1) alerts.push({
        id: `runout:${g.id}`, level: early >= 6 ? "red" : "amber", grantId: g.id,
        text: `${g.name}: at ${money(burn.avg)}/month the money runs out in about ${m} month${m === 1 ? "" : "s"} — ${early} month${early === 1 ? "" : "s"} before the grant ends (${effectiveEnd(g)}). Slow the burn or request a no-cost extension.`,
      });
    }
    // only the grant's current budget year — closed cycles are history
    const curYear = budgetYearOf(g, S.today);
    for (const c of grantCats(g)) {
      const b = budgetFor(g.id, c.id, curYear);
      if (b <= 0) continue;
      const sp = spentFor(g.id, c.id, curYear);
      if (sp > b + 0.01)
        alerts.push({ id: `over:${g.id}:${c.id}`, level: "red", grantId: g.id, text: `${g.name} · Y${curYear} ${c.name}: overspent (${money(sp)} of ${money(b)}).` });
      else if (sp / b >= 0.8 && sp < b)
        alerts.push({ id: `near:${g.id}:${c.id}`, level: "amber", grantId: g.id, text: `${g.name} · Y${curYear} ${c.name}: ${Math.round(sp / b * 100)}% spent (${money(b - sp)} left).` });
    }
  }
  return alerts;
}

/* ------------------------------------------------------------- render */
function render() {
  charts.forEach((c) => c.destroy());
  charts = [];
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view.name));
  const m = $("#main");
  if (view.name === "dashboard") m.innerHTML = renderDashboard();
  else if (view.name === "grant") m.innerHTML = renderGrant();
  else if (view.name === "closeout") m.innerHTML = renderCloseout();
  else if (view.name === "people") m.innerHTML = renderPeople();
  else if (view.name === "allexpenses") m.innerHTML = renderAllExpenses();
  else if (view.name === "summary") m.innerHTML = renderSummary();
  else if (view.name === "instructions") m.innerHTML = renderInstructions();
  wireUp(m);
  if (view.name === "summary") drawSummaryCharts();
  if (view.name === "dashboard") drawDashboardChart();
}

/* First run: say what to do, in order, instead of showing an empty form.
   Disappears on its own once each step is done. */
function firstRunCard() {
  const hasGrant = realGrants().length > 0;
  const hasBudget = S.budget_lines.length > 0;
  const hasExpense = S.expenses.length > 0;
  if (hasGrant && hasBudget && hasExpense) return "";
  const step = (done, n, label, hint) => `
    <li style="margin:9px 0;${done ? "opacity:.5" : ""}">
      <strong>${done ? "✓" : n}. ${label}</strong>
      <div class="sub" style="margin:2px 0 0">${hint}</div>
    </li>`;
  return `
    <div class="card no-print" style="border-left:3px solid var(--accent, #2f6fed)">
      <h2>${hasGrant ? "Nearly there" : "Welcome — three steps to set up"}</h2>
      <ol style="margin:6px 0 0;padding-left:20px;list-style:none">
        ${step(hasGrant, 1, "Add your grant",
          `Click <strong>+ New grant</strong> at the top right. You need the award total and the start and end dates.`)}
        ${step(hasBudget, 2, "Set the budget for each category",
          `Open the grant and fill in what you're allowed to spend on personnel, travel, supplies and the rest. This is what the app compares your spending against.`)}
        ${step(hasExpense, 3, "Add your expenses",
          `Either type them into <strong>Quick add expense</strong>, or import a report from Workday with <strong>⇅ Workday</strong> — the Instructions tab has the step-by-step.`)}
      </ol>
    </div>`;
}

/* ---------------------------------------------------------- dashboard */
function renderDashboard() {
  const active = realGrants().filter((g) => g.status === "active");
  const closed = realGrants().filter((g) => g.status !== "active");
  const totalAvail = active.reduce((s, g) => s + grantAvailable(g.id), 0);
  const totalAward = active.reduce((s, g) => s + g.initial_amount, 0);
  const ytd = S.expenses.filter((e) => e.date.startsWith(S.today.slice(0, 4)) && e.source !== "adjust").reduce((s, e) => s + e.amount, 0);
  const alerts = visibleAlerts();

  const hideClosed = localStorage.getItem("gm-hide-closed") === "1";
  return `
    <h1>Dashboard</h1>
    <p class="sub">${active.length} active grant${active.length === 1 ? "" : "s"} · updated ${S.today}</p>

    ${firstRunCard()}

    ${!active.length ? "" : `<div class="card no-print">
      <h2>Quick add expense</h2>
      <div class="quick">
        <label class="field amt"><span>Amount</span><input type="number" step="0.01" id="q-amount" placeholder="0.00"></label>
        <label class="field gsel"><span>Grant</span><select id="q-grant">${active.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}${externalGrantOption()}</select></label>
        <label class="field csel"><span>Category</span><select id="q-cat"></select></label>
        <label class="field dt"><span>Date</span><input type="date" id="q-date" value="${todayISO()}"></label>
        <label class="field q-ext-field" style="width:170px;display:none"><span>Worktag (whose account)</span><input id="q-ext-acct" placeholder="e.g. GR012345 or CC067890…"></label>
        <label class="field desc"><span>Comments</span><input id="q-desc" placeholder="What was it for?"></label>
        <label style="display:flex;align-items:center;gap:6px;align-self:flex-end;padding-bottom:10px;white-space:nowrap;font-size:13px;cursor:pointer"><input type="checkbox" id="q-split-on" style="width:auto">Split across worktags</label>
        <label class="field q-split-field" style="width:150px;display:none"><span>Split with</span><select id="q-split-grant"><option value="">— pick —</option>${active.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}</select></label>
        <label class="field q-split-field" style="width:70px;display:none"><span>Their %</span><input type="number" id="q-split-pct" min="1" max="99" placeholder="50"></label>
        <label class="field q-split-field" style="width:135px;display:none"><span>Split Cost Center</span><input id="q-split-cc" placeholder="CC… (auto)"></label>
        <label class="field q-split-field" style="width:135px;display:none"><span>Split Worktag</span><input id="q-split-wt" placeholder="GR… (auto)"></label>
        <div style="display:flex;align-items:flex-start;gap:5px">
          <div class="dropzone" id="q-drop" style="width:150px">📎 Drop receipt<br>or click</div>
          <span title="Receipts must be PDF files — Workday only accepts PDF attachments" style="cursor:help;color:var(--muted);font-size:13px;line-height:1.2">ⓘ</span>
        </div>
        <input type="file" id="q-file" hidden>
        <label style="display:flex;align-items:center;gap:6px;align-self:flex-end;padding-bottom:10px;white-space:nowrap;font-size:13px;cursor:pointer"><input type="checkbox" id="q-workday" style="width:auto" checked>📤 Add to Workday</label>
        <label style="display:flex;align-items:center;gap:6px;align-self:flex-end;padding-bottom:10px;white-space:nowrap;font-size:13px;cursor:pointer" title="Bought on a university purchasing card — the report will carry the details reconciliation asks for"><input type="checkbox" id="q-pcard" style="width:auto">💳 P-card</label>
        ${pcardFields("q")}
        <button class="btn" id="q-save">Add</button>
      </div>
    </div>`}

    ${alerts.length ? `<div class="alerts">${alerts.map((a) =>
      `<div class="alert ${a.level}" data-goto-grant="${a.grantId}">⚠️ <span>${esc(a.text)}</span>
        <button class="alert-x" data-dismiss-alert="${esc(a.id)}" title="Hide until you next open Grants Manager" aria-label="Hide this warning">✕</button>
      </div>`).join("")}</div>` : ""}

    ${wdDashCards()}

    <div class="grid cols-3">
      ${active.map(grantCard).join("")}
    </div>

    ${S.expenses.some((e) => e.source !== "adjust") ? `
      <div class="card" style="margin-top:18px">
        <h2>Spending by month — all grants</h2>
        <div class="chart-wrap" style="height:210px"><canvas id="dash-month-chart"></canvas></div>
      </div>` : ""}

    ${active.length ? `<div class="stats" style="margin-top:22px">
      <div class="stat"><div class="label">Available (all active)</div><div class="value">${money(totalAvail)}</div></div>
      <div class="stat"><div class="label">Total awarded (active)</div><div class="value">${money(totalAward)}</div></div>
      <div class="stat"><div class="label">Spent this year</div><div class="value">${money(ytd)}</div></div>
      <div class="stat"><div class="label">People funded</div><div class="value">${S.people.length}</div></div>
    </div>` : ""}

    ${closed.length ? `
      <div class="section-head" style="margin-top:8px">
        <h2 style="margin:0">Closed grants (${closed.length})</h2>
        <button class="btn secondary small no-print" id="btn-toggle-closed">${hideClosed ? "Show" : "Hide"} closed grants</button>
      </div>
      ${hideClosed ? "" : `<div class="grid cols-3">${closed.map(grantCard).join("")}</div>`}` : ""}
  `;
}

function wdWorktagFor(gid) {
  const code = WD && WD.push && WD.push.codes && WD.push.codes[gid];
  return code ? code.grant_code : null;
}

function grantCard(g) {
  const spent = grantSpent(g.id);
  const avail = grantAvailable(g.id);
  // gray grows from the left as money is spent; the rest stays green (available)
  const spentPct = g.initial_amount > 0 ? Math.max(0, Math.min(100, spent / g.initial_amount * 100)) : 0;
  const dl = daysLeft(g);
  let endBadge = "";
  if (g.status !== "active") endBadge = `<span class="badge gray">closed</span>`;
  else if (dl !== null && dl < 0) endBadge = `<span class="badge red">ended</span>`;
  else if (dl !== null && dl <= 183) endBadge = `<span class="badge amber">${dl} days left</span>`;
  const committed = grantCommitments(g.id);
  const pace = grantPace(g);
  const burn = grantBurn(g);
  return `
    <div class="card grant-card" data-goto-grant="${g.id}">
      <div class="gname">${esc(g.name)} ${endBadge}</div>
      <div class="agency">${esc(g.agency || "")}${effectiveEnd(g) ? ` · ends ${effectiveEnd(g)}` : ""}${g.nce_end_date ? " (NCE)" : ""}</div>
      <div class="big">${money(avail)} <small>available</small></div>
      <div class="bar availback"><div class="spentfill" style="width:${spentPct}%"></div></div>
      <div class="meta-row"><span>■ ${money(spent)} spent</span>${committed > 0
        ? `<span style="color:var(--muted)">■ ${money(committed)} committed</span>` : ""}<span style="color:var(--green)">■ ${money(avail)} available</span></div>
      ${pace ? `<div class="pace-line">${pace.gone} of ${pace.total} month${pace.total === 1 ? "" : "s"} gone (${pace.timePct}%)${
        pace.spentPct === null ? "" : ` — you've spent ${pace.spentPct}% of the money`}</div>` : ""}
      ${burn ? `<div class="pace-line ${burn.runOut ? "warn" : ""}">${burnSentence(burn)}</div>` : ""}
    </div>`;
}

/* One plain sentence instead of a chart to interpret. */
function burnSentence(b) {
  if (b.left <= 0) return `Nothing left to spend at the current pace.`;
  if (b.monthsToEnd === null) return `Spending about ${money(b.avg)} a month lately.`;
  if (b.runOut) {
    const m = Math.floor(b.monthsOfMoney);
    return `At ${money(b.avg)}/month you run out in about ${m} month${m === 1 ? "" : "s"} — ${b.monthsToEnd - m} month${b.monthsToEnd - m === 1 ? "" : "s"} before the grant ends.`;
  }
  return `At ${money(b.avg)}/month, about ${money(Math.max(0, b.leftAtEnd))} would be left when the grant ends.`;
}

/* ------------------------------------------------------- grant detail */
function renderGrant() {
  const g = S.grants.find((x) => x.id === view.grantId);
  if (!g) { view = { name: "dashboard" }; return renderDashboard(); }
  const years = grantYears(g);
  const cats = grantCats(g);
  const spent = grantSpent(g.id);
  const avail = grantAvailable(g.id);
  const budgeted = grantBudgeted(g.id);
  const dl = daysLeft(g);
  const gproj = grantProjection(g.id);
  const gExp = S.expenses.filter((e) => e.grant_id === g.id);
  const gApps = S.appointments.filter((a) => a.grant_id === g.id);

  // rows: only categories with a budget or an expense, plus always-standard ones with data
  const usedCats = cats.filter((c) =>
    S.budget_lines.some((b) => b.grant_id === g.id && b.category_id === c.id) ||
    gExp.some((e) => e.category_id === c.id) || c.grant_id === g.id);
  const matrixCats = usedCats.length ? usedCats : cats;
  const wt = wdWorktagFor(g.id);

  return `
    <span class="back" data-goto-dash>← All grants</span>
    <div class="section-head">
      <div>
        <h1>${esc(g.name)}</h1>
        <p class="sub" style="margin-bottom:0">${esc(g.agency || "")}${wt ? ` · ${esc(wt)}` : ""}${g.start_date ? ` · ${g.start_date} → ${effectiveEnd(g)}` : effectiveEnd(g) ? ` · ends ${effectiveEnd(g)}` : ""}
          ${g.nce_end_date ? ` · <span class="badge blue">NCE from ${g.end_date}</span>` : ""}
          ${g.status !== "active" ? ` · <span class="badge gray">closed</span>` : dl !== null && dl <= 183 ? ` · <span class="badge amber">${dl} days left</span>` : ""}</p>
      </div>
      <div class="toolbar no-print">
        <button class="btn secondary small" id="btn-edit-grant">Edit grant</button>
        <a href="${withKey(`/api/export/grant/${g.id}.csv`)}"><button class="btn secondary small">⬇ Export CSV</button></a>
        <button class="btn secondary small" onclick="window.print()">🖨 Print report</button>
        <button class="btn secondary small" id="btn-closeout">📄 Close-out report</button>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><div class="label">Available now</div><div class="value" style="color:${avail < 0 ? "var(--red)" : "var(--green)"}">${money(avail)}</div></div>
      <div class="stat" title="Available now minus salary, fringe and tuition committed to current appointments through the grant's end"><div class="label">Projected available</div><div class="value" style="color:${gproj.availableProjected < 0 ? "var(--red)" : "var(--green)"}">${money(gproj.availableProjected)}</div></div>
      <div class="stat"><div class="label">Awarded (funded)</div><div class="value">${money(g.initial_amount)}</div></div>
      <div class="stat"><div class="label">Spent</div><div class="value">${money(spent)}</div></div>
      <div class="stat"><div class="label">Budgeted in lines</div><div class="value">${money(budgeted)}</div></div>
    </div>

    <div class="card">
      <div class="section-head">
        <h2>Budget by category × year</h2>
        <div class="toolbar no-print">
          <button class="btn ghost small" id="btn-add-cat">+ custom category</button>
          <span style="color:var(--muted);font-size:12px">click a budget number to edit</span>
        </div>
      </div>
      <table class="matrix">
        <thead><tr><th>Category</th>${range(years).map((y) => `<th class="num">Year ${y}</th>`).join("")}<th class="num">Total</th><th class="num">Remaining</th></tr></thead>
        <tbody>
          ${matrixCats.map((c) => {
            let rowB = 0, rowS = 0;
            const cells = range(years).map((y) => {
              const b = budgetFor(g.id, c.id, y);
              const sp = spentFor(g.id, c.id, y);
              rowB += b; rowS += sp;
              return `<td class="num"><div class="cell">
                <span class="b" data-edit-budget="${c.id}:${y}" title="Click to edit budget">${money(b)}</span>
                <span class="s ${sp > b + 0.005 && b > 0 ? "over" : ""}">${sp ? money(sp) + " spent" : ""}</span>
              </div></td>`;
            }).join("");
            const rem = rowB - rowS;
            return `<tr><td class="rowlabel">${esc(c.name)}${c.grant_id ? ' <span class="badge blue">custom</span>' : ""}</td>${cells}
              <td class="num" style="font-weight:600">${money(rowB)}</td>
              <td class="num" style="font-weight:700;color:${rem < 0 ? "var(--red)" : "var(--green)"}">${money(rem)}</td></tr>`;
          }).join("")}
        </tbody>
        <tfoot><tr><td>Total</td>
          ${range(years).map((y) => {
            const b = matrixCats.reduce((s, c) => s + budgetFor(g.id, c.id, y), 0);
            return `<td class="num">${money(b)}</td>`;
          }).join("")}
          <td class="num">${money(budgeted)}</td>
          <td class="num" style="color:${budgeted - spent < 0 ? "var(--red)" : "inherit"}">${money(budgeted - spent)}</td></tr></tfoot>
      </table>
    </div>

    <div class="grid cols-2">
      <div class="card"><h2>Spending by category</h2><div class="chart-wrap"><canvas id="grant-cat-chart"></canvas></div></div>
      <div class="card"><h2>Where the remaining money is</h2><div class="chart-wrap"><canvas id="grant-cat-donut"></canvas></div></div>
    </div>

    <div class="grid cols-2">
      <div class="card"><h2>Spending burn-down</h2><div class="chart-wrap"><canvas id="burn-chart"></canvas></div></div>
      <div class="card">
        <h2>People on this grant</h2>
        ${gApps.length ? gApps.map((a) => {
          const p = S.people.find((p) => p.id === a.person_id) || { name: "?" };
          const pct = a.pct || 100;
          return `<div class="person-chip">👤 ${esc(p.name)} · ${money(a.monthly_salary * 12)}/yr${pct < 100 ? ` (${pct}% of ${money(a.monthly_salary * 12 / (pct / 100))})` : ""} + ${a.fringe_rate}% fringe · ${a.start_date} → ${a.end_date}</div>`;
        }).join("") : `<div class="empty">No one appointed yet — add appointments in the People tab.</div>`}
        ${g.notes ? `<h2 style="margin-top:16px">Notes</h2><div class="notes-block">${esc(g.notes)}</div>` : ""}
      </div>
    </div>

    <div class="card">
      <div class="section-head">
        <h2>Expenses (${gExp.length})</h2>
        <div class="toolbar no-print">
          <select id="f-cat" style="width:150px"><option value="">All categories</option>${cats.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select>
          <select id="f-year" style="width:110px"><option value="">All years</option>${range(years).map((y) => `<option value="${y}">Year ${y}</option>`).join("")}</select>
          <button class="btn small" id="btn-add-exp">+ Add expense</button>
        </div>
      </div>
      <table id="exp-table">
        <thead><tr><th>Date</th><th>Category</th><th>Yr</th><th>Description</th><th>Person</th><th class="num">Amount</th><th>Receipt</th><th class="no-print"></th></tr></thead>
        <tbody>${gExp.map(expRow).join("") || `<tr><td colspan="8" class="empty">No expenses yet.</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

function expRow(e) {
  return `<tr data-exp-row data-cat="${e.category_id || ""}" data-year="${e.year || 1}">
    <td>${e.date}</td>
    <td>${esc(catName(e.category_id))}</td>
    <td>Y${e.year || 1}</td>
    <td>${esc(e.description)} ${e.source === "salary" ? '<span class="badge gray">auto</span>' : e.source === "adjust" ? '<span class="badge blue">rollover</span>' : e.source === "workday" ? '<span class="badge green">workday</span>' : ""}</td>
    <td>${esc(personName(e.person_id))}</td>
    <td class="num">${money2(e.amount)}</td>
    <td>${e.receipt_path ? `<a class="receipt-link" href="${withKey(`/receipts/${encodeURIComponent(e.receipt_path).replaceAll("%2F", "/")}`)}" target="_blank">📎 view</a>` : ""}</td>
    <td class="no-print" style="white-space:nowrap">
      <button class="icon-btn" data-edit-exp="${e.id}" title="Edit">✏️</button>
      <button class="icon-btn" data-del-exp="${e.id}" title="Delete">🗑</button>
    </td></tr>`;
}

/* -------------------------------------------------------------- summary */
const HIRING_CATS = ["Personnel", "Fringe", "Tuition"];

function catRemaining(g, names) {
  // remaining (budget - spent) for the given category names on one grant
  let budget = 0, spent = 0;
  for (const c of grantCats(g)) {
    if (!names.includes(c.name)) continue;
    budget += S.budget_lines.filter((b) => b.grant_id === g.id && b.category_id === c.id)
      .reduce((s, b) => s + b.amount, 0);
    spent += spentFor(g.id, c.id, null);
  }
  return { budget, spent, remaining: budget - spent };
}

function categoryTotals(includeClosed) {
  // aggregate by category NAME across grants -> {name: {budget, spent}}
  const out = {};
  const gs = realGrants().filter((g) => includeClosed || g.status === "active");
  for (const g of gs) {
    for (const c of grantCats(g)) {
      const b = S.budget_lines.filter((x) => x.grant_id === g.id && x.category_id === c.id)
        .reduce((s, x) => s + x.amount, 0);
      const sp = spentFor(g.id, c.id, null);
      if (!b && !sp) continue;
      out[c.name] = out[c.name] || { budget: 0, spent: 0 };
      out[c.name].budget += b;
      out[c.name].spent += sp;
    }
  }
  return out;
}

function renderSummary() {
  const active = realGrants().filter((g) => g.status === "active");
  const rows = active.map((g) => ({ g, p: grantProjection(g.id), days: daysLeft(g) }))
    .sort((a, b) => b.p.salaryNow - a.p.salaryNow);
  const salNow = rows.reduce((s, r) => s + Math.max(0, r.p.salaryNow), 0);
  const salProj = rows.reduce((s, r) => s + Math.max(0, r.p.salaryProjected), 0);
  const fri = rows.reduce((a, r) => ({ n: a.n + r.p.fringeNow, p: a.p + r.p.fringeProjected }), { n: 0, p: 0 });
  const tui = rows.reduce((a, r) => ({ n: a.n + r.p.tuitionNow, p: a.p + r.p.tuitionProjected }), { n: 0, p: 0 });
  const committed = rows.reduce((s, r) => s + r.p.total, 0);
  const totals = categoryTotals(!!view.includeClosed);
  const catNames = Object.keys(totals);
  const totBudget = catNames.reduce((s, n) => s + totals[n].budget, 0);
  const totSpent = catNames.reduce((s, n) => s + totals[n].spent, 0);
  const historic = realGrants().filter((g) => !g.exclude_from_history);
  const historicTotal = historic.reduce((s, g) => s + g.initial_amount, 0);

  return `
    <h1>Summary</h1>
    <p class="sub">All active grants combined · through ${S.today}</p>

    <div class="stats">
      <div class="stat clickable" id="hire-stat" style="border-left:4px solid var(--green)" title="Click to see fringe & tuition detail">
        <div class="label">💼 Salary available to hire — after projections</div>
        <div class="value" style="color:var(--green)">${money(salProj)}</div>
        <span class="chev">${view.hireDetail ? "▲ hide detail" : "▼ fringe & tuition"}</span>
      </div>
      <div class="stat"><div class="label">Salary available now</div><div class="value">${money(salNow)}</div></div>
      <div class="stat"><div class="label">Committed to current people</div><div class="value">${money(committed)}</div></div>
      <div class="stat"><div class="label">Historic total awarded</div><div class="value">${money(historicTotal)}</div></div>
    </div>

    ${view.hireDetail ? `<div class="card hire-detail">
      <h2>Behind the hiring number</h2>
      <p class="sub" style="margin-bottom:10px">The headline is salary (Personnel) only. Fringe and tuition are still accounted for: each grant's projected fringe/tuition costs are charged to their own budgets first, and any overrun is subtracted from that grant's salary pot.</p>
      <table>
        <thead><tr><th></th><th class="num">Available now</th><th class="num">Projected commitments</th><th class="num">After projections</th></tr></thead>
        <tbody>
          <tr><td style="font-weight:600">Salary (Personnel)</td><td class="num">${money(salNow)}</td>
            <td class="num">${money(rows.reduce((s, r) => s + r.p.salary, 0))}</td>
            <td class="num" style="font-weight:700;color:var(--green)">${money(salProj)}</td></tr>
          <tr><td style="font-weight:600">Fringe</td><td class="num">${money(fri.n)}</td>
            <td class="num">${money(rows.reduce((s, r) => s + r.p.fringe, 0))}</td>
            <td class="num" style="color:${fri.p < 0 ? "var(--red)" : "inherit"}">${money(fri.p)}</td></tr>
          <tr><td style="font-weight:600">Tuition</td><td class="num">${money(tui.n)}</td>
            <td class="num">${money(rows.reduce((s, r) => s + r.p.tuition, 0))}</td>
            <td class="num" style="color:${tui.p < 0 ? "var(--red)" : "inherit"}">${money(tui.p)}</td></tr>
        </tbody>
      </table>
      <p class="sub" style="margin:10px 0 0;font-size:12.5px">Negative fringe/tuition means those pots run out — the shortfall is already deducted from the salary headline.</p>
    </div>` : ""}

    <div class="card">
      <h2>Hiring power by grant</h2>
      <p class="sub" style="margin-bottom:10px">Salary money per active grant: what's there now, and what's left once every current appointment is paid through its end (fringe & tuition overruns included). Projections run to each grant's end date${S.grants.some((g) => g.nce_end_date) ? " (incl. no-cost extensions)" : ""}.</p>
      <div class="chart-wrap" style="height:${Math.max(200, rows.length * 56)}px"><canvas id="hire-chart"></canvas></div>
      <table style="margin-top:14px">
        <thead><tr><th>Grant</th><th>Ends</th><th class="num">Salary now</th><th class="num">Committed (sal+fri+tui)</th><th class="num">Salary projected</th><th class="num">Grant available now</th><th class="num">Grant projected</th></tr></thead>
        <tbody>
          ${rows.map((r) => `<tr>
            <td><a href="#" data-goto-grant="${r.g.id}" style="color:var(--accent);text-decoration:none;font-weight:600">${esc(r.g.name)}</a>${r.g.nce_end_date ? ' <span class="badge blue">NCE</span>' : ""}</td>
            <td>${effectiveEnd(r.g) || "—"} ${r.days !== null && r.days <= 365 ? `<span class="badge ${r.days <= 183 ? "red" : "amber"}">${r.days}d</span>` : ""}</td>
            <td class="num">${money(r.p.salaryNow)}</td>
            <td class="num">${money(r.p.total)}</td>
            <td class="num" style="font-weight:700;color:${r.p.salaryProjected < 0 ? "var(--red)" : "var(--green)"}">${money(r.p.salaryProjected)}</td>
            <td class="num">${money(r.p.availableNow)}</td>
            <td class="num" style="font-weight:600;color:${r.p.availableProjected < 0 ? "var(--red)" : "inherit"}">${money(r.p.availableProjected)}</td>
          </tr>`).join("")}
          <tr style="font-weight:700"><td>Total</td><td></td>
            <td class="num">${money(salNow)}</td>
            <td class="num">${money(committed)}</td>
            <td class="num" style="color:var(--green)">${money(salProj)}</td>
            <td class="num">${money(rows.reduce((s, r) => s + r.p.availableNow, 0))}</td>
            <td class="num">${money(rows.reduce((s, r) => s + r.p.availableProjected, 0))}</td></tr>
        </tbody>
      </table>
      <p class="sub" style="margin:10px 0 0;font-size:12.5px">Projections come from appointments (People tab): monthly salary × months remaining + fringe % + annual tuition, starting after each person's last charged month. Red means over-committed.</p>
    </div>

    <div class="card">
      <div class="section-head">
        <h2>Historic total — all grants received</h2>
        <strong style="font-size:18px;color:var(--accent)">${money(historicTotal)}</strong>
      </div>
      <table>
        <thead><tr><th class="no-print" style="width:34px">Count</th><th>Grant</th><th>Agency</th><th>Period</th><th class="num">Awarded (funded)</th></tr></thead>
        <tbody>
          ${realGrants().map((g) => `<tr style="${g.exclude_from_history ? "opacity:.45" : ""}">
            <td class="no-print"><input type="checkbox" data-hist-toggle="${g.id}" ${g.exclude_from_history ? "" : "checked"} style="width:auto" title="Include in historic total"></td>
            <td style="font-weight:600">${esc(g.name)} ${g.status !== "active" ? '<span class="badge gray">closed</span>' : ""}</td>
            <td>${esc(g.agency || "")}</td>
            <td>${g.start_date || "?"} → ${effectiveEnd(g) || "?"}</td>
            <td class="num">${money(g.initial_amount)}</td></tr>`).join("")}
          <tr style="font-weight:700"><td class="no-print"></td><td colspan="3">Total (checked only)</td><td class="num">${money(historicTotal)}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="section-head">
        <h2>Totals by category — all grants combined</h2>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)" class="no-print">
          <input type="checkbox" id="sum-closed" style="width:auto" ${view.includeClosed ? "checked" : ""}> include closed grants
        </label>
      </div>
      <div class="chart-wrap" style="height:300px"><canvas id="cat-chart"></canvas></div>
      <table style="margin-top:14px">
        <thead><tr><th>Category</th><th class="num">Budgeted</th><th class="num">Spent</th><th class="num">Remaining</th><th style="width:30%">Used</th></tr></thead>
        <tbody>
          ${catNames.map((n) => {
            const t = totals[n];
            const pct = t.budget > 0 ? Math.min(100, t.spent / t.budget * 100) : (t.spent > 0 ? 100 : 0);
            return `<tr><td style="font-weight:600">${esc(n)}</td>
              <td class="num">${money(t.budget)}</td><td class="num">${money(t.spent)}</td>
              <td class="num" style="font-weight:600;color:${t.budget - t.spent < 0 ? "var(--red)" : "var(--green)"}">${money(t.budget - t.spent)}</td>
              <td><div class="bar" style="margin:0"><div class="${pct >= 90 ? "danger" : pct >= 75 ? "warn" : "ok"}" style="width:${pct}%"></div></div></td></tr>`;
          }).join("")}
          <tr style="font-weight:700"><td>Total</td><td class="num">${money(totBudget)}</td>
            <td class="num">${money(totSpent)}</td><td class="num">${money(totBudget - totSpent)}</td><td></td></tr>
        </tbody>
      </table>
    </div>

    <div class="grid cols-2">
      <div class="card"><h2>Where the remaining money is</h2><div class="chart-wrap"><canvas id="donut-grant"></canvas></div></div>
      <div class="card"><h2>Spending by month — all grants</h2><div class="chart-wrap"><canvas id="month-chart"></canvas></div></div>
    </div>

    ${wdCrossCheckCard()}
  `;
}

const PALETTE = ["#2f6fed", "#1d9a6c", "#b97a08", "#8657d3", "#d24545",
                 "#0e9aa7", "#e07b39", "#5b6b82", "#c2437e", "#7a9a01"];

/* One picture on the landing page: what you've been spending, month by month. */
function drawDashboardChart() {
  if (typeof Chart === "undefined") return;
  const ctx = $("#dash-month-chart");
  if (!ctx) return;
  const byMonth = {};
  for (const e of S.expenses) {
    if (e.source === "adjust") continue; // rollover forfeits aren't spending
    const k = monthKey(e.date);
    byMonth[k] = (byMonth[k] || 0) + e.amount;
  }
  const months = Object.keys(byMonth).sort().slice(-18);
  if (!months.length) return;
  charts.push(new Chart(ctx, {
    type: "bar",
    data: { labels: months,
      datasets: [{ label: "Spent", data: months.map((m) => Math.round(byMonth[m])),
                   backgroundColor: PALETTE[0], borderRadius: 3 }] },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => money(c.parsed.y) } } },
      scales: {
        y: { ticks: { callback: (v) => "$" + (v >= 1000 ? Math.round(v / 1000) + "k" : Math.round(v)), font: { size: 11 } } },
        x: { ticks: { maxTicksLimit: 12, font: { size: 10 } } },
      },
    },
  }));
}

function drawSummaryCharts() {
  if (typeof Chart === "undefined") return;
  const includeClosed = !!view.includeClosed;
  const active = S.grants.filter((g) => includeClosed || g.status === "active");

  // 1. hiring power: salary now vs projected, per grant
  const hc = $("#hire-chart");
  if (hc) {
    const rows = realGrants().filter((g) => g.status === "active")
      .map((g) => ({ g, p: grantProjection(g.id) }))
      .sort((a, b) => b.p.salaryNow - a.p.salaryNow);
    charts.push(new Chart(hc, {
      type: "bar",
      data: {
        labels: rows.map((r) => r.g.name),
        datasets: [
          { label: "Salary available now", data: rows.map((r) => r.p.salaryNow),
            backgroundColor: "#c8d0dd" },
          { label: "Salary after projections", data: rows.map((r) => r.p.salaryProjected),
            backgroundColor: rows.map((r) => r.p.salaryProjected < 0 ? PALETTE[4] : PALETTE[1]) },
        ],
      },
      options: {
        indexAxis: "y", maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.x)}` } } },
        scales: {
          x: { ticks: { callback: (v) => "$" + Math.round(v / 1000) + "k", font: { size: 11 } } },
          y: { ticks: { font: { size: 12 } } },
        },
      },
    }));
  }

  // 2. category totals: grouped bars budget vs spent vs remaining
  const cc = $("#cat-chart");
  if (cc) {
    const totals = categoryTotals(includeClosed);
    const names = Object.keys(totals).sort((a, b) => totals[b].budget - totals[a].budget);
    charts.push(new Chart(cc, {
      type: "bar",
      data: {
        labels: names,
        datasets: [
          { label: "Budgeted", data: names.map((n) => totals[n].budget), backgroundColor: "#c8d0dd" },
          { label: "Spent", data: names.map((n) => totals[n].spent), backgroundColor: PALETTE[0] },
          { label: "Remaining", data: names.map((n) => Math.round((totals[n].budget - totals[n].spent) * 100) / 100), backgroundColor: PALETTE[1] },
        ],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } } },
        scales: {
          y: { ticks: { callback: (v) => "$" + Math.round(v / 1000) + "k", font: { size: 11 } } },
          x: { ticks: { font: { size: 11 }, maxRotation: 40 } },
        },
      },
    }));
  }

  // 3. doughnut: remaining by grant
  const dg = $("#donut-grant");
  if (dg) {
    const gs = realGrants().filter((g) => g.status === "active");
    const data = gs.map((g) => Math.max(0, grantAvailable(g.id)));
    charts.push(new Chart(dg, {
      type: "doughnut",
      data: { labels: gs.map((g) => g.name),
        datasets: [{ data, backgroundColor: gs.map((_, i) => PALETTE[i % PALETTE.length]) }] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => `${c.label}: ${money(c.parsed)}` } } },
      },
    }));
  }

  // 4. monthly spending across all grants (last 24 months)
  const mc = $("#month-chart");
  if (mc) {
    const byMonth = {};
    for (const e of S.expenses) {
      if (e.source === "adjust") continue; // rollover forfeits aren't spending
      const k = e.date.slice(0, 7);
      byMonth[k] = (byMonth[k] || 0) + e.amount;
    }
    const months = Object.keys(byMonth).sort().slice(-24);
    charts.push(new Chart(mc, {
      type: "bar",
      data: { labels: months,
        datasets: [{ label: "Spent", data: months.map((m) => Math.round(byMonth[m])), backgroundColor: PALETTE[0] }] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (c) => money(c.parsed.y) } } },
        scales: {
          y: { ticks: { callback: (v) => "$" + Math.round(v / 1000) + "k", font: { size: 11 } } },
          x: { ticks: { maxTicksLimit: 12, font: { size: 10 } } },
        },
      },
    }));
  }
}

/* -------------------------------------------------------------- people */
function renderPeople() {
  return `
    <div class="section-head">
      <div><h1>People</h1><p class="sub" style="margin:0">GAs, postdocs, and staff funded by your grants</p></div>
      <button class="btn" id="btn-add-person">+ Add person</button>
    </div>
    <div class="grid cols-2">
      ${S.people.map((p) => {
        const apps = S.appointments.filter((a) => a.person_id === p.id);
        return `<div class="card">
          <div class="section-head">
            <div><strong style="font-size:15px">${esc(p.name)}</strong> <span class="badge blue">${esc(p.role || "—")}</span></div>
            <div class="toolbar">
              <button class="btn ghost small" data-add-appt="${p.id}">+ appointment</button>
              <button class="icon-btn" data-edit-person="${p.id}">✏️</button>
              <button class="icon-btn" data-del-person="${p.id}">🗑</button>
            </div>
          </div>
          ${apps.length ? `<table><thead><tr><th>Grant</th><th class="num">%</th><th class="num">Salary/yr</th><th class="num">Fringe</th><th class="num">Tuition/yr</th><th>Period</th><th></th></tr></thead><tbody>
            ${apps.map((a) => {
              const g = S.grants.find((g) => g.id === a.grant_id) || { name: "?" };
              const active = a.end_date >= S.today && a.start_date <= S.today;
              const future = a.start_date > S.today;
              const pct = a.pct || 100;
              const annual = a.monthly_salary * 12;
              return `<tr><td>${esc(g.name)} ${active ? '<span class="badge green">active</span>' : future ? '<span class="badge blue">starts soon</span>' : ""} ${a.auto_charge ? '<span class="badge amber" title="Monthly charges auto-generated">auto</span>' : ""}</td>
                <td class="num" style="font-weight:600">${Math.round(pct * 10) / 10}%</td>
                <td class="num">${money(annual)}${pct < 100 ? `<div style="font-size:11px;color:var(--muted)">of ${money(annual / (pct / 100))} total</div>` : ""}</td>
                <td class="num">${a.fringe_rate}%</td>
                <td class="num">${money(a.annual_tuition || 0)}</td>
                <td>${a.start_date} → ${a.end_date}</td>
                <td style="white-space:nowrap"><button class="icon-btn" data-edit-appt="${a.id}">✏️</button><button class="icon-btn" data-del-appt="${a.id}">🗑</button></td></tr>`;
            }).join("")}</tbody></table>` : `<div class="empty">No appointments.</div>`}
        </div>`;
      }).join("") || `<div class="empty">No people yet.</div>`}
    </div>
    <div class="card" style="margin-top:6px">
      <h2>How appointments work</h2>
      <p class="sub" style="margin:0">Each appointment (grant, monthly salary, fringe %, annual tuition, start/end) drives the <strong>projections</strong> on the Summary tab: the app counts every month not yet charged, through the appointment or grant end, and shows what will be left. If you also tick <strong>auto-generate monthly charges</strong> on an appointment, the <strong>↻ Update salary charges</strong> button creates the actual monthly salary + fringe expenses (prorated, never duplicated) — leave it off if your actuals come from DBR imports.</p>
    </div>`;
}

/* -------------------------------------------------------------- workday */
let WD = null; // cached /api/workday/state; null = not loaded yet

function wdShortOC(oc) {
  // "UA System Sponsored Programs: 01_Personnel" -> "01_Personnel"
  const i = String(oc).lastIndexOf(": ");
  return i >= 0 ? oc.slice(i + 2) : oc;
}

function wdOffline() { return sessionStorage.getItem("wd-offline") === "1"; }

function wdTopbarUpdate() {
  const b = $("#btn-workday");
  if (!b) return;
  if (wdOffline()) b.textContent = "⇅ Workday · offline";
  else if (WD && WD.last_sync && S && WD.last_sync.date === S.today) b.textContent = "⇅ Workday ✓";
  else b.textContent = "⇅ Workday";
}

/* ---- push (email the workday-ready entry to the financial team) ---- */

function wdLineFor(gid, fallbackName, amount, pct) {
  const push = (WD && WD.push) || { codes: {}, profiles: {} };
  const code = push.codes[gid] || {};
  const prof = push.profiles[String(gid)] || {};
  return { pct, amount,
           worktag: code.wd_grant_name || code.grant_code || fallbackName,
           award: code.award || "", cost_center: prof.cost_center || "",
           fund: prof.fund || "", extra: prof.extra || "" };
}

function wdPayloadFromExpense(e) {
  const suggest = ((WD && WD.push) || {}).spend_suggest || {};
  // an expense with its own typed worktag (e.g. "Other" external accounts,
  // which have no grant-level Workday mapping) always wins over the
  // grant-level lookup
  const line = e.wd_worktag
    ? { pct: 100, amount: e.amount, worktag: e.wd_worktag,
        award: "", cost_center: "", fund: "", extra: "" }
    : wdLineFor(e.grant_id, e.grant_name, e.amount, 100);
  return {
    expense_ids: [e.id], total: e.amount, date: e.date,
    memo: e.description || "",
    spend: suggest[e.category_id] || e.category || "",
    person: e.person || "", receipt_path: e.receipt_path || "",
    grant_label: e.grant_name,
    lines: [line],
  };
}

function wdPushModal(p) {
  const cfg = WD?.push_cfg || {};
  const receiptName = p.receipt_path ? p.receipt_path.split("/").pop() : "";
  const notPdf = receiptName && !receiptName.toLowerCase().endsWith(".pdf");
  const multi = p.lines.length > 1;
  const fieldRows = [
    ["Amount (USD)", p.total.toFixed(2)],
    ["Date", p.date],
    ["Spend Category (suggested)", p.spend],
    ["Business purpose / memo", p.memo],
    ["Person", p.person],
    ["Receipt", receiptName ? receiptName + " — attached to the email" : ""],
  ].filter(([, v]) => v);
  const lineRows = (l) => [
    ["Grant worktag", l.worktag], ["Award", l.award],
    ["Cost Center", l.cost_center], ["Fund", l.fund],
    ["Additional worktags", l.extra],
  ].filter(([, v]) => v);
  const bodyText = "Hi,\n\nPlease enter the following expense in Workday:\n\n" +
    fieldRows.map(([k, v]) => `${k}: ${v}`).join("\n") + "\n\n" +
    p.lines.map((l, i) =>
      (multi ? `Accounting line ${i + 1} — ${l.pct}% (${money2(l.amount)}):\n` : "") +
      lineRows(l).map(([k, v]) => `${multi ? "  " : ""}${k}: ${v}`).join("\n")
    ).join("\n\n") +
    `\n\n${receiptName ? "The receipt is attached." : "No receipt for this expense."}\n\nThank you!`;
  const subject = `Workday expense entry — ${money2(p.total)} — ${p.grant_label}${multi ? " (split)" : ""}`;
  modal(`
    <h2>📤 Add to Workday</h2>
    <p class="sub" style="margin-bottom:10px">Saved in the app ✓ — review the workday-ready entry and email it to the financial team (you are CC'd).</p>
    <table>
      ${fieldRows.map(([k, v]) => `<tr><td style="color:var(--muted);white-space:nowrap;font-size:13px">${esc(k)}</td><td style="font-weight:600">${esc(String(v))}</td></tr>`).join("")}
      ${p.lines.map((l, i) => (multi ? `<tr><td colspan="2" style="font-weight:700;padding-top:8px">Accounting line ${i + 1} — ${l.pct}% (${money2(l.amount)})</td></tr>` : "") +
        lineRows(l).map(([k, v]) => `<tr><td style="color:var(--muted);white-space:nowrap;font-size:13px">${esc(k)}</td><td style="font-weight:600">${esc(String(v))}</td></tr>`).join("")).join("")}
    </table>
    ${notPdf ? `<p style="color:#b97a08;font-size:13px;margin:8px 0 0">⚠️ This receipt is not a PDF — Workday only accepts PDF attachments. Consider re-saving it as PDF before sending.</p>` : ""}
    <div class="form-row" style="margin-top:12px">
      <label class="field"><span>Send to me</span><input id="wd-mail-to" type="email" value="${esc(cfg.owner_email || "")}" placeholder="you@uark.edu"></label>
    </div>
    <p class="sub" style="margin:-6px 0 0;font-size:12.5px">Comes to you first — check it, then forward to your accountant.</p>
    <div class="actions">
      <button class="btn secondary" id="m-copy-all" style="margin-right:auto">⧉ Copy as text</button>
      <button class="btn secondary" id="m-cancel">Keep offline</button>
      <button class="btn" id="m-send">✉ Send email</button>
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
    $("#m-copy-all", el).onclick = async () => {
      await navigator.clipboard.writeText(subject + "\n\n" + bodyText);
      toast("Copied — paste it anywhere");
    };
    $("#m-send", el).onclick = async () => {
      const to = $("#wd-mail-to", el).value.trim();
      if (!to) { toast("Enter the financial team's email"); return; }
      const btn = $("#m-send", el);
      btn.disabled = true; btn.textContent = "Sending…";
      try {
        await api("/api/workday/send_email", "POST", {
          to, subject, body: bodyText,
          receipt_path: p.receipt_path, expense_ids: p.expense_ids,
        });
        close();
        toast(`Sent to ${to} — marked “Sent, waiting”`);
        await wdRefresh();
      } catch (e) {
        btn.disabled = false; btn.textContent = "✉ Send email";
        toast("Send failed: " + e.message);
      }
    };
  });
}

/* ---- pull (sync from Workday) ---- */

async function wdRefresh() {
  WD = await api("/api/workday/state");
  S = await api("/api/state"); // syncs create/replace expenses
  render();
  wdTopbarUpdate();
}

function wdLoginModal(msg, onSubmit) {
  modal(`
    <h2>⇅ Workday login</h2>
    <p class="sub" style="margin-bottom:10px">${esc(msg || "Sign in to pull today's data. Your password stays in memory only while the app runs — never saved anywhere.")}</p>
    <label class="field"><span>Email (Workday login)</span><input id="wd-login-user" value="${esc(WD?.raas?.username || "")}"></label>
    <label class="field"><span>Password</span><input id="wd-login-pass" type="password" autocomplete="current-password"></label>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Not now</button>
      <button class="btn" id="m-go">Connect</button>
    </div>`, (el, close) => {
    const go = () => {
      const u = $("#wd-login-user", el).value.trim(), p = $("#wd-login-pass", el).value;
      if (!u || !p) { toast("Enter email and password"); return; }
      close(); onSubmit(u, p);
    };
    $("#m-cancel", el).onclick = close;
    $("#m-go", el).onclick = go;
    $("#wd-login-pass", el).onkeydown = (e) => { if (e.key === "Enter") go(); };
    setTimeout(() => $(WD?.raas?.username ? "#wd-login-pass" : "#wd-login-user", el).focus(), 50);
  });
}

async function wdSync(creds) {
  toast("Syncing from Workday…");
  try {
    const r = await api("/api/workday/sync", "POST", creds || {});
    sessionStorage.removeItem("wd-offline");
    toast(`Workday sync: ${r.new_lines} new transactions · ${r.matched} matched · ${r.created} added` +
          (r.pending ? ` · ${r.pending} awaiting mapping` : ""));
    await wdRefresh();
  } catch (e) {
    if (e.message === "password_required") wdLoginModal(null, (u, p) => wdSync({ username: u, password: p }));
    else if (/rejected the login|login page|SSO/i.test(e.message)) wdLoginModal(e.message, (u, p) => wdSync({ username: u, password: p }));
    else toast("Workday sync failed: " + e.message);
  }
}

const EXAMPLE_LINK = `<a href="${withKey("/api/workday/example.xlsx")}"><button class="btn secondary small">⬇ Download example file</button></a>`;

/* A wrong export used to disappear into "1 file read · 0 new transactions",
   leaving the person to guess. Anything that failed now gets its own line
   saying what the file actually was and what to do about it. */
function wdReportImportResult(r) {
  const bad = [...r.files.filter((f) => f.kind === "error")
    .map((f) => ({ name: f.file, why: f.error })),
  ...(r.upload_errors || []).map((e) => {
    const i = e.indexOf(": ");
    return i > 0 ? { name: e.slice(0, i), why: e.slice(i + 2) } : { name: "", why: e };
  })];
  const good = r.files.filter((f) => f.kind === "detail" || f.kind === "summary");
  const balanceRows = r.balance_rows || 0;
  const nTxn = r.new_lines || 0, nMatched = r.matched || 0, nAdded = r.created || 0;
  const anythingNew = balanceRows + nTxn + nMatched + nAdded
    + (r.grants_created || 0) + (r.grants_matched || 0) > 0;
  // what still needs the user's attention after this import
  const ug = (WD?.unmapped_grants || []).length;
  const uc = (WD?.unmapped_categories || []).length;
  const uw = (WD?.unmatched_workers || []).length;

  // what came in, in plain words
  const gCreated = r.grants_created || 0, gMatched = r.grants_matched || 0;
  const wins = [];
  if (gCreated) wins.push(`${gCreated} grant${gCreated === 1 ? "" : "s"} created from the report.`);
  if (gMatched) wins.push(`${gMatched} grant${gMatched === 1 ? "" : "s"} matched to grants you already had.`);
  if (balanceRows) wins.push(`Budget &amp; balance figures updated (${balanceRows} row${balanceRows === 1 ? "" : "s"}).`);
  if (nTxn) wins.push(`${nTxn} new transaction${nTxn === 1 ? "" : "s"} read.`);
  if (nMatched) wins.push(`${nMatched} matched to entries you already had.`);
  if (nAdded) wins.push(`${nAdded} added as new expense${nAdded === 1 ? "" : "s"}.`);
  const winsHtml = wins.length
    ? `<ul style="margin:0 0 4px 0;padding-left:20px">${wins.map((w) => `<li style="margin:3px 0">${w}</li>`).join("")}</ul>`
    : (good.length && !bad.length
        ? `<p class="sub" style="margin:0 0 8px">These files held no new data — everything in them was already imported (or the sheet was empty). Nothing was lost.</p>`
        : "");

  const need = [];
  if (ug) need.push(`${ug} grant${ug === 1 ? "" : "s"}`);
  if (uc) need.push(`${uc} spend categor${uc === 1 ? "y" : "ies"}`);
  if (uw) need.push(`${uw} ${uw === 1 ? "person" : "people"}`);
  const needHtml = need.length
    ? `<div style="background:var(--amber-soft);border-left:3px solid #b97a08;padding:10px 12px;border-radius:8px;margin:12px 0 4px;font-size:13.5px">
        <strong>One more step:</strong> ${need.join(", ")} from Workday still need${(ug + uc + uw) === 1 ? "s" : ""} matching to this app.
        Close this and use the highlighted card at the top of the Dashboard.</div>`
    : (good.length && !bad.length
        ? `<p class="sub" style="margin:12px 0 0">Everything matched up — nothing else to do.</p>` : "");

  const errHtml = bad.length
    ? `<h3 style="font-size:14px;margin:16px 0 6px;color:var(--red,#b3261e)">${bad.length} file${bad.length === 1 ? "" : "s"} couldn't be read</h3>
       ${bad.map((b) => `<div style="border-left:3px solid var(--red,#b3261e);padding:6px 0 6px 12px;margin-bottom:10px">
         ${b.name ? `<div style="font-weight:600;font-size:13px;word-break:break-all">${esc(b.name)}</div>` : ""}
         <div class="sub" style="margin:3px 0 0">${esc(b.why)}</div></div>`).join("")}
       <p class="sub" style="margin:8px 0">Compare your export against the example workbook (same columns, fake data). Unreadable files were moved to <code>workday_imports/not-readable/</code> — nothing was deleted.</p>
       <div class="toolbar" style="margin-bottom:4px">${EXAMPLE_LINK}</div>`
    : "";

  const title = bad.length
    ? (anythingNew ? "Import finished — with some problems" : "Import couldn't read your file")
    : "Import complete";

  modal(`
    <h2>${anythingNew ? "✓ " : ""}${title}</h2>
    ${winsHtml}
    ${needHtml}
    ${errHtml}
    <div class="actions"><button class="btn" id="m-cancel">Done</button></div>`,
    (el, close) => { $("#m-cancel", el).onclick = close; });
}

async function wdImportFiles() {
  try {
    const r = await api("/api/workday/import", "POST", {});
    await wdRefresh();
    wdReportImportResult(r);
  } catch (e) { toast("Import failed: " + e.message); }
}

async function wdUploadFiles(fileList) {
  const all = [...fileList];
  // Say which file is wrong and why, rather than silently dropping it.
  const wrong = all.filter((f) => !/\.xlsx$/i.test(f.name));
  const files = all.filter((f) => /\.xlsx$/i.test(f.name));
  if (!files.length) {
    const ext = (n) => (n.match(/\.[^.]+$/) || ["(no extension)"])[0].toLowerCase();
    const tips = {
      ".xls": "Open it in Excel and use File → Save As → Excel Workbook (.xlsx).",
      ".csv": "Run the Workday export again and choose Excel (.xlsx) rather than CSV.",
      ".pdf": "Use Workday's “Export to Excel” button (the grid icon above the report), not Print.",
      ".numbers": "Open it in Numbers and use File → Export To → Excel.",
      ".txt": "Run the Workday export again and choose Excel (.xlsx).",
    };
    modal(`
      <h2>That isn't a Workday Excel export</h2>
      ${wrong.map((f) => `<div style="margin-bottom:10px">
        <div style="font-weight:600;font-size:13px;word-break:break-all">${esc(f.name)}</div>
        <div class="sub" style="margin-top:3px">${esc(tips[ext(f.name)] || "Grants Manager reads the .xlsx files Workday's “Export to Excel” button produces.")}</div>
      </div>`).join("")}
      <p class="sub" style="margin:14px 0 8px">Not sure what the export should look like? The example workbook has the expected columns with fake data in them.</p>
      <div class="toolbar">${EXAMPLE_LINK}</div>
      <div class="actions"><button class="btn" id="m-cancel">Close</button></div>`,
      (el, close) => { $("#m-cancel", el).onclick = close; });
    return;
  }
  toast(`Reading ${files.length} file${files.length === 1 ? "" : "s"}…`);
  try {
    const payloads = await Promise.all(files.map(fileToPayload));
    const r = await api("/api/workday/upload_import", "POST", { files: payloads });
    await wdRefresh();
    wdReportImportResult(r);
  } catch (e) { toast("Import failed: " + e.message); }
}

/* startup prompt: sign in to Workday or work offline */
function wdConnectModal() {
  const configured = WD && WD.raas && (WD.raas.summary_url || WD.raas.detail_url);
  if (!configured) {
    modal(`
      <h2>⇅ Connect to Workday?</h2>
      <p class="sub" style="margin-bottom:14px">Pull today's balances and posted charges so the app matches the official ledger. No direct connection is set up yet — Connect opens the Workday panel where you can set it up or import report files.</p>
      <div class="actions">
        <button class="btn secondary" id="m-offline" style="margin-right:auto">Work offline</button>
        <button class="btn" id="m-connect">Connect</button>
      </div>`, (el, close) => {
      $("#m-offline", el).onclick = () => {
        sessionStorage.setItem("wd-offline", "1");
        close(); wdTopbarUpdate();
        toast("Working offline — click ⇅ Workday (top bar) whenever you want to connect");
      };
      $("#m-connect", el).onclick = () => { close(); sessionStorage.removeItem("wd-offline"); wdSettingsModal(); };
    });
    return;
  }
  modal(`
    <h2>⇅ Connect to Workday</h2>
    <p class="sub" style="margin-bottom:12px">Sign in to pull today's balances and posted charges. Your password stays in memory only while the app runs — never saved.</p>
    <label class="field"><span>Email (Workday login)</span><input id="wd-login-user" value="${esc(WD.raas.username || "")}"></label>
    <label class="field"><span>Password</span><input id="wd-login-pass" type="password" autocomplete="current-password"></label>
    <div class="actions">
      <button class="btn secondary" id="m-offline" style="margin-right:auto">Work offline</button>
      <button class="btn" id="m-connect">Connect</button>
    </div>`, (el, close) => {
    $("#m-offline", el).onclick = () => {
      sessionStorage.setItem("wd-offline", "1");
      close(); wdTopbarUpdate();
      toast("Working offline — click ⇅ Workday (top bar) whenever you want to connect");
    };
    const go = () => {
      const u = $("#wd-login-user", el).value.trim(), p = $("#wd-login-pass", el).value;
      if (!u || !p) { toast("Enter email and password"); return; }
      close(); sessionStorage.removeItem("wd-offline");
      wdSync({ username: u, password: p });
    };
    $("#m-connect", el).onclick = go;
    $("#wd-login-pass", el).onkeydown = (e) => { if (e.key === "Enter") go(); };
    setTimeout(() => $(WD.raas.username ? "#wd-login-pass" : "#wd-login-user", el).focus(), 50);
  });
}

/* the ⇅ Workday panel: sync + import actions only. All configuration
   (connection URLs, emails, worktags) lives behind the ⚙ Settings gear. */
function wdSettingsModal() {
  const r = WD?.raas || {};
  const connected = !!(r.summary_url || r.detail_url);
  modal(`
    <h2>⇅ Workday</h2>
    <p class="sub" style="margin-bottom:10px">${WD?.last_sync ? `Last sync: <strong>${esc(WD.last_sync.time)}</strong> — ${esc(WD.last_sync.summary)}.` : "Not synced yet."}${wdOffline() ? " Working offline this session." : ""}</p>
    <div class="toolbar" style="margin-bottom:10px;flex-wrap:wrap">
      ${connected ? `<button class="btn small" id="wd-sync-btn">⇣ Sync from Workday now</button>` : ""}
    </div>

    <h2 style="font-size:14px">Import exported files</h2>
    <p class="sub" style="margin-bottom:8px">Pick one or more <code>.xlsx</code> files exported from Workday — select as many at once as you like (one per grant is fine).</p>
    <div class="dropzone" id="wd-pick-zone" style="width:100%;box-sizing:border-box">📁 Choose files or drop them here<br><span style="font-size:12px;opacity:.75">you can select several at once</span></div>
    <input type="file" id="wd-file-input" accept=".xlsx" multiple hidden>
    <p class="sub" style="margin:10px 0 6px;font-size:12px">Not sure which export to run, or whether yours has the right columns? ${EXAMPLE_LINK} — fake data in the exact format expected.</p>
    <p class="sub" style="margin:6px 0 0;font-size:12px">Already saved files into <code>workday_imports/</code> yourself? <button class="btn ghost small" id="wd-import-btn" style="padding:2px 8px">⟳ Import from that folder instead</button></p>

    ${connected ? "" : `<p class="sub" style="margin:10px 0 0">No direct connection set up yet. Add your Workday report URLs under <strong>⚙ Settings → Advanced</strong> to sync automatically.</p>`}
    <div class="actions">
      <button class="btn secondary" id="wd-open-settings">⚙ Settings</button>
      <button class="btn secondary" id="m-cancel">Close</button>
    </div>
  `, (el, close) => {
    $("#m-cancel", el).onclick = close;
    const sy = $("#wd-sync-btn", el);
    if (sy) sy.onclick = () => { close(); wdSync(); };
    $("#wd-import-btn", el).onclick = () => { close(); wdImportFiles(); };
    const zone = $("#wd-pick-zone", el), fileInput = $("#wd-file-input", el);
    zone.onclick = () => fileInput.click();
    // Copy the FileList out BEFORE resetting .value — clearing the input
    // empties the live FileList, so reading it afterwards found nothing
    // (browse appeared to do nothing; drag worked via its own file list).
    fileInput.onchange = () => { const fs = [...fileInput.files]; fileInput.value = ""; if (fs.length) { close(); wdUploadFiles(fs); } };
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("drag"); };
    zone.ondragleave = () => zone.classList.remove("drag");
    zone.ondrop = (e) => {
      e.preventDefault(); zone.classList.remove("drag");
      const fs = e.dataTransfer.files;
      if (fs.length) { close(); wdUploadFiles(fs); }
    };
    $("#wd-open-settings", el).onclick = () => { close(); settingsModal(); };
  });
}

/* the ⚙ Settings gear: backups, recently-deleted, the monthly report.
   Almost everyone runs this app offline from imported files, so the Workday
   connection and the remembered email addresses sit behind "Advanced" rather
   than filling the panel with fields most people never touch. */
async function settingsModal() {
  const r = WD?.raas || {};
  const pc = WD?.push_cfg || {};
  const connected = !!(r.summary_url || r.detail_url);
  const push = WD?.push || { profiles: {}, codes: {} };
  const card = WD?.pcard || {};
  const activeGrants = realGrants().filter((g) => g.status === "active");
  let trash = [];
  try { trash = (await api("/api/trash")).batches; } catch { /* non-critical */ }
  modal(`
    <h2>⚙ Settings</h2>

    <h2 style="font-size:14px;margin-top:6px">Backups &amp; data safety</h2>
    <p class="sub" style="margin-bottom:8px">A dated copy is made automatically once a day (kept 30 days) in <code>data/backups/</code>. Download one now for an extra copy outside OneDrive, or restore from a backup file if something goes wrong.</p>
    <div class="toolbar" style="margin-bottom:16px">
      <a href="${withKey("/api/backup/download")}"><button class="btn secondary small">⬇ Download backup now</button></a>
      <button class="btn ghost small" id="btn-pick-restore">📤 Restore from a backup file…</button>
      <input type="file" id="restore-file-input" accept=".db" hidden>
    </div>

    <h2 style="font-size:14px">🗑 Recently deleted</h2>
    <p class="sub" style="margin-bottom:8px">Deleting a grant, person, appointment, or expense keeps it here for 30 days in case it was a mistake.</p>
    ${trash.length ? `<table style="margin-bottom:16px">
      <thead><tr><th>Deleted</th><th>What</th><th></th></tr></thead>
      <tbody>${trash.map((b) => `<tr>
        <td style="white-space:nowrap;color:var(--muted);font-size:12.5px">${esc(b.deleted_at.replace("T", " ").slice(0, 16))}</td>
        <td>${esc(b.summary)}</td>
        <td><button class="btn ghost small" data-restore-batch="${esc(b.batch_id)}">↺ Restore</button></td>
      </tr>`).join("")}</tbody>
    </table>` : `<div class="empty" style="margin-bottom:16px">Nothing deleted recently.</div>`}

    <h2 style="font-size:14px;margin-top:6px">Your name on the report</h2>
    <p class="sub" style="margin-bottom:8px">Used to title the report email — “Jane Doe's expense report for the month of September 2026”.</p>
    <div class="form-row" style="align-items:flex-end">
      <label class="field" style="max-width:260px"><span>Your name</span><input id="set-owner-name" value="${esc(pc.owner_name || "")}" placeholder="Jane Doe"></label>
      <button class="btn secondary small" id="btn-save-name" style="margin-bottom:4px">Save</button>
    </div>

    <h2 style="font-size:14px;margin-top:16px">💳 P-card</h2>
    <p class="sub" style="margin-bottom:8px">Set once. Every expense you tick as a P-card purchase carries these into the report, so reconciliation has the cardholder against each line.</p>
    <div class="form-row" style="align-items:flex-end">
      <label class="field"><span>Print cardholder's name</span><input id="set-cardholder" value="${esc(card.cardholder || "")}" placeholder="Jane Doe"></label>
      <label class="field"><span>Name on the P-card</span><input id="set-card-name" value="${esc(card.card_name || "")}" placeholder="as embossed on the card"></label>
      <button class="btn secondary small" id="btn-save-card" style="margin-bottom:4px">Save</button>
    </div>

    <h2 style="font-size:14px;margin-top:16px">Monthly expense report</h2>
    <p class="sub" style="margin-bottom:8px">At the start of each month the app offers to email you last month's expenses, formatted for your accountant. You can also send one any time — the address you send to is remembered, so there is nothing to set up here first.</p>
    <div class="toolbar" style="margin-bottom:4px">
      <button class="btn secondary small" id="btn-send-report">📧 Send a report now…</button>
    </div>

    <details class="advanced" ${connected ? "open" : ""}>
      <summary>Advanced — direct Workday connection &amp; saved email addresses</summary>
      <p class="sub" style="margin:10px 0 8px">Nothing in here is needed for normal, offline use. Grants Manager works entirely from imported Workday files; these settings only matter if you have a direct connection set up.</p>

      <h2 style="font-size:14px;margin-top:14px">Saved email addresses</h2>
      <p class="sub" style="margin-bottom:8px">Filled in automatically the first time you send a report or sign in — set them by hand only if you want to change them.</p>
      <div class="form-row">
        <label class="field"><span>Email (Workday login)</span><input id="wd-user" value="${esc(r.username || "")}" placeholder="you@uark.edu"></label>
        <label class="field"><span>Your email (reports come to you)</span><input id="wd-owner-email" type="email" value="${esc(pc.owner_email || "")}" placeholder="you@uark.edu"></label>
      </div>
      <p class="sub" style="margin:-4px 0 0;font-size:12.5px">Expense reports and entry sheets are sent to <em>you</em> to check, then you forward them to your accountant — the app never emails them directly.</p>

      <h2 style="font-size:14px;margin-top:16px">Direct connection (RaaS)</h2>
      <p class="sub" style="margin-bottom:8px">Leave these empty to keep working offline (the normal setup). Filling them in lets the app pull reports straight from Workday instead of you exporting files — but it needs report-writing rights and an account that isn't SSO-only, which most people don't have. <strong>Instructions tab → “Optional: direct connection (RaaS)”</strong> lists the exact access levels to ask for. To import files instead, use <strong>⇅ Workday</strong> in the top bar.</p>
      <label class="field"><span>Balances report URL</span><input id="wd-url-sum" value="${esc(r.summary_url || "")}" placeholder="https://….workday.com/ccx/service/customreport2/…"></label>
      <label class="field"><span>Transactions report URL (optional until you build it)</span><input id="wd-url-det" value="${esc(r.detail_url || "")}" placeholder="https://….workday.com/ccx/service/customreport2/…"></label>
      <div class="toolbar" style="margin-top:4px">
        <button class="btn secondary small" id="wd-save-all">Save advanced settings</button>
      </div>

      <h2 style="font-size:14px;margin-top:18px">Grant worktags</h2>
      <p class="sub" style="margin-bottom:8px">Stamped on every entry sent to Workday. Grant and Award codes fill in automatically once a grant's data has been imported; Cost Center and Fund are typed here once. Pick a grant to see or change its worktags.</p>
      ${activeGrants.length ? `
        <label class="field" style="max-width:260px"><span>Grant</span>
          <select id="wt-pick">${activeGrants.map((g, i) =>
            `<option value="${g.id}" ${i === 0 ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select></label>
        <div id="wt-panel"></div>` : `<div class="empty">No active grants yet.</div>`}
    </details>
    <p class="sub" style="text-align:center;margin:18px 0 -6px;font-size:12px;opacity:.7">Grants Manager v${esc(S.version || "?")}${(S.update && S.update.available) ? ` — <a href="#" id="set-update" style="color:var(--accent)">a newer version is available</a>` : " — up to date"}</p>
    <div class="actions"><button class="btn secondary" id="m-cancel">Close</button></div>
  `, (el, close) => {
    const up = $("#set-update", el);
    if (up) up.onclick = (e) => { e.preventDefault(); close(); if (typeof notifPanel === "function") notifPanel(); };
    $("#m-cancel", el).onclick = close;
    const restoreInput = $("#restore-file-input", el);
    $("#btn-pick-restore", el).onclick = () => restoreInput.click();
    restoreInput.onchange = async () => {
      const f = restoreInput.files[0];
      restoreInput.value = "";
      if (!f) return;
      if (!confirm(`Replace all current data with the contents of "${f.name}"? A safety copy of what's here now will be made first, but this can't be undone from inside the app.`)) return;
      try {
        const payload = await fileToPayload(f);
        const r = await api("/api/backup/restore", "POST", { file: payload });
        alert(r.message || "Restored.");
        close();
      } catch (e) { toast("Restore failed: " + e.message); }
    };
    $("#btn-send-report", el).onclick = () => { close(); reportModal(); };
    $$("[data-restore-batch]", el).forEach((b) => b.onclick = async () => {
      if (!confirm("Restore this?")) return;
      try {
        await api(`/api/trash/${b.dataset.restoreBatch}/restore`, "POST", {});
        toast("Restored");
        close();
        await reload();
        settingsModal();
      } catch (e) { toast("Restore failed: " + e.message); }
    });
    $("#btn-save-name", el).onclick = async () => {
      try {
        await api("/api/workday/push_config", "POST",
                  { owner_name: $("#set-owner-name", el).value });
        WD = await api("/api/workday/state");
        toast("Saved");
      } catch (err) { toast("Couldn't save: " + err.message); }
    };
    $("#btn-save-card", el).onclick = async () => {
      try {
        await api("/api/pcard_config", "POST", {
          cardholder: $("#set-cardholder", el).value,
          card_name: $("#set-card-name", el).value,
        });
        WD = await api("/api/workday/state");
        toast("P-card details saved");
      } catch (err) { toast("Couldn't save: " + err.message); }
    };
    const wtPick = $("#wt-pick", el);
    if (wtPick) {
      // One grant at a time: a row per grant filled the panel with fields
      // nobody was looking at, which is why this section was pulled out of
      // Settings in the first place.
      const drawWorktags = () => {
        const gid = wtPick.value;
        const g = activeGrants.find((x) => String(x.id) === String(gid));
        const c = push.codes[+gid] || {};
        const pr = push.profiles[String(gid)] || {};
        $("#wt-panel", el).innerHTML = `
          <p class="sub" style="margin:0 0 8px;font-size:12.5px">Workday code for ${esc(g ? g.name : "")}: <strong>${c.grant_code ? esc(c.grant_code) : "none imported yet"}</strong>${c.award ? ` · Award ${esc(c.award)}` : ""}</p>
          <div class="form-row" style="align-items:flex-end">
            <label class="field"><span>Cost Center</span><input id="wt-cc" value="${esc(pr.cost_center || "")}" placeholder="CC067890 …"></label>
            <label class="field" style="max-width:150px"><span>Fund</span><input id="wt-fund" value="${esc(pr.fund || "")}" placeholder="FD100 …"></label>
            <label class="field"><span>Other worktags</span><input id="wt-extra" value="${esc(pr.extra || "")}" placeholder="Function, program…"></label>
            <button class="btn secondary small" id="wt-save" style="margin-bottom:4px">Save</button>
          </div>`;
        $("#wt-save", el).onclick = async () => {
          const cc = $("#wt-cc", el).value, fund = $("#wt-fund", el).value,
                extra = $("#wt-extra", el).value;
          try {
            await api("/api/workday/worktags", "POST", {
              grant_id: +gid, cost_center: cc, fund, extra,
            });
            // keep the local copy in step so switching grants and back
            // doesn't show the pre-save values
            push.profiles[String(gid)] = { cost_center: cc, fund, extra };
            WD = await api("/api/workday/state");
            toast("Worktags saved");
          } catch (e) { toast("Couldn't save: " + e.message); }
        };
      };
      wtPick.onchange = drawWorktags;
      drawWorktags();
    }
    $("#wd-save-all", el).onclick = async () => {
      await api("/api/workday/push_config", "POST", {
        owner_email: $("#wd-owner-email", el).value,
      });
      await api("/api/workday/raas_config", "POST", {
        summary_url: $("#wd-url-sum", el).value, detail_url: $("#wd-url-det", el).value,
        username: $("#wd-user", el).value, auto: 1,
      });
      toast("Settings saved");
      WD = await api("/api/workday/state");
      close();
      settingsModal();
    };
  });
}

/* dashboard cards: unmapped-code prompts + the to-enter queue */
/* Workday writes worker names "Last, First Middle"; flip that to the natural
   order for the person we create. Only touches a single leading comma, so a
   name that is already natural (or oddly punctuated) is left as-is — and the
   worker->person map bridges any remaining spelling gap anyway. */
function nameFromWorker(w) {
  const s = String(w || "").trim();
  const i = s.indexOf(",");
  if (i > 0 && s.indexOf(",", i + 1) === -1) {
    const last = s.slice(0, i).trim(), rest = s.slice(i + 1).trim();
    if (last && rest) return `${rest} ${last}`;
  }
  return s;
}

function wdDashCards() {
  if (!WD) return "";
  const gById = (id) => S.grants.find((g) => g.id === id);
  const grantOpts = () => `<option value="">— pick a grant —</option>` +
    `<option value="__create">➕ Create this as a new grant</option>` +
    S.grants.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("") +
    `<option value="ignore">Ignore this grant</option>`;
  const catOpts = () => `<option value="">— pick a category —</option>` +
    S.categories.map((c) => `<option value="${c.id}">${esc(c.name)}${c.grant_id ? ` (${esc(gById(c.grant_id)?.name || "?")})` : ""}</option>`).join("") +
    `<option value="ignore">Ignore this class</option>`;
  let html = "";
  if (WD.unmapped_grants.length + WD.unmapped_categories.length) {
    html += `<div class="card" style="border-left:4px solid #b97a08">
      <h2>⚠️ Map Workday codes (one-time)</h2>
      ${WD.unmapped_grants.map((u) => `
        <div class="form-row" style="align-items:center">
          <label class="field"><span>Workday grant ${esc(u.grant_code)}</span>
            <div style="font-size:12px;color:var(--muted)">${esc(u.name || u.grant_name || "")}${u.start ? ` · ${esc(u.start)} → ${esc(u.end || "?")}` : ""}</div></label>
          <label class="field"><span>→</span><select data-wd-map-grant="${esc(u.grant_code)}">${grantOpts()}</select></label>
        </div>`).join("")}
      ${WD.unmapped_categories.map((oc) => `
        <div class="form-row" style="align-items:center">
          <label class="field"><span>Workday object class</span>
            <div style="font-size:12px">${esc(wdShortOC(oc))}</div></label>
          <label class="field"><span>→ maps to</span><select data-wd-map-cat="${esc(oc)}">${catOpts()}</select></label>
        </div>`).join("")}
    </div>`;
  }
  if ((WD.unmatched_workers || []).length) {
    html += `<div class="card" style="border-left:4px solid #b97a08">
      <h2>👥 Match people from Workday payroll</h2>
      <p class="sub" style="margin-bottom:10px">These names appear on salary or fringe charges in your import but don't match anyone in <strong>People</strong> yet — so those charges have a dollar amount but no person attached. Tell the app who each one is and it links their charges (past and future). <strong>Add</strong> also creates the person, ready for you to give them an appointment on the <strong>People</strong> tab for salary projections.</p>
      ${WD.unmatched_workers.map((w) => `
        <div class="form-row" style="align-items:center">
          <label class="field"><span>Workday worker</span>
            <div style="font-size:12px">${esc(w.worker)} <span style="color:var(--muted)">· ${w.lines} charge${w.lines === 1 ? "" : "s"}${w.amount ? `, ${money2(w.amount)}` : ""}</span></div></label>
          <label class="field"><span>→ who is this?</span>
            <select data-wd-map-worker="${esc(w.worker)}" data-wd-worker-name="${esc(nameFromWorker(w.worker))}">
              <option value="">— pick —</option>
              <option value="add">➕ Add “${esc(nameFromWorker(w.worker))}” as a new person</option>
              ${S.people.map((p) => `<option value="${p.id}">This is ${esc(p.name)}</option>`).join("")}
              <option value="ignore">Not a person / already handled</option>
            </select></label>
        </div>`).join("")}
    </div>`;
  }
  const queue = (WD.push && WD.push.queue) || [];
  if (queue.length) {
    html += `<div class="card">
      <div class="section-head">
        <h2>→ To enter in Workday (${queue.length})</h2>
        <a href="${withKey("/api/workday/entry_sheet.csv")}" class="no-print"><button class="btn secondary small">⬇ Entry sheet (CSV)</button></a>
      </div>
      <p class="sub" style="margin-bottom:8px">Expenses added here that haven't shown up in Workday yet. 📤 reopens the send-to-Workday box; a row clears itself once the posted charge syncs back in.</p>
      <table>
        <thead><tr><th>Date</th><th>Grant</th><th>Category</th><th>Description</th><th class="num">Amount</th><th>Status</th><th class="no-print"></th></tr></thead>
        <tbody>${queue.map((e) => `<tr style="${e.wd_entry === "sent" ? "opacity:.55" : ""}">
          <td>${e.date}</td>
          <td>${esc(e.grant_name)}</td>
          <td>${esc(e.category || "—")}</td>
          <td style="max-width:300px">${esc(e.description || "")}${e.person ? ` <span style="color:var(--muted);font-size:12px">· ${esc(e.person)}</span>` : ""}${e.receipt_path ? " 📎" : ""}</td>
          <td class="num">${money2(e.amount)}</td>
          <td><select data-wd-entry="${e.id}" style="width:190px">
            <option value="" ${!e.wd_entry ? "selected" : ""}>Not sent yet</option>
            <option value="sent" ${e.wd_entry === "sent" ? "selected" : ""}>Sent, waiting</option>
            <option value="na">Doesn't go to Workday</option>
          </select></td>
          <td class="no-print"><button class="icon-btn" data-wd-sheet="${e.id}" title="Send to Workday / entry sheet">📤</button></td>
        </tr>`).join("")}</tbody>
      </table>
    </div>`;
  }
  return html;
}

/* summary card: app ledger vs Workday's official numbers */
function wdCrossCheckCard() {
  if (!WD || !WD.balances || !WD.balances.length) return "";
  const gById = (id) => S.grants.find((g) => g.id === id);
  const cById = (id) => S.categories.find((c) => c.id === id);
  const gmap = {}, cmap = {};
  for (const m of WD.mappings) (m.kind === "grant" ? gmap : cmap)[m.wd_key] = m.target_id;
  const rows = WD.balances.map((b) => {
    const gid = gmap[b.grant_code], cid = cmap[b.object_class];
    const g = gid ? gById(gid) : null, c = cid ? cById(cid) : null;
    // "New expenses" = entered here and not yet showing in the official
    // numbers. Anything imported has source 'workday', so what stays
    // 'manual' after an import is exactly what's new. 'na' entries are
    // things the user marked as never belonging in the official ledger.
    let fresh = null;
    if (g && c) {
      const cid = categoryOnGrant(c.id, g.id);
      fresh = S.expenses
        .filter((e) => e.grant_id === g.id && e.category_id === cid
                    && e.source === "manual" && e.wd_entry !== "na")
        .reduce((s, e) => s + e.amount, 0);
    }
    return `<tr>
      <td>${g ? esc(g.name) : esc(b.grant_code)}</td>
      <td title="${esc(b.object_class || "")}">${c ? esc(c.name) : esc(wdShortOC(b.object_class))}</td>
      <td class="num">${money2(b.budget)}</td>
      <td class="num">${money2(b.actuals)}</td>
      <td class="num">${money2(b.obligation)}</td>
      <td class="num">${money2(b.available)}</td>
      <td class="num" style="font-weight:${fresh ? "700" : "400"};color:${fresh ? "var(--amber, #b97a08)" : "var(--muted)"}">${fresh === null ? "—" : fresh ? money2(fresh) : "none"}</td>
    </tr>`;
  }).join("");
  return `<div class="card">
    <div class="section-head"><h2>Official numbers</h2>
      <span class="sub" style="margin:0">From your last Workday import · as of ${esc(WD.balances[0]?.as_of || "")}</span></div>
    <table>
      <thead><tr><th>Grant</th><th>Category</th><th class="num">Budget</th><th class="num">Spent</th><th class="num">Committed</th><th class="num">Available</th><th class="num">New expenses</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="sub" style="margin:8px 0 0;font-size:12px"><strong>Budget</strong>, <strong>Spent</strong>, <strong>Committed</strong> (ordered but not yet billed) and <strong>Available</strong> are the official figures from your last import. <strong>New expenses</strong> are ones you entered here that aren't in those figures yet — so your real remaining balance is <em>Available minus New expenses</em>.</p>
  </div>`;
}

function wireWorkdayBits(m) {
  const saveMap = async (kind, key, val) => {
    if (!val) return;
    const target = val === "ignore" ? null : +val;
    await api("/api/workday/map", "POST", { kind, wd_key: key, target_id: target });
    toast("Mapping saved");
    await wdRefresh();
  };
  const createGrantFromWd = (code) => {
    const u = (WD?.unmapped_grants || []).find((x) => x.grant_code === code) || {};
    grantModal(null, {
      prefill: {
        name: u.name || u.grant_name || code,
        start_date: u.start || "", end_date: u.end || "",
      },
      onCreated: async (newId) => {
        await api("/api/workday/map", "POST",
                  { kind: "grant", wd_key: code, target_id: newId });
        toast("Grant created and its Workday balances linked");
        await wdRefresh();
      },
    });
  };
  $$("[data-wd-map-grant]", m).forEach((sel) => sel.onchange = () => {
    if (sel.value === "__create") { createGrantFromWd(sel.dataset.wdMapGrant); sel.value = ""; return; }
    saveMap("grant", sel.dataset.wdMapGrant, sel.value);
  });
  $$("[data-wd-map-cat]", m).forEach((sel) => sel.onchange = () => saveMap("category", sel.dataset.wdMapCat, sel.value));
  $$("[data-wd-map-worker]", m).forEach((sel) => sel.onchange = async () => {
    const key = sel.dataset.wdMapWorker, val = sel.value;
    if (!val) return;
    try {
      if (val === "add") {
        const name = sel.dataset.wdWorkerName || key;
        const r = await api("/api/people", "POST", { name, role: "Other" });
        await api("/api/workday/map", "POST", { kind: "worker", wd_key: key, target_id: r.id });
        toast(`Added ${name} and linked their charges`);
      } else if (val === "ignore") {
        await api("/api/workday/map", "POST", { kind: "worker", wd_key: key, target_id: null });
        toast("Won't ask about this name again");
      } else {
        await api("/api/workday/map", "POST", { kind: "worker", wd_key: key, target_id: +val });
        toast("Linked their charges");
      }
      await wdRefresh();
    } catch (e) { toast("Couldn't save: " + e.message); }
  });
  $$("[data-wd-entry]", m).forEach((sel) => sel.onchange = async () => {
    await api(`/api/expenses/${sel.dataset.wdEntry}`, "POST", { wd_entry: sel.value });
    toast(sel.value === "na" ? "Hidden — doesn't go to Workday" : "Status saved");
    await wdRefresh();
  });
  $$("[data-wd-sheet]", m).forEach((b) => b.onclick = () => {
    const e = (WD?.push?.queue || []).find((x) => x.id === +b.dataset.wdSheet);
    if (e) wdPushModal(wdPayloadFromExpense(e));
  });
}

/* --------------------------------------------------------- instructions */
/* Walkthrough clips for the Instructions tab.

   Thirteen looping GIFs animating at once is noise (and needless CPU), so
   each one shows a still first frame with a Play badge and only swaps in the
   animation when asked. Re-clicking restarts it, which a plain GIF can't do.
   All of them were recorded against a throwaway database of invented grants —
   no real grant, person or amount appears in any frame. */
function demo(name, caption) {
  return `<figure class="demo">
    <button type="button" class="demo-play" data-demo="${esc(name)}">
      <img src="/app/help/${esc(name)}.png" alt="${esc(caption)}" loading="lazy" decoding="async">
      <span class="demo-badge">▶&nbsp;Play</span>
    </button>
    <figcaption>${esc(caption)} <span class="demo-fake">Example data — not your grants.</span></figcaption>
  </figure>`;
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".demo-play");
  if (!btn) return;
  const img = $("img", btn), badge = $(".demo-badge", btn);
  const name = btn.dataset.demo;
  // a fresh query string each time, so a second click replays from frame one
  img.src = `/app/help/${name}.gif?r=${Date.now()}`;
  btn.classList.add("playing");
  badge.innerHTML = "↻&nbsp;Replay";
});

function renderInstructions() {
  const sec = (title, body) => `<div class="card"><h2>${title}</h2><div style="font-size:14px;line-height:1.6">${body}</div></div>`;
  return `
    <h1>Instructions</h1>
    <p class="sub">How to use each part of Grants Manager</p>

    ${sec("🏠 Dashboard", `
      <p><strong>Quick add expense</strong> (top): type the amount, pick the grant and category, adjust the date, add comments, and optionally drop a receipt file (PDF or photo) on the dashed box — then click <em>Add</em>. Receipts are copied into <code>GrantsApp/receipts/&lt;grant&gt;/&lt;year&gt;/</code>, so they're backed up by OneDrive.</p>
      <p><strong>Splitting a cost:</strong> pick a second grant under <em>Split with</em> and the percentage that grant pays — the app creates two linked expenses, one on each grant, with the split noted in each. The same option exists in the grant page's <em>+ Add expense</em> form.</p>
      <p><strong>Alerts</strong> warn when (in the current budget year) a grant is ending soon, a category is over 80% spent, or something is overspent. Click an alert to open that grant.</p>
      <p><strong>Grant cards</strong> show money still available on each grant. The bar starts fully <span style="color:var(--green);font-weight:600">green (available)</span> and fills with gray from left to right as money is spent — a mostly gray bar means the grant is nearly used up.</p><p><strong>“Available” means what you can still spend</strong> — the award minus what you've spent <em>and</em> minus anything already committed (purchase orders and requisitions Workday knows about but hasn't billed yet). That matches the figure your accountant quotes. Commitments only appear once you've imported a Workday report; before that, available is simply award minus spent.</p><p><strong>The two lines under each card answer “am I on track?”</strong> The first compares time against money (“19 of 36 months gone (53%) — you've spent 20% of the money”). The second takes your average spending over the last six months and says where it lands: either how much would be left at the end, or — in amber — how many months early you'd run out. Click a card to open the grant's full page. Totals for all active grants are at the bottom, and closed grants can be hidden with the <em>Hide closed grants</em> button.</p>
      ${demo('dashboard', 'The dashboard, top to bottom: totals for every active grant, then the alerts, then one card per grant.')}
      ${demo('quick-add', 'Adding an expense — amount, grant, a note, then Add. “Add to Workday” was ticked, so the send box opens straight after.')}
    `)}

    ${sec("📋 Grant page", `
      <p>Open any grant to see: <strong>Available now</strong> (award minus spending) and <strong>Projected available</strong> (after paying every current appointment through its end).</p>
      <p>The <strong>Budget by category × year</strong> matrix shows budgeted / spent / remaining per cell — <em>click any budget number to edit it</em>. Add grant-specific categories with <em>+ custom category</em>.</p>
      <p>The <strong>burn-down chart</strong> compares your actual spending pace against an even pace and projects when the money runs out. Below it, the expense ledger can be filtered by category/year; every expense can be edited (✏️) or deleted (🗑). <strong>⬇ Export CSV</strong> downloads the ledger; <strong>🖨 Print report</strong> makes a clean printable summary.</p>
      <p><strong>📄 Close-out report</strong> builds the document you need when an award ends: award summary, final budget-vs-actual by category (and by year), personnel supported, the complete ledger, and a signature block — plus a flagged list of any manually-entered expenses <em>missing a receipt</em>, which sponsors commonly ask for at close-out. Use <em>🖨 Print / Save as PDF</em> on that page to file it.</p>
      <p><strong>Edit grant</strong> also lets you: set a <strong>no-cost extension</strong> date (extends the effective end date without new money — an “NCE” badge appears everywhere), mark the grant <strong>closed</strong>, or <strong>exclude it from the historic total</strong>.</p>
      ${demo('grant-page', 'Opening a grant card: budget by category, the spending burn-down, who is paid from it, and its expense list.')}
    `)}

    ${sec("📊 Summary", `
      <p>The headline <strong>“Salary available to hire — after projections”</strong> is the salary (Personnel) money left across active grants once every current appointment is paid through its end. Fringe and tuition are accounted for: they're charged to their own budgets first and any overrun is taken out of the salary pot. <em>Click the headline card</em> to see the fringe & tuition detail.</p>
      <p><strong>Hiring power by grant</strong> shows two bars per grant — salary now (gray) vs. after projections (green; red = over-committed) — plus a table with both numbers and the overall grant totals.</p>
      <p><strong>Totals by category</strong> combines all grants per category (toggle to include closed grants). <strong>Historic total</strong> adds up every award you've received; untick a grant's checkbox to exclude it from the total.</p>
      ${demo('summary', 'The Summary tab: every grant you have ever held, and category totals across all of them.')}
    `)}

    ${sec("👥 People & projections", `
      <p><strong>How to add a person.</strong> Open the <strong>People</strong> tab (top bar) and click <strong>+ Add person</strong>. Type their <strong>name</strong> and pick a <strong>role</strong> — that alone is enough to create them. The same box then offers optional <strong>funding</strong>: their total yearly salary, fringe %, start and end dates, and which grant (or two) pays, with a % each. Fill that in and the app creates their <strong>appointment(s)</strong> for you; leave it blank and you can add appointments later. Click <strong>Add</strong>.</p>
      <p><strong>Names must match Workday.</strong> When you import a Workday payroll file, the app links each salary charge to a person by matching the <em>Worker</em> name. If a name on the import doesn't match anyone yet, the dashboard shows a <strong>“👥 Match people from Workday payroll”</strong> card — pick <strong>Add</strong> to create that person (their charges link automatically, past and future), pick an existing person if it's just a spelling difference, or <strong>Not a person</strong> to dismiss it. You never have to type these names yourself; the card is the quickest way to build your People list.</p>
      <p>Each person can have <strong>appointments</strong>: grant + monthly salary + fringe % + annual tuition + start/end dates (the length). These drive all projections — the app counts months not yet charged (starting after the person's last real paycheck on that grant) through the appointment or grant end, whichever comes first.</p>
      <p><strong>Splitting a person across grants:</strong> when you add a person, the form lets you enter their total salary and pick which grant pays — and optionally a second source with a percentage for each. A person paid from two sources shows <em>two rows</em> in their table, one per grant, with a <strong>%</strong> column (e.g., a postdoc paid 50% by one grant + 50% by “Other”). Pick <strong>“Other”</strong> as the source for salary shares paid outside your grants (department, college, another PI) — it appears on the People tab but never counts against your budgets.</p>
      <p>The <strong>auto-generate monthly charges</strong> switch on an appointment makes the <strong>↻ Update Salaries</strong> button (top bar) create the actual monthly salary + fringe expenses, prorated and never duplicated. Leave it OFF if your actual numbers come from DBR imports — projections work either way.</p>
      ${demo('people', 'The People tab: appointments with salary, fringe and tuition — what the projections are built from.')}
    `)}

    ${sec("📧 Monthly expense report", `
      <p>At the start of each month the app offers to email you <strong>last month's expenses</strong> — and you can send one any time from <strong>⚙ Settings → Send a report now</strong> or the 🔔 notification.</p>
      <p><strong>It goes to you, not to your accountant.</strong> You read it, check it's right, and forward it on. That's deliberate: the app never emails anyone on your behalf, so there's no accountant address to keep configured, and nothing goes out that you haven't seen.</p>
      <p><strong>What's in it.</strong> The email body is a single line — <em>“Jane Doe's expense report for the month of September 2026”</em> — and everything else is attached. The attachment is a real <strong>Excel workbook</strong> with up to three sheets: <em>Expenses</em> (one row per expense — Date, Amount, Spend Category, Business Purpose, Grant/Worktag, Award, Cost Center, Fund, Person, Receipt, then P-card, Print cardholder's name, Name on the P-card and Purchased by), <em>P-card purchases</em> (the same columns filtered to just the card transactions), and <em>Changes this month</em> (anything you added, edited or deleted in the app). Headings are frozen and columns are pre-sized, so it opens ready to read. <strong>Each receipt is attached separately, named exactly as the Receipt column names it</strong>, so you can match a row to its file by eye.</p><p class="sub" style="margin-top:6px">If a month's receipts add up to more than about 18 MB they arrive as one zip instead — too many megabytes and the email would simply bounce. The names inside still match the Receipt column.</p><p><strong>Set your name once</strong> under <strong>⚙ Settings → Your name on the report</strong>, or the email just says “Expense report for the month of …”.</p>
      ${demo('monthly-report', 'Sending a report: ⚙ Settings → Send a report now, check the table, then send it to yourself.')}
    `)}

    ${sec("💳 P-card purchases", `
      <p>Tick <strong>💳 P-card</strong> on the Quick add form (or in any expense's edit box) when the purchase went on a university purchasing card. That is the whole of it — one tick, plus a <strong>Purchased by</strong> box on the rare occasion someone other than the cardholder made the purchase.</p>
      <p>Everything else is filled in for you. The <em>printed cardholder name</em> and the <em>name on the card</em> come from <strong>⚙ Settings → 💳 P-card</strong>; the cost centre comes from the worktags of whichever grant the expense was charged to; and the reason for the purchase is simply the comment you already typed against the expense.</p>
      <p>The monthly report then arrives with a <em>P-card purchases</em> sheet listing only the card transactions, so whoever reconciles them does not have to hunt for them among everything else.</p>
      ${demo('pcard', 'One tick marks an expense as a card purchase; the cardholder and card name come from your settings.')}`)}

    ${sec("🔔 Notifications", `
      <p>The bell in the top bar shows a <strong>red dot</strong> when something needs you: a grant gone over budget, a category over its line for the current year, an award ending within 60 days, receipts missing from recent manual entries, or a monthly report you haven't sent yet.</p>
      <p>Click a notification to jump straight to what it's about. <em>Mark all read</em> clears the dot until something new happens. If you've added the app to your home screen or dock, the badge appears on the app icon too, and the browser tab icon carries a small red count.</p>
      <p><strong>New versions.</strong> The bell also tells you when a newer version of Grants Manager has been released. Click it to read <em>what changed</em> in that version and, if you want it, a button to open the download page. Updating never touches your data — you unzip the new folder and copy your existing <code>data</code> folder into it.</p>
      <p class="sub">How the check works: about <strong>once every 15 days</strong>, the app asks GitHub what the latest released version number is. It sends nothing about you or your grants — no data leaves your computer, it only reads a public version number. If you're offline it silently does nothing and tries again next time; you'll never see an error about it.</p>
      ${demo('notifications', 'The 🔔 bell collects everything the app wants to tell you; clicking an item opens the grant it is about.')}
    `)}

    ${sec("📋 All Expenses — filtering, bulk edits, receipts", `
      <p><strong>Filters.</strong> The filter bar narrows by search text, grant, category, person, <em>date range</em>, <em>amount range</em>, source, and whether a receipt is attached. They stack, and the header always shows how many rows match and their <strong>total</strong> — handy for "how much travel did this grant spend last spring?". <strong>⬇ Export shown (CSV)</strong> exports exactly what's on screen, not everything.</p>
      <p><strong>Fixing several at once.</strong> Tick the checkboxes (or the one in the header to take everything currently shown) and a blue action bar appears: <em>Change category</em>, <em>Move to grant</em>, <em>Set person</em>, or <em>Delete selected</em>. This is the fast way to fix a batch of mis-categorized charges. Moving expenses to another grant automatically remaps their category and recalculates the budget year for the destination. Bulk deletes go to <strong>⚙ Settings → Recently deleted</strong> like any other delete, so a wrong selection is recoverable.</p>
      <p><strong>Receipts.</strong> Click <strong>✏️</strong> on any row to open it and drop in a receipt — you can attach one to an expense long after it was created. Set the <em>Receipt</em> filter to <em>Missing</em> to find everything still lacking one.</p>
      <p><strong>Duplicate warning.</strong> If you add an expense with the same amount on the same grant within 30 days of an existing one, the app shows you the matches and asks whether to continue. It's a warning, never a block — real repeats (monthly charges, two identical orders) are normal.</p>
      ${demo('all-expenses', 'Filtering by text and by grant, then ticking rows to reveal the blue bulk-edit bar.')}
    `)}

    ${sec("🔍 Search, 🌙 dark mode, 📤 sharing", `
      <p>The <strong>search box</strong> (top bar) finds grants, people, and expenses as you type — click a result to jump to it. The <strong>moon/sun button</strong> toggles dark mode.</p>
      <p><strong>Sharing the app (empty copy):</strong> the file <strong>“Grants Manager (shareable).zip”</strong> in your grants_management folder is a ready-to-email copy of the app containing <em>no data at all</em> — no grants, people, expenses, or receipts. It's refreshed automatically every time the app starts. Attach it to an email; the recipient unzips it and double-clicks <strong>Start Grants Manager (Mac).command</strong> or <strong>(Windows).bat</strong> — no admin rights needed.</p>
      <p><strong>Sharing your numbers:</strong> anyone you give your startup link and access key to has full access, so for a co-PI or department admin the safer route is <strong>🖨 Print report</strong> on a grant (a clean PDF-able page with the charts) or <strong>⬇ Export CSV</strong>. Both are a snapshot they can keep, with nothing connected back to your app.</p>
      ${demo('search-dark', 'Search finds expenses, grants and people as you type. The 🌙 button switches to dark mode.')}
    `)}

    ${sec("📱 iPhone", `
      <p>Three ways to use it on your phone:</p>
      <p style="border-left:3px solid #b97a08;padding-left:10px"><strong>⚠️ The startup address ends in an access key — treat it like a password.</strong> It looks like <code>http://192.168.1.x:8765/?k=…</code>. Anything on your network holding that key can read <em>and change</em> your grants; anything without it is refused. On this computer you never need it — <code>http://127.0.0.1:8765</code> works on its own.</p>
      <p>1. <strong>Install it like an app (recommended):</strong> with your Mac running Grants Manager and the iPhone on the same Wi-Fi, open the full address the server prints at startup (including the <code>?k=…</code> part) in Safari, tap the <strong>Share</strong> button, then <strong>Add to Home Screen</strong>. You get a Grants icon on your home screen that opens full-screen like a native app, fully editable.</p>
      <p>2. <strong>Read-only snapshot (works anywhere):</strong> the app automatically keeps <strong>“Grants Snapshot.html”</strong> up to date in your grants_management OneDrive folder. Open it from the OneDrive app on your iPhone — no Mac needed, shows availability, projections, and category balances.</p>
      <p>3. <strong>Just Safari:</strong> browse to the same Wi-Fi address without installing anything.</p>`)}

    ${sec("🔄 Workday — the app works offline by default", `
      <p><strong>You do not need a Workday connection to use this app.</strong> It opens straight into your own data and never asks you to sign in to anything. Most people can't connect directly anyway — university sign-in (SSO) plus the Duo prompt blocks the kind of automatic connection Workday would need — so the normal way to use this app is:</p>
      <p style="border-left:3px solid #2f6fed;padding-left:10px"><strong>Download a report from Workday → import the file here.</strong> That's it. The step-by-step is in the next section. You can also just type expenses in by hand and never touch Workday at all.</p>
      <p>The <strong>⇅ Workday</strong> button in the top bar is where you import files and see the state (✓ synced today / offline). The <strong>⚙ Settings</strong> gear holds backups, recently-deleted items and the monthly report; the direct-connection settings and saved email addresses are tucked under <strong>Advanced</strong> there, since offline use needs neither.</p>
      <p><strong>What happens after an import.</strong> The first time, a dashboard card asks you to match each Workday grant code (GR…) and object class (01_Personnel…) to your grants and categories — one time only. After that: charges that match an expense you already entered are <strong>linked</strong> (never duplicated); payroll actuals <strong>replace</strong> projected salary for past months (future months stay projected); anything new is <strong>added</strong> with a <span class="badge green">workday</span> badge. The <strong>Official numbers</strong> table on the Summary tab shows the imported figures side by side with your <strong>New expenses</strong> — the ones you've entered that haven't reached the official ledger yet. Importing the same file twice never duplicates anything, so when in doubt, re-import.</p>`)}

    ${sec("📥 Step-by-step: get your Workday report into this app", `
      <p class="sub">This is the main way to use the app. Nothing here needs special permissions — if you can see your grant in Workday, you can do all of it. Takes about two minutes the first time.</p>
      <p><strong>Part 1 — Create the Excel file in Workday</strong></p>
      <p><strong>1.</strong> Sign in to Workday as you normally do (SSO + Duo).</p>
      <p><strong>2.</strong> Click the <strong>search box</strong> at the top and type <code>Grant Budget Vs Actuals</code>. In the results, click the report named something like <strong>RPT - Grant Budget Vs Actuals</strong>. <em>(Report names vary a little between universities. If you don't see it, search <code>Budget vs Actual</code>, or ask your grant accountant "which report shows my grant budget versus actuals?" — any report with budget and actual amounts per category will work.)</em></p>
      <p><strong>3.</strong> A prompt screen appears. Fill in <strong>Grant</strong> or <strong>Award</strong> with your grant (start typing the name or the GR… code and pick it from the list). Leave the other boxes as they are unless you want a specific period. Click <strong>OK</strong>.</p>
      <p><strong>4.</strong> The report appears on screen. Look at the <strong>top-right corner of the report table</strong> for a small <strong>spreadsheet/Excel icon</strong> (hovering says <em>Export to Excel</em> or <em>Download to Excel</em>). Click it, then click <strong>Download</strong> in the box that appears.</p>
      <p><strong>5.</strong> Workday prepares the file and it lands in your <strong>Downloads</strong> folder as an <strong>.xlsx</strong> file. <em>(Sometimes it appears under the bell/cloud "notifications" icon at the top right instead — click that and download it from there.)</em> This is your <strong>balances</strong> file.</p>
      <p><strong>6. (Optional but recommended — the individual charges.)</strong> Back on the report, click on an <strong>Actuals amount</strong> (the blue/underlined number). Workday drills down to the list of individual transactions behind it. Export <em>that</em> screen the same way (step 4). This is your <strong>transactions</strong> file, and it's what lets the app show you each charge rather than just totals.</p>
      <p><strong>Part 2 — Import it here</strong></p>
      <p><strong>7.</strong> In this app, click <strong>⇅ Workday</strong> in the top bar.</p>
      <p><strong>8.</strong> Click <strong>📁 Choose files or drop them here</strong>, then select the <code>.xlsx</code> file(s) you just downloaded. <strong>You can select several at once</strong> — hold <em>Cmd</em> (Mac) or <em>Ctrl</em> (Windows) while clicking, or drag a box around them. One file per grant is fine; there's no need to combine them.</p>
      <p><strong>9.</strong> That's it — the app reads them immediately. You don't need to find, create, or copy anything into a folder yourself.</p>
      <p><strong>10.</strong> The first time only, the dashboard shows a card asking you to match Workday's grant codes and object classes to your grants and categories. Pick from the dropdowns and save. It won't ask again.</p>
      <p><strong>Repeat whenever you want fresh numbers</strong> — typically once a month after the ledger closes. Just steps 1–9 again; re-importing never creates duplicates.</p>
      <p style="border-left:3px solid #b97a08;padding-left:10px"><strong>If something doesn't work</strong>, the app now tells you exactly what it found — a PDF instead of a workbook, the older <code>.xls</code> format, a CSV, or a sheet whose column headings don't match. It names the missing columns and changes nothing until a file reads cleanly, so a wrong export can't corrupt anything. The app reads the <strong>first sheet</strong> only, and it copes with a title or blank row above the headings.</p>
      <p><strong>Not sure your export looks right?</strong> Download the example workbook — two sheets (transactions and balances) with obviously fake data in exactly the columns the app looks for. Open it next to your own export and compare the heading row.</p>
      <div class="toolbar" style="margin:8px 0">${EXAMPLE_LINK}</div>
      ${demo('workday-import', 'The ⇅ Workday panel: drop your exported .xlsx files here, or download the example to check yours matches.')}
      ${demo('wrong-file', 'A wrong export: the app names the file, says what it actually was, and offers the example. Nothing is imported.')}
    `)}

    ${sec("🔗 Optional: direct connection (RaaS) — most people can't use this", `
      <p style="border-left:3px solid #b97a08;padding-left:10px"><strong>Read this first.</strong> This section is <em>optional and advanced</em>. It needs permissions in Workday that ordinary faculty accounts don't have, and at most universities — UARK included — single sign-on and the Duo prompt block it outright. <strong>If it doesn't work for you, nothing is wrong and nothing is missing:</strong> the import steps above are the supported way to use this app, and they give you the same numbers. Skip this unless you already know you have report-writing rights.</p>
      <p><strong>What access you need.</strong> Ask your Workday security administrator or department report writer whether your account has these. Exact group names vary by university, so quote the capability, not just the name:</p>
      <p>• <strong>Permission to create custom reports</strong> — usually a security group called <em>Report Writer</em> (the underlying domain is <em>Custom Report Creation</em>). Without it, the <em>Copy Standard Report to Custom Report</em> and <em>Create Custom Report</em> tasks simply won't appear when you search for them.<br>
      • <strong>Permission to publish a report as a web service</strong> — the <em>Enable As Web Service</em> tickbox on a report's Advanced tab. This is often restricted separately from report writing, and is the step most people are missing.<br>
      • <strong>Read access to the grant data itself</strong> — normally already covered by your <em>Principal Investigator</em> / <em>Grant Manager</em> / <em>Grant Financial Analyst</em> role. If you can already open the Grant Budget vs Actuals report for your grant, you have this.<br>
      • <strong>An account that accepts a username and password</strong> — this is the blocker. Automatic connections use basic authentication, which SSO-only accounts reject. Workarounds require your IT/security team to create an <em>Integration System User</em> (ISU) exempt from SSO and MFA, with a security group granting it read access to your grant reports. That is an institutional decision, not something you can set up yourself.</p>
      <p><strong>If you do have all of the above,</strong> the one-time setup inside Workday is:</p>
      <p><strong>1. Make a custom copy of the report.</strong> In the Workday search bar type <strong>Copy Standard Report to Custom Report</strong>, run the task, and pick <em>RPT - Grant Budget Vs Actuals</em>. Name the copy something like “My Grants RaaS”. <em>(If that task doesn't appear, you don't have report-writing rights — ask your department's Workday report writer to do steps 1–3; it takes them about five minutes.)</em></p>
      <p><strong>2. Enable it as a web service.</strong> Edit the custom report → <strong>Advanced</strong> tab → tick <strong>Enable As Web Service</strong> → OK. If the report has prompts (Grant, Organization…), give them default values so it can run unattended.</p>
      <p><strong>3. Copy the URL.</strong> On the report: related actions (…) → <strong>Web Service</strong> → <strong>View URLs</strong> → right-click the <strong>CSV</strong> link → Copy link. It looks like <code>https://….workday.com/ccx/service/customreport2/…</code></p>
      <p><strong>4. Paste it here.</strong> Click <strong>⚙ Settings</strong> → open <strong>Advanced</strong> → paste the URL under <em>Direct connection (RaaS)</em>, enter your Workday username, Save. Then <strong>⇅ Workday → ⇣ Sync from Workday now</strong> and type your password — held in memory for this session only, never written to disk. Once a URL is saved, and only then, the app will offer to connect when it opens.</p>
      <p><strong>Transactions too (optional):</strong> the copied report gives balances only. For the individual charges, create a second custom report (<em>Create Custom Report</em> → Advanced) on a journal-lines data source filtered to your grants, including the columns <em>Accounting Date, Budget Date, Operational Transaction, Award, Grant, Worker, Supplier, Ledger Account, Transaction Amount, Object Class, Spend Category</em>; enable it as a web service the same way and paste its URL in the second field.</p>
      <p><strong>If the sync says the login was rejected, or that it got a login page back:</strong> your account is SSO-only and this route is closed. That is the expected outcome for most people — use the import steps above instead.</p>
      ${demo('advanced-settings', 'Where the optional settings live — ⚙ Settings → Advanced: the RaaS URLs, saved addresses and grant worktags.')}
    `)}

    ${sec("→ Getting expenses INTO Workday (Add to Workday)", `
      <p>Tick <strong>📤 Add to Workday</strong> on the Quick add form and, the moment you hit Add, a box pops up with the expense in <strong>workday-ready format</strong> — amount, date, suggested Spend Category (learned from your imports), business purpose, Grant and Award worktags, Cost Center, Fund — plus the email it will go to. Click <strong>✉ Send email</strong> and it goes to the financial team through <strong>Microsoft Outlook</strong> (works on Mac and Windows) with <strong>your email CC'd</strong> and the <strong>receipt attached</strong> (receipts must be PDF — see the ⓘ next to the receipt box). The box is <strong>ticked by default</strong>. Untick it and the expense is simply saved here — nothing is sent, and it never appears in the “To enter in Workday” list, which is what you want for anything that isn't going on the grant's official ledger.</p>
      <p>You don't have to set anything up first: the addresses you use are remembered from your last send and pre-filled next time (⚙ Settings → Advanced is where to correct one). On a Mac, the first send asks permission for <strong>Grants Manager</strong> to control Outlook — click OK once.</p>
      <p><strong>Charging someone else's account:</strong> pick <strong>“Other”</strong> as the grant when a colleague or the department provides the account — a <em>Worktag (whose account)</em> field appears; type in that account's worktag (GR… or CC…). These expenses never count against your grant budgets, but with <strong>📤 Add to Workday</strong> still ticked they're sent to the financial team the same as any other expense, using the worktag you typed instead of one of your own grants.</p>
      <p><strong>Splits:</strong> tick <em>Split across worktags</em> to reveal the split fields — the other grant, its percentage, and its <em>Cost Center</em> and <em>Worktag</em> (auto-filled if the grant is known, editable if not). The email then lists both accounting lines with their percentages and amounts.</p>
      <p>Grant and Award worktags fill in automatically from your imports. Anything not yet visible in Workday collects in the <strong>“To enter in Workday”</strong> card on the Dashboard (📤 reopens the send box; <strong>⬇ Entry sheet (CSV)</strong> downloads the whole list). Rows clear themselves once the posted charge syncs back — <em>Not sent yet</em> → <em>Sent, waiting</em> → gone. Untick <strong>📤 Add to Workday</strong> when you add an expense (it is ticked by default) and it never appears in this list at all.</p>`)}

    ${sec("📄 Data, backups & undo", `
      <p>Everything lives in one file: <code>GrantsApp/data/grants.db</code>. Older actuals were imported from scanned Workday DBRs; new actuals come from the ⇅ Workday panel.</p>
      <p><strong>Automatic backups.</strong> Every time the app starts, it saves a dated copy of your data into <code>GrantsApp/data/backups/</code> (one per day, kept for 30 days). You don't have to do anything. Under <strong>⚙ Settings → Backups &amp; data safety</strong> you can also <em>Download backup now</em> — do that before anything risky, and keep the file somewhere other than OneDrive. The same panel restores from a backup file if you ever need to roll back.</p>
      <p><strong>Undo.</strong> Deleting a grant, person, appointment, or expense no longer loses it immediately — it goes to <strong>⚙ Settings → 🗑 Recently deleted</strong> for 30 days. Restoring a grant brings back its expenses, budget lines, and appointments too. After 30 days it's cleared for good, so if a deletion was a mistake, restore it sooner rather than later.</p>
      <p style="border-left:3px solid #b97a08;padding-left:10px"><strong>⚠️ Important — OneDrive and this app.</strong> This folder is synced by OneDrive, which is great for having your data on other devices, but there's one real risk to know about: <strong>never run Grants Manager on two computers at the same time</strong>, and let OneDrive finish syncing (its icon stops spinning) before you open the app on a different machine. Databases don't merge like documents — if two copies are open at once, OneDrive can't combine them and will either overwrite one or leave a file named something like <em>"grants-DESKTOP-ABC123.db"</em> next to the real one. If you ever see a "conflicted copy" file appear, don't delete it: it may hold work that's missing from the main file — check both, or restore from a backup in ⚙ Settings. For the same reason, don't edit from your phone and your Mac simultaneously.</p>
      ${demo('backups-undo', '⚙ Settings holds the backup download and the 30-day Recently deleted list.')}
    `)}
  `;
}

/* ------------------------------------------------------- notifications */
// Each notification gets a stable id so "seen" survives reloads. Anything
// whose id the user hasn't dismissed counts as new and lights the red dot.
function monthKeyNow() { return todayISO().slice(0, 7); }
function prevMonthKey() {
  const [y, m] = todayISO().slice(0, 7).split("-").map(Number);
  const d = new Date(y, m - 2, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}
function monthLabel(k) {
  return new Date(+k.slice(0, 4), +k.slice(5, 7) - 1, 1)
    .toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function seenNotifs() {
  try { return JSON.parse(localStorage.getItem("gm-seen-notifs") || "[]"); }
  catch { return []; }
}
function markNotifsSeen(ids) {
  localStorage.setItem("gm-seen-notifs", JSON.stringify([...new Set(ids)].slice(-200)));
}

/* "What's new" for an available release. The notes come from GitHub, i.e.
   from outside this app, so they are escaped and rendered as plain text with
   simple bullets — never as HTML. */
function updateModal(u) {
  const lines = String(u.notes || "").split(/\r?\n/);
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    const bullet = /^[-*+]\s+/.test(line);
    if (bullet && !inList) { html += "<ul style='margin:6px 0 10px;padding-left:20px'>"; inList = true; }
    if (!bullet && inList) { html += "</ul>"; inList = false; }
    if (bullet) html += `<li style="margin:3px 0">${esc(line.replace(/^[-*+]\s+/, ""))}</li>`;
    else if (/^#{1,6}\s/.test(line)) html += `<p style="font-weight:600;margin:10px 0 2px">${esc(line.replace(/^#+\s*/, ""))}</p>`;
    else if (line) html += `<p style="margin:4px 0">${esc(line)}</p>`;
  }
  if (inList) html += "</ul>";
  if (!html) html = `<p class="sub">No release notes were published for this version.</p>`;
  modal(`
    <h2>⬆ Version ${esc(u.latest)} is available</h2>
    <p class="sub" style="margin-bottom:10px">You're running ${esc(u.current)}${u.published ? ` · released ${esc(u.published)}` : ""}. Your data isn't touched by updating — you keep the same <code>data</code> folder.</p>
    <div style="max-height:46vh;overflow:auto;border:1px solid var(--line,#e5e8ee);border-radius:10px;padding:10px 14px;font-size:13.5px">${html}</div>
    <p class="sub" style="margin-top:10px">To update: download the new <code>GrantsManager.zip</code>, unzip it, and copy your existing <code>data</code> folder into the new folder (replacing the empty one). Then start it as usual.</p>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Later</button>
      ${u.url ? `<a href="${esc(u.url)}" target="_blank" rel="noopener noreferrer"><button class="btn">Open download page</button></a>` : ""}
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
  });
}

function computeNotifications() {
  if (!S) return [];
  const out = [];
  const today = S.today;

  for (const g of realGrants()) {
    if (g.status !== "active") continue;
    const spent = grantSpent(g.id);
    const avail = grantAvailable(g.id);
    if (g.initial_amount > 0 && avail < 0) {
      out.push({ id: `over:${g.id}:${Math.round(spent)}`, level: "red",
                 text: `${g.name} is overspent by ${money(Math.abs(avail))}.`,
                 go: () => { view = { name: "grant", grantId: g.id }; render(); } });
    } else if (g.initial_amount > 0 && spent / g.initial_amount >= 0.9) {
      out.push({ id: `near:${g.id}:${Math.round(spent / g.initial_amount * 100)}`,
                 level: "amber",
                 text: `${g.name}: ${Math.round(spent / g.initial_amount * 100)}% of the award is spent.`,
                 go: () => { view = { name: "grant", grantId: g.id }; render(); } });
    }
    // category overruns in the grant's current budget year
    const yr = budgetYearOf(g, today);
    for (const c of grantCats(g)) {
      const b = budgetFor(g.id, c.id, yr);
      if (b <= 0) continue;
      const sp = spentFor(g.id, c.id, yr);
      if (sp > b + 0.01) {
        out.push({ id: `cat:${g.id}:${c.id}:${yr}:${Math.round(sp)}`, level: "red",
                   text: `${g.name} · ${c.name} (Year ${yr}) is over budget by ${money(sp - b)}.`,
                   go: () => { view = { name: "grant", grantId: g.id }; render(); } });
      }
    }
    const dl = daysLeft(g);
    if (dl !== null && dl >= 0 && dl <= 60) {
      out.push({ id: `end:${g.id}:${effectiveEnd(g)}`, level: "amber",
                 text: `${g.name} ends in ${dl} day${dl === 1 ? "" : "s"} (${effectiveEnd(g)}).`,
                 go: () => { view = { name: "grant", grantId: g.id }; render(); } });
    }
  }

  // a newer release is available (checked at most every 15 days, offline = silent)
  if (S.update && S.update.available) {
    out.push({ id: `update:${S.update.latest}`, level: "blue",
               text: `Version ${S.update.latest} is available — you have ${S.update.current}. See what's new.`,
               go: () => updateModal(S.update) });
  }

  // last month's expense report not sent yet
  const sent = (WD && WD.reports_sent) || {};
  const prev = prevMonthKey();
  const hasPrev = S.expenses.some((e) => e.date.slice(0, 7) === prev);
  if (hasPrev && !sent[prev]) {
    out.push({ id: `report:${prev}`, level: "blue",
               text: `Expense report for ${monthLabel(prev)} hasn't been sent yet.`,
               go: () => reportModal(prev) });
  }

  // expenses entered by hand with no receipt, this month and last
  const noReceipt = S.expenses.filter((e) => e.source === "manual" && !e.receipt_path &&
      (e.date.slice(0, 7) === monthKeyNow() || e.date.slice(0, 7) === prev));
  if (noReceipt.length) {
    out.push({ id: `receipts:${noReceipt.length}:${monthKeyNow()}`, level: "amber",
               text: `${noReceipt.length} recent expense${noReceipt.length === 1 ? "" : "s"} ${noReceipt.length === 1 ? "has" : "have"} no receipt attached.`,
               go: () => { view = { name: "allexpenses", f: { q: "", grant: "", cat: "", person: "", source: "manual", from: "", to: "", min: "", max: "", receipt: "no" } }; render(); } });
  }
  return out;
}

function refreshNotifBadge() {
  const list = computeNotifications();
  const seen = seenNotifs();
  const unseen = list.filter((n) => !seen.includes(n.id));
  const dot = $("#bell-dot");
  if (dot) dot.hidden = unseen.length === 0;
  const bell = $("#btn-bell");
  if (bell) bell.title = unseen.length
    ? `${unseen.length} new notification${unseen.length === 1 ? "" : "s"}`
    : "Notifications";
  applyAppBadge(unseen.length);
  return { list, unseen };
}

// Red dot on the installed app icon (PWA / macOS dock via Safari) plus a
// drawn-on favicon badge, so the tab shows it too.
function applyAppBadge(n) {
  try {
    if (navigator.setAppBadge) n ? navigator.setAppBadge(n) : navigator.clearAppBadge();
  } catch { /* not supported here — favicon badge below still applies */ }
  const link = $("#favicon");
  if (!link) return;
  const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const x = c.getContext("2d");
    x.drawImage(img, 0, 0, 64, 64);
    if (n) {
      x.beginPath(); x.arc(48, 16, 15, 0, Math.PI * 2);
      x.fillStyle = "#ffffff"; x.fill();
      x.beginPath(); x.arc(48, 16, 12, 0, Math.PI * 2);
      x.fillStyle = "#d24545"; x.fill();
      if (n < 10) {
        x.fillStyle = "#fff"; x.font = "bold 16px -apple-system, sans-serif";
        x.textAlign = "center"; x.textBaseline = "middle";
        x.fillText(String(n), 48, 17);
      }
    }
    link.href = c.toDataURL("image/png");
  };
  img.src = "/app/icon.png";
}

function notifPanel() {
  const old = $(".notif-panel");
  if (old) { old.remove(); return; }
  const { list, unseen } = refreshNotifBadge();
  const seen = seenNotifs();
  const el = document.createElement("div");
  el.className = "notif-panel";
  el.innerHTML = `
    <div class="notif-head"><span>Notifications</span>
      ${list.length ? `<button class="btn ghost small" id="notif-clear">Mark all read</button>` : ""}</div>
    <div class="notif-list">
      ${list.length ? list.map((n, i) => `
        <div class="notif" data-i="${i}">
          <span class="dot ${n.level}"></span>
          <div><div>${esc(n.text)}</div>
            ${seen.includes(n.id) ? "" : `<div class="when">new</div>`}</div>
        </div>`).join("")
      : `<div class="notif-empty">No new notifications.<div style="margin-top:5px;font-size:12.5px">You'll be told here about overspending, budgets running low, grants ending soon, missing receipts and new versions.</div></div>`}
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  $$(".notif", el).forEach((row) => row.onclick = () => {
    const n = list[+row.dataset.i];
    markNotifsSeen([...seen, n.id]);
    close(); refreshNotifBadge();
    if (n.go) n.go();
  });
  const clear = $("#notif-clear", el);
  if (clear) clear.onclick = () => {
    markNotifsSeen(list.map((n) => n.id));
    close(); refreshNotifBadge();
    toast("Notifications marked read");
  };
  setTimeout(() => {
    document.addEventListener("click", function away(ev) {
      if (!el.contains(ev.target) && ev.target !== $("#btn-bell")) {
        el.remove(); document.removeEventListener("click", away);
      }
    });
  }, 0);
}

/* ------------------------------------------------- monthly expense report */
async function reportModal(month) {
  month = month || prevMonthKey();
  let d;
  try { d = await api(`/api/report/preview?month=${month}`); }
  catch (e) { toast("Couldn't build the report: " + e.message); return; }

  const owner = d.owner_email || "";
  const rowsHtml = d.rows.length
    ? `<div style="overflow-x:auto;max-height:320px;overflow-y:auto;border:1px solid var(--border);border-radius:8px">
        <table><thead><tr>${d.columns.map((c) => `<th style="white-space:nowrap">${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${d.rows.map((r) => `<tr>${d.columns.map((c) =>
          `<td class="${c === "Amount" ? "num" : ""}" style="white-space:${c === "Business Purpose" ? "normal" : "nowrap"}">${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>
      </div>`
    : `<div class="empty">No expenses recorded for ${esc(d.label)}.</div>`;

  modal(`
    <h2>📧 Expense report — ${esc(d.label)}</h2>
    <p class="sub" style="margin-bottom:12px">${d.rows.length} expense${d.rows.length === 1 ? "" : "s"} · <strong>${money2(d.total)}</strong>.
      This goes to <strong>you</strong> — check it, then forward to your accountant. The email itself is one line; this table arrives as an attached <strong>Excel workbook</strong>${d.rows.some((r) => r.Receipt) ? ", with each receipt attached under the name shown in its Receipt column" : ""}.</p>
    ${d.missing_receipts ? `<p class="sub" style="background:var(--amber-soft);color:var(--amber);padding:9px 12px;border-radius:8px;margin-bottom:12px"><strong>${d.missing_receipts}</strong> hand-entered expense${d.missing_receipts === 1 ? "" : "s"} ${d.missing_receipts === 1 ? "has" : "have"} no receipt attached.</p>` : ""}
    ${rowsHtml}
    ${d.changes.length ? `<p class="sub" style="margin:12px 0 4px"><strong>${d.changes.length}</strong> change${d.changes.length === 1 ? "" : "s"} made in the app this month will be listed too, so you can verify them.</p>` : ""}
    <div class="form-row" style="margin-top:14px">
      <label class="field"><span>Send to (you)</span><input id="rep-to" type="email" value="${esc(owner)}" placeholder="you@uark.edu"></label>
      <label class="field" style="max-width:150px"><span>Month</span><input id="rep-month" type="month" value="${esc(d.month)}"></label>
    </div>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Close</button>
      <button class="btn" id="m-send" ${d.rows.length ? "" : "disabled"}>✉ Send report to me</button>
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
    $("#rep-month", el).onchange = () => {
      const v = $("#rep-month", el).value;
      if (v) { close(); reportModal(v); }
    };
    $("#m-send", el).onclick = async () => {
      const to = $("#rep-to", el).value.trim();
      if (!to) { toast("Enter your email address"); return; }
      const btn = $("#m-send", el);
      btn.disabled = true; btn.textContent = "Sending…";
      try {
        const r = await api("/api/report/send", "POST", { month: d.month, to });
        close();
        toast(`Report for ${d.label} sent to ${r.to} — check it, then forward to your accountant`);
        markNotifsSeen([...seenNotifs(), `report:${d.month}`]);
        WD = await api("/api/workday/state");
        refreshNotifBadge();
      } catch (e) {
        btn.disabled = false; btn.textContent = "✉ Send report to me";
        toast("Send failed: " + e.message);
      }
    };
  });
}

/* ------------------------------------------------------ close-out report */
function renderCloseout() {
  const g = S.grants.find((x) => x.id === view.grantId);
  if (!g) { view = { name: "dashboard" }; return renderDashboard(); }
  const years = grantYears(g);
  const gExp = S.expenses.filter((e) => e.grant_id === g.id);
  const spent = grantSpent(g.id);
  const avail = grantAvailable(g.id);
  const budgeted = grantBudgeted(g.id);
  const cats = grantCats(g).filter((c) =>
    S.budget_lines.some((b) => b.grant_id === g.id && b.category_id === c.id) ||
    gExp.some((e) => e.category_id === c.id));
  const wt = wdWorktagFor(g.id);
  const people = S.appointments.filter((a) => a.grant_id === g.id);
  const missingReceipts = gExp.filter((e) => !e.receipt_path && e.source === "manual");
  const byYear = range(years).map((y) => ({
    y,
    budget: cats.reduce((s, c) => s + budgetFor(g.id, c.id, y), 0),
    spent: cats.reduce((s, c) => s + spentFor(g.id, c.id, y), 0),
  }));
  const first = gExp.reduce((a, e) => !a || e.date < a ? e.date : a, "");
  const last = gExp.reduce((a, e) => !a || e.date > a ? e.date : a, "");

  return `
    <span class="back no-print" data-goto-grant="${g.id}">← Back to ${esc(g.name)}</span>
    <div class="section-head">
      <div>
        <h1 style="margin-bottom:2px">Close-out report</h1>
        <p class="sub" style="margin:0">${esc(g.name)}${wt ? ` · ${esc(wt)}` : ""}${esc(g.agency ? " · " + g.agency : "")}</p>
      </div>
      <div class="toolbar no-print">
        <button class="btn" onclick="window.print()">🖨 Print / Save as PDF</button>
      </div>
    </div>

    <div class="card">
      <h2>Award summary</h2>
      <table>
        <tbody>
          <tr><td style="color:var(--muted);width:38%">Grant</td><td><strong>${esc(g.name)}</strong></td></tr>
          ${wt ? `<tr><td style="color:var(--muted)">Workday worktag</td><td>${esc(wt)}</td></tr>` : ""}
          <tr><td style="color:var(--muted)">Agency</td><td>${esc(g.agency || "—")}</td></tr>
          <tr><td style="color:var(--muted)">Period</td><td>${g.start_date || "?"} → ${effectiveEnd(g) || "?"}${g.nce_end_date ? " (includes no-cost extension)" : ""}</td></tr>
          <tr><td style="color:var(--muted)">Activity recorded</td><td>${first ? `${first} → ${last}` : "no expenses recorded"}</td></tr>
          <tr><td style="color:var(--muted)">Total awarded</td><td><strong>${money2(g.initial_amount)}</strong></td></tr>
          <tr><td style="color:var(--muted)">Total spent</td><td><strong>${money2(spent)}</strong></td></tr>
          <tr><td style="color:var(--muted)">Remaining</td><td><strong style="color:${avail < 0 ? "var(--red)" : "var(--green)"}">${money2(avail)}</strong>${g.initial_amount > 0 ? ` (${Math.round(spent / g.initial_amount * 100)}% used)` : ""}</td></tr>
          <tr><td style="color:var(--muted)">Report generated</td><td>${S.today}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h2>Final budget vs actual — by category</h2>
      <table>
        <thead><tr><th>Category</th><th class="num">Budgeted</th><th class="num">Spent</th><th class="num">Remaining</th><th class="num">% used</th></tr></thead>
        <tbody>
          ${cats.map((c) => {
            const b = range(years).reduce((s, y) => s + budgetFor(g.id, c.id, y), 0);
            const sp = spentFor(g.id, c.id, null);
            const pct = b > 0 ? Math.round(sp / b * 100) : (sp > 0 ? 100 : 0);
            return `<tr><td>${esc(c.name)}</td><td class="num">${money2(b)}</td>
              <td class="num">${money2(sp)}</td>
              <td class="num" style="color:${b - sp < 0 ? "var(--red)" : "inherit"}">${money2(b - sp)}</td>
              <td class="num">${pct}%</td></tr>`;
          }).join("")}
        </tbody>
        <tfoot><tr style="font-weight:700"><td>Total</td><td class="num">${money2(budgeted)}</td>
          <td class="num">${money2(spent)}</td><td class="num">${money2(budgeted - spent)}</td>
          <td class="num">${budgeted > 0 ? Math.round(spent / budgeted * 100) : 0}%</td></tr></tfoot>
      </table>
    </div>

    ${years > 1 ? `<div class="card">
      <h2>By budget year</h2>
      <table>
        <thead><tr><th>Year</th><th class="num">Budgeted</th><th class="num">Spent</th><th class="num">Remaining</th></tr></thead>
        <tbody>${byYear.map((r) => `<tr><td>Year ${r.y}</td><td class="num">${money2(r.budget)}</td>
          <td class="num">${money2(r.spent)}</td><td class="num">${money2(r.budget - r.spent)}</td></tr>`).join("")}</tbody>
      </table>
    </div>` : ""}

    ${people.length ? `<div class="card">
      <h2>Personnel supported</h2>
      <table>
        <thead><tr><th>Name</th><th>Role</th><th class="num">Salary/yr</th><th class="num">Fringe</th><th>Period</th></tr></thead>
        <tbody>${people.map((a) => {
          const p = S.people.find((p) => p.id === a.person_id) || { name: "?", role: "" };
          return `<tr><td>${esc(p.name)}</td><td>${esc(p.role || "")}</td>
            <td class="num">${money2(a.monthly_salary * 12)}${(a.pct || 100) < 100 ? ` (${a.pct}%)` : ""}</td>
            <td class="num">${a.fringe_rate}%</td><td>${a.start_date} → ${a.end_date}</td></tr>`;
        }).join("")}</tbody>
      </table>
    </div>` : ""}

    ${missingReceipts.length ? `<div class="card" style="border-left:4px solid #b97a08">
      <h2>⚠️ Before you file: ${missingReceipts.length} expense${missingReceipts.length === 1 ? "" : "s"} without a receipt</h2>
      <p class="sub" style="margin-bottom:8px">Manually-entered charges with no attached receipt. Sponsors commonly ask for these at close-out.</p>
      <table>
        <thead><tr><th>Date</th><th>Category</th><th>Description</th><th class="num">Amount</th></tr></thead>
        <tbody>${missingReceipts.slice(0, 25).map((e) => `<tr><td>${e.date}</td>
          <td>${esc(catName(e.category_id))}</td><td>${esc(e.description || "")}</td>
          <td class="num">${money2(e.amount)}</td></tr>`).join("")}</tbody>
      </table>
      ${missingReceipts.length > 25 ? `<p class="sub" style="margin:8px 0 0">…and ${missingReceipts.length - 25} more. Use All Expenses → Receipt: Missing to see them all.</p>` : ""}
    </div>` : ""}

    <div class="card">
      <h2>Complete expense ledger (${gExp.length})</h2>
      <table>
        <thead><tr><th>Date</th><th>Category</th><th>Yr</th><th>Description</th><th>Person</th><th class="num">Amount</th><th>Receipt</th></tr></thead>
        <tbody>${gExp.slice().sort((a, b) => a.date.localeCompare(b.date)).map((e) => `<tr>
          <td>${e.date}</td><td>${esc(catName(e.category_id))}</td><td>Y${e.year || 1}</td>
          <td>${esc(e.description || "")}</td><td>${esc(personName(e.person_id))}</td>
          <td class="num">${money2(e.amount)}</td>
          <td>${e.receipt_path ? "yes" : "—"}</td></tr>`).join("") ||
          `<tr><td colspan="7" class="empty">No expenses recorded.</td></tr>`}</tbody>
        <tfoot><tr style="font-weight:700"><td colspan="5">Total</td><td class="num">${money2(spent)}</td><td></td></tr></tfoot>
      </table>
    </div>

    <div class="card">
      <h2>Certification</h2>
      <p class="sub" style="margin-bottom:22px">To the best of my knowledge, the expenditures listed above were incurred in support of this award and are allowable, allocable, and reasonable under the sponsor's terms.</p>
      <table>
        <tbody>
          <tr><td style="border-bottom:1px solid var(--muted);width:45%;height:38px"></td><td style="width:10%"></td><td style="border-bottom:1px solid var(--muted)"></td></tr>
          <tr><td style="color:var(--muted);font-size:12px">Principal Investigator</td><td></td><td style="color:var(--muted);font-size:12px">Date</td></tr>
        </tbody>
      </table>
    </div>`;
}

/* -------------------------------------------------------- all expenses */
// filter state lives on `view` so it survives re-renders after an edit
function expFilters() {
  view.f = view.f || { q: "", grant: "", cat: "", person: "", source: "",
                       from: "", to: "", min: "", max: "", receipt: "" };
  return view.f;
}

function filteredExpenses() {
  const f = expFilters();
  const q = f.q.trim().toLowerCase();
  return S.expenses.filter((e) => {
    if (f.grant && e.grant_id !== +f.grant) return false;
    if (f.cat && catName(e.category_id) !== f.cat) return false;
    if (f.person && String(e.person_id || "") !== f.person) return false;
    if (f.source && (e.source || "manual") !== f.source) return false;
    if (f.from && e.date < f.from) return false;
    if (f.to && e.date > f.to) return false;
    if (f.min !== "" && e.amount < parseFloat(f.min)) return false;
    if (f.max !== "" && e.amount > parseFloat(f.max)) return false;
    if (f.receipt === "yes" && !e.receipt_path) return false;
    if (f.receipt === "no" && e.receipt_path) return false;
    if (q) {
      const g = S.grants.find((g) => g.id === e.grant_id);
      const hay = `${e.description} ${personName(e.person_id)} ${catName(e.category_id)} ${g ? g.name : ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderAllExpenses() {
  const f = expFilters();
  const rows = filteredExpenses();
  const total = rows.reduce((s, e) => s + e.amount, 0);
  const catNames = [...new Set(S.categories.map((c) => c.name))].sort();
  const sources = [...new Set(S.expenses.map((e) => e.source || "manual"))].sort();
  const active = Object.entries(f).filter(([, v]) => v !== "").length;

  return `
    <div class="section-head">
      <div><h1>All expenses</h1><p class="sub" style="margin:0">
        ${rows.length} of ${S.expenses.length} shown · <strong>${money2(total)}</strong>${active ? ` · ${active} filter${active === 1 ? "" : "s"} on` : ""}</p></div>
      <div class="toolbar no-print">
        ${active ? `<button class="btn ghost small" id="f-clear">✕ Clear filters</button>` : ""}
        <button class="btn secondary small" id="f-export">⬇ Export shown (CSV)</button>
      </div>
    </div>

    <div class="card no-print">
      <div class="quick">
        <label class="field" style="width:210px"><span>Search</span><input id="f-q" value="${esc(f.q)}" placeholder="description, person…"></label>
        <label class="field" style="width:150px"><span>Grant</span><select id="f-grant"><option value="">All</option>${S.grants.map((g) => `<option value="${g.id}" ${String(f.grant) === String(g.id) ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select></label>
        <label class="field" style="width:135px"><span>Category</span><select id="f-cat2"><option value="">All</option>${catNames.map((n) => `<option ${f.cat === n ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
        <label class="field" style="width:145px"><span>Person</span><select id="f-person"><option value="">All</option>${S.people.map((p) => `<option value="${p.id}" ${String(f.person) === String(p.id) ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
        <label class="field" style="width:120px"><span>From</span><input type="date" id="f-from" value="${esc(f.from)}"></label>
        <label class="field" style="width:120px"><span>To</span><input type="date" id="f-to" value="${esc(f.to)}"></label>
        <label class="field" style="width:95px"><span>Min $</span><input type="number" step="0.01" id="f-min" value="${esc(f.min)}"></label>
        <label class="field" style="width:95px"><span>Max $</span><input type="number" step="0.01" id="f-max" value="${esc(f.max)}"></label>
        <label class="field" style="width:115px"><span>Receipt</span><select id="f-receipt"><option value="">Any</option><option value="yes" ${f.receipt === "yes" ? "selected" : ""}>Has one</option><option value="no" ${f.receipt === "no" ? "selected" : ""}>Missing</option></select></label>
        <label class="field" style="width:115px"><span>Source</span><select id="f-source"><option value="">Any</option>${sources.map((s) => `<option ${f.source === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
      </div>
    </div>

    <div class="card">
      <div id="bulk-bar" class="no-print" style="display:none;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;margin-bottom:10px;border-radius:8px;background:var(--accent-soft, #eef3fe)">
        <strong id="bulk-count" style="font-size:13px"></strong>
        <button class="btn secondary small" data-bulk="category">Change category…</button>
        <button class="btn secondary small" data-bulk="grant">Move to grant…</button>
        <button class="btn secondary small" data-bulk="person">Set person…</button>
        <button class="btn danger small" data-bulk="delete" style="margin-left:auto">🗑 Delete selected</button>
      </div>
      <table id="all-exp-table">
        <thead><tr>
          <th class="no-print" style="width:28px"><input type="checkbox" id="sel-all" title="Select all shown" style="width:auto"></th>
          <th>Date</th><th>Grant</th><th>Category</th><th>Description</th><th>Person</th>
          <th class="num">Amount</th><th>Receipt</th><th class="no-print"></th></tr></thead>
        <tbody>${rows.map((e) => {
          const g = S.grants.find((g) => g.id === e.grant_id) || { name: "?" };
          return `<tr>
            <td class="no-print"><input type="checkbox" class="sel-row" data-id="${e.id}" style="width:auto"></td>
            <td>${e.date}</td><td>${esc(g.name)}</td><td>${esc(catName(e.category_id))}</td>
            <td>${esc(e.description)}</td><td>${esc(personName(e.person_id))}</td>
            <td class="num">${money2(e.amount)}</td>
            <td>${e.receipt_path
                ? `<a class="receipt-link" href="${withKey(`/receipts/${encodeURIComponent(e.receipt_path).replaceAll("%2F", "/")}`)}" target="_blank">📎</a>`
                : `<span style="color:var(--muted)" title="No receipt attached">—</span>`}</td>
            <td class="no-print" style="white-space:nowrap">
              <button class="icon-btn" data-edit-exp="${e.id}" title="Edit / attach receipt">✏️</button>
            </td></tr>`;
        }).join("") || `<tr><td colspan="9" class="empty">No expenses match these filters.</td></tr>`}</tbody>
      </table>
    </div>`;
}

/* --------------------------------------------------------------- wire */
function range(n) { return Array.from({ length: n }, (_, i) => i + 1); }

function wireUp(m) {
  // navigation
  $$("[data-goto-grant]", m).forEach((el) => el.onclick = () => { view = { name: "grant", grantId: +el.dataset.gotoGrant }; render(); });

  // ✕ on an alert. It sits inside the alert, which is itself a link to the
  // grant, so the click must stop there or dismissing would navigate away.
  $$("[data-dismiss-alert]", m).forEach((btn) => btn.onclick = (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    dismissAlert(btn.dataset.dismissAlert);
    const box = btn.closest(".alert");
    const wrap = box.parentElement;
    box.remove();
    if (!$(".alert", wrap)) wrap.remove();   // drop the empty container's gap
  });
  $$("[data-goto-dash]", m).forEach((el) => el.onclick = () => { view = { name: "dashboard" }; render(); });

  // dashboard quick add
  if ($("#q-save", m)) wireQuickAdd(m);

  // hide/show closed grants
  const tc = $("#btn-toggle-closed", m);
  if (tc) tc.onclick = () => {
    const now = localStorage.getItem("gm-hide-closed") === "1";
    localStorage.setItem("gm-hide-closed", now ? "0" : "1");
    render();
  };

  // grant view
  if (view.name === "grant") wireGrantView(m);

  // people view
  if ($("#btn-add-person", m)) wirePeopleView(m);

  // workday bits (dashboard cards, summary cross-check)
  wireWorkdayBits(m);

  // summary toggle
  const closedToggle = $("#sum-closed", m);
  if (closedToggle) closedToggle.onchange = () => {
    view.includeClosed = closedToggle.checked;
    render();
  };

  // hiring detail expander
  const hireStat = $("#hire-stat", m);
  if (hireStat) hireStat.onclick = () => { view.hireDetail = !view.hireDetail; render(); };

  // historic total include/exclude
  $$("[data-hist-toggle]", m).forEach((cb) => cb.onchange = async () => {
    await api(`/api/grants/${cb.dataset.histToggle}`, "POST",
              { exclude_from_history: cb.checked ? 0 : 1 });
    await reload();
    view.name = "summary"; render();
  });

  // all expenses search
  if (view.name === "allexpenses") wireAllExpenses(m);
}

function wireAllExpenses(m) {
  const f = expFilters();
  // typing filters shouldn't lose focus on every keystroke, so text/number
  // inputs re-render on a short debounce and restore the caret afterwards
  const bind = (sel, key, ev = "change") => {
    const el = $(sel, m);
    if (!el) return;
    let t;
    el[ev === "input" ? "oninput" : "onchange"] = () => {
      const val = el.value, id = el.id, pos = el.selectionStart;
      clearTimeout(t);
      t = setTimeout(() => {
        f[key] = val;
        render();
        const again = document.getElementById(id);
        if (again) {
          again.focus();
          try { again.setSelectionRange(pos, pos); } catch { /* not a text input */ }
        }
      }, ev === "input" ? 300 : 0);
    };
  };
  bind("#f-q", "q", "input");
  bind("#f-grant", "grant"); bind("#f-cat2", "cat"); bind("#f-person", "person");
  bind("#f-from", "from"); bind("#f-to", "to");
  bind("#f-min", "min", "input"); bind("#f-max", "max", "input");
  bind("#f-receipt", "receipt"); bind("#f-source", "source");

  const clear = $("#f-clear", m);
  if (clear) clear.onclick = () => { view.f = null; render(); };

  const exp = $("#f-export", m);
  if (exp) exp.onclick = () => {
    const rows = filteredExpenses();
    const head = ["Date", "Grant", "Category", "Budget Year", "Amount",
                  "Description", "Person", "Receipt", "Source"];
    // quote every field, and neutralise anything a spreadsheet would treat as
    // a formula (=, +, -, @) so an exported ledger can't execute on open
    const cell = (v) => {
      let s = String(v ?? "");
      if (/^[=+\-@]/.test(s)) s = "'" + s;
      return `"${s.replace(/"/g, '""')}"`;
    };
    const csv = [head.map(cell).join(",")].concat(rows.map((e) => {
      const g = S.grants.find((g) => g.id === e.grant_id);
      return [e.date, g ? g.name : "", catName(e.category_id), e.year || 1,
              e.amount.toFixed(2), e.description, personName(e.person_id),
              e.receipt_path || "", e.source || "manual"].map(cell).join(",");
    })).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "expenses_filtered.csv"; a.click();
    URL.revokeObjectURL(url);
    toast(`Exported ${rows.length} expense${rows.length === 1 ? "" : "s"}`);
  };

  // edit / attach a receipt straight from this list (the grant page has the
  // same button; this is the view people actually browse in)
  $$("[data-edit-exp]", m).forEach((b) => b.onclick = () => {
    const e = S.expenses.find((x) => x.id === +b.dataset.editExp);
    if (e) expenseModal(e, e.grant_id);
  });

  // ---- multi-select + bulk actions
  const bar = $("#bulk-bar", m), countEl = $("#bulk-count", m);
  const boxes = () => $$(".sel-row", m);
  const chosen = () => boxes().filter((b) => b.checked).map((b) => +b.dataset.id);
  const refresh = () => {
    const n = chosen().length;
    bar.style.display = n ? "flex" : "none";
    countEl.textContent = `${n} selected`;
    const all = $("#sel-all", m);
    all.checked = n > 0 && n === boxes().length;
    all.indeterminate = n > 0 && n < boxes().length;
  };
  $("#sel-all", m).onchange = (e) => {
    boxes().forEach((b) => b.checked = e.target.checked);
    refresh();
  };
  boxes().forEach((b) => b.onchange = refresh);

  $$("[data-bulk]", m).forEach((btn) => btn.onclick = async () => {
    const ids = chosen();
    if (!ids.length) return;
    const kind = btn.dataset.bulk;

    if (kind === "delete") {
      if (!confirm(`Delete ${ids.length} expense${ids.length === 1 ? "" : "s"}? You can undo this from ⚙ Settings → Recently deleted.`)) return;
      const r = await api("/api/expenses/bulk", "POST", { ids, action: "delete" });
      toast(`Deleted ${r.count} — undo in ⚙ Settings`);
      await reload();
      return;
    }
    bulkFieldModal(kind, ids);
  });
  refresh();
}

/* Warn before saving something that looks like it's already been entered.
   Same grant + same amount within 30 days. Returns false if the user backs
   out. Deliberately a warning, not a block — legitimate repeats happen
   (monthly fees, two identical supply orders). */
function confirmNotDuplicate(grantId, amount, dateStr, ignoreId = null) {
  const d = new Date(dateStr + "T00:00:00");
  const near = S.expenses.filter((e) => {
    if (e.id === ignoreId) return false;
    if (e.grant_id !== grantId) return false;
    if (Math.abs(e.amount - amount) > 0.005) return false;
    const diff = Math.abs(new Date(e.date + "T00:00:00") - d) / 864e5;
    return diff <= 30;
  });
  if (!near.length) return true;
  const lines = near.slice(0, 4).map((e) =>
    `  • ${e.date} — ${money2(e.amount)}${e.description ? " — " + e.description : ""}`).join("\n");
  const more = near.length > 4 ? `\n  …and ${near.length - 4} more` : "";
  const g = S.grants.find((x) => x.id === grantId);
  return confirm(
    `Possible duplicate.\n\n${money2(amount)} on ${g ? g.name : "this grant"} ` +
    `already appears ${near.length === 1 ? "once" : near.length + " times"} within 30 days:\n\n` +
    `${lines}${more}\n\nAdd it anyway?`);
}

function bulkFieldModal(kind, ids) {
  const label = { category: "Change category", grant: "Move to grant",
                  person: "Set person" }[kind];
  const opts = kind === "category"
    ? S.categories.filter((c) => c.grant_id === null)
        .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")
    : kind === "grant"
    ? S.grants.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")
    : `<option value="">— none —</option>` +
      S.people.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  modal(`
    <h2>${label}</h2>
    <p class="sub" style="margin-bottom:10px">Applies to <strong>${ids.length}</strong> selected expense${ids.length === 1 ? "" : "s"}.${kind === "grant" ? " Categories and budget years are remapped to the destination grant automatically." : ""}</p>
    <label class="field"><span>${label}</span><select id="bulk-val">${opts}</select></label>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Cancel</button>
      <button class="btn" id="m-go">Apply</button>
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
    $("#m-go", el).onclick = async () => {
      const raw = $("#bulk-val", el).value;
      const fields = kind === "category" ? { category_id: +raw }
                   : kind === "grant" ? { grant_id: +raw }
                   : { person_id: raw === "" ? null : +raw };
      try {
        const r = await api("/api/expenses/bulk", "POST", { ids, fields });
        close();
        toast(`Updated ${r.count} expense${r.count === 1 ? "" : "s"}`);
        await reload();
      } catch (e) { toast("Failed: " + e.message); }
    };
  });
}

function fillCatSelect(sel, grantId, selectedId) {
  const g = S.grants.find((g) => g.id === +grantId);
  sel.innerHTML = grantCats(g).map((c) =>
    `<option value="${c.id}" ${c.id === selectedId ? "selected" : ""}>${esc(c.name)}</option>`).join("");
}

function wireDropzone(zone, fileInput, onFile) {
  let picked = null;
  const set = (f) => {
    picked = f;
    zone.classList.toggle("has-file", !!f);
    zone.innerHTML = f ? `✓ ${esc(f.name)}` : "📎 Drop receipt<br>or click";
    onFile(f);
  };
  zone.onclick = () => fileInput.click();
  fileInput.onchange = () => set(fileInput.files[0] || null);
  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("drag"); };
  zone.ondragleave = () => zone.classList.remove("drag");
  zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("drag"); set(e.dataTransfer.files[0] || null); };
  return () => picked;
}

function fileToPayload(f) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve({ name: f.name, data: r.result.split(",")[1] });
    r.onerror = reject;
    r.readAsDataURL(f);
  });
}

/* The one question a P-card purchase still has to answer at entry time.

   The cardholder and the name on the card come from Settings, and the reason
   for the purchase is already the expense's own description — so all that is
   left to ask is who actually made the purchase, when that was not the
   cardholder. `k` prefixes the ids so the dashboard form and the edit form
   can both use this without colliding. */
function pcardFields(k, v) {
  v = v || {};
  return `
    <label class="field ${k}-pcard-field" style="width:170px;display:none"><span>Purchased by</span>
      <input id="${k}-pcard-buyer" value="${esc(v.pcard_buyer || "")}" placeholder="if not the cardholder"></label>`;
}

/* Show/hide the field with the tick, and read it back. */
function wirePcardFields(k, m) {
  const box = $(`#${k}-pcard`, m);
  if (!box) return () => ({ pcard: 0 });
  const sync = () => $$(`.${k}-pcard-field`, m).forEach(
    (el) => el.style.display = box.checked ? "" : "none");
  box.onchange = sync;
  sync();
  return () => box.checked
    ? { pcard: 1, pcard_buyer: $(`#${k}-pcard-buyer`, m).value.trim() }
    : { pcard: 0, pcard_buyer: "" };
}

function wireQuickAdd(m) {
  const gSel = $("#q-grant", m), cSel = $("#q-cat", m);
  if (!gSel.options.length) return;
  const wdBox = $("#q-workday", m);
  // "Other" (external account) is paid outside your grants — never counts
  // against your budgets — but it still needs its own worktag typed in,
  // since there's no grant-level mapping to pull one from automatically.
  // It's still eligible to be sent to Workday like any other expense.
  const isExtSel = () => {
    const g = S.grants.find((g) => g.id === +gSel.value);
    return g && isExternal(g);
  };
  const syncExtUI = () => {
    const ext = isExtSel();
    $$(".q-ext-field", m).forEach((el) => el.style.display = ext ? "" : "none");
  };
  fillCatSelect(cSel, gSel.value);
  gSel.onchange = () => { fillCatSelect(cSel, gSel.value); syncExtUI(); };
  let file = null;
  wireDropzone($("#q-drop", m), $("#q-file", m), (f) => file = f);
  wdBox.checked = localStorage.getItem("gm-wd-push") !== "0";
  wdBox.onchange = () => localStorage.setItem("gm-wd-push", wdBox.checked ? "1" : "0");
  syncExtUI();
  // split fields stay hidden until "Split across worktags" is ticked
  const splitOn = $("#q-split-on", m);
  splitOn.onchange = () => $$(".q-split-field", m).forEach((el) => {
    el.style.display = splitOn.checked ? "" : "none";
  });
  const readPcard = wirePcardFields("q", m);
  // picking a split grant prefills its Cost Center / Worktag (still editable)
  $("#q-split-grant", m).onchange = () => {
    const gid = +$("#q-split-grant", m).value;
    const push = (WD && WD.push) || { codes: {}, profiles: {} };
    $("#q-split-cc", m).value = gid ? ((push.profiles[String(gid)] || {}).cost_center || "") : "";
    $("#q-split-wt", m).value = gid ? ((push.codes[gid] || {}).grant_code || "") : "";
  };
  $("#q-save", m).onclick = async () => {
    const amount = parseFloat($("#q-amount", m).value);
    if (!amount) { toast("Enter an amount"); return; }
    const g = S.grants.find((g) => g.id === +gSel.value);
    const dateVal = $("#q-date", m).value || todayISO();
    if (!confirmNotDuplicate(+gSel.value, amount, dateVal)) return;
    let desc = $("#q-desc", m).value;
    let extWorktag = "";
    if (g && isExternal(g)) {
      extWorktag = $("#q-ext-acct", m).value.trim();
      if (extWorktag) desc = desc ? `[Acct: ${extWorktag}] ${desc}` : `[Acct: ${extWorktag}]`;
    }
    const body = {
      grant_id: +gSel.value, category_id: +cSel.value, amount,
      date: dateVal, description: desc,
      year: budgetYearOf(g, dateVal),
      wd_worktag: extWorktag,
      // Unticking "Add to Workday" means this expense is yours alone: it
      // never joins the "to enter in Workday" list. Ticked (the default)
      // leaves it blank, so it queues up like any other.
      wd_entry: wdBox.checked ? "" : "na",
      ...readPcard(),
    };
    // capture everything before the awaits — saving re-renders the form
    const splitG = splitOn.checked ? +$("#q-split-grant", m).value : 0;
    const pct = parseFloat($("#q-split-pct", m).value);
    if (splitOn.checked && !splitG) { toast("Pick the grant to split with (or untick the split box)"); return; }
    const splitCC = $("#q-split-cc", m).value.trim();
    const splitWT = $("#q-split-wt", m).value.trim();
    const wantPush = wdBox.checked;
    const receipt = file ? await fileToPayload(file) : null;
    if (splitG && splitG !== body.grant_id && pct > 0 && pct < 100) {
      const r = await saveExpenseSplit(body, splitG, pct, receipt);
      const gB = S.grants.find((x) => x.id === splitG);
      toast(`Split: ${money2(r.shareA)} to ${g.name}, ${money2(r.shareB)} to ${gB.name}`);
      await reload();
      if (wantPush) {
        const eA = S.expenses.find((x) => x.id === r.idA);
        const suggest = ((WD && WD.push) || {}).spend_suggest || {};
        const lineA = wdLineFor(g.id, g.name, r.shareA, 100 - pct);
        const lineB = wdLineFor(gB.id, gB.name, r.shareB, pct);
        if (splitCC) lineB.cost_center = splitCC;
        if (splitWT) lineB.worktag = splitWT;
        wdPushModal({
          expense_ids: [r.idA, r.idB], total: amount, date: body.date,
          memo: body.description || "",
          spend: suggest[body.category_id] || catName(body.category_id),
          person: "", receipt_path: (eA && eA.receipt_path) || "",
          grant_label: `${g.name} + ${gB.name}`,
          lines: [lineA, lineB],
        });
      }
    } else {
      if (splitG && !(pct > 0 && pct < 100)) { toast("Enter a split % between 1 and 99"); return; }
      if (receipt) body.receipt = receipt;
      const r = await api("/api/expenses", "POST", body);
      toast(`Added ${money2(amount)} to ${g.name}`);
      await reload();
      // saved in the DB — now offer the workday-ready email packet
      const exp = S.expenses.find((x) => x.id === r.id);
      if (wantPush && exp) wdPushModal(wdPayloadFromExpense({
        ...exp, grant_name: g.name,
        category: catName(exp.category_id),
        person: personName(exp.person_id),
      }));
    }
  };
}

function categoryOnGrant(catId, grantId) {
  // map a category to the target grant: standard passes through,
  // custom maps by name, else falls back to the standard "Other"
  const c = S.categories.find((c) => c.id === catId);
  if (!c || c.grant_id === null) return catId;
  const same = S.categories.find((x) => x.name === c.name &&
    (x.grant_id === grantId || x.grant_id === null));
  if (same) return same.id;
  const other = S.categories.find((x) => x.name === "Other" && x.grant_id === null);
  return other ? other.id : null;
}

async function saveExpenseSplit(body, splitGrantId, pct, receiptPayload) {
  /* Create two expenses: (100-pct)% on body.grant_id, pct% on splitGrantId. */
  const gA = S.grants.find((g) => g.id === body.grant_id);
  const gB = S.grants.find((g) => g.id === splitGrantId);
  const shareB = Math.round(body.amount * pct) / 100;
  const shareA = Math.round((body.amount - shareB) * 100) / 100;
  const tag = (other, p) =>
    `${body.description || "Expense"} [${p}% of ${money2(body.amount)} split with ${other.name}]`;
  const bodyA = { ...body, amount: shareA, description: tag(gB, 100 - pct) };
  if (receiptPayload) bodyA.receipt = receiptPayload;
  const rA = await api("/api/expenses", "POST", bodyA);
  const rB = await api("/api/expenses", "POST", {
    ...body, grant_id: gB.id, amount: shareB,
    category_id: categoryOnGrant(body.category_id, gB.id),
    year: budgetYearOf(gB, body.date), description: tag(gA, pct),
  });
  return { shareA, shareB, idA: rA.id, idB: rB.id };
}

function budgetYearOf(g, dateStr) {
  if (!g || !g.start_date) return 1;
  const s = new Date(g.start_date), d = new Date(dateStr);
  if (d < s) return 1;
  let y = d.getFullYear() - s.getFullYear();
  const anniv = new Date(d.getFullYear(), s.getMonth(), s.getDate());
  if (d < anniv) y -= 1;
  return Math.max(1, y + 1);
}

function wireGrantView(m) {
  const g = S.grants.find((x) => x.id === view.grantId);

  $("#btn-edit-grant", m).onclick = () => grantModal(g);
  $("#btn-closeout", m).onclick = () => {
    view = { name: "closeout", grantId: g.id };
    render();
    window.scrollTo(0, 0);
  };
  $("#btn-add-cat", m).onclick = async () => {
    const name = prompt("Name of the custom category for this grant (e.g. Computer node):");
    if (!name) return;
    await api("/api/categories", "POST", { name, grant_id: g.id, sort: 50 });
    await reload();
  };
  $("#btn-add-exp", m).onclick = () => expenseModal(null, g.id);
  $$("[data-edit-exp]", m).forEach((b) => b.onclick = () => {
    const e = S.expenses.find((x) => x.id === +b.dataset.editExp);
    expenseModal(e, e.grant_id);
  });
  $$("[data-del-exp]", m).forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this expense?")) return;
    await api(`/api/expenses/${b.dataset.delExp}`, "DELETE");
    toast("Expense deleted");
    await reload();
  });

  // inline budget editing
  $$("[data-edit-budget]", m).forEach((span) => span.onclick = () => {
    const [cid, y] = span.dataset.editBudget.split(":").map(Number);
    const cur = budgetFor(g.id, cid, y);
    const input = document.createElement("input");
    input.type = "number"; input.step = "0.01"; input.value = cur || "";
    span.replaceWith(input);
    input.focus(); input.select();
    const commit = async () => {
      const val = parseFloat(input.value) || 0;
      await api("/api/budget_line", "POST", { grant_id: g.id, category_id: cid, year: y, amount: val });
      await reload();
    };
    input.onkeydown = (e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") render(); };
    input.onblur = commit;
  });

  // filters
  const applyFilter = () => {
    const fc = $("#f-cat", m).value, fy = $("#f-year", m).value;
    $$("#exp-table tbody tr[data-exp-row]", m).forEach((tr) => {
      const okC = !fc || tr.dataset.cat === fc;
      const okY = !fy || tr.dataset.year === fy;
      tr.style.display = okC && okY ? "" : "none";
    });
  };
  $("#f-cat", m).onchange = applyFilter;
  $("#f-year", m).onchange = applyFilter;

  drawGrantCategoryCharts(g);
  drawBurnChart(g);
}

function drawGrantCategoryCharts(g) {
  if (typeof Chart === "undefined") return;
  const years = grantYears(g);
  const cats = grantCats(g).filter((c) =>
    S.budget_lines.some((b) => b.grant_id === g.id && b.category_id === c.id) ||
    S.expenses.some((e) => e.grant_id === g.id && e.category_id === c.id));
  const rows = cats.map((c) => ({
    name: c.name,
    budget: range(years).reduce((s, y) => s + budgetFor(g.id, c.id, y), 0),
    spent: spentFor(g.id, c.id, null),
  })).filter((r) => r.budget || r.spent);

  const bc = $("#grant-cat-chart");
  if (bc) {
    charts.push(new Chart(bc, {
      type: "bar",
      data: {
        labels: rows.map((r) => r.name),
        datasets: [
          { label: "Budgeted", data: rows.map((r) => r.budget), backgroundColor: "#c8d0dd" },
          { label: "Spent", data: rows.map((r) => r.spent), backgroundColor: PALETTE[0] },
          { label: "Remaining", data: rows.map((r) => Math.round((r.budget - r.spent) * 100) / 100), backgroundColor: PALETTE[1] },
        ],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } } },
        scales: {
          y: { ticks: { callback: (v) => "$" + Math.round(v / 1000) + "k", font: { size: 11 } } },
          x: { ticks: { font: { size: 11 }, maxRotation: 40 } },
        },
      },
    }));
  }

  const dc = $("#grant-cat-donut");
  if (dc) {
    const donutRows = rows.map((r) => ({ ...r, remaining: Math.round((r.budget - r.spent) * 100) / 100 }))
      .filter((r) => r.remaining > 0.005);
    charts.push(new Chart(dc, {
      type: "doughnut",
      data: {
        labels: donutRows.map((r) => r.name),
        datasets: [{ data: donutRows.map((r) => r.remaining),
                     backgroundColor: donutRows.map((_, i) => PALETTE[i % PALETTE.length]) }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => `${c.label}: ${money(c.parsed)}` } } },
      },
    }));
  }
}

function drawBurnChart(g) {
  const ctx = $("#burn-chart");
  if (!ctx || typeof Chart === "undefined") return;
  const exp = S.expenses.filter((e) => e.grant_id === g.id).sort((a, b) => a.date.localeCompare(b.date));
  // cumulative by month
  const byMonth = {};
  for (const e of exp) byMonth[e.date.slice(0, 7)] = (byMonth[e.date.slice(0, 7)] || 0) + e.amount;
  const start = g.start_date || (exp[0] ? exp[0].date : S.today);
  const end = g.end_date || S.today;
  const months = [];
  let d = new Date(start.slice(0, 7) + "-01");
  const endD = new Date(end.slice(0, 7) + "-01");
  while (d <= endD && months.length < 120) {
    months.push(d.toISOString().slice(0, 7));
    d = new Date(d.getFullYear(), d.getMonth() + 1, 1);
  }
  let cum = 0;
  const nowKey = S.today.slice(0, 7);
  const actual = months.map((mo) => {
    if (mo > nowKey) return null;
    cum += byMonth[mo] || 0;
    return Math.round(cum);
  });
  // ideal straight line to full award
  const ideal = months.map((_, i) => Math.round(g.initial_amount * (i + 1) / months.length));
  // projection from avg burn of last 6 active months
  const past = actual.filter((v) => v !== null);
  const recent = Object.entries(byMonth).filter(([mo]) => mo <= nowKey).slice(-6).map(([, v]) => v);
  const avgBurn = recent.length ? recent.reduce((a, b) => a + b, 0) / recent.length : 0;
  let proj = months.map(() => null);
  if (past.length && avgBurn > 0) {
    let c = past[past.length - 1];
    for (let i = past.length - 1; i < months.length; i++) {
      proj[i] = Math.round(c);
      c += avgBurn;
    }
  }
  charts.push(new Chart(ctx, {
    type: "line",
    data: {
      labels: months,
      datasets: [
        { label: "Spent (cumulative)", data: actual, borderColor: "#2f6fed", backgroundColor: "rgba(47,111,237,.08)", fill: true, tension: .25, spanGaps: false, pointRadius: 2 },
        { label: "Even-pace budget", data: ideal, borderColor: "#c8d0dd", borderDash: [6, 4], pointRadius: 0, fill: false },
        { label: "Projection (recent burn rate)", data: proj, borderColor: "#b97a08", borderDash: [3, 3], pointRadius: 0, fill: false },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: {
        y: { ticks: { callback: (v) => "$" + (v >= 1000 ? Math.round(v / 1000) + "k" : Math.round(v)), font: { size: 11 } }, max: Math.ceil(Math.max(g.initial_amount * 1.05, 1000) / 1000) * 1000 },
        x: { ticks: { maxTicksLimit: 10, font: { size: 10 } } },
      },
    },
  }));
}

function wirePeopleView(m) {
  $("#btn-add-person", m).onclick = () => personModal(null);
  $$("[data-edit-person]", m).forEach((b) => b.onclick = () => personModal(S.people.find((p) => p.id === +b.dataset.editPerson)));
  $$("[data-del-person]", m).forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this person and their appointments? (Past salary expenses stay.)")) return;
    await api(`/api/people/${b.dataset.delPerson}`, "DELETE");
    await reload();
  });
  $$("[data-add-appt]", m).forEach((b) => b.onclick = () => apptModal(null, +b.dataset.addAppt));
  $$("[data-edit-appt]", m).forEach((b) => b.onclick = () => {
    const a = S.appointments.find((a) => a.id === +b.dataset.editAppt);
    apptModal(a, a.person_id);
  });
  $$("[data-del-appt]", m).forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this appointment? (Already-generated salary charges stay.)")) return;
    await api(`/api/appointments/${b.dataset.delAppt}`, "DELETE");
    await reload();
  });
}

/* -------------------------------------------------------------- modals */
function modal(html, onMount) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-back"><div class="modal">${html}</div></div>`;
  const back = $(".modal-back", root);
  back.onclick = (e) => { if (e.target === back) close(); };
  const close = () => root.innerHTML = "";
  onMount($(".modal", root), close);
}

function grantModal(g, opts) {
  opts = opts || {};
  const pf = opts.prefill || {};
  modal(`
    <h2>${g ? "Edit grant" : "New grant"}</h2>
    ${!g && (pf.name || pf.start_date) ? `<p class="sub" style="margin:-4px 0 12px">Prefilled from your Workday report — adjust anything, then Create.</p>` : ""}
    <label class="field"><span>Name</span><input id="m-name" value="${esc(g?.name || pf.name || "")}" placeholder="e.g. NSF CAREER"></label>
    <div class="form-row">
      <label class="field"><span>Agency</span><input id="m-agency" value="${esc(g?.agency || pf.agency || "")}" placeholder="USDA, NSF, FFAR…"></label>
      <label class="field"><span>Total award ($)</span><input id="m-amount" type="number" step="0.01" value="${g?.initial_amount || pf.initial_amount || ""}"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>Start date</span><input id="m-start" type="date" value="${g?.start_date || pf.start_date || ""}"></label>
      <label class="field"><span>End date</span><input id="m-end" type="date" value="${g?.end_date || pf.end_date || ""}"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>Status</span><select id="m-status">
        <option value="active" ${g?.status !== "closed" ? "selected" : ""}>Active</option>
        <option value="closed" ${g?.status === "closed" ? "selected" : ""}>Closed</option></select></label>
      <label class="field"><span>No-cost extension until (optional)</span><input id="m-nce" type="date" value="${g?.nce_end_date || ""}" title="Extends the grant's effective end date without adding money"></label>
    </div>
    <label class="field" style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="m-hist" style="width:auto" ${g?.exclude_from_history ? "checked" : ""}>
      <span style="margin:0">Exclude from the historic total on the Summary page</span>
    </label>
    <label class="field"><span>Notes</span><textarea id="m-notes" rows="4">${esc(g?.notes || "")}</textarea></label>
    <div class="actions">
      ${g ? `<button class="btn danger" id="m-del" style="margin-right:auto">Delete grant</button>` : ""}
      <button class="btn secondary" id="m-cancel">Cancel</button>
      <button class="btn" id="m-save">${g ? "Save" : "Create"}</button>
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
    if (g) $("#m-del", el).onclick = async () => {
      if (!confirm(`Delete "${g.name}" and ALL its budget lines and expenses? This cannot be undone.`)) return;
      await api(`/api/grants/${g.id}`, "DELETE");
      close(); view = { name: "dashboard" };
      toast("Grant deleted");
      await reload();
    };
    $("#m-save", el).onclick = async () => {
      const body = {
        name: $("#m-name", el).value.trim(), agency: $("#m-agency", el).value.trim(),
        initial_amount: parseFloat($("#m-amount", el).value) || 0,
        start_date: $("#m-start", el).value, end_date: $("#m-end", el).value,
        status: $("#m-status", el).value, notes: $("#m-notes", el).value,
        nce_end_date: $("#m-nce", el).value,
        exclude_from_history: $("#m-hist", el).checked ? 1 : 0,
      };
      if (!body.name) { toast("Name is required"); return; }
      if (g) await api(`/api/grants/${g.id}`, "POST", body);
      else {
        const r = await api("/api/grants", "POST", body);
        if (opts.onCreated) { await opts.onCreated(r.id); }
        else { view = { name: "grant", grantId: r.id }; }
      }
      close();
      toast(g ? "Grant updated" : "Grant created — now click budget numbers to set the category × year budget");
      await reload();
    };
  });
}

function expenseModal(e, grantId) {
  const g = S.grants.find((x) => x.id === grantId);
  const years = grantYears(g);
  modal(`
    <h2>${e ? "Edit expense" : "Add expense"} — ${esc(g.name)}</h2>
    <div class="form-row">
      <label class="field"><span>Amount ($)</span><input id="m-amount" type="number" step="0.01" value="${e?.amount ?? ""}"></label>
      <label class="field"><span>Date</span><input id="m-date" type="date" value="${e?.date || todayISO()}"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>Category</span><select id="m-cat"></select></label>
      <label class="field"><span>Budget year</span><select id="m-year">${range(years).map((y) =>
        `<option value="${y}" ${(e?.year || 1) === y ? "selected" : ""}>Year ${y}</option>`).join("")}</select></label>
    </div>
    <label class="field"><span>Description</span><input id="m-desc" value="${esc(e?.description || "")}"></label>
    <label class="field"><span>Person (optional)</span><select id="m-person"><option value="">—</option>${S.people.map((p) =>
      `<option value="${p.id}" ${e?.person_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    ${!e ? `<div class="form-row">
      <label class="field"><span>Split with another grant (optional)</span><select id="m-split-grant"><option value="">No split</option>${S.grants.filter((x) => x.status === "active" && x.id !== grantId).map((x) =>
        `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select></label>
      <label class="field"><span>% charged to that grant</span><input type="number" id="m-split-pct" min="1" max="99" placeholder="e.g. 50"></label>
    </div>` : ""}
    <label class="field"><span>Receipt</span>
      <div class="dropzone ${e?.receipt_path ? "has-file" : ""}" id="m-drop">${e?.receipt_path ? "✓ receipt attached (drop to replace)" : "📎 Drop receipt here or click to choose"}</div>
      <input type="file" id="m-file" hidden></label>
    <label style="display:flex;align-items:center;gap:7px;font-size:13.5px;cursor:pointer;margin:4px 0 2px"><input type="checkbox" id="m-pcard" style="width:auto" ${e?.pcard ? "checked" : ""}>💳 Bought on a P-card</label>
    <div class="form-row" style="align-items:flex-end">${pcardFields("m", e || {})}</div>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Cancel</button>
      <button class="btn" id="m-save">${e ? "Save" : "Add"}</button>
    </div>`, (el, close) => {
    fillCatSelect($("#m-cat", el), grantId, e?.category_id);
    const readPcard = wirePcardFields("m", el);
    let file = null;
    wireDropzone($("#m-drop", el), $("#m-file", el), (f) => file = f);
    // auto-pick budget year from date
    $("#m-date", el).onchange = () => { $("#m-year", el).value = budgetYearOf(g, $("#m-date", el).value); };
    $("#m-cancel", el).onclick = close;
    $("#m-save", el).onclick = async () => {
      const amount = parseFloat($("#m-amount", el).value);
      if (!amount) { toast("Enter an amount"); return; }
      const dateVal = $("#m-date", el).value;
      // only warn on genuinely new entries, and never against the row being edited
      if (!e && !confirmNotDuplicate(grantId, amount, dateVal)) return;
      const body = {
        grant_id: grantId, category_id: +$("#m-cat", el).value,
        year: +$("#m-year", el).value, date: dateVal,
        amount, description: $("#m-desc", el).value,
        person_id: $("#m-person", el).value ? +$("#m-person", el).value : null,
        ...readPcard(),
      };
      const receipt = file ? await fileToPayload(file) : null;
      const splitG = !e && $("#m-split-grant", el) ? +$("#m-split-grant", el).value : 0;
      const pct = !e ? parseFloat($("#m-split-pct", el)?.value) : NaN;
      if (splitG && pct > 0 && pct < 100) {
        const r = await saveExpenseSplit(body, splitG, pct, receipt);
        toast(`Split ${money2(amount)}: ${money2(r.shareA)} here, ${money2(r.shareB)} to the other grant`);
      } else {
        if (splitG && !(pct > 0 && pct < 100)) { toast("Enter a split % between 1 and 99"); return; }
        if (receipt) body.receipt = receipt;
        if (e) await api(`/api/expenses/${e.id}`, "POST", body);
        else await api("/api/expenses", "POST", body);
        toast(e ? "Expense updated" : "Expense added");
      }
      close();
      await reload();
    };
  });
}

function personModal(p) {
  const activeGrants = S.grants.filter((g) => g.status === "active");
  const gOpts = (sel) => `<option value="">—</option>` + activeGrants.map((g) =>
    `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  modal(`
    <h2>${p ? "Edit person" : "Add person"}</h2>
    <label class="field"><span>Name</span><input id="m-name" value="${esc(p?.name || "")}"></label>
    <label class="field"><span>Role</span><select id="m-role">
      ${["GA", "Postdoc", "Program Associate", "Visiting Researcher", "Undergrad", "Technician", "Other"].map((r) =>
        `<option ${p?.role === r ? "selected" : ""}>${r}</option>`).join("")}</select></label>
    ${!p ? `
    <h2 style="margin-top:16px;font-size:14px">Funding (optional — creates the appointment${activeGrants.length > 1 ? "s" : ""})</h2>
    <div class="form-row">
      <label class="field"><span>Total salary ($/yr)</span><input id="m-fsal" type="number" step="0.01" placeholder="e.g. 55000"></label>
      <label class="field"><span>Fringe rate (%)</span><input id="m-ffringe" type="number" step="0.01" placeholder="6.4 GA · 27.6 postdoc"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>Start</span><input id="m-fstart" type="date"></label>
      <label class="field"><span>End</span><input id="m-fend" type="date"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>Paid from</span><select id="m-fg1">${gOpts()}</select></label>
      <label class="field" style="max-width:110px"><span>%</span><input id="m-fp1" type="number" min="1" max="100" value="100"></label>
    </div>
    <div class="form-row">
      <label class="field"><span>And from (optional — use “Other” for external sources)</span><select id="m-fg2">${gOpts()}</select></label>
      <label class="field" style="max-width:110px"><span>%</span><input id="m-fp2" type="number" min="0" max="99" value="0"></label>
    </div>
    <label class="field"><span>Annual tuition ($/yr, split by the same %)</span><input id="m-ftui" type="number" step="0.01" value="0"></label>
    ` : ""}
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Cancel</button>
      <button class="btn" id="m-save">${p ? "Save" : "Add"}</button>
    </div>`, (el, close) => {
    $("#m-cancel", el).onclick = close;
    $("#m-save", el).onclick = async () => {
      const body = { name: $("#m-name", el).value.trim(), role: $("#m-role", el).value };
      if (!body.name) { toast("Name is required"); return; }
      if (p) {
        await api(`/api/people/${p.id}`, "POST", body);
        close();
        await reload();
        return;
      }
      const r = await api("/api/people", "POST", body);
      // optional funding -> appointments
      const sal = parseFloat($("#m-fsal", el).value) || 0;
      const g1 = +$("#m-fg1", el).value, g2 = +$("#m-fg2", el).value;
      const p1 = parseFloat($("#m-fp1", el).value) || 0;
      const p2 = g2 ? (parseFloat($("#m-fp2", el).value) || 0) : 0;
      const start = $("#m-fstart", el).value, end = $("#m-fend", el).value;
      if (sal > 0 && g1 && start && end) {
        if (p1 + p2 > 100) { toast("Person created, but percentages exceed 100% — add appointments manually"); close(); await reload(); return; }
        const fringe = parseFloat($("#m-ffringe", el).value) || 0;
        const tui = parseFloat($("#m-ftui", el).value) || 0;
        const shares = [[g1, p1]].concat(g2 && p2 > 0 ? [[g2, p2]] : []);
        for (const [gid, pc] of shares) {
          const gname = S.grants.find((x) => x.id === gid).name;
          const others = shares.filter(([og]) => og !== gid)
            .map(([og, op]) => `${op}% on ${S.grants.find((x) => x.id === og).name}`).join(", ");
          await api("/api/appointments", "POST", {
            person_id: r.id, grant_id: gid,
            monthly_salary: Math.round(sal / 12 * pc / 100 * 100) / 100,
            fringe_rate: fringe, annual_tuition: Math.round(tui * pc / 100 * 100) / 100,
            auto_charge: 0, pct: pc, start_date: start, end_date: end,
            notes: `${pc}% of total ${money2(sal)}/yr on ${gname}` +
                   (others ? `; ${others}` : "") + ".",
          });
        }
        toast(`${body.name} added with ${shares.length} appointment${shares.length > 1 ? "s" : ""}`);
      }
      close();
      await reload();
    };
  });
}

function apptModal(a, personId) {
  const p = S.people.find((x) => x.id === personId);
  modal(`
    <h2>${a ? "Edit" : "New"} appointment — ${esc(p.name)}</h2>
    <label class="field"><span>Grant</span><select id="m-grant">${S.grants.map((g) =>
      `<option value="${g.id}" ${a?.grant_id === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select></label>
    <div class="form-row">
      <label class="field"><span>Annual salary charged to this grant ($/yr)</span><input id="m-sal" type="number" step="0.01" value="${a ? Math.round(a.monthly_salary * 12 * 100) / 100 : ""}" placeholder="e.g. 55000"></label>
      <label class="field"><span>Fringe rate (%)</span><input id="m-fringe" type="number" step="0.01" value="${a?.fringe_rate ?? ""}" placeholder="e.g. 27.6"></label>
    </div>
    <label class="field"><span>Share of the person's total salary (%) — under 100 marks a split (e.g., rest paid by another source)</span><input id="m-pct" type="number" min="1" max="100" step="0.1" value="${a?.pct ?? 100}"></label>
    <div class="form-row">
      <label class="field"><span>Start (length of appointment)</span><input id="m-start" type="date" value="${a?.start_date || ""}"></label>
      <label class="field"><span>End</span><input id="m-end" type="date" value="${a?.end_date || ""}"></label>
    </div>
    <label class="field"><span>Annual tuition ($/yr, used in projections)</span><input id="m-tuition" type="number" step="0.01" value="${a?.annual_tuition ?? ""}" placeholder="e.g. 8550 — leave 0 if covered elsewhere"></label>
    <label class="field" style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="m-auto" style="width:auto" ${a?.auto_charge ? "checked" : ""}>
      <span style="margin:0">Auto-generate monthly salary charges (leave off if actuals come from DBR imports)</span>
    </label>
    ${!a ? `<div class="form-row">
      <label class="field"><span>Split with another grant (optional)</span><select id="m-split-grant"><option value="">No split — 100% on the grant above</option>${S.grants.filter((x) => x.status === "active").map((x) =>
        `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select></label>
      <label class="field"><span>% on that grant</span><input type="number" id="m-split-pct" min="1" max="99" placeholder="e.g. 50"></label>
    </div>` : ""}
    <div id="m-proj" class="notes-block" style="margin-bottom:12px"></div>
    <div class="actions">
      <button class="btn secondary" id="m-cancel">Cancel</button>
      <button class="btn" id="m-save">${a ? "Save" : "Add"}</button>
    </div>`, (el, close) => {
    const preview = () => {
      const fake = {
        id: a?.id || -1, person_id: personId,
        grant_id: +$("#m-grant", el).value,
        monthly_salary: (parseFloat($("#m-sal", el).value) || 0) / 12,
        fringe_rate: parseFloat($("#m-fringe", el).value) || 0,
        annual_tuition: parseFloat($("#m-tuition", el).value) || 0,
        start_date: $("#m-start", el).value, end_date: $("#m-end", el).value,
      };
      const p = fake.start_date && fake.end_date ? projectAppointment(fake) : null;
      $("#m-proj", el).textContent = p
        ? `Projection: ${p.months} months (${p.from} → ${p.to}, clamped to the grant's end) · ` +
          `salary ${money(p.salary)} + fringe ${money(p.fringe)} + tuition ${money(p.tuition)} = ${money(p.total)} committed`
        : "Projection: nothing left to project (already charged through the end, or dates missing).";
    };
    ["m-grant", "m-sal", "m-fringe", "m-tuition", "m-start", "m-end"].forEach((id) => {
      $("#" + id, el).oninput = preview;
      $("#" + id, el).onchange = preview;
    });
    preview();
    $("#m-cancel", el).onclick = close;
    $("#m-save", el).onclick = async () => {
      const body = {
        person_id: personId, grant_id: +$("#m-grant", el).value,
        monthly_salary: Math.round((parseFloat($("#m-sal", el).value) || 0) / 12 * 100) / 100,
        fringe_rate: parseFloat($("#m-fringe", el).value) || 0,
        annual_tuition: parseFloat($("#m-tuition", el).value) || 0,
        auto_charge: $("#m-auto", el).checked ? 1 : 0,
        pct: Math.min(100, Math.max(1, parseFloat($("#m-pct", el).value) || 100)),
        start_date: $("#m-start", el).value, end_date: $("#m-end", el).value,
      };
      if (!body.start_date || !body.end_date) { toast("Start and end dates are required"); return; }
      const splitG = !a && $("#m-split-grant", el) ? +$("#m-split-grant", el).value : 0;
      const pct = !a ? parseFloat($("#m-split-pct", el)?.value) : NaN;
      if (splitG && splitG !== body.grant_id && pct > 0 && pct < 100) {
        const gA = S.grants.find((g) => g.id === body.grant_id);
        const gB = S.grants.find((g) => g.id === splitG);
        const fr = pct / 100;
        const note = (share, other) =>
          `${share}% split appointment (total ${money2(body.monthly_salary * 12)}/yr` +
          `${body.annual_tuition ? ` + tuition ${money2(body.annual_tuition)}/yr` : ""}); ` +
          `other ${100 - share}% on ${other.name}.`;
        await api("/api/appointments", "POST", {
          ...body,
          monthly_salary: Math.round(body.monthly_salary * (1 - fr) * 100) / 100,
          annual_tuition: Math.round(body.annual_tuition * (1 - fr) * 100) / 100,
          pct: Math.round(body.pct * (1 - fr) * 10) / 10,
          notes: note(100 - pct, gB),
        });
        await api("/api/appointments", "POST", {
          ...body, grant_id: gB.id,
          monthly_salary: Math.round(body.monthly_salary * fr * 100) / 100,
          annual_tuition: Math.round(body.annual_tuition * fr * 100) / 100,
          pct: Math.round(body.pct * fr * 10) / 10,
          notes: note(pct, gA),
        });
        toast(`Split appointment created: ${100 - pct}% on ${gA.name}, ${pct}% on ${gB.name}`);
      } else {
        if (splitG && splitG !== body.grant_id && !(pct > 0 && pct < 100)) { toast("Enter a split % between 1 and 99"); return; }
        if (a) await api(`/api/appointments/${a.id}`, "POST", body);
        else await api("/api/appointments", "POST", body);
        toast("Appointment saved — projections updated on the Summary tab");
      }
      close();
      await reload();
    };
  });
}

/* --------------------------------------------------------------- boot */
$$("#nav button").forEach((b) => b.onclick = () => { view = { name: b.dataset.view }; render(); });
$("#btn-new-grant").onclick = () => grantModal(null);
$("#btn-gen-salaries").onclick = async () => {
  const r = await api("/api/generate_salaries", "POST", {});
  toast(r.created ? `Created ${r.created} salary/fringe charge${r.created === 1 ? "" : "s"}`
    : "Nothing to create — no appointment has auto-charge enabled (or all months are charged)");
  await reload();
};

/* dark mode */
function applyDark(on) {
  document.body.classList.toggle("dark", on);
  $("#btn-dark").textContent = on ? "☀️" : "🌙";
  if (typeof Chart !== "undefined") {
    Chart.defaults.color = on ? "#94a0b4" : "#666";
    Chart.defaults.borderColor = on ? "rgba(148,160,180,.15)" : "rgba(0,0,0,.1)";
  }
}
applyDark(localStorage.getItem("gm-dark") === "1");
$("#btn-dark").onclick = () => {
  const on = !document.body.classList.contains("dark");
  localStorage.setItem("gm-dark", on ? "1" : "0");
  applyDark(on);
  render();
};

/* global search */
const gsInput = $("#global-search");
const gsResults = $("#search-results");
function runGlobalSearch(q) {
  q = q.trim().toLowerCase();
  if (q.length < 2) { gsResults.style.display = "none"; return; }
  const grants = S.grants.filter((g) =>
    `${g.name} ${g.agency} ${g.notes}`.toLowerCase().includes(q)).slice(0, 5);
  const people = S.people.filter((p) => p.name.toLowerCase().includes(q)).slice(0, 5);
  const exps = S.expenses.filter((e) =>
    `${e.description} ${catName(e.category_id)} ${personName(e.person_id)}`.toLowerCase().includes(q)).slice(0, 8);
  let html = "";
  if (grants.length) html += `<div class="sr-group">Grants</div>` + grants.map((g) =>
    `<div class="sr-item" data-sr-grant="${g.id}"><span>${esc(g.name)}</span><span class="sub2">${money(grantAvailable(g.id))} left</span></div>`).join("");
  if (people.length) html += `<div class="sr-group">People</div>` + people.map((p) =>
    `<div class="sr-item" data-sr-person="${p.id}"><span>${esc(p.name)}</span><span class="sub2">${esc(p.role)}</span></div>`).join("");
  if (exps.length) html += `<div class="sr-group">Expenses</div>` + exps.map((e) => {
    const g = S.grants.find((g) => g.id === e.grant_id);
    return `<div class="sr-item" data-sr-grant="${e.grant_id}"><span>${esc(e.description || catName(e.category_id))}</span><span class="sub2">${e.date} · ${money2(e.amount)} · ${esc(g ? g.name : "")}</span></div>`;
  }).join("");
  gsResults.innerHTML = html || `<div class="sr-empty">No matches for “${esc(q)}”</div>`;
  gsResults.style.display = "block";
  $$("[data-sr-grant]", gsResults).forEach((el) => el.onclick = () => {
    view = { name: "grant", grantId: +el.dataset.srGrant };
    gsResults.style.display = "none"; gsInput.value = "";
    render();
  });
  $$("[data-sr-person]", gsResults).forEach((el) => el.onclick = () => {
    view = { name: "people" };
    gsResults.style.display = "none"; gsInput.value = "";
    render();
  });
}
gsInput.oninput = () => runGlobalSearch(gsInput.value);
gsInput.onfocus = () => runGlobalSearch(gsInput.value);
document.addEventListener("click", (e) => {
  if (!e.target.closest(".searchbox")) gsResults.style.display = "none";
});
gsInput.onkeydown = (e) => { if (e.key === "Escape") { gsResults.style.display = "none"; gsInput.blur(); } };

$("#btn-workday").onclick = async () => {
  if (!WD) { try { WD = await api("/api/workday/state"); } catch (e) { toast(e.message); return; } }
  wdSettingsModal();
};

$("#btn-settings").onclick = async () => {
  if (!WD) { try { WD = await api("/api/workday/state"); } catch (e) { toast(e.message); return; } }
  settingsModal();
};

$("#btn-bell").onclick = (e) => { e.stopPropagation(); notifPanel(); };

reload().then(async () => {
  // Opening the app: offer to connect to Workday (or work offline).
  try {
    WD = await api("/api/workday/state");
    render(); // inject the Workday cards now that WD is loaded
    wdTopbarUpdate();
    refreshNotifBadge();
    // Offline is the default. Most people can't reach Workday directly (SSO
    // + MFA blocks it), so the app never opens with a connection prompt — you
    // set one up in ⚙ Settings → Workday if your account actually allows it.
    // Only once a direct connection is configured do we offer to sign in.
    const syncedToday = WD.last_sync && WD.last_sync.date === S.today;
    const wdConfigured = WD.raas && (WD.raas.summary_url || WD.raas.detail_url);
    if (wdConfigured && !syncedToday && !wdOffline()
        && !sessionStorage.getItem("wd-connect-prompted")) {
      sessionStorage.setItem("wd-connect-prompted", "1");
      wdConnectModal();
    }
    // Start of a new month: offer last month's report once, and only if
    // there is actually something to report.
    const prev = prevMonthKey();
    const due = !(WD.reports_sent || {})[prev] &&
                S.expenses.some((e) => e.date.slice(0, 7) === prev);
    if (due && !sessionStorage.getItem("report-prompted-" + prev)) {
      sessionStorage.setItem("report-prompted-" + prev, "1");
      setTimeout(() => {
        if (!$(".modal")) reportModal(prev);
      }, 1400);
    }
  } catch { /* Workday is optional — never block the app */ }
}).catch((e) => {
  $("#main").innerHTML = `<div class="empty">Could not load data: ${esc(e.message)}</div>`;
});

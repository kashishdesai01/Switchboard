"use strict";

const element = (id) => document.getElementById(id);
const label = (value) => value.replaceAll("_", " ");
const needsReview = new Set(["emergency", "handoff", "needs_review"]);
let token;
let selection;
let snapshot;
let timer;
let lastWork;
const resolutions = ["Patient contacted", "Referred to scheduling", "Referral verified", "Write reconciled", "Closed without booking"];
const contextLabels = {
  patient_reference: "Patient record", request: "Request", topic: "Topic",
  service: "Service", visit: "Visit type", provider: "Provider", location: "Location",
  day: "Requested date", days: "Requested dates", week: "Week of", period: "Preferred period",
  requested_time: "Requested time", after: "Earliest start", before: "Latest start",
  start: "Opening", slot: "Slot", operation: "Operation", delivery: "Delivery status",
};

function content(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function itemDetails(context) {
  const list = document.createElement("dl");
  for (const [key, value] of Object.entries(context || {})) {
    if (!value || !contextLabels[key]) continue;
    const row = document.createElement("div");
    row.append(content("dt", contextLabels[key]), content("dd", String(value)));
    list.append(row);
  }
  return list;
}

function taskForm(item) {
  const claim = item.status === "open";
  const form = document.createElement("form");
  const field = document.createElement(claim ? "input" : "select");
  field.setAttribute("aria-label", claim ? "Staff name" : "Resolution");
  field.required = true;
  if (claim) {
    field.placeholder = "Staff name";
    field.maxLength = 60;
  } else {
    field.append(new Option("Select resolution", ""), ...resolutions.map((value) => new Option(value)));
  }
  const button = content("button", claim ? "Claim request" : "Resolve request");
  form.append(field, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      const response = await fetch(`/api/operations/work/${encodeURIComponent(item.id)}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ action: claim ? "claim" : "resolve", revision: item.revision, value: field.value }),
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Check the staff name or resolution.");
      }
      element("work-status").textContent = claim ? "Request claimed." : "Request resolved.";
      field.blur();
      clearTimeout(timer);
      await refresh();
    } catch (error) {
      element("work-status").textContent = error.message || "The work item could not be updated.";
    } finally {
      button.disabled = false;
    }
  });
  return form;
}

function renderWork(data) {
  const work = element("work-items");
  const encoded = JSON.stringify(data.tasks || []);
  if (lastWork !== encoded && !work.contains(document.activeElement)) {
    lastWork = encoded;
    const tasks = (data.tasks || []).map((item) => {
      const card = document.createElement("article");
      card.className = "work-item";
      card.append(content("h3", label(item.reason)), content("p", `${label(item.status)}${item.assignee ? ` · ${item.assignee}` : ""}`));
      card.append(content("p", item.next_action), itemDetails(item.context));
      if (item.context.patient_reference || item.context.slot || item.context.operation) {
        const lookup = content("button", "Check current scheduling record");
        const results = content("div", "");
        results.setAttribute("role", "status");
        lookup.addEventListener("click", async () => {
          lookup.disabled = true;
          try {
            const response = await fetch(`/api/operations/work/${encodeURIComponent(item.id)}/context`, {
              headers: {Authorization: `Bearer ${token}`},
            });
            if (!response.ok) throw new Error();
            const context = await response.json();
            results.replaceChildren(...Object.entries(context).map(([name, value]) =>
              content("p", `${name} · ${Array.isArray(value) ? value.join(" · ") : value}`)
            ));
          } catch {
            results.textContent = "The scheduling record is temporarily unavailable.";
          } finally {
            lookup.disabled = false;
          }
        });
        card.append(lookup, results);
      }
      if (item.trace_id) {
        const seeCall = content("button", "View call timeline");
        seeCall.addEventListener("click", () => {
          openStaffCall(item.trace_id);
        });
        card.append(seeCall);
      }
      if (item.status === "resolved") card.append(content("p", item.resolution));
      else card.append(taskForm(item));
      return card;
    });
    work.replaceChildren(...(tasks.length ? tasks : [content("p", "No work items yet.")]));
  }
  const notices = (data.notices || []).map((notice) => {
    const item = document.createElement("details");
    item.append(content("summary", `${notice.subject} · ${label(notice.status)}`), content("pre", notice.body));
    return item;
  });
  const noticesPanel = element("notice-items");
  const noticeKey = JSON.stringify(data.notices || []);
  if (noticesPanel.dataset.snapshot !== noticeKey) {
    noticesPanel.dataset.snapshot = noticeKey;
    noticesPanel.replaceChildren(...(notices.length ? notices : [content("p", "No emails requested yet.")]));
  }
  const waitlist = (data.waitlist || []).map((entry) => {
    const item = document.createElement("details");
    item.append(content("summary", `${label(entry.status)} · ${entry.criteria.visit}`), itemDetails(entry.criteria));
    return item;
  });
  const waitlistPanel = element("waitlist-items");
  const waitlistKey = JSON.stringify(data.waitlist || []);
  if (waitlistPanel.dataset.snapshot !== waitlistKey) {
    waitlistPanel.dataset.snapshot = waitlistKey;
    waitlistPanel.replaceChildren(...(waitlist.length ? waitlist : [content("p", "No waitlist requests yet.")]));
  }
}

let externalTrace;
function showTimeline() {
  const trace = snapshot.find((call) => call.id === selection) || (externalTrace?.id === selection ? externalTrace : null);
  element("timeline-id").textContent = trace ? `Call ${trace.id.slice(0, 8)}` : "Select a call";
  element("mark-reviewed").hidden = !(trace && needsReview.has(trace.outcome) && !trace.reviewed_at);
  const rows = (trace?.events || []).map((entry) => {
    const row = document.createElement("li");
    const details = [`${entry.elapsed_ms} ms`, label(entry.event), entry.state];
    if (entry.latency_ms !== null) details.push(`${entry.latency_ms} ms processing`);
    row.textContent = details.filter(Boolean).join(" · ");
    return row;
  });
  element("timeline").replaceChildren(...rows);
  updateCallReview(trace);
}

function render(data) {
  const duration = (value) => value === null ? "—" : `${value} ms`;
  element("active-count").textContent = data.live.length;
  element("review-count").textContent = (data.tasks || []).filter((item) => item.status !== "resolved").length;
  element("turn-p50").textContent = duration(data.turns.p50);
  element("turn-p95").textContent = duration(data.turns.p95);
  const unresolved = data.unresolved_mutations;
  const writes = [...unresolved, ...(data.settled_mutations || [])];
  element("write-summary").textContent = unresolved.length
    ? "Confirm unresolved outcomes before retrying."
    : "Recent confirmed outcomes";
  const writeRows = writes.map((item) => {
    const row = document.createElement("li");
    const details = [label(item.action), label(item.status), `operation ${item.operation}`];
    if (Number.isFinite(item.checked)) {
      details.push(`Last receipt lookup attempt ${new Date(item.checked * 1000).toLocaleString()}`);
    } else if (item.status === "pending" || item.status === "unknown") {
      details.push("Awaiting receipt lookup");
    }
    row.textContent = details.join(" · ");
    return row;
  });
  element("unresolved").replaceChildren(...writeRows);
  element("unresolved-panel").hidden = writeRows.length === 0;
  snapshot = [...data.live, ...data.recent];
  renderWork(data);
  if ((!externalTrace || externalTrace.id !== selection) && !snapshot.some((call) => call.id === selection)) selection = snapshot[0]?.id;
  const rows = snapshot.map((call) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "ops-row";
    row.setAttribute("aria-pressed", call.id === selection);
    const started = new Date(call.started * 1000).toLocaleTimeString([], {
      hour: "numeric", minute: "2-digit",
    });
    for (const value of [started, label(call.transport), label(call.outcome || "active")]) {
      const cell = document.createElement("span");
      cell.textContent = value;
      row.append(cell);
    }
    row.addEventListener("click", () => {
      selection = call.id;
      element("ops-calls").querySelectorAll("button").forEach((button) => {
        button.setAttribute("aria-pressed", button === row);
      });
      showTimeline();
    });
    return row;
  });
  element("ops-calls").replaceChildren(...rows);
  showTimeline();
}

async function refresh() {
  try {
    const response = await fetch("/api/operations", { headers: { Authorization: `Bearer ${token}` } });
    if (response.status === 401) {
      token = undefined;
      element("ops-status").textContent = "The token was rejected.";
      element("unlock-panel").hidden = false;
      element("dashboard").hidden = true;
      clearStaffViews();
      return;
    }
    if (!response.ok) throw new Error("Operations unavailable");
    render(await response.json());
    element("unlock-panel").hidden = true;
    element("dashboard").hidden = false;
    if (element("appointment-items").dataset.loaded !== "yes") loadStaffCalendar();
    element("ops-status").textContent = snapshot.length ? "" : "Start a call in the call lab to see activity.";
  } catch {
    element("ops-status").textContent = "Unable to load call activity.";
  } finally {
    if (token) timer = setTimeout(refresh, 3000);
  }
}

element("unlock").addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(timer);
  token = element("ops-token").value;
  clearStaffViews();
  element("ops-token").value = "";
  refresh();
});

fetch("/capabilities").then((response) => response.json()).then(({ clinic }) => {
  element("clinic-name").textContent = clinic;
}).catch(() => {});

element("mark-reviewed").addEventListener("click", async () => {
  const active = selection;
  if (!active || !token) return;
  element("mark-reviewed").disabled = true;
  try {
    const response = await fetch(`/api/operations/${encodeURIComponent(active)}/review`, {
      method: "POST", headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error("Review unavailable");
    if (externalTrace?.id === active) externalTrace.reviewed_at = Date.now() / 1000;
    clearTimeout(timer);
    await refresh();
  } catch {
    element("ops-status").textContent = "Unable to update the review.";
  } finally {
    element("mark-reviewed").disabled = false;
  }
});

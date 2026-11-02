"use strict";

let calendarData;
let calendarRequest = 0;
let actionRequest = 0;
let callRequest = 0;
let reviewId;
let pendingAction;
let targetSlot;

function staffFetch(path, options = {}) {
  return fetch(`/api/operations/${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...options.headers },
  }).then(async (response) => {
    if (response.status === 204) return null;
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be completed.");
    return data;
  });
}

function clearStaffViews() {
  calendarRequest++;
  callRequest++;
  calendarData = undefined;
  externalTrace = undefined;
  selection = undefined;
  updateCallReview(null);
  element("appointment-items").replaceChildren();
  delete element("appointment-items").dataset.loaded;
}

function appointmentTime(value, full = false) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: calendarData.timezone,
    ...(full ? { weekday: "short", month: "short", day: "numeric" } : {}),
    hour: "numeric", minute: "2-digit",
  }).format(new Date(value));
}

async function loadStaffCalendar() {
  const request = ++calendarRequest;
  const list = element("appointment-items");
  const day = element("calendar-day");
  const provider = element("calendar-provider");
  const filters = new URLSearchParams();
  if (day.value) filters.set("day", day.value);
  if (provider.value) filters.set("provider", provider.value);
  list.dataset.loaded = "yes";
  element("appointment-status").textContent = "Loading appointments…";
  list.replaceChildren();
  try {
    const data = await staffFetch(`appointments?${filters}`);
    if (request !== calendarRequest) return;
    calendarData = data;
    day.value = data.day;
    const doctor = provider.value;
    provider.replaceChildren(new Option("All doctors", ""),
      ...Object.entries(data.providers).map(([id, name]) => new Option(name, id)));
    provider.value = doctor;
    element("clinic-zone").textContent = `Clinic time · ${data.timezone}`;
    renderStaffCalendar();
  } catch (error) {
    if (request === calendarRequest) {
      calendarData = undefined;
      element("appointment-status").textContent = error.message;
    }
  }
}

function renderStaffCalendar() {
  if (!calendarData) return;
  const status = element("calendar-status").value;
  const items = calendarData.appointments.filter((item) => !status || status === item.status);
  element("appointment-status").textContent = items.length
    ? `${items.length} appointment${items.length === 1 ? "" : "s"}`
    : "No appointments match this date and filters.";
  element("appointment-items").replaceChildren(...items.map((item) => {
    const row = document.createElement("article");
    row.className = "staff-appointment";
    const info = document.createElement("div");
    info.append(content("h3", item.provider),
      content("p", [item.patient, item.visit, item.location].filter(Boolean).join(" · ")),
      content("p", [label(item.status), item.confirmation && `Email ${label(item.confirmation)}`].filter(Boolean).join(" · ")));
    const actions = document.createElement("div");
    actions.className = "staff-actions";
    if (item.call_id) actions.append(staffButton("View activity", () => openStaffCall(item.call_id)));
    if (item.can_change) actions.append(
      staffButton("Reschedule", () => openAppointmentAction(item, "reschedule")),
      staffButton("Cancel appointment", () => openAppointmentAction(item, "cancel")));
    row.append(content("strong", `${appointmentTime(item.start)} – ${appointmentTime(item.end)}`), info, actions);
    return row;
  }));
}

function staffButton(text, action) {
  const button = content("button", text);
  button.type = "button";
  button.addEventListener("click", action);
  return button;
}

for (const [id, amount] of [["previous-day", -1], ["next-day", 1]]) {
  element(id).addEventListener("click", () => {
    const day = element("calendar-day");
    if (!day.value) return;
    const next = new Date(day.value);
    next.setUTCDate(next.getUTCDate() + amount);
    day.value = next.toISOString().slice(0, 10);
    loadStaffCalendar();
  });
}
for (const id of ["calendar-day", "calendar-provider"]) element(id).addEventListener("change", loadStaffCalendar);
element("calendar-status").addEventListener("change", renderStaffCalendar);
element("calendar-refresh").addEventListener("click", loadStaffCalendar);

function openAppointmentAction(item, action) {
  pendingAction = {item, action};
  targetSlot = undefined;
  element("appointment-action-title").textContent = action === "cancel" ? "Cancel appointment" : "Reschedule appointment";
  element("appointment-action-details").textContent = `${item.patient} · ${item.provider} · ${appointmentTime(item.start, true)}`;
  element("appointment-action-status").textContent = "";
  element("move-date-label").hidden = action === "cancel";
  element("move-openings").replaceChildren();
  const confirm = element("confirm-action");
  confirm.textContent = action === "cancel" ? "Confirm cancellation" : "Confirm new time";
  confirm.disabled = action === "reschedule";
  element("appointment-dialog").showModal();
  if (action === "reschedule") {
    element("move-date").value = calendarData.day;
    loadStaffOpenings();
  }
}

async function loadStaffOpenings() {
  const request = ++actionRequest;
  const item = pendingAction.item;
  targetSlot = undefined;
  element("confirm-action").disabled = true;
  element("move-openings").replaceChildren();
  const status = element("appointment-action-status");
  status.textContent = "Looking for openings with the same doctor…";
  try {
    const day = element("move-date").value;
    if (!day) throw new Error("Choose a date.");
    const slots = await staffFetch(`appointments/${encodeURIComponent(item.id)}/openings?day=${day}`);
    if (request !== actionRequest || !element("appointment-dialog").open) return;
    status.textContent = slots.length ? "" : "No openings with this doctor on that day.";
    element("move-openings").replaceChildren(...slots.map((slot) => {
      const option = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "staff-opening";
      radio.addEventListener("change", () => {
        targetSlot = slot.id;
        element("confirm-action").disabled = false;
      });
      option.append(radio, content("span", appointmentTime(slot.start, true)));
      return option;
    }));
  } catch (error) {
    if (request === actionRequest) status.textContent = error.message;
  }
}

element("move-date").addEventListener("change", loadStaffOpenings);
element("dismiss-action").addEventListener("click", () => element("appointment-dialog").close());
element("appointment-dialog").addEventListener("cancel", (event) => {
  if (element("dismiss-action").disabled) event.preventDefault();
});
element("confirm-action").addEventListener("click", async () => {
  const {item, action} = pendingAction;
  const confirm = element("confirm-action");
  confirm.disabled = true;
  element("dismiss-action").disabled = true;
  element("move-date").disabled = true;
  element("move-openings").querySelectorAll("input").forEach((input) => input.disabled = true);
  const status = element("appointment-action-status");
  status.textContent = "Saving…";
  try {
    await staffFetch(`appointments/${encodeURIComponent(item.id)}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, revision: item.revision, slot: targetSlot}),
    });
    element("appointment-dialog").close();
    if (action === "reschedule") element("calendar-day").value = element("move-date").value;
    await loadStaffCalendar();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    element("dismiss-action").disabled = false;
    element("move-date").disabled = false;
  }
});

function updateCallReview(trace) {
  const id = trace?.id;
  element("call-review").hidden = !id;
  if (reviewId === id) return;
  reviewId = id;
  callRequest++;
  element("view-transcript").setAttribute("aria-expanded", "false");
  element("view-transcript").textContent = "View conversation";
  element("call-conversation").hidden = true;
  element("call-conversation").replaceChildren();
}

async function openStaffCall(id) {
  selection = id;
  element("timeline-title").scrollIntoView({block: "center"});
  const local = snapshot.find((call) => call.id === id);
  if (!local) {
    try {
      const detail = await staffFetch(`calls/${encodeURIComponent(id)}`);
      if (selection === id) externalTrace = detail.call;
    } catch (error) {
      if (selection === id) element("ops-status").textContent = error.message;
    }
  }
  showTimeline();
}

element("view-transcript").addEventListener("click", async () => {
  const button = element("view-transcript");
  const panel = element("call-conversation");
  const request = ++callRequest;
  const open = panel.hidden;
  panel.hidden = !open;
  button.setAttribute("aria-expanded", open);
  button.textContent = open ? "Hide conversation" : "View conversation";
  if (!open) return;
  panel.replaceChildren(content("p", "Loading conversation…"));
  try {
    const {transcript, call} = await staffFetch(`calls/${encodeURIComponent(reviewId)}`);
    if (request !== callRequest) return;
    if (!transcript.length) {
      panel.replaceChildren(content("p", call.outcome
        ? "No transcript was saved for this call. Older calls still have an activity timeline."
        : "The conversation will be available after the call ends if transcript capture is enabled."));
      return;
    }
    const questions = transcript.filter((turn) =>
      turn.text.includes("?") || (turn.speaker === "caller" &&
        /^(?:what|when|where|which|who|how|why|can|could|do|does|is|are|would)\b/i.test(turn.text))
    );
    const summary = document.createElement("details");
    summary.append(content("summary", `Questions asked (${questions.length})`),
      ...questions.map((turn) => content("p", `${label(turn.speaker)} · ${turn.text}`)));
    const list = document.createElement("ol");
    list.className = "staff-turns";
    for (const turn of transcript) {
      const row = document.createElement("li");
      row.append(content("strong", turn.speaker === "caller" ? "Caller" : "Switchboard"), content("p", turn.text));
      list.append(row);
    }
    panel.replaceChildren(summary, list);
  } catch (error) {
    if (request === callRequest) panel.replaceChildren(content("p", error.message));
  }
});

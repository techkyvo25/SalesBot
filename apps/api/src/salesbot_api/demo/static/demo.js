// Demo page (docs/DEMO.md). Plain JS, no build step. Polls the demo API every 2 seconds.
"use strict";

const session = Math.random().toString(36).slice(2) + Date.now().toString(36);
const LANGS = { en: "English", hi: "Hindi", mr: "Marathi" };
const FUNNEL = ["registered", "booked", "confirmed", "rescheduled", "cancelled"];
let dayOffset = 0;

const $ = (id) => document.getElementById(id);
const log = $("log");

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, body) {
  const res = await fetch("/demo/api/" + path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function scrollDown() { log.scrollTop = log.scrollHeight; }

function system(text) { log.appendChild(el("div", "msg system", text)); scrollDown(); }

function patientSays(text, language) {
  const box = el("div", "msg patient");
  if (language) {
    const tags = el("div", "tags");
    tags.appendChild(el("span", "tag", LANGS[language] || language));
    box.appendChild(tags);
  }
  box.appendChild(el("div", "", text));
  log.appendChild(box);
  scrollDown();
}

function clinicSays(m) {
  const box = el("div", "msg clinic");
  const tags = el("div", "tags");
  tags.appendChild(el("span", "tag", LANGS[m.language] || m.language));
  if (m.draft) tags.appendChild(el("span", "tag draft", "DRAFT"));
  box.appendChild(tags);
  if (m.live_text) {
    // Exactly what a live channel sends while this wording is DRAFT: numbers only.
    box.appendChild(el("div", "note", "A live channel sends only this:"));
    box.appendChild(el("div", "live", m.live_text));
    box.appendChild(el("div", "wording", "DRAFT wording (not sent live): " + m.text));
  } else {
    box.appendChild(el("div", "", m.text));
  }
  if (m.options.length) {
    const opts = el("div", "options");
    m.options.forEach((o, i) => {
      const b = el("button", "", (i + 1) + ". " + o.label);
      b.onclick = () => send("", o.id, o.label);
      opts.appendChild(b);
    });
    box.appendChild(opts);
  }
  if (m.outbound) box.appendChild(el("div", "note", "WhatsApp would send this (simulated)"));
  log.appendChild(box);
  scrollDown();
}

async function send(text, choice, label) {
  try {
    const data = await api("chat", { session, patient: $("patient").value, text, choice });
    patientSays(choice ? label : text, choice ? null : data.inbound_language);
    data.messages.forEach(clinicSays);
    if (!data.messages.length) system("(no reply)");
    if (data.opt_out) system("Opt-out saved before the reply: no more messages to this patient.");
    refresh();
  } catch (err) {
    system("Error: " + err.message);
  }
}

$("send").onsubmit = (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  $("text").value = "";
  send(text, null, null);
};

$("register").onclick = async () => {
  try {
    const data = await api("register", { session });
    $("patient").value = data.patient;
    system(data.created
      ? data.name + " registered (simulated form, through the real intake code)."
      : data.name + " is already registered: no second welcome.");
    data.messages.forEach(clinicSays);
    refresh();
  } catch (err) {
    system("Error: " + err.message);
  }
};

$("take").onclick = async () => {
  try {
    const data = await api("other-patient-takes-slot", { session, patient: $("patient").value });
    system(data.booked_by.length
      ? data.booked_by.join(" and ") + " just booked " + data.time + ". Now try to pick it."
      : "That time was already gone.");
    refresh();
  } catch (err) {
    system("Error: " + err.message);
  }
};

$("reset").onclick = async () => {
  if (!window.confirm("Reset the demo clinic? This deletes only the demo clinic's data.")) return;
  try {
    await api("reset", {});
    log.textContent = "";
    system("Demo reset: fresh fake clinic, Ravi and Meera seeded. Register Asha to start.");
    refresh();
  } catch (err) {
    system("Error: " + err.message);
  }
};

$("patient").onchange = () => system("Now chatting as " + $("patient").selectedOptions[0].text + ".");
$("prev").onclick = () => { dayOffset -= 1; refresh(); };
$("next").onclick = () => { dayOffset += 1; refresh(); };

function istDay(offset) {
  const now = new Date(Date.now() + 330 * 60000 + offset * 86400000);
  return now.toISOString().slice(0, 10);
}

function renderDay(data) {
  $("daylabel").textContent = data.label + (dayOffset === 0 ? " (today)" : "");
  const days = $("days");
  days.textContent = "";
  data.doctors.forEach((d) => {
    const box = el("div");
    box.appendChild(el("h3", "", d.name));
    const table = el("table");
    const head = el("tr");
    ["Time", "Patient", "Status", "Lang"].forEach((h) => head.appendChild(el("th", "", h)));
    table.appendChild(head);
    if (!d.rows.length) {
      const tr = el("tr");
      const td = el("td", "muted", "No appointments");
      td.colSpan = 4;
      tr.appendChild(td);
      table.appendChild(tr);
    }
    d.rows.forEach((r) => {
      const tr = el("tr", "st-" + r.status);
      [r.time, r.patient, r.status.replace("_", " "), r.language].forEach((v) =>
        tr.appendChild(el("td", "", v)));
      table.appendChild(tr);
    });
    box.appendChild(table);
    days.appendChild(box);
  });
}

function renderFunnel(data) {
  const box = $("funnel");
  box.textContent = "";
  FUNNEL.forEach((k) => {
    const tile = el("div", "tile");
    tile.appendChild(el("b", "", String(data[k])));
    tile.appendChild(el("span", "", k));
    box.appendChild(tile);
  });
}

function renderTasks(rows) {
  const box = $("tasks");
  box.textContent = "";
  if (!rows.length) box.appendChild(el("div", "muted", "No tasks."));
  rows.forEach((t) => {
    const item = el("div", "task" + (t.urgent ? " urgent" : ""));
    item.appendChild(el("span", "kind", (t.urgent ? "URGENT " : "") + t.kind));
    item.appendChild(el("span", "muted", "  " + t.at + (t.patient ? " · " + t.patient : "")));
    item.appendChild(el("div", "", t.summary));
    box.appendChild(item);
  });
}

async function refresh() {
  try {
    const [day, funnel, tasks] = await Promise.all([
      api("day?day=" + istDay(dayOffset)), api("funnel"), api("tasks"),
    ]);
    renderDay(day);
    renderFunnel(funnel);
    renderTasks(tasks);
  } catch (err) {
    $("daylabel").textContent = "Not available: " + err.message;
  }
}

system("Press Register patient (simulated) to start as Asha, or chat as Ravi or Meera.");
refresh();
setInterval(refresh, 2000);

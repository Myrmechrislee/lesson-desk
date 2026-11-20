"use strict";

// Native time inputs follow the browser locale rather than the account's clock
// preference. Keep their canonical values and validation, with explicit controls.
for (const field of document.querySelectorAll('input[type="time"], input[type="datetime-local"]')) {
  const withDate = field.type === "datetime-local";
  const twelveHour = document.body.dataset.timeFormat !== "24";
  const label = field.closest("label")?.firstChild.textContent.trim() || field.name;
  const picker = document.createElement("span");
  picker.className = "time-picker";
  picker.dataset.timeInput = field.name;
  const controls = [];
  const select = (part, values, placeholder) => {
    const control = document.createElement("select");
    control.dataset.timePart = part;
    control.setAttribute("aria-label", `${label} ${part}`);
    control.add(new Option(placeholder, ""));
    for (const [value, text] of values) control.add(new Option(text, String(value)));
    picker.append(control);
    controls.push(control);
    return control;
  };
  let date;
  if (withDate) {
    date = document.createElement("input");
    date.type = "date";
    date.dataset.timePart = "date";
    date.setAttribute("aria-label", `${label} date`);
    picker.append(date);
    controls.push(date);
  }
  const pad = (value) => String(value).padStart(2, "0");
  const hour = select("hour", Array.from({ length: twelveHour ? 12 : 24 }, (_, index) => {
    const value = index + (twelveHour ? 1 : 0);
    return [value, twelveHour ? String(value) : pad(value)];
  }), "Hour");
  const minute = select("minute", Array.from({ length: 60 }, (_, value) => [value, pad(value)]), "Min");
  const period = twelveHour ? select("AM or PM", [["AM", "AM"], ["PM", "PM"]], "AM/PM") : null;
  const error = document.createElement("small");
  error.className = "time-picker-error";
  error.hidden = true;
  error.setAttribute("role", "status");
  const render = () => {
    const clock = withDate ? field.value.slice(11) : field.value;
    const hours = clock ? Number(clock.slice(0, 2)) : null;
    if (date) date.value = field.value.slice(0, 10);
    hour.value = hours === null ? "" : String(twelveHour ? hours % 12 || 12 : hours);
    minute.value = clock ? String(Number(clock.slice(3, 5))) : "";
    if (period) period.value = hours === null ? "" : hours < 12 ? "AM" : "PM";
    for (const control of controls) {
      control.disabled = field.disabled;
      control.required = field.required;
    }
    if (field.validity.valid) error.hidden = true;
  };
  const update = () => {
    let clock = "";
    if (hour.value !== "" && minute.value !== "" && (!period || period.value)) {
      const hours = twelveHour ? Number(hour.value) % 12 + (period.value === "PM" ? 12 : 0) : Number(hour.value);
      clock = `${pad(hours)}:${pad(minute.value)}`;
    }
    field.value = withDate ? (date.value && clock ? `${date.value}T${clock}` : "") : clock;
    // Partial selections stay visible until the whole date/time is complete.
    field.dispatchEvent(new Event("input", { bubbles: true }));
    if (field.value) field.dispatchEvent(new Event("change", { bubbles: true }));
  };
  for (const control of controls) control.addEventListener("input", update);
  field.addEventListener("change", render);
  field.addEventListener("invalid", (event) => {
    event.preventDefault();
    error.textContent = field.validationMessage;
    error.hidden = false;
    (controls.find((control) => !control.value) || hour).focus();
  });
  field.form?.addEventListener("reset", () => queueMicrotask(render));
  new MutationObserver(render).observe(field, { attributes: true, attributeFilter: ["disabled", "required"] });
  field.hidden = true;
  field.after(picker, error);
  render();
}

const deviceTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const useDevice = document.querySelector("[data-use-device-timezone]");
if (useDevice) {
  useDevice.addEventListener("click", () => {
    const selection = document.querySelector("[data-timezone-select]");
    const match = Array.from(selection.options).find((option) => option.value === deviceTimezone);
    const status = document.querySelector("[data-device-timezone]");
    if (match) {
      selection.value = match.value;
      status.textContent = ` ${deviceTimezone}. Save preferences to apply.`;
    } else {
      status.textContent = " Choose your timezone from the list.";
    }
  });
}

if (deviceTimezone && document.body.dataset.detectTimezoneUrl) {
  // Initialize once, before a form can be edited in the previous timezone.
  const detection = new AbortController();
  const timeout = setTimeout(() => detection.abort(), 5000);
  document.body.inert = true;
  fetch(document.body.dataset.detectTimezoneUrl, {
    method: "POST",
    body: new URLSearchParams({ timezone: deviceTimezone }),
    headers: { "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content },
    signal: detection.signal,
  }).then(async (response) => {
    if (response.ok && (await response.json()).updated) window.location.reload();
  }).catch(() => {}).finally(() => {
    clearTimeout(timeout);
    document.body.inert = false;
  });
}

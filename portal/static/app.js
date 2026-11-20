"use strict";

function connect(element, url, eventName, update) {
  if (!("EventSource" in window)) return;
  const label = element.querySelector("[data-live-label]");
  const connection = new EventSource(url);

  connection.addEventListener(eventName, (event) => {
    update(JSON.parse(event.data));
    label.textContent = "Live";
    label.classList.add("is-live");
  });
  connection.addEventListener("error", () => {
    label.textContent = "Reconnecting";
    label.classList.remove("is-live");
  });
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) connection.close();
  });
}

const liveCounts = document.querySelector("[data-live-counts]");
if (liveCounts) {
  const counts = Array.from(liveCounts.querySelectorAll("[data-assignment-count]"));
  connect(liveCounts, liveCounts.dataset.liveCounts, "counts", (snapshot) => {
    for (const node of counts) {
      const count = snapshot[node.dataset.assignmentCount] || 0;
      node.textContent = count + (count === 1 ? " file" : " files");
    }
  });
}

const livePhotos = document.querySelector("[data-live-photos]");
if (livePhotos) {
  const grid = livePhotos.querySelector("[data-photo-grid]");
  const noPhotos = livePhotos.querySelector("[data-no-photos]");
  connect(livePhotos, livePhotos.dataset.livePhotos, "photos", (snapshot) => {
    noPhotos.hidden = snapshot.length > 0;
    const currentIds = new Set(snapshot.map((photo) => photo.id));
    for (const node of Array.from(grid.children)) {
      if (!currentIds.has(node.dataset.photoId)) node.remove();
    }
    const displayed = new Set(Array.from(grid.children, (node) => node.dataset.photoId));
    for (const [index, photo] of snapshot.entries()) {
      if (displayed.has(photo.id)) continue;
      const tile = document.createElement("div");
      tile.className = "homework-file";
      tile.dataset.photoId = photo.id;
      const link = document.createElement("a");
      link.className = "photo-tile";
      link.href = photo.url;
      link.target = "_blank";
      link.rel = "noopener";
      if (photo.is_image) {
        const image = document.createElement("img");
        image.src = photo.url;
        image.alt = photo.filename || "Homework photo " + (index + 1);
        image.loading = "lazy";
        link.append(image);
      } else {
        const preview = document.createElement("span");
        preview.className = "document-preview";
        preview.textContent = "Document ↓";
        link.append(preview);
      }
      const caption = document.createElement("span");
      caption.className = "file-caption";
      caption.textContent = photo.filename;
      link.append(caption);
      tile.append(link);
      grid.append(tile);
      displayed.add(photo.id);
    }
  });
}

const charge = document.querySelector("[data-requires-feedback]");
if (charge) {
  const label = document.querySelector("[data-charge-feedback]");
  const field = label.querySelector("textarea");
  const toggleFeedback = () => {
    label.hidden = !charge.checked;
    field.required = charge.checked;
  };
  charge.addEventListener("change", toggleFeedback);
  toggleFeedback();
}

const copyLink = document.querySelector("[data-copy-link]");
if (copyLink) {
  const field = document.querySelector("[data-setup-url]");
  copyLink.addEventListener("click", async () => {
    field.select();
    try {
      await navigator.clipboard.writeText(field.value);
      copyLink.textContent = "Link copied";
    } catch {
      copyLink.textContent = "Copy selected link";
    }
  });
}

const outcomeStatus = document.querySelector("[data-outcome-status]");
if (outcomeStatus) {
  const updateOutcomeFields = () => {
    for (const group of document.querySelectorAll("[data-outcome-field]")) {
      const visible = group.dataset.outcomeField === outcomeStatus.value;
      group.hidden = !visible;
      for (const field of group.querySelectorAll("input, textarea")) {
        field.disabled = !visible;
        field.required = visible && field.hasAttribute("data-required");
      }
    }
  };
  outcomeStatus.addEventListener("change", updateOutcomeFields);
  updateOutcomeFields();
}

const bookingForm = document.querySelector("[data-booking-form]");
if (bookingForm) {
  const tutor = bookingForm.querySelector("[data-booking-tutor]");
  const student = bookingForm.querySelector("[data-booking-student]");
  const topic = bookingForm.querySelector("[data-booking-topic]");
  const title = bookingForm.querySelector("[data-booking-title]");
  const repeat = bookingForm.querySelector("[data-booking-repeat]");
  const weeksGroup = bookingForm.querySelector("[data-booking-weeks]");
  const weeks = weeksGroup.querySelector("input");
  const start = bookingForm.querySelector("[data-booking-start]");
  const duration = bookingForm.querySelector("[data-booking-duration]");
  const day = bookingForm.querySelector("[data-booking-day]");
  const calendar = bookingForm.querySelector("[data-booking-calendar]");
  const slots = bookingForm.querySelector("[data-booking-slots]");
  const validation = bookingForm.querySelector("[data-booking-validation]");
  const submit = bookingForm.querySelector("[data-booking-submit]");
  let controller;
  let timer;
  let previousTitle = "";

  const selectTopic = () => {
    const selected = topic.selectedOptions[0];
    if (!title.value || title.value === previousTitle) title.value = selected?.dataset.topic || "";
    previousTitle = selected?.dataset.topic || "";
  };
  const filterTopics = () => {
    for (const option of topic.options) {
      if (!option.value) continue;
      option.hidden = option.dataset.studentId !== student.value || Boolean(tutor && option.dataset.tutorId !== tutor.value);
      option.disabled = option.hidden;
    }
    const choices = Array.from(topic.options).filter((option) => option.value && !option.disabled);
    if (!choices.some((option) => option.value === topic.value)) topic.value = "";
    if (choices.length === 1) topic.value = choices[0].value;
    selectTopic();
  };
  const showHours = (selector, hours) => {
    let label = hours.available.length ? hours.available.join(", ") : "No available hours set.";
    if (hours.unavailable.length) label += " Unavailable: " + hours.unavailable.join(", ") + ".";
    bookingForm.querySelector(selector).textContent = label;
  };
  const check = async () => {
    controller?.abort();
    controller = new AbortController();
    calendar.hidden = !student.value;
    submit.disabled = true;
    slots.replaceChildren(new Option("Choose an available time", ""));
    start.setCustomValidity("");
    if (!student.value || (tutor && !tutor.value) || !day.value || !duration.validity.valid) {
      validation.textContent = "Choose a tutor, student, date and duration to check the booking.";
      return;
    }
    const params = new URLSearchParams({ student_id: student.value, day: day.value,
      minutes: duration.value, repeat: repeat.value, weeks: weeks.value });
    if (tutor) params.set("tutor_id", tutor.value);
    if (start.value) params.set("starts_at", start.value);
    validation.textContent = "Checking student and tutor availability…";
    start.setCustomValidity("Wait for availability to be checked.");
    try {
      const response = await fetch(bookingForm.dataset.availabilityUrl + "?" + params, { signal: controller.signal });
      if (!response.ok) throw new Error("Availability check failed");
      const snapshot = await response.json();
      showHours("[data-tutor-hours]", snapshot.tutor);
      showHours("[data-parent-hours]", snapshot.student);
      for (const [value, label] of snapshot.slots) slots.add(new Option(label, value));
      if (!snapshot.slots.length) slots.options[0].textContent = "No mutual available times on this day";
      slots.value = snapshot.slots.some(([value]) => value === start.value) ? start.value : "";
      validation.textContent = snapshot.reason;
      validation.classList.toggle("booking-invalid", Boolean(start.value) && !snapshot.valid);
      start.setCustomValidity(start.value && !snapshot.valid ? snapshot.reason : "");
      submit.disabled = !snapshot.valid;
    } catch (error) {
      if (error.name === "AbortError") return;
      validation.textContent = "Availability could not be checked. Reload the page and try again.";
      start.setCustomValidity(validation.textContent);
    }
  };
  const scheduleCheck = () => {
    controller?.abort();
    clearTimeout(timer);
    submit.disabled = true;
    start.setCustomValidity("Wait for availability to be checked.");
    timer = setTimeout(check, 150);
  };
  const resetBooking = () => {
    start.value = "";
    start.dispatchEvent(new Event("change", { bubbles: true }));
    filterTopics();
    scheduleCheck();
  };
  student.addEventListener("change", resetBooking);
  tutor?.addEventListener("change", resetBooking);
  topic.addEventListener("change", selectTopic);
  repeat.addEventListener("change", () => {
    weeksGroup.hidden = repeat.value !== "count";
    weeks.disabled = weeksGroup.hidden;
    weeks.required = !weeksGroup.hidden;
    scheduleCheck();
  });
  for (const field of [duration, weeks]) field.addEventListener("input", scheduleCheck);
  start.addEventListener("input", () => {
    if (start.value) day.value = start.value.slice(0, 10);
    scheduleCheck();
  });
  day.addEventListener("change", () => {
    start.value = "";
    start.dispatchEvent(new Event("change", { bubbles: true }));
    scheduleCheck();
  });
  slots.addEventListener("change", () => {
    start.value = slots.value;
    start.dispatchEvent(new Event("change", { bubbles: true }));
    scheduleCheck();
  });
  filterTopics();
  check();
}

const announcementForm = document.querySelector("[data-announcement-form]");
if (announcementForm) {
  const important = announcementForm.querySelector("[data-announcement-important]");
  const popup = announcementForm.querySelector("[data-announcement-popup-option]");
  const banner = announcementForm.querySelector("[data-announcement-banner]");
  const dates = announcementForm.querySelector("[data-announcement-banner-dates]");
  const updateOptions = () => {
    popup.hidden = !important.checked;
    popup.querySelector("input").disabled = !important.checked;
    dates.hidden = !banner.checked;
    for (const input of dates.querySelectorAll("input")) {
      input.disabled = !banner.checked;
      input.required = banner.checked;
    }
  };
  important.addEventListener("change", updateOptions);
  banner.addEventListener("change", updateOptions);
  updateOptions();
}

const announcementPopups = Array.from(document.querySelectorAll("[data-announcement-popup]"));
if (announcementPopups.length) {
  let nextPopup = 0;
  const openNext = () => {
    const popup = announcementPopups[nextPopup++];
    if (popup && typeof popup.showModal === "function") popup.showModal();
  };
  for (const popup of announcementPopups) {
    popup.addEventListener("close", openNext);
    const form = popup.querySelector("[data-acknowledge-announcement]");
    popup.addEventListener("cancel", (event) => {
      event.preventDefault();
      form.requestSubmit();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      const errorLabel = popup.querySelector("[data-popup-error]");
      button.disabled = true;
      errorLabel.hidden = true;
      try {
        const response = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } });
        if (!response.ok || response.redirected) throw new Error("Acknowledgement failed");
        const badge = document.querySelector("[data-announcement-count]");
        if (badge) {
          const remaining = Math.max(0, Number(badge.textContent) - 1);
          badge.textContent = String(remaining);
          badge.setAttribute("aria-label", `${remaining} unread announcements`);
          badge.hidden = remaining === 0;
        }
        popup.close();
      } catch {
        errorLabel.textContent = "Your acknowledgement could not be saved. Reload the page and try again.";
        errorLabel.hidden = false;
        button.disabled = false;
      }
    });
  }
  openNext();
}

// Calendar cells and existing intervals share the same precise editor fields.
for (const editor of document.querySelectorAll("[data-calendar-editor]")) {
  const kind = editor.dataset.calendarEditor;
  const entrySelect = editor.querySelector("[data-calendar-entry]");
  const selection = editor.querySelector("[data-calendar-selection]");
  const remove = editor.querySelector("[data-calendar-remove]");
  const temporaryStart = kind === "exception" ? editor.elements.namedItem("starts_at") : null;
  const temporaryEnd = kind === "exception" ? editor.elements.namedItem("ends_at") : null;
  let temporaryDuration = 60 * 60 * 1000;
  // Arithmetic on wall-clock values keeps the default hour when a new date is
  // chosen, including intervals crossing midnight or a different browser zone.
  const wallTime = (value) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) ? Date.parse(value + ":00Z") : NaN;
  const rememberDuration = () => {
    if (!temporaryStart) return;
    const difference = wallTime(temporaryEnd.value) - wallTime(temporaryStart.value);
    if (difference > 0) temporaryDuration = difference;
  };
  if (temporaryStart) {
    temporaryStart.addEventListener("input", () => {
      const startTime = wallTime(temporaryStart.value);
      if (Number.isFinite(startTime)) {
        temporaryEnd.value = new Date(startTime + temporaryDuration).toISOString().slice(0, 16);
        temporaryEnd.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    temporaryEnd.addEventListener("input", rememberDuration);
    rememberDuration();
  }
  const updateRemove = () => {
    const option = entrySelect.selectedOptions[0];
    remove.hidden = !entrySelect.value;
    if (entrySelect.value) remove.formAction = option.dataset.removeUrl;
    else remove.removeAttribute("formaction");
  };
  const populate = (source) => {
    const fields = kind === "weekly"
      ? [["weekday", "weekday"], ["start", "start"], ["end", "end"], ["end_next_day", "endNextDay"], ["kind", "kind"]]
      : [["starts_at", "startsAt"], ["ends_at", "endsAt"], ["kind", "kind"]];
    for (const [name, key] of fields) {
      if (source.dataset[key] !== undefined) {
        const field = editor.elements.namedItem(name);
        if (field.type === "checkbox") field.checked = source.dataset[key] === "yes";
        else field.value = source.dataset[key];
        field.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    selection.textContent = entrySelect.value ? "Editing the selected interval." : "New interval selected. Adjust the times and save.";
    rememberDuration();
    updateRemove();
  };
  entrySelect.addEventListener("change", () => {
    if (entrySelect.value) populate(entrySelect.selectedOptions[0]);
    else {
      selection.textContent = "Adding a new interval. Choose a cell or enter your times.";
      updateRemove();
    }
  });
  for (const cell of document.querySelectorAll(`[data-calendar-select="${kind}"]`)) {
    cell.addEventListener("click", () => {
      entrySelect.value = cell.dataset.entryId || "";
      populate(entrySelect.value ? entrySelect.selectedOptions[0] : cell);
      for (const selected of document.querySelectorAll(`[data-calendar-select="${kind}"][aria-pressed="true"]`)) selected.setAttribute("aria-pressed", "false");
      cell.setAttribute("aria-pressed", "true");
      const field = editor.elements.namedItem(kind === "weekly" ? "start" : "starts_at");
      (field.hidden ? field.nextElementSibling.querySelector("input, select") : field).focus({ preventScroll: true });
    });
    cell.addEventListener("keydown", (event) => {
      const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      if (!(event.key in steps)) return;
      event.preventDefault();
      const cells = Array.from(document.querySelectorAll(`[data-calendar-select="${kind}"]`));
      const next = cells[cells.indexOf(cell) + steps[event.key]];
      if (next) next.focus({ preventScroll: true });
    });
  }
  for (const link of document.querySelectorAll(`[data-calendar-target="${kind}"]`)) {
    link.addEventListener("click", () => {
      entrySelect.value = link.dataset.calendarEdit;
      populate(entrySelect.selectedOptions[0]);
    });
  }
  updateRemove();
}

for (const selection of document.querySelectorAll("[data-availability-student], [data-availability-account]")) {
  selection.addEventListener("change", () => selection.form.requestSubmit());
}

for (const scroller of document.querySelectorAll("[data-calendar-scroll]")) {
  const firstChoice = scroller.querySelector(".calendar-slot");
  const firstEntry = scroller.querySelector(".calendar-entry");
  const target = firstChoice || firstEntry || scroller.querySelector('[data-calendar-hour="08:00"]');
  if (target) scroller.scrollTop = Math.max(0, target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - scroller.querySelector("thead").offsetHeight - 20);
}

const slotSelection = document.querySelector("[data-slot-selection]");
if (slotSelection) {
  for (const choice of document.querySelectorAll('.calendar-slot input[type="radio"]')) {
    choice.addEventListener("change", () => {
      slotSelection.textContent = `Requested time: ${choice.closest("label").textContent.trim()}.`;
    });
  }
}

const accountForm = document.querySelector("[data-account-form]");
if (accountForm) {
  const role = accountForm.elements.namedItem("role");
  const email = accountForm.elements.namedItem("email");
  const username = accountForm.elements.namedItem("username");
  const children = accountForm.querySelector("[data-parent-children]");
  const childList = children.querySelector("[data-child-list]");
  const addChild = children.querySelector("[data-add-child]");
  const updateChildren = () => {
    addChild.disabled = childList.children.length >= 20;
    for (const row of childList.children) {
      const fields = row.querySelectorAll("input");
      const started = Array.from(fields).some((field) => field.value.trim());
      for (const field of fields) field.required = started;
    }
  };
  childList.addEventListener("input", updateChildren);
  childList.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-child]");
    if (!remove) return;
    remove.closest("[data-child-row]").remove();
    updateChildren();
    addChild.focus();
  });
  addChild.addEventListener("click", () => {
    if (childList.children.length >= 20) return;
    childList.append(children.querySelector("[data-child-template]").content.cloneNode(true));
    updateChildren();
    childList.lastElementChild.querySelector("input").focus();
  });
  const updateIdentifiers = () => {
    const student = role.value === "student";
    email.required = Boolean(role.value) && !student;
    username.required = student;
    username.disabled = !student;
    username.closest("label").hidden = !student;
    children.hidden = role.value !== "parent";
    children.disabled = role.value !== "parent";
    accountForm.querySelector("[data-create-account]").textContent = role.value === "parent" ? "Create parent & children" : "Create account";
  };
  role.addEventListener("change", updateIdentifiers);
  updateIdentifiers();
  updateChildren();
}

// A single composer keeps the same Markdown tools for comments, replies and edits.
for (const discussion of document.querySelectorAll("[data-comments]")) {
  const form = discussion.querySelector("[data-comment-form]");
  const home = discussion.querySelector("[data-comment-home]");
  const body = form.elements.namedItem("body");
  const replyTo = form.elements.namedItem("reply_to");
  const images = form.elements.namedItem("images");
  const context = form.querySelector("[data-comment-context]");
  const contextLabel = form.querySelector("[data-comment-context-label]");
  const editorLabel = form.querySelector("[data-comment-editor-label]");
  const imageOptions = form.querySelector("[data-comment-image-options]");
  const submit = form.querySelector("[data-comment-submit]");
  const createAction = form.action;
  const drafts = new Map();
  let activeKey = "new";
  let activeButton = null;
  let activeComment = null;

  const saveDraft = () => drafts.set(activeKey, {
    body: body.value, files: Array.from(images.files),
    removed: Array.from(imageOptions.querySelectorAll("input:checked"), (input) => input.value),
  });
  const clearSelection = () => {
    activeComment?.classList.remove("is-comment-target");
    activeButton?.setAttribute("aria-pressed", "false");
    activeComment = null;
    activeButton = null;
  };
  const loadDraft = (initialBody = "") => {
    form.reset();
    const draft = drafts.get(activeKey);
    body.value = draft?.body ?? initialBody;
    if (draft?.files.length) {
      const selection = new DataTransfer();
      for (const file of draft.files) selection.items.add(file);
      images.files = selection.files;
    }
    for (const checkbox of imageOptions.querySelectorAll("input")) checkbox.checked = draft?.removed.includes(checkbox.value) || false;
    body.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const cancel = () => {
    drafts.delete(activeKey);
    clearSelection();
    activeKey = "new";
    form.action = createAction;
    replyTo.value = "";
    context.hidden = true;
    imageOptions.replaceChildren();
    imageOptions.hidden = true;
    form.classList.remove("is-comment-active");
    editorLabel.textContent = "Write a comment";
    submit.textContent = "Post comment";
    home.append(form);
    loadDraft();
    body.focus({ preventScroll: true });
  };
  form.querySelector("[data-comment-cancel]").addEventListener("click", cancel);
  discussion.addEventListener("click", (event) => {
    const button = event.target.closest("[data-comment-reply], [data-comment-edit]");
    if (!button || !discussion.contains(button)) return;
    if (button === activeButton) { cancel(); return; }
    saveDraft();
    clearSelection();
    const comment = button.closest("[data-comment-id]");
    const editing = button.hasAttribute("data-comment-edit");
    activeKey = `${editing ? "edit" : "reply"}:${comment.dataset.commentId}`;
    activeComment = comment;
    activeButton = button;
    comment.classList.add("is-comment-target");
    button.setAttribute("aria-pressed", "true");
    context.hidden = false;
    contextLabel.textContent = `${editing ? "Editing comment by" : "Replying to"} ${comment.querySelector("[data-comment-author]").textContent}`;
    editorLabel.textContent = editing ? "Edit comment" : "Write a reply";
    submit.textContent = editing ? "Save comment" : "Post reply";
    form.action = editing ? button.dataset.commentEdit : createAction;
    imageOptions.replaceChildren();
    if (editing) imageOptions.append(comment.querySelector("[data-comment-image-template]").content.cloneNode(true));
    imageOptions.hidden = !imageOptions.children.length;
    form.classList.add("is-comment-active");
    comment.after(form);
    loadDraft(editing ? JSON.parse(comment.querySelector("[data-comment-source]").textContent) : "");
    replyTo.value = editing ? "" : comment.dataset.commentId;
    form.scrollIntoView({ block: "nearest", behavior: "smooth" });
    body.focus({ preventScroll: true });
  });
}

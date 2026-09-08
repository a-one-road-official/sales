/**
 * Calendar Busy Mirror - canonical production source
 * SOURCE: 16ec099z@gmail.com
 * DESTINATION: admin@a1-road.com
 *
 * CREATE-only contract: never edit/delete existing calendar events.
 */

const CONFIG = Object.freeze({
  SOURCE_CALENDAR_ID: '16ec099z@gmail.com',
  DESTINATION_CALENDAR_ID: 'admin@a1-road.com',
  LOOKAHEAD_DAYS: 365,
  TITLE: 'UNAVAILABLE',
  MARKER: 'MIRROR_SYNC_V1\nSOURCE_CALENDAR=16ec099z@gmail.com',
  TRIGGER_EVERY_MINUTES: 15,
});

function install() {
  ensureTrigger_();
  syncBusyToAdmin();
}

function syncBusyToAdmin() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;

  try {
    const now = new Date();

    // A1_CALENDAR_RECURSION_FIX_V5
    // Google FreeBusy clips an already-running busy interval to request.timeMin.
    // If timeMin=now, the start changes on every 15-minute trigger and creates
    // the staircase recursion. Query from the start of the local day so the
    // returned start remains stable throughout the day.
    const freeBusyStart = new Date(now);
    freeBusyStart.setHours(0, 0, 0, 0);

    const timeMax = new Date(
      now.getTime() + CONFIG.LOOKAHEAD_DAYS * 24 * 60 * 60 * 1000
    );

    const sourceBusy = getSourceBusy_(freeBusyStart, timeMax);
    const existing = getExistingMirrorState_(freeBusyStart, timeMax);

    let created = 0;
    let skipped = 0;

    for (const busy of sourceBusy) {
      const start = new Date(busy.start);
      const end = new Date(busy.end);
      if (!isValidInterval_(start, end)) continue;

      const key = intervalKey_(start, end);
      if (existing.keys.has(key) || isCoveredByExistingMirror_(start, end, existing.intervals)) {
        skipped++;
        continue;
      }

      createMirrorEvent_(busy.start, busy.end);
      existing.keys.add(key);
      existing.intervals.push({ start, end });
      created++;
      Utilities.sleep(100);
    }

    console.log(JSON.stringify({ created, skipped, sourceBusy: sourceBusy.length }));
  } finally {
    lock.releaseLock();
  }
}

// Compatibility with the already-installed production trigger.
function syncBusyCalendar() {
  return syncBusyToAdmin();
}

function getSourceBusy_(timeMin, timeMax) {
  const response = Calendar.Freebusy.query({
    timeMin: timeMin.toISOString(),
    timeMax: timeMax.toISOString(),
    items: [{ id: CONFIG.SOURCE_CALENDAR_ID }],
  });

  const calendar = response.calendars && response.calendars[CONFIG.SOURCE_CALENDAR_ID];
  if (!calendar) throw new Error('SOURCE calendar missing from FreeBusy response');
  if (calendar.errors && calendar.errors.length) {
    throw new Error('SOURCE FreeBusy error: ' + JSON.stringify(calendar.errors));
  }
  return calendar.busy || [];
}

function getExistingMirrorState_(timeMin, timeMax) {
  const keys = new Set();
  const intervals = [];
  let pageToken;

  do {
    const response = Calendar.Events.list(CONFIG.DESTINATION_CALENDAR_ID, {
      timeMin: timeMin.toISOString(),
      timeMax: timeMax.toISOString(),
      singleEvents: true,
      showDeleted: false,
      maxResults: 2500,
      pageToken: pageToken,
    });

    for (const event of (response.items || [])) {
      if ((event.description || '') !== CONFIG.MARKER) continue;
      if (!event.start || !event.end) continue;

      const startRaw = event.start.dateTime || event.start.date;
      const endRaw = event.end.dateTime || event.end.date;
      if (!startRaw || !endRaw) continue;

      const start = new Date(startRaw);
      const end = new Date(endRaw);
      if (!isValidInterval_(start, end)) continue;

      keys.add(intervalKey_(start, end));
      intervals.push({ start, end });
    }

    pageToken = response.nextPageToken;
  } while (pageToken);

  return { keys, intervals };
}

function isCoveredByExistingMirror_(start, end, intervals) {
  const startMs = start.getTime();
  const endMs = end.getTime();
  return intervals.some(i => i.start.getTime() <= startMs && i.end.getTime() >= endMs);
}

function createMirrorEvent_(startDateTime, endDateTime) {
  Calendar.Events.insert({
    summary: CONFIG.TITLE,
    description: CONFIG.MARKER,
    visibility: 'private',
    transparency: 'opaque',
    start: { dateTime: startDateTime },
    end: { dateTime: endDateTime },
  }, CONFIG.DESTINATION_CALENDAR_ID, { sendUpdates: 'none' });
}

function ensureTrigger_() {
  const handlers = new Set(['syncBusyToAdmin', 'syncBusyCalendar']);
  const exists = ScriptApp.getProjectTriggers().some(t => handlers.has(t.getHandlerFunction()));
  if (exists) return;

  ScriptApp.newTrigger('syncBusyToAdmin')
    .timeBased()
    .everyMinutes(CONFIG.TRIGGER_EVERY_MINUTES)
    .create();
}

function intervalKey_(start, end) {
  return `${start.getTime()}|${end.getTime()}`;
}

function isValidInterval_(start, end) {
  return start instanceof Date && end instanceof Date &&
    !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) &&
    end.getTime() > start.getTime();
}

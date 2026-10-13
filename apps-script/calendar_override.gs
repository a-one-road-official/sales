/**
 * A1_CALENDAR_EVENT_OVERRIDE_V6
 * Owner-scoped, exact all-day-instance exceptions. No event writes here.
 * Rules stay in owner-only destination events; private IDs never enter Git.
 * All unconfigured source events and the installed trigger cadence are retained.
 */
function a1MirrorFreebusyQueryV6_(request) {
  const response = Calendar.Freebusy.query(request);
  const source = '16ec099z@gmail.com';
  const destination = 'admin@a1-road.com';
  const calendar = response.calendars && response.calendars[source];
  if (!calendar || (calendar.errors && calendar.errors.length)) return response;
  const low = Date.parse(request.timeMin);
  const high = Date.parse(request.timeMax);
  if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) {
    throw new Error('A1_OVERRIDE_INVALID_QUERY_WINDOW');
  }
  const marker = 'A1_MIRROR_OVERRIDE_V1=';
  const rules = [];
  let token;
  do {
    const params = {
      timeMin: request.timeMin, timeMax: request.timeMax,
      singleEvents: true, showDeleted: false, maxResults: 2500, q: marker.slice(0, -1)
    };
    if (token) params.pageToken = token;
    const page = Calendar.Events.list(destination, params);
    for (const host of page.items || []) {
      // Only a solo event owned by the destination account may carry control.
      if (host.status === 'cancelled' || !host.organizer ||
          host.organizer.email !== destination ||
          (host.attendees || []).some(a => a.email !== destination)) continue;
      const match = (host.description || '').match(/A1_MIRROR_OVERRIDE_V1=(\{[^\n]*?\})/);
      if (!match) continue;
      let rule;
      try { rule = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')); }
      catch (_) { throw new Error('A1_OVERRIDE_INVALID_RULE_JSON'); }
      if (rule.source_calendar !== source || rule.action !== 'exclude_all_day_instance') {
        throw new Error('A1_OVERRIDE_UNSUPPORTED_RULE');
      }
      if (typeof rule.source_event_id !== 'string' || !rule.source_event_id ||
          !/^\d{4}-\d{2}-\d{2}$/.test(rule.start_date || '') ||
          !/^\d{4}-\d{2}-\d{2}$/.test(rule.end_date || '') ||
          rule.timezone !== 'Asia/Tokyo') throw new Error('A1_OVERRIDE_INVALID_RULE');
      const start = Date.parse(rule.start_date + 'T00:00:00+09:00');
      const end = Date.parse(rule.end_date + 'T00:00:00+09:00');
      if (!Number.isFinite(start) || end - start !== 86400000) {
        throw new Error('A1_OVERRIDE_REQUIRES_EXACT_ONE_DAY');
      }
      if (start < high && end > low) rules.push({ rule, start, end });
    }
    token = page.nextPageToken;
  } while (token);
  if (!rules.length) return response;

  // Keep the provider's original busy intervals to cross-check source completeness.
  const original = a1MergeIntervalsV6_((calendar.busy || []).map(b => [Date.parse(b.start), Date.parse(b.end)]));
  let repaired = original.map(b => b.slice());
  const windows = a1MergeIntervalsV6_(rules.map(r => [Math.max(low, r.start), Math.min(high, r.end)]));
  for (const window of windows) {
    const all = [], kept = [];
    let pageToken;
    do {
      const params = {
        timeMin: new Date(window[0]).toISOString(), timeMax: new Date(window[1]).toISOString(),
        singleEvents: true, showDeleted: false, maxResults: 2500
      };
      if (pageToken) params.pageToken = pageToken;
      const page = Calendar.Events.list(source, params);
      for (const event of page.items || []) {
        if (event.status === 'cancelled' || event.transparency === 'transparent' ||
            (event.attendees || []).some(a => a.self && a.responseStatus === 'declined')) continue;
        if (!event.start || !event.end) throw new Error('A1_OVERRIDE_SOURCE_BOUNDARIES_MISSING');
        let start, end;
        if (event.start.dateTime && event.end.dateTime) {
          start = Date.parse(event.start.dateTime); end = Date.parse(event.end.dateTime);
        } else if (event.start.date && event.end.date) {
          if (page.timeZone !== 'Asia/Tokyo') throw new Error('A1_OVERRIDE_SOURCE_TIMEZONE_UNVERIFIED');
          start = Date.parse(event.start.date + 'T00:00:00+09:00');
          end = Date.parse(event.end.date + 'T00:00:00+09:00');
        } else { throw new Error('A1_OVERRIDE_SOURCE_BOUNDARIES_INVALID'); }
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
          throw new Error('A1_OVERRIDE_SOURCE_INTERVAL_INVALID');
        }
        const clipped = [Math.max(start, window[0]), Math.min(end, window[1])];
        if (clipped[1] <= clipped[0]) continue;
        all.push(clipped);
        const excluded = rules.some(({ rule }) => event.id === rule.source_event_id &&
          !event.start.dateTime && !event.end.dateTime &&
          event.start.date === rule.start_date && event.end.date === rule.end_date);
        if (!excluded) kept.push(clipped);
      }
      pageToken = page.nextPageToken;
    } while (pageToken);
    const expected = a1MergeIntervalsV6_(original.map(b => [Math.max(b[0], window[0]), Math.min(b[1], window[1])]).filter(b => b[1] > b[0]));
    if (JSON.stringify(a1MergeIntervalsV6_(all)) !== JSON.stringify(expected)) {
      throw new Error('A1_OVERRIDE_EVENT_FREEBUSY_MISMATCH_NO_WRITES');
    }
    const outside = [];
    for (const b of repaired) {
      if (b[1] <= window[0] || b[0] >= window[1]) { outside.push(b); continue; }
      if (b[0] < window[0]) outside.push([b[0], window[0]]);
      if (b[1] > window[1]) outside.push([window[1], b[1]]);
    }
    repaired = a1MergeIntervalsV6_(outside.concat(kept));
  }
  calendar.busy = repaired.map(b => ({ start: new Date(b[0]).toISOString(), end: new Date(b[1]).toISOString() }));
  console.log('A1_CALENDAR_EVENT_OVERRIDE_V6 applied; unrelated timed and all-day events preserved');
  return response;
}

function a1MergeIntervalsV6_(intervals) {
  const sorted = intervals.map(b => {
    if (!Number.isFinite(b[0]) || !Number.isFinite(b[1]) || b[1] <= b[0]) {
      throw new Error('A1_OVERRIDE_INVALID_BUSY_INTERVAL');
    }
    return b.slice();
  }).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const result = [];
  for (const b of sorted) {
    const last = result[result.length - 1];
    if (last && b[0] <= last[1]) last[1] = Math.max(last[1], b[1]);
    else result.push(b);
  }
  return result;
}

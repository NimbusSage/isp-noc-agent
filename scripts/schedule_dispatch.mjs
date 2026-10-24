#!/usr/bin/env node
// Field-tech dispatch scheduler — Sonar ticket ➜ Google Calendar appointment.
//
// Companion to the `schedule-field-tech` skill. Maps a Sonar ticket to a
// Google Calendar appointment for a field tech:
//   - event title      = ticket subject (subject → appointment title)
//   - event location   = site coordinates parsed from the ticket (or --lat/--lon)
//   - event description = initial ticket details + latest ticket comments
//
// Calendar strategy (operator rule):
//   1. Attempt a DIRECT insert on the tech's calendar when the consenting user
//      holds writer/owner access there.
//   2. On 401/403/404 (not allowed) fall back to the shared dispatch account
//      (CALENDAR_FALLBACK_CALENDAR_ID, default `primary`) and add the tech as
//      an attendee guest.
//
// Tech calendars are resolved from the consenting user's calendarList by
// EXACT normalized display-name match (summary/summaryOverride), optionally
// decorated by the tech calendar map (calendar_id / attendee overrides).
// Map location: site-pack/tech_calendars.json by default (untracked, org data);
// override with CALENDAR_TECH_MAP_FILE or --tech_map.
// Never guess a tech from a fuzzy name: zero or multiple exact matches fail.
//
// Auth: user-OAuth only, via scripts/google_user_auth.mjs (same pattern as
// temp_ospf_sheet.mjs / tracker_sheet.mjs). Secrets live outside the repo
// (owner-only file); CALENDAR_OAUTH_FILE defaults to the tracker's OAuth file.
//
// Commands (read-only unless noted):
//   oauth-url                        one-time consent URL (calendar scopes)
//   oauth-exchange --code CODE       exchange a pasted consent code (no store)
//   oauth-bootstrap --code CODE      exchange AND store refresh token (0600)
//   resolve --ticket ID --tech NAME  full read-only plan (event body + target
//                                    calendar + dup check); NO writes
//   create --ticket ID --tech NAME [--start WALL] [--end WALL] [--duration-min N]
//          --confirm-create true     GATED appointment creation + readback +
//                                    audit (write gates below)
//   sonar-open --ticket ID           GATED Sonar status flip on dispatch
//          --confirm-sonar true      PENDING_INTERNAL → OPEN; NO comment is
//                                    posted (the tech's completion notes are
//                                    the comment — the dispatch lead's daily
//                                    standard; NOC dispatches nights/weekends
//                                    when the lead is off the clock)
//   delete-event --ticket ID         GATED removal of the appointment(s)
//          --confirm-delete true     carrying this ticket's dedup key
//                                    (fallback calendar, or --calendar target)
//   mark-dispatched --ticket ID      GATED tracker-sheet Status=Dispatched
//          --confirm-write true      for the scheduled ticket's own row
//
// Gates for create (ALL required):
//   CALENDAR_WRITE_ENABLED=true AND CLI --confirm-create true
// Gates for sonar-open (ALL required):
//   DISPATCH_SONAR_ENABLED=true AND CLI --confirm-sonar true
// Gates for delete-event (ALL required):
//   CALENDAR_WRITE_ENABLED=true AND CLI --confirm-delete true (removes ONLY
//   the event(s) carrying this ticket's sonar_ticket_id key)
// Gates for mark-dispatched (ALL required):
//   TRACKER_ACCESS=write AND TRACKER_WRITE_ENABLED=true AND --confirm-write true
//
// Event dedup: every event carries
//   extendedProperties.private.sonar_ticket_id = "<ticket id>"
// and create refuses to run while an event with that key already exists on the
// target calendar (unless --allow-dup). The event id is NOT stored anywhere;
// the calendar is the source of truth (searched by the private key).
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  GOOGLE_CALENDAR,
  GOOGLE_CALENDAR_EVENTS,
  GOOGLE_CALENDAR_READONLY,
  GOOGLE_SHEETS,
  GOOGLE_SHEETS_READONLY,
  GOOGLE_DRIVE_READONLY,
  assertSecretFileOutsideRepo,
  buildUserConsentUrl,
  exchangeAuthCode,
  getGoogleUserToken,
  readOAuthSecrets,
  storeRefreshToken,
} from './google_user_auth.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CALENDAR_API = 'https://www.googleapis.com/calendar/v3';
const SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets';

function loadProjectEnv(file = resolve(ROOT, '.env')) {
  let text;
  try { text = readFileSync(file, 'utf8'); } catch { return; }
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match || Object.hasOwn(process.env, match[1])) continue;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
      if (match[2].startsWith('"')) {
        try { value = JSON.parse(match[2]); } catch { /* retain unwrapped value */ }
      }
    }
    process.env[match[1]] = value;
  }
}

export function auditFilePath(e = process.env) {
  return e.DISPATCH_AUDIT_FILE ? resolve(e.DISPATCH_AUDIT_FILE) : resolve(ROOT, 'logs/schedule_dispatch_audit.jsonl');
}

function audit(record, e = process.env) {
  const file = auditFilePath(e);
  mkdirSync(dirname(file), { recursive: true });
  appendFileSync(
    file,
    `${JSON.stringify({ ts: new Date().toISOString(), ...record })}\n`
  );
}

export function loadConfig(e = process.env) {
  const sonarEnabled = e.DISPATCH_SONAR_ENABLED === 'true';
  const authMode = e.CALENDAR_AUTH_MODE || 'user-oauth';
  if (authMode !== 'user-oauth') throw new Error('CALENDAR_AUTH_MODE must be user-oauth (dispatch-account consent)');
  const oauthFile = e.CALENDAR_OAUTH_FILE || e.TRACKER_OAUTH_FILE;
  if (!oauthFile) throw new Error('Missing CALENDAR_OAUTH_FILE (or TRACKER_OAUTH_FILE fallback); set the user-OAuth secret path in .env');
  return {
    sonarEnabled,
    oauthFile,
    fallbackCalendarId: e.CALENDAR_FALLBACK_CALENDAR_ID || 'primary',
    techMapFile: e.CALENDAR_TECH_MAP_FILE
      ? resolve(e.CALENDAR_TECH_MAP_FILE)
      : resolve(ROOT, 'site-pack/tech_calendars.json'),
    writeEnabled: e.CALENDAR_WRITE_ENABLED === 'true',
    timezone: e.DISPATCH_TIMEZONE || 'America/Chicago',
    defaultStartHour: Number(e.DISPATCH_DEFAULT_START_HOUR || 8),
    defaultDurationMinutes: Number(e.DISPATCH_DEFAULT_DURATION_MINUTES || 60),
    notify: e.DISPATCH_NOTIFY || 'none',
    trackerSpreadsheetId: e.TRACKER_SPREADSHEET_ID || null,
    trackerTab: e.TRACKER_TAB || 'NOC Ticket Tracker',
    trackerWriteEnabled: e.TRACKER_ACCESS === 'write' && e.TRACKER_WRITE_ENABLED === 'true',
  };
}

/** Load the optional tech-calendars map. Returns {} when the file is absent. */
export function loadTechMap(file) {
  if (!file || !existsSync(file)) return {};
  const parsed = JSON.parse(readFileSync(file, 'utf8'));
  const map = {};
  for (const [key, value] of Object.entries(parsed || {})) {
    map[normalizeName(key)] = {
      calendarId: value?.calendar_id || value?.calendarId || null,
      attendee: value?.attendee || value?.email || null,
    };
  }
  return map;
}

export function normalizeName(value) {
  return String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Tech calendars are decorated in the wild ("*Sam Rivera | ST",
 *  "3Tech One | ST", "Tech Two - Charlie ST", "Tech Three | L2 |
 *  Alpha Falls, ST"). Extract the bare tech name: drop the leading run of
 *  non-letter marks/digits, cut at the first '|' or '- location' separator.
 *  A name-internal hyphen would be split this way — none exist in the fleet,
 *  and a wrong split fails loudly as "no match" rather than mis-dispatching.
 */
export function techNameFromSummary(summary) {
  let name = String(summary || '').replace(/^[^\p{L}]+/u, '');
  const cut = name.search(/\s*\|\s*|[.,]\s|\s-{1,2}\s|-(?=\s)/);
  if (cut > 0) name = name.slice(0, cut);
  return normalizeName(name);
}

// ---------------------------------------------------------------- Coordinates

/** Parse "at 10.123, -20.456" style coords from ticket text. */
export function parseTicketCoords(text) {
  const re = /\b(-?\d{1,2}\.\d{3,8})[,\s]+(-?\d{1,3}\.\d{3,8})\b/g;
  for (const match of String(text || '').matchAll(re)) {
    const lat = Number(match[1]);
    const lon = Number(match[2]);
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat: match[1], lon: match[2] };
  }
  return null;
}

// ----------------------------------------------------------------- Formatting

function decodeEntities(text) {
  return String(text || '')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/gu, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

export function stripHtml(html) {
  return decodeEntities(
    String(html || '')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/p>/gi, '\n')
      .replace(/<[^>]+>/g, '')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
  ).trim();
}

/** Sonar base: env SONAR_BASE_URL, else install profile org.sonar_base_url,
 *  else a neutral example (links go to a dead address instead of someone
 *  else's Sonar instance). Shared by ticketUrl() and the GraphQL fallback. */
export function profileBase() {
  const envUrl = (process.env.SONAR_BASE_URL || '').trim();
  if (envUrl) return envUrl.replace(/\/$/, '');
  try {
    const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
    const prof = JSON.parse(readFileSync(resolve(ROOT, 'site-pack', 'profile.json'), 'utf8'));
    const base = ((prof.org || {}).sonar_base_url || '').trim();
    if (base) return base.replace(/\/$/, '');
  } catch {}
  return 'https://your-org.sonar.software';
}

export function ticketUrl(ticketId) {
  return `${profileBase()}/app#/tickets/show/${ticketId}`;
}

const MAX_DESCRIPTION_CHARS = 8000;

/** Build the plain-text event description: header + ticket details + comments. */
const PRIORITY_EMOJI = { LOW: '🟢', MEDIUM: '🟡', HIGH: '🟠', CRITICAL: '🔴' };

/** "Alphaville,  ST - Bravo Feeders: No Power" → "Bravo Feeders".
 *  Town/region prefix is stripped; the site label keeps ticket id/URL out of the
 *  details while the appointment body stays short for mobile. */
export function siteFromSubject(subject) {
  const core = String(subject || '').split(':')[0] || '';
  const parts = core.split(/\s+-\s+/);
  return parts.length > 1 ? parts.pop().replace(/\s{2,}/g, ' ').trim() : '';
}

/** Sonar marks empty separators with standalone "|" lines — display them as
 *  blank lines (operator rule) and squeeze repeats. */
function normalizeDetailLines(text) {
  return String(text || '')
    .split('\n')
    .map((line) => (/^\|+\s*$/.test(line.trim()) ? '' : line))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Comment times render in CT (techs read these on phones — UTC is noise). */
function commentStampCT(iso) {
  const ms = Date.parse(String(iso || ''));
  if (!Number.isFinite(ms)) return String(iso || '').replace('T', ' ').slice(0, 16) + ' UTC';
  return `${new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(ms)} CT`;
}

/** Mobile-first plain-text description (techs read these on phones):
 *  priority + tech quick-line, bare ticket link, site pin with coords, readable
 *  ticket details, then updates. No status/created noise, no id outside the
 *  URL (operator rules). */
export function buildDescription({ ticket, comments = [], commentsTruncated = false, coords, techName, site }) {
  const out = [];
  const link = ticketUrl(ticket.id);
  const emoji = PRIORITY_EMOJI[String(ticket.priority || '').toUpperCase()] || '🚦';
  const head = [`${emoji} Priority: ${ticket.priority || 'unknown'}`];
  if (techName) head.push(`🧰 Tech: ${techName}`);
  out.push(head.join('  ·  '));
  out.push('', `🔗 ${link}`);
  const siteLabel = siteFromSubject(ticket.subject) || site || '';
  if (coords) out.push('', `📍 ${siteLabel ? `${siteLabel} — ` : ''}${coords.lat}, ${coords.lon}`);
  const details = normalizeDetailLines(stripHtml(ticket.description)) || '(no description on ticket)';
  out.push('', '——— Ticket details ———', details);
  if (comments.length) {
    out.push('', commentsTruncated
      ? `——— Latest ${comments.length} ticket updates (see the link for more) ———`
      : '——— Updates from the ticket ———');
    for (const comment of comments) {
      out.push('', `🕐 ${commentStampCT(comment.created_at)} — ${comment.user || 'unknown'}`, normalizeDetailLines(stripHtml(comment.body)));
    }
  }
  let text = out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (text.length > MAX_DESCRIPTION_CHARS) {
    text = `${text.slice(0, MAX_DESCRIPTION_CHARS)}\n\n——— Truncated — full ticket: ${link}`;
  }
  return text;
}

// ------------------------------------------------------------- Calendar window

function wallParts(utcMs, timezone) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false, weekday: 'short',
  });
  const parts = {};
  for (const part of fmt.formatToParts(utcMs)) parts[part.type] = part.value;
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour % 24),
    minute: Number(parts.minute),
    weekday: parts.weekday,
    dateKey: `${parts.year}-${parts.month}-${parts.day}`,
  };
}

function tzOffsetMinutes(utcMs, timezone) {
  const fmt = new Intl.DateTimeFormat('en-US', { timeZone: timezone, timeZoneName: 'longOffset' });
  const name = (fmt.formatToParts(utcMs).find((p) => p.type === 'timeZoneName') || {}).value || 'GMT';
  if (name === 'GMT') return 0;
  const match = /^GMT([+-])(\d{1,2}):?(\d{2})?/.exec(name) || [];
  if (!match[1]) throw new Error(`Unrecognized timezone offset ${name} for ${timezone}`);
  const minutes = Number(match[2] || 0) * 60 + Number(match[3] || 0);
  return match[1] === '+' ? minutes : -minutes;
}

/** Wall-clock local string ("YYYY-MM-DDTHH:MM") in a timezone ➜ ISO instant. */
export function wallToIsoInstant(wall, timezone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{1,2}):(\d{2})$/.exec(String(wall).trim());
  if (!match) throw new Error(`Invalid --start/--end "${wall}": expected YYYY-MM-DDTHH:MM (wall clock in ${timezone})`);
  const [, y, mo, d, h, mi] = match;
  const wallUtc = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi));
  let offset = tzOffsetMinutes(wallUtc, timezone);
  let instant = wallUtc - offset * 60_000;
  const offsetAtInstant = tzOffsetMinutes(instant, timezone);
  if (offsetAtInstant !== offset) {
    offset = offsetAtInstant;
    instant = wallUtc - offset * 60_000;
  }
  const back = wallParts(instant, timezone);
  if (back.month !== Number(mo) || back.day !== Number(d) || back.hour !== Number(h) || back.minute !== Number(mi)) {
    throw new Error(`Wall time "${wall}" does not exist in ${timezone}`);
  }
  return new Date(instant).toISOString();
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** Default window: next business weekday at startHour in timezone. */
export function resolveDispatchWindow({
  now = new Date().toISOString(),
  timezone,
  start,
  end,
  startHour = 8,
  durationMinutes = 60,
} = {}) {
  if (start && end) {
    return {
      start: { dateTime: wallToIsoInstant(start, timezone), timeZone: timezone },
      end: { dateTime: wallToIsoInstant(end, timezone), timeZone: timezone },
    };
  }
  if (start && !end) {
    const startIso = wallToIsoInstant(start, timezone);
    return {
      start: { dateTime: startIso, timeZone: timezone },
      end: { dateTime: new Date(Date.parse(startIso) + durationMinutes * 60_000).toISOString(), timeZone: timezone },
    };
  }
  const nowMs = Date.parse(now);
  const pad = (n) => String(n).padStart(2, '0');
  for (let dayOffset = 0; dayOffset <= 14; dayOffset++) {
    const probe = wallParts(nowMs + dayOffset * 86_400_000, timezone);
    const dayIndex = WEEKDAYS.indexOf(probe.weekday);
    if (dayIndex < 1 || dayIndex > 5) continue;
    const wall = `${probe.year}-${pad(probe.month)}-${pad(probe.day)}T${pad(startHour)}:00`;
    const startIso = wallToIsoInstant(wall, timezone);
    if (Date.parse(startIso) <= nowMs) continue; // today slot already passed → next day
    return {
      start: { dateTime: startIso, timeZone: timezone },
      end: { dateTime: new Date(Date.parse(startIso) + durationMinutes * 60_000).toISOString(), timeZone: timezone },
    };
  }
  throw new Error('No business day found within 14 days for the default dispatch window');
}

// ------------------------------------------------------------ Tech resolution

/** Resolve the tech's calendar entry from calendarList + optional map override. */
export function chooseTechCalendar(entries, techName, techMap = {}) {
  const name = normalizeName(techName);
  if (!name) throw new Error('Tech name is required');
  const override = techMap[name];
  const exact = entries.filter((entry) => {
    if (!entry) return false;
    return normalizeName(entry.summaryOverride) === name
      || normalizeName(entry.summary) === name
      || techNameFromSummary(entry.summaryOverride || entry.summary) === name;
  });
  if (override?.calendarId) {
    const entry = exact.find((e) => e.id === override.calendarId) || entries.find((e) => e.id === override.calendarId) || null;
    const writable = entry ? entry.accessRole === 'owner' || entry.accessRole === 'writer' : true;
    const looksLikeEmail = entry && /.+@[^@\s]+$/.test(entry.id) && !entry.id.includes('@group.calendar.google.com');
    const mode = writable ? 'direct' : 'fallback';
    return {
      source: 'map',
      entry,
      calendarId: override.calendarId,
      attendee: override.attendee || (entry && looksLikeEmail ? entry.id : null),
      mode,
    };
  }
  if (exact.length === 0) {
    throw new Error(`No calendar matching "${techName}" in the consenting user's calendarList (exact or bare-name match required; set CALENDAR_TECH_MAP_FILE or --calendar to override)`);
  }
  const rank = { owner: 2, writer: 1 };
  exact.sort((a, b) => (rank[b.accessRole] || 0) - (rank[a.accessRole] || 0));
  if (exact.length > 1 && (rank[exact[0].accessRole] || 0) === (rank[exact[1].accessRole] || 0)) {
    // Same-rank duplicates stay ambiguous even if one is an exact-name match
    // and the other only matches after decoration stripping.
    const exactTop = exact.some((e) => normalizeName(e.summaryOverride) === name || normalizeName(e.summary) === name);
    const preferExact = exactTop ? exact.filter((e) => normalizeName(e.summaryOverride) === name || normalizeName(e.summary) === name) : exact;
    if (preferExact.length === 1) {
      const entry = preferExact[0];
      const writable = entry.accessRole === 'owner' || entry.accessRole === 'writer';
      const looksLikeEmail = /.+@[^@\s]+$/.test(entry.id) && !entry.id.includes('@group.calendar.google.com');
      return { source: 'calendarList', entry, calendarId: entry.id, attendee: entry.id && looksLikeEmail ? entry.id : null, mode: writable ? 'direct' : 'fallback' };
    }
    const names = exact.map((e) => `${e.id} (${e.accessRole})`).join(', ');
    throw new Error(`Ambiguous calendars match "${techName}": ${names}. Set CALENDAR_TECH_MAP_FILE or pass --calendar`);
  }
  const entry = exact[0];
  const writable = entry.accessRole === 'owner' || entry.accessRole === 'writer';
  const looksLikeEmail = /.+@[^@\s]+$/.test(entry.id) && !entry.id.includes('@group.calendar.google.com');
  return {
    source: 'calendarList',
    entry,
    calendarId: entry.id,
    attendee: entry.id && looksLikeEmail ? entry.id : null,
    mode: writable ? 'direct' : 'fallback',
  };
}

export function buildEventBody({ ticket, coords, description, window: win, techName, ticketId, attendee }) {
  const body = {
    summary: stripHtml(ticket.subject),
    description,
    location: `${coords.lat}, ${coords.lon}`,
    start: win.start,
    end: win.end,
    extendedProperties: {
      private: {
        sonar_ticket_id: String(ticketId),
        sonar_dispatch: 'noc-dispatch-v1',
        dispatched_tech: techName || '',
      },
    },
  };
  if (attendee) body.attendees = [{ email: attendee }];
  return body;
}

// ---------------------------------------------------------------- Sonar fetch

const SONAR_GRAPHQL_URL = process.env.SONAR_GRAPHQL_URL || `${profileBase()}/api/graphql`;

export function parseSonarTicket(entity) {
  return {
    id: String(entity.id),
    subject: entity.subject || '',
    description: entity.description || '',
    status: entity.status || '',
    priority: entity.priority || '',
    group: entity.ticket_group?.name || null,
    accountId: entity.account?.id ? String(entity.account.id) : null,
    created_at: entity.created_at || null,
    updated_at: entity.updated_at || null,
  };
}

export async function fetchSonarTicket(fetchImpl, { ticketId, sonarToken = process.env.SONAR_TOKEN, commentsLimit = 10 }) {
  if (!sonarToken) throw new Error('SONAR_TOKEN missing from environment (.env)');
  const headers = { Authorization: `Bearer ${sonarToken}` };
  const run = async (query, variables = {}) => {
    const res = await fetchImpl(SONAR_GRAPHQL_URL, {
      method: 'POST',
      headers: { ...headers, 'content-type': 'application/json' },
      body: JSON.stringify({ query, variables }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) throw new Error(`Sonar GraphQL failed (HTTP ${res.status})`);
    const body = await res.json();
    if (body.errors?.length) throw new Error(`Sonar GraphQL error: ${body.errors.map((e) => e.message).join('; ').slice(0, 300)}`);
    return body.data;
  };
  const { id: idVar } = { id: Number(ticketId) };
  const ticketData = await run(
    `query dispatchTicket($id: Int64Bit!) {
      tickets(search: { integer_fields: [{ attribute: "id", operator: EQ, search_value: $id }] }) {
        entities {
          id subject description status priority created_at updated_at
          ticket_group { id name }
        }
      }
    }`,
    { id: idVar }
  );
  const entity = ticketData?.tickets?.entities?.[0];
  if (!entity) throw new Error(`Sonar ticket ${ticketId} not found`);
  const commentsData = await run(
    `query dispatchComments($id: Int64Bit!) {
      ticket_comments(
        search: { integer_fields: [{ attribute: "ticket_id", operator: EQ, search_value: $id }] }
        sorter: { attribute: "created_at", direction: DESC }
        paginator: { page: 1, records_per_page: ${commentsLimit} }
      ) {
        entities { id body created_at user { name } }
        page_info { total_count }
      }
    }`,
    { id: idVar }
  );
  const connection = commentsData?.ticket_comments;
  const entities = connection?.entities || [];
  return {
    ticket: parseSonarTicket(entity),
    comments: entities.reverse().map((c) => ({ id: c.id, body: c.body, created_at: c.created_at, user: c.user?.name || null })),
    commentsTotal: connection?.page_info?.total_count ?? entities.length,
    commentsTruncated: (connection?.page_info?.total_count ?? entities.length) > entities.length,
  };
}

// ------------------------------------------------------------- Google API I/O

function scopeForAccessType(write) {
  return write ? GOOGLE_CALENDAR_EVENTS : GOOGLE_CALENDAR_READONLY;
}

function calendarApiPath(calendarId) {
  return `${CALENDAR_API}/calendars/${encodeURIComponent(calendarId)}`;
}

async function calendarRequest(fetchImpl, cfg, { path, method = 'GET', body = null, write = false, searchParams = {} }) {
  const token = await getGoogleUserToken(cfg.oauthFile, scopeForAccessType(write), fetchImpl);
  const url = new URL(path);
  for (const [key, value] of Object.entries(searchParams)) url.searchParams.set(key, value);
  const res = await fetchImpl(url, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30_000),
  });
  const responseText = await res.text();
  let parsed = {};
  try { parsed = JSON.parse(responseText); } catch { parsed = { raw: responseText.slice(0, 300) }; }
  if (!res.ok) {
    const error = new Error(`Google Calendar ${method} ${new URL(path).pathname} failed (HTTP ${res.status}): ${(parsed.error?.message || responseText).slice(0, 200)}`);
    error.status = res.status;
    throw error;
  }
  return parsed;
}

/** Read the consenting user's calendarList (paginated). */
export async function listCalendars(fetchImpl, cfg) {
  const items = [];
  let pageToken = '';
  do {
    const params = { maxResults: '250' };
    if (pageToken) params.pageToken = pageToken;
    const page = await calendarRequest(fetchImpl, cfg, {
      path: `${CALENDAR_API}/users/me/calendarList`,
      searchParams: params,
    });
    items.push(...(page.items || []));
    pageToken = page.nextPageToken || '';
  } while (pageToken);
  return items;
}

export async function searchDupEvents(fetchImpl, cfg, { calendarId, ticketId }) {
  // Google expects the extended-property query as `key=value` — a colon form
  // 400s with `incompleteExtendedPropertyMatch` (verified live).
  const page = await calendarRequest(fetchImpl, cfg, {
    path: `${calendarApiPath(calendarId)}/events`,
    searchParams: { privateExtendedProperty: `sonar_ticket_id=${ticketId}`, maxResults: '10', showDeleted: 'false' },
  });
  return (page.items || []).map((e) => ({ id: e.id, summary: e.summary, start: e.start?.dateTime || e.start?.date, htmlLink: e.htmlLink }));
}

export async function getEvent(fetchImpl, cfg, { calendarId, eventId }) {
  return calendarRequest(fetchImpl, cfg, { path: `${calendarApiPath(calendarId)}/events/${encodeURIComponent(eventId)}` });
}

async function insertEvent(fetchImpl, cfg, { calendarId, event, notify }) {
  const created = await calendarRequest(fetchImpl, cfg, {
    path: `${calendarApiPath(calendarId)}/events`,
    method: 'POST',
    body: event,
    write: true,
    searchParams: { sendUpdates: notify },
  });
  if (!created.id) throw new Error('Google Calendar insert did not return an event id');
  return created;
}

// ------------------------------------------------------------------- Workflow

async function buildPlan(fetchImpl, cfg, options) {
  const techMap = loadTechMap(options.techMapFile ?? cfg.techMapFile);
  const { ticket, comments, commentsTruncated } = await fetchSonarTicket(fetchImpl, { ticketId: options.ticket });
  const coords = options.lat && options.lon
    ? { lat: options.lat, lon: options.lon }
    : parseTicketCoords(`${ticket.subject}\n${ticket.description}`);
  if (!coords) {
    throw new Error(`No coordinates found in ticket ${ticket.id} subject/description; resolve them (ticket, tracker row, or network_sites) and pass --lat --lon`);
  }
  const win = resolveDispatchWindow({
    timezone: cfg.timezone,
    startHour: options.start_hour ? Number(options.start_hour) : cfg.defaultStartHour,
    durationMinutes: options.duration_min ? Number(options.duration_min) : cfg.defaultDurationMinutes,
    start: options.start,
    end: options.end,
    now: options.now,
  });
  const description = buildDescription({ ticket, comments, commentsTruncated, coords, techName: options.tech });
  return { techMap, ticket, coords, win, description };
}

export async function resolveDispatch(fetchImpl, cfg, options) {
  const plan = await buildPlan(fetchImpl, cfg, options);
  let tech;
  try {
    tech = chooseTechCalendar(await listCalendars(fetchImpl, cfg), options.tech, plan.techMap);
  } catch (error) {
    audit({ action: 'resolve', ok: false, ticket_id: options.ticket, tech: options.tech, error: String(error.message || error) });
    throw error;
  }
  if (options.calendar) tech.calendarId = options.calendar;
  if (options.attendee) tech.attendee = options.attendee;
  const dupCandidates = tech.calendarId
    ? await searchDupEvents(fetchImpl, cfg, { calendarId: tech.calendarId, ticketId: options.ticket })
    : [];
  const dupFallback = tech.calendarId && tech.calendarId !== cfg.fallbackCalendarId
    ? await searchDupEvents(fetchImpl, cfg, { calendarId: cfg.fallbackCalendarId, ticketId: options.ticket })
    : [];
  const result = {
    ok: true,
    mode: 'plan',
    ticket: { id: plan.ticket.id, subject: plan.ticket.subject, group: plan.ticket.group, status: plan.ticket.status, priority: plan.ticket.priority, created_at: plan.ticket.created_at },
    coords: plan.coords,
    window: plan.win,
    event: buildEventBody({ ticket: plan.ticket, coords: plan.coords, description: plan.description, window: plan.win, techName: options.tech, ticketId: plan.ticket.id, attendee: null }),
    tech: {
      name: options.tech,
      calendarId: tech.calendarId,
      summary: tech.entry?.summary || tech.entry?.summaryOverride || null,
      accessRole: tech.entry?.accessRole || null,
      mode: tech.mode,
      attendee: tech.attendee,
      source: tech.source,
    },
    fallbackCalendarId: cfg.fallbackCalendarId,
    duplicates: { target: dupCandidates, fallback: dupFallback },
  };
  audit({ action: 'resolve', ok: true, ticket_id: plan.ticket.id, tech: options.tech, mode: tech.mode, calendar_id: tech.calendarId });
  return result;
}

export async function createDispatch(fetchImpl, cfg, options) {
  // Gate: app-level kill switch + explicit operator confirmation.
  if (!cfg.writeEnabled) throw new Error('CALENDAR_WRITE_ENABLED must be true in .env to create appointments');
  if (options.confirmCreate !== 'true') throw new Error('Explicit --confirm-create true is required to create appointments');
  const plan = await buildPlan(fetchImpl, cfg, options);
  const calendarEntries = await listCalendars(fetchImpl, cfg);
  const tech = chooseTechCalendar(calendarEntries, options.tech, plan.techMap);
  if (options.calendar) tech.calendarId = options.calendar;
  if (options.attendee) tech.attendee = options.attendee;
  if (options.calendar && !tech.mode) tech.mode = 'direct'; // override target: attempt direct first
  const notify = options.notify || cfg.notify;
  const ticketId = plan.ticket.id;
  const event = buildEventBody({
    ticket: plan.ticket,
    coords: plan.coords,
    description: plan.description,
    window: plan.win,
    techName: options.tech,
    ticketId,
  });

  // Dedup on the target calendar (and the fallback) by sonar_ticket_id key.
  const dupTarget = await searchDupEvents(fetchImpl, cfg, { calendarId: tech.calendarId, ticketId });
  const dupFallback = tech.calendarId !== cfg.fallbackCalendarId
    ? await searchDupEvents(fetchImpl, cfg, { calendarId: cfg.fallbackCalendarId, ticketId })
    : [];
  if (!options.allowDup && (dupTarget.length || dupFallback.length)) {
    audit({ action: 'create', ok: false, ticket_id: ticketId, tech: options.tech, error: 'duplicate_event', dup: [...dupTarget, ...dupFallback] });
    throw new Error(`An appointment for ticket ${ticketId} already exists on the tech's (or fallback) calendar: ${[...dupTarget, ...dupFallback].map((d) => d.htmlLink).join(', ')} — use --allow-dup to override`);
  }

  const attempt = (calendarId) => insertEvent(fetchImpl, cfg, { calendarId, event, notify });
  let inserted;
  let usedCalendar;
  let usedMode;
  const directTarget = tech.calendarId === cfg.fallbackCalendarId ? null : tech.calendarId;
  if (tech.mode === 'direct' && directTarget) {
    try {
      inserted = await attempt(tech.calendarId);
      usedCalendar = tech.calendarId;
      usedMode = 'direct';
    } catch (directError) {
      const fallbackEligible = [401, 403, 404].includes(directError.status);
      if (!fallbackEligible) {
        audit({ action: 'create', ok: false, ticket_id: ticketId, tech: options.tech, calendar_id: tech.calendarId, error: String(directError.message || directError) });
        throw directError;
      }
      // Not allowed on the tech's calendar (401/403/404) → shared dispatch
      // account + tech guest (operator rule). Re-read/share fixes are done manually.
      if (!tech.attendee) {
        throw new Error(`Direct insert on ${tech.calendarId} not allowed (HTTP ${directError.status}) and no attendee email known for "${options.tech}" — set CALENDAR_TECH_MAP_FILE attendee or pass --attendee`);
      }
      event.attendees = [{ email: tech.attendee }];
      inserted = await attempt(cfg.fallbackCalendarId);
      usedCalendar = cfg.fallbackCalendarId;
      usedMode = 'fallback';
    }
  } else {
    // accessRole reader (or same-as-fallback target): skip the doomed attempt.
    if (!tech.attendee) {
      throw new Error(`Calendar "${tech.calendarId}" is ${tech.entry?.accessRole || 'unshared'} — create there is not allowed and no attendee email is known for "${options.tech}" — set CALENDAR_TECH_MAP_FILE attendee or pass --attendee`);
    }
    event.attendees = [{ email: tech.attendee }];
    inserted = await attempt(cfg.fallbackCalendarId);
    usedCalendar = cfg.fallbackCalendarId;
    usedMode = 'fallback';
  }
  const readback = await getEvent(fetchImpl, cfg, { calendarId: usedCalendar, eventId: inserted.id });
  const verifyOk = readback.summary === event.summary && (readback.location || '').startsWith(`${plan.coords.lat}`) && readback.extendedProperties?.private?.sonar_ticket_id === String(ticketId);
  if (!verifyOk) {
    audit({ action: 'create', ok: false, ticket_id: ticketId, tech: options.tech, calendar_id: usedCalendar, event_id: inserted.id, error: 'readback_mismatch' });
    throw new Error(`Appointment created but readback verification failed for ticket ${ticketId} (event ${inserted.id}) — check the calendar manually`);
  }
  const result = {
    ok: true,
    mode: usedMode,
    ticketId,
    tech: options.tech,
    calendarId: usedCalendar,
    eventId: inserted.id,
    htmlLink: inserted.htmlLink,
    attendee: usedMode === 'fallback' ? tech.attendee || null : null,
    window: plan.win,
  };
  audit({ action: 'create', ok: true, ticket_id: ticketId, tech: options.tech, mode: usedMode, calendar_id: usedCalendar, event_id: inserted.id, attendee: result.attendee });
  return result;
}

// ------------------------------------------------------------------- Sonar flip
// Dispatch convention (the dispatch lead): once a tech is scheduled
// on a Sonar ticket, the status goes PENDING_INTERNAL → OPEN and NO comment is
// posted — the comment only comes later, when the tech has done the job and
// has notes. NOC dispatches nights/weekends when the lead is off the clock.

export async function sonarOpenDispatch(fetchImpl, { ticketId, sonarToken = process.env.SONAR_TOKEN, sonarEnabled = false, confirmSonar }) {
  if (confirmSonar !== 'true') throw new Error('Explicit --confirm-sonar true is required for the Sonar status flip');
  if (!sonarEnabled) throw new Error('DISPATCH_SONAR_ENABLED must be true in .env to flip ticket status on dispatch');
  if (!sonarToken) throw new Error('SONAR_TOKEN missing from environment (.env)');
  const run = async (query, variables = {}) => {
    const res = await fetchImpl(SONAR_GRAPHQL_URL, {
      method: 'POST',
      headers: { Authorization: `Bearer ${sonarToken}`, 'content-type': 'application/json' },
      body: JSON.stringify({ query, variables }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) throw new Error(`Sonar GraphQL failed (HTTP ${res.status})`);
    const body = await res.json();
    if (body.errors?.length) throw new Error(`Sonar GraphQL error: ${body.errors.map((e) => e.message).join('; ').slice(0, 300)}`);
    return body.data;
  };
  const idVar = Number(ticketId);
  const statusData = await run(
    `query dispatchStatus($id: Int64Bit!) {
      tickets(search: { integer_fields: [{ attribute: "id", operator: EQ, search_value: $id }] }) {
        entities { id status }
      }
    }`,
    { id: idVar }
  );
  const current = statusData?.tickets?.entities?.[0];
  if (!current) throw new Error(`Sonar ticket ${ticketId} not found`);
  if (current.status === 'OPEN') {
    audit({ action: 'sonar-open', ok: true, ticket_id: String(ticketId), already_open: true });
    return { ok: true, alreadyOpen: true, ticketId: String(ticketId), status: 'OPEN' };
  }
  // Guard: ONLY the documented dispatch transition. Anything else is refused loudly.
  if (current.status !== 'PENDING_INTERNAL') {
    audit({ action: 'sonar-open', ok: false, ticket_id: String(ticketId), error: `status=${current.status}` });
    throw new Error(`Ticket ${ticketId} is ${current.status}, not PENDING_INTERNAL — refusing to flip (dispatch standard covers PENDING_INTERNAL → OPEN only)`);
  }
  const mutated = await run(
    `mutation dispatchOpen($id: Int64Bit!) {
      updateTicket(id: $id, input: { status: OPEN }) {
        id
        status
      }
    }`,
    { id: idVar }
  );
  const updated = mutated?.updateTicket;
  if (!updated?.id || updated.status !== 'OPEN') {
    audit({ action: 'sonar-open', ok: false, ticket_id: String(ticketId), error: `mutation_payload=${JSON.stringify(mutated).slice(0, 200)}` });
    throw new Error(`Sonar status flip did not verify (response missing id or status not OPEN) — DO NOT report success`);
  }
  // Deliberately NO comment here: the tech's completion notes are the comment.
  audit({ action: 'sonar-open', ok: true, ticket_id: String(ticketId), from: 'PENDING_INTERNAL', to: 'OPEN' });
  return { ok: true, ticketId: String(ticketId), status: updated.status, alreadyOpen: false };
}

export async function deleteDispatchEvent(fetchImpl, cfg, { ticketId, calendarId, confirmDelete }) {
  // Destructive: BOTH gates required (kill switch + explicit confirmation), and
  // ONLY events carrying this ticket's dedup key are removed — never a bare id.
  if (confirmDelete !== 'true') throw new Error('Explicit --confirm-delete true is required to remove an appointment');
  if (!cfg.writeEnabled) throw new Error('CALENDAR_WRITE_ENABLED must be true to delete appointments');
  const candidates = calendarId ? [calendarId, cfg.fallbackCalendarId] : [cfg.fallbackCalendarId];
  const searched = [...new Set(candidates)];
  for (const candidate of searched) {
    const found = await searchDupEvents(fetchImpl, cfg, { calendarId: candidate, ticketId });
    for (const ev of found) {
      await calendarRequest(fetchImpl, cfg, {
        path: `${calendarApiPath(candidate)}/events/${encodeURIComponent(ev.id)}`,
        method: 'DELETE',
        write: true,
      });
      // Google's Calendar API does NOT 404 on deleted events — it returns the
      // event with status "cancelled" (verified live). Both that
      // shape and a true 404 count as proof of deletion; anything else fails.
      let gone = false;
      try {
        const readback = await getEvent(fetchImpl, cfg, { calendarId: candidate, eventId: ev.id });
        gone = readback.status === 'cancelled';
      } catch (error) {
        if (error.status === 404) gone = true;
      }
      if (!gone) {
        throw new Error(`Delete readback still returns a live event ${ev.id} on ${candidate} — do NOT trust the delete`);
      }
      audit({ action: 'delete-event', ok: true, ticket_id: String(ticketId), calendar_id: candidate, event_id: ev.id });
      return { ok: true, deleted: ev.id, calendarId: candidate, htmlLink: ev.htmlLink };
    }
  }
  throw new Error(`No appointment carrying ticket ${ticketId} found on: ${searched.join(', ')} — pass --calendar <id> to search a specific calendar`);
}

// ---------------------------------------------------- tracker sheet row update

export async function markDispatched(fetchImpl, cfg, options) {
  if (!cfg.trackerSpreadsheetId) throw new Error('TRACKER_SPREADSHEET_ID missing from .env; cannot reach the tracker sheet');
  if (!cfg.trackerWriteEnabled) throw new Error('TRACKER_ACCESS=write + TRACKER_WRITE_ENABLED=true required in .env to update tracker rows');
  if (options.confirmWrite !== 'true') throw new Error('Explicit --confirm-write true is required for tracker Status updates');
  const spreadsheetId = cfg.trackerSpreadsheetId;
  const token = await getGoogleUserToken(cfg.oauthFile, GOOGLE_SHEETS_READONLY, fetchImpl);
  const headers = { Authorization: `Bearer ${token}` };
  const get = async (url) => {
    const res = await fetchImpl(url, { headers, signal: AbortSignal.timeout(30_000) });
    if (!res.ok) throw new Error(`Sheets GET failed (HTTP ${res.status})`);
    return res.json();
  };
  // SHEETS_API already ends with `/spreadsheets` — do not repeat the segment
  // (a duplicated `/spreadsheets/spreadsheets/…` path 404s as Google error HTML).
  const ticketRange = `${SHEETS_API}/${spreadsheetId}/values/${encodeURIComponent("'" + cfg.trackerTab + "'" + '!F2:F8000')}?majorDimension=ROWS`;
  const values = await get(ticketRange);
  const col = values.values || [];
  const rowIndex = col.findIndex((row) => String(row[0] ?? '').trim() === String(options.ticket));
  if (rowIndex < 0) {
    audit({ action: 'mark-dispatched', ok: false, ticket_id: options.ticket, error: 'row_not_found' });
    throw new Error(`Ticket #${options.ticket} not found in tracker tab "${cfg.trackerTab}" (col F)`);
  }
  const sheetRow = rowIndex + 2; // values range starts at sheet row 2
  const statusCell = encodeURIComponent("'" + cfg.trackerTab + "'" + `!A${sheetRow}`);
  const writeToken = await getGoogleUserToken(cfg.oauthFile, GOOGLE_SHEETS, fetchImpl);
  const writeHeaders = { Authorization: `Bearer ${writeToken}`, 'content-type': 'application/json' };
  const putUrl = `${SHEETS_API}/${spreadsheetId}/values/${statusCell}?valueInputOption=USER_ENTERED&includeValuesInResponse=true`;
  const putRes = await fetchImpl(putUrl, {
    method: 'PUT',
    headers: writeHeaders,
    body: JSON.stringify({ values: [['Dispatched']] }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!putRes.ok) {
    const text = await putRes.text();
    audit({ action: 'mark-dispatched', ok: false, ticket_id: options.ticket, sheet_row: sheetRow, error: `HTTP ${putRes.status}: ${text.slice(0, 200)}` });
    throw new Error(`Tracker Status update failed (HTTP ${putRes.status})`);
  }
  const putBody = await putRes.json();
  const written = putBody.updatedData?.values?.[0]?.[0];
  if (written !== 'Dispatched') {
    audit({ action: 'mark-dispatched', ok: false, ticket_id: options.ticket, sheet_row: sheetRow, error: `readback=${written}` });
    throw new Error(`Tracker Status readback mismatch: "${written}" !== "Dispatched"`);
  }
  audit({ action: 'mark-dispatched', ok: true, ticket_id: options.ticket, sheet_row: sheetRow, status: 'Dispatched' });
  return { ok: true, sheetRow, status: 'Dispatched', ticketId: options.ticket };
}

// -------------------------------------------------------------------- CLI CLI

const ALLOWED_OPTIONS = [
  'ticket', 'tech', 'start', 'end', 'duration_min', 'start_hour', 'lat', 'lon',
  'confirm_create', 'confirm_write', 'allow_dup', 'notify', 'attendee',
  'calendar', 'code', 'redirect_uri', 'scope', 'tech_map', 'now', 'confirm_sonar',
  'confirm_delete',
];

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const options = {};
  for (let index = 0; index < rest.length; index++) {
    const arg = rest[index];
    if (!arg.startsWith('--')) throw new Error(`Unexpected argument: ${arg}`);
    const [rawKey, inlineValue] = arg.slice(2).split('=', 2);
    const key = rawKey.replaceAll('-', '_');
    if (!ALLOWED_OPTIONS.includes(key)) throw new Error(`Unknown option: --${rawKey}`);
    // Space values come from `rest` (never the full argv, which still holds `--flag`).
    options[key] = inlineValue ?? rest[++index];
  }
  return { command, options };
}

async function main(argv) {
  loadProjectEnv();
  const { command, options } = parseArgs(argv);
  const cfg = loadConfig();
  if (command === 'oauth-url') {
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    // 'all' requests the union of everything in the module allowlist: use when
    // a grants-only subset swap would orphan scopes (e.g. calendar consent
    // replacing a shared Sheets token in the same owner-only file).
    const ALL_UNION = [
      GOOGLE_CALENDAR_EVENTS,
      GOOGLE_CALENDAR_READONLY,
      GOOGLE_SHEETS,
      GOOGLE_SHEETS_READONLY,
      GOOGLE_DRIVE_READONLY,
      'https://www.googleapis.com/auth/drive.file',
      'openid',
      'https://www.googleapis.com/auth/userinfo.email',
    ].join(' ');
    const scope = options.scope === 'readonly' ? GOOGLE_CALENDAR_READONLY
      : options.scope === 'write' ? GOOGLE_CALENDAR_EVENTS
      : options.scope === 'all' ? ALL_UNION
      : GOOGLE_CALENDAR;
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    console.log(buildUserConsentUrl({ clientId: secrets.clientId, redirectUri }, scope));
    console.log('No local server listens on that redirect — after Allow the browser tab will fail to connect. That is expected: copy the ?code= value from the hung tab ADDRESS BAR and run oauth-bootstrap --code with it.');
    return;
  }
  if (command === 'oauth-exchange') {
    if (!options.code) throw new Error('Missing required option: --code');
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    const body = await exchangeAuthCode(secrets, options.code, redirectUri);
    console.log(JSON.stringify({
      exchanged: true,
      scope: body.scope || null,
      note: 'Granted scope above should include both calendar scopes. Store body.refresh_token in CALENDAR_OAUTH_FILE via secure transfer; never paste it into chat.',
    }, null, 2));
    return;
  }
  if (command === 'oauth-bootstrap') {
    if (!options.code) throw new Error('Missing required option: --code');
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    const body = await exchangeAuthCode(secrets, options.code, redirectUri);
    assertSecretFileOutsideRepo(cfg.oauthFile, 'CALENDAR_OAUTH_FILE');
    storeRefreshToken(cfg.oauthFile, body.refresh_token);
    console.log(JSON.stringify({
      bootstrapped: true,
      oauth_file: '<redacted-path>',
      scope: body.scope || null,
      note: 'Refresh token stored owner-only. Delete the auth code from chat history; it is single-use.',
    }, null, 2));
    return;
  }
  if (command === 'resolve') {
    console.log(JSON.stringify(await resolveDispatch(fetch, cfg, {
      ticket: options.ticket, tech: options.tech, lat: options.lat, lon: options.lon,
      start: options.start, end: options.end, now: options.now,
      start_hour: options.start_hour, duration_min: options.duration_min,
      calendar: options.calendar, attendee: options.attendee, techMapFile: options.tech_map,
    }), null, 2));
    return;
  }
  if (command === 'create') {
    console.log(JSON.stringify(await createDispatch(fetch, cfg, {
      ticket: options.ticket, tech: options.tech, lat: options.lat, lon: options.lon,
      start: options.start, end: options.end, now: options.now,
      start_hour: options.start_hour, duration_min: options.duration_min,
      confirmCreate: options.confirm_create, allowDup: options.allow_dup === 'true',
      notify: options.notify, calendar: options.calendar, attendee: options.attendee,
      techMapFile: options.tech_map,
    }), null, 2));
    return;
  }
  if (command === 'mark-dispatched') {
    console.log(JSON.stringify(await markDispatched(fetch, cfg, {
      ticket: options.ticket, confirmWrite: options.confirm_write,
    }), null, 2));
    return;
  }
  if (command === 'delete-event') {
    console.log(JSON.stringify(await deleteDispatchEvent(fetch, cfg, {
      ticketId: options.ticket,
      calendarId: options.calendar,
      confirmDelete: options.confirm_delete,
    }), null, 2));
    return;
  }
  if (command === 'sonar-open') {
    console.log(JSON.stringify(await sonarOpenDispatch(fetch, {
      ticketId: options.ticket,
      sonarEnabled: cfg.sonarEnabled,
      confirmSonar: options.confirm_sonar,
      sonarToken: process.env.SONAR_TOKEN,
    }), null, 2));
    return;
  }
  throw new Error(`Unknown command: ${command || '(none)'}. Available: oauth-url | oauth-exchange | oauth-bootstrap | resolve | create | sonar-open | mark-dispatched`);
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  main(process.argv.slice(2)).catch((error) => {
    console.error(`schedule_dispatch: ${error.message || error}`);
    process.exit(2);
  });
}

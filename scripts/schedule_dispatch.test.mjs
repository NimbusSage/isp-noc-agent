#!/usr/bin/env node
// Offline tests for scripts/schedule_dispatch.mjs — node --test, no network.
// Mirrors scripts/tracker_sheet.test.mjs style: pure-function checks plus a
// mocked fetch flow proving gates / direct-vs-fallback / dedup / readback
// verification work without touching Sonar, Google, or the tracker sheet.
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  auditFilePath,
  buildDescription,
  buildEventBody,
  chooseTechCalendar,
  createDispatch,
  deleteDispatchEvent,
  loadTechMap,
  normalizeName,
  parseTicketCoords,
  resolveDispatch,
  resolveDispatchWindow,
  siteFromSubject,
  sonarOpenDispatch,
  techNameFromSummary,
  wallToIsoInstant,
} from './schedule_dispatch.mjs';

const sampleDescription = `Link: Bravo Feeders<br>Reason: No Power<br>|<br>NOC's Notes: Left SMS<br>|<br>TOWER1: Bravo Feeders is a Grain Leg at 120ft at 10.123, -20.456<br>Power location: <br>Site Notes: <br>|<br>NOC Tech - tech-1`;

const sampleTicket = {
  id: '100001',
  subject: 'Alphaville,  ST - Bravo Feeders: No Power',
  description: sampleDescription,
  status: 'PENDING_INTERNAL',
  priority: 'LOW',
  group: 'Field Dispatch',
  created_at: '2026-10-18T01:57:10+00:00',
  updated_at: '2026-10-18T01:57:10+00:00',
};

const SAMPLE_COORDS = { lat: '10.123', lon: '-20.456' };

// Owner-only fake secret fixture (outside the repo, mode 0600) so the shared
// google_user_auth token loader passes its safety checks offline. No network
// is ever made: the token refresh call goes through the mocked fetch.
const fixtureDir = mkdtempSync(join(tmpdir(), 'dispatch-tok-'));
const oauthFixture = join(fixtureDir, 'oauth.json');
writeFileSync(oauthFixture, JSON.stringify({ client_id: 'cid', client_secret: 'cs', refresh_token: 'rt' }));
chmodSync(oauthFixture, 0o600);
// Keep mock audit records OUT of the production audit log.
process.env.DISPATCH_AUDIT_FILE = join(fixtureDir, 'audit.jsonl');

// ---------------------------------------------------------------- coordinates

test('parseTicketCoords finds real dispatch coords in ticket text', () => {
  assert.deepEqual(parseTicketCoords(`${sampleTicket.subject}\n${sampleTicket.description}`), SAMPLE_COORDS);
});

test('parseTicketCoords rejects out-of-range junk and bare integers', () => {
  assert.equal(parseTicketCoords('site is at 10, -20 far corner'), null);
  assert.equal(parseTicketCoords('nope 200.123, -20.456'), null);
  assert.equal(parseTicketCoords('nothing here'), null);
});

// -------------------------------------------------------------- event contents

test('stripHtml flattens ticket HTML and mobile-first details are clean', async () => {
  const text = buildDescription({ ticket: sampleTicket, comments: [], coords: SAMPLE_COORDS, techName: 'Sam Rivera' });
  assert.ok(text.includes('🟢 Priority: LOW'), text.slice(0, 200));
  assert.ok(text.includes('🧰 Tech: Sam Rivera'));
  // Bare link only — no id or prefix outside the URL (operator rule).
  assert.ok(text.includes(`${SONAR_BASE}/app#/tickets/show/100001`));
  assert.ok(!/Sonar ticket #\d/.test(text));
  // Status + created-hour noise is gone; priority stays.
  assert.ok(!text.includes('PENDING_INTERNAL'));
  assert.ok(!text.includes('Created:'));
  // Sonar's standalone pipe separator lines render as blank lines.
  assert.ok(!text.split('\n').some((l) => l.trim().startsWith('|')), text);
  assert.ok(text.includes("NOC's Notes: Left SMS"));
  assert.ok(!text.includes('<br>'), 'raw <br> must not leak into the event body');
  assert.ok(!text.includes('&quot;'));
  // Site pin with coords, stripped from the subject.
  assert.ok(text.includes('📍 Bravo Feeders — 10.123, -20.456'));
});

test('buildDescription includes comments in order with truncation note', async () => {
  const comments = [
    { created_at: '2026-10-18T12:00:00+00:00', user: 'tech-1', body: 'Checked with TH, went dark at 19:20 CT' },
    { created_at: '2026-10-18T13:30:00+00:00', user: 'NOC Agent', body: 'tried bump ether, no luck' },
  ];
  const text = buildDescription({ ticket: sampleTicket, comments, commentsTruncated: true });
  const iTh = text.indexOf('Checked with TH');
  const iNoc = text.indexOf('tried bump ether');
  assert.ok(iTh >= 0 && iNoc > iTh, 'ASC order expected');
  assert.ok(text.includes('——— Latest 2 ticket updates (see the link for more) ———'));
});

test('buildDescription truncates to the calendar limit', () => {
  const huge = { ...sampleTicket, description: 'x'.repeat(50_000) };
  const text = buildDescription({ ticket: huge, comments: [] });
  assert.ok(text.length <= 8_400, `too long: ${text.length}`);
  assert.ok(text.endsWith(`——— Truncated — full ticket: ${SONAR_BASE}/app#/tickets/show/100001`));
});

test('siteFromSubject strips the town prefix from dispatch subjects', () => {
  assert.equal(siteFromSubject('Alphaville,  ST - Bravo Feeders: No Power'), 'Bravo Feeders');
  assert.equal(siteFromSubject('Delta,  ST - Delta 2 Echo Coop Fiber: Locked Up'), 'Delta 2 Echo Coop Fiber');
  assert.equal(siteFromSubject('plain subject without separators'), '');
});

test('comment times render for phone readers (CT)', () => {
  const text = buildDescription({
    ticket: sampleTicket,
    comments: [{ created_at: '2026-10-18T03:00:00+00:00', user: 'Jordan Sender', body: 'TH said breaker tripped' }],
  });
  assert.match(text, /CT — Jordan Sender\nTH said breaker tripped/);
});

test('buildEventBody maps subject→summary, coords→location, ticket key→extended props', () => {
  const win = {
    start: { dateTime: '2026-10-19T13:00:00.000Z', timeZone: 'America/Chicago' },
    end: { dateTime: '2026-10-19T14:00:00.000Z', timeZone: 'America/Chicago' },
  };
  const body = buildEventBody({
    ticket: sampleTicket,
    coords: SAMPLE_COORDS,
    description: 'detail body',
    window: win,
    techName: 'Jordan Sender',
    ticketId: '100001',
  });
  assert.equal(body.summary, sampleTicket.subject);
  assert.equal(body.location, '10.123, -20.456');
  assert.equal(body.start.timeZone, 'America/Chicago');
  assert.deepEqual(body.extendedProperties.private, {
    sonar_ticket_id: '100001',
    sonar_dispatch: 'noc-dispatch-v1',
    dispatched_tech: 'Jordan Sender',
  });
  assert.equal(body.attendees, undefined);
  const withGuest = buildEventBody({
    ticket: sampleTicket, coords: SAMPLE_COORDS, description: 'd', window: win,
    techName: 'T', ticketId: '1', attendee: 'jordan@example.test',
  });
  assert.deepEqual(withGuest.attendees, [{ email: 'jordan@example.test' }]);
});

// --------------------------------------------------------------------- window

test('wallToIsoInstant converts CT wall time to the right instant across DST', () => {
  assert.equal(wallToIsoInstant('2026-10-19T08:00', 'America/Chicago'), '2026-10-19T13:00:00.000Z');  // CDT
  assert.equal(wallToIsoInstant('2026-01-15T08:00', 'America/Chicago'), '2026-01-15T14:00:00.000Z');  // CST
  assert.throws(() => wallToIsoInstant('2026-10-19 8am', 'America/Chicago'), /Invalid/);
});

test('resolveDispatchWindow defaults to the next business weekday at 08:00 CT', () => {
  const win = (now) => resolveDispatchWindow({ now, timezone: 'America/Chicago' });
  // Sat T15:00Z (10:00 CDT) → Monday 08:00 CDT.
  assert.equal(win('2026-10-17T15:00:00Z').start.dateTime, '2026-10-19T13:00:00.000Z');
  // Monday 04:00 CDT: today's 08:00 slot is still ahead.
  assert.equal(win('2026-10-19T09:00:00Z').start.dateTime, '2026-10-19T13:00:00.000Z');
  // Monday 10:00 CDT: today's slot passed → Tuesday.
  assert.equal(win('2026-10-19T15:00:00Z').start.dateTime, '2026-10-20T13:00:00.000Z');
  const explicitStart = resolveDispatchWindow({ now: '2026-10-17T15:00:00Z', timezone: 'America/Chicago', start: '2026-10-21T14:30' });
  assert.equal(explicitStart.start.dateTime, '2026-10-21T19:30:00.000Z');
});

// ------------------------------------------------------------------- calendars

const jordanOwner = { id: 'jordan@example.test', summary: 'Jordan Sender', accessRole: 'owner' };
const techReader = { id: 'tech-team@example.test', summary: 'Tech Team', accessRole: 'reader' };
const fieldDispatchPrimary = { id: 'field.dispatch@example.test', summary: 'Field Dispatch', accessRole: 'owner', primary: true };

test('chooseTechCalendar exact name match', () => {
  const pick = chooseTechCalendar([fieldDispatchPrimary, techReader, jordanOwner], 'Jordan Sender', {});
  assert.equal(pick.calendarId, 'jordan@example.test');
  assert.equal(pick.mode, 'direct');
  assert.equal(pick.source, 'calendarList');
});

test('chooseTechCalendar matches decorated display names', () => {
  const decorated = { id: 'jordansender@example.test', summary: '*Jordan Sender | ST', accessRole: 'writer' };
  const pick = chooseTechCalendar([decorated], 'Jordan Sender', {});
  assert.equal(pick.calendarId, 'jordansender@example.test');
  assert.equal(pick.mode, 'direct');
});

test('chooseTechCalendar reader access → fallback mode', () => {
  const pick = chooseTechCalendar([techReader], 'Tech Team', {});
  assert.equal(pick.mode, 'fallback');
});

test('chooseTechCalendar never fuzzy-matches and refuses ambiguity', () => {
  assert.throws(() => chooseTechCalendar([jordanOwner], 'Jord'), /No calendar matching/);
  // Same-name calendars at the SAME access rank are genuinely ambiguous.
  assert.throws(
    () => chooseTechCalendar([
      { id: 'x1', summary: 'Jordan Sender', accessRole: 'writer' },
      { id: 'x2', summary: 'Jordan Sender', accessRole: 'writer' },
    ], 'Jordan Sender'),
    /Ambiguous calendars match/
  );
  // An owner entry outranks a same-name writer entry and wins deterministically.
  const ranked = chooseTechCalendar([
    { id: 'w1', summary: 'Jordan Sender', accessRole: 'writer' },
    { id: 'o1', summary: 'Jordan Sender', accessRole: 'owner' },
  ], 'Jordan Sender');
  assert.equal(ranked.calendarId, 'o1');
});

test('chooseTechCalendar honors the tech map override', () => {
  const mapFile = join(fixtureDir, 'tech_calendars.json');
  writeFileSync(mapFile, JSON.stringify({ 'Jordan Sender': { calendar_id: 'map-id@example.test', attendee: 'jordan@example.test' } }));
  const map = loadTechMap(mapFile); // loadTechMap normalizes display-name keys
  const pick = chooseTechCalendar([techReader], 'Jordan Sender', map);
  assert.equal(pick.calendarId, 'map-id@example.test');
  assert.equal(pick.attendee, 'jordan@example.test');
  assert.equal(pick.source, 'map');
});

test('normalizeName collapses whitespace/casing for exact matching', () => {
  assert.equal(normalizeName('   Jordan   Sender '), 'jordan sender');
  assert.equal(normalizeName('JORDAN SENDER'), 'jordan sender');
});

test('techNameFromSummary strips real-fleet decorations', () => {
  assert.equal(techNameFromSummary('*Jordan Sender | ST'), 'jordan sender');
  assert.equal(techNameFromSummary('3Tech One | ST'), 'tech one');
  assert.equal(techNameFromSummary('Tech Two - Charlie ST'), 'tech two');
  assert.equal(techNameFromSummary('Tech Three | L2 | Alpha Falls, ST'), 'tech three');
  assert.equal(techNameFromSummary('-Sam Rivera | ST'), 'sam rivera');
  assert.equal(techNameFromSummary('Jordan Sender'), 'jordan sender');
});


// ------------------------------------------------------- mocked flow (no net)

function config(overrides = {}) {
  return {
    oauthFile: oauthFixture,
    fallbackCalendarId: 'primary',
    techMapFile: '',
    writeEnabled: true,
    timezone: 'America/Chicago',
    defaultStartHour: 8,
    defaultDurationMinutes: 60,
    notify: 'none',
    trackerSpreadsheetId: 'sheet-1',
    trackerTab: 'NOC Ticket Tracker',
    trackerWriteEnabled: true,
    ...overrides,
  };
}

function jsonResponse(payload, status = 200) {
  // Our fetch path reads res.json() from the Sonar client and res.text() from
  // the calendar client — provide both.
  return { ok: status < 400, status, text: async () => JSON.stringify(payload), json: async () => payload };
}


// Profile-agnostic base: tests assert against whatever instance the helpers
// resolve on this install (site-pack profile, env, or neutral fallback).
// Top-level await is legal in this ESM test module.
const SONAR_BASE = await (await import('./schedule_dispatch.mjs')).profileBase();

function sonarStub() {
  return async (href, init = {}) => {
    href = String(href);
    if (href.startsWith(`${SONAR_BASE}/api/graphql`)) {
      const body = JSON.parse(init.body);
      if (body.query.includes('dispatchTicket')) {
        // ID-blind stubs prove nothing: only resolve the real sample ticket.
        const askedId = Number(body.variables?.id);
        return jsonResponse({
          data: {
            tickets: {
              entities: askedId === 100001 ? [{
                id: '100001',
                subject: sampleTicket.subject,
                description: sampleTicket.description,
                status: 'PENDING_INTERNAL',
                priority: 'LOW',
                created_at: '2026-10-18T01:57:10+00:00',
                updated_at: '2026-10-18T01:57:10+00:00',
                ticket_group: { id: '15', name: 'Field Dispatch' },
              }] : [],
            },
          },
        });
      }
      return jsonResponse({
        data: {
          ticket_comments: {
            entities: [{ id: '923', body: 'TH said breaker tripped', created_at: '2026-10-18T03:00:00+00:00', user: { name: 'Jordan Sender' } }],
            page_info: { total_count: 1 },
          },
        },
      });
    }
    return null;
  };
}

// Google stubs: calendarList read; events search/insert; insert on
// restrictCalendarId → 403; readback verifies the last successful insert.
function googleStub({ restrictCalendarId = null, existingDup = [] } = {}) {
  let numInserted = 0;
  const inserted = [];
  const deletedIds = new Set();
  const dups = new Set(existingDup);
  const fetchImpl = async (href, init = {}) => {
    href = String(href);
    const url = new URL(href);
    const path = url.pathname;
    if (path === '/token') {
      return jsonResponse({ access_token: 'test-access-token', expires_in: 3600 });
    }
    if (path === '/calendar/v3/users/me/calendarList') {
      return jsonResponse({ items: [fieldDispatchPrimary, jordanOwner] });
    }
    if (path.startsWith('/calendar/v3/calendars/') && path.endsWith('/events')) {
      if (init.method === 'POST') {
        // /calendar/v3/calendars/<id>/events → segment index 4 (raw URL-encoded).
        const calendarId = decodeURIComponent(path.split('/')[4]);
        const event = JSON.parse(init.body);
        if (restrictCalendarId && calendarId === restrictCalendarId) {
          return jsonResponse({ error: { message: 'not allowed' } }, 403);
        }
        numInserted += 1;
        const id = `evt${numInserted}`;
        inserted.push({ calendarId, event });
        return jsonResponse({ id, ...event, htmlLink: `https://calendar.example.test/event_${id}` });
      }
      const key = url.searchParams.get('privateExtendedProperty') || '';
      const wantId = /sonar_ticket_id=(\d+)/.exec(key)?.[1];
      if (wantId === '100001' && dups.size) {
        return jsonResponse({
          items: [...dups].map((id) => ({ id, summary: 'duplicate', htmlLink: `https://calendar.example.test/event_${id}` })),
        });
      }
      return jsonResponse({ items: [] });
    }
    if (init.method === 'DELETE' && /\/events\/[^/]+$/.test(path)) {
      deletedIds.add(decodeURIComponent(path.split('/').pop()));
      return jsonResponse({});
    }
    if (/\/events\/[^/]+$/.test(path)) {
      const id = decodeURIComponent(path.split('/').pop());
      if (deletedIds.has(id)) return jsonResponse({ error: { message: 'gone' } }, 404);
      const last = inserted[inserted.length - 1];
      return jsonResponse({ id, ...last.event, htmlLink: 'x' });
    }
    return null;
  };
  return { inserted, seedDup: (_calendarId, id) => dups.add(id), fetchImpl };
}

function combinedStub(opts = {}) {
  const sonar = sonarStub();
  const google = googleStub(opts);
  const fetchImpl = async (href, init) => {
    const result = await sonar(href, init);
    if (result) return result;
    return google.fetchImpl(href, init);
  };
  return { ...google, fetchImpl };
}

process.env.SONAR_TOKEN = process.env.SONAR_TOKEN || 'test-token';

const createOptions = (over = {}) => ({
  ticket: '100001',
  tech: 'Jordan Sender',
  confirmCreate: 'true',
  now: '2026-10-18T15:00:00Z',
  start: '2026-10-19T08:00',
  end: '2026-10-19T09:00',
  ...over,
});

test('create refuses without both write gates', async () => {
  const stub = combinedStub({});
  await assert.rejects(
    createDispatch(stub.fetchImpl, config({ writeEnabled: false }), createOptions({ confirmCreate: 'true' })),
    /CALENDAR_WRITE_ENABLED/,
  );
  const stub2 = combinedStub({});
  await assert.rejects(
    createDispatch(stub2.fetchImpl, config(), createOptions({ confirmCreate: 'false' })),
    /--confirm-create true/,
  );
});

test('create inserts DIRECT when the tech calendar is writer/owned', async () => {
  const stub = combinedStub({});
  const result = await createDispatch(stub.fetchImpl, config(), createOptions());
  assert.equal(result.mode, 'direct');
  assert.equal(result.calendarId, 'jordan@example.test');
  assert.equal(stub.inserted.length, 1);
  assert.equal(stub.inserted[0].calendarId, 'jordan@example.test');
  const event = stub.inserted[0].event;
  assert.equal(event.summary, sampleTicket.subject);
  assert.equal(event.location, '10.123, -20.456');
  assert.equal(event.attendees, undefined, 'direct events must not carry an attendee guest');
  assert.equal(event.extendedProperties.private.sonar_ticket_id, '100001');
});

test('create falls back to the fallback calendar with tech guest on 403', async () => {
  const stub = combinedStub({ restrictCalendarId: 'jordan@example.test' });
  const result = await createDispatch(
    stub.fetchImpl,
    config(),
    createOptions({ calendar: 'jordan@example.test', attendee: 'jordan@example.test' }),
  );
  assert.equal(result.mode, 'fallback');
  assert.equal(result.calendarId, 'primary');
  assert.equal(result.attendee, 'jordan@example.test');
  const fallback = stub.inserted.find((i) => i.calendarId === 'primary');
  assert.ok(fallback, 'fallback insert must happen');
  assert.equal(fallback.event.attendees?.[0]?.email, 'jordan@example.test');
});

test('create refuses when an appointment for the ticket already exists', async () => {
  const stub = combinedStub({ existingDup: ['dup1'] });
  await assert.rejects(
    createDispatch(stub.fetchImpl, config(), createOptions()),
    /already exists/,
  );
  assert.equal(stub.inserted.length, 0, 'no insert may happen while duplicates exist');
});

test('resolve produces a full plan with zero calendar writes', async () => {
  const stub = combinedStub({});
  const plan = await resolveDispatch(stub.fetchImpl, config(), createOptions());
  assert.equal(plan.mode, 'plan');
  assert.equal(plan.tech.mode, 'direct');
  assert.equal(plan.event.location, '10.123, -20.456');
  assert.match(plan.event.description, /tickets\/show\/100001/);
  assert.match(plan.event.description, /TH said breaker tripped/);
  assert.deepEqual(plan.duplicates.target, []);
  assert.equal(stub.inserted.length, 0, 'resolve must not insert anything');
});

test('create surfaces a clear error on missing coords instead of guessing', async () => {
  const stub = combinedStub({});
  await assert.rejects(
    createDispatch(
      stub.fetchImpl,
      config(),
      createOptions({ ticket: '999999', now: '2026-10-18T15:00:00Z' }),
    ),
    /No coordinates|Sonar ticket 999999 not found/,
  );
});

// --------------------------------------------------------------------- audit

test('audit log file lives under logs/ by default and honors the override', () => {
  assert.match(auditFilePath({}), /logs\/schedule_dispatch_audit\.jsonl$/);
  const temp = mkdtempSync(join(tmpdir(), 'dispatch-'));
  const overridden = auditFilePath({ DISPATCH_AUDIT_FILE: join(temp, 'audit.jsonl') });
  assert.ok(overridden.startsWith(temp));
});

test('loadTechMap tolerates a missing file', () => {
  assert.deepEqual(loadTechMap(join(fixtureDir, 'does-not-exist.json')), {});
});

// ---------------------------------------------------------------- sonar-open
// Dispatch standard (the dispatch lead): dispatched ticket flips PENDING_INTERNAL → OPEN,
// NO comment posted — comments only carry the tech's post-job notes.

function sonarOpenStub(initialStatus, { failMutation = false } = {}) {
  let mutations = 0;
  const fetchImpl = async (href, init = {}) => {
    href = String(href);
    if (href.startsWith(`${SONAR_BASE}/api/graphql`)) {
      const body = JSON.parse(init.body);
      if (body.query.includes('dispatchStatus')) {
        return jsonResponse({ data: { tickets: { entities: [{ id: '100001', status: initialStatus }] } } });
      }
      if (body.query.includes('dispatchOpen')) {
        mutations += 1;
        if (failMutation) return jsonResponse({ data: { updateTicket: { id: '100001' } } }); // status missing → must fail verify
        return jsonResponse({ data: { updateTicket: { id: '100001', status: 'OPEN' } } });
      }
      throw new Error(`sonarOpenStub: unexpected query ${body.query.slice(0, 40)}`);
    }
    return null;
  };
  return { fetchImpl, getMutations: () => mutations };
}

const openOptions = (over = {}) => ({
  ticketId: '100001',
  sonarEnabled: true,
  confirmSonar: 'true',
  ...over,
});

test('sonar-open flips PENDING_INTERNAL → OPEN and verifies the response', async () => {
  const stub = sonarOpenStub('PENDING_INTERNAL');
  const result = await sonarOpenDispatch(stub.fetchImpl, openOptions({ sonarToken: 'tok' }));
  assert.equal(result.ok, true);
  assert.equal(result.status, 'OPEN');
  assert.equal(result.alreadyOpen, false);
  assert.equal(stub.getMutations(), 1);
});

test('sonar-open refuses without both gates', async () => {
  const stub = sonarOpenStub('PENDING_INTERNAL');
  await assert.rejects(sonarOpenDispatch(stub.fetchImpl, openOptions({ confirmSonar: 'false' })), /--confirm-sonar true/);
  const stub2 = sonarOpenStub('PENDING_INTERNAL');
  await assert.rejects(sonarOpenDispatch(stub2.fetchImpl, openOptions({ sonarEnabled: false })), /DISPATCH_SONAR_ENABLED/);
  assert.equal(stub2.getMutations(), 0);
});

test('sonar-open refuses to touch anything but PENDING_INTERNAL (dispatch-lead transition only)', async () => {
  for (const status of ['CLOSED', 'PENDING_EXTERNAL', 'IN_PROGRESS']) {
    const stub = sonarOpenStub(status);
    await assert.rejects(
      sonarOpenDispatch(stub.fetchImpl, openOptions({ sonarToken: 'tok' })),
      new RegExp(`is ${status}, not PENDING_INTERNAL`),
    );
    assert.equal(stub.getMutations(), 0, `${status} must never be mutated`);
  }
});

test('sonar-open is a safe no-op when the ticket is already OPEN', async () => {
  const stub = sonarOpenStub('OPEN');
  const result = await sonarOpenDispatch(stub.fetchImpl, openOptions({ sonarToken: 'tok' }));
  assert.equal(result.alreadyOpen, true);
  assert.equal(stub.getMutations(), 0);
});

// -------------------------------------------------------------- delete-event

test('delete-event refuses without both gates and removes only the keyed event', async () => {
  const stub = googleStub({});
  await assert.rejects(
    deleteDispatchEvent(stub.fetchImpl, config(), { ticketId: '100001' }),
    /--confirm-delete true/,
  );
  await assert.rejects(
    deleteDispatchEvent(stub.fetchImpl, config({ writeEnabled: false }), { ticketId: '100001', confirmDelete: 'true' }),
    /CALENDAR_WRITE_ENABLED/,
  );
  const stub2 = googleStub({});
  // Seed an existing dup so the search finds something deletable.
  stub2.seedDup('primary', 'dup1');
  const result = await deleteDispatchEvent(stub2.fetchImpl, config(), { ticketId: '100001', confirmDelete: 'true' });
  assert.equal(result.deleted, 'dup1');
  assert.equal(result.calendarId, 'primary');
});

test('sonar-open fails closed when the mutation response does not verify', async () => {
  const stub = sonarOpenStub('PENDING_INTERNAL', { failMutation: true });
  await assert.rejects(
    sonarOpenDispatch(stub.fetchImpl, openOptions({ sonarToken: 'tok' })),
    /did not verify/,
  );
});

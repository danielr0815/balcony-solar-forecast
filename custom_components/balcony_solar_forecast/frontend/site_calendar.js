/** Calendar boundaries and elapsed-hour buckets in the HA site's timezone.
 * Calendar arithmetic uses UTC only on date-only values; instants always pass
 * through Intl. A DST day owns 23/25 distinct hourly buckets, including both
 * occurrences of a repeated clock hour.
 */
export const HOUR_MS = 60 * 60 * 1000;
export function toDate(value) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "string" && /^-?\d+$/.test(value.trim());
  const d = new Date(numeric ? Number(value) : value);
  return Number.isFinite(d.getTime()) ? d : null;
}

export class SiteCalendar {
  constructor(timeZone) {
    this.timeZone = timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    this.formatter = new Intl.DateTimeFormat("en-GB", {
      timeZone: this.timeZone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    this.midnights = new Map();
  }
  parts(value) {
    return Object.fromEntries(this.formatter.formatToParts(value).map((p) => [p.type, p.value]));
  }
  key(value = new Date()) {
    const d = toDate(value);
    if (!d) return "";
    const p = this.parts(d);
    return `${p.year}-${p.month}-${p.day}`;
  }
  shift(iso, offset) {
    const d = new Date(`${iso}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() + offset);
    return d.toISOString().slice(0, 10);
  }
  start(iso) {
    if (this.midnights.has(iso)) return new Date(this.midnights.get(iso));
    // Search the first instant in the calendar day. This also handles zones
    // whose summer-time transition skips midnight, without assuming a 24h day.
    const noon = Date.parse(`${iso}T12:00:00Z`);
    let lo = noon - 36 * HOUR_MS, hi = noon + 36 * HOUR_MS;
    while (hi - lo > 1) {
      const mid = Math.floor((lo + hi) / 2);
      if (this.key(new Date(mid)) < iso) lo = mid;
      else hi = mid;
    }
    this.midnights.set(iso, hi);
    return new Date(hi);
  }
  dayAt(offset) { return this.start(this.shift(this.key(), offset)); }
  hours(start) {
    const end = this.start(this.shift(this.key(start), 1));
    const result = [];
    for (let ms = start.getTime(); ms < end.getTime(); ms += HOUR_MS) result.push(new Date(ms));
    return result;
  }
  hourIndex(value, start) {
    const d = toDate(value);
    if (!d || this.key(d) !== this.key(start)) return -1;
    return Math.floor((d - start) / HOUR_MS);
  }
  hourLabel(value) {
    const p = this.parts(value);
    // Offset distinguishes the repeated autumn hour in details and tables.
    const offset = new Intl.DateTimeFormat("en", { timeZone: this.timeZone,
      timeZoneName: "shortOffset" }).formatToParts(value).find((p) => p.type === "timeZoneName").value;
    return `${p.hour}:${p.minute} ${offset}`;
  }
  shortDate(value, withYear = false) {
    const p = this.parts(value);
    return `${Number(p.day)}.${Number(p.month)}.${withYear ? p.year : ""}`;
  }
  weekday(value) { return new Date(`${this.key(value)}T12:00:00Z`).getUTCDay(); }
}

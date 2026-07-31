"use strict";

(function exposeAvailabilityModel(root, factory) {
  const AvailabilityModel = factory();
  if (typeof module === "object" && module.exports) module.exports = AvailabilityModel;
  root.AvailabilityModel = AvailabilityModel;
})(typeof globalThis === "undefined" ? this : globalThis, function createAvailabilityModel() {
  const MAX_STAY_NIGHTS = 90;
  const DEFAULT_DATE_PAGE_SIZE = 14;
  const MAX_STAY_OPTIONS_PER_DATE = 3;

  function addIsoDays(value, days) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const amount = Number(days);
    if (!Number.isSafeInteger(amount) || Math.abs(amount) > 366) return null;
    const date = new Date(`${value}T00:00:00Z`);
    if (Number.isNaN(date.getTime())) return null;
    if (date.toISOString().slice(0, 10) !== value) return null;
    date.setUTCDate(date.getUTCDate() + amount);
    if (Number.isNaN(date.getTime())) return null;
    try {
      return date.toISOString().slice(0, 10);
    } catch (_error) {
      return null;
    }
  }

  function normalizeStayNights(value) {
    if (value == null || String(value).trim() === "") return null;
    const text = String(value).trim();
    if (!/^\d+$/.test(text)) return null;
    const nights = Number(text);
    return Number.isSafeInteger(nights) && nights >= 1 && nights <= MAX_STAY_NIGHTS
      ? nights
      : null;
  }

  function stayInputIsValid(value) {
    return value == null || String(value).trim() === "" || normalizeStayNights(value) !== null;
  }

  function lastCheckInDate(lastCheckedNight, nights) {
    const normalized = normalizeStayNights(nights);
    if (!normalized) {
      return nights == null || String(nights).trim() === ""
        ? lastCheckedNight || null
        : null;
    }
    return addIsoDays(lastCheckedNight, -(normalized - 1));
  }

  function plural(count, singular, pluralForm = `${singular}s`) {
    return Number(count) === 1 ? singular : pluralForm;
  }

  return {
    MAX_STAY_NIGHTS,
    DEFAULT_DATE_PAGE_SIZE,
    MAX_STAY_OPTIONS_PER_DATE,
    addIsoDays,
    normalizeStayNights,
    stayInputIsValid,
    lastCheckInDate,
    plural,
  };
});

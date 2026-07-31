"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const model = require("./availability_model.js");

test("stay lengths are whole nights within the checked-window bound", () => {
  assert.equal(model.normalizeStayNights(""), null);
  assert.equal(model.normalizeStayNights("2"), 2);
  for (const value of ["0", "1.5", "91", "1000000000", "not-a-number"]) {
    assert.equal(model.normalizeStayNights(value), null);
    assert.equal(model.stayInputIsValid(value), false);
  }
  assert.equal(model.stayInputIsValid(""), true);
});

test("bounded ISO date arithmetic fails closed", () => {
  assert.equal(model.addIsoDays("2026-08-01", 2), "2026-08-03");
  assert.equal(model.addIsoDays("bad", 2), null);
  assert.equal(model.addIsoDays("2026-02-31", 2), null);
  assert.equal(model.addIsoDays("2026-08-01", 1000000000), null);
});

test("the final check-in accounts for every selected night", () => {
  assert.equal(model.lastCheckInDate("2026-10-28", 1), "2026-10-28");
  assert.equal(model.lastCheckInDate("2026-10-28", 2), "2026-10-27");
  assert.equal(model.lastCheckInDate("2026-10-28", 10), "2026-10-19");
  assert.equal(model.lastCheckInDate("2026-10-28", 91), null);
});

test("plural chooses readable singular and plural labels", () => {
  assert.equal(model.plural(1, "site"), "site");
  assert.equal(model.plural(2, "site"), "sites");
  assert.equal(model.plural(0, "campground"), "campgrounds");
});

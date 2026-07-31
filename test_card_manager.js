"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {CardManager} = require("./card_manager.js");

function open(manager, key, left, top) {
  return manager.open({key, position: {left, top}, size: {width: 200, height: 120}});
}

test("opening another marker closes only unpinned cards and never changes pin state", () => {
  const manager = new CardManager();
  open(manager, "a", 10, 20);
  assert.equal(manager.get("a").pinned, false);

  open(manager, "b", 240, 20);
  assert.equal(manager.get("a"), null);
  assert.equal(manager.get("b").pinned, false);

  manager.setPinned("b", true);
  open(manager, "c", 470, 20);
  assert.equal(manager.get("b").pinned, true);
  assert.equal(manager.get("c").pinned, false);
});

test("reopening an existing card preserves its position and pin state while foregrounding it", () => {
  const manager = new CardManager();
  open(manager, "a", 10, 20);
  manager.setPinned("a", true);
  open(manager, "b", 240, 20);
  const before = manager.get("a");

  open(manager, "a", 900, 900);
  const after = manager.get("a");
  assert.deepEqual(after.position, {left: 10, top: 20});
  assert.equal(after.pinned, true);
  assert.ok(after.zIndex > before.zIndex);
  assert.equal(manager.focusedId, "a");
});

test("ordinary dismissal leaves pinned cards open, while explicit close and Escape may close them", () => {
  const manager = new CardManager();
  open(manager, "a", 10, 20);
  manager.setPinned("a", true);

  assert.deepEqual(manager.dismissUnpinned(), []);
  assert.ok(manager.get("a"));
  assert.equal(manager.close("a"), false);
  assert.ok(manager.get("a"));
  assert.equal(manager.close("a", {explicit: true}), true);

  open(manager, "b", 10, 20);
  manager.setPinned("b", true);
  assert.deepEqual(manager.handleKey("Escape"), {kind: "close", cardId: "b"});
  assert.equal(manager.get("b"), null);
});

test("pinning a sixth card automatically unpins the oldest pinned card", () => {
  const manager = new CardManager({maxPinned: 5});
  for (const [index, key] of ["a", "b", "c", "d", "e", "f"].entries()) {
    open(manager, key, index * 220, 20);
    manager.setPinned(key, true);
  }

  assert.equal(manager.get("a").pinned, false);
  assert.deepEqual(manager.pinnedKeys(), ["b", "c", "d", "e", "f"]);
});

test("card keyboard navigation selects the nearest open card in each arrow direction", () => {
  const manager = new CardManager();
  open(manager, "west", 0, 110);
  manager.setPinned("west", true);
  open(manager, "north", 220, 0);
  manager.setPinned("north", true);
  open(manager, "center", 220, 220);
  manager.setPinned("center", true);
  open(manager, "east", 500, 225);
  manager.setPinned("east", true);
  open(manager, "south", 220, 500);
  manager.focus("center");

  assert.deepEqual(manager.handleKey("ArrowLeft"), {kind: "focus", cardId: "west"});
  manager.focus("center");
  assert.deepEqual(manager.handleKey("ArrowUp"), {kind: "focus", cardId: "north"});
  manager.focus("center");
  assert.deepEqual(manager.handleKey("ArrowRight"), {kind: "focus", cardId: "east"});
  manager.focus("center");
  assert.deepEqual(manager.handleKey("ArrowDown"), {kind: "focus", cardId: "south"});
});

test("Page Up and Page Down target the focused card; all other keys remain map shortcuts", () => {
  const manager = new CardManager();
  open(manager, "a", 10, 20);

  assert.deepEqual(manager.handleKey("PageUp"), {kind: "scroll", cardId: "a", direction: -1});
  assert.deepEqual(manager.handleKey("PageDown"), {kind: "scroll", cardId: "a", direction: 1});
  assert.deepEqual(manager.handleKey("+", {metaKey: true}), {kind: "map"});
  assert.deepEqual(manager.handleKey("Home"), {kind: "map"});
});

test("question mark opens the Gmail-style keyboard-shortcuts overlay from an active card", () => {
  const manager = new CardManager();
  open(manager, "a", 10, 20);

  assert.deepEqual(manager.handleKey("?"), {kind: "shortcuts"});
});

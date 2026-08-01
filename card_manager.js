"use strict";

(function exposeCardManager(root, factory) {
  const CardManager = factory();
  if (typeof module === "object" && module.exports) module.exports = {CardManager};
  root.CardManager = CardManager;
})(typeof globalThis === "undefined" ? this : globalThis, function createCardManager() {
  class CardManager {
    constructor({maxPinned = 5} = {}) {
      this.maxPinned = maxPinned;
      this.cards = new Map();
      this.focusedId = null;
      this.clock = 0;
    }

    get(key) {
      const card = this.cards.get(key);
      return card ? this.copy(card) : null;
    }

    all() {
      return [...this.cards.values()].map(card => this.copy(card));
    }

    open({key, position, size}) {
      const existing = this.cards.get(key);
      if (existing) {
        this.focus(key);
        return {card: this.get(key), opened: false, closedKeys: []};
      }
      const closedKeys = this.dismissUnpinned();
      const card = {
        key,
        pinned: false,
        pinnedAt: null,
        position: this.copyPosition(position),
        size: this.copySize(size),
        zIndex: 0,
      };
      this.cards.set(key, card);
      this.focus(key);
      return {card: this.get(key), opened: true, closedKeys};
    }

    close(key, {explicit = false} = {}) {
      const card = this.cards.get(key);
      if (!card || (card.pinned && !explicit)) return false;
      this.cards.delete(key);
      if (this.focusedId === key) this.focusedId = this.topmostKey();
      return true;
    }

    dismissUnpinned({exceptKey = null} = {}) {
      const closedKeys = [];
      this.cards.forEach((card, key) => {
        if (!card.pinned && key !== exceptKey) {
          this.cards.delete(key);
          closedKeys.push(key);
        }
      });
      if (closedKeys.includes(this.focusedId)) this.focusedId = this.topmostKey();
      return closedKeys;
    }

    setPinned(key, pinned) {
      const card = this.cards.get(key);
      if (!card) return [];
      if (card.pinned === pinned) return [];
      card.pinned = pinned;
      card.pinnedAt = pinned ? this.nextTick() : null;
      if (!pinned) return [];
      const unpinnedKeys = [];
      while (this.pinnedKeys().length > this.maxPinned) {
        const oldest = this.pinnedCards()[0];
        oldest.pinned = false;
        oldest.pinnedAt = null;
        unpinnedKeys.push(oldest.key);
      }
      return unpinnedKeys;
    }

    updateLayout(key, {position, size}) {
      const card = this.cards.get(key);
      if (!card) return;
      if (position) card.position = this.copyPosition(position);
      if (size) card.size = this.copySize(size);
    }

    focus(key) {
      const card = this.cards.get(key);
      if (!card) return false;
      card.zIndex = this.nextTick();
      this.focusedId = key;
      return true;
    }

    blur() {
      this.focusedId = null;
    }

    pinnedKeys() {
      return this.pinnedCards().map(card => card.key);
    }

    handleKey(key) {
      if (!this.focusedId) return {kind: "map"};
      if (key === "?") return {kind: "shortcuts"};
      if (key === "Escape") {
        const cardId = this.focusedId;
        if (this.close(cardId, {explicit: true})) return {kind: "close", cardId};
        return {kind: "map"};
      }
      if (key === "PageUp") return {kind: "scroll", cardId: this.focusedId, direction: -1};
      if (key === "PageDown") return {kind: "scroll", cardId: this.focusedId, direction: 1};
      const direction = {
        ArrowLeft: "left",
        ArrowRight: "right",
        ArrowUp: "up",
        ArrowDown: "down",
      }[key];
      if (!direction) return {kind: "map"};
      const cardId = this.focusNearest(direction);
      return cardId ? {kind: "focus", cardId} : {kind: "map"};
    }

    focusNearest(direction) {
      const current = this.cards.get(this.focusedId);
      if (!current) return null;
      const origin = this.center(current);
      const candidates = this.all()
        .filter(card => card.key !== current.key)
        .filter(card => this.isInDirection(origin, this.center(card), direction))
        .map(card => ({card, distance: this.distance(origin, this.center(card))}))
        .sort((left, right) => left.distance - right.distance || right.card.zIndex - left.card.zIndex);
      const nearest = candidates[0]?.card;
      if (!nearest) return null;
      this.focus(nearest.key);
      return nearest.key;
    }

    pinnedCards() {
      return [...this.cards.values()]
        .filter(card => card.pinned)
        .sort((left, right) => left.pinnedAt - right.pinnedAt);
    }

    topmostKey() {
      return [...this.cards.values()]
        .sort((left, right) => right.zIndex - left.zIndex)[0]?.key || null;
    }

    nextTick() {
      this.clock += 1;
      return this.clock;
    }

    center(card) {
      return {
        x: card.position.left + card.size.width / 2,
        y: card.position.top + card.size.height / 2,
      };
    }

    distance(first, second) {
      return Math.hypot(second.x - first.x, second.y - first.y);
    }

    isInDirection(origin, target, direction) {
      if (direction === "left") return target.x < origin.x;
      if (direction === "right") return target.x > origin.x;
      if (direction === "up") return target.y < origin.y;
      return target.y > origin.y;
    }

    copy(card) {
      return {
        ...card,
        position: this.copyPosition(card.position),
        size: this.copySize(card.size),
      };
    }

    copyPosition(position = {}) {
      return {left: Number(position.left) || 0, top: Number(position.top) || 0};
    }

    copySize(size = {}) {
      return {width: Number(size.width) || 0, height: Number(size.height) || 0};
    }
  }

  return CardManager;
});

// The overlay's window manager: where a drop snaps, and that panels never
// overlap while there is room (web/src/shell.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { insetsFrom, place, snap } from "../src/shell.js";

test("a drop snaps to the nearest edge, keeping its place along it", () => {
  assert.deepEqual(snap({ left: 20, top: 300, width: 200, height: 100 }, 1000, 800),
                   { edge: "left", offset: 300 });
  assert.deepEqual(snap({ left: 500, top: 690, width: 200, height: 100 }, 1000, 800),
                   { edge: "bottom", offset: 500 });
  assert.deepEqual(snap({ left: 790, top: 40, width: 200, height: 300 }, 1000, 800),
                   { edge: "right", offset: 40 });
  assert.equal(snap({ left: -50, top: -30, width: 200, height: 100 }, 1000, 800).offset, 8,
               "an offset is never off the canvas");
});

test("right and bottom edges are measured from their own side", () => {
  const xy = place({ a: { edge: "right", offset: 50 }, b: { edge: "bottom", offset: 100 } },
                   { a: { w: 300, h: 200 }, b: { w: 200, h: 100 } }, 1000, 800);
  assert.deepEqual(xy.a, { left: 1000 - 300 - 8, top: 50 });
  assert.deepEqual(xy.b, { left: 100, top: 800 - 100 - 8 });
});

test("panels on one edge are pushed clear of each other", () => {
  const xy = place({ a: { edge: "left", offset: 10 }, b: { edge: "left", offset: 50 } },
                   { a: { w: 300, h: 200 }, b: { w: 300, h: 200 } }, 1000, 800);
  assert.equal(xy.b.top, xy.a.top + 200 + 8);
});

test("a side tab clears a panel in the corner (the Choose tab under Data)", () => {
  const xy = place({ data: { edge: "top", offset: 8 }, tab: { edge: "left", offset: 140 } },
                   { data: { w: 300, h: 160 }, tab: { w: 24, h: 70 } }, 1000, 800);
  assert.ok(xy.tab.top >= xy.data.top + 160, `tab at ${xy.tab.top} under the Data panel`);
});

test("QGIS's message bar across the top pushes the top edge down", () => {
  assert.deepEqual(insetsFrom([{ x: 0, y: 0, w: 1000, h: 42 }], 1000, 800),
                   { top: 42, right: 0, bottom: 0, left: 0 });
  assert.deepEqual(insetsFrom([{ x: 300, y: 300, w: 50, h: 50 }], 1000, 800),
                   { top: 0, right: 0, bottom: 0, left: 0 },
                   "something in the middle is no edge's business");
  assert.deepEqual(insetsFrom([], 1000, 800), { top: 0, right: 0, bottom: 0, left: 0 });
});

test("with no room left a panel overlaps rather than leaving the canvas", () => {
  const xy = place({ a: { edge: "left", offset: 8 }, b: { edge: "left", offset: 8 } },
                   { a: { w: 300, h: 500 }, b: { w: 300, h: 500 } }, 1000, 800);
  assert.ok(xy.b.top + 500 <= 800, "inside the viewport");
});

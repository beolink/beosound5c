/**
 * Tests for the PLAYING backdrop carousel (web/js/backdrop.js).
 *
 * The carousel is what the free wheel and the remote's FORMAT key step
 * through: Auto, the bare artwork, a Spotify canvas, a music video, a CD's
 * track list, and the visualizer's four modes.
 *
 * Two properties are worth guarding, because getting either wrong produces
 * a control that feels haunted rather than broken:
 *
 *   1. The list is built from what exists right now. A track with no canvas
 *      has no Canvas entry, a device with beo-visualizer stopped has no
 *      meters. Stepping must never land somewhere that would draw nothing.
 *
 *   2. The choice is held by name, not by position. Keyed on position, a
 *      track without a canvas would shift you one along on the way past —
 *      pick MilkDrop, get Lake, with nothing touched.
 *
 * Run with: node --test tests/unit/js/test_backdrop_carousel.js
 */

const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

// ── Minimal DOM stub ──────────────────────────────────────────────────────
// backdrop.js only ever creates the label element and looks up the PLAYING
// container, so a real DOM would be far more machinery than the carousel
// logic under test needs. The stubs are read at call time, so each test can
// change which services exist.

function fakeEl() {
    const classes = new Set();
    return {
        textContent: '',
        className: '',
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
        appendChild(child) { return child; },
    };
}

global.window = {};
global.document = {
    readyState: 'complete',
    body: fakeEl(),
    createElement: fakeEl,
    getElementById: () => null,          // no PLAYING container in a test
    addEventListener() {},
};
global.performance = { now: () => Date.now() };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });

const backdrop = require('../../../web/js/backdrop.js');

function withServices({ canvas = false, video = false, cd = false, visualizer = false } = {}) {
    global.window.CanvasPanel = {
        hasCanvas: canvas || video,
        hasMusicVideo: video,
        setSuppressed() {},
        show() {},
        hide() {},
    };
    global.window.SourcePresets = cd
        ? { cd: { controller: { setFlipped() {}, canFlip: true } } }
        : {};
    global.window.BeoVisualizer = visualizer
        ? { mount() {}, unmount() {}, setMode() {}, mode: 'vu' }
        : undefined;
}

const ids = () => backdrop.available().map(e => e.id);

beforeEach(() => {
    withServices({});
    backdrop._setChosen('auto');
});

describe('what the carousel offers', () => {
    it('always offers Auto and the artwork', () => {
        assert.deepEqual(ids(), ['auto', 'artwork']);
    });

    it('offers Canvas only when the track has one', () => {
        assert.ok(!ids().includes('canvas'));
        withServices({ canvas: true });
        assert.ok(ids().includes('canvas'));
    });

    it('offers the video only when there is one', () => {
        withServices({ canvas: true });
        assert.ok(!ids().includes('video'));
        withServices({ video: true });
        assert.ok(ids().includes('video'));
    });

    it('offers the track list only on a CD with a back to show', () => {
        assert.ok(!ids().includes('tracks'));
        withServices({ cd: true });
        assert.ok(ids().includes('tracks'));
    });

    it('offers no meters without the visualizer', () => {
        assert.deepEqual(ids().filter(i => ['vu', 'lake', 'lake_blue', 'milkdrop'].includes(i)), []);
        withServices({ visualizer: true });
        assert.deepEqual(ids().filter(i => ['vu', 'lake', 'lake_blue', 'milkdrop'].includes(i)),
                         ['vu', 'lake', 'lake_blue', 'milkdrop']);
    });
});

describe('stepping', () => {
    it('walks only what is available', () => {
        withServices({ visualizer: true });      // no canvas, no video, no CD
        const seen = [];
        for (let i = 0; i < 6; i++) { backdrop.step(1); seen.push(backdrop.chosen); }
        assert.deepEqual(seen, ['artwork', 'vu', 'lake', 'lake_blue', 'milkdrop', 'auto']);
        assert.ok(!seen.includes('canvas'), 'stepped onto a backdrop that would draw nothing');
    });

    it('wraps in both directions', () => {
        backdrop._setChosen('auto');
        backdrop.step(-1);
        assert.equal(backdrop.chosen, 'artwork');   // two entries, so back = the other
        backdrop.step(1);
        assert.equal(backdrop.chosen, 'auto');
    });

    it('does nothing it cannot do when the list is one long', () => {
        // Availability can collapse mid-track; stepping must still be safe.
        withServices({});
        backdrop._setChosen('auto');
        backdrop.step(1);
        assert.ok(ids().includes(backdrop.chosen));
    });
});

describe('remembering the choice', () => {
    it('keeps the name when a neighbour disappears', () => {
        withServices({ visualizer: true, canvas: true });
        backdrop._setChosen('milkdrop');
        // Next track has no canvas: the entry before the meters vanishes.
        withServices({ visualizer: true });
        assert.equal(backdrop.chosen, 'milkdrop',
                     'the choice shifted when the list shortened');
    });

    it('takes a value pushed from another client', () => {
        withServices({ visualizer: true });
        backdrop.onBackdrop({ backdrop: 'lake_blue' });
        assert.equal(backdrop.chosen, 'lake_blue');
    });

    it('ignores a broadcast that carries nothing', () => {
        backdrop._setChosen('artwork');
        backdrop.onBackdrop({});
        backdrop.onBackdrop(null);
        assert.equal(backdrop.chosen, 'artwork');
    });
});

describe('the wheel', () => {
    it('needs real travel before it steps', () => {
        withServices({ visualizer: true });
        backdrop._setChosen('auto');
        backdrop.handleNav({ direction: 'clock', speed: 1 });
        assert.equal(backdrop.chosen, 'auto', 'a nudge moved the carousel');
        for (let i = 0; i < 4; i++) backdrop.handleNav({ direction: 'clock', speed: 10 });
        assert.notEqual(backdrop.chosen, 'auto', 'a full turn did not move it');
    });

    it('always consumes the event, so the wheel never falls through', () => {
        assert.equal(backdrop.handleNav({ direction: 'clock', speed: 10 }), true);
    });
});

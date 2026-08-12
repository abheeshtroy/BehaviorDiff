import { describe, expect, it } from "vitest";
import { AMPLITUDE, DEFAULT_BAND, displacementMapPixels, edgeFalloff, NEUTRAL } from "./refraction";

/** Reads one pixel out of the flat RGBA buffer. */
const at = (pixels, size, x, y) => {
  const i = (y * size + x) * 4;
  return { r: pixels[i], g: pixels[i + 1], b: pixels[i + 2], a: pixels[i + 3] };
};

describe("edgeFalloff", () => {
  it("leaves the interior of the pane flat", () => {
    expect(edgeFalloff(0, 0.2)).toBe(0);
    expect(edgeFalloff(0.5, 0.2)).toBe(0);
    // The band starts at 1 - band, and starts from zero.
    expect(edgeFalloff(0.8, 0.2)).toBeCloseTo(0, 10);
    expect(edgeFalloff(0.79, 0.2)).toBe(0);
  });

  it("reaches full strength at the edge", () => {
    expect(edgeFalloff(1, 0.2)).toBe(1);
  });

  it("ramps monotonically across the band", () => {
    const samples = [0.82, 0.85, 0.9, 0.95, 1].map((t) => edgeFalloff(t, 0.2));
    for (let i = 1; i < samples.length; i++) {
      expect(samples[i]).toBeGreaterThan(samples[i - 1]);
    }
  });

  it("is weighted toward the rim rather than linear, so the bevel has no crease", () => {
    // Halfway through the band a linear ramp would read 0.5.
    expect(edgeFalloff(0.9, 0.2)).toBeCloseTo(0.25, 10);
  });

  it("clamps outside the pane instead of running away", () => {
    expect(edgeFalloff(1.5, 0.2)).toBe(1);
    expect(edgeFalloff(-1, 0.2)).toBe(0);
  });

  it("bends nothing when there is no band", () => {
    expect(edgeFalloff(1, 0)).toBe(0);
  });
});

describe("displacementMapPixels", () => {
  const size = 65; // odd, so one row and column land exactly on the centre
  const pixels = displacementMapPixels({ size, band: DEFAULT_BAND });
  const mid = (size - 1) / 2;

  it("fills a full RGBA buffer", () => {
    expect(pixels).toHaveLength(size * size * 4);
  });

  it("is neutral at the centre, so the middle of a pane is undistorted", () => {
    expect(at(pixels, size, mid, mid)).toEqual({ r: NEUTRAL, g: NEUTRAL, b: NEUTRAL, a: 255 });
  });

  it("keeps the whole interior neutral", () => {
    const inside = Math.round(mid * 0.5);
    for (const [x, y] of [[mid, inside], [inside, mid], [inside, inside]]) {
      const { r, g } = at(pixels, size, x, y);
      expect({ r, g }).toEqual({ r: NEUTRAL, g: NEUTRAL });
    }
  });

  it("pushes the horizontal offset into red and the vertical into green", () => {
    // Mid-height, extreme left/right: red swings, green stays put.
    expect(at(pixels, size, 0, mid)).toEqual({ r: NEUTRAL - AMPLITUDE, g: NEUTRAL, b: NEUTRAL, a: 255 });
    expect(at(pixels, size, size - 1, mid)).toEqual({ r: NEUTRAL + AMPLITUDE, g: NEUTRAL, b: NEUTRAL, a: 255 });
    // Mid-width, extreme top/bottom: green swings, red stays put.
    expect(at(pixels, size, mid, 0)).toEqual({ r: NEUTRAL, g: NEUTRAL - AMPLITUDE, b: NEUTRAL, a: 255 });
    expect(at(pixels, size, mid, size - 1)).toEqual({ r: NEUTRAL, g: NEUTRAL + AMPLITUDE, b: NEUTRAL, a: 255 });
  });

  it("bends outward symmetrically, so neither side of a pane is favoured", () => {
    for (let x = 0; x < size; x++) {
      const left = at(pixels, size, x, mid).r;
      const right = at(pixels, size, size - 1 - x, mid).r;
      // Mirrored columns straddle neutral by the same amount in opposite directions.
      expect(left + right).toBe(2 * NEUTRAL);
    }
  });

  it("bends diagonally in the corners, where both edges meet", () => {
    const corner = at(pixels, size, size - 1, size - 1);
    expect(corner.r).toBe(NEUTRAL + AMPLITUDE);
    expect(corner.g).toBe(NEUTRAL + AMPLITUDE);
  });

  it("stays inside the byte range at every pixel", () => {
    for (let i = 0; i < pixels.length; i += 4) {
      expect(pixels[i]).toBeGreaterThanOrEqual(1);
      expect(pixels[i]).toBeLessThanOrEqual(255);
      expect(pixels[i + 3]).toBe(255);
    }
  });

  it("is fully neutral with no band, so a zero-strength map is a no-op", () => {
    const flat = displacementMapPixels({ size: 8, band: 0 });
    for (let i = 0; i < flat.length; i += 4) {
      expect([flat[i], flat[i + 1], flat[i + 2]]).toEqual([NEUTRAL, NEUTRAL, NEUTRAL]);
    }
  });

  it("rejects a size too small to have an interior", () => {
    expect(() => displacementMapPixels({ size: 1 })).toThrow(/integer >= 2/);
    expect(() => displacementMapPixels({ size: 12.5 })).toThrow(/integer >= 2/);
  });
});

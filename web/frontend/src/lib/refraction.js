/**
 * Displacement map for the liquid-glass refraction filter.
 *
 * `feDisplacementMap` reads two channels of a map image as a vector field: it
 * moves each output pixel to `x + scale * (R/255 - 0.5)`, `y + scale *
 * (G/255 - 0.5)`. So mid-grey means "leave this pixel alone", and how far a
 * channel sits from mid-grey is how far the backdrop bends there.
 *
 * The map we want is a convex pane: optically flat through the middle, bending
 * only in a band along the rim. That makes the interior of a card perfectly
 * readable and puts the distortion where a real glass edge would put it.
 *
 * The rim bends *inward* — the right edge samples from further left, the top
 * edge from further down. That is a hard constraint, not a taste call. As a
 * backdrop filter the source image is the backdrop snapshot of the pane's own
 * box, and there is nothing outside it: an outward-bending rim samples past
 * that edge, comes back transparent, and erases the backdrop in exactly the
 * band that was supposed to show the bend. Pulling inward samples real
 * content, so a line crossing the boundary visibly jumps and hooks instead of
 * dying at the edge.
 *
 * Kept as plain arithmetic, separate from the canvas that paints it, so the
 * profile is testable without a DOM.
 */

/** Mid-grey. `feDisplacementMap` treats this as zero offset. */
export const NEUTRAL = 128;

/**
 * Half the channel range, so the ramp reaches 1 and 255 without clipping.
 * A full-strength edge therefore displaces by `scale / 2` pixels.
 *
 * This is a ceiling, not a dial: the map is bytes, so 127 is already the
 * furthest a channel can sit from mid-grey. Raising it only clips the ramp
 * into a plateau and leaves the peak bend exactly where it was. How hard the
 * glass actually bends is the filter's `scale` — see GlassFilters.jsx.
 */
export const AMPLITUDE = 127;

/**
 * How far in from each edge the bend starts, as a fraction of the distance
 * from the centre to that edge. 0.35 leaves the middle ~65% of the pane flat.
 *
 * Wide enough that the bent rim is a band you can read the distortion in
 * rather than a hairline at the very edge, and still leaving the interior —
 * where the text sits — optically flat.
 */
export const DEFAULT_BAND = 0.35;

/**
 * Bend strength along one axis at normalised distance `t` from the centre
 * (0 at the centre, 1 at the edge).
 *
 * Flat 0 until the band starts, then a squared ramp to 1 at the very edge.
 * Squared rather than linear for two reasons: it meets the flat interior with
 * zero slope, so there is no visible crease where the bevel begins, and it
 * concentrates the bend at the rim the way a curved surface does — the
 * steeper the surface tilts, the further the light walks sideways.
 */
export function edgeFalloff(t, band = DEFAULT_BAND) {
  if (!(band > 0)) return 0;
  const clamped = Math.min(Math.max(t, 0), 1);
  // Measured back from the edge rather than forward from the band's start, so
  // both ends land on exactly 0 and exactly 1 instead of a float's width away.
  const k = 1 - (1 - clamped) / band;
  if (k <= 0) return 0;
  return k * k;
}

/**
 * RGBA bytes for a `size` x `size` displacement map, row-major, ready for
 * `ImageData`. Red carries the horizontal offset, green the vertical; blue is
 * unused and alpha is opaque.
 *
 * The map is stretched to each pane's box by the filter, so the band is a
 * fraction of the pane rather than a fixed number of pixels — one map serves
 * every size of card and button.
 */
export function displacementMapPixels({ size = 256, band = DEFAULT_BAND } = {}) {
  if (!Number.isInteger(size) || size < 2) {
    throw new Error(`displacement map size must be an integer >= 2, got ${size}`);
  }
  const pixels = new Uint8ClampedArray(size * size * 4);
  const span = size - 1;

  for (let y = 0; y < size; y++) {
    // -1 at the top edge, 0 through the middle, 1 at the bottom edge.
    const v = (y * 2) / span - 1;
    const dy = Math.sign(v) * edgeFalloff(Math.abs(v), band);
    for (let x = 0; x < size; x++) {
      const u = (x * 2) / span - 1;
      const dx = Math.sign(u) * edgeFalloff(Math.abs(u), band);
      const i = (y * size + x) * 4;
      // Negated so the rim pulls inward; see the note at the top of the file.
      pixels[i] = NEUTRAL - dx * AMPLITUDE;
      pixels[i + 1] = NEUTRAL - dy * AMPLITUDE;
      pixels[i + 2] = NEUTRAL;
      pixels[i + 3] = 255;
    }
  }
  return pixels;
}

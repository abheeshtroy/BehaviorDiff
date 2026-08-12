import { useMemo } from "react";
import { DEFAULT_BAND, displacementMapPixels } from "../lib/refraction";

/**
 * The id `.liquid-glass` reaches for in `backdrop-filter: url(...)`. Fixed
 * rather than per-instance (unlike Grain's ids) because CSS has to name it, so
 * this component is mounted exactly once, at the app root.
 */
export const REFRACTION_FILTER_ID = "liquid-glass-refraction";

/**
 * Peak sideways walk of the backdrop, in CSS pixels. `feDisplacementMap`
 * spends half its `scale` in each direction, so the rim of a pane pulls the
 * background about `SCALE / 2` px.
 *
 * This is the only real strength dial. The map's own amplitude is pinned at
 * the byte ceiling (lib/refraction.js), so bending harder means moving further
 * per unit of map, not writing a bolder map. 115 puts the rim's peak walk at
 * ~57px: enough that a straight line crossing behind a card — a god ray, an
 * edge — visibly bows where it enters the glass, which is the whole point of
 * refracting rather than only blurring. Past ~130 the rim oversamples its own
 * neighbourhood and the bend smears into a blob instead of reading as a line.
 */
const SCALE = 115;

/** Resolution of the generated map. Stretched to each pane, so this is only
 *  how finely the ramp is sampled — the profile itself is smooth. */
const MAP_SIZE = 256;

/**
 * Paints the displacement map onto a canvas and hands back a data URI, which
 * is what `feImage` can load. Returns null where there is no DOM (a build or
 * a node test), and the filter simply isn't emitted.
 */
function useDisplacementMapUrl(size, band) {
  return useMemo(() => {
    if (typeof document === "undefined") return null;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    const image = ctx.createImageData(size, size);
    image.data.set(displacementMapPixels({ size, band }));
    ctx.putImageData(image, 0, 0);
    return canvas.toDataURL("image/png");
  }, [size, band]);
}

/**
 * Refraction for the liquid-glass surfaces: the SVG filter that CSS hands to
 * `backdrop-filter` so the page behind a pane bends instead of only blurring.
 *
 * Chromium is the only engine that runs an SVG filter as a backdrop filter, so
 * the CSS treats this as an enhancement — see the `@supports` block in
 * index.css, which leaves other engines on the blur-only surface.
 *
 * The filter pushes the pane's backdrop through `feDisplacementMap`, steering
 * it with a generated map whose red channel carries the horizontal offset and
 * green the vertical (see lib/refraction.js). The map is mid-grey through the
 * middle and ramps outward, so a pane stays optically flat where the text sits
 * and only bends in a band along its rim — a convex pane, not a fisheye.
 *
 * Two details that quietly break it if changed:
 *
 * - `color-interpolation-filters="sRGB"`. Filters default to linearRGB, which
 *   moves mid-grey off centre and would leave the "undisplaced" interior
 *   drifting.
 * - the `<svg>` is 0x0 and clipped, never `display: none`. Blink skips filters
 *   in a `display: none` subtree, and the `url()` reference stops resolving.
 *
 * `feImage` is given no subregion on purpose: it then fills the filter region,
 * which is pinned to the pane's own box below, so one map stretches to fit
 * every card and button rather than needing a filter per element.
 */
export default function GlassFilters({ scale = SCALE, band = DEFAULT_BAND, size = MAP_SIZE }) {
  const mapUrl = useDisplacementMapUrl(size, band);
  if (!mapUrl) return null;

  return (
    <svg className="glass-filters" width="0" height="0" aria-hidden="true" focusable="false">
      <filter
        id={REFRACTION_FILTER_ID}
        colorInterpolationFilters="sRGB"
        x="0%"
        y="0%"
        width="100%"
        height="100%"
      >
        <feImage href={mapUrl} preserveAspectRatio="none" result="displacementMap" />
        <feDisplacementMap
          in="SourceGraphic"
          in2="displacementMap"
          scale={scale}
          xChannelSelector="R"
          yChannelSelector="G"
        />
      </filter>
    </svg>
  );
}

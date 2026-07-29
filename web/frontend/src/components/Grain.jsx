import { useId } from "react";

/**
 * Film grain overlay. Two turbulence layers at different frequencies, both
 * soft-light blended, so a flat gradient reads as a lit surface rather than
 * a CSS gradient.
 *
 * Filter ids are per-instance: two Grains on one page sharing a hardcoded id
 * would both resolve url(#…) to whichever mounted first.
 */
export default function Grain({ fineOpacity = 0.145, coarseOpacity = 0.062 }) {
  const raw = useId();
  const uid = raw.replace(/:/g, "");
  const fineId = `grainFine${uid}`;
  const coarseId = `grainCoarse${uid}`;

  return (
    <>
      <svg className="grain-fine" style={{ opacity: fineOpacity }}>
        <filter id={fineId}>
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="4" />
        </filter>
        <rect width="100%" height="100%" filter={`url(#${fineId})`} />
      </svg>
      <svg className="grain-coarse" style={{ opacity: coarseOpacity }}>
        <filter id={coarseId}>
          <feTurbulence type="fractalNoise" baseFrequency="0.35" numOctaves="2" />
        </filter>
        <rect width="100%" height="100%" filter={`url(#${coarseId})`} />
      </svg>
    </>
  );
}

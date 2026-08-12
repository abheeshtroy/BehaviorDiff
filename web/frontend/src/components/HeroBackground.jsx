import Grain from "./Grain";

export default function HeroBackground() {
  return (
    <div className="herobg" aria-hidden="true">
      <div className="shaft shaft-l shaft-l1" />
      <div className="shaft shaft-l shaft-l2" />
      <div className="shaft shaft-l shaft-l3" />
      <div className="shaft shaft-l shaft-l4" />
      <div className="shaft shaft-r shaft-r1" />
      <div className="shaft shaft-r shaft-r2" />
      <div className="shaft shaft-r shaft-r3" />
      <div className="shaft shaft-r shaft-r4" />

      <svg className="trace-svg" viewBox="0 0 1600 780" preserveAspectRatio="none">
        <defs>
          <linearGradient id="traceL" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="rgba(40,255,225,0)" />
            <stop offset="40%" stopColor="rgba(60,255,230,0.55)" />
            <stop offset="100%" stopColor="rgba(120,255,240,0.90)" />
          </linearGradient>
          <linearGradient id="traceR" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="rgba(255,165,155,0.90)" />
            <stop offset="50%" stopColor="rgba(255,135,125,0.50)" />
            <stop offset="100%" stopColor="rgba(240,95,95,0)" />
          </linearGradient>
        </defs>
        {/* Stroke widths are in viewBox units, and the viewBox is 1600 wide
            stretched to the viewport — roughly a 0.46x squeeze at desktop. A
            1.5 here used to land under a pixel, thin enough that the glass
            panes' 2px blur erased it and the refracted bend had nothing left
            to show. Sized so the traces survive the blur and stay legible
            lines through the curve where they cross a pane. */}
        <g fill="none" strokeLinecap="round">
          <path d="M-40 300 C 240 300, 560 300, 800 300" stroke="url(#traceL)" strokeWidth="2.6" />
          <path d="M-40 311 C 240 311, 560 311, 800 305" stroke="url(#traceL)" strokeWidth="1.4" opacity="0.34" />
          <path d="M-40 289 C 240 289, 560 289, 800 295" stroke="url(#traceL)" strokeWidth="1.4" opacity="0.34" />
          <path d="M800 300 C 1000 230, 1250 174, 1640 130" stroke="url(#traceR)" strokeWidth="2.6" />
          <path d="M800 300 C 1000 370, 1250 426, 1640 470" stroke="url(#traceR)" strokeWidth="2.6" opacity="0.7" />
          <path d="M800 300 C 1020 266, 1280 234, 1640 208" stroke="url(#traceR)" strokeWidth="1.6" opacity="0.42" />
          <path d="M800 300 C 1020 334, 1280 368, 1640 396" stroke="url(#traceR)" strokeWidth="1.6" opacity="0.42" />
          <path d="M800 300 C 1040 298, 1340 296, 1640 294" stroke="url(#traceR)" strokeWidth="1.1" opacity="0.26" />
        </g>
        <circle cx="800" cy="300" r="28" fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="1" />
        <circle cx="800" cy="300" r="14" fill="none" stroke="rgba(255,255,255,0.14)" strokeWidth="1" />
        <circle cx="800" cy="300" r="3.5" fill="rgba(255,255,255,0.65)" />
      </svg>

      <div className="hero-divider" />
      <div className="hero-vignette" />

      <span className="side-label side-label-base">base</span>
      <span className="side-label side-label-target">target</span>

      <Grain />
    </div>
  );
}

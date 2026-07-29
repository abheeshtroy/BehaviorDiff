import Grain from "./Grain";

export default function DemoBackground({ diverged = false }) {
  return (
    <div className="demobg" aria-hidden="true">
      <div className={`col col-base ${diverged ? "recede" : ""}`} />
      <div className={`col col-target ${diverged ? "intensify" : ""}`} />
      <div className="col-mid" />
      <div className="demo-vignette" />
      {/* Lighter than the hero's — content sits on top of these pages. */}
      <Grain fineOpacity={0.085} coarseOpacity={0.038} />
    </div>
  );
}

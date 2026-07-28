export default function DemoBackground({ diverged = false }) {
  return (
    <div className="demobg" aria-hidden="true">
      <div className={`col col-base ${diverged ? "recede" : ""}`} />
      <div className={`col col-target ${diverged ? "intensify" : ""}`} />
      <div className="col-mid" />
      <div className="demo-vignette" />
    </div>
  );
}

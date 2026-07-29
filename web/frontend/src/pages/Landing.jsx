import HeroBackground from "../components/HeroBackground";
import { Link } from "react-router-dom";

const HEADLINE = "Find every unintended change before your users do";
const WORDS = HEADLINE.split(" ");

export default function Landing() {
  const tailDelay = 0.18 + WORDS.length * 0.055;

  return (
    <div style={{ position: "relative" }}>
      <HeroBackground />

      <div className="hero">
        <h1>
          {WORDS.map((word, i) => (
            <span
              key={i}
              className="hero-word"
              style={{ animationDelay: `${0.18 + i * 0.055}s` }}
            >
              {word}&nbsp;
            </span>
          ))}
        </h1>
        <p className="hero-sub hero-fade" style={{ animationDelay: `${tailDelay + 0.05}s` }}>
          BehaviorDiff runs your app on both branches under identical conditions
          and surfaces every difference in responses, database state, side effects, and timing.
        </p>
        <div className="hero-btns hero-fade" style={{ animationDelay: `${tailDelay + 0.14}s` }}>
          <Link to="/runs/new"><button className="btn-pri">Try a live comparison</button></Link>
          <a href="https://github.com/abheeshtroy/BehaviorDiff" target="_blank" rel="noopener noreferrer">
            <button className="btn-sec">View source</button>
          </a>
        </div>
        <div className="hero-note hero-fade" style={{ animationDelay: `${tailDelay + 0.22}s` }}>
          No signup required · Results in ~40 seconds · All evidence is reproducible
        </div>
      </div>

      <div className="pitch">
        <div className="pitch-cols">
          <div>
            <div className="pitch-label">What the diff shows</div>
            <div className="diff-block">
              <div className="diff-ctx">&nbsp;def checkout(cart):</div>
              <div className="diff-add">+&nbsp;&nbsp;&nbsp;if not cart.items:</div>
              <div className="diff-add">+&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;raise ValidationError</div>
              <div className="diff-rem">-&nbsp;&nbsp;&nbsp;charge(cart.total)</div>
              <div className="diff-add">+&nbsp;&nbsp;&nbsp;charge(cart.total)</div>
            </div>
            <div className="ci-row">
              <span className="ci-badge ci-pass">CI passing</span>
              <span className="ci-badge ci-cov">94% coverage</span>
            </div>
          </div>
          <div className="pitch-arrow">→</div>
          <div>
            <div className="pitch-label">What actually changed</div>
            <div className="actual-list">
              <div className="actual-row intended"><span className="actual-tag">intended</span> 500 → 400 on bad address</div>
              <div className="actual-row bug"><span className="actual-tag">bug</span> Discount silently cleared</div>
              <div className="actual-row bug"><span className="actual-tag">bug</span> Payment charged on failure</div>
              <div className="actual-row regression"><span className="actual-tag">latency</span> +280ms on checkout</div>
            </div>
          </div>
        </div>
      </div>

      <div className="section-title">Try it yourself</div>
      <div className="demos">
        <Link to="/demo/checkout-validation" className="demo-card">
          <div className="demo-icon" style={{ color: "var(--red)" }}>⊘</div>
          <div className="demo-name">Checkout validation fix</div>
          <div className="demo-desc">Four-line fix that silently breaks discounts and charges on failure</div>
          <span className="demo-tag tag-red">3 regressions</span>
        </Link>
        <Link to="/demo/retry-logic" className="demo-card">
          <div className="demo-icon" style={{ color: "var(--amber)" }}>↻</div>
          <div className="demo-name">Retry logic refactor</div>
          <div className="demo-desc">Background job retry that schedules duplicate work</div>
          <span className="demo-tag tag-amber">2 regressions</span>
        </Link>
        <Link to="/demo/api-cleanup" className="demo-card">
          <div className="demo-icon" style={{ color: "var(--purple)" }}>⟐</div>
          <div className="demo-name">API response cleanup</div>
          <div className="demo-desc">Field rename that breaks downstream consumers</div>
          <span className="demo-tag tag-purple">1 breaking change</span>
        </Link>
      </div>

      <div className="section-title">How it works</div>
      <div className="steps">
        <div className="step">
          <div className="step-num">01</div>
          <div className="step-title">Pick a change</div>
          <div className="step-desc">Select a PR or point at two commits</div>
        </div>
        <div className="step">
          <div className="step-num">02</div>
          <div className="step-title">Run both versions</div>
          <div className="step-desc">Same database, same requests, same timing</div>
        </div>
        <div className="step">
          <div className="step-num">03</div>
          <div className="step-title">See what changed</div>
          <div className="step-desc">Every difference with raw evidence</div>
        </div>
      </div>

      <div className="cta-block">
        <p>Code review shows what changed in the source. BehaviorDiff shows what changed in the software.</p>
      </div>
    </div>
  );
}

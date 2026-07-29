/**
 * Content for the orientation beat that runs before a scenario starts.
 *
 * Every number here comes from SCENARIOS["checkout-validation"] in
 * demoData.js: `checks` supplies the left column, `findings` the right
 * (classification "intended" vs everything else), `pr` and `title` the
 * header. It is written out rather than derived because the headline and
 * sub are prose about this one change; when the other scenarios need a beat,
 * add an entry here keyed by scenario id.
 */
export const CHECKOUT_BEAT = {
  pr: "#482",
  title: "Fix checkout validation for empty carts",
  scale: "4 lines changed",
  headline: "Every check was green. Two bugs shipped anyway.",
  sub: "A four-line checkout fix also wiped customer discounts and charged the card on orders it rejected. The diff didn't say so. CI didn't catch it.",
  // Left column: what the existing checks reported. All green, all true.
  checks: ["47 tests passing", "94% coverage", "lint clean", "2 approvals"],
  // Right column: what the run actually did. One of these was the point of the PR.
  changes: [
    { tag: "intended", tone: "intended", text: "POST /checkout: 200 → 400" },
    { tag: "bug", tone: "bug", text: "Discount silently cleared" },
    { tag: "bug", tone: "bug", text: "Card charged on a rejected order" },
  ],
  caption: "Only one of those three was in the pull request description.",
};

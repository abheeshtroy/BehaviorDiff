/**
 * The one place loading / error / empty / not-found are rendered.
 *
 * Every page used to inline its own unstyled paragraph for these, which is
 * why the non-happy paths looked like a different product. Actions render
 * as children so callers can offer a way out of the state.
 */
const EMPTY_ICON = { empty: "◇", notfound: "∅" };

export default function StateNotice({ variant, title, detail = null, children = null }) {
  if (variant === "loading") {
    return (
      <div className="state-loading">
        <span className="state-spinner" aria-hidden="true" />
        {title}
      </div>
    );
  }

  if (variant === "error") {
    return (
      <div className="alert alert-err">
        <span className="alert-icon">⚠</span>
        <div>
          <div className="alert-title">{title}</div>
          {detail && <div className="alert-detail">{detail}</div>}
          {children && <div className="state-actions">{children}</div>}
        </div>
      </div>
    );
  }

  return (
    <div className="empty-state">
      <div className="empty-icon">{EMPTY_ICON[variant] ?? "◇"}</div>
      <p className="empty-title">{title}</p>
      {detail && <p className="empty-hint">{detail}</p>}
      {children && <div className="state-actions state-actions-center">{children}</div>}
    </div>
  );
}

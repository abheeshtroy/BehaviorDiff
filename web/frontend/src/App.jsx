import { BrowserRouter, Routes, Route, Link } from "react-router-dom";
import Landing from "./pages/Landing";
import RunList from "./pages/RunList";
import RunDetail from "./pages/RunDetail";

export default function App() {
  return (
    <BrowserRouter>
      <div>
        <header className="topbar">
          <Link to="/" className="topbar-logo">BehaviorDiff</Link>
          <div className="topbar-sep" />
          <span className="topbar-ctx">differential testing engine</span>
          <div className="topbar-right">
            <a href="https://github.com/abheeshtroy/BehaviorDiff" target="_blank" rel="noopener noreferrer" className="topbar-link">
              GitHub
            </a>
          </div>
        </header>
        <main className="page">
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/runs" element={<RunList />} />
            <Route path="/runs/:runId" element={<RunDetail />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

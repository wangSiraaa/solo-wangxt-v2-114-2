import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import PlotMap from "./components/PlotMap.jsx";
import PlotDetail from "./components/PlotDetail.jsx";
import ConflictsWorkbench from "./components/ConflictsWorkbench.jsx";
import EstimatePanel from "./components/EstimatePanel.jsx";
import TimelinePanel from "./components/TimelinePanel.jsx";

const TABS = ["map", "timeline", "conflicts", "estimates"];

export default function App() {
  const [tab, setTab] = useState("map");
  const [plots, setPlots] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [chains, setChains] = useState([]);
  const [pair, setPair] = useState(null);      // [t1, t2] adjacent codes
  const [m1, setM1] = useState([]);
  const [m2, setM2] = useState([]);
  const [selectedPlot, setSelectedPlot] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [error, setError] = useState("");

  async function refreshConflicts() {
    setConflicts(await api.conflicts("open"));
  }

  useEffect(() => {
    (async () => {
      try {
        const [ps, cs, sq] = await Promise.all([
          api.plots(), api.campaigns(), api.sequences()]);
        setPlots(ps);
        setCampaigns(cs);
        setChains(sq);
        // adjacent campaign pairs from the chain; fall back to the two
        // outermost campaigns when no sequence exists yet
        const first = sq[0];
        let pairs = [];
        if (first && first.intervals.length) {
          pairs = first.intervals
            .filter((i) => i.is_adjacent)
            .map((i) => [i.t1, i.t2]);
        } else {
          const ordered = [...cs].sort((a, b) =>
            a.measured_on.localeCompare(b.measured_on));
          for (let i = 1; i < ordered.length; i++)
            pairs.push([ordered[i - 1].code, ordered[i].code]);
        }
        if (pairs.length) setPair(pairs[pairs.length - 1]);
        setConflicts(await api.conflicts("open"));
      } catch (e) {
        setError(e.message);
      }
    })();
  }, []);

  const [t1, t2] = pair ?? [null, null];

  useEffect(() => {
    if (!t1 || !t2) return;
    (async () => {
      const [a, b] = await Promise.all([
        api.measurements(t1), api.measurements(t2)]);
      setM1(a);
      setM2(b);
    })();
  }, [t1, t2]);

  const pairs = useMemo(() => {
    const first = chains[0];
    if (first && first.intervals.length) {
      return first.intervals.filter((i) => i.is_adjacent)
        .map((i) => [i.t1, i.t2]);
    }
    const ordered = [...campaigns].sort((a, b) =>
      a.measured_on.localeCompare(b.measured_on));
    const out = [];
    for (let i = 1; i < ordered.length; i++)
      out.push([ordered[i - 1].code, ordered[i].code]);
    return out;
  }, [chains, campaigns]);

  const ctx = useMemo(() => ({
    plots, campaigns, chains, t1, t2, m1, m2, conflicts,
    setSelectedPlot, refreshConflicts,
  }), [plots, campaigns, chains, t1, t2, m1, m2, conflicts]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Fixed-plot remeasurement station</h1>
        <div className="meta">
          {campaigns.map((c) => (
            <span key={c.code} className="chip">
              {c.code} · {c.measured_on}
            </span>
          ))}
          <span className="chip warn-chip">
            {conflicts.length} open identity conflict
            {conflicts.length === 1 ? "" : "s"}
          </span>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? "tab active" : "tab"}
                  onClick={() => setTab(t)}>
            {t === "map" ? "Plots & individuals"
              : t === "timeline" ? "Timeline (interval chain)"
              : t === "conflicts" ? `Identity conflicts (${conflicts.length})`
              : "Estimates"}
          </button>
        ))}
      </nav>

      <main>
        {tab === "map" && (
          <>
            <div className="pair-picker">
              interval:{" "}
              {pairs.map(([a, b]) => (
                <button key={a + b}
                        className={t1 === a && t2 === b ? "tab active" : "tab"}
                        onClick={() => setPair([a, b])}>
                  {a} → {b}
                </button>
              ))}
            </div>
            {selectedPlot
              ? <PlotDetail plotCode={selectedPlot} ctx={ctx}
                            onBack={() => setSelectedPlot(null)} />
              : <PlotMap ctx={ctx} onSelect={setSelectedPlot} />}
          </>
        )}
        {tab === "timeline" && <TimelinePanel ctx={ctx} />}
        {tab === "conflicts" && (
          <ConflictsWorkbench ctx={ctx}
                              onChanged={async () => {
                                setConflicts(await api.conflicts("open"));
                              }} />
        )}
        {tab === "estimates" && <EstimatePanel ctx={ctx} />}
      </main>

      <footer>
        Fictional demonstration data · coordinates EPSG:{plots[0]?.crs_epsg}
        {" "}· dbh cm (raw unit retained) · height m · areas in hectares ·
        estimates are computed per ADJACENT interval only
      </footer>
    </div>
  );
}

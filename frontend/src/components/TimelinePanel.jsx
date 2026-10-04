import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const STATUS_LABEL = {
  alive_measured: "measured",
  alive_not_measured: "NOT measured",
  dead: "dead",
  missing_tree: "not located",
};
const KIND_LABEL = {
  survivor: "survivor",
  survivor_renumber: "survivor (renumber)",
  survivor_unmeasured: "survivor, dbh missing",
  mortality: "mortality",
  not_located: "not located",
  ingrowth: "ingrowth",
  below_recruitment: "below 5 cm",
  gap_reappearance: "GAP reappearance — verify",
  resurrected: "dead→alive contradiction",
  identity_conflict: "identity conflict",
};

function Mg(v) { return v == null ? "—" : v.toFixed(2); }

/**
 * Multi-period timeline: the adjacent-interval chain of the survey
 * sequence, expandable per interval (provenance), per plot (individual
 * matrix) and per tree (full chronological trace).
 */
export default function TimelinePanel({ ctx }) {
  const { plots } = ctx;
  const [chains, setChains] = useState([]);
  const [seqId, setSeqId] = useState(null);
  const [chain, setChain] = useState(null);
  const [openInterval, setOpenInterval] = useState(null);
  const [prov, setProv] = useState(null);
  const [plotCode, setPlotCode] = useState(null);
  const [matrix, setMatrix] = useState(null);
  const [treeId, setTreeId] = useState(null);
  const [tree, setTree] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function loadChains() {
    const all = await api.sequences();
    setChains(all);
    if (all.length && seqId == null) setSeqId(all[0].id);
  }
  useEffect(() => { loadChains().catch((e) => setErr(e.message)); }, []);

  async function loadChain(id) {
    setChain(await api.sequence(id));
  }
  useEffect(() => {
    if (seqId != null) loadChain(seqId).catch((e) => setErr(e.message));
  }, [seqId]);

  useEffect(() => {
    if (openInterval == null) { setProv(null); return; }
    api.intervalProvenance(openInterval).then(setProv)
      .catch((e) => setErr(e.message));
  }, [openInterval]);

  useEffect(() => {
    if (seqId == null || !plotCode) { setMatrix(null); return; }
    api.plotTimeline(seqId, plotCode).then(setMatrix)
      .catch((e) => setErr(e.message));
  }, [seqId, plotCode]);

  useEffect(() => {
    if (treeId == null) { setTree(null); return; }
    api.treeTimeline(treeId).then(setTree).catch((e) => setErr(e.message));
  }, [treeId]);

  async function sync(runEstimates) {
    setBusy(true); setErr("");
    try {
      await api.syncSequence(seqId, { run_estimates: runEstimates });
      await loadChains(); await loadChain(seqId);
      if (openInterval != null)
        setProv(await api.intervalProvenance(openInterval));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function recompute(intervalId, runEstimate) {
    setBusy(true); setErr("");
    try {
      await api.recomputeInterval(intervalId, { run_estimate: runEstimate });
      await loadChains(); await loadChain(seqId);
      setProv(await api.intervalProvenance(intervalId));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  const intervalsById = useMemo(() => {
    const m = {};
    (chain?.intervals || []).forEach((i) => { m[i.id] = i; });
    return m;
  }, [chain]);

  if (!chains.length) {
    return (
      <div>
        <h2>Survey sequence timeline</h2>
        {err && <div className="error">{err}</div>}
        <p className="hint">No survey sequence yet. Seed the demo data
          (seed_demo) — the sequence is created automatically.</p>
      </div>
    );
  }

  return (
    <div>
      <h2>Survey sequence timeline
        <small> adjacent intervals only — 2019 and 2029 are never stitched
          into one continuous-survival pair</small>
      </h2>
      {err && <div className="error">{err}</div>}

      <div className="chain-controls">
        {chains.length > 1 && (
          <select value={seqId ?? ""}
                  onChange={(e) => setSeqId(Number(e.target.value))}>
            {chains.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>))}
          </select>
        )}
        <button disabled={busy} onClick={() => sync(false)}>
          sync chain (补齐区间)
        </button>
        <button disabled={busy} onClick={() => sync(true)}>
          sync + draft estimates for new intervals
        </button>
        {chain?.cumulative_net_change && (
          <span className="chip">
            chained net Δ {chain.cumulative_net_change.net_change_mg.toFixed(2)}
            {" "}Mg — sum of adjacent intervals, not a direct pair
          </span>
        )}
      </div>

      {chain && (
        <div className="chain">
          {chain.campaigns.map((c, idx) => {
            const iv = chain.intervals[idx - 1];
            return (
              <React.Fragment key={c.code}>
                {iv && (
                  <button
                    className={`interval-card cov-${iv.coverage}` +
                      (openInterval === iv.id ? " open" : "")}
                    onClick={() => setOpenInterval(
                      openInterval === iv.id ? null : iv.id)}>
                    <strong>{iv.t1} → {iv.t2}</strong>
                    <span className="iv-meta">{iv.interval_years} yr ·
                      {" "}{iv.coverage}</span>
                    {iv.components ? (
                      <span className="iv-nums">
                        G {Mg(iv.components.survivor_growth_mg)} ·
                        M {Mg(iv.components.mortality_mg)} ·
                        I {Mg(iv.components.ingrowth_mg)} Mg
                      </span>
                    ) : <span className="iv-meta">no estimate yet</span>}
                    <span className="iv-meta">
                      {iv.pending_items > 0
                        ? <b className="pending-badge">{iv.pending_items} pending</b>
                        : "no pending items"}
                      {" · "}{iv.versions.length} edition(s)
                    </span>
                  </button>
                )}
                <span className="campaign-node">
                  <b>{c.code}</b>
                  <small>{c.measured_on}</small>
                </span>
              </React.Fragment>
            );
          })}
        </div>
      )}

      {prov && intervalsById[openInterval] && (
        <section className="interval-detail">
          <h3>Interval {prov.interval.t1} → {prov.interval.t2}
            <span className={`badge cov-${prov.interval.coverage}`}>
              {prov.interval.coverage}
            </span>
          </h3>
          <div className="two-col">
            <div>
              <h4>Sources (区间来源)</h4>
              {["t1", "t2"].map((k) => (
                <p key={k} className="src-line">
                  <b>{prov.sources[k].campaign}</b>:{" "}
                  {prov.sources[k].measurements} measurements ·{" "}
                  {prov.sources[k].import_rows_accepted} accepted /{" "}
                  {prov.sources[k].import_rows_rejected} rejected import rows
                </p>
              ))}
              <h4>Editions (估计版本)</h4>
              {prov.versions.length === 0 && <p className="hint">none yet</p>}
              {prov.versions.map((v) => (
                <p key={v.id} className="src-line">
                  #{v.id} {v.label}{" "}
                  <span className={`status-${v.status}`}>{v.status}</span>
                  {!v.linked && " (legacy, matched by campaigns)"}
                </p>
              ))}
              <button disabled={busy}
                      onClick={() => recompute(openInterval, false)}>
                recompute interval (重算)
              </button>
              <button disabled={busy}
                      onClick={() => recompute(openInterval, true)}>
                recompute + new draft estimate
              </button>
            </div>
            <div>
              <h4>Pending verification (待核实链路)</h4>
              {prov.identity.pending.length === 0 &&
                <p className="ok">no pending identity items</p>}
              {prov.identity.pending.map((l, i) => (
                <div key={i} className="pending-item">
                  <b>{l.plot}/{l.label_t1 ?? "—"} → {l.label_t2 ?? "—"}</b>
                  {" "}{KIND_LABEL[l.kind] || l.kind}
                  <br /><small>{l.note}</small>
                </div>
              ))}
              {prov.identity.open_conflicts.length > 0 && (
                <p className="hint">
                  {prov.identity.open_conflicts.length} open identity
                  conflict(s) — resolve them in the Identity conflicts tab;
                  they apply to this interval only.
                </p>
              )}
            </div>
          </div>
        </section>
      )}

      <section>
        <h3>By plot &amp; individual (按样地和个体展开)</h3>
        <div className="chain-controls">
          <select value={plotCode ?? ""}
                  onChange={(e) => setPlotCode(e.target.value || null)}>
            <option value="">choose a plot…</option>
            {plots.map((p) => (
              <option key={p.code} value={p.code}>{p.code}</option>))}
          </select>
        </div>
        {matrix && (
          <table className="tree-table timeline-table">
            <thead>
              <tr>
                <th>tree</th>
                {matrix.campaigns.map((c) => <th key={c.code}>{c.code}</th>)}
                {matrix.intervals.map((i) => <th key={i}>{i}</th>)}
              </tr>
            </thead>
            <tbody>
              {matrix.trees.map((t) => (
                <tr key={t.tree_id}
                    className={matrix.intervals.some(
                      (i) => t.intervals[i]?.pending) ? "pending-row" : ""}
                    onClick={() => setTreeId(
                      treeId === t.tree_id ? null : t.tree_id)}>
                  <td>#{t.tree_id} {t.current_field_number}
                    <small> {t.species}</small></td>
                  {matrix.campaigns.map((c) => {
                    const o = t.occasions[c.code];
                    return (
                      <td key={c.code}
                          className={o ? `st-${o.status}` : "st-none"}>
                        {o ? (
                          <>
                            {o.field_number} · {STATUS_LABEL[o.status]}
                            {o.dbh_cm != null && ` · ${o.dbh_cm} cm`}
                          </>
                        ) : "—"}
                      </td>
                    );
                  })}
                  {matrix.intervals.map((i) => {
                    const l = t.intervals[i];
                    return (
                      <td key={i}
                          className={l ? (l.pending ? "link-pending"
                                                    : "link-ok")
                                       : "st-none"}>
                        {l ? (KIND_LABEL[l.kind] || l.kind) : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tree && (
          <div className="tree-trace">
            <h4>Individual #{tree.tree_id} — plot {tree.plot},
              tag “{tree.current_field_number}” ({tree.species})</h4>
            <div className="trace-line">
              {tree.occasions.map((o) => (
                <span key={o.campaign} className={`trace-node st-${o.status}`}>
                  <b>{o.campaign}</b> {o.field_number} · {STATUS_LABEL[o.status]}
                  {o.dbh_cm != null && ` · ${o.dbh_cm} cm`}
                </span>
              ))}
            </div>
            {tree.interval_links.map((l, i) => (
              <span key={i}
                    className={`chip ${l.pending ? "warn-chip" : ""}`}>
                {l.interval}: {KIND_LABEL[l.kind] || l.kind}
              </span>
            ))}
            {tree.interval_links.some((l) => l.pending) && (
              <p className="hint">
                Pending links are excluded from every estimate component
                until a human verifies the identity — survival is never
                stitched across a missing occasion.
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

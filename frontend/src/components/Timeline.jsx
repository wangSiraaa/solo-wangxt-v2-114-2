import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * Plot & individual timeline over an adjacent-interval chain.
 *
 * Columns are campaigns (2019, 2024, 2029); edges exist ONLY between
 * adjacent campaigns. A missing occasion is drawn as a broken edge with a
 * "verify" marker — never a straight 2019 -> 2029 survivor line.
 */

const EDGE_LABEL = {
  survivor_same_number: "same tag — survivor growth",
  survivor_renumber: "verified renumber — survivor",
  survivor_zero_growth: "verified zero growth",
  alive_not_measured: "alive, NOT measured (ratio-imputed)",
  mortality: "MORTALITY",
  ingrowth: "INGROWTH",
  ingrowth_distinct_verified: "ingrowth (distinct verified)",
  below_recruitment: "below recruitment — excluded",
  not_located_t2: "not located",
  removal_unobserved: "removal unobserved",
  dead_at_t1: "already dead at t1",
  pending_same_number_mismatch: "PENDING: number / position mismatch",
  possible_renumber: "PENDING: possible renumber",
  gap_reappearance: "PENDING: reappears after gap",
  gap_reappearance_verified: "gap checked — still excluded",
  not_covered: "not covered",
};

function edgeClass(e) {
  if (e.gap) return e.verified ? "gap-verified" : "gap";
  if (e.pending) return "pending";
  if (e.kind === "mortality") return "mortality";
  if (e.kind === "ingrowth" || e.kind === "ingrowth_distinct_verified")
    return "ingrowth";
  if (e.kind === "below_recruitment" || e.kind === "not_located_t2"
      || e.kind === "removal_unobserved" || e.kind === "dead_at_t1")
    return "excluded";
  if (e.kind === "alive_not_measured") return "notmeasured";
  return "survivor";
}

function StatusCell({ node }) {
  if (!node) return <span className="hole">— missing —</span>;
  const cls = node.status === "dead" ? "mortality"
    : node.status === "missing_tree" ? "missing"
    : node.status === "alive_not_measured" ? "notmeasured"
    : "growth";
  return (
    <span className={`tcell ${cls}`}>
      {node.field_number}
      <small>
        {node.status === "alive_measured"
          ? `Ø ${node.dbh_cm?.toFixed?.(1) ?? node.dbh_cm} cm`
          : node.status.replace(/_/g, " ")}
      </small>
    </span>
  );
}

function EdgeCell({ edge }) {
  if (!edge) return <span className="no-edge" />;
  return (
    <span className={`edge-badge ${edgeClass(edge)}`}
          title={EDGE_LABEL[edge.kind] || edge.kind}>
      {edge.gap && "⛓‍⚠ "}
      {edge.pending ? "VERIFY"
        : edge.kind === "mortality" ? "†"
        : edge.kind?.startsWith("ingrowth") ? "+"
        : edge.kind === "alive_not_measured" ? "~"
        : edge.kind === "not_located_t2" ? "?"
        : edge.kind === "survivor_zero_growth" ? "0"
        : edge.kind === "not_covered" ? "·"
        : "→"}
    </span>
  );
}

export default function Timeline({ ctx }) {
  const [sequenceCode, setSequenceCode] = useState("MAIN");
  const [sequences, setSequences] = useState([]);
  const [plotCode, setPlotCode] = useState(null);
  const [data, setData] = useState(null);
  const [treeId, setTreeId] = useState(null);
  const [treeData, setTreeData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.sequences().then((s) => {
      setSequences(s);
      if (s.length && !sequenceCode) setSequenceCode(s[0].code);
    }).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    if (!plotCode || !sequenceCode) return;
    setTreeId(null);
    setTreeData(null);
    api.plotTimeline(sequenceCode, plotCode)
      .then(setData).catch((e) => setErr(e.message));
  }, [sequenceCode, plotCode]);

  useEffect(() => {
    if (!treeId || !sequenceCode) return;
    api.treeTimeline(sequenceCode, treeId)
      .then(setTreeData).catch((e) => setErr(e.message));
  }, [treeId, sequenceCode, data]);

  const stats = useMemo(() => {
    if (!data) return null;
    const n = data.individuals.length;
    const pending = new Set();
    const gaps = new Set();
    for (const ind of data.individuals) {
      for (const e of ind.edges) {
        if (e.pending) pending.add(ind.tree_id);
        if (e.gap) gaps.add(ind.tree_id);
      }
    }
    return { n, pending: pending.size, gaps: gaps.size };
  }, [data]);

  const current = data?.individuals.find((i) => i.tree_id === treeId) || null;

  return (
    <div>
      <h2>Multi-campaign timeline
        <small> identity holds only inside adjacent intervals</small>
      </h2>
      <p className="hint">
        Each arrow is a separate chain link with its own coverage snapshot,
        identity verdict and estimate edition. A cross-gap reappearance is a
        ⛓⚠ VERIFY item — 2019 and 2029 are never joined directly.
      </p>
      {err && <div className="error">{err}</div>}

      <div className="chain-controls">
        <label>Sequence
          <select value={sequenceCode}
                  onChange={(e) => setSequenceCode(e.target.value)}>
            {sequences.map((s) => <option key={s.id} value={s.code}>
              {s.code} — {s.name}
            </option>)}
          </select>
        </label>
        <label>Plot
          <select value={plotCode || ""}
                  onChange={(e) => setPlotCode(e.target.value || null)}>
            <option value="">choose plot…</option>
            {ctx.plots.map((p) => <option key={p.code} value={p.code}>
              {p.code} ({p.declared_area_ha} ha)
            </option>)}
          </select>
        </label>
        {data?.intervals.map((iv) => (
          <span key={iv.id}
                className={`chip ${iv.n_pending_links ? "warn-chip" : ""}`}>
            {iv.code} · {iv.status}
            {iv.n_pending_links ? ` · ${iv.n_pending_links} to verify` : ""}
          </span>
        ))}
      </div>

      {data && stats && (
        <p className="hint">
          {stats.n} individuals · {stats.pending} with a pending link ·
          {" "}{stats.gaps} cross a gap
        </p>
      )}

      {data && (
        <table className="timeline-table">
          <thead>
            <tr>
              <th>individual</th>
              {data.campaigns.map((c, i) => (
                <React.Fragment key={c.code}>
                  <th>{c.code}</th>
                  {i < data.intervals.length
                    && <th className="edge-col">{data.intervals[i].code}
                      {data.intervals[i].n_pending_links
                        ? <span className="warn-dot">
                            {data.intervals[i].n_pending_links}
                          </span> : null}
                    </th>}
                </React.Fragment>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.individuals.map((ind) => (
              <tr key={ind.tree_id}
                  className={!ind.chain_complete ? "has-pending" : ""}
                  onClick={() => setTreeId(ind.tree_id)}>
                <td className="who">
                  <strong>{ind.current_field_number}</strong>
                  <small>{ind.species} · #{ind.tree_id}</small>
                </td>
                {ind.nodes.map((node, i) => (
                  <React.Fragment key={i}>
                    <td><StatusCell node={node} /></td>
                    {i < ind.edges.length
                      && <td className="edge-col">
                        <EdgeCell edge={ind.edges[i]} />
                      </td>}
                  </React.Fragment>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {treeData && current && (
        <section className="tree-chain">
          <h3>Individual #{treeId} — {current.current_field_number}
            <small> chain of adjacent links only</small>
          </h3>
          <div className="chain-row">
            {current.nodes.map((node, i) => (
              <React.Fragment key={i}>
                <div className={`chain-node ${node ? node.status : "hole"}`}>
                  <span className="year">{treeData.campaigns[i].code}</span>
                  {node ? (
                    <>
                      <strong>{node.field_number}</strong>
                      <span>{node.status.replace(/_/g, " ")}</span>
                      {node.dbh_cm != null
                        && <span>Ø {node.dbh_cm} cm</span>}
                      <span>({node.x_m.toFixed(1)}, {node.y_m.toFixed(1)})
                      </span>
                    </>
                  ) : <em>no record (chain hole)</em>}
                </div>
                {i < current.edges.length && (
                  <div className={`chain-edge ${edgeClass(current.edges[i])}`}
                       title={EDGE_LABEL[current.edges[i].kind]
                              || current.edges[i].kind}>
                    <span className="arrow">
                      {current.edges[i].gap ? "⛓⚠"
                        : current.edges[i].pending ? "VERIFY" : "→"}
                    </span>
                    <small>
                      {EDGE_LABEL[current.edges[i].kind]
                       || current.edges[i].kind}
                    </small>
                  </div>
                )}
              </React.Fragment>
            ))}
          </div>
          {treeData.intervals.map((iv) => {
            const e = current.edges.find((x) => x.interval === iv.code);
            return (
              <div key={iv.id} className="link-detail">
                <strong>{iv.code}</strong>:{" "}
                {EDGE_LABEL[e?.kind] || e?.kind || "—"}
                {e?.excluded && <em> · excluded from components</em>}
                {e?.detail?.reason && <small> — {e.detail.reason}</small>}
              </div>
            );
          })}
        </section>
      )}
    </div>
  );
}

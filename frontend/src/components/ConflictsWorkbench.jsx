import React, { useEffect, useState } from "react";
import { api } from "../api.js";

/**
 * Human-in-the-loop identity workbench.
 * Every item is scoped to ONE adjacent interval. A same number with
 * contradictory positions, a near-neighbour relabel, or a tree reappearing
 * after a missing occasion is never treated as the same individual until a
 * human verifies it here. A gap verdict keeps the tree EXCLUDED — verifying
 * does not invent the missing occasion.
 */
export default function ConflictsWorkbench({ onChanged }) {
  const [all, setAll] = useState([]);
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    setAll(await api.conflicts());
  }
  useEffect(() => { load(); }, []);

  async function resolve(c, decision) {
    setBusy(c.id);
    setErr("");
    try {
      const out = await api.resolveConflict(c.id, { status: decision, note });
      setNote(`#${c.id} (${c.field_number}) recorded as ${decision}`
        + (out.rebuilt_intervals?.length
          ? `; rebuilt ${out.rebuilt_intervals.map((r) => r.interval)
              .join(", ")}` : ""));
      await load();
      onChanged?.();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <h2>Identity verification workbench
        <small> per-interval chain items only</small>
      </h2>
      <p className="hint">
        Each row belongs to exactly one adjacent interval (shown). A
        cross-gap reappearance (missing 2024, found 2029) stays excluded
        even after verification — it is never counted as survivor growth.
      </p>
      {note && <div className="ok">{note}</div>}
      {err && <div className="error">{err}</div>}
      <table className="conflict-table">
        <thead>
          <tr><th>interval</th><th>plot</th><th>number</th>
            <th>distance</th><th>reason</th><th>state</th>
            <th>verification</th></tr>
        </thead>
        <tbody>
          {all.map((c) => {
            const isGap = c.hint === "gap_reappearance";
            return (
              <tr key={c.id}
                  className={c.status === "open" ? "open" : "closed"}>
                <td className="interval-tag">
                  {c.t1_campaign_code} → {c.t2_campaign_code}
                </td>
                <td>{c.plot_code}</td>
                <td>{c.field_number}</td>
                <td>{c.distance_m == null
                  ? "—" : `${c.distance_m?.toFixed(2)} m`}</td>
                <td className="hint-cell">
                  <code>{c.hint.replace(/_/g, " ")}</code>
                  {isGap && <div className="gap-note">
                    gap: verification excludes only; never auto-growth
                  </div>}
                </td>
                <td>{c.status}{c.resolution_note
                  ? ` — ${c.resolution_note}` : ""}</td>
                <td>
                  {c.status === "open" ? (
                    <>
                      <button disabled={busy === c.id}
                              onClick={() => resolve(c, "renumber")}>
                        {isGap ? "verified reappearance (excluded)"
                               : "same tree, renumbered"}
                      </button>
                      <button className="danger" disabled={busy === c.id}
                              onClick={() => resolve(c, "distinct")}>
                        different trees
                      </button>
                    </>
                  ) : <span className="locked">verified {c.status}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./http";
import { opsApi } from "./opsApi";

type Overview = {
  sessions: number; disputes_created: number; cards_blocked?: number; resolved_by_self_service: number; human_handoffs: number;
  provider_errors: number; blocked_policy_attempts: number; ambiguous_matches: number;
  latency_ms: { p50?: number; p90?: number; p95?: number; samples?: number };
  sessions_by_outcome: Record<string, number>; handoff_reasons: Record<string, number>;
  intents: Record<string, number>; providers: Record<string, unknown>;
};
type Sess = { session_id: string; status: string; outcome: string | null; language?: string; turn_count: number; created_at?: string };
type Handoff = { handoff_id: string; reason: string; status: string; summary?: string; created_at?: string };

type Who = { username: string; role: "VIEWER" | "AGENT" | "ADMIN" };
// The operator token lives in memory only: never in localStorage/sessionStorage, where any injected script could read
// it. The cost is signing in again after a page refresh, which is the intended trade-off for an operator console.
const stored = (): { token: string; who: Who } | null => {
  // Older builds persisted the token here; purge it so no stale credential lingers in the browser.
  try { sessionStorage.removeItem("kv_operator_session"); } catch { /* storage unavailable */ }
  return null;
};
const remember = (_v: { token: string; who: Who } | null) => { /* intentionally not persisted */ };

type Detail = {
  session: Record<string, unknown>;
  tool_timeline?: { turn_index: number; tool: string; ok: boolean; error_code: string | null }[];
  audit?: { event_id: string; action: string; decision: string; reason_code: string; resource_id: string;
            actor_type: string; created_at: string }[];
  disputes?: { dispute_id: string; status: string }[];
  handoffs?: { handoff_id: string; reason: string; status: string }[];
};

// Audit actions that change customer state or reveal account data, shown first in the timeline.
const NOTABLE: Record<string, string> = {
  DISPUTE_CREATED: "Dispute raised", CARD_BLOCKED: "Card blocked", CARD_BLOCK_PREPARED: "Card block prepared",
  BALANCE_VIEWED: "Balance viewed", STATEMENT_VIEWED: "Statement viewed", LOAN_VIEWED: "Loans viewed",
  CARDS_LISTED: "Cards listed", HANDOFF_CREATED: "Handed to human", DISPUTE_PREPARED: "Dispute checked",
};

function SessionDetail({ d }: { d: Detail }) {
  const events = (d.audit ?? []).filter((a) => NOTABLE[a.action]);
  return (
    <>
      <p className="eyebrow">{String(d.session.session_id ?? "")} · {String(d.session.outcome ?? d.session.status ?? "")}</p>
      <h4>What the customer did</h4>
      {events.length === 0 && <p className="empty">No account actions in this session.</p>}
      <ol className="timeline">
        {events.map((a) => (
          <li key={a.event_id} className={a.decision === "DENIED" ? "denied" : ""}>
            <b>{NOTABLE[a.action]}</b>
            <span className="mono">{a.resource_id} · {a.decision.toLowerCase()} · {a.reason_code.replaceAll("_", " ").toLowerCase()}</span>
          </li>
        ))}
      </ol>
      <h4>Tool calls</h4>
      <ol className="timeline">
        {(d.tool_timeline ?? []).map((t, i) => (
          <li key={i} className={t.ok ? "" : "denied"}><b className="mono">{t.tool}</b><span className="mono">{t.ok ? "ok" : t.error_code}</span></li>
        ))}
      </ol>
    </>
  );
}

function Bars({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map((r) => r[1]));
  if (!rows.length) return <p className="empty">No data yet.</p>;
  return (
    <ul className="bars">
      {rows.map(([k, v]) => (
        <li key={k}><span>{k.replaceAll("_", " ")}</span><i style={{ width: `${(v / max) * 100}%` }} /><b className="mono">{v}</b></li>
      ))}
    </ul>
  );
}

export default function Ops() {
  const init = stored();
  const [token, setToken] = useState(init?.token ?? "");
  const [who, setWho] = useState<Who | null>(init?.who ?? null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ov, setOv] = useState<Overview | null>(null);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canAct = who?.role === "AGENT" || who?.role === "ADMIN";

  const signOutLocal = useCallback((msg?: string) => {
    remember(null); setToken(""); setWho(null); setOv(null); setPassword(""); setError(msg ?? null);
  }, []);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const [o, s] = await Promise.all([opsApi.get<Overview>("/overview", token), opsApi.get<{ sessions: Sess[] }>("/sessions?limit=25", token)]);
      setOv(o); setSessions(s.sessions); setError(null);
      if (canAct) setHandoffs((await opsApi.get<{ handoffs: Handoff[] }>("/handoffs", token)).handoffs);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) signOutLocal("Your session has ended. Please sign in again.");
      else setError(e instanceof ApiError ? e.message : "Failed to load.");
    }
  }, [token, canAct, signOutLocal]);

  const signIn = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    try {
      const r = await opsApi.login(username, password);
      const w = { username: r.username, role: r.role };
      remember({ token: r.token, who: w }); setToken(r.token); setWho(w); setPassword("");
    } catch (err) { setError(err instanceof ApiError ? err.message : "Sign-in failed."); }
    finally { setBusy(false); }
  };

  const signOut = async () => {
    try { await opsApi.logout(token); } catch { /* token may already be gone */ }
    signOutLocal();
  };

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!token) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [token, load]);

  const setStatus = async (id: string, status: "IN_PROGRESS" | "RESOLVED") => {
    try {
      await opsApi.get(`/handoffs/${id}/status`, token, { method: "POST", body: JSON.stringify({ status }) });
      load();
    } catch (e) { setError(e instanceof ApiError ? e.message : "Update failed."); }
  };

  const open = async (id: string) => {
    try { setDetail(await opsApi.get<Detail>(`/sessions/${id}`, token)); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Failed to load session."); }
  };

  if (!token) {
    return (
      <main className="ops gate">
        <form className="gate-card rise" onSubmit={signIn}>
          <p className="eyebrow">Operations console</p>
          <h1>Operator sign-in</h1>
          <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username"
                 autoComplete="username" autoFocus />
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password"
                 autoComplete="current-password" />
          <button className="cta" type="submit" disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
          {error && <div className="banner err" role="alert">{error}</div>}
          {import.meta.env.DEV && <a className="ops-link" href="#/">← Customer view</a>}
        </form>
      </main>
    );
  }

  const kpis: [string, number | string][] = ov ? [
    ["Sessions", ov.sessions], ["Disputes raised", ov.disputes_created], ["Cards blocked", ov.cards_blocked ?? 0],
    ["Self-service", ov.resolved_by_self_service],
    ["Handoffs", ov.human_handoffs], ["Provider errors", ov.provider_errors], ["Policy blocks", ov.blocked_policy_attempts],
    ["Ambiguous", ov.ambiguous_matches], ["p95 latency", ov.latency_ms.p95 != null ? `${ov.latency_ms.p95} ms` : "-"],
  ] : [];

  return (
    <main className="ops">
      <header className="ops-top">
        <div><p className="eyebrow">KoshVaani · Today</p><h1>Operations</h1></div>
        <nav><span className="pill">{who?.username} · {who?.role}</span><button className="ghost sm" onClick={load}>Refresh</button><button className="ghost sm" onClick={signOut}>Sign out</button></nav>
      </header>
      {error && <div className="banner err" role="alert">{error}</div>}

      <section className="kpis">
        {kpis.map(([l, v], i) => (
          <div className="kpi rise" style={{ animationDelay: `${i * 50}ms` }} key={l}><span>{l}</span><b className="mono">{v}</b></div>
        ))}
      </section>

      <section className="grid3">
        <div className="panel"><h3>Outcomes</h3>{ov && <Bars data={ov.sessions_by_outcome} />}</div>
        <div className="panel"><h3>Intents</h3>{ov && <Bars data={ov.intents} />}</div>
        <div className="panel"><h3>Handoff reasons</h3>{ov && <Bars data={ov.handoff_reasons} />}</div>
      </section>

      <section className="grid2">
        {canAct && <div className="panel">
          <h3>Handoff queue <small className="mono">{handoffs.length}</small></h3>
          {handoffs.length === 0 && <p className="empty">Queue is clear.</p>}
          {handoffs.map((h) => (
            <div className="row" key={h.handoff_id}>
              <div><b className="mono">{h.handoff_id}</b><p>{h.reason.replaceAll("_", " ")}</p></div>
              <span className={`pill ${h.status.toLowerCase()}`}>{h.status}</span>
              <div className="acts">
                <button className="ghost sm" onClick={() => setStatus(h.handoff_id, "IN_PROGRESS")}>Take</button>
                <button className="cta sm" onClick={() => setStatus(h.handoff_id, "RESOLVED")}>Resolve</button>
              </div>
            </div>
          ))}
        </div>}

        <div className="panel">
          <h3>Recent sessions</h3>
          {sessions.length === 0 && <p className="empty">No sessions yet.</p>}
          {sessions.map((s) => (
            <button className="row link" key={s.session_id} disabled={!canAct} onClick={() => open(s.session_id)}>
              <div><b className="mono">{s.session_id}</b><p>{s.turn_count} turns</p></div>
              <span className="pill">{s.outcome ?? s.status}</span>
            </button>
          ))}
        </div>
      </section>

      {detail != null && (
        <div className="drawer" onClick={() => setDetail(null)}>
          <aside onClick={(e) => e.stopPropagation()}>
            <button className="ghost sm" onClick={() => setDetail(null)}>Close</button>
            <h3>Session detail</h3>
            <SessionDetail d={detail} />
          </aside>
        </div>
      )}
    </main>
  );
}

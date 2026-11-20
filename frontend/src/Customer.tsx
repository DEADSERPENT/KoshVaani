import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, WS_BASE, type CardT } from "./api";
import { Desk, type CardPending, type Tab } from "./panels";

type Phase = "welcome" | "change" | "call";
/** Balance figures stay veiled in the transcript exactly like the Balance card, until the customer taps Show amounts. */
const AMOUNT = /\d[\d,]*(?:\.\d+)?\s*rupees(?:\s+\d+\s*paise)?/gi;
const veil = (text: string) => text.replace(AMOUNT, "•••• rupees");

type Msg = { id: number; role: "you" | "kosh"; text: string; intent?: string; state?: string };
type Ev = { t: string; type: string; detail: string };
type Pending = { kind: "DISPUTE" | "CARD_BLOCK"; prepId: string; via: "ws" | "rest"; text?: string; last4?: string };

// A successful agent tool result opens the matching panel, so "what's my balance?" also shows it.
const TOOL_TAB: Record<string, Tab> = {
  get_account_balance: "balance", get_mini_statement: "statement", list_cards: "cards",
  prepare_card_block: "cards", get_loan_summary: "loans",
};
const IDLE_MS = 3 * 60_000;  // end the call after 3 minutes with no touch, key or tap
const PROMPTS = ["What's my balance?", "Show my mini statement", "My card was stolen, block it",
  "When is my next EMI due?", "My UPI payment failed"];

const STATE_LABEL: Record<string, string> = {
  LISTENING: "Listening", TRANSCRIBING: "Transcribing", UNDERSTANDING: "Thinking", SPEAKING: "Speaking",
  HANDOFF: "Handing off", IDLE: "Ready",
};

const b64ToBlob = (parts: string[], type: string) => {
  const bytes = parts.map((p) => Uint8Array.from(atob(p), (c) => c.charCodeAt(0)));
  return new Blob(bytes as BlobPart[], { type });
};

export default function Customer() {
  const [phase, setPhase] = useState<Phase>("welcome");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [session, setSession] = useState<{ id: string; token: string } | null>(null);
  const [consent, setConsent] = useState(false);
  const [password, setPassword] = useState("");
  const [newPw, setNewPw] = useState("");
  const [newPw2, setNewPw2] = useState("");
  const [who, setWho] = useState<{ first: string } | null>(null);
  const DEV = import.meta.env.DEV;  // false in every production build: dev-only UI is stripped, not just hidden

  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [agent, setAgent] = useState("IDLE");
  const [pending, setPending] = useState<Pending | null>(null);
  const [tab, setTab] = useState<Tab | null>(null);
  const [hidden, setHidden] = useState(true);
  useEffect(() => {  // re-veil balances whenever the tab is left, as the Balance card always did
    const hide = () => { if (document.hidden) setHidden(true); };
    document.addEventListener("visibilitychange", hide);
    return () => document.removeEventListener("visibilitychange", hide);
  }, []);
  const [nonce, setNonce] = useState(0);
  const [sealed, setSealed] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [handoffId, setHandoffId] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [text, setText] = useState("");
  const [dev, setDev] = useState(false);
  const [events, setEvents] = useState<Ev[]>([]);
  const [latency, setLatency] = useState<Record<string, number> | null>(null);

  const ws = useRef<WebSocket | null>(null);
  const rec = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Record<number, string>>({});
  const audioType = useRef("audio/mpeg");
  const mid = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);

  const validPhone = /^[6-9]\d{9}$/.test(ref);
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "Something went wrong.");
  const log = (type: string, detail = "") =>
    setEvents((p) => [...p.slice(-60), { t: new Date().toLocaleTimeString(), type, detail }]);
  const push = (m: Omit<Msg, "id">) => setMsgs((p) => [...p, { ...m, id: ++mid.current }]);

  useEffect(() => { logRef.current?.scrollTo({ top: 1e6, behavior: "smooth" }); }, [msgs]);
  useEffect(() => () => { const s = ws.current; ws.current = null; s?.close(); }, []);

  const play = useCallback(() => {
    const parts = Object.keys(chunks.current).map(Number).sort((a, b) => a - b).map((k) => chunks.current[k]);
    chunks.current = {};
    if (!parts.length) return;
    const url = URL.createObjectURL(b64ToBlob(parts, audioType.current));
    const a = new Audio(url);
    a.onended = () => URL.revokeObjectURL(url);
    a.play().catch(() => log("audio.blocked", "Browser blocked autoplay"));
  }, []);

  const onMessage = useCallback((raw: MessageEvent) => {
    const m = JSON.parse(raw.data);
    log(m.type, m.state ?? m.tool ?? m.code ?? "");
    switch (m.type) {
      case "assistant.state": setAgent(m.state); break;
      case "transcript.final": push({ role: "you", text: m.text }); break;
      case "assistant.message":
        push({ role: "kosh", text: m.text, intent: m.intent, state: m.state });
        setPending(m.state === "READY_TO_CONFIRM" && m.preparation_id
          ? { kind: m.pending_action === "CARD_BLOCK" ? "CARD_BLOCK" : "DISPUTE", prepId: m.preparation_id, via: "ws" } : null);
        break;
      case "tool.result":
        if (m.status === "success" && TOOL_TAB[m.tool]) { setTab(TOOL_TAB[m.tool]); setNonce((n) => n + 1); }
        break;
      case "case.created": setCaseId(m.dispute_id); setPending(null); break;
      case "card.blocked": setSealed("Card blocked"); setPending(null); setTab("cards"); setNonce((n) => n + 1); break;
      case "handoff.created": setHandoffId(m.handoff_id); setPending(null); break;
      case "audio.chunk":
        audioType.current = m.content_type; chunks.current[m.seq] = m.data;
        if (m.final) play();
        break;
      case "turn.completed": setLatency(m.latency_ms); setAgent("IDLE"); break;
      case "error": setError(m.message); setAgent("IDLE"); break;
      case "session.completed": setAgent("IDLE"); break;
    }
  }, [play]);

  const enterCall = async (sess: { id: string; token: string }, displayName: string) => {
    setWho({ first: displayName.split(" ")[0] });  // full name and phone are never kept or shown
    // A 30-second single-use ticket goes in the URL; the session token itself never does.
    const { ticket } = await api.streamTicket(sess.token);
    const sock = new WebSocket(`${WS_BASE}/api/v1/voice/sessions/${sess.id}/stream?ticket=${ticket}`);
    sock.onmessage = onMessage;
    sock.onclose = (e) => {
      if (e.code === 4401) setError("Session expired. Please start again.");
      else if (ws.current === sock) setError("Connection lost. End the call and start again.");
    };
    ws.current = sock;
    setPassword(""); setNewPw(""); setNewPw2("");  // never kept in memory longer than needed
    setPhase("call");
  };

  const begin = async () => {
    setBusy(true); setError(null);
    try {
      const s = await api.createSession(consent);
      const sess = { id: s.session_id, token: s.session_token };
      setSession(sess);
      const v = await api.login(ref, password, sess.token);
      if (v.must_change_password) {
        setWho({ first: v.customer_display_name.split(" ")[0] });
        setPhase("change");
      } else await enterCall(sess, v.customer_display_name);
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) setError("Too many attempts. Please wait a few minutes and try again.");
      else fail(e);
    } finally { setBusy(false); }
  };

  const changePassword = async () => {
    if (!session) return;
    setBusy(true); setError(null);
    try {
      await api.changePassword(password, newPw, session.token);
      await enterCall(session, who?.first ?? "");
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const send = (o: unknown) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(JSON.stringify(o));

  const sendText = (t: string) => {
    if (!t.trim()) return;
    setError(null); push({ role: "you", text: t }); send({ type: "text.input", text: t }); setText("");
  };

  const answer = async (value: boolean) => {
    if (!pending || !session) return;
    const p = pending;
    setPending(null);
    push({ role: "you", text: value ? "Yes, go ahead." : "No, cancel." });
    if (p.via === "ws") { send({ type: "client.confirmation", value, preparation_id: p.prepId }); return; }
    try {
      const r = await api.confirmCardBlock(session.token, p.prepId, value);
      push({ role: "kosh", text: r.message });
      if (r.blocked) setSealed(`Card ending ${p.last4} blocked`);
      setTab("cards"); setNonce((n) => n + 1);
    } catch (e) {
      if (value) fail(e); else push({ role: "kosh", text: "Okay, I have not blocked the card." });
    }
  };

  const fromDesk = (c: CardPending) => {
    setError(null); setSealed(null);
    setPending({ kind: "CARD_BLOCK", prepId: c.prepId, via: "rest", text: c.text, last4: c.cardLast4 });
  };

  const cardHelp = async (c: CardT) => {
    if (!session) return;
    try {
      const h = await api.handoff(session.token, "CARD_UNBLOCK_REQUEST", `Customer asked for help with a ${c.status.toLowerCase()} card`);
      setHandoffId(h.handoff_id);
    } catch (e) { fail(e); }
  };

  const toggleMic = async () => {
    setError(null);
    if (recording) { rec.current?.stop(); setRecording(false); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const type = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
      const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const parts: Blob[] = [];
      r.ondataavailable = (e) => { if (e.data.size) parts.push(e.data); };
      r.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const buf = await new Blob(parts, { type: r.mimeType }).arrayBuffer();
        send({ type: "audio.start", content_type: (r.mimeType || "audio/webm").split(";")[0] });
        for (let i = 0; i < buf.byteLength; i += 65536) ws.current?.send(buf.slice(i, i + 65536));
        send({ type: "audio.end" });
      };
      r.start(); rec.current = r; setRecording(true);
    } catch { setError("Microphone unavailable. Type your message instead."); }
  };

  const human = async () => {
    if (!session) return;
    try {
      const h = await api.handoff(session.token, "CUSTOMER_REQUESTED", "Requested from web call screen");
      setHandoffId(h.handoff_id);
    } catch (e) { fail(e); }
  };

  const lastActive = useRef(Date.now());
  useEffect(() => {
    if (phase !== "call") return;
    const touch = () => { lastActive.current = Date.now(); };
    const evs = ["pointerdown", "keydown", "touchstart"] as const;
    evs.forEach((e) => window.addEventListener(e, touch, { passive: true }));
    const timer = setInterval(() => {
      if (Date.now() - lastActive.current > IDLE_MS) {
        hangUp().then(() => setError("You were signed out after a few minutes of inactivity."));
      }
    }, 15_000);
    return () => { evs.forEach((e) => window.removeEventListener(e, touch)); clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  const hangUp = async () => {
    const s = ws.current; ws.current = null; s?.close();  // detach first so onclose does not report a lost connection
    if (session) await api.endSession(session.id, session.token).catch(() => null);
    setPhase("welcome"); setSession(null); setPassword(""); setNewPw(""); setNewPw2(""); setMsgs([]); setPending(null);
    setTab(null); setSealed(null); setCaseId(null); setHandoffId(null); setEvents([]); setLatency(null); setAgent("IDLE"); setRecording(false);
  };

  return (
    <main className="shell">
      <section className="stage">
        <header className="brand">
          <span className="brand-dev" lang="hi">कोश वाणी</span>
          <h1 className="brand-lat">KOSH<br />VAANI</h1>
          <p className="brand-tag">Speak to your bank.<br />It listens, checks, and acts, with your yes.</p>
        </header>

        <div className={`orb ${recording ? "rec" : agent.toLowerCase()}`} aria-live="polite">
          <i /><i /><i /><b />
          <span>{recording ? "Recording" : STATE_LABEL[agent] ?? agent}</span>
        </div>

        {phase === "call" && (
          <div className="controls">
            <button className={`mic ${recording ? "on" : ""}`} onClick={toggleMic}>
              {recording ? "Tap to send" : "Tap to speak"}
            </button>
            <button className="ghost" onClick={human}>Talk to a human</button>
            <button className="ghost danger" onClick={hangUp}>End call</button>
          </div>
        )}
        {DEV && <a className="ops-link" href="#/ops">Operations console →</a>}
      </section>

      <section className="paper">
        {phase === "welcome" && (
          <div className="card rise">
            <p className="eyebrow">Step 01 · Identify yourself</p>
            <h2>Sign in with your mobile number and password.</h2>
            <div className="phonefield">
              <span className="mono">+91</span>
              <input className="mono" name="phone" type="tel" inputMode="numeric" autoComplete="tel-national" value={ref} autoFocus
                     placeholder="98765 43210" maxLength={10}
                     onChange={(e) => setRef(e.target.value.replace(/\D/g, "").slice(0, 10))}
                     onKeyDown={(e) => { if (e.key === "Enter" && validPhone && consent) begin(); }} />
            </div>
            <input className="pw" name="password" type="password" autoComplete="current-password" value={password}
                   placeholder="Password" maxLength={128} onChange={(e) => setPassword(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && validPhone && password && consent) begin(); }} />
            <label className="consent">
              <input type="checkbox" name="consent" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
              <span>I agree to use my microphone and to my voice being processed by our speech providers to help with my banking query.
                I will never say my PIN or password aloud. Recordings are not kept; call text is deleted after 7 days.</span>
            </label>
            <button className="cta" disabled={busy || !validPhone || !password || !consent} onClick={begin}>{busy ? "Signing in…" : "Start the call"}</button>
          </div>
        )}

        {phase === "change" && (
          <div className="card rise">
            <p className="eyebrow">Step 02 · Choose your password</p>
            <h2>Hello, {who?.first}. Set a new password.</h2>
            <p className="fine">You signed in with a temporary password. Choose your own to continue: at least 10 characters, not only digits.</p>
            <input className="pw" name="new-password" type="password" autoComplete="new-password" value={newPw} autoFocus
                   placeholder="New password" maxLength={128} onChange={(e) => setNewPw(e.target.value)} />
            <input className="pw" name="new-password-repeat" type="password" autoComplete="new-password" value={newPw2}
                   placeholder="Repeat the new password" maxLength={128} onChange={(e) => setNewPw2(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && newPw.length >= 10 && newPw === newPw2) changePassword(); }} />
            {newPw2 && newPw !== newPw2 && <p className="banner err" role="alert">The two passwords do not match.</p>}
            <button className="cta" disabled={busy || newPw.length < 10 || newPw !== newPw2} onClick={changePassword}>
              {busy ? "Saving…" : "Save and continue"}</button>
          </div>
        )}

        {phase === "call" && (
          <div className="call rise">
            <div className="call-head">
              <div><p className="eyebrow">Verified</p><h2>Hello, {who?.first}</h2></div>
              {DEV && <button className="ghost sm" onClick={() => setDev(!dev)}>{dev ? "Hide" : "Show"} dev panel</button>}
            </div>

            <div className="log" ref={logRef}>
              {msgs.length === 0 && (
                <div className="starters">
                  <p className="empty">Say it, type it, or tap one:</p>
                  {PROMPTS.map((p) => <button key={p} className="chip-s" onClick={() => sendText(p)}>{p}</button>)}
                </div>
              )}
              {msgs.map((m) => (
                <div key={m.id} className={`bubble ${m.role}`}>
                  <span>{m.role === "you" ? "You" : "Kosh"}</span>
                  <p>{m.role === "kosh" && m.intent === "BALANCE_ENQUIRY" && hidden ? veil(m.text) : m.text}</p>
                  {m.role === "kosh" && m.intent === "BALANCE_ENQUIRY" && m.text.search(AMOUNT) >= 0 && (
                    <button className="reveal" onClick={() => setHidden(!hidden)}>{hidden ? "Show amounts" : "Hide amounts"}</button>
                  )}
                </div>
              ))}
            </div>

            {session && (
              <Desk token={session.token} tab={tab} nonce={nonce} hidden={hidden} setHidden={setHidden} onTab={setTab} onPending={fromDesk} onHelp={cardHelp} />
            )}
            {sealed && <div className="banner sealed" role="status"><span className="stamp">SEALED</span>{sealed}. To unblock or replace it, ask for a human agent.</div>}
            {caseId && <div className="banner ok">Case raised <b className="mono">{caseId}</b></div>}
            {handoffId && <div className="banner">A human agent is queued · <b className="mono">{handoffId}</b></div>}
            {pending && (
              <div className={`confirm ${pending.kind === "CARD_BLOCK" ? "card" : ""}`} role="alertdialog" aria-label="Please confirm">
                <p>{pending.text ?? (pending.kind === "CARD_BLOCK" ? "Shall I block this card?" : "Shall I raise this dispute?")}</p>
                <button className="cta sm" onClick={() => answer(true)}>{pending.kind === "CARD_BLOCK" ? "Yes, block it" : "Yes, raise it"}</button>
                <button className="ghost sm" onClick={() => answer(false)}>No</button>
              </div>
            )}

            <form className="typebar" onSubmit={(e) => { e.preventDefault(); sendText(text); }}>
              <input name="message" value={text} onChange={(e) => setText(e.target.value)} placeholder="Or type here…"
                     autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} maxLength={1000} />
              <button className="cta sm" type="submit">Send</button>
            </form>

            {DEV && dev && (
              <aside className="dev mono">
                <p>latency {latency ? `asr ${latency.asr ?? "-"} · tts ${latency.tts ?? "-"} · total ${latency.total ?? "-"} ms` : "-"}</p>
                {msgs.filter((m) => m.intent).slice(-1).map((m) => <p key={m.id}>intent {m.intent} · {m.state}</p>)}
                {events.slice(-12).map((e, i) => <p key={i}>{e.t} {e.type} {e.detail}</p>)}
              </aside>
            )}
          </div>
        )}
        {error && <div className="banner err" role="alert">{error}</div>}
      </section>
    </main>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, rupees, type Account, type BlockReason, type CardT, type Loan, type Txn } from "./api";

export type Tab = "balance" | "statement" | "cards" | "loans";
export const TABS: { id: Tab; label: string }[] = [
  { id: "balance", label: "Balance" }, { id: "statement", label: "Statement" },
  { id: "cards", label: "Cards" }, { id: "loans", label: "Loans" },
];

export type CardPending = { prepId: string; text: string; cardLast4: string };

type Remote<T> = { status: "loading" | "ready" | "error"; data: T | null; error?: string };
type Store = { balance?: Remote<Account[]>; statement?: Remote<Txn[]>; cards?: Remote<CardT[]>; loans?: Remote<Loan[]> };

const REASONS: { id: BlockReason; label: string }[] = [
  { id: "LOST", label: "Lost" }, { id: "STOLEN", label: "Stolen" },
  { id: "SUSPECTED_FRAUD", label: "Fraud" }, { id: "OTHER", label: "Other" },
];

const shortDate = (iso: string) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
const daysUntil = (iso: string) => Math.ceil((new Date(iso + "T00:00:00").getTime() - Date.now()) / 86_400_000);

function Balance({ data, hidden, toggle }: { data: Account[]; hidden: boolean; toggle: () => void }) {
  if (!data.length) return <p className="empty">No account found on your profile.</p>;
  return (
    <div className="balances">
      {data.map((a) => (
        <article className="passbook" key={a.account_id}>
          <header>
            <span className="eyebrow">{a.account_type.toLowerCase()} account</span>
            <span className="mono">{a.account_number_masked}</span>
          </header>
          <p className={`big ${hidden ? "veil" : ""}`} aria-label={hidden ? "Balance hidden" : a.balance_text}>
            {hidden ? "₹ ••,••,•••" : rupees(a.balance_paise)}
          </p>
          <footer>
            <span>Available</span>
            <i className="leader" />
            <b className={`mono ${hidden ? "veil" : ""}`}>{hidden ? "•••••" : rupees(a.available_paise)}</b>
          </footer>
          {a.available_paise !== a.balance_paise && !hidden && (
            <p className="fine">{rupees(a.balance_paise - a.available_paise)} is on hold for a card authorisation.</p>
          )}
          <button className="ghost sm" onClick={toggle}>{hidden ? "Show amounts" : "Hide amounts"}</button>
        </article>
      ))}
    </div>
  );
}

function Statement({ data }: { data: Txn[] }) {
  if (!data.length) return <p className="empty">No recent transactions.</p>;
  return (
    <ol className="ledger">
      {data.map((t, i) => (
        <li key={t.transaction_id} style={{ animationDelay: `${i * 60}ms` }}>
          <span className="when mono">{shortDate(t.transaction_at)}</span>
          <span className="what"><b>{t.payee_label}</b><em className="mono">{t.channel}{t.status !== "SUCCESS" ? ` · ${t.status.replaceAll("_", " ").toLowerCase()}` : ""}</em></span>
          <i className="leader" />
          <b className={`amt mono ${t.direction === "CREDIT" ? "cr" : "dr"}`}>
            {t.direction === "CREDIT" ? "+" : "−"}{rupees(t.amount_paise)}
          </b>
        </li>
      ))}
    </ol>
  );
}

function Loans({ data }: { data: Loan[] }) {
  if (!data.length) return <p className="empty">No loans on your profile.</p>;
  return (
    <div className="loans">
      {data.map((l) => {
        const d = daysUntil(l.next_due_date);
        return (
          <article className="loan" key={l.loan_id}>
            <header><span className="eyebrow">{l.loan_type.toLowerCase()} loan</span><span className="pill">{l.status}</span></header>
            <p className="big">{rupees(l.outstanding_paise)}<small> outstanding</small></p>
            <p className="emi">Next EMI <b className="mono">{rupees(l.emi_paise)}</b> · <span className={d <= 3 ? "soon" : ""}>
              {d < 0 ? "overdue" : d === 0 ? "due today" : `due in ${d} day${d === 1 ? "" : "s"}`}</span> · {shortDate(l.next_due_date)}</p>
            <p className="fine">{l.emis_remaining} EMIs remaining. Prepayment and restructuring need a human agent.</p>
          </article>
        );
      })}
    </div>
  );
}

function Cards({ data, busy, onBlock, onHelp }: {
  data: CardT[]; busy: string | null; onBlock: (c: CardT, r: BlockReason) => void; onHelp: (c: CardT) => void;
}) {
  const [picking, setPicking] = useState<string | null>(null);
  if (!data.length) return <p className="empty">No cards on your profile.</p>;
  return (
    <div className="cardrack">
      {data.map((c) => (
        <article className={`plastic ${c.card_type.toLowerCase()} ${c.status.toLowerCase()}`} key={c.card_id}>
          <header><span>{c.card_type === "DEBIT" ? "Debit" : "Credit"}</span><span>{c.network}</span></header>
          <p className="pan mono">•••• •••• •••• {c.last4}</p>
          <footer>
            {c.status === "ACTIVE" && picking !== c.card_id && (
              <button className="ghost sm" disabled={busy !== null} onClick={() => setPicking(c.card_id)}>Block card</button>
            )}
            {c.status === "ACTIVE" && picking === c.card_id && (
              <div className="reasons" role="group" aria-label="Why are you blocking this card?">
                {REASONS.map((r) => (
                  <button key={r.id} className="chip-s" disabled={busy !== null}
                          onClick={() => { setPicking(null); onBlock(c, r.id); }}>{r.label}</button>
                ))}
                <button className="chip-s x" onClick={() => setPicking(null)} aria-label="Cancel">×</button>
              </div>
            )}
            {c.status !== "ACTIVE" && <button className="ghost sm" onClick={() => onHelp(c)}>Contact an agent</button>}
          </footer>
          {c.status !== "ACTIVE" && <div className="seal" aria-label={`Card ${c.status.toLowerCase()}`}>{c.status}</div>}
        </article>
      ))}
    </div>
  );
}

/** Account panels. Loaded only when opened (or when the agent just answered with that data), so a balance is never
 *  fetched, and never audited as viewed, unless the customer asked for it. */
export function Desk({ token, tab, nonce, hidden, setHidden, onTab, onPending, onHelp }: {
  token: string; tab: Tab | null; nonce: number; hidden: boolean; setHidden: (h: boolean) => void;
  onTab: (t: Tab | null) => void;
  onPending: (p: CardPending) => void; onHelp: (c: CardT) => void;
}) {
  const [store, setStore] = useState<Store>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async (t: Tab) => {
    const mine = ++seq.current;
    const put = (v: Remote<any>) => mine === seq.current && setStore((s) => ({ ...s, [t]: v }));
    put({ status: "loading", data: store[t]?.data ?? null });
    try {
      const data = t === "balance" ? await api.accounts(token) : t === "cards" ? await api.cards(token)
        : t === "loans" ? await api.loans(token) : (await api.statement(token)).transactions;
      put({ status: "ready", data });
    } catch (e) {
      put({ status: "error", data: null, error: e instanceof ApiError ? e.message : "Could not load this." });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => { if (tab) load(tab); }, [tab, nonce, load]);
  const block = async (c: CardT, reason: BlockReason) => {
    setBusy(c.card_id); setNotice(null);
    try {
      const p = await api.prepareCardBlock(token, c.card_id, reason);
      onPending({ prepId: p.preparation_id, text: p.confirmation_text, cardLast4: c.last4 });
    } catch (e) { setNotice(e instanceof ApiError ? e.message : "Could not start that."); }
    finally { setBusy(null); }
  };

  const cur = tab ? store[tab] : undefined;
  return (
    <section className="desk" aria-label="Your accounts">
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""}
                  onClick={() => onTab(tab === t.id ? null : t.id)}>{t.label}</button>
        ))}
      </div>
      {!tab && <p className="fine desk-hint">Open a tab to view it, or just ask. Nothing is fetched until you do.</p>}
      {tab && (
        <div className="pane" role="tabpanel">
          {cur?.status === "loading" && !cur.data && <p className="empty">Fetching…</p>}
          {cur?.status === "error" && <p className="banner err" role="alert">{cur.error}</p>}
          {notice && <p className="banner err" role="alert">{notice}</p>}
          {cur?.data && tab === "balance" && <Balance data={cur.data as Account[]} hidden={hidden} toggle={() => setHidden(!hidden)} />}
          {cur?.data && tab === "statement" && <Statement data={cur.data as Txn[]} />}
          {cur?.data && tab === "loans" && <Loans data={cur.data as Loan[]} />}
          {cur?.data && tab === "cards" && <Cards data={cur.data as CardT[]} busy={busy} onBlock={block} onHelp={onHelp} />}
        </div>
      )}
    </section>
  );
}

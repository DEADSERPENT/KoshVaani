import { API_BASE, WS_BASE, ApiError, call, post } from "./http";

export { API_BASE, WS_BASE, ApiError };

export type Account = {
  account_id: string; account_type: string; account_number_masked: string; currency: string; status: string;
  balance_paise: number; available_paise: number; balance_text: string; available_text: string;
};
export type CardT = {
  card_id: string; card_type: "DEBIT" | "CREDIT"; network: string; last4: string;
  status: "ACTIVE" | "BLOCKED" | "EXPIRED"; blocked_reason: string | null;
};
export type Loan = {
  loan_id: string; loan_type: string; status: string; outstanding_paise: number; emi_paise: number;
  next_due_date: string; emis_remaining: number;
};
export type Txn = {
  transaction_id: string; channel: string; direction: "DEBIT" | "CREDIT"; amount_paise: number;
  transaction_at: string; status: string; payee_label: string; utr_masked: string;
};
export type BlockReason = "LOST" | "STOLEN" | "SUSPECTED_FRAUD" | "OTHER";

export const api = {
  createSession: (consent: boolean) => call<{ session_id: string; session_token: string; language: string }>(
    "/sessions", post({ channel: "web", language: "en", consent_microphone: consent, consent_notice: consent })),
  endSession: (id: string, token: string) => call(`/sessions/${id}/end`, { method: "POST", token }),
  challenge: (phone: string, token: string) => call<{
    challenge_id: string; delivery_mode: string; expires_in_seconds: number; message: string; dev_code: string | null;
  }>("/auth/challenges", { ...post({ phone }), token }),
  verify: (challengeId: string, code: string, token: string) => call<{
    authenticated: boolean; customer_display_name: string; phone_masked: string;
  }>(`/auth/challenges/${challengeId}/verify`, { ...post({ code }), token }),
  login: (phone: string, password: string, token: string) => call<{
    authenticated: boolean; customer_display_name: string; phone_masked: string; must_change_password: boolean;
  }>("/auth/login", { ...post({ phone, password }), token }),
  changePassword: (current: string, next: string, token: string) => call<{ changed: boolean }>(
    "/auth/password", { ...post({ current_password: current, new_password: next }), token }),
  streamTicket: (token: string) => call<{ ticket: string; expires_in_seconds: number }>(
    "/voice/ws-ticket", { method: "POST", token }),
  handoff: (token: string, reason: string, summary?: string) =>
    call<{ handoff_id: string }>("/handoffs", { ...post({ reason, summary }), token }),
  accounts: (token: string) => call<Account[]>("/accounts", { token }),
  cards: (token: string) => call<CardT[]>("/cards", { token }),
  loans: (token: string) => call<Loan[]>("/loans", { token }),
  statement: (token: string) => call<{ transactions: Txn[]; count: number }>("/transactions?limit=5", { token }),
  prepareCardBlock: (token: string, card_id: string, reason: BlockReason) => call<{
    preparation_id: string; card: CardT; confirmation_text: string; expires_in_seconds: number;
  }>("/cards/block/prepare", { ...post({ card_id, reason }), token }),
  confirmCardBlock: (token: string, preparation_id: string, customer_confirmation: boolean) => call<{
    blocked: boolean; card: CardT; message: string;
  }>("/cards/block/confirm", { ...post({ preparation_id, customer_confirmation }), token }),
};

export const rupees = (paise: number) => `₹${(paise / 100).toLocaleString("en-IN")}`;

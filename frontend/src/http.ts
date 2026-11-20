export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
export const WS_BASE = API_BASE.replace(/^http/, "ws");
const V1 = `${API_BASE}/api/v1`;

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number,
              public retryable = false, public details?: Record<string, unknown>) {
    super(message);
  }
}

export async function call<T>(path: string, init: RequestInit & { token?: string } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (init.token) headers.Authorization = `Bearer ${init.token}`;
  let res: Response;
  try {
    res = await fetch(`${V1}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("NETWORK", "Cannot reach the KoshVaani server. Is the backend running?", 0, true);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const e = body?.error;
    throw new ApiError(e?.code ?? "UNKNOWN", e?.message ?? `Request failed (${res.status})`, res.status,
      !!e?.retryable, e?.details);
  }
  return res.json() as Promise<T>;
}

export const post = (b: unknown) => ({ method: "POST", body: JSON.stringify(b) });

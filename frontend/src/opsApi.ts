import { call, post } from "./http";

export type OpsRole = "VIEWER" | "AGENT" | "ADMIN";

export const opsApi = {
  login: (username: string, password: string) => call<{
    token: string; username: string; role: OpsRole; expires_in_seconds: number;
  }>("/ops/auth/login", post({ username, password })),
  logout: (token: string) => call("/ops/auth/logout", { method: "POST", token }),
  get: <T>(path: string, token: string, init: RequestInit = {}) => call<T>(`/ops${path}`, { ...init, token }),
};

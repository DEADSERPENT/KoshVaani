import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/** Production builds only: a strict CSP pinned to the one API origin, plus a no-referrer policy. A <meta> CSP cannot
 *  set frame-ancestors, so the host must also send the headers listed in docs/frontend-security.md. */
function hardening(apiBase: string): Plugin {
  return {
    name: "koshvaani-hardening",
    apply: "build",
    transformIndexHtml() {
      const api = new URL(apiBase);
      const ws = `${api.protocol === "https:" ? "wss:" : "ws:"}//${api.host}`;
      const csp = [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",  // inline: React style={{}} attributes only
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        `connect-src ${api.origin} ${ws}`,
        "media-src blob:",
        "object-src 'none'", "base-uri 'none'", "form-action 'none'",
      ].join("; ");
      return [
        { tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: csp }, injectTo: "head-prepend" },
        { tag: "meta", attrs: { name: "referrer", content: "no-referrer" }, injectTo: "head-prepend" },
      ];
    },
    buildStart() {
      if (!apiBase.startsWith("https://") && !process.env.ALLOW_INSECURE_API) {
        throw new Error(
          `VITE_API_BASE is "${apiBase}". A production build must talk to the API over https. ` +
          "For a local-only build set ALLOW_INSECURE_API=1.");
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const apiBase = env.VITE_API_BASE ?? "http://127.0.0.1:8000";
  return {
    plugins: [react(), hardening(apiBase)],
    server: { port: 5173, strictPort: true },
    build: { outDir: mode === "ops" ? "dist-ops" : "dist", sourcemap: false },
  };
});

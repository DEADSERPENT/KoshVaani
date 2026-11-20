# Frontend security and deployment

The browser is untrusted. Everything the frontend holds, an attacker with a script in the page (or a person at the
screen) can read. So the frontend holds as little as possible and the backend decides everything.

## What the frontend does

| Risk | Control | Verified |
|---|---|---|
| Phone number or full name on screen, in screenshots or recordings | Never displayed after login. Header shows the first name only; the OTP page shows no number | browser check on the production build |
| Operator console shipped to the public | Two separate builds. `npm run build` (customer, `dist/`) contains no ops code; `npm run build:ops` (`dist-ops/`) contains no customer code. Deploy them to different hosts | grep of both bundles |
| Dev helpers reaching customers (dev panel with intents/tool names, test-customer chips, on-screen OTP, ops link) | Behind `import.meta.env.DEV`, so they are removed from production bundles, not just hidden. The dev OTP is also dropped before it reaches React state | grep of the customer bundle |
| Operator token stolen by injected script | Held in memory only, never in `localStorage`/`sessionStorage`. Signing in again after a refresh is intended. A legacy stored token is purged on load | code + browser check |
| Customer session token | Held in React state only; gone on refresh or End call | code |
| Balance visible to shoulder-surfers / screen share | Balance amounts are masked, on the Balance card and in the agent's reply text in the transcript, until the customer taps Show amounts; they re-mask whenever the tab is hidden (spoken audio cannot be masked) | browser check |
| Unattended open session | Call ends after 3 minutes without a touch, key or tap; chat and panels are cleared | code |
| Script injection / data exfiltration | Production build injects a strict CSP: `default-src 'none'`, scripts from self only, `connect-src` pinned to the one API origin (https/wss), no objects, no base tag, no forms | browser run under the CSP: 0 violations |
| URL leakage via Referer | `referrer: no-referrer` | html check |
| Plain-http API in production | The build fails unless `VITE_API_BASE` is `https://` (override for local-only builds: `ALLOW_INSECURE_API=1`) | build test |
| Browser autofill / cloud spellcheck keeping banking text | Chat box: `autocomplete=off`, `spellcheck=false`; OTP field `one-time-code` | code |

## What the host must also do (cannot be set from frontend code)

Send these response headers for the customer site, from your CDN or reverse proxy:

```
Strict-Transport-Security: max-age=63072000; includeSubDomains
Content-Security-Policy: frame-ancestors 'none'          # a <meta> CSP cannot set this
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy: camera=(), geolocation=(), microphone=(self)
Cache-Control: no-store                                  # on index.html, so a logged-out page is never replayed
```

- Serve the ops build only on an internal hostname, behind a VPN or IP allow-list and SSO if you have one. Do not link
  to it from the public site. The backend already rejects customer tokens on ops routes, but a login page that is not
  on the internet cannot be brute-forced from it.
- Set the backend `FRONTEND_ORIGIN` to exactly the customer site's https origin (CORS). The production start-up lock
  already requires https.
- Put rate limiting / a WAF in front of `/auth/challenges` (OTP request flooding costs money and enables SMS abuse).

## Rules for anyone changing the frontend

- No secrets in `VITE_*` variables or source: they are public. API keys for Groq, Deepgram, SMS stay on the server.
- Never render, log, or store phone numbers, account numbers, OTPs or tokens. Use `rupees()` for amounts and the
  already-masked fields from the API (`account_number_masked`, `last4`).
- Do not add `console.log` of API responses, analytics or session-replay tools to the customer build: they would
  capture exactly the data above.
- Test fixtures (`9900000101` etc.) are fictional and exist only in `import.meta.env.DEV` code. The backend seed must
  never run in production; the production start-up lock enforces that.

## Known gaps

- **Google Fonts** are loaded from `fonts.googleapis.com`, so Google sees each visitor's IP and referrer-less request.
  For a bank, self-host the fonts (e.g. `@fontsource` packages) and drop the two Google hosts from the CSP in
  `vite.config.ts`.
- The customer token is a bearer token in JSON and the WebSocket token travels in the query string (see the threat
  model). A cookie-based `HttpOnly; Secure; SameSite=Strict` session would remove token handling from JavaScript.
- No automated UI or accessibility tests; the checks above were made by hand in a browser.
- The CSP allows inline `style` attributes (`'unsafe-inline'` in `style-src`) because React `style={{}}` is used for
  small dynamic values. Scripts are not affected.

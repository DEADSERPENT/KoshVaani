import { lazy, Suspense, useEffect, useState } from "react";

// Which console a build contains is fixed at build time (VITE_APP_MODE), so the public customer site never ships
// the operator console's code. `npm run dev` includes both for local work.
//   customer: `npm run build`      -> dist/      (deploy to the public site)
//   ops:      `npm run build:ops`  -> dist-ops/   (deploy separately, behind your network controls)
const MODE: string = import.meta.env.VITE_APP_MODE ?? (import.meta.env.DEV ? "all" : "customer");
const WANT_CUSTOMER = MODE === "all" || MODE === "customer";
const WANT_OPS = MODE === "all" || MODE === "ops";

const Customer = WANT_CUSTOMER ? lazy(() => import("./Customer")) : null;
const Ops = WANT_OPS ? lazy(() => import("./Ops")) : null;

const route = () => (WANT_OPS && (!WANT_CUSTOMER || location.hash.startsWith("#/ops")) ? "ops" : "customer");

export default function App() {
  const [view, setView] = useState(route());
  useEffect(() => {
    const on = () => setView(route());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const Page = view === "ops" ? Ops : Customer;
  return Page ? <Suspense fallback={null}><Page /></Suspense> : null;
}

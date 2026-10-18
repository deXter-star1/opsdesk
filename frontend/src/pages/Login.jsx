import { useState } from "react";
import { api } from "../lib/api.js";
import { cache } from "../lib/queryCache.js";

const DEMO = [
  ["priya", "Priya Sharma", "Support lead · Engineering member"],
  ["rahul", "Rahul Verma", "Support member"],
  ["meera", "Meera Iyer", "Payments lead (approver)"],
  ["arjun", "Arjun Nair", "Payments + Support member"],
  ["kabir", "Kabir Singh", "Engineering lead · SRE member"],
  ["neha", "Neha Kulkarni", "SRE lead · Engineering member"],
  ["vikram", "Vikram Rao", "Compliance lead · Payments viewer"],
  ["isha", "Isha Patel", "Support viewer · Compliance member"],
  ["sam", "Sam Thomas", "No team: can only raise requests"],
  ["admin", "Asha Admin", "Admin (jobs page) · Ops lead"],
];

export function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(false);

  const login = async (e, as) => {
    e?.preventDefault();
    setPending(true);
    setError(null);
    try {
      await api("/auth/login", {
        method: "POST",
        body: as ? { email: `${as}@opsdesk.test`, password: "demo1234" } : { email, password },
      });
      cache.invalidate(["me"]);
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login">
      <div className="login-card">
        <div className="brand brand-lg"><span className="brand-mark">✓</span> OpsDesk</div>
        <p className="muted">Coordinate operational work: who owns it, what state it is in, and what needs attention next.</p>
        <form onSubmit={login}>
          <label className="field"><span>Email</span>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
          </label>
          <label className="field"><span>Password</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </label>
          {error && <div className="notice notice-error">{error.message}</div>}
          <button className="btn btn-primary btn-block" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
        </form>
      </div>
      <div className="demo-card">
        <h3>Demo accounts</h3>
        <p className="muted small">Password for all: <code>demo1234</code>. Open two browsers (or a private window) as different people to see live updates and conflicts.</p>
        <ul>
          {DEMO.map(([u, name, role]) => (
            <li key={u}>
              <button disabled={pending} onClick={(e) => login(e, u)}>
                <strong>{name}</strong><span className="muted small">{role}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

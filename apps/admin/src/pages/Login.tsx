import { ArrowRight } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button, ErrorNote } from "../components/ui";
import { useSession } from "../lib/session";

const input =
  "h-10 w-full rounded-md bg-white px-3 text-[14px] text-ink shadow-sm ring-1 ring-inset ring-sand placeholder:text-stone focus:outline-none focus:ring-2 focus:ring-signal/50";

export function Login() {
  const { login } = useSession();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password, totp);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center bg-parchment px-4">
      <div className="w-full max-w-[380px]">
        <div className="mb-8 flex items-center justify-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-signal to-[#8f6bff] text-base font-bold text-white shadow-sm">
            R
          </div>
          <span className="text-xl font-semibold tracking-tight text-ink">Reflexo</span>
        </div>
        <form onSubmit={submit} className="rounded-xl bg-white p-8 shadow-pop">
          <h1 className="text-[18px] font-semibold tracking-tight text-ink">Sign in to the admin console</h1>
          <p className="mb-6 mt-1 text-[13px] text-dusk">Monitor and operate your AI employee.</p>
          <label className="mb-1.5 block text-[13px] font-medium text-soot">Username</label>
          <input className={`${input} mb-4`} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          <label className="mb-1.5 block text-[13px] font-medium text-soot">Password</label>
          <input
            type="password"
            className={`${input} mb-5`}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            autoFocus
          />
          <label className="mb-1.5 block text-[13px] font-medium text-soot">Authenticator code</label>
          <input
            className={`${input} mb-5`}
            value={totp}
            onChange={(e) => setTotp(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="6 digits (operators)"
          />
          <ErrorNote error={error} />
          <Button type="submit" disabled={busy || !password} className="mt-2 h-10 w-full text-[14px]">
            {busy ? "Signing in…" : (
              <>
                Sign in <ArrowRight size={15} />
              </>
            )}
          </Button>
        </form>
        <p className="mt-6 text-center text-xs text-stone">Tell it once. It handles the rest.</p>
      </div>
    </div>
  );
}

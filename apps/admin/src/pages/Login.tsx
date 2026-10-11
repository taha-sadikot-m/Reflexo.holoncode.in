import { useState, type FormEvent } from "react";

import { Button, ErrorNote } from "../components/ui";
import { useSession } from "../lib/session";

export function Login() {
  const { login } = useSession();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center bg-ink">
      <form onSubmit={submit} className="w-80 rounded-2xl bg-indigo p-7 text-bone shadow-2xl">
        <div className="font-display text-2xl font-bold">
          REFLEXO<span className="text-marigold">.</span>
        </div>
        <p className="mb-6 mt-1 text-sm text-stone">Bol do. Baaki hum.</p>
        <label className="mb-1 block text-xs font-semibold text-stone">Username</label>
        <input
          className="mb-3 w-full rounded-lg border border-twilight bg-ink px-3 py-2 text-sm outline-none focus:border-marigold"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
        />
        <label className="mb-1 block text-xs font-semibold text-stone">Password</label>
        <input
          type="password"
          className="mb-4 w-full rounded-lg border border-twilight bg-ink px-3 py-2 text-sm outline-none focus:border-marigold"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          autoFocus
        />
        <ErrorNote error={error} />
        <Button type="submit" disabled={busy || !password} className="mt-3 w-full !bg-marigold !text-ink">
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Brand } from "../Studio";
import { accessToken, authEnabled, finishNewPassword, restoreSignIn, signIn, signInWithPassword, subscribeAuth, type PasswordChallenge } from "./session";

export function AuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(!authEnabled);
  const [signedIn, setSignedIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [challenge, setChallenge] = useState<PasswordChallenge | null>(null);
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => {
    if (!authEnabled) return;
    const update = () => setSignedIn(Boolean(accessToken()));
    void restoreSignIn().then(() => { update(); setReady(true); }).catch((cause: unknown) => { setError(cause instanceof Error ? cause.message : "Sign-in failed."); setReady(true); });
    return subscribeAuth(update);
  }, []);
  async function submit() {
    if (!ready || active.current) return;
    if (challenge && password !== confirmation) { setError("The new passwords do not match."); return; }
    const request = new AbortController();
    active.current = request;
    setBusy(true); setError(null);
    try {
      if (challenge) await finishNewPassword(challenge, password, request.signal);
      else {
        const next = await signInWithPassword(username.trim(), password, request.signal);
        if (!request.signal.aborted) setChallenge(next);
      }
      if (!request.signal.aborted) { setPassword(""); setConfirmation(""); }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not sign in. Try again.");
    } finally { active.current = null; if (!request.signal.aborted) setBusy(false); }
  }
  if (!authEnabled || (ready && signedIn)) return children;
  return <main className="auth-main"><section className="camera-card auth-card"><Brand /><span className="eyebrow">Private reading studio</span><h1>{challenge ? "Choose a password" : "Open your library"}</h1><p>{challenge ? "Set a new password for your first sign-in. Use at least 12 characters, including uppercase and lowercase letters, a number, and a symbol." : "Sign in with the adult account to access your books, saved pages, and narration."}</p>
    <form className="auth-form" aria-label={challenge ? "Set new password" : "Sign in"} onSubmit={event => { event.preventDefault(); void submit(); }}>
      <label htmlFor="login-username">Username or email</label><input id="login-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required value={username} disabled={!ready || busy || Boolean(challenge)} onChange={event => setUsername(event.target.value)} />
      <label htmlFor="login-password">{challenge ? "New password" : "Password"}</label><input key={challenge ? "new" : "current"} id="login-password" name="password" type="password" autoComplete={challenge ? "new-password" : "current-password"} required minLength={challenge ? 12 : undefined} value={password} disabled={!ready || busy} onChange={event => setPassword(event.target.value)} />
      {challenge && <><label htmlFor="login-confirm">Confirm new password</label><input id="login-confirm" name="confirm-password" type="password" autoComplete="new-password" required minLength={12} value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} /></>}
      {error && <p role="alert" className="error">{error}</p>}
      <button className="primary" disabled={!ready || busy || !username.trim() || !password} aria-busy={busy}>{!ready ? "Checking sign-in..." : busy ? "Signing in..." : challenge ? "Set password and sign in" : "Sign In"}</button>
      {challenge && <button type="button" disabled={busy} onClick={() => { setChallenge(null); setPassword(""); setConfirmation(""); setError(null); }}>Back to sign in</button>}
      <button type="button" disabled={!ready || busy} onClick={() => void signIn().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Could not open account help."))}>Account help</button>
    </form>
  </section></main>;
}

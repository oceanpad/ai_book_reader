import { useEffect, useState, type ReactNode } from "react";
import { accessToken, authEnabled, restoreSignIn, signIn, subscribeAuth } from "./session";

export function AuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(!authEnabled);
  const [signedIn, setSignedIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!authEnabled) return;
    const update = () => setSignedIn(Boolean(accessToken()));
    void restoreSignIn().then(() => { update(); setReady(true); }).catch((cause: unknown) => { setError(cause instanceof Error ? cause.message : "Sign-in failed."); setReady(true); });
    return subscribeAuth(update);
  }, []);
  if (!authEnabled || (ready && signedIn)) return children;
  return <main><section className="camera-card"><h1>Little Pages</h1><p>Private book reading. Sign in with the adult account to continue.</p>{error && <p role="alert" className="error">{error}</p>}<button className="primary" disabled={!ready} onClick={() => void signIn().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Could not start sign-in."))}>{ready ? "Sign In" : "Checking sign-in..."}</button></section></main>;
}

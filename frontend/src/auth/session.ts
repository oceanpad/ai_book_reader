const domain = import.meta.env.VITE_COGNITO_DOMAIN ?? "";
const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID ?? "";
export const authEnabled = Boolean(domain && clientId);
const tokenKey = "reader.auth.tokens";
const flowKey = "reader.auth.pkce";
const changed = "reader-auth-changed";
const manualLoginKey = "reader.auth.manual-login";

type Tokens = { accessToken: string; expiresAt: number };
type Flow = { state: string; verifier: string; created: number };

function read<T>(key: string): T | null {
  const value = sessionStorage.getItem(key);
  if (!value) return null;
  try { return JSON.parse(value); }
  catch { sessionStorage.removeItem(key); throw new Error("Sign-in data was invalid. Sign in again."); }
}

function base64url(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function accessToken(): string | null {
  const tokens = read<Tokens>(tokenKey);
  return tokens && tokens.expiresAt > Date.now() + 15000 ? tokens.accessToken : null;
}

export function expireLogin() {
  sessionStorage.removeItem(tokenKey);
  window.dispatchEvent(new Event(changed));
}

export async function signIn() {
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(48)));
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  sessionStorage.setItem(flowKey, JSON.stringify({ state, verifier, created: Date.now() } satisfies Flow));
  const url = new URL("/oauth2/authorize", domain);
  url.search = new URLSearchParams({ client_id: clientId, response_type: "code", scope: "openid email reader/read reader/write", redirect_uri: `${location.origin}/auth/callback`, state, code_challenge: challenge, code_challenge_method: "S256" }).toString();
  location.assign(url);
}

let exchange: Promise<void> | null = null;
export function finishSignIn(): Promise<void> {
  if (exchange) return exchange;
  const params = new URLSearchParams(location.search);
  if (location.pathname !== "/auth/callback") return Promise.resolve();
  exchange = (async () => {
    const flow = read<Flow>(flowKey);
    sessionStorage.removeItem(flowKey);
    history.replaceState(null, "", "/");
    if (params.has("error")) throw new Error("Sign-in was cancelled or denied. Try again.");
    const code = params.get("code");
    if (!flow || !code || params.get("state") !== flow.state || Date.now() - flow.created > 600000) throw new Error("The sign-in request expired or could not be verified. Sign in again.");
    const response = await fetch(new URL("/oauth2/token", domain), { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ grant_type: "authorization_code", client_id: clientId, redirect_uri: `${location.origin}/auth/callback`, code, code_verifier: flow.verifier }) });
    if (!response.ok) throw new Error("The sign-in code could not be exchanged. Sign in again.");
    const result: unknown = await response.json();
    if (!result || typeof result !== "object" || !("access_token" in result) || typeof result.access_token !== "string" || !("expires_in" in result) || typeof result.expires_in !== "number") throw new Error("The sign-in service returned an invalid token response.");
    sessionStorage.setItem(tokenKey, JSON.stringify({ accessToken: result.access_token, expiresAt: Date.now() + result.expires_in * 1000 } satisfies Tokens));
  })();
  return exchange;
}

let startup: Promise<void> | null = null;
export function restoreSignIn(): Promise<void> {
  // StrictMode can mount the gate twice. Both mounts must share the same PKCE flow.
  return startup ??= (async () => {
    if (location.pathname === "/auth/callback") await finishSignIn();
    if (accessToken()) {
      sessionStorage.removeItem(manualLoginKey);
      if (location.pathname === "/login") history.replaceState(null, "", "/");
    } else if (!sessionStorage.getItem(manualLoginKey)) {
      // Cognito checks its own SSO cookie; only it can decide whether a prompt is needed.
      sessionStorage.setItem(manualLoginKey, "1");
      await signIn();
    }
  })();
}

export function signOut() {
  sessionStorage.setItem(manualLoginKey, "1");
  expireLogin();
  const url = new URL("/logout", domain);
  url.search = new URLSearchParams({ client_id: clientId, logout_uri: location.origin + "/" }).toString();
  location.assign(url);
}

export function subscribeAuth(listener: () => void) {
  window.addEventListener(changed, listener);
  const timer = setInterval(listener, 15000);
  return () => { window.removeEventListener(changed, listener); clearInterval(timer); };
}

export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  if (!authEnabled) return fetch(input, init);
  const token = accessToken();
  if (!token) { expireLogin(); throw new Error("Your login expired. Sign in again."); }
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(input, { ...init, headers });
  if (response.status === 401) expireLogin();
  return response;
}

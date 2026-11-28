const domain = import.meta.env.VITE_COGNITO_DOMAIN ?? "";
const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID ?? "";
const region = import.meta.env.VITE_COGNITO_REGION ?? "";
export const authEnabled = Boolean(domain && clientId);
const tokenKey = "reader.auth.tokens";
const flowKey = "reader.auth.pkce";
const changed = "reader-auth-changed";

type Tokens = { accessToken: string; expiresAt: number };
type Flow = { state: string; verifier: string; created: number };
export type PasswordChallenge = { session: string; username: string };

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function saveTokens(token: string, expiresIn: number) {
  if (!token || !Number.isFinite(expiresIn) || expiresIn <= 15) throw new Error("The sign-in service returned an invalid token response.");
  sessionStorage.setItem(tokenKey, JSON.stringify({ accessToken: token, expiresAt: Date.now() + expiresIn * 1000 } satisfies Tokens));
  if (location.pathname === "/login") history.replaceState(null, "", "/");
  window.dispatchEvent(new Event(changed));
}

async function passwordRequest(action: string, body: Record<string, unknown>, signal: AbortSignal): Promise<Record<string, unknown>> {
  if (!/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(region)) throw new Error("Sign-in is not configured. Redeploy with the Cognito region setting.");
  const response = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST", signal: AbortSignal.any([signal, AbortSignal.timeout(20000)]), credentials: "omit", referrerPolicy: "no-referrer",
    headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}` },
    body: JSON.stringify({ ClientId: clientId, ...body }),
  });
  const result: unknown = await response.json();
  signal.throwIfAborted();
  if (!response.ok) {
    const code = object(result) && typeof result.__type === "string" ? result.__type.split("#").pop() : "";
    if (code === "NotAuthorizedException" || code === "UserNotFoundException") throw new Error("Incorrect username or password. Try again.");
    if (code === "InvalidPasswordException") throw new Error("Use at least 12 characters with uppercase and lowercase letters, a number, and a symbol.");
    if (code === "TooManyRequestsException" || code === "LimitExceededException") throw new Error("Too many sign-in attempts. Wait a moment and try again.");
    if (code === "PasswordResetRequiredException" || code === "UserNotConfirmedException") throw new Error("This account needs recovery or confirmation. Select Account help to continue.");
    throw new Error("Could not sign in. Try again or select Account help.");
  }
  if (!object(result)) throw new Error("The sign-in service returned an invalid response.");
  return result;
}

function passwordResult(result: Record<string, unknown>, username: string): PasswordChallenge | null {
  const tokens = result.AuthenticationResult;
  if (object(tokens) && typeof tokens.AccessToken === "string" && typeof tokens.ExpiresIn === "number") {
    saveTokens(tokens.AccessToken, tokens.ExpiresIn);
    return null;
  }
  if (result.ChallengeName === "NEW_PASSWORD_REQUIRED" && typeof result.Session === "string") {
    const params = result.ChallengeParameters;
    return { session: result.Session, username: object(params) && typeof params.USER_ID_FOR_SRP === "string" ? params.USER_ID_FOR_SRP : username };
  }
  throw new Error("This account needs an additional sign-in step. Select Account help to continue.");
}

export async function signInWithPassword(username: string, password: string, signal: AbortSignal): Promise<PasswordChallenge | null> {
  return passwordResult(await passwordRequest("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: username, PASSWORD: password },
  }, signal), username);
}

export async function finishNewPassword(challenge: PasswordChallenge, password: string, signal: AbortSignal): Promise<void> {
  const result = await passwordRequest("RespondToAuthChallenge", {
    ChallengeName: "NEW_PASSWORD_REQUIRED", Session: challenge.session,
    ChallengeResponses: { USERNAME: challenge.username, NEW_PASSWORD: password },
  }, signal);
  if (passwordResult(result, challenge.username)) throw new Error("Password setup is incomplete. Select Account help to continue.");
}

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
    saveTokens(result.access_token, result.expires_in);
  })();
  return exchange;
}

let startup: Promise<void> | null = null;
export function restoreSignIn(): Promise<void> {
  // StrictMode can mount the gate twice. Both mounts must share the same callback.
  return startup ??= (async () => {
    if (location.pathname === "/auth/callback") await finishSignIn();
    if (accessToken() && location.pathname === "/login") history.replaceState(null, "", "/");
  })();
}

export function signOut() {
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

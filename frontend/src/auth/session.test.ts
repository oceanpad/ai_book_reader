import { afterEach, beforeEach, expect, test, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("VITE_COGNITO_DOMAIN", "https://login.example.invalid");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", "reader-test-client");
  vi.stubEnv("VITE_COGNITO_REGION", "ap-southeast-1");
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("window", new EventTarget());
  vi.stubGlobal("location", { origin: "https://reader.example.invalid", pathname: "/", search: "", assign: vi.fn() });
  vi.stubGlobal("history", { replaceState: vi.fn() });
});

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

test("managed login uses fresh state and PKCE, never a client secret", async () => {
  const { signIn } = await import("./session");
  await signIn();
  const target = new URL(vi.mocked(location.assign).mock.calls[0][0]);
  const flow = JSON.parse(sessionStorage.getItem("reader.auth.pkce")!);
  expect(target.searchParams.get("state")).toBe(flow.state);
  expect(target.searchParams.get("code_challenge_method")).toBe("S256");
  expect(target.searchParams.get("scope")).toContain("reader/write");
  expect(target.searchParams.has("client_secret")).toBe(false);
  expect(target.searchParams.get("code_challenge")).not.toBe(flow.verifier);
});

test("the callback exchanges a verified code once under duplicate initialization", async () => {
  sessionStorage.setItem("reader.auth.pkce", JSON.stringify({ state: "test-state", verifier: "test-verifier", created: Date.now() }));
  vi.stubGlobal("location", { origin: "https://reader.example.invalid", pathname: "/auth/callback", search: "?code=test-code&state=test-state" });
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ access_token: "test-access-token", expires_in: 3600 })));
  vi.stubGlobal("fetch", fetcher);
  const { finishSignIn, accessToken } = await import("./session");
  await Promise.all([finishSignIn(), finishSignIn()]);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1].body.get("code_verifier")).toBe("test-verifier");
  expect(sessionStorage.getItem("reader.auth.pkce")).toBeNull();
  expect(accessToken()).toBe("test-access-token");
});

test("mismatched OAuth state is rejected before contacting the token endpoint", async () => {
  sessionStorage.setItem("reader.auth.pkce", JSON.stringify({ state: "correct", verifier: "test", created: Date.now() }));
  vi.stubGlobal("location", { origin: "https://reader.example.invalid", pathname: "/auth/callback", search: "?code=test&state=incorrect" });
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const { finishSignIn } = await import("./session");
  await expect(finishSignIn()).rejects.toThrow("could not be verified");
  expect(fetcher).not.toHaveBeenCalled();
});

test("only API requests receive the access token; signed S3 downloads never do", async () => {
  sessionStorage.setItem("reader.auth.tokens", JSON.stringify({ accessToken: "test-token", expiresAt: Date.now() + 3600000 }));
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ url: "https://media.example.invalid/audio.wav" }))).mockResolvedValueOnce(new Response(new Blob(["synthetic-audio"])));
  vi.stubGlobal("fetch", fetcher);
  const { readingAudio } = await import("../api/client");
  await readingAudio("test-run", "test-job", 0, new AbortController().signal);
  expect(fetcher.mock.calls[0][1].headers.get("Authorization")).toBe("Bearer test-token");
  expect(fetcher.mock.calls[1][1].headers).toBeUndefined();
});

test("login page returns a valid tab session to the homepage without contacting Cognito", async () => {
  sessionStorage.setItem("reader.auth.tokens", JSON.stringify({ accessToken: "live", expiresAt: Date.now() + 3600000 }));
  vi.stubGlobal("location", { pathname: "/login", assign: vi.fn() });
  const { restoreSignIn } = await import("./session");
  await restoreSignIn();
  expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  expect(location.assign).not.toHaveBeenCalled();
});

test("missing tab session stays on the inline form, including after browser Back", async () => {
  const { restoreSignIn } = await import("./session");
  await Promise.all([restoreSignIn(), restoreSignIn()]);
  expect(location.assign).not.toHaveBeenCalled();
  vi.resetModules();
  await (await import("./session")).restoreSignIn();
  expect(location.assign).not.toHaveBeenCalled();
});

test("password sign-in saves only the access token and never redirects", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ AuthenticationResult: { AccessToken: "live", ExpiresIn: 3600, RefreshToken: "unused" } }));
  vi.stubGlobal("fetch", fetcher);
  const { signInWithPassword, accessToken } = await import("./session");
  expect(await signInWithPassword("adult@example.invalid", "test-password", new AbortController().signal)).toBeNull();
  const [url, init] = fetcher.mock.calls[0];
  expect(url).toBe("https://cognito-idp.ap-southeast-1.amazonaws.com/");
  expect(init.headers["X-Amz-Target"]).toContain(".InitiateAuth");
  expect(JSON.parse(init.body)).toEqual({ ClientId: "reader-test-client", AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: "adult@example.invalid", PASSWORD: "test-password" } });
  expect(accessToken()).toBe("live");
  expect(sessionStorage.getItem("reader.auth.tokens")).not.toContain("password");
  expect(sessionStorage.getItem("reader.auth.tokens")).not.toContain("unused");
  expect(location.assign).not.toHaveBeenCalled();
});

test("temporary passwords are completed on the same page with the Cognito challenge session", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ ChallengeName: "NEW_PASSWORD_REQUIRED", Session: "challenge-session", ChallengeParameters: { USER_ID_FOR_SRP: "canonical-user" } }))
    .mockResolvedValueOnce(Response.json({ AuthenticationResult: { AccessToken: "live", ExpiresIn: 3600 } }));
  vi.stubGlobal("fetch", fetcher);
  const { signInWithPassword, finishNewPassword, accessToken } = await import("./session");
  const signal = new AbortController().signal;
  const challenge = await signInWithPassword("adult@example.invalid", "temporary", signal);
  expect(challenge).toEqual({ session: "challenge-session", username: "canonical-user" });
  expect(accessToken()).toBeNull();
  await finishNewPassword(challenge!, "new-test-password", signal);
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ ClientId: "reader-test-client", ChallengeName: "NEW_PASSWORD_REQUIRED", Session: "challenge-session", ChallengeResponses: { USERNAME: "canonical-user", NEW_PASSWORD: "new-test-password" } });
  expect(accessToken()).toBe("live");
  expect(location.assign).not.toHaveBeenCalled();
});

test.each(["NotAuthorizedException", "UserNotFoundException"])("invalid credentials report the same safe message: %s", async code => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ __type: code, message: "private service details" }, { status: 400 })));
  const { signInWithPassword, accessToken } = await import("./session");
  await expect(signInWithPassword("adult", "incorrect", new AbortController().signal)).rejects.toThrow("Incorrect username or password");
  expect(accessToken()).toBeNull();
});

test("unsupported verification and invalid tokens cannot open the library", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ ChallengeName: "SMS_MFA", Session: "challenge" }))
    .mockResolvedValueOnce(Response.json({ AuthenticationResult: { AccessToken: "", ExpiresIn: 3600 } })));
  const { signInWithPassword, accessToken } = await import("./session");
  const signal = new AbortController().signal;
  await expect(signInWithPassword("adult", "test", signal)).rejects.toThrow("Account help");
  await expect(signInWithPassword("adult", "test", signal)).rejects.toThrow("invalid token");
  expect(accessToken()).toBeNull();
});

test("explicit logout leaves the sign-in button available without an auto-login loop", async () => {
  const { signOut, restoreSignIn } = await import("./session");
  signOut();
  await restoreSignIn();
  expect(location.assign).toHaveBeenCalledTimes(1);
  expect(new URL(vi.mocked(location.assign).mock.calls[0][0]).pathname).toBe("/logout");
});

test("expired login prevents a paid API request", async () => {
  sessionStorage.setItem("reader.auth.tokens", JSON.stringify({ accessToken: "expired", expiresAt: Date.now() - 1 }));
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const { apiFetch } = await import("./session");
  await expect(apiFetch("/api/readings/test", { method: "POST" })).rejects.toThrow("login expired");
  expect(fetcher).not.toHaveBeenCalled();
});

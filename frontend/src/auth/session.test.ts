import { afterEach, beforeEach, expect, test, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("VITE_COGNITO_DOMAIN", "https://login.example.invalid");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", "reader-test-client");
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

test("expired login prevents a paid API request", async () => {
  sessionStorage.setItem("reader.auth.tokens", JSON.stringify({ accessToken: "expired", expiresAt: Date.now() - 1 }));
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const { apiFetch } = await import("./session");
  await expect(apiFetch("/api/readings/test", { method: "POST" })).rejects.toThrow("login expired");
  expect(fetcher).not.toHaveBeenCalled();
});

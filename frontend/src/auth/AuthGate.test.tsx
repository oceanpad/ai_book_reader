// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, test, vi } from "vitest";
import { AuthGate } from "./AuthGate";
import { finishNewPassword, signIn, signInWithPassword } from "./session";

vi.mock("./session", () => ({
  authEnabled: true, accessToken: () => null, restoreSignIn: vi.fn().mockResolvedValue(undefined),
  subscribeAuth: () => () => {}, signIn: vi.fn(), signInWithPassword: vi.fn(), finishNewPassword: vi.fn(),
}));
beforeEach(() => vi.clearAllMocks());

async function type(host: HTMLElement, id: string, value: string) {
  await act(async () => {
    const input = host.querySelector<HTMLInputElement>(`#${id}`)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

test("the initial page contains working username and password fields without a redirect step", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(signInWithPassword).mockRejectedValue(new Error("Incorrect username or password."));
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<AuthGate><p>Library</p></AuthGate>));
    expect(host.querySelector('input[autocomplete="username"]')).not.toBeNull();
    expect(host.querySelector('input[type="password"][autocomplete="current-password"]')).not.toBeNull();
    await type(host, "login-username", " adult@example.invalid ");
    await type(host, "login-password", "test-password");
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(signInWithPassword).toHaveBeenCalledWith("adult@example.invalid", "test-password", expect.any(AbortSignal));
    expect(signIn).not.toHaveBeenCalled();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Incorrect username or password");
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"], button.primary')!.disabled).toBe(false);
  } finally { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});

test("first-login password setup remains inline and checks confirmation", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const challenge = { session: "session", username: "adult" };
  vi.mocked(signInWithPassword).mockResolvedValue(challenge);
  vi.mocked(finishNewPassword).mockResolvedValue(undefined);
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  const submit = () => act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  try {
    await act(async () => root.render(<AuthGate><p>Library</p></AuthGate>));
    await type(host, "login-username", "adult");
    await type(host, "login-password", "temporary");
    await submit();
    expect(host.querySelector("h1")?.textContent).toBe("Choose a password");
    expect(host.querySelector<HTMLInputElement>("#login-password")!.value).toBe("");
    await type(host, "login-password", "New-test-password1!");
    await type(host, "login-confirm", "Different-password1!");
    await submit();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("do not match");
    expect(finishNewPassword).not.toHaveBeenCalled();
    await type(host, "login-confirm", "New-test-password1!");
    await submit();
    expect(finishNewPassword).toHaveBeenCalledWith(challenge, "New-test-password1!", expect.any(AbortSignal));
    expect(signIn).not.toHaveBeenCalled();
  } finally { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});

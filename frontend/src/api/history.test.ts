import { afterEach, expect, test, vi } from "vitest";

const signal = new AbortController().signal;
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules(); });

async function client(cloud = false) {
  vi.stubEnv("VITE_COGNITO_DOMAIN", cloud ? "https://auth.example.invalid" : "");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", cloud ? "client" : "");
  vi.stubGlobal("location", { origin: "https://reader.example.invalid" });
  return import("./client");
}

test("history starts with the newest page and preserves opaque cursor characters", async () => {
  const { recentReadings } = await client();
  const cursor = "abc/+?=&";
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items: [], next_cursor: cursor }))
    .mockResolvedValueOnce(Response.json({ items: [], next_cursor: null }));
  vi.stubGlobal("fetch", fetcher);
  const page = await recentReadings("run", signal);
  expect(fetcher.mock.calls[0][0]).toBe("/api/local/readings");
  const last = await recentReadings("run", signal, page.next_cursor!);
  expect(new URL(fetcher.mock.calls[1][0], location.origin).searchParams.get("cursor")).toBe(cursor);
  expect(last.next_cursor).toBeNull();
});

test("delete targets the saved entry using its owning run", async () => {
  const { discardReading } = await client();
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  await discardReading("older-run", "saved-id");
  expect(fetcher).toHaveBeenCalledWith("/api/local/readings/saved-id", expect.objectContaining({ method: "DELETE", headers: { "X-Reader-Run": "older-run" } }));
});

test("local inline thumbnails need no network and other inline formats are rejected", async () => {
  const { thumbnailSource } = await client();
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const jpeg = "data:image/jpeg;base64,/9j/";
  expect(await thumbnailSource(jpeg, "run", signal)).toBe(jpeg);
  await expect(thumbnailSource("data:text/html,invalid", "run", signal)).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
});

test("cloud thumbnails become private blob URLs without sending the login token", async () => {
  const { thumbnailSource } = await client(true);
  const fetcher = vi.fn().mockResolvedValue(new Response(new Blob(["synthetic-image"])));
  vi.stubGlobal("fetch", fetcher);
  const signed = "https://media.example.invalid/image.jpg?signature=abc";
  const source = await thumbnailSource(signed, "run", signal);
  expect(source).toMatch(/^blob:/);
  URL.revokeObjectURL(source);
  expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: "omit" });
  expect(fetcher.mock.calls[0][1].headers).toBeUndefined();
  await expect(thumbnailSource(signed.replace("https:", "http:"), "run", signal)).rejects.toThrow();
  expect(fetcher).toHaveBeenCalledOnce();
});

import { afterEach, expect, test, vi } from "vitest";

const signal = new AbortController().signal;
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules(); });

async function client(cloud = false) {
  vi.stubEnv("VITE_COGNITO_DOMAIN", cloud ? "https://auth.example.invalid" : "");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", cloud ? "client" : "");
  vi.stubGlobal("location", { origin: "https://reader.example.invalid" });
  return import("./client");
}

test.each([false, true])("only show Earlier captures when recordings exist: %s", async hasLegacy => {
  const { listBooks } = await client();
  const named = { id: "book", name: "Story" };
  const items = [{ id: "earlier", name: "Earlier captures", created_at: hasLegacy ? 1000 : null }, named];
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items }));
  vi.stubGlobal("fetch", fetcher);
  expect((await listBooks("run", signal)).items).toEqual(hasLegacy ? items : [named]);
  expect(fetcher).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls[0][0]).toBe("/api/local/books");
});

test("metadata updates can clear a field without renaming the book", async () => {
  const { updateBook, createBook } = await client();
  const book = { id: "book", name: "Story", level: "P3", language: "zh" };
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(book)).mockResolvedValueOnce(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  expect(await createBook("run", "Story", signal, { level: "P3", language: "zh" })).toEqual(book);
  await updateBook("run", "book", { level: null }, signal);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ name: "Story", level: "P3", language: "zh" });
  expect(fetcher.mock.calls[1]).toEqual(["/api/local/books/book", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ level: null }), signal })]);
});

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

test("book playback spans all batches in recording order, even in the same second", async () => {
  const { allBookPages } = await client();
  const items = Array.from({ length: 12 }, (_, i) => ({ run_id: "run", id: `id-${i}`, created_at: 1 }));
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items: items.slice(0, 10), next_cursor: "+/" }))
    .mockResolvedValueOnce(Response.json({ items: items.slice(10), next_cursor: null }));
  vi.stubGlobal("fetch", fetcher);
  expect(await allBookPages("run", "book", signal)).toEqual(items.reverse());
  expect(fetcher.mock.calls.map(([url]) => new URL(url, location.origin).searchParams.get("book_id"))).toEqual(["book", "book"]);
});

test("retrying a completed read reuses its ID and targets the same book", async () => {
  const { recordCompletedRead } = await client();
  const fetcher = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(Response.json({ read_count: 1 }));
  vi.stubGlobal("fetch", fetcher);
  const id = crypto.randomUUID();
  await expect(recordCompletedRead("run", "book", id)).rejects.toThrow("offline");
  expect(await recordCompletedRead("run", "book", id)).toEqual({ read_count: 1 });
  for (const [url, init] of fetcher.mock.calls) {
    expect(url).toBe("/api/local/books/book/completions");
    expect(JSON.parse(init.body)).toEqual({ completion_id: id });
  }
});

test("cover selection uses the page's capture run and keeps the library's owner header", async () => {
  const { setBookCover } = await client();
  const cover = { run_id: "older-run", page_id: "page", thumbnail_url: "/thumbnail" };
  const fetcher = vi.fn().mockResolvedValue(Response.json({ id: "book", name: "Story", cover }));
  vi.stubGlobal("fetch", fetcher);
  expect((await setBookCover("library-run", "book", { id: "page", run_id: "older-run" }, signal)).cover).toEqual(cover);
  expect(fetcher).toHaveBeenCalledWith("/api/local/books/book/cover", expect.objectContaining({
    method: "POST", headers: { "X-Reader-Run": "library-run", "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: "older-run", page_id: "page" }),
  }));
});

test("book deletion uses its own endpoint and sends the local library run", async () => {
  const { deleteBook } = await client();
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  await deleteBook("library", "one/book", signal);
  expect(fetcher).toHaveBeenCalledWith("/api/local/books/one%2Fbook", expect.objectContaining({ method: "DELETE", headers: { "X-Reader-Run": "library" }, signal }));
});

test("delete targets the saved entry using its owning run", async () => {
  const { discardReading } = await client();
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  await discardReading("older-run", "saved-id");
  expect(fetcher).toHaveBeenCalledWith("/api/local/readings/saved-id", expect.objectContaining({ method: "DELETE", headers: { "X-Reader-Run": "older-run" } }));
});

test("editing a page sends corrected text to its owning run without uploading an image", async () => {
  const { editNarration } = await client();
  const fetcher = vi.fn().mockResolvedValue(Response.json({ state: "QUEUED" }));
  vi.stubGlobal("fetch", fetcher);
  await editNarration("older-run", "saved-id", "小恐龙\n生病了", signal);
  expect(fetcher).toHaveBeenCalledWith("/api/local/readings/saved-id/narration", expect.objectContaining({
    method: "POST", headers: { "X-Reader-Run": "older-run", "Content-Type": "application/json" },
    body: JSON.stringify({ narration: "小恐龙\n生病了" }), signal,
  }));
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

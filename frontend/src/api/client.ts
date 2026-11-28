import type { Book, BookPageMove, BookPageOrder, BookUpdate, HomeworkEntry, HomeworkHistory, HomeworkSubmission, PlayableCapture, PublicConfig, ReadingJob, RecentCaptures } from "./contracts.generated";
import { apiFetch, authEnabled } from "../auth/session";

const readings = authEnabled ? "/api/readings" : "/api/local/readings";
const homework = authEnabled ? "/api/homework" : "/api/local/homework";

export async function submitHomework(run: string, upload: HomeworkSubmission, signal: AbortSignal): Promise<HomeworkEntry> {
  const response = await checked(await apiFetch(homework, {
    method: "POST", signal, headers: { "X-Reader-Run": run, "Content-Type": "application/json" },
    body: JSON.stringify(upload),
  }));
  return response.json();
}

export async function homeworkHistory(run: string, signal: AbortSignal, cursor?: string): Promise<HomeworkHistory> {
  const query = cursor ? `?${new URLSearchParams({ cursor })}` : "";
  const response = await checked(await apiFetch(homework + query, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function getHomework(run: string, id: string, signal: AbortSignal): Promise<HomeworkEntry> {
  const response = await checked(await apiFetch(`${homework}/${encodeURIComponent(id)}`, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function deleteHomework(run: string, id: string, signal: AbortSignal): Promise<void> {
  await checked(await apiFetch(`${homework}/${encodeURIComponent(id)}`, {
    method: "DELETE", signal, headers: { "X-Reader-Run": run },
  }));
}

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const message = body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
      ? body.detail : `Reader request failed (${response.status}). Try again.`;
    throw new Error(message);
  }
  return response;
}

export type { Book } from "./contracts.generated";
const books = authEnabled ? "/api/books" : "/api/local/books";

export async function listBooks(run: string, signal: AbortSignal): Promise<{items: Book[]}> {
  const library: { items: Book[] } = await (await checked(await apiFetch(books, { signal, cache: "no-store", headers: { "X-Reader-Run": run } }))).json();
  return { items: library.items.filter(book => book.id !== "earlier" || book.created_at != null) };
}

export async function recordCompletedRead(run: string, bookId: string, completionId: string): Promise<{ read_count: number }> {
  return (await checked(await apiFetch(`${books}/${encodeURIComponent(bookId)}/completions`, {
    method: "POST", keepalive: true, signal: AbortSignal.timeout(20000),
    headers: { "Content-Type": "application/json", "X-Reader-Run": run },
    body: JSON.stringify({ completion_id: completionId }),
  }))).json();
}

export async function setBookCover(run: string, bookId: string, page: Pick<PlayableCapture, "run_id" | "id">, signal: AbortSignal): Promise<Book> {
  return (await checked(await apiFetch(`${books}/${encodeURIComponent(bookId)}/cover`, {
    method: "POST", signal, headers: { "Content-Type": "application/json", "X-Reader-Run": run },
    body: JSON.stringify({ run_id: page.run_id, page_id: page.id }),
  }))).json();
}

export async function moveBookPage(run: string, bookId: string, move: BookPageMove, signal: AbortSignal): Promise<BookPageOrder> {
  return (await checked(await apiFetch(`${books}/${encodeURIComponent(bookId)}/page-order`, {
    method: "POST", signal, headers: { "Content-Type": "application/json", "X-Reader-Run": run },
    body: JSON.stringify(move),
  }))).json();
}

export async function updateBook(run: string, bookId: string, changes: BookUpdate, signal: AbortSignal): Promise<void> {
  await checked(await apiFetch(`${books}/${encodeURIComponent(bookId)}`, {
    method: "PATCH", signal, headers: { "Content-Type": "application/json", "X-Reader-Run": run }, body: JSON.stringify(changes),
  }));
}

export async function deleteBook(run: string, bookId: string, signal: AbortSignal): Promise<void> {
  await checked(await apiFetch(`${books}/${encodeURIComponent(bookId)}`, { method: "DELETE", signal, headers: { "X-Reader-Run": run } }));
}

export async function createBook(run: string, name: string, signal: AbortSignal, metadata: Pick<Book, "level" | "language"> = {}): Promise<Book> {
  return (await checked(await apiFetch(books, { method: "POST", signal, body: JSON.stringify({ name, ...metadata }), headers: { "Content-Type": "application/json", "X-Reader-Run": run } }))).json();
}

export async function submitReading(run: string, id: string, image: Blob, mode: string, signal: AbortSignal, bookId: string): Promise<ReadingJob> {
  const response = await checked(await apiFetch(`${readings}/${id}?${new URLSearchParams({ mode, book_id: bookId })}`, {
    method: "POST", body: image, signal,
    headers: { "Content-Type": "image/jpeg", "X-Reader-Run": run },
  }));
  return response.json();
}

export async function editNarration(run: string, id: string, narration: string, signal: AbortSignal): Promise<ReadingJob> {
  const response = await checked(await apiFetch(`${readings}/${id}/narration`, {
    method: "POST", signal, body: JSON.stringify({ narration }),
    headers: { "Content-Type": "application/json", "X-Reader-Run": run },
  }));
  return response.json();
}

export async function readingRequest(run: string, id: string, signal: AbortSignal, retry = false): Promise<ReadingJob> {
  const response = await checked(await apiFetch(`${readings}/${id}${retry ? "/retry" : ""}`, {
    method: retry ? "POST" : "GET", signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function allBookPages(run: string, bookId: string, signal: AbortSignal): Promise<PlayableCapture[]> {
  const pages = new Map<string, PlayableCapture>();
  let order: string[] | null | undefined;
  let cursor: string | undefined;
  do {
    const result = await recentReadings(run, signal, cursor, bookId);
    if (!cursor) order = result.page_order;
    for (const page of result.items) pages.set(captureKey(page), page);
    cursor = result.next_cursor ?? undefined;
  } while (cursor);
  // Preserve the server's sub-second capture order, including across batch boundaries.
  return applyBookPageOrder([...pages.values()].reverse(), order);
}

export const captureKey = (page: Pick<PlayableCapture, "run_id" | "id">) => `${page.run_id}/${page.id}`;

export function applyBookPageOrder(pages: PlayableCapture[], order?: string[] | null): PlayableCapture[] {
  if (!order?.length) return pages;
  const remaining = new Map(pages.map(page => [captureKey(page), page]));
  const ordered: PlayableCapture[] = [];
  for (const key of order) {
    const page = remaining.get(key);
    if (page) {
      ordered.push(page);
      remaining.delete(key);
    }
  }
  return ordered.concat([...remaining.values()]);
}

export async function recentReadings(run: string, signal: AbortSignal, cursor?: string, bookId?: string): Promise<RecentCaptures> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (bookId) params.set("book_id", bookId);
  const query = params.size ? `?${params}` : "";
  const response = await checked(await apiFetch(readings + query, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return response.json();
}

export async function thumbnailSource(url: string, run: string, signal: AbortSignal): Promise<string> {
  if (!authEnabled && url.startsWith("data:image/jpeg;base64,")) return url;
  const target = new URL(url, location.origin);
  if (target.origin !== location.origin) {
    if (!authEnabled || target.protocol !== "https:") throw new Error("Invalid thumbnail URL.");
    // The cloud image policy permits blob URLs. Never send API credentials to signed media URLs.
    const response = await checked(await fetch(target.href, { signal, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" }));
    return URL.createObjectURL(await response.blob());
  }
  const response = await checked(await apiFetch(target.pathname + target.search, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  return URL.createObjectURL(await response.blob());
}

export async function readingAudio(run: string, id: string, chunk: number, signal: AbortSignal): Promise<Blob> {
  return readingMedia(run, id, `audio/${chunk}`, signal);
}

export function readingOriginal(run: string, id: string, signal: AbortSignal): Promise<Blob> {
  return readingMedia(run, id, "original", signal);
}

async function readingMedia(run: string, id: string, resource: string, signal: AbortSignal): Promise<Blob> {
  const response = await checked(await apiFetch(`${readings}/${id}/${resource}`, {
    signal, cache: "no-store", headers: { "X-Reader-Run": run },
  }));
  if (!authEnabled) return response.blob();
  const media: unknown = await response.json();
  if (!media || typeof media !== "object" || !("url" in media) || typeof media.url !== "string" || !media.url.startsWith("https://")) throw new Error("The reader returned an invalid private media URL.");
  return (await checked(await fetch(media.url, { signal, cache: "no-store" }))).blob();
}

export async function discardReading(run: string, id: string): Promise<void> {
  await checked(await apiFetch(`${readings}/${id}`, {
    method: "DELETE", keepalive: true, signal: AbortSignal.timeout(20000), headers: { "X-Reader-Run": run },
  }));
}

export async function getConfig(signal: AbortSignal): Promise<PublicConfig> {
  const response = await apiFetch("/api/config", { signal, cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("Your account could not access the reader. Sign in again or check the deployed Cognito permissions.");
    }
    throw new Error(`The reader service is unavailable (${response.status}). Try again.`);
  }
  return response.json();
}

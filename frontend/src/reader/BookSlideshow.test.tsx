import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { waitForNarration } from "./NarrationEditor";
import type { ReadingJob } from "../api/contracts.generated";
import { readingAudio, readingOriginal, readingRequest, thumbnailSource } from "../api/client";
import type { PlayableCapture } from "../api/contracts.generated";
import { BookSlideshow, readPagePreview, startBookPlayback } from "./BookSlideshow";

vi.mock("../api/client", () => ({ readingRequest: vi.fn(), readingAudio: vi.fn(), readingOriginal: vi.fn(), thumbnailSource: vi.fn() }));
const pages: PlayableCapture[] = ["old", "middle", "new"].map(id => ({ run_id: "run", id, created_at: 1, language: "en", narration: `${id} ${"full line\n".repeat(30)}`, audio_chunks: id === "middle" ? 2 : 1, thumbnail_url: null }));
const fetchAudio = vi.mocked(readingAudio);
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const cleanups: (() => void)[] = [];
let visibility: EventTarget & { hidden: boolean };
beforeEach(() => {
  visibility = Object.assign(new EventTarget(), { hidden: false });
  vi.stubGlobal("document", visibility);
  vi.stubGlobal("window", new EventTarget());
  fetchAudio.mockReset().mockResolvedValue(new Blob());
  vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:${crypto.randomUUID()}`);
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
});
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function open(start = 1, book = pages, autoplay = true) {
  const audio = { src: "", ended: true, pause: vi.fn(), play: vi.fn<() => Promise<void>>().mockResolvedValue(undefined), load: vi.fn(), removeAttribute: vi.fn(), onended: null as (() => void) | null };
  const update = vi.fn();
  const onComplete = vi.fn();
  const player = startBookPlayback(book, start, audio as unknown as HTMLAudioElement, update, autoplay, onComplete);
  cleanups.push(player.dispose);
  return { audio, player, update, onComplete };
}
const chunks = () => fetchAudio.mock.calls.map(([, id, chunk]) => [id, chunk]);

test("shows the selected page and complete narration inside a named dialog", () => {
  const html = renderToStaticMarkup(<BookSlideshow pages={pages} startIndex={1} close={() => {}} />);
  expect(html).toContain('<dialog class="book-slideshow" aria-label="Book slideshow"');
  expect(html).toContain("Page 2 of 3");
  expect(html).toContain(">Pause</button>");
  expect(html).toMatch(/<header\b[^>]*>.*?>Close<\/button><\/header>/s);
  expect(html).toContain(pages[1].narration);
  expect(html).not.toContain(pages[0].narration);
});

test("advances only after the last chunk of each page; stops and releases the last source", async () => {
  const { audio, update, onComplete } = open();
  await settle();
  expect(chunks()).toEqual([["middle", 0]]);
  audio.onended!(); await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "playing" });
  vi.useFakeTimers();
  audio.onended!();
  await vi.advanceTimersByTimeAsync(1999);
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "thinking" });
  expect(chunks()).toEqual([["middle", 0], ["middle", 1]]);
  await vi.advanceTimersByTimeAsync(1);
  expect(update).toHaveBeenLastCalledWith({ index: 2, status: "playing" });
  audio.onended!();
  expect(update).toHaveBeenLastCalledWith({ index: 2, status: "finished" });
  expect(chunks()).toEqual([["middle", 0], ["middle", 1], ["new", 0]]);
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(3);
  expect(onComplete).not.toHaveBeenCalled();
});

test("skipping a partial page prevents completion; returning to it and finishing in order counts", async () => {
  vi.useFakeTimers();
  const { audio, player, onComplete } = open(0);
  await vi.advanceTimersByTimeAsync(0);
  audio.onended!(); await vi.advanceTimersByTimeAsync(2000);
  audio.onended!(); await vi.advanceTimersByTimeAsync(0);
  const skippedChunk = audio.onended!;
  player.move(1); skippedChunk(); await vi.advanceTimersByTimeAsync(0);
  audio.onended!();
  expect(onComplete).not.toHaveBeenCalled();

  player.move(-1); await vi.advanceTimersByTimeAsync(0);
  player.toggle(); await vi.advanceTimersByTimeAsync(0);
  audio.onended!(); await vi.advanceTimersByTimeAsync(0);
  fetchAudio.mockRejectedValueOnce(new Error("offline"));
  audio.onended!(); await vi.advanceTimersByTimeAsync(2000);
  player.toggle(); await vi.advanceTimersByTimeAsync(0);
  expect(onComplete).not.toHaveBeenCalled();
  audio.onended!();
  expect(onComplete).toHaveBeenCalledTimes(1);
});

test("a paused final end counts immediately and only once, and Replay starts a new pass", async () => {
  const { audio, player, onComplete, update } = open(0, [pages[1]]);
  await settle();
  audio.onended!(); await settle();
  visibility.hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  expect(onComplete).not.toHaveBeenCalled();
  const end = audio.onended!;
  end(); end();
  expect(onComplete).toHaveBeenCalledTimes(1);
  expect(update).toHaveBeenLastCalledWith({ index: 0, status: "paused" });
  visibility.hidden = false;
  player.toggle();
  expect(update).toHaveBeenLastCalledWith({ index: 0, status: "finished" });
  expect(onComplete).toHaveBeenCalledTimes(1);
  player.toggle(); await settle();
  audio.onended!(); await settle();
  expect(onComplete).toHaveBeenCalledTimes(1);
  audio.onended!();
  player.dispose();
  expect(onComplete).toHaveBeenCalledTimes(2);
});

test("restarting after an edit discards the old pass and stale end events", async () => {
  vi.useFakeTimers();
  const original = open(0);
  await vi.advanceTimersByTimeAsync(0);
  original.audio.onended!(); await vi.advanceTimersByTimeAsync(2000);
  const staleEnd = original.audio.onended!;
  original.player.dispose();
  const edited = open(1, [pages[0], { ...pages[1], audio_chunks: 1 }], false);
  await vi.advanceTimersByTimeAsync(0);
  edited.player.toggle(); await vi.advanceTimersByTimeAsync(0);
  staleEnd(); edited.audio.onended!();
  expect(original.onComplete).not.toHaveBeenCalled();
  expect(edited.onComplete).not.toHaveBeenCalled();
});

test("waits for a queued page without skipping or prematurely counting it", async () => {
  vi.useFakeTimers();
  const queued = { ...pages[1], state: "QUEUED" as const, audio_chunks: 0 };
  const job = { ...queued, stage: "vision", reason: null, retryable: false, vision_model: "v", speech_model: "s", voice: "x", prompt_version: "1" } satisfies ReadingJob;
  vi.mocked(readingRequest).mockResolvedValueOnce(job).mockResolvedValueOnce({ ...job, state: "READY", audio_chunks: 1 });
  const { audio, onComplete } = open(0, [pages[0], queued]);
  await vi.advanceTimersByTimeAsync(0);
  audio.onended!(); await vi.advanceTimersByTimeAsync(2000);
  expect(chunks()).toEqual([["old", 0]]);
  expect(onComplete).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1000);
  expect(chunks()).toEqual([["old", 0], ["middle", 0]]);
  audio.onended!();
  expect(onComplete).toHaveBeenCalledTimes(1);
});

test("pausing during thinking time holds the page until Play", async () => {
  const { audio, player } = open(0); await settle();
  vi.useFakeTimers();
  audio.onended!(); player.toggle();
  await vi.advanceTimersByTimeAsync(6000);
  expect(chunks()).toEqual([["old", 0]]);
  player.toggle();
  expect(chunks()).toEqual([["old", 0], ["middle", 0]]);
});

test("a user gesture retries denied autoplay immediately with the same audio", async () => {
  const { audio, player, update } = open();
  audio.play.mockRejectedValueOnce(new DOMException("Autoplay blocked", "NotAllowedError"));
  await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "blocked" });
  player.toggle();
  expect(audio.play).toHaveBeenCalledTimes(2);
  await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "playing" });
  expect(fetchAudio).toHaveBeenCalledTimes(1);
});

test("pausing while loading in the background requires deliberate resume", async () => {
  const { audio, player, update } = open();
  visibility.hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  await settle();
  visibility.hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  expect(audio.play).not.toHaveBeenCalled();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "paused" });
  player.toggle(); await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "playing" });
});

test("navigation invalidates previous events and pending play; disposal cancels pending media", async () => {
  let reject!: (reason: Error) => void;
  const { audio, player, update } = open();
  audio.play.mockImplementationOnce(() => new Promise((_, r) => { reject = r; }));
  await settle();
  const oldEnd = audio.onended!;
  player.move(-1);
  reject(new Error("interrupted")); oldEnd(); await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 0, status: "playing" });
  player.move(1);
  player.dispose();
  update.mockClear(); await settle();
  expect(chunks()).toEqual([["middle", 0], ["old", 0], ["middle", 0]]);
  expect(fetchAudio.mock.calls.every(call => call[3].aborted)).toBe(true);
  expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  expect(update).not.toHaveBeenCalled();
});

test("an end event arriving in the background waits for Play before advancing", async () => {
  const { audio, player } = open(0); await settle();
  visibility.hidden = true;
  audio.onended!();
  expect(chunks()).toEqual([["old", 0]]);
  visibility.hidden = false;
  player.toggle(); await settle();
  expect(chunks()).toEqual([["old", 0], ["middle", 0]]);
});

test("waits for regenerated audio and uses the updated chunk count", async () => {
  vi.useFakeTimers();
  const signal = new AbortController().signal;
  const job = { ...pages[0], state: "QUEUED", stage: "speech", reason: null, retryable: false,
    vision_model: "v", speech_model: "s", voice: "voice", prompt_version: "1" } satisfies ReadingJob;
  const ready = { ...job, state: "READY", narration: "Corrected text", audio_chunks: 2 } as const;
  vi.mocked(readingRequest).mockResolvedValueOnce({ ...job, state: "PROCESSING" }).mockResolvedValueOnce(ready);
  const result = waitForNarration(pages[0], job, signal);
  await vi.advanceTimersByTimeAsync(2000);
  expect(await result).toEqual(ready);
  expect(readingRequest).toHaveBeenLastCalledWith(pages[0].run_id, pages[0].id, signal);
});

test("prefers the original and resolves a legacy thumbnail only if needed", async () => {
  const page = { ...pages[0], thumbnail_url: "/thumb" };
  const { signal } = new AbortController();
  vi.mocked(readingOriginal).mockResolvedValueOnce(new Blob()).mockRejectedValueOnce(new Error("missing"));
  vi.mocked(thumbnailSource).mockResolvedValueOnce("blob:thumb");
  expect(await readPagePreview(page, false, signal)).toMatchObject({ thumbnail: false });
  expect(thumbnailSource).not.toHaveBeenCalled();
  expect(await readPagePreview(page, false, signal)).toEqual({ thumbnail: true, url: "blob:thumb" });
  expect(thumbnailSource).toHaveBeenCalledWith("/thumb", page.run_id, signal);
});

test("retry after a chunk fetch error resumes at the failed chunk", async () => {
  const { audio, player, update } = open(); await settle();
  fetchAudio.mockRejectedValueOnce(new Error("offline"));
  audio.onended!(); await settle();
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "error" });
  player.toggle(); await settle();
  expect(chunks()).toEqual([["middle", 0], ["middle", 1], ["middle", 1]]);
  expect(update).toHaveBeenLastCalledWith({ index: 1, status: "playing" });
});

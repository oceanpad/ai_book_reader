// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { allBookPages, discardReading, readingAudio, readingRequest, submitReading } from "../api/client";
import type { PlayableCapture, ReadingJob } from "../api/contracts.generated";
import { useReading } from "./useReading";

vi.mock("../api/client", () => ({ allBookPages: vi.fn(), discardReading: vi.fn(), readingAudio: vi.fn(), readingRequest: vi.fn(), submitReading: vi.fn() }));
const queued = (id: string): ReadingJob => ({ id, state: "QUEUED", stage: "vision", narration: "", language: "en", reason: null, retryable: false, audio_chunks: 0, vision_model: "", speech_model: "", voice: "", prompt_version: "" });
function row(id: string, state?: ReadingJob["state"]): PlayableCapture {
  return { id, state, run_id: "run", created_at: 1, narration: "", language: "en", audio_chunks: state ? 0 : 1, thumbnail_url: null };
}
let hook: ReturnType<typeof useReading>;
let root: Root;
function Harness({ book }: { book: string }) {
  hook = useReading("run", book);
  return <audio ref={hook.audioRef} />;
}
const mount = (book = "book") => act(async () => { root.render(<Harness key={book} book={book} />); });
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const image = new Blob(["page"]);
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.resetAllMocks();
  vi.mocked(allBookPages).mockResolvedValue([]);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  root = createRoot(document.createElement("div"));
});
afterEach(async () => {
  await act(async () => { root.unmount(); });
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

test("serializes uploads, unlocks on acceptance, and switching books leaves queued captures intact", async () => {
  const items: PlayableCapture[] = [];
  vi.mocked(allBookPages).mockImplementation(async (_, book) => book === "book" ? [...items] : []);
  let accept!: () => void;
  vi.mocked(submitReading).mockImplementation((_, id) => new Promise(resolve => {
    accept = () => { items.push(row(id, "QUEUED")); resolve(queued(id)); };
  }));
  await mount();
  await act(async () => { void hook.submit(image, "spread"); void hook.submit(image, "spread"); });
  expect(submitReading).toHaveBeenCalledTimes(1);
  expect(hook.busy).toBe(true);
  await act(async () => accept());
  expect(hook.busy).toBe(false);
  expect(hook.history.items).toEqual(items);
  await act(async () => { void hook.submit(image, "spread"); accept(); });
  expect(submitReading).toHaveBeenCalledTimes(2);
  expect(hook.history.items.map(item => item.id)).toEqual(vi.mocked(submitReading).mock.calls.map(call => call[1]));
  await tick(3000);
  expect(readingRequest).not.toHaveBeenCalled();
  await mount("other");
  const calls = vi.mocked(allBookPages).mock.calls.length;
  await tick(5000);
  expect(allBookPages).toHaveBeenCalledTimes(calls);
  await mount();
  expect(hook.history.items).toEqual(items);
  expect(discardReading).not.toHaveBeenCalled();
});

test("loads terminal details once; a superseded audio fetch cannot autoplay", async () => {
  let items: PlayableCapture[] = [];
  vi.mocked(allBookPages).mockImplementation(async () => items);
  vi.mocked(submitReading).mockImplementation(async (_, id) => { items = [...items, row(id, "QUEUED")]; return queued(id); });
  vi.mocked(readingRequest).mockImplementation(async (_, id) => ({ ...queued(id), state: "READY", audio_chunks: 1 }));
  let finishAudio!: (value: Blob) => void;
  vi.mocked(readingAudio).mockImplementationOnce(() => new Promise(resolve => { finishAudio = resolve; }));
  await mount();
  await act(async () => { await hook.submit(image, "spread"); });
  items = [row(hook.job!.id)];
  await tick(1000);
  expect(hook.job?.state).toBe("READY");
  expect(hook.busy).toBe(false);
  await act(async () => { await hook.submit(image, "spread"); finishAudio(image); });
  await tick(3000);
  expect(readingRequest).toHaveBeenCalledTimes(1);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(hook.job?.id).toBe(items[1].id);
  expect(hook.audioReady).toBe(false);
});

test("queue-full errors unlock the camera; retry reuses the same capture and recovers failed work", async () => {
  const items: PlayableCapture[] = [];
  vi.mocked(allBookPages).mockImplementation(async () => [...items]);
  vi.mocked(submitReading).mockRejectedValueOnce(new Error("Queue full: 10 pending pages."))
    .mockImplementationOnce(async (_, id) => { items.push(row(id, "PROCESSING")); return queued(id); });
  await mount();
  await act(async () => { await hook.submit(image, "spread"); });
  expect(hook.error).toContain("Queue full");
  expect(hook.busy).toBe(false);
  await act(async () => { await hook.recover(); });
  const id = items[0].id;
  expect(vi.mocked(submitReading).mock.calls.map(call => call[1])).toEqual([id, id]);
  items[0] = row(id, "FAILED");
  vi.mocked(readingRequest).mockResolvedValueOnce({ ...queued(id), state: "FAILED", retryable: true }).mockImplementationOnce(async () => {
    items[0] = row(id, "QUEUED"); return queued(id);
  });
  await tick(1000);
  expect(hook.job?.retryable).toBe(true);
  await act(async () => { await hook.recover(); });
  expect(readingRequest).toHaveBeenLastCalledWith("run", id, expect.any(AbortSignal), true);
  expect(hook.job?.state).toBe("QUEUED");
  expect(submitReading).toHaveBeenCalledTimes(2);
});

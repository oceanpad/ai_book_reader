// @vitest-environment happy-dom
import { act, createRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { allBookPages, moveBookPage, readingAudio, readingOriginal, submitReading } from "../api/client";
import type { PlayableCapture, PublicConfig } from "../api/contracts.generated";
import { Camera } from "./Camera";
import { captureAndStop } from "./frame";
import { useCamera } from "./useCamera";

vi.mock("../api/client", async importOriginal => ({
  ...await importOriginal<typeof import("../api/client")>(),
  allBookPages: vi.fn(), moveBookPage: vi.fn(), readingAudio: vi.fn(), readingOriginal: vi.fn(), submitReading: vi.fn(),
}));
vi.mock("./frame", async importOriginal => ({ ...await importOriginal<typeof import("./frame")>(), captureAndStop: vi.fn() }));
vi.mock("./useCamera", async importOriginal => ({ ...await importOriginal<typeof import("./useCamera")>(), useCamera: vi.fn() }));

const config: PublicConfig = {
  mode: "local", recognition_available: true, speech_available: true, setup_message: null,
  camera: { samples_per_second: 2, stable_window_ms: 1000, motion_threshold: 0.1, scene_change_threshold: 0.2, sharpness_threshold: 10, min_crop_width: 300, min_crop_height: 250, max_longest_edge: 2560, max_upload_bytes: 4194304 },
  limits: { max_captures_ahead: 10, max_pages_per_session: 100, capture_attempts_per_day: 200, speech_attempts_per_day: 200, stage_attempts: 3 },
};
let host: HTMLDivElement;
let root: Root;
let camera: ReturnType<typeof useCamera>;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.resetAllMocks();
  const pages: PlayableCapture[] = [];
  vi.mocked(allBookPages).mockImplementation(async () => [...pages]);
  vi.mocked(submitReading).mockImplementation(async (_, id) => {
    pages.push({ id, run_id: "run", state: "QUEUED", created_at: 1, narration: "", language: "en", audio_chunks: 0, thumbnail_url: null });
    return { id, state: "QUEUED", stage: "vision", narration: "", language: "en", reason: null, retryable: false, audio_chunks: 0, vision_model: "", speech_model: "", voice: "", prompt_version: "" };
  });
  camera = { videoRef: createRef<HTMLVideoElement>(), status: "live", error: null, facing: "environment", aspect: 4 / 3, start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), resize: vi.fn() };
  vi.mocked(useCamera).mockReturnValue(camera);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

test.each([
  ["single", 16 / 21, 0],
  ["spread", 31 / 20, 1],
] as const)("switches to the %s preview without restarting the camera and submits that mode", async (mode, aspect, index) => {
  const blob = new Blob(["page"], { type: "image/jpeg" });
  let finishCapture!: (frame: Awaited<ReturnType<typeof captureAndStop>>) => void;
  vi.mocked(captureAndStop).mockReturnValue(new Promise(resolve => { finishCapture = resolve; }));
  await act(async () => root.render(<Camera config={config} book={{ id: "book", name: "Story" }} run="run" preview={false} back={() => {}} />));
  const sizes = host.querySelectorAll<HTMLButtonElement>('[aria-label="Preview size"] button');
  expect(sizes[0].getAttribute("aria-pressed")).toBe("true");
  await act(async () => sizes[1].click());
  await act(async () => sizes[index].click());
  expect(sizes[index].getAttribute("aria-pressed")).toBe("true");
  expect(sizes[1 - index].getAttribute("aria-pressed")).toBe("false");
  expect(host.querySelector<HTMLElement>(".capture-session")?.style.getPropertyValue("--frame-aspect")).toBe(String(aspect));
  expect(camera.start).not.toHaveBeenCalled();
  const submit = host.querySelector<HTMLButtonElement>('button[aria-describedby="studio-submit-note"]')!;
  await act(async () => submit.click());
  expect(captureAndStop).toHaveBeenCalledWith(host.querySelector("video"), mode, config.camera, camera.stop);
  expect(Array.from(sizes, button => button.disabled)).toEqual([true, true]);
  await act(async () => finishCapture({ blob, mode, width: 800, height: 600 }));
  expect(submitReading).toHaveBeenCalledWith("run", expect.any(String), blob, mode, expect.any(AbortSignal), "book");
});

test("moving page seven to one renumbers the page list and starts slideshow playback there", async () => {
  const pages: PlayableCapture[] = Array.from({ length: 7 }, (_, index) => ({
    id: `id-${index + 1}`, run_id: `capture-run-${index}`, narration: `Original ${index + 1}`,
    language: "en", created_at: 1, audio_chunks: 1, thumbnail_url: null,
  }));
  const moved = [pages[6], ...pages.slice(0, 6)];
  let saved = false;
  vi.mocked(allBookPages).mockImplementation(async () => saved ? moved : pages);
  vi.mocked(moveBookPage).mockRejectedValueOnce(new Error("Could not save. Try again.")).mockImplementation(async () => {
    saved = true;
    return { page_order: moved.map(item => `${item.run_id}/${item.id}`) };
  });
  vi.mocked(readingOriginal).mockResolvedValue(new Blob(["page"]));
  vi.mocked(readingAudio).mockResolvedValue(new Blob(["audio"]));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  await act(async () => root.render(<Camera config={config} book={{ id: "book", name: "Story" }} run="run" preview={false} back={() => {}} />));
  const pageCards = () => Array.from(host.querySelectorAll(".studio-page-copy"));
  const option = host.querySelector<HTMLElement>('[aria-label="Page 7 options"]')!;
  await act(async () => option.click());
  await act(async () => Array.from(option.parentElement!.querySelectorAll("button")).find(button => button.textContent === "Move to page…")!.click());
  const dialog = host.querySelector<HTMLDialogElement>('dialog[aria-label="Move page"]')!;
  expect(dialog.open).toBe(true);
  expect(dialog.querySelector("h2")?.textContent).toBe("Move page 7");
  const input = dialog.querySelector("input")!;
  const submit = dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  const choose = (value: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await choose("8");
  expect(submit.disabled).toBe(true);
  await choose("1");
  expect(submit.disabled).toBe(false);
  await act(async () => submit.click());
  expect(dialog.querySelector('[role="alert"]')?.textContent).toContain("Could not save");
  expect(pageCards()[6].textContent).toContain("Original 7");

  await act(async () => submit.click());
  expect(moveBookPage).toHaveBeenLastCalledWith("run", "book", {
    run_id: pages[6].run_id, page_id: pages[6].id, page_number: 1,
  }, expect.any(AbortSignal));
  expect(host.contains(dialog)).toBe(false);
  for (const [index, card] of pageCards().entries()) {
    expect(card.querySelector("strong")?.textContent).toContain(`Page ${index + 1} ·`);
    expect(card.textContent).toContain(moved[index].narration);
  }
  expect(host.textContent).toContain("Page 7 moved to page 1.");
  await act(async () => host.querySelector<HTMLButtonElement>(".studio-section-title .studio-actions button")!.click());
  expect(host.querySelector(".book-slideshow-counter")?.textContent).toBe("Page 1 of 7");
  expect(readingAudio).toHaveBeenCalledWith(pages[6].run_id, pages[6].id, 0, expect.any(AbortSignal));
});

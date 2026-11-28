// @vitest-environment happy-dom
import { act, createRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { allBookPages, submitReading } from "../api/client";
import type { PlayableCapture, PublicConfig } from "../api/contracts.generated";
import { Camera } from "./Camera";
import { captureAndStop } from "./frame";
import { useCamera } from "./useCamera";

vi.mock("../api/client", async importOriginal => ({ ...await importOriginal<typeof import("../api/client")>(), allBookPages: vi.fn(), submitReading: vi.fn() }));
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

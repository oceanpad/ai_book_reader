import { renderToStaticMarkup } from "react-dom/server";
import { expect, test, vi } from "vitest";
import * as reading from "./useReading";
import type { PublicConfig, PlayableCapture } from "../api/contracts.generated";
import { Camera } from "../camera/Camera";
import { HistoryCards } from "./HistoryCards";
import { BusyDialog } from "./BusyDialog";

test("the initial reader offers Start without showing a viewfinder or processing dialog", () => {
  const html = renderToStaticMarkup(<Camera book={{id: "earlier", name: "Earlier captures"}} run="test" preview={false} back={() => {}} config={{ camera: {}, recognition_available: true, speech_available: true } as PublicConfig} />);
  expect(html).toMatch(/<div[^>]*hidden[^>]*class="viewfinder"/);
  expect(html).toContain("Start Reading");
  expect(html).not.toContain(">Submit</button>");
  expect(html).not.toContain("<dialog");
});

test("a submitted capture offers editing above the recognized text without loading book history", () => {
  const original = reading.useReading;
  const spy = vi.spyOn(reading, "useReading").mockImplementation((run, book) => ({ ...original(run, book), job: {
    id: "page", state: "READY", narration: "Freshly recognized", language: "en", stage: "speech", audio_chunks: 1,
    reason: null, retryable: false, vision_model: "v", speech_model: "s", voice: "voice", prompt_version: "1",
  } }));
  try {
    const html = renderToStaticMarkup(<Camera book={{id: "book", name: "Story"}} run="run" preview={false} back={() => {}} config={{ camera: {} } as PublicConfig} />);
    expect(html).toMatch(/Selected capture.*?>Edit text<\/button>.*?Freshly recognized.*?Book pages/s);
  } finally { spy.mockRestore(); }
});

test("named book details end with a deletion action and explicit confirmation", () => {
  const html = renderToStaticMarkup(<Camera book={{id: "named", name: "Story"}} run="test" preview={false} back={() => {}} config={{ camera: {} } as PublicConfig} />);
  expect(html.indexOf("Book pages")).toBeLessThan(html.indexOf(">Delete book</button>"));
  expect(html).toMatch(/<dialog[^>]*aria-label="Delete book"[^>]*>/);
  expect(html).toContain("Delete Story?");
  expect(html).toContain(">Cancel</button>");
  expect(html).toContain(">Confirm delete book</button>");
});

test("busy feedback announces its stage and has a keyboard-accessible way out", () => {
  const html = renderToStaticMarkup(<BusyDialog message="Generating narration…" onStop={() => {}} />);
  expect(html).toContain('<p role="status">Generating narration…</p>');
  expect(html).toContain('<button>Stop waiting</button>');
});

test("cards bound narration excerpts and offer older pages, replay and deletion even without a thumbnail", () => {
  const item: PlayableCapture = { run_id: "run", id: "id", narration: "word ".repeat(35), language: "en", created_at: 10, audio_chunks: 1, thumbnail_url: null };
  const noop = () => {};
  const history = { items: [item], loading: false, error: false, refreshHistory: noop };
  const html = renderToStaticMarkup(<HistoryCards book={{ id: "book", name: "Story" }} run="run" history={history} disabled={false} play={noop} remove={async () => {}} />);
  expect(html).toContain("Slide Play");
  expect(html).toContain("Delete capture from");
  expect(html).toContain("Page 1 ·");
  expect(html).toContain("…</span>");
  expect(html).not.toContain(item.narration);
});

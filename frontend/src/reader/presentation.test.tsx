import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";
import type { PublicConfig, PlayableCapture } from "../api/contracts.generated";
import { Camera } from "../camera/Camera";
import { HistoryCards } from "./HistoryCards";
import { BusyDialog } from "./BusyDialog";

test("the initial reader offers Start without showing a viewfinder or processing dialog", () => {
  const html = renderToStaticMarkup(<Camera config={{ camera: {}, recognition_available: true, speech_available: true } as PublicConfig} />);
  expect(html).toMatch(/<div[^>]*hidden[^>]*class="viewfinder"/);
  expect(html).toContain("Start Reading");
  expect(html).not.toContain(">Submit</button>");
  expect(html).not.toContain("<dialog");
});

test("busy feedback announces its stage and has a keyboard-accessible way out", () => {
  const html = renderToStaticMarkup(<BusyDialog message="Generating narration…" onStop={() => {}} />);
  expect(html).toContain('<p role="status">Generating narration…</p>');
  expect(html).toContain('<button>Stop waiting</button>');
});

test("cards bound narration excerpts and offer older pages, replay and deletion even without a thumbnail", () => {
  const item: PlayableCapture = { run_id: "run", id: "id", narration: "word ".repeat(35), language: "en", created_at: 10, audio_chunks: 1, thumbnail_url: null };
  const noop = () => {};
  const history = { items: [item] as [PlayableCapture], loading: false, error: false, older: false, hasNext: true, refreshHistory: noop, retry: noop, next: noop, previous: noop };
  const html = renderToStaticMarkup(<HistoryCards history={history} disabled={false} play={noop} remove={async () => {}} />);
  expect(html).toContain("Browse all captures");
  expect(html).toContain("Delete capture from");
  expect(html).toContain("Play ·");
  expect(html).toContain("…</span>");
  expect(html).not.toContain(item.narration);
});

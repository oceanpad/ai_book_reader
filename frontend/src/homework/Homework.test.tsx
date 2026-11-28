// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, test, vi } from "vitest";
import { getHomework, homeworkHistory, submitHomework } from "../api/client";
import type { HomeworkEntry } from "../api/contracts.generated";
import { Homework } from "./Homework";

vi.mock("../api/client", () => ({ homeworkHistory: vi.fn(), submitHomework: vi.fn(), deleteHomework: vi.fn(), getHomework: vi.fn() }));
vi.mock("./photos", () => ({ maxPhotos: 4, encodeWorksheet: async (file: File) => file.name }));

const entry: HomeworkEntry = {
  id: "homework-entry", state: "ready", created_at: "2026-11-23T11:00:00Z", level: "primary2", subject: "chinese", error: null,
  result: { note: "", questions: [{ question: "小狗在做什么？", answer: "跑步。", steps: ["看看小狗的脚。"], language: "zh" }] },
};

class Utterance {
  constructor(public text: string) {}
  lang = "";
  rate = 1;
}

test("uploads photos together and reads a saved Chinese answer only when requested", async () => {
  const speak = vi.fn<(utterance: Utterance) => void>();
  const cancel = vi.fn();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("speechSynthesis", { speak, cancel });
  vi.stubGlobal("SpeechSynthesisUtterance", Utterance);
  vi.mocked(homeworkHistory).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(submitHomework).mockResolvedValue(entry);
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  const button = (label: string) => Array.from(host.querySelectorAll("button")).find(item => item.textContent === label)!;
  try {
    await act(async () => root.render(<Homework enabled />));
    const [subject, level] = host.querySelectorAll("select");
    expect(Array.from(level.options, option => [option.value, option.textContent])).toEqual([
      ["kindergarten", "Kindergarten"],
      ...Array.from({ length: 6 }, (_, index) => [`primary${index + 1}`, `Primary ${index + 1}`]),
    ]);
    await act(async () => {
      subject.value = "chinese";
      subject.dispatchEvent(new Event("change", { bubbles: true }));
      level.value = "primary2";
      level.dispatchEvent(new Event("change", { bubbles: true }));
      const input = host.querySelector('input[type="file"]')!;
      expect(input.hasAttribute("multiple")).toBe(true);
      Object.defineProperty(input, "files", { value: [new File(["1"], "one.jpg"), new File(["2"], "two.jpg")] });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => button("Check photos").click());
    expect(submitHomework).toHaveBeenCalledWith(expect.any(String), {
      subject: "chinese", level: "primary2", images: ["one.jpg", "two.jpg"],
    }, expect.anything());
    expect(host.querySelector("h3")?.textContent).toBe("小狗在做什么？");
    expect(host.querySelector(".homework-steps")?.textContent).toBe("看看小狗的脚。");
    expect(host.querySelector(".homework-history")).not.toBeNull();
    expect(speak).not.toHaveBeenCalled();
    await act(async () => button("Read out answer").click());
    expect(speak).toHaveBeenCalledWith(expect.objectContaining({ lang: "zh-CN", text: expect.stringContaining("跑步。") }));
    await act(async () => button("Stop reading").click());
    expect(cancel).toHaveBeenCalledTimes(1);

    vi.mocked(homeworkHistory).mockResolvedValue({ items: [entry], next_cursor: null });
    await act(async () => root.render(<Homework key="new-visit" enabled />));
    expect(host.querySelector("h3")).toBeNull();
    await act(async () => host.querySelector<HTMLButtonElement>(".homework-history-item")!.click());
    expect(host.querySelector("h3")?.textContent).toBe("小狗在做什么？");
    expect(speak).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  }
});

test("a pending check opened from All History updates when the worker is done", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(homeworkHistory).mockResolvedValue({ items: [{ ...entry, state: "pending", result: null }], next_cursor: null });
  vi.mocked(getHomework).mockResolvedValue(entry);
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<Homework enabled />));
    await act(async () => host.querySelector<HTMLButtonElement>(".homework-history-item")!.click());
    expect(host.textContent).toContain("Checking your photos.");
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(getHomework).toHaveBeenCalledWith(expect.any(String), entry.id, expect.anything());
    expect(host.querySelector("h3")?.textContent).toBe("小狗在做什么？");
    expect(host.querySelector(".homework-history-item")?.textContent).toContain("Ready");
  } finally {
    await act(async () => root.unmount());
    host.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  }
});

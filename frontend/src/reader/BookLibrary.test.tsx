// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, test, vi } from "vitest";
import { createBook, listBooks, updateBook } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { BookLibrary } from "./BookLibrary";
import { BookHeading } from "./BookHeading";

vi.mock("../api/client", async importOriginal => ({ ...await importOriginal<typeof import("../api/client")>(), listBooks: vi.fn(), createBook: vi.fn(), updateBook: vi.fn() }));
vi.mock("../camera/Camera", () => ({ Camera: () => <p>Book opened</p> }));
beforeEach(() => vi.clearAllMocks());

async function choose(host: HTMLElement, selector: string, value: string) {
  await act(async () => {
    const select = host.querySelector<HTMLSelectElement>(selector)!;
    select.value = value; select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

test("search filters the shelf locally and clearing a miss restores the books and search focus", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(listBooks).mockResolvedValue({ items: [{ id: "a", name: "Garden journal" }, { id: "b", name: "A summer story" }] });
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookLibrary config={{} as PublicConfig} />));
    const input = host.querySelector<HTMLInputElement>('input[type="search"]')!;
    const type = (text: string) => act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await type("  GARDEN ");
    expect(host.querySelectorAll(".book-title")).toHaveLength(1);
    expect(host.querySelector(".book-title")?.textContent).toContain("Garden journal");
    await type("winter");
    expect(host.textContent).toContain("No matching books");
    await act(async () => host.querySelector<HTMLButtonElement>(".shelf-empty button")!.click());
    expect(host.querySelectorAll(".book-title")).toHaveLength(2);
    expect(document.activeElement).toBe(input);
    expect(listBooks).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals();
  }
});

test("defaults to newest and ranks by completed reads without losing undated books", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(listBooks).mockResolvedValue({ items: [
    { id: "u", name: "Undated" }, { id: "o", name: "Older", created_at: 100, read_count: 4 },
    { id: "n", name: "Newest", created_at: 200 },
  ] });
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookLibrary config={{} as PublicConfig} />));
    const titles = () => Array.from(host.querySelectorAll(".book-title strong"), el => el.textContent);
    const select = host.querySelector<HTMLSelectElement>('select[aria-label="Sort books"]')!;
    const choose = (value: string) => act(async () => {
      select.value = value; select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(titles()).toEqual(["Newest", "Older", "Undated"]);
    await choose("oldest"); expect(titles()).toEqual(["Older", "Newest", "Undated"]);
    await choose("most-read"); expect(titles()).toEqual(["Older", "Newest", "Undated"]);
    await choose("least-read"); expect(titles()).toEqual(["Newest", "Undated", "Older"]);
  } finally {
    await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals();
  }
});

test("language filters combine with title search and keep unclassified books in All languages", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(listBooks).mockResolvedValue({ items: [
    { id: "en", name: "Garden", language: "en", level: "P1" },
    { id: "zh", name: "Garden in Chinese", language: "zh", level: "P6" },
    { id: "old", name: "Old book" },
  ] });
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookLibrary config={{} as PublicConfig} />));
    expect(host.querySelectorAll(".book-title")).toHaveLength(3);
    await choose(host, '[aria-label="Filter books by language"]', "zh");
    expect(host.querySelectorAll(".book-title")).toHaveLength(1);
    expect(host.querySelector(".book-title")?.textContent).toContain("P6 · Chinese");
    await choose(host, '[aria-label="Filter books by language"]', "en");
    expect(host.querySelector(".book-title")?.textContent).toContain("P1 · English");
    await act(async () => {
      const input = host.querySelector<HTMLInputElement>('input[type="search"]')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Chinese");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(host.textContent).toContain("No matching books");
    await act(async () => host.querySelector<HTMLButtonElement>(".shelf-empty button")!.click());
    expect(host.querySelectorAll(".book-title")).toHaveLength(3);
    expect(listBooks).toHaveBeenCalledOnce();
  } finally { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});

test("new book creation sends the selected level and language", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(listBooks).mockResolvedValue({ items: [] });
  vi.mocked(createBook).mockResolvedValue({ id: "new", name: "Story", level: "P4", language: "zh" });
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookLibrary config={{} as PublicConfig} />));
    await act(async () => host.querySelector<HTMLButtonElement>(".banner-action")!.click());
    expect(host.querySelector<HTMLSelectElement>("#new-book-level")!.value).toBe("");
    expect(host.querySelector<HTMLSelectElement>("#new-book-language")!.value).toBe("");
    await act(async () => {
      const input = host.querySelector<HTMLInputElement>("#book-name")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Story");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await choose(host, "#new-book-level", "P4");
    await choose(host, "#new-book-language", "zh");
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(createBook).toHaveBeenCalledWith(expect.any(String), "Story", expect.any(AbortSignal), { level: "P4", language: "zh" });
    expect(host.textContent).toBe("Book opened");
  } finally { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});

test("editing metadata works without changing the title and preserves the other book details", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(updateBook).mockResolvedValue(undefined);
  const updated = vi.fn();
  const book = { id: "old", name: "Old book", created_at: 100, read_count: 3 };
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookHeading book={book} run="run" disabled={false} updated={updated} />));
    await act(async () => host.querySelector("button")!.click());
    await choose(host, "#edit-book-level", "P2");
    await choose(host, "#edit-book-language", "en");
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(updateBook).toHaveBeenCalledWith("run", "old", { name: "Old book", level: "P2", language: "en" }, expect.any(AbortSignal));
    expect(updated).toHaveBeenCalledWith({ ...book, level: "P2", language: "en" });
  } finally { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});

test("the icon-only refresh button has an accessible name and reloads the shelf", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let finishRefresh!: (result: Awaited<ReturnType<typeof listBooks>>) => void;
  vi.mocked(listBooks)
    .mockResolvedValueOnce({ items: [{ id: "a", name: "Garden journal" }] })
    .mockImplementationOnce(() => new Promise(resolve => { finishRefresh = resolve; }));
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => root.render(<BookLibrary config={{} as PublicConfig} />));
    const refresh = host.querySelector<HTMLButtonElement>('button[aria-label="Refresh books"]')!;
    expect(refresh.textContent).toBe("");
    expect(refresh.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
    expect(refresh.disabled).toBe(false);
    await act(async () => refresh.click());
    expect(refresh.disabled).toBe(true);
    await act(async () => finishRefresh({ items: [{ id: "b", name: "A summer story" }] }));
    expect(refresh.disabled).toBe(false);
    expect(host.querySelector(".book-title")?.textContent).toContain("A summer story");
    expect(host.textContent).not.toContain("Garden journal");
  } finally {
    await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals();
  }
});

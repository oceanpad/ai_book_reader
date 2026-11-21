// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, test, vi } from "vitest";
import { listBooks } from "../api/client";
import type { PublicConfig } from "../api/contracts.generated";
import { BookLibrary } from "./BookLibrary";

vi.mock("../api/client", async importOriginal => ({ ...await importOriginal<typeof import("../api/client")>(), listBooks: vi.fn() }));

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
    const select = host.querySelector("select")!;
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

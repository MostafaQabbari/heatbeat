// What this file does: test setup for Vitest: clean the page after each test, and add the
// browser features jsdom lacks (ResizeObserver; canvas, which axe asks for).
import { cleanup } from "@testing-library/vue";
import { afterEach } from "vitest";

afterEach(cleanup);

class NoResize {
  observe(): void {}
  disconnect(): void {}
  unobserve(): void {}
}
globalThis.ResizeObserver ??= NoResize as unknown as typeof ResizeObserver;
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;

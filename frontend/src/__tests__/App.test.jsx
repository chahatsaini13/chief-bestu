import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "../App.jsx";

const okResponse = (overrides = {}) => ({
  ok: true,
  status: 200,
  json: async () => ({
    session_id: "s-x",
    reply: "Paris, bhai!",
    metadata: {
      resolution_source: "LIVE_INFERENCE",
      model: "gemini/gemini-2.0-flash",
      latency_ms: 812,
      input_tokens: 42,
      output_tokens: 9,
      key_id: "key_1",
      fallback_attempts: 0,
      ...overrides,
    },
  }),
});

beforeEach(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.restoreAllMocks());

test("sends a message, clears input, renders every meta testid", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(okResponse());
  render(<App />);
  const input = screen.getByTestId("chat-input");
  await userEvent.type(input, "What is the capital of France?");
  await userEvent.click(screen.getByTestId("send-button"));

  expect(input.value).toBe("");
  const msg = await screen.findByTestId("assistant-message");
  expect(msg.textContent).toContain("Paris, bhai!");
  expect(screen.getByTestId("meta-source").textContent).toBe("LIVE_INFERENCE");
  expect(screen.getByTestId("meta-model").textContent).toBe("gemini/gemini-2.0-flash");
  expect(screen.getByTestId("meta-latency").textContent).toBe("812 ms");
  expect(screen.getByTestId("meta-input-tokens").textContent).toBe("42");
  expect(screen.getByTestId("meta-output-tokens").textContent).toBe("9");

  const [url, opts] = fetchMock.mock.calls[0];
  expect(url).toMatch(/\/chat$/);
  const body = JSON.parse(opts.body);
  expect(body.session_id).toMatch(/^s-/);
  expect(body.message).toBe("What is the capital of France?");
  expect(body.system_prompt).toBeUndefined();
});

test("Enter sends the message", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(okResponse({ resolution_source: "LLM_CACHE", key_id: null }));
  render(<App />);
  await userEvent.type(screen.getByTestId("chat-input"), "hello{Enter}");
  await screen.findByTestId("assistant-message");
  expect(screen.getByTestId("meta-source").textContent).toBe("LLM_CACHE");
});

test("422 array detail shows a readable error, not a blank screen", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: false,
    status: 422,
    json: async () => ({ detail: [{ loc: ["body", "message"], msg: "Value error, message must be 1-4000 characters after trimming", type: "value_error" }] }),
  });
  render(<App />);
  await userEvent.type(screen.getByTestId("chat-input"), "x{Enter}");
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/message: message must be 1-4000/);
  expect(screen.queryByTestId("assistant-message")).toBeNull();
});

test("503 and network failure are shown as readable errors", async () => {
  const spy = vi.spyOn(globalThis, "fetch");
  spy.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: "all providers unavailable" }) });
  render(<App />);
  await userEvent.type(screen.getByTestId("chat-input"), "one{Enter}");
  expect((await screen.findByRole("alert")).textContent).toMatch(/all providers unavailable/);

  spy.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await userEvent.type(screen.getByTestId("chat-input"), "two{Enter}");
  await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
  expect(screen.getAllByRole("alert")[1].textContent).toMatch(/connect nahi/);
});

test("system prompt and simulate-failure header are sent when set", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(okResponse());
  render(<App />);
  await userEvent.click(screen.getByText(/boardroom settings/i));
  await userEvent.type(screen.getByTestId("system-prompt-input"), "You are a pirate.");
  await userEvent.type(screen.getByTestId("simulate-failure-input"), "key_1=429");
  await userEvent.type(screen.getByTestId("chat-input"), "hi{Enter}");
  await screen.findByTestId("assistant-message");
  const [, opts] = fetchMock.mock.calls[0];
  expect(JSON.parse(opts.body).system_prompt).toBe("You are a pirate.");
  expect(opts.headers["X-Simulate-Failure"]).toBe("key_1=429");
});

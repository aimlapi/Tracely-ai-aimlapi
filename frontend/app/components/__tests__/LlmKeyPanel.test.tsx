import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_LLM_GATEWAY, LlmKeyPanel } from "@/app/components/LlmKeyPanel";

describe("LlmKeyPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let current: { configured: boolean; provider?: string };

  beforeEach(() => {
    current = { configured: false };
    fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        current = { configured: true, provider: body.provider };
      }
      return new Response(JSON.stringify(current), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("starts a workspace with no key on AI/ML API", async () => {
    expect(DEFAULT_LLM_GATEWAY).toBe("aimlapi");
    render(<LlmKeyPanel />);
    const select = (await screen.findByLabelText("LLM provider")) as HTMLSelectElement;
    expect(select.value).toBe("aimlapi");
  });

  it("keeps a configured OpenRouter workspace on OpenRouter", async () => {
    current = { configured: true, provider: "openrouter" };
    render(<LlmKeyPanel />);
    expect(await screen.findByText(/A workspace OpenRouter key is configured/)).toBeTruthy();
    fireEvent.click(screen.getByText("Replace"));
    const select = (await screen.findByLabelText("LLM provider")) as HTMLSelectElement;
    expect(select.value).toBe("openrouter");
  });

  it("saves an AI/ML API key with its provider", async () => {
    render(<LlmKeyPanel />);
    const select = (await screen.findByLabelText("LLM provider")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "aimlapi" } });
    fireEvent.change(screen.getByLabelText("AI/ML API API key"), { target: { value: "k-test" } });
    fireEvent.click(screen.getByText("Save key"));
    await waitFor(() => expect(screen.getByText(/A workspace AI\/ML API key is configured/)).toBeTruthy());
    const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({ api_key: "k-test", provider: "aimlapi" });
  });

  it("shows the gateway a configured workspace saved its key with", async () => {
    current = { configured: true, provider: "aimlapi" };
    render(<LlmKeyPanel />);
    expect(await screen.findByText(/A workspace AI\/ML API key is configured/)).toBeTruthy();
  });
});

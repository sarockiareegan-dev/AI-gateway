import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { renderWithProviders, screen, waitFor, within } from "../../../tests/test-utils";
import { apiClient } from "@/components/networking";
import KeyRequestsTab from "./KeyRequestsTab";
import type { KeyRequest } from "./keyRequests";

vi.mock("@/components/networking", () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ accessToken: "token", userId: "bob" }),
}));

vi.mock("@/lib/toast", () => ({
  toast: { success: vi.fn(), fromError: vi.fn() },
}));

const modelCheckbox = (model: string) =>
  within(screen.getByText(model).closest("label") as HTMLElement).getByRole("checkbox");

const request = (overrides: Partial<KeyRequest>): KeyRequest => ({
  request_id: "req-1",
  team_id: "team-a",
  user_id: "bob",
  key_alias: "bob-dev",
  models: ["gpt-4o", "claude"],
  max_budget: 10,
  duration: "30d",
  reason: "prototype",
  status: "pending",
  key_ready: false,
  ...overrides,
});

describe("KeyRequestsTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lets a member request a key for the team", async () => {
    vi.mocked(apiClient.get).mockResolvedValue([]);
    vi.mocked(apiClient.post).mockResolvedValue(request({}));
    renderWithProviders(<KeyRequestsTab teamId="team-a" teamModels={["gpt-4o"]} canReview={false} />);

    expect(await screen.findByText("No key requests yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Request key/ }));
    await userEvent.type(screen.getByLabelText("Key name"), "bob-dev");
    await userEvent.click(modelCheckbox("gpt-4o"));
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith("/key_request/new", {
        accessToken: "token",
        body: expect.objectContaining({ team_id: "team-a", key_alias: "bob-dev", models: ["gpt-4o"] }),
      }),
    );
  });

  it("shows an approved key to its requester once and hides review actions", async () => {
    vi.mocked(apiClient.get).mockResolvedValue([request({ status: "approved", key_ready: true })]);
    vi.mocked(apiClient.post).mockResolvedValue({ key: "sk-collected", key_token: "tok" });
    renderWithProviders(<KeyRequestsTab teamId="team-b" teamModels={[]} canReview={false} />);

    await userEvent.click(await screen.findByRole("button", { name: /View key/ }));

    expect(await screen.findByText("sk-collected")).toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledWith("/key_request/req-1/key", { accessToken: "token", body: {} });
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("lets a reviewer narrow the models when approving", async () => {
    vi.mocked(apiClient.get).mockResolvedValue([request({ user_id: "vera" })]);
    vi.mocked(apiClient.post).mockResolvedValue(request({ status: "approved" }));
    renderWithProviders(<KeyRequestsTab teamId="team-c" teamModels={["gpt-4o", "claude"]} canReview />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    await userEvent.click(modelCheckbox("claude"));
    await userEvent.click(screen.getByRole("button", { name: "Approve and create key" }));

    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith("/key_request/req-1/approve", {
        accessToken: "token",
        body: expect.objectContaining({ models: ["gpt-4o"], max_budget: 10, duration: "30d" }),
      }),
    );
  });
});

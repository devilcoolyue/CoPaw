import { request } from "../request";

export interface BrowserStatus {
  running: boolean;
  headless: boolean;
  current_page_id: string | null;
  url: string;
  viewport: { width: number; height: number };
  agent_id: string;
}

export const browserApi = {
  getStatus: (agentId?: string) =>
    request<BrowserStatus>(
      `/browser/status${
        agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""
      }`,
    ),
};

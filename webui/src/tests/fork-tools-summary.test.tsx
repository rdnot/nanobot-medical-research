// FORK: the fork's per-turn "Tools used:" summary arrives as a second assistant
// message with kind "tools_summary". It must render as its own bubble and must
// NOT capture the answer's folded activity trace (completedMessageBlocks used to
// attach activity to "the next assistant message", which became the summary).
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { projectThreadEvents } from "@/lib/thread-event-projection";
import type { ThreadProjectionEvent } from "@/lib/types";

afterEach(() => cleanup());

const T = "turn-1";
const SUMMARY_TEXT =
  "**Tools used:**\n- search(`sepsis`)\n- fetch(`https://www.bbc.com/news`)";

const base = [
  { event: "user_message", chat_id: "c", text: "do it", turn_id: T, starts_turn: true },
  { event: "reasoning_delta", chat_id: "c", text: "The user wants me to edit", turn_id: T, turn_phase: "reasoning" },
  { event: "reasoning_end", chat_id: "c", turn_id: T, turn_phase: "reasoning" },
  { event: "message", chat_id: "c", text: "search(sepsis)", kind: "tool_hint", turn_id: T },
  {
    event: "file_edit", chat_id: "c", turn_id: T, turn_phase: "activity",
    edits: [{ version: 1, call_id: "e1", tool: "edit_file", path: "test_tools.md", phase: "end", added: 1, deleted: 1, status: "done" }],
  },
  { event: "message", chat_id: "c", text: "web_fetch(bbc)", kind: "tool_hint", turn_id: T },
  { event: "delta", chat_id: "c", text: "All done.", turn_id: T },
  { event: "stream_end", chat_id: "c", text: "All done.", turn_id: T },
] as unknown as ThreadProjectionEvent[];

const summary = {
  event: "message", chat_id: "c", turn_id: T, text: SUMMARY_TEXT, kind: "tools_summary",
} as unknown as ThreadProjectionEvent;
const end = { event: "turn_end", chat_id: "c", turn_id: T, latency_ms: 9000 } as unknown as ThreadProjectionEvent;

interface RowReport {
  text: string;
  toggle: string | null;
  expanded: string | null;
}

/** Render the thread, open every row's block menu, and record the activity toggle. */
function inspectRows(events: ThreadProjectionEvent[]): { rows: RowReport[]; messages: ReturnType<typeof projectThreadEvents> } {
  const messages = projectThreadEvents(events);
  const { container } = render(<ThreadMessages messages={messages} isStreaming={false} />);
  const rows: RowReport[] = [];
  for (const unit of Array.from(container.querySelectorAll<HTMLElement>("[data-thread-display-unit]"))) {
    const trigger = unit.querySelector<HTMLElement>("[data-message-block-menu-trigger]");
    const report: RowReport = { text: unit.textContent ?? "", toggle: null, expanded: null };
    if (trigger) {
      fireEvent.click(trigger);
      const action = document.querySelector<HTMLElement>("[data-message-block-activity-action]");
      if (action) {
        report.toggle = action.textContent;
        fireEvent.click(action);
        report.expanded =
          document.querySelector("[data-testid='agent-activity-content']")?.textContent ?? null;
      }
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    }
    rows.push(report);
  }
  return { rows, messages };
}

function expectIntactTrace(events: ThreadProjectionEvent[]): void {
  const { rows, messages } = inspectRows(events);

  // The summary is its own assistant message and does not change the answer.
  const answer = messages.find((m) => m.role === "assistant" && m.content === "All done.");
  expect(answer).toBeTruthy();
  expect(answer?.kind).toBeUndefined();
  const summaryMessage = messages.find((m) => m.kind === "tools_summary");
  expect(summaryMessage?.content).toBe(SUMMARY_TEXT);
  expect(summaryMessage?.role).toBe("assistant");

  // The answer row keeps the whole folded trace.
  const answerRow = rows.find((r) => r.text.includes("All done."));
  expect(answerRow?.toggle).toBe("Worked for 9s");
  expect(answerRow?.expanded).toContain("The user wants me to edit");
  expect(answerRow?.expanded).toContain("Completed Search");
  expect(answerRow?.expanded).toContain("Edited test_tools.md");
  expect(answerRow?.expanded).toContain("Read bbc");

  // The summary is a visible bubble with no activity toggle of its own.
  const summaryRow = rows.find((r) => r.text.includes("Tools used:"));
  expect(summaryRow).toBeTruthy();
  expect(summaryRow?.toggle).toBeNull();
}

describe("fork tools summary", () => {
  it("keeps the answer's full folded trace when the summary precedes turn_end", () => {
    expectIntactTrace([...base, summary, end]);
  });

  it("keeps the answer's full folded trace when the summary arrives after turn_end", () => {
    expectIntactTrace([...base, end, summary]);
  });

  it("replays identically from persisted history", () => {
    // Persisted transcript events carry created_at_ms and the same kind.
    const persisted = [...base, summary, end].map((event, index) => ({
      ...(event as object),
      created_at_ms: 1_700_000_000_000 + index,
    })) as unknown as ThreadProjectionEvent[];
    expectIntactTrace(persisted);
  });

  it("never folds the summary text into the answer message", () => {
    const messages = projectThreadEvents([...base, summary, end]);
    const answers = messages.filter((m) => m.role === "assistant" && m.kind !== "tools_summary");
    expect(answers).toHaveLength(1);
    expect(answers[0].content).toBe("All done.");
    expect(answers[0].content).not.toContain("Tools used:");
  });

  it("does not offer answer-level fork on the summary bubble", () => {
    const { container } = render(
      <ThreadMessages messages={projectThreadEvents([...base, summary, end])} isStreaming={false} />,
    );
    const summaryUnit = Array.from(
      container.querySelectorAll<HTMLElement>("[data-thread-display-unit]"),
    ).find((unit) => unit.textContent?.includes("Tools used:"));
    expect(summaryUnit).toBeTruthy();
    expect(summaryUnit?.querySelector("[data-message-block-menu-trigger]")).toBeNull();
  });
});

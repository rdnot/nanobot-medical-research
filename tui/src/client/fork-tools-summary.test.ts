// FORK: a "tools_summary" message arrives after the answer's stream_end. History
// decoding must keep it as its own row and never overwrite the streamed answer
// (a kind-less message after stream_end used to replace stream.row.content).
import { afterEach, expect, test } from "bun:test"
import { fetchHistory } from "."

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

function respond(events: Array<Record<string, unknown>>) {
  globalThis.fetch = Object.assign(async () => Response.json({
    schemaVersion: 3, projection: "events",
    events: events.map((event) => ({ chat_id: "chat", turn_id: "turn", ...event })),
    page: {},
  }), { preconnect: originalFetch.preconnect })
}

const SUMMARY = "**Tools used:**\n- search(`sepsis`)"

test("a tools_summary after stream_end does not overwrite the answer", async () => {
  respond([
    { event: "user_message", starts_turn: true, text: "question" },
    { event: "delta", text: "All done." },
    { event: "stream_end", text: "All done." },
    { event: "message", kind: "tools_summary", text: SUMMARY },
    { event: "turn_end" },
  ])
  const { messages } = await fetchHistory("http://fixture", "token", "chat")
  const answers = messages.filter((row) => row.role === "assistant")
  expect(answers).toHaveLength(1)
  expect(answers[0]?.content).toBe("All done.")
  expect(messages.some((row) => row.content === SUMMARY)).toBe(true)
  expect(messages.some((row) => row.role === "assistant" && row.content.includes("Tools used"))).toBe(false)
})

test("a tools_summary arriving after turn_end is still its own row", async () => {
  respond([
    { event: "user_message", starts_turn: true, text: "question" },
    { event: "stream_end", text: "All done." },
    { event: "turn_end" },
    { event: "message", kind: "tools_summary", text: SUMMARY },
  ])
  const { messages } = await fetchHistory("http://fixture", "token", "chat")
  expect(messages.filter((row) => row.role === "assistant").map((row) => row.content)).toEqual(["All done."])
  expect(messages.some((row) => row.content === SUMMARY)).toBe(true)
})

test("a tools_summary does not overwrite an answer whose stream is still open", async () => {
  // resuming + merge_next keeps `stream` open; a kind-less message would then
  // replace stream.row.content with the message text.
  respond([
    { event: "user_message", starts_turn: true, text: "question" },
    { event: "stream_end", text: "All done.", resuming: true, merge_next: true },
    { event: "message", kind: "tools_summary", text: SUMMARY },
    { event: "turn_end" },
  ])
  const { messages } = await fetchHistory("http://fixture", "token", "chat")
  expect(messages.filter((row) => row.role === "assistant").map((row) => row.content)).toEqual(["All done."])
  expect(messages.some((row) => row.content === SUMMARY)).toBe(true)
})

test("a kind-less message keeps upstream behaviour (control)", async () => {
  // Closed stream: the message is a second assistant row. Open stream: it
  // replaces the streamed text. Both are upstream semantics the fork keeps.
  respond([
    { event: "user_message", starts_turn: true, text: "question" },
    { event: "stream_end", text: "draft" },
    { event: "message", text: "final answer" },
    { event: "turn_end" },
  ])
  const closed = await fetchHistory("http://fixture", "token", "chat")
  expect(closed.messages.filter((row) => row.role === "assistant").map((row) => row.content))
    .toEqual(["draft", "final answer"])

  respond([
    { event: "user_message", starts_turn: true, text: "question" },
    { event: "stream_end", text: "draft", resuming: true, merge_next: true },
    { event: "message", text: "final answer" },
    { event: "turn_end" },
  ])
  const open = await fetchHistory("http://fixture", "token", "chat")
  expect(open.messages.filter((row) => row.role === "assistant").map((row) => row.content))
    .toEqual(["final answer"])
})

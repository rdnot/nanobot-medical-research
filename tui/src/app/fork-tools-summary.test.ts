// FORK: live handling of the fork's "tools_summary" message in the TUI. It must
// not become finalMessage (that is only shown when nothing streamed) and must not
// replace the streamed answer; it appears as its own transcript row instead.
import { afterEach, describe, expect, test } from "bun:test"
import { createTestRenderer, type TestRendererSetup } from "@opentui/core/testing"
import { mount } from "./test-support"

describe("tools_summary in the live TUI", () => {
  let setup: TestRendererSetup | undefined
  afterEach(() => {
    if (setup && !setup.renderer.isDestroyed) setup.renderer.destroy()
    setup = undefined
  })

  const internals = (app: unknown) => app as { finalMessage: string; turnHadAnswer: boolean }

  test("does not set finalMessage and keeps the streamed answer", async () => {
    setup = await createTestRenderer({ width: 90, height: 30, screenMode: "alternate-screen" })
    const app = mount(setup)
    app.accept({ event: "attached", chat_id: "chat" })
    app.accept({ event: "delta", chat_id: "chat", text: "All done." })
    app.accept({ event: "stream_end", chat_id: "chat", text: "All done." })
    app.accept({
      event: "message", chat_id: "chat", kind: "tools_summary",
      text: "**Tools used:**\n- search(`sepsis`)",
    })
    expect(internals(app).finalMessage).toBe("")
    expect(internals(app).turnHadAnswer).toBe(true)

    app.accept({ event: "turn_end", chat_id: "chat" })
    await Bun.sleep(1)
    await setup.flush()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("All done.")
    expect(frame).toContain("sepsis")
  })

  test("a kind-less message still becomes finalMessage (control)", async () => {
    setup = await createTestRenderer({ width: 90, height: 30, screenMode: "alternate-screen" })
    const app = mount(setup)
    app.accept({ event: "attached", chat_id: "chat" })
    app.accept({ event: "message", chat_id: "chat", text: "plain final" })
    expect(internals(app).finalMessage).toBe("plain final")
  })
})

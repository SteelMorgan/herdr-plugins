import { describe, expect, it } from "vitest";
import { parseAnsi } from "../../ansi";
import { splitLines, lineText } from "../../blocks";
import { locateComposer, extractInputDraft } from "./chrome";
import { isStatusRow } from "./markers";

const footer = "  \x1b[0m\x1b[38;2;246;226;183mGPT-6.1-Sol me…\x1b[0m \x1b[0m\x1b[38;5;5mPursuing goal (6d 11h 57m)\x1b[0m  ";
const screen = (row: string) => splitLines(parseAnsi("› Ask Codex to do anything\r\n\r\n" + row + "\r\n  \x1b[0m\x1b[1m?\x1b[0m for shortcuts"));

describe("local narrow Codex goal footer", () => {
  it("finds the live composer in the captured narrow platsec footer", () => {
    expect(locateComposer(screen(footer))).not.toBeNull();
  });
  it("recognizes the paused goal and preserves a wrapped PAM draft", () => {
    const row = footer.replace("Pursuing goal (6d 11h 57m)", "Goal paused (/goal resume)");
    const lines = splitLines(parseAnsi("› слушай, а остальные проблемы с операциями\r\n  ты проверил и отладил? осталось только\r\n  отражение фин результата ?\r\n\r\n" + row + "\r\n\r\n"));
    expect(locateComposer(lines)).not.toBeNull();
    expect(extractInputDraft(lines)).toBe("слушай, а остальные проблемы с операциями ты проверил и отладил? осталось только отражение фин результата ?");
  });
  it("recognizes the stalled goal with the warning shortcut footer", () => {
    const row = footer.replace("Pursuing goal (6d 11h 57m)", "Goal stalled (/goal resume)");
    const lines = splitLines(parseAnsi("\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\r\n\r\n" + row + "\r\n  \x1b[1m?\x1b[0m for shortcuts                    ⚠ \x1b[38;2;196;167;103m1\x1b[0m · \x1b[1mf2\x1b[0m "));
    expect(locateComposer(lines)).not.toBeNull();
    expect(extractInputDraft(lines)).toBeNull();
  });
  it("refuses unpainted prose and a footer without the live prompt", () => {
    expect(locateComposer(screen("  GPT-6.1-Sol me… Pursuing goal (6d 11h 57m)"))).toBeNull();
    expect(locateComposer(splitLines(parseAnsi(footer)))).toBeNull();
  });
  it("refuses truncated menu text, a non-truncated field, and shared field colours", () => {
    for (const row of [footer.replace("Pursuing goal (6d 11h 57m)", "enter select · esc back"), footer.replace("me…", "medium"), footer.replace("\x1b[38;5;5m", "\x1b[38;2;246;226;183m")]) {
      const line = splitLines(parseAnsi(row))[0]!;
      expect(isStatusRow(lineText(line), line)).toBe(false);
    }
  });
});

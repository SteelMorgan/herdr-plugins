#!/usr/bin/env python3
"""Apply the owner-maintained narrow Codex footer fix to a Collie checkout."""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
p = root / 'web/src/lib/harness/codex/markers.ts'
s = p.read_text()
tag = '// Local Collie overlay: narrow Codex goal footer v1.'
helper = '''// Local Collie overlay: narrow Codex goal footer v1.
// A narrow pane can truncate every left status field into one coloured field.
// Require that truncation, a painted gap, and the exact coloured goal notice.
// locateComposer still requires the live prompt and status at the buffer tail.
function isNarrowGoalStatusRow(text: string, line: StyledLine): boolean {
  const row = rstrip(text);
  if (rstrip(lineText(line)) !== row || hasControlChar(row)
      || codePointCount(row) > MAX_STATUS_ROW_CHARS) return false;
  const folded = foldTrailingPadding(line.segments);
  if (folded === null) return false;
  const parts = splitPaintedGaps(folded);
  if (parts.length < 4 || !isIndentSegment(parts[0]!)
      || !isFieldSegment(parts[1]!) || !parts[1]!.text.endsWith("…")
      || !isUnstyled(parts[2]!) || !/^ +$/.test(parts[2]!.text)) return false;
  const notice = parts.slice(3);
  return isRightNotice(notice)
    && /^(?:Pursuing goal \\(\\d+[dhms](?: \\d+[dhms])*\\)|Goal (?:paused|stalled) \\(\\/goal resume\\))$/.test(notice.map(s => s.text).join(""))
    && notice.every(s => s.fg !== parts[1]!.fg);
}

'''
old = 'return line !== undefined && isStyledStatusRow(text, line);'
new = 'return line !== undefined && (isStyledStatusRow(text, line) || isNarrowGoalStatusRow(text, line));'
if tag not in s:
    anchor = 'export function isStatusRow(text: string, line?: StyledLine): boolean {'
    if s.count(anchor) != 1 or s.count(old) != 1:
        sys.exit('Collie overlay: upstream grammar changed; inspect before updating. No patch applied.')
    s = s.replace(anchor, helper + anchor).replace(old, new)
    p.write_text(s)
elif new not in s or helper not in s:
    paused_helper = helper.replace('Goal (?:paused|stalled)', 'Goal paused')
    old_helper = helper.replace(
        '/^(?:Pursuing goal \\(\\d+[dhms](?: \\d+[dhms])*\\)|Goal (?:paused|stalled) \\(\\/goal resume\\))$/',
        '/^Pursuing goal \\(\\d+[dhms](?: \\d+[dhms])*\\)$/')
    prior = next((candidate for candidate in [paused_helper, old_helper] if s.count(candidate) == 1), None)
    if new not in s or prior is None:
        sys.exit('Collie overlay: partial or changed patch; inspect before updating.')
    s = s.replace(prior, helper)
    p.write_text(s)
test = pathlib.Path(__file__).with_name('narrow-goal.test.ts').read_text()
(root / 'web/src/lib/harness/codex/narrow-goal.local.test.ts').write_text(test)
print('Collie narrow-goal overlay applied:', root)

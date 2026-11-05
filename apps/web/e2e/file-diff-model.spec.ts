import { expect, test } from '@playwright/test';
import { diffInlineRanges, gitDiffLines, intralineDiff, splitDiffRows } from '../src/components/fileDiffModel';

const changedText = (before: string, after: string) => {
  const ranges = intralineDiff(before, after);
  return {
    before: ranges.before.map(range => before.slice(range.start, range.end)),
    after: ranges.after.map(range => after.slice(range.start, range.end)),
  };
};

test('intraline edits isolate appended numbers, replacements, separate islands and whitespace', () => {
  expect(changedText('发布 1123', '发布 1123123')).toEqual({ before: [], after: ['123'] });
  expect(changedText("print('old')", "print('new')")).toEqual({ before: ['old'], after: ['new'] });
  expect(changedText('foo = 10; bar = 20;', 'foo = 90; bar = 30;')).toEqual({ before: ['1', '2'], after: ['9', '3'] });
  expect(changedText('  value\t', '    value ')).toEqual({ before: ['\t'], after: ['  ', ' '] });
  expect(changedText('remove this', '')).toEqual({ before: ['remove this'], after: [] });
  expect(changedText('same', 'same')).toEqual({ before: [], after: [] });
});

test('intraline offsets preserve Unicode characters and bound work on huge replacements', () => {
  expect(changedText('中文😀说明旧', '中文😁说明新')).toEqual({ before: ['😀', '旧'], after: ['😁', '新'] });
  expect(changedText(`prefix${'a'.repeat(20_000)}suffix`, `prefix${'b'.repeat(20_000)}suffix`)).toEqual({ before: ['a'.repeat(20_000)], after: ['b'.repeat(20_000)] });
});

test('intraline alignment excludes context, unpaired lines and separate Git hunks', () => {
  const lines = gitDiffLines('@@ -1,3 +1,3 @@\n same\n-old\n+new\n+extra\n@@ -10 +10 @@\n-unpaired\n@@ -20 +20 @@\n+separate\n');
  const ranges = diffInlineRanges(splitDiffRows(lines, (previous, next) => previous.hunk !== next.hunk));
  expect([...ranges.keys()]).toEqual([1, 2]);
  expect(ranges.get(1)).toEqual([{ start: 0, end: 3 }]);
  expect(ranges.get(2)).toEqual([{ start: 0, end: 3 }]);
});

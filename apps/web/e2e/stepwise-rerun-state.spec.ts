import { expect, test } from '@playwright/test';
import { activeRerunSource } from '../src/stepwiseRerun';

test('a rerun draft stays active only while its source is the newest execution', () => {
  expect(activeRerunSource(undefined, [{ id: 'a', state: 'ACCEPTED' }])).toBeUndefined();
  expect(activeRerunSource('a', [{ id: 'a', state: 'ACCEPTED' }])).toBe('a');
  // Agent already reran the node: the newer execution (and its session) must show.
  expect(activeRerunSource('a', [{ id: 'a', state: 'ACCEPTED' }, { id: 'b', state: 'ACCEPTED' }])).toBeUndefined();
  // A cancelled newer execution does not displace the source.
  expect(activeRerunSource('a', [{ id: 'a', state: 'ACCEPTED' }, { id: 'b', state: 'CANCELLED' }])).toBe('a');
});

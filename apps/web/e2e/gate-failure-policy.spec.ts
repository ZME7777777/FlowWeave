import { expect, test } from '@playwright/test';
import { readOnFail, writeOnFail } from '../src/utils/gateFailurePolicy';

test('the default is stored as no declaration so existing gates are unchanged', () => {
  expect(writeOnFail({ prompt: 'p', on_fail: { action: 'STOP' } }, 'ESCALATE')).toEqual({ prompt: 'p' });
  expect(readOnFail({ prompt: 'p' })).toEqual({ action: 'ESCALATE', rounds: 1 });
});

test('rerun keeps a bounded round count and stop carries none', () => {
  expect(writeOnFail({ prompt: 'p' }, 'RERUN', 2).on_fail).toEqual({ action: 'RERUN', max_rounds: 2 });
  expect(writeOnFail({}, 'RERUN', 99).on_fail).toEqual({ action: 'RERUN', max_rounds: 3 });
  expect(writeOnFail({}, 'RERUN', 0).on_fail).toEqual({ action: 'RERUN', max_rounds: 1 });
  expect(writeOnFail({ on_fail: { action: 'RERUN', max_rounds: 2 } }, 'STOP').on_fail).toEqual({ action: 'STOP' });
});

test('invalid stored values fall back to the default and other config is preserved', () => {
  expect(readOnFail({ on_fail: { action: 'FORK' } })).toEqual({ action: 'ESCALATE', rounds: 1 });
  expect(readOnFail({ on_fail: { action: 'RERUN', max_rounds: 9 } })).toEqual({ action: 'RERUN', rounds: 1 });
  const config = { prompt: 'p', business_result_contract: { schema_version: 1 } };
  expect(writeOnFail(config, 'STOP')).toMatchObject({ prompt: 'p', business_result_contract: { schema_version: 1 } });
  expect(config).not.toHaveProperty('on_fail');
});

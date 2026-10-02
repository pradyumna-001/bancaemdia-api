import { check } from 'k6';
import { coletaWaitMs } from '../scenarios/pacing.js';

export const options = { thresholds: { checks: ['rate==1'] } };

export default function () {
  const cases = [
    [1000, 1500, {}, false, 5500],
    [1000, 8000, {}, false, 0],
    [1000, 1500, { 'X-Ratelimit-Remaining': '0', 'X-Ratelimit-Reset': '62.5' }, false, 61000],
    [1000, 1500, { 'x-ratelimit-remaining': '1', 'x-ratelimit-reset': '62.5' }, false, 5500],
    [1000, 1500, { 'X-RATELIMIT-REMAINING': '0', 'X-RATELIMIT-RESET': 'invalid' }, false, 5500],
    [1000, 8000, { 'X-Ratelimit-Remaining': '0', 'X-Ratelimit-Reset': '6' }, false, 0],
    [1000, 1200, { 'X-Ratelimit-Remaining': '0', 'X-Ratelimit-Reset': '62.5' }, true, 300],
  ];
  for (const [index, [start, now, headers, smoke, expected]] of cases.entries()) {
    check(coletaWaitMs(start, now, headers, smoke), {
      [`cadence and quota boundary ${index}`]: (actual) => actual === expected,
    });
  }
}

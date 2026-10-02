import { check } from 'k6';
import { handleSummary } from '../load-test.js';

export const options = { thresholds: { checks: ['rate==1'] } };

export default function () {
  const metric = { type: 'trend', values: { 'p(95)': 321 }, thresholds: { 'p(95)<1000': { ok: true } } };
  const reports = handleSummary({
    setup_data: { jwt: ['synthetic-private-jwt'], coleta: ['synthetic-private-coleta'] },
    metrics: { http_req_duration: metric },
    state: { testRunDurationMs: 300000 },
  });
  const json = JSON.parse(reports['load-test-summary.json']);
  check(reports, {
    'credentials never enter JSON or HTML artifacts': (output) =>
      !Object.prototype.hasOwnProperty.call(json, 'setup_data') &&
      Object.values(output).every((value) => !value.includes('synthetic-private-')),
    'measurement values and gates remain in the report': () =>
      json.metrics.http_req_duration.values['p(95)'] === 321 &&
      json.metrics.http_req_duration.thresholds['p(95)<1000'].ok &&
      json.state.testRunDurationMs === 300000,
  });
}

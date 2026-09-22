// Fast contract smoke test for the k6 script. It uses the local mock server,
// never the staging environment and never real credentials.
import { options as loadOptions } from './config.js';
export { steadyState, spikeLoad, coletaBurst, painelRead, probeMetrics, handleSummary } from './load-test.js';

export const options = {
  thresholds: loadOptions.thresholds,
  scenarios: Object.fromEntries(
    ['steady', 'spike', 'coleta', 'painel'].map((name, index) => [name, {
      executor: 'constant-vus', exec: {
        steady: 'steadyState', spike: 'spikeLoad', coleta: 'coletaBurst', painel: 'painelRead',
      }[name], vus: 1, duration: '3s', startTime: `${index * 3}s`, gracefulStop: '0s',
    }]),
  ),
};
options.scenarios.telemetry = {
  executor: 'constant-vus', exec: 'probeMetrics', vus: 1,
  duration: '12s', gracefulStop: '0s',
};

export function setup() {
  return { jwt: Array(250).fill('smoke-jwt'), coleta: Array(100).fill('smoke-coleta') };
}

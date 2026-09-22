import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { authHeaders, baseUrl, getPanel, verify } from './common.js';

// One fixture is intentionally shared across VUs. The real extraction cache can
// serve repeated images; see README for the resulting workload limitation.
const exportZip = open('../fixtures/telegram-small.zip', 'b');
const state = {};

export function uploadJourney(token, intervalSeconds) {
  const journey = exec.scenario.name;
  if (!state[journey]) {
    const response = http.post(`${baseUrl}/api/v1/upload`, {
      file: http.file(exportZip, 'telegram-small.zip', 'application/zip'),
    }, { headers: authHeaders(token), tags: { name: 'POST /api/v1/upload' }, timeout: '20s' });
    if (verify(response, 'upload accepted', 202, (r) => !!r.json('job_id'))) {
      state[journey] = { statusUrl: response.json('status_url'), done: false };
    } else {
      state[journey] = { statusUrl: null, done: true };
    }
  }

  const job = state[journey];
  if (job.statusUrl && !job.done) {
    const status = http.get(`${baseUrl}${job.statusUrl}`, {
      headers: authHeaders(token), tags: { name: 'GET /api/v1/upload/{job_id}' }, timeout: '15s',
    });
    if (verify(status, 'upload status readable', 200)) {
      const outcome = status.json('status');
      if (outcome === 'failed') {
        verify(status, 'upload completed', 200, () => false);
        job.done = true;
      } else if (outcome === 'completed') {
        job.done = true;
      }
    }
  }
  getPanel(token);
  sleep(intervalSeconds);
}

import http from 'k6/http';
import { sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { baseUrl } from './common.js';

const queueDepth = new Trend('extraction_queue_depth');
const replicaLag = new Trend('replica_lag');
const available = new Rate('telemetry_available');

function metric(body, name, label) {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = body.match(new RegExp(`^${name}\\{[^\\n}]*${escaped}[^\\n}]*\\} ([^\\s]+)$`, 'm'));
  if (!match) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function sampleMetrics() {
  const url = __ENV.METRICS_URL || `${baseUrl}/metrics`;
  const response = http.get(url, { tags: { name: 'GET /metrics' }, timeout: '10s' });
  const depth = response.status === 200 ? metric(response.body, 'celery_queue_depth', 'queue="extraction"') : null;
  const lag = response.status === 200 ? metric(response.body, 'pg_replication_lag_seconds', 'role="replica"') : null;
  const valid = depth !== null && lag !== null;
  available.add(valid);
  // Missing telemetry must never look like a healthy zero.
  queueDepth.add(depth === null ? 1e9 : depth);
  replicaLag.add(lag === null ? 1e9 : lag);
  sleep(__ENV.K6_LOCAL_SMOKE === '1' ? 0.5 : 15);
}

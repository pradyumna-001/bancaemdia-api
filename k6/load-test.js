import { options, selectedProfile } from './config.js';
import exec from 'k6/execution';
import { credentials, getPanel, pauseToInterval } from './scenarios/common.js';
import { uploadJourney } from './scenarios/upload.js';
import { sendColeta } from './scenarios/coleta.js';
import { sampleMetrics } from './scenarios/telemetry.js';

export { options };
const uploadInterval = __ENV.K6_LOCAL_SMOKE === '1' ? 0.5 : 10;

export function setup() {
  return credentials(selectedProfile);
}

export function steadyState(data) {
  uploadJourney(data.jwt[exec.vu.idInScenario - 1], uploadInterval);
}

export function spikeLoad(data) {
  const offset = selectedProfile === 'all' ? 50 : 0;
  uploadJourney(data.jwt[offset + exec.vu.idInScenario - 1], uploadInterval);
}

export function coletaBurst(data) {
  sendColeta(data.coleta[exec.vu.idInScenario - 1]);
}

export function painelRead(data) {
  const started = Date.now();
  getPanel(data.jwt[exec.vu.idInScenario - 1]);
  pauseToInterval(started, uploadInterval);
}

export function probeMetrics() {
  sampleMetrics();
}

function escape(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

export function handleSummary(data) {
  const evidence = {
    ...data,
    bancaemdia: {
      profile: selectedProfile,
      environment: __ENV.APP_ENV || null,
      release_sha: __ENV.RELEASE_SHA || null,
      generated_at: new Date().toISOString(),
    },
  };
  const rows = Object.entries(data.metrics || {})
    .filter(([name]) => ['http_req_duration', 'http_req_failed', 'checks_pass_rate',
      'extraction_queue_depth', 'replica_lag', 'telemetry_available'].includes(name))
    .map(([name, metric]) => `<tr><td>${escape(name)}</td><td>${escape(JSON.stringify(metric.values || {}))}</td><td>${escape(JSON.stringify(metric.thresholds || {}))}</td></tr>`)
    .join('\n');
  const html = `<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>k6 load test</title>
<style>body{font:16px system-ui;margin:2rem;max-width:80rem}table{border-collapse:collapse;width:100%}td,th{border:1px solid #aaa;padding:.5rem;text-align:left}td{word-break:break-word}</style>
<h1>BancaEmDia — teste de carga</h1><p>Perfil: ${escape(selectedProfile)} · Gerado em ${escape(new Date().toISOString())}</p>
<table><thead><tr><th>Métrica</th><th>Valores</th><th>Limiares</th></tr></thead><tbody>${rows}</tbody></table></html>`;
  return { 'load-test-report.html': html, 'load-test-summary.json': JSON.stringify(evidence, null, 2) };
}

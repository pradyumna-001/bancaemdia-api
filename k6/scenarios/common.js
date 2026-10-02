import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';
import exec from 'k6/execution';

export const checksPassRate = new Rate('checks_pass_rate');
export const baseUrl = (__ENV.BASE_URL || '').replace(/\/$/, '');
const leases = {};

export function tokenFor(data, kind) {
  if (__ENV.K6_LOCAL_SMOKE === '1') return data[kind][0];
  const scenario = exec.scenario.name;
  if (!leases[scenario]) {
    const origin = __ENV.K6_CREDENTIALS_URL || '';
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) {
      throw new Error('Run staging through python k6/run.py to allocate distinct credentials');
    }
    const response = http.get(`${origin}/${scenario}/${exec.vu.idInTest}`, {
      tags: { name: 'local credential lease', staging_api: 'false' }, timeout: '5s',
    });
    if (response.status !== 200 || !response.json('token')) {
      throw new Error('Could not allocate a distinct staging credential');
    }
    leases[scenario] = response.json('token');
  }
  return leases[scenario];
}

function parseTokens(name, minimum) {
  let tokens;
  try {
    tokens = [];
    for (const part of [name, ...[1, 2, 3, 4, 5].map((index) => `${name}_${index}`)]) {
      if (__ENV[part]) tokens.push(...JSON.parse(__ENV[part]));
    }
  } catch (_) {
    throw new Error(`${name} must be a JSON array of staging tokens`);
  }
  if (!Array.isArray(tokens) || tokens.length < minimum || tokens.some((token) => typeof token !== 'string' || !token)) {
    throw new Error(`${name} must contain at least ${minimum} nonempty staging tokens`);
  }
  return tokens;
}

export function credentials(profile) {
  if (!/^https:\/\//.test(baseUrl) && !(__ENV.ALLOW_HTTP_LOCAL === '1' && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(baseUrl))) {
    throw new Error('BASE_URL must be an HTTPS staging origin (or localhost with ALLOW_HTTP_LOCAL=1)');
  }
  const jwtCount = profile === 'all' ? 250 : profile === 'spike' ? 200 : profile === 'steady' ? 50 : profile === 'painel' ? 100 : profile === 'cd-smoke' ? 1 : 0;
  const coletaCount = profile === 'all' || profile === 'coleta' ? 100 : 0;
  return {
    jwt: jwtCount ? parseTokens('JWT_TOKENS_JSON', jwtCount) : [],
    coleta: coletaCount ? parseTokens('COLETA_TOKENS_JSON', coletaCount) : [],
  };
}

export function verify(response, name, expected, predicate = () => true) {
  const passed = check(response, { [name]: (r) => r.status === expected && predicate(r) });
  checksPassRate.add(passed);
  if (!passed) console.warn(`${name}: HTTP ${response.status}, expected ${expected}`);
  return passed;
}

export function authHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

export function getPanel(token) {
  const response = http.get(`${baseUrl}/api/v1/painel`, {
    headers: authHeaders(token), tags: { name: 'GET /api/v1/painel' }, timeout: '15s',
  });
  verify(response, 'painel returns data', 200, (r) => !!r.json('resumo'));
  return response;
}

export function pauseToInterval(started, seconds) {
  sleep(Math.max(0, seconds - (Date.now() - started) / 1000));
}

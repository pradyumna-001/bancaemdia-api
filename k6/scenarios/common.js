import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

export const checksPassRate = new Rate('checks_pass_rate');
export const baseUrl = (__ENV.BASE_URL || '').replace(/\/$/, '');

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

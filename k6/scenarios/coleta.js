import http from 'k6/http';
import { baseUrl, pauseToInterval, verify } from './common.js';

export function sendColeta(token) {
  const started = Date.now();
  const now = Date.now();
  const id = `${now}${__VU}${__ITER}`;
  const payload = {
    contrato: 1, casa: 'betano', capturado_em: new Date(now).toISOString(),
    apostas: [{
      id, bonusType: 0, totalAmount: 10,
      totalAmountWithCurrency: { amount: 10, currencyCode: 'BRL' },
      totalOdds: 1.9, finalWinnings: 0, placedAt: now,
      legs: [{ legItems: [{ eventId: id, eventName: 'Carga - Teste',
        startTime: now + 3600000,
        selections: [{ description: 'Casa', odds: 1.9 }],
      }] }],
    }],
  };
  const response = http.post(`${baseUrl}/coleta`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json', 'X-Coleta-Token': token },
    tags: { name: 'POST /coleta' }, timeout: '15s',
  });
  verify(response, 'coleta accepted', 200);
  pauseToInterval(started, __ENV.K6_LOCAL_SMOKE === '1' ? 0.5 : 6);
}

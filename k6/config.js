const profile = __ENV.LOAD_PROFILE || 'all';

const steady = {
  executor: 'ramping-vus', exec: 'steadyState', startTime: '0s',
  stages: [
    { duration: '5m', target: 50 },
    { duration: '20m', target: 50 },
    { duration: '5m', target: 0 },
  ],
  gracefulRampDown: '0s',
};

const spike = {
  executor: 'ramping-vus', exec: 'spikeLoad', startTime: profile === 'all' ? '30m' : '0s',
  stages: [
    { duration: '2m', target: 200 },
    { duration: '6m', target: 200 },
    { duration: '2m', target: 0 },
  ],
  gracefulRampDown: '0s',
};

const coleta = {
  executor: 'constant-vus', exec: 'coletaBurst', vus: 100, duration: '5m',
  startTime: profile === 'all' ? '40m' : '0s', gracefulStop: '0s',
};

const painel = {
  executor: 'constant-vus', exec: 'painelRead', vus: 100, duration: '10m',
  startTime: profile === 'all' ? '45m' : '0s', gracefulStop: '0s',
};

const cdSmoke = {
  executor: 'constant-vus', exec: 'painelRead', vus: 1, duration: '30s',
  startTime: '0s', gracefulStop: '0s',
};

const profiles = { steady, spike, coleta, painel, 'cd-smoke': cdSmoke };
if (profile !== 'all' && !profiles[profile]) {
  throw new Error(`LOAD_PROFILE must be all, steady, spike, coleta, painel, or cd-smoke; got ${profile}`);
}

const durations = { all: '55m', steady: '30m', spike: '10m', coleta: '5m', painel: '10m', 'cd-smoke': '30s' };

export const options = {
  scenarios: {
    ...(profile === 'all' ? profiles : { [profile]: profiles[profile] }),
    ...(profile === 'cd-smoke' ? {} : { telemetry: {
      executor: 'constant-vus', exec: 'probeMetrics', vus: 1,
      duration: durations[profile], startTime: '0s', gracefulStop: '0s',
    } }),
  },
  thresholds: {
    http_req_duration: ['p(95)<1000', 'p(99)<2000'],
    http_req_failed: ['rate<0.01'],
    checks_pass_rate: ['rate>0.99'],
    ...(profile === 'cd-smoke' ? {} : {
      extraction_queue_depth: ['max<100'],
      replica_lag: ['max<30'],
      telemetry_available: ['rate==1'],
    }),
  },
};

export const selectedProfile = profile;

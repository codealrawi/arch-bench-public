// Сценарий нагрузки k6 для ОДНОЙ ступени (протокол v2.1). Все параметры — из окружения
// (experiments/orchestrator/k6runner.py ← bench.yaml). Значений по умолчанию нет.
//
// Профиль (решение автора M-1): 9 реальных операций API v1, доли — MIX_JSON.
// Фазы ступени: ramp → warmup → measure; фаза — тег `phase` в момент НАЧАЛА запроса.
// setup() (вне измерений): JWT для пула пользователей, пул id одобренных постов из ленты.
import http from 'k6/http';
import exec from 'k6/execution';
import { sleep, randomSeed } from 'k6';
import { SharedArray } from 'k6/data';
import { Gauge } from 'k6/metrics';

const scenarioStart = new Gauge('arch_bench_scenario_start_ms');

function req(name) {
  const v = __ENV[name];
  if (v === undefined || v === '') throw new Error(`переменная окружения ${name} обязательна`);
  return v;
}
function reqNum(name) {
  const v = Number(req(name));
  if (!Number.isFinite(v)) throw new Error(`${name} должна быть числом`);
  return v;
}

const BASE = req('TARGET_BASE').replace(/\/$/, '');
const ROUTES = JSON.parse(req('ROUTES_JSON'));   // {auth, content, stats} → префикс шлюза
const MIX = JSON.parse(req('MIX_JSON'));          // {операция: доля}
const SEARCH_TERMS = JSON.parse(req('SEARCH_TERMS_JSON'));
const VUS = reqNum('VUS');
const RAMP_S = reqNum('RAMP_S');
const WARMUP_S = reqNum('WARMUP_S');
const MEASURE_S = reqNum('MEASURE_S');
const THINK_S = reqNum('THINK_S');
const TIMEOUT_S = reqNum('TIMEOUT_S');
const SEED = reqNum('K6_SEED');
const LOGIN_POOL = reqNum('LOGIN_POOL');
const USERS = reqNum('USERS');
const POST_POOL_PAGES = reqNum('POST_POOL_PAGES');
const EMAIL_TEMPLATE = req('EMAIL_TEMPLATE');
const PW_TEMPLATE = req('PASSWORD_TEMPLATE');
const DOMAIN = {
  feed: 'content', get_post: 'content', get_profile: 'auth', search: 'content',
  create_post: 'content', create_comment: 'content', reaction: 'content',
  update_profile: 'auth', stats: 'stats',
};
const OPS = Object.keys(MIX).sort();
const total = OPS.reduce((s, k) => s + MIX[k], 0);
if (Math.abs(total - 1) > 1e-9) throw new Error('доли MIX_JSON должны давать 1');
for (const op of OPS) if (!DOMAIN[op]) throw new Error(`неизвестная операция ${op}`);
const CUM = [];
OPS.reduce((acc, op) => { CUM.push([acc + MIX[op], op]); return acc + MIX[op]; }, 0);

// По элементу на текст: SharedArray разбирает JSON элемента при КАЖДОМ обращении. Прежний вариант
// (весь файл 2,4 МБ одним элементом, TEXTS[0] в каждой итерации) загружал генератор на ~70 % CPU
// уже при 100 VU (пробный прогон на Timeweb, 2026-10-05).
function texts(key) {
  return new SharedArray(`write_texts_${key}`, () => {
    const all = JSON.parse(open(req('TEXTS_FILE')));
    if (!Array.isArray(all[key]) || !all[key].length) throw new Error(`TEXTS_FILE: нет «${key}»`);
    return all[key];
  });
}
const POST_TEXTS = texts('posts');
const COMMENT_TEXTS = texts('comments');
const BIO_TEXTS = texts('bios');
const KINDS = ['like', 'support', 'useful'];

randomSeed(SEED * 100003 + __VU);  // одинакова у всех архитектур для (прогон, ступень)

export const options = {
  scenarios: {
    step: {
      executor: 'ramping-vus', startVUs: 0,
      stages: [{ duration: `${RAMP_S}s`, target: VUS }, { duration: `${WARMUP_S + MEASURE_S}s`, target: VUS }],
      gracefulRampDown: '0s', gracefulStop: `${TIMEOUT_S}s`,
    },
  },
  setupTimeout: '900s',
  discardResponseBodies: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)', 'count'],
  // error — текст ошибки транспорта: без него код 1000 («generic») нельзя отнести к серверу
  // или генератору (серия 1 остановлена 2026-10-06 из-за этого)
  systemTags: ['status', 'method', 'error', 'error_code', 'scenario'],
  thresholds: Object.fromEntries([['http_req_duration{phase:measure}', ['max>=0']],
    ...OPS.map((op) => [`http_req_duration{phase:measure,op:${op}}`, ['max>=0']])]),
};

function url(domain, path) { return `${BASE}${ROUTES[domain]}/api/v1${path}`; }
const JSON_HEADERS = { 'Content-Type': 'application/json' };

export function setup() {
  const tokens = [];
  for (let uid = 1; uid <= LOGIN_POOL; uid++) {
    const body = JSON.stringify({ email: EMAIL_TEMPLATE.replace('{user_id}', String(uid)),
      password: PW_TEMPLATE.replace('{user_id}', String(uid)) });
    const r = http.post(url('auth', '/auth/login'), body, {
      headers: JSON_HEADERS, tags: { phase: 'setup', op: 'login' }, responseType: 'text', timeout: `${TIMEOUT_S}s` });
    if (r.status !== 200) throw new Error(`login user ${uid}: HTTP ${r.status}`);
    tokens.push(JSON.parse(r.body).access_token);
  }
  // пул одобренных постов: страницы ленты (тот же API, что в профиле нагрузки)
  const postIds = [];
  let before = '';
  const auth = { Authorization: `Bearer ${tokens[0]}` };
  for (let page = 0; page < POST_POOL_PAGES; page++) {
    const r = http.get(url('content', `/posts${before}`), {
      headers: auth, tags: { phase: 'setup', op: 'post_pool' }, responseType: 'text', timeout: `${TIMEOUT_S}s` });
    if (r.status !== 200) throw new Error(`post pool: HTTP ${r.status}`);
    const posts = JSON.parse(r.body);
    if (!posts.length) break;
    posts.forEach((p) => postIds.push(p.id));
    before = `?before_id=${posts[posts.length - 1].id}`;
  }
  if (!postIds.length) throw new Error('пустой пул постов');
  return { tokens, postIds };
}

function phaseNow() {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  if (elapsed < RAMP_S) return 'ramp';
  if (elapsed < RAMP_S + WARMUP_S) return 'warmup';
  return 'measure';
}

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function chooseOp() {
  const u = Math.random();
  for (const [edge, op] of CUM) if (u < edge) return op;
  return CUM[CUM.length - 1][1];
}

export default function (data) {
  if (exec.vu.idInTest === 1 && exec.vu.iterationInScenario === 0) {
    scenarioStart.add(exec.scenario.startTime, { phase: 'meta', op: 'meta' });
  }
  const token = data.tokens[(exec.vu.idInTest - 1) % data.tokens.length];
  const op = chooseOp();
  const params = { headers: { Authorization: `Bearer ${token}`, ...JSON_HEADERS },
    timeout: `${TIMEOUT_S}s`, tags: { phase: phaseNow(), op } };
  const d = DOMAIN[op];
  switch (op) {
    case 'feed': http.get(url(d, '/posts'), params); break;
    case 'get_post': http.get(url(d, `/posts/${pick(data.postIds)}`), params); break;
    case 'get_profile': http.get(url(d, `/users/${1 + Math.floor(Math.random() * USERS)}`), params); break;
    case 'search': http.get(url(d, `/search?q=${encodeURIComponent(pick(SEARCH_TERMS))}`), params); break;
    case 'create_post': http.post(url(d, '/posts'), JSON.stringify(pick(POST_TEXTS)), params); break;
    case 'create_comment':
      http.post(url(d, `/posts/${pick(data.postIds)}/comments`), JSON.stringify({ body: pick(COMMENT_TEXTS) }), params); break;
    case 'reaction':
      http.put(url(d, `/posts/${pick(data.postIds)}/reaction`), JSON.stringify({ kind: pick(KINDS) }), params); break;
    case 'update_profile': http.patch(url(d, '/users/me'), JSON.stringify({ bio: pick(BIO_TEXTS) }), params); break;
    case 'stats': http.get(url(d, '/stats'), params); break;
    default: throw new Error(op);
  }
  sleep(THINK_S);
}

const $ = id => document.getElementById(id);
let state = null;
let selected = null;
let editing = false;
let disconnected = false;

function text(id, value) { $(id).textContent = value ?? ''; }
function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = String(value);
  if (className) element.className = className;
  return element;
}
async function command(name, args = {}) {
  const response = await fetch('/api/' + name, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(args)
  });
  if (!response.ok) text('error', await response.text());
}
$('start').onclick = () => {
  if (!state) return;
  command('start', {map: $('mapchoice').value, controller: $('controller').value,
    provider: $('provider').value, seconds: Number($('duration').value), revision: state.config.revision});
};
$('probe').onclick = () => command('probe', {provider: $('provider').value});
$('pause').onclick = () => { if (state) command(state.status === 'paused' ? 'resume' : 'pause'); };
$('stop').onclick = () => command('stop');
$('apply').onclick = () => { command('orders', {text: $('orders').value}); editing = false; };
$('orders').oninput = () => { editing = true; };
$('controller').onchange = $('provider').onchange = () => { if (state) render(state); };

function stat(label, value) {
  const box = node('div', undefined, 'stat');
  box.append(node('small', label), node('strong', value));
  return box;
}
function selectRecord(record) {
  selected = record.id;
  text('record', JSON.stringify(record, null, 2));
  renderDecisions(state.records || []);
}
function renderDecisions(records) {
  const timeline = $('timeline');
  timeline.replaceChildren();
  if (!records.length) timeline.append(node('span', 'No calls yet.', 'muted'));
  for (const record of records) {
    const answer = record.response?.answers?.tactic;
    const probability = answer?.probabilities?.[answer.choice];
    const label = `#${record.id} · ${answer?.choice || 'no choice'} · ${probability == null ? '—' : Math.round(probability * 100) + '%'} · ${record.response?.elapsed_ms ?? '—'} ms · age ${record.decision_age_ticks ?? '—'} ticks`;
    const button = node('button', label);
    button.setAttribute('aria-pressed', String(record.id === selected));
    button.onclick = () => selectRecord(record);
    timeline.append(button);
  }
  const record = records.find(item => item.id === selected) || records.at(-1);
  const answers = record?.response?.answers || {};
  const answer = answers.tactic;
  text('why', record ? `${record.accepted ? 'Applied' : 'Not applied'}: ${answer?.choice || 'no choice'}. ${record.override_reasons?.join('; ') || (answer ? 'Chosen from the supplied options.' : 'No answer was recorded.')}` : 'No decision yet.');
  const judgments = $('judgments');
  judgments.replaceChildren();
  for (const [question, choice] of Object.entries(answers)) {
    judgments.append(node('h3', question));
    for (const [option, probability] of Object.entries(choice.probabilities || {}).sort((a, b) => b[1] - a[1])) {
      const row = node('div', undefined, 'barrow' + (option === choice.choice ? ' winner' : ''));
      const bar = node('div', undefined, 'bar');
      const fill = node('i');
      fill.style.width = `${Math.max(0, Math.min(100, Number(probability) * 100))}%`;
      bar.append(fill);
      row.append(node('span', option), bar, node('span', `${(Number(probability) * 100).toFixed(0)}%`));
      judgments.append(row);
    }
  }
}
function drawMap() {
  const canvas = $('map'), ctx = canvas.getContext('2d');
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const lines = state?.map?.lines || [];
  if (!lines.length) return;
  const points = lines.flatMap(line => [line.a, line.b]);
  const minx = Math.min(...points.map(point => point[0])), maxx = Math.max(...points.map(point => point[0]));
  const miny = Math.min(...points.map(point => point[1])), maxy = Math.max(...points.map(point => point[1]));
  const scale = Math.min(canvas.width / (maxx - minx + 180), canvas.height / (maxy - miny + 180));
  const xy = point => [(point[0] - minx + 90) * scale, canvas.height - (point[1] - miny + 90) * scale];
  for (const line of lines) {
    ctx.strokeStyle = line.blocking ? '#66818c' : '#293c45'; ctx.beginPath();
    ctx.moveTo(...xy(line.a)); ctx.lineTo(...xy(line.b)); ctx.stroke();
  }
  const player = state?.observation?.player;
  if (player) { ctx.fillStyle = '#7be9dc'; ctx.beginPath(); ctx.arc(...xy([player.x, player.y]), 5, 0, 2 * Math.PI); ctx.fill(); }
}
function render(next) {
  state = next;
  const config = state.config || {}, observation = state.observation || {}, player = observation.player || {}, progress = observation.progress || {};
  const replay = config.controller === 'replay';
  if (replay) $('controller').value = 'replay';
  const busy = replay || ['booting', 'warming', 'running', 'paused', 'recovering', 'waiting_provider'].includes(state.status) || state.busy;
  const controller = $('controller').value, provider = $('provider').value;
  const ready = controller === 'local' || (!!state.auth_providers?.[provider] && !!state.provider_health?.[provider]);
  for (const id of ['controller', 'provider', 'mapchoice', 'duration']) $(id).disabled = busy;
  $('start').disabled = busy || !ready;
  $('probe').disabled = busy || !state.auth_providers?.[provider];
  $('pause').disabled = !['running', 'paused', 'warming', 'recovering', 'waiting_provider'].includes(state.status);
  $('pause').textContent = state.status === 'paused' ? 'Resume' : 'Pause';
  $('stop').disabled = replay || !busy;
  $('apply').disabled = replay || !state.observation;
  text('status', state.status); text('error', state.error || '');
  text('selector', config.controller === 'local' ? 'Local rule, no model' : config.controller === 'replay' ? 'Replay' : 'Jev');
  text('setup', replay ? `Recorded run: ${state.result?.reason || 'playing'}; no model or provider call.` : controller === 'local' ? 'Local rule uses no key and makes no model call.' : 'Check the selected provider before starting. Checking makes one billed call.');
  $('stats').replaceChildren(stat('Health', player.health ?? '—'), stat('Armor', player.armor ?? '—'), stat('Ammo', player.ammo ?? '—'), stat('Game seconds', progress.game_seconds ?? '—'));
  text('objective', state.objective?.subject?.label || state.objective?.goal || 'Waiting for a decision.');
  if (!editing && document.activeElement !== $('orders')) $('orders').value = state.order || '';
  text('budget', `Charged or reserved: $${Number(state.budget?.charged_usd || 0).toFixed(4)} / $${Number(config.budget_usd || 0).toFixed(2)} cap`);
  text('latency', `Last decision: ${state.latency?.last_ms == null ? '—' : state.latency.last_ms.toFixed(0) + ' ms'} · ${state.busy ? 'request in flight' : 'no request in flight'}`);
  text('progress', `${progress.visited_cells || 0} visited cells · ${progress.discovered_cells || 0} discovered · ${progress.distance || 0} units travelled`);
  text('inspect', JSON.stringify({tactic: state.tactics, observation}, null, 2));
  renderDecisions(state.records || []);
  drawMap();
}
async function poll() {
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error('state unavailable');
    render(await response.json());
    text('connection', 'LOCAL LIVE');
    if (disconnected) { $('game').src = '/stream?reconnect=' + Date.now(); disconnected = false; }
  } catch (_) { text('connection', 'DISCONNECTED'); disconnected = true; }
  setTimeout(poll, 350);
}
$('game').onerror = () => { disconnected = true; };
window.addEventListener('resize', drawMap);
poll();

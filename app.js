const $ = id => document.getElementById(id);
let state = null;
let selected = null;
let editing = false;
let disconnected = false;
let selectedQuestion = null;
let commandError = null;
let pendingOrder = null;

function text(id, value) { $(id).textContent = value ?? ''; }
function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = String(value);
  if (className) element.className = className;
  return element;
}
async function command(name, args = {}) {
  commandError = null;
  try {
    const response = await fetch('/api/' + name, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(args)
    });
    if (!response.ok) throw new Error(await response.text());
    return true;
  } catch (error) {
    commandError = error.message || 'Command failed';
    text('error', commandError);
    return false;
  }
}
$('start').onclick = () => {
  if (!state) return;
  command('start', {map: $('mapchoice').value, controller: $('controller').value,
    provider: $('provider').value, seconds: Number($('duration').value), revision: state.config.revision});
};
$('probe').onclick = () => command('probe', {provider: $('provider').value});
$('pause').onclick = () => { if (state) command(state.status === 'paused' ? 'resume' : 'pause'); };
$('stop').onclick = () => command('stop');
$('apply').onclick = async () => {
  const value = $('orders').value;
  if (await command('orders', {text: value})) {
    pendingOrder = value;
    if (state?.order === value && $('orders').value === value) { editing = false; pendingOrder = null; }
  }
};
$('orders').oninput = () => { editing = true; };
$('controller').onchange = $('provider').onchange = () => { if (state) render(state); };

function stat(label, value) {
  const box = node('div', undefined, 'stat');
  box.append(node('small', label), node('strong', value));
  return box;
}
function words(value) {
  return String(value ?? '—').replaceAll('_', ' ');
}
function readableLabel(label) {
  return String(label).replace(/front_-?\d+_-?\d+/g, 'unexplored area').replace(/line\d+/g, 'nearby wall');
}
function optionLabel(record, id) {
  if (!id) return 'No choice';
  if (record?.option?.id === id && record.option.label) return readableLabel(record.option.label);
  const description = record?.questions?.tactic?.criteria?.[id];
  return description ? readableLabel(description.split('. Controls ')[0]) : words(id);
}
function fields(target, entries) {
  target.replaceChildren();
  for (const [label, value] of entries) {
    const line = node('p');
    line.append(node('strong', label + ': '), document.createTextNode(String(value ?? '—')));
    target.append(line);
  }
}
function controlsText(controls) {
  if (!controls || !Object.keys(controls).length) return 'Not recorded';
  return Object.entries(controls).map(([key, value]) => `${words(key)}: ${words(value)}`).join(' · ');
}
function selectRecord(record) {
  selected = record.id;
  selectedQuestion = null;
  renderDecisions(state.records || []);
}
$('question').onchange = () => { selectedQuestion = $('question').value; renderDecisions(state?.records || []); };
function renderDecisions(records) {
  const timeline = $('timeline');
  timeline.replaceChildren();
  if (!records.length) timeline.append(node('span', 'No calls yet.', 'muted'));
  for (const record of records) {
    const answer = record.response?.answers?.tactic;
    const probability = answer?.probabilities?.[answer.choice];
    const label = `#${record.id} · ${optionLabel(record, answer?.choice)} · ${probability == null ? '—' : Math.round(probability * 100) + '%'} · ${record.response?.elapsed_ms ?? '—'} ms · age ${record.decision_age_ticks ?? '—'} ticks`;
    const button = node('button', label);
    button.setAttribute('aria-pressed', String(record.id === selected));
    button.onclick = () => selectRecord(record);
    timeline.append(button);
  }
  const record = records.find(item => item.id === selected) || records.at(-1);
  const answers = record?.response?.answers || {};
  const answer = answers.tactic;
  const option = record?.option;
  text('why', record ? `${record.accepted ? 'Applied' : 'Not applied'}: ${optionLabel(record, answer?.choice)}. ${record.override_reasons?.join('; ') || (option?.feasibility ? 'Option feasibility: ' + option.feasibility : 'No written reason recorded.')}` : 'No decision yet.');
  fields($('record'), record ? [
    ['Option', optionLabel(record, answer?.choice)],
    ['Why', record.override_reasons?.join('; ') || 'No written reason recorded'],
    ['Option feasibility', option?.feasibility || 'Not recorded'],
    ['Controls', controlsText(option?.controls || record.composed_intent)],
    ['Prediction', option?.prediction?.text || 'Not recorded'],
    ['Decider', words(record.provider)],
    ['Result', record.accepted ? 'Applied' : 'Not applied']
  ] : [['Decision', 'No decision yet']]);
  text('record-raw', record ? JSON.stringify(record, null, 2) : '');
  const groups = [];
  for (const [question, choice] of Object.entries(answers)) {
    const probabilities = Object.entries(choice.probabilities || {}).sort(([a], [b]) => a.localeCompare(b));
    const key = JSON.stringify([choice.choice, probabilities]);
    let group = groups.find(item => item.key === key);
    if (!group) { group = {key, names: [], choice}; groups.push(group); }
    group.names.push(question);
  }
  const questionSelect = $('question');
  const preferred = groups.find(group => group.names.includes(selectedQuestion)) || groups.find(group => group.names.includes('tactic')) || groups[0];
  questionSelect.replaceChildren();
  for (const group of groups) {
    const optionNode = node('option', group.names.map(words).join(' / '));
    optionNode.value = group.names[0];
    questionSelect.append(optionNode);
  }
  questionSelect.disabled = !groups.length;
  if (preferred) { questionSelect.value = preferred.names[0]; selectedQuestion = preferred.names[0]; }
  const judgments = $('judgments');
  judgments.replaceChildren();
  const choice = preferred?.choice;
  for (const [id, probability] of Object.entries(choice?.probabilities || {}).sort((a, b) => b[1] - a[1])) {
    const row = node('div', undefined, 'barrow' + (id === choice.choice ? ' winner' : ''));
    const bar = node('div', undefined, 'bar');
    const fill = node('i');
    fill.style.width = `${Math.max(0, Math.min(100, Number(probability) * 100))}%`;
    bar.append(fill);
    row.append(node('span', optionLabel(record, id)), bar, node('span', `${(Number(probability) * 100).toFixed(0)}%`));
    judgments.append(row);
  }
}
function drawMap() {
  const canvas = $('map'), ctx = canvas.getContext('2d');
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const lines = state?.map?.lines || [], cells = state?.map?.seen_cells || [];
  const cellSize = state?.map?.cell_size ?? 24;
  const player = state?.observation?.player;
  const points = [...lines.flatMap(line => [line.a, line.b]), ...cells.map(cell => [cell[0] * cellSize, cell[1] * cellSize])];
  if (player) points.push([player.x, player.y]);
  if (!points.length) return;
  const minx = Math.min(...points.map(point => point[0])), maxx = Math.max(...points.map(point => point[0]));
  const miny = Math.min(...points.map(point => point[1])), maxy = Math.max(...points.map(point => point[1]));
  const scale = Math.min(canvas.width / (maxx - minx + 180), canvas.height / (maxy - miny + 180));
  const xy = point => [canvas.width / 2 + (point[0] - (minx + maxx) / 2) * scale, canvas.height / 2 - (point[1] - (miny + maxy) / 2) * scale];
  ctx.fillStyle = '#273b42';
  for (const cell of cells) { const [x, y] = xy([cell[0] * cellSize, cell[1] * cellSize]); const size = Math.max(1, cellSize * scale); ctx.fillRect(x - size / 2, y - size / 2, size, size); }
  ctx.lineWidth = Math.max(1.5 * devicePixelRatio, 2);
  for (const line of lines) {
    ctx.strokeStyle = line.blocking ? '#a8c1c8' : '#6d8b90'; ctx.beginPath();
    ctx.moveTo(...xy(line.a)); ctx.lineTo(...xy(line.b)); ctx.stroke();
  }
  if (player) {
    const [x, y] = xy([player.x, player.y]);
    const angle = Number(player.facing || 0) * Math.PI / 180, size = 8 * devicePixelRatio;
    ctx.save(); ctx.translate(x, y); ctx.rotate(-angle); ctx.fillStyle = '#7be9dc'; ctx.beginPath();
    ctx.moveTo(size, 0); ctx.lineTo(-size * .7, -size * .65); ctx.lineTo(-size * .7, size * .65); ctx.closePath(); ctx.fill(); ctx.restore();
  }
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
  $('provider').hidden = controller === 'local' || replay;
  $('probe').hidden = controller === 'local' || replay;
  $('pause').disabled = !['running', 'paused', 'warming', 'recovering', 'waiting_provider'].includes(state.status);
  $('pause').textContent = state.status === 'paused' ? 'Resume' : 'Pause';
  $('stop').disabled = replay || !busy;
  $('apply').disabled = replay || !state.observation;
  text('status', state.status); text('error', commandError || state.error || '');
  text('selector', config.controller === 'local' ? 'Local rule, no model' : config.controller === 'replay' ? 'Replay' : 'Jev');
  text('setup', replay ? `Recorded run: ${state.result?.reason || 'playing'}; no model or provider call.` : controller === 'local' ? 'Local rule uses no key and makes no model call.' : 'Check the selected provider before starting. Checking makes one billed call.');
  $('stats').replaceChildren(stat('Health', player.health ?? '—'), stat('Armor', player.armor ?? '—'), stat('Ammo', player.ammo ?? '—'), stat('Game seconds', progress.game_seconds ?? '—'));
  text('objective', state.objective?.subject?.label || state.objective?.goal || 'Waiting for a decision.');
  if (pendingOrder !== null && state.order === pendingOrder) {
    if ($('orders').value === pendingOrder) editing = false;
    pendingOrder = null;
  }
  if (!editing && document.activeElement !== $('orders')) $('orders').value = state.order || '';
  text('budget', `Charged or reserved: $${Number(state.budget?.charged_usd || 0).toFixed(4)} / $${Number(config.budget_usd || 0).toFixed(2)} cap`);
  text('latency', `Last decision: ${state.latency?.last_ms == null ? '—' : state.latency.last_ms.toFixed(0) + ' ms'} · ${state.busy ? 'request in flight' : 'no request in flight'}`);
  text('progress', `${progress.visited_cells || 0} visited cells · ${progress.discovered_cells || 0} discovered · ${progress.distance || 0} units travelled`);
  const active = state.tactics?.active;
  fields($('inspect'), [
    ['Option', active?.option?.label ? readableLabel(active.option.label) : 'None'],
    ['Decider', active?.selector === 'local fallback' ? 'Local fallback' : words(active?.selector)],
    ['Controls', controlsText(active?.option?.controls)],
    ['Prediction', active?.option?.prediction?.text || 'Not recorded'],
    ['Visible enemies', observation.enemies?.length ?? 0],
    ['Visible items', observation.items?.length ?? 0]
  ]);
  text('inspect-raw', JSON.stringify({tactic: state.tactics, observation}, null, 2));
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

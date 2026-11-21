import {feedback} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function scheduleTextarea(entries = []) {
  const text = entries.map(item => `${item.market_id},${item.activate_date}`).join('\n');
  return el('label', {class:'field', for:'schedule-text'}, el('span',{},'启用排期（每行：市场,启用日 YYYY-MM-DD）'),
    el('textarea', {id:'schedule-text', name:'entries', rows:8, required:true,
      placeholder:'南桥,2026-11-02\n西苑,2026-11-02'}, text));
}

function parseEntries(raw) {
  const entries = [];
  const seen = new Set();
  raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean).forEach((line, index) => {
    const [market, day] = line.split(',').map(part => part && part.trim());
    if (!market || !/^\d{4}-\d{2}-\d{2}$/.test(day || '')) {
      throw new Error(`第 ${index + 1} 行格式应为「市场,YYYY-MM-DD」`);
    }
    if (seen.has(market)) throw new Error(`市场「${market}」在排期中重复`);
    seen.add(market);
    entries.push({market_id: market, activate_date: day});
  });
  if (!entries.length) throw new Error('请至少填写一条市场启用日');
  return entries;
}

export async function rolloutPage(route, refresh) {
  const {spec, datasets} = workspace;
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const message = feedback();
  if (!selected) return panel('分批启用排期', empty('请先导入门岗数据。', '#inputs', '前往导入'));

  let versions = await workspace.versions(selected.id);
  const active = versions.find(item => item.is_active) || versions[0];

  async function rerender() {
    versions = await workspace.versions(selected.id);
    await refresh();
  }

  // 版本提交（首次排期或更正）
  const submitVersion = button('保存排期版本', {class:'primary', 'data-testid':'save-version'});
  const versionForm = el('form', {class:'fields', onSubmit: handler(submitVersion, message, async event => {
    const data = new FormData(event.target);
    const entries = parseEntries(data.get('entries'));
    const saved = await workspace.submitVersion({
      dataset_id: selected.id, name: data.get('name') || `排期 v${versions.length + 1}`,
      entries, note: data.get('note') || ''});
    message.textContent = saved.created
      ? `已保存第 ${saved.version} 版（${saved.affected_markets.length ? '更正市场：' + saved.affected_markets.join('、') : '首版排期'}）`
      : '该排期内容已存在，未生成新版本';
    await rerender();
  })}, el('label', {class:'field', for:'version-name'}, el('span',{},'版本名称'),
      el('input', {id:'version-name', name:'name', type:'text', maxlength:120,
        placeholder:`排期 v${versions.length + 1}`})),
    scheduleTextarea(active?.entries),
    el('label', {class:'field', for:'version-note'}, el('span',{},'更正说明（可空）'),
      el('input', {id:'version-note', name:'note', type:'text', maxlength:500,
        placeholder:'例如：滨河提前到 11 月 2 日启用'})),
    submitVersion, message);

  // 发起分批测算
  const runButton = button('执行分批双重差分', {class:'primary', 'data-testid':'start-rollout'});
  const runForm = active && el('form', {class:'fields', onSubmit: handler(runButton, message, async event => {
    const data = new FormData(event.target);
    const item = await workspace.startRollout({
      dataset_id: selected.id, name: data.get('name') || `分批测算 v${active.version}`,
      activation_version_id: active.id,
      params: {window_days: Number(data.get('window_days')),
               pretrend_threshold: Number(data.get('pretrend_threshold'))},
      publish_group: data.get('publish_group') || ''});
    location.hash = `#results/${item.id}`;
  })}, el('label', {class:'field', for:'rollout-name'}, el('span',{},'运行名称'),
      el('input', {id:'rollout-name', name:'name', type:'text', maxlength:120,
        placeholder:`分批测算 v${active.version}`})),
    el('div', {class:'form-grid'},
      el('label', {class:'field'}, el('span',{},'每段自然日数'),
        el('input', {name:'window_days', type:'number', min:1, max:28, step:1, value:7})),
      el('label', {class:'field'}, el('span',{},'试点前偏离阈值（分钟）'),
        el('input', {name:'pretrend_threshold', type:'number', min:0, max:1440, step:'any', value:2})),
      el('label', {class:'field'}, el('span',{},'发布组（留空自动独立发布）'),
        el('input', {name:'publish_group', type:'text', maxlength:64, placeholder:'同组重算会一次性切换'}))),
    runButton);

  // 更正影响面与一键重算
  let impactPanel = null;
  async function loadImpact(version) {
    if (!version.supersedes_id) {
      impactPanel?.replaceChildren(p('首版排期没有需要更正重算的历史运行。', 'subtle'));
      return;
    }
    const impact = await workspace.versionImpact(version.id);
    const recalc = button('按新版重算受影响批次', {class:'primary', 'data-testid':'recalculate'});
    recalc.addEventListener('click', handler(recalc, message, async () => {
      const out = await workspace.recalculate(version.id);
      message.textContent = `已排队 ${out.run_ids.length} 个重算运行，全部成功后页面与下载一次性切换；失败保留旧版。`;
      const finished = item => ['complete', 'failed'].includes(item.status);
      await poll(async () => {
        const runs = await Promise.all(out.run_ids.map(id => workspace.getRun(id)));
        return runs.every(finished) ? runs : null;
      }, Boolean, () => true);
      await refresh();
    }));
    impactPanel?.replaceChildren(
      p(`更正市场：${impact.affected_markets.join('、') || '无'}`, 'eyeline'),
      table([
        {label:'受影响运行', render: row => link(row.name, `#results/${row.id}`)},
        {key:'status', label:'状态'},
        {label:'当前发布', render: row => row.published ? '线上版本' : '未发布/已下线'},
      ], impact.impacted_runs, {caption:'含原来拿变更市场作对照的批次', empty:'没有受影响的历史运行'}),
      impact.impacted_runs.length ? recalc : p('该更正不影响任何历史运行，无需重算。', 'subtle'));
  }
  impactPanel = el('div', {});
  if (active) loadImpact(active);

  const versionTable = table([
    {key:'version', label:'版本'},
    {label:'状态', render: row => row.is_active ? pill('当前生效', 'complete') : pill('历史版本', 'returned')},
    {key:'name', label:'名称'},
    {label:'更正市场', render: row => row.affected_markets.length ? row.affected_markets.join('、') : '—'},
    {label:'时间', render: row => date(row.created_at)},
    {label:'作者', key:'author'},
    {label:'操作', render: row => row.id === active?.id ? '当前' : button('查看影响面', {class:'quiet',
      onClick: () => loadImpact(row)})},
  ], versions, {caption:'排期版本（旧版永久保留）', empty:'还没有排期版本。'});

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel('分批启用排期', p('按启用日划分批次，同批前后共用同一批对照；截至窗口末日已启用的市场不能作对照。', 'subtle'),
        session.canWrite ? versionForm : p('只读角色可查看排期版本与影响面。', 'empty')),
      active && panel('分批双重差分', runForm),
      panel('更正影响面与重算', impactPanel)),
    el('aside', {class:'sidebar-panel'},
      panel('版本记录', versionTable),
      panel('切换与发布规则', el('ul', {class:'method-list'}, [
        '排期更正生成新版本，旧版本不覆盖、可追溯。',
        '重算覆盖直接受影响批次，以及原来拿变更市场作对照的批次。',
        '同发布组的重算全部成功后，页面和下载一次性切换到新版。',
        '任一重算失败则保留旧版，线上结果不变。',
        '原统一启用日的历史运行照常可查、可下载。',
      ].map(text => el('li', {}, text))))));
}

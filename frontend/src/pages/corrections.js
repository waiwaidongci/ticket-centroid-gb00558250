import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {CORRECTION_KIND, CORRECTION_STATUS, STATUS} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function entryRows(spec, items = []) {
  const queueOptions = spec.fields.find(item => item.key === 'label').options;
  const body = el('tbody');
  const ticketOptions = spec.holdout || [];
  function paint() {
    body.replaceChildren(...items.map((entry, index) => el('tr', {},
      el('td', {}, el('select', {name:'ticket_id', required:true, onChange:event => {
        entry.ticket_id = event.target.value;
      }}, ticketOptions.map(ticket => el('option', {value:ticket.ticket_id,
        selected:ticket.ticket_id === entry.ticket_id}, `${ticket.ticket_id} · ${ticket.actual}`)))),
      el('td', {}, el('select', {name:'label', required:true, onChange:event => {
        entry.label = event.target.value;
      }}, queueOptions.map(option => el('option', {value:option, selected:option === entry.label}, option)))),
      el('td', {}, button('移除', {class:'quiet', onClick:() => { items.splice(index, 1); paint(); }})))));
  }
  paint();
  const add = button('添加一封', {class:'quiet', 'data-testid':'add-correction-row', onClick:() => {
    items.push({ticket_id:ticketOptions[0]?.ticket_id || '', label:queueOptions[0]});
    paint();
  }});
  return el('div', {}, el('div', {class:'table-wrap'}, el('table', {},
    el('thead', {}, el('tr', {}, el('th', {scope:'col'}, '留出来信（当前归口）'),
      el('th', {scope:'col'}, '更正后归口'), el('th', {scope:'col'}, '操作'))), body)), add);
}

function correctionForm(rootId, spec, holdout, refresh) {
  const message = feedback();
  const items = holdout.length ? [{ticket_id:holdout[0].ticket_id, label:holdout[0].actual}] : [];
  const submit = button('提交更正并重算', {class:'primary', type:'submit', 'data-testid':'correction-submit'});
  const formSpec = {...spec, holdout};
  return el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    const ticketIds = data.getAll('ticket_id');
    const labels = data.getAll('label');
    const entries = ticketIds.map((ticket_id, index) => ({ticket_id, label:labels[index]}));
    await workspace.submitCorrection(rootId, {
      review_no:data.get('review_no'), note:data.get('note') || '', entries,
    });
    await refresh();
  })}, field({key:'review_no', label:'复核号', type:'string', max_length:80}),
    entryRows(formSpec, items),
    el('label', {class:'field', for:'correction-note'}, el('span', {}, '更正说明（可选）'),
      el('textarea', {id:'correction-note', name:'note', maxlength:500, rows:2})),
    p('同封来信在本批中出现冲突归口会整批退回；提交后沿用原分流词表、阈值和基线重算，仅受影响来信改变计分。', 'subtle'),
    submit, message);
}

function versionTable(versions, currentRunId) {
  return table([
    {label:'版本', render:row => row.revision_no === 0 ? '原始交付版' : `第 ${row.revision_no} 版更正`},
    {label:'运行', render:row => link(row.name, `#results/${row.id}`)},
    {label:'状态', render:row => pill(STATUS[row.status], row.status)},
    {label:'当前生效', render:row => row.id === currentRunId ? pill('是', 'accepted') : '—'},
    {label:'完成时间', render:row => date(row.finished_at)},
  ], versions, {caption:'版本链（历史版本仍可查看与下载）', empty:'暂无重算版本。'});
}

function batchTable(batches, spec, rootId, refresh) {
  const message = feedback();
  return table([
    {key:'revision_no', label:'修订序号', render:row => `第 ${row.revision_no} 版`},
    {key:'review_no', label:'复核号'},
    {label:'类型', render:row => CORRECTION_KIND[row.kind]},
    {label:'内容', render:row => row.kind === 'withdrawal' ? '撤回上一批更正'
      : row.entries.map(item => `${item.ticket_id}→${item.label}`).join('；')},
    {label:'状态', render:row => pill(CORRECTION_STATUS[row.status] || row.status, row.status)},
    {label:'重算', render:row => row.recalc_run_id
      ? link(`运行 ${row.recalc_run_id}`, `#results/${row.recalc_run_id}`) : '—'},
    {label:'时间', render:row => date(row.created_at)},
    {label:'操作', render:row => {
      if (row.kind !== 'correction' || row.status !== 'active' || !session.canWrite) return '—';
      const control = button('撤回此更正', {class:'quiet', 'data-testid':`withdraw-${row.id}`});
      control.addEventListener('click', handler(control, message, async () => {
        await workspace.withdrawCorrection(row.id, {note:''});
        await refresh();
      }));
      return control;
    }},
  ], batches, {caption:'更正事件链（同封来信按修订序号取最新归口）', empty:'还没有归口更正。'});
}

export async function correctionsPage(route, refresh) {
  const {spec, runs} = workspace;
  const delivered = runs.filter(run => run.status === 'complete' && !run.is_recalc);
  const id = Number(route.id) || delivered[0]?.id;
  if (!id) return panel(spec.pages[5], empty('先完成并通过一次运行复核，再发起归口更正。', '#run', spec.pages[2]));
  const detail = await workspace.corrections(id);
  const source = await workspace.getDataset(runs.find(run => run.id === id)?.dataset_id || delivered[0]?.dataset_id);
  const holdout = (source?.rows || []).filter(row => row.split === 'test').map(row => ({
    ticket_id: row.ticket_id, actual: detail.overrides[row.ticket_id] || row.label,
  }));
  const picker = el('label', {class:'field run-picker', for:'correction-run-picker'}, el('span', {}, '选择已交付运行'),
    el('select', {id:'correction-run-picker', onChange:event => {
      location.hash = `#corrections/${event.target.value}`;
    }}, delivered.map(item => el('option', {value:item.id, selected:item.id === id}, item.name))));
  const currentPill = pill(`当前生效：${detail.current_revision_no === 0 ? '原始交付版' : `第 ${detail.current_revision_no} 版`}`,
    detail.current_revision_no === 0 ? 'pending' : 'accepted');
  const form = detail.correctable && session.canWrite
    ? panel('提交归口更正', correctionForm(detail.root_run_id, spec, holdout, refresh))
    : panel('归口更正', p(detail.correctable ? '当前角色为只读，可查看更正记录。' : '该运行尚未通过复核交付，暂不能更正。', 'subtle'));
  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel(spec.pages[5], picker, currentPill,
        p('更正只影响留出件的人工归口：训练词表、质心、阈值和多数类基线全部沿用原运行；重算成功后结果页面与下载一次性切换到同一完整版本，中断时保留旧版。', 'intro-copy'),
        link('查看当前生效结果', `#results/${detail.current_run_id}`, {class:'quiet'})),
      panel('更正批次', batchTable(detail.batches, spec, id, refresh)),
      panel('版本链', versionTable(detail.versions, detail.current_run_id))),
    el('aside', {class:'sidebar-panel'}, form,
      panel('处理规则', el('ul', {class:'method-list'}, [
        '更正可同时提交多封来信，同封来信按复核号对应的修订序号取最新归口。',
        '撤回指定更正后恢复上一条有效归口；撤回先到达也会触发一次重算。',
        '同封同号内容冲突时整批退回，不会产生重算或计分。',
        '重跑不重复计分，只重算受影响来信所属运行的计分。',
      ].map(text => el('li', {}, text))))));
}

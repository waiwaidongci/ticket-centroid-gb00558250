"""归口更正业务规则的自包含验证（不依赖数据库/Redis）。

运行：python -m scripts.test_corrections
"""

from src.constants.enums import Review, Status
from src.domain.analysis import analyze, _apply_overrides
from src.domain.demo import demo_rows
from src.domain.spec import QUEUES
from src.services import corrections as service
from src.services.corrections import effective_overrides, validate_entries

DATASET_ROWS = demo_rows()
HOLDOUT = {row['ticket_id']: row['label'] for row in DATASET_ROWS if row['split'] == 'test'}
PARAMS = {'max_terms': 0, 'min_similarity': 0.12}
ROOT_ID = 1


class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name, value):
        self[name] = value


class FakeDB:
    """内存会话：只实现 corrections 服务实际调用的接口。"""

    def __init__(self, runs, batches):
        self.runs = {run['id']: run for run in runs}
        self.batches = {batch['id']: batch for batch in batches}
        self._run_seq = max(self.runs, default=0)
        self._batch_seq = max(self.batches, default=0)

    def get(self, model, key, with_for_update=False):
        name = model.__name__
        if name == 'Run':
            return self.runs.get(key)
        if name == 'CorrectionBatch':
            return self.batches.get(key)
        if name == 'Dataset':
            return AttrDict(rows=DATASET_ROWS)
        raise KeyError(name)

    def scalar(self, query):
        clauses = " ".join(query.clauses)
        if 'review_no =' in clauses:
            token = clauses.split('review_no =')[1].split('AND')[0].strip().strip("'")
            return next((b.id for b in self.batches.values()
                         if b.run_id == ROOT_ID and b.review_no == token), None)
        nums = [b.revision_no for b in self.batches.values() if b.run_id == ROOT_ID]
        return max(nums) if nums else None

    def scalars(self, statement):
        items = [b for b in self.batches.values()
                 if b.run_id == ROOT_ID and b.status == 'active']
        items.sort(key=lambda b: (b.revision_no, b.id))

        class Result:
            def all(self_inner):
                return items

        return Result()

    def add(self, obj):
        if hasattr(obj, 'review_no'):
            self._batch_seq += 1
            obj.id = self._batch_seq
            self.batches[obj.id] = obj
        else:
            self._run_seq += 1
            obj.id = self._run_seq
            self.runs[obj.id] = obj

    def flush(self):
        pass


def base_result():
    return analyze(DATASET_ROWS, PARAMS)


def overrides_from(events):
    """模拟 effective_overrides 的事件重放。"""
    result = {}
    for batch in events:
        if batch['status'] != 'active' or batch['kind'] != 'correction':
            continue
        for entry in batch['entries']:
            result[entry['ticket_id']] = entry['label']
    return result


def test_override_only_changes_holdout():
    ticket = next(row for row in DATASET_ROWS if row['split'] == 'test')
    new_label = next(q for q in QUEUES if q != ticket['label'])
    adjusted, affected = _apply_overrides(DATASET_ROWS, {ticket['ticket_id']: new_label})
    assert affected == [ticket['ticket_id']]
    train_before = [r for r in DATASET_ROWS if r['split'] == 'train']
    train_after = [r for r in adjusted if r['split'] == 'train']
    assert train_before == train_after, '训练件必须保持冻结'
    updated = next(r for r in adjusted if r['ticket_id'] == ticket['ticket_id'])
    assert updated['label'] == new_label and updated['text'] == ticket['text']


def test_override_unknown_ticket_rejected():
    try:
        _apply_overrides(DATASET_ROWS, {'QQ-NOPE-99': '票务退改'})
    except ValueError as error:
        assert '留出批' in str(error)
    else:
        raise AssertionError('非留出件编号必须拒绝')


def test_same_label_no_affected():
    ticket = next(row for row in DATASET_ROWS if row['split'] == 'test')
    _, affected = _apply_overrides(DATASET_ROWS, {ticket['ticket_id']: ticket['label']})
    assert affected == [], '归口未变化时不计为受影响'


def test_validate_batch_conflict_whole_reject():
    ticket = next(iter(HOLDOUT))
    other = next(q for q in QUEUES if q != HOLDOUT[ticket])
    try:
        validate_entries([
            {'ticket_id': ticket, 'label': HOLDOUT[ticket]},
            {'ticket_id': ticket, 'label': other},
        ], HOLDOUT)
    except ValueError as error:
        assert '整批退回' in str(error)
    else:
        raise AssertionError('同封同号冲突必须整批退回')


def test_validate_duplicate_same_label_collapses():
    ticket = next(iter(HOLDOUT))
    normalized = validate_entries([
        {'ticket_id': ticket, 'label': HOLDOUT[ticket]},
        {'ticket_id': ticket, 'label': HOLDOUT[ticket]},
    ], HOLDOUT)
    assert len(normalized) == 1


def test_validate_unknown_ticket_and_label():
    try:
        validate_entries([{'ticket_id': 'QQ-TEST-01', 'label': '不存在队列'}], HOLDOUT)
    except ValueError:
        pass
    else:
        raise AssertionError('非法归口必须拒绝')
    try:
        validate_entries([{'ticket_id': 'QQ-GHOST-00', 'label': next(iter(QUEUES))}], HOLDOUT)
    except ValueError:
        pass
    else:
        raise AssertionError('非留出批编号必须拒绝')


def test_event_chain_latest_wins_and_withdrawal_restores():
    ticket = 'QQ-TEST-07'
    original = HOLDOUT[ticket]
    queue_b = next(q for q in QUEUES if q != original)
    events = [
        AttrDict(id=1, status='active', kind='correction',
                 entries=[{'ticket_id': ticket, 'label': queue_b}], revision_no=1),
        AttrDict(id=2, status='active', kind='correction',
                 entries=[{'ticket_id': ticket, 'label': original}], revision_no=2),
    ]
    assert overrides_from(events)[ticket] == original, '同封来信按修订序号取最新'
    events[1]['status'] = 'withdrawn'
    assert overrides_from(events)[ticket] == queue_b, '撤回后恢复上一条有效归口'
    events[0]['status'] = 'withdrawn'
    assert ticket not in overrides_from(events), '全部撤回后回到原始归口'


def test_withdrawal_first_then_correction():
    """撤回事件先到也算：事件链里只有撤回时覆盖表为空，重算等于原始口径。"""
    events = [AttrDict(id=1, status='active', kind='withdrawal', entries=[], revision_no=1)]
    assert overrides_from(events) == {}
    baseline = base_result()
    rerun = analyze(DATASET_ROWS, PARAMS, overrides={})
    assert rerun['metrics'][0]['value'] == baseline['metrics'][0]['value']


def test_recalc_keeps_baseline_and_only_affected_scoring_changes():
    baseline = base_result()
    target = 'QQ-TEST-07'  # 原始归口为失物招领，改到票务退改（训练多数队列）
    new_label = '票务退改'
    assert new_label != HOLDOUT[target]
    rerun = analyze(DATASET_ROWS, PARAMS, overrides={target: new_label})
    # 多数类基线的预测方向、训练词表和质心必须完全不变
    assert rerun['note'] == baseline['note']
    assert '全部投向票务退改' in rerun['note']
    detail = rerun['tables'][0]['rows']
    row = next(item for item in detail if item['ticket_id'] == target)
    assert row['actual'] == new_label
    # 预测只依赖文本与冻结的训练产物，逐件去向和相似度必须与原版完全一致
    for new_row in detail:
        old_row = next(item for item in baseline['tables'][0]['rows']
                       if item['ticket_id'] == new_row['ticket_id'])
        for key in ('predicted', 'candidate', 'similarity', 'matched_terms', 'reason', 'baseline'):
            assert new_row[key] == old_row[key], f'{target} 的预测口径必须冻结，字段 {key} 不应变化'
    # 基线宏 F1 随真值变化：支持数 3→4，基线 F1 = (2*4/(4+7))/3
    expected_baseline_macro = round((2 * 4 / (4 + 7)) / 3, 6)
    assert rerun['metrics'][1]['value'] == expected_baseline_macro
    assert rerun['metrics'][1]['value'] != baseline['metrics'][1]['value']
    assert rerun['correction']['affected'] == [target]
    assert rerun['correction']['override_count'] == 1


def test_full_service_flow_with_fake_db():
    root = AttrDict(id=ROOT_ID, dataset_id=9, parent_run_id=None, revision_no=0,
                    current_run_id=ROOT_ID, status=Status.COMPLETE, review=Review.ACCEPTED,
                    params=dict(PARAMS), author='admin', name='样本运行')
    db = FakeDB([root], [])

    target = 'QQ-TEST-06'
    queue_b = next(q for q in QUEUES if q != HOLDOUT[target])
    batch1, job1 = service.submit_correction(
        db, ROOT_ID, 'REV-001', [{'ticket_id': target, 'label': queue_b}], None, 'admin')
    assert batch1.revision_no == 1 and job1.parent_run_id == ROOT_ID
    assert job1.params['_label_overrides'] == {target: queue_b}
    assert job1.current_run_id == ROOT_ID, '重算未完成前指针仍指向旧版'

    # 同复核号不能重复
    try:
        service.submit_correction(db, ROOT_ID, 'REV-001',
                                  [{'ticket_id': target, 'label': queue_b}], None, 'admin')
    except ValueError as error:
        assert '复核号' in str(error)
    else:
        raise AssertionError('复核号必须唯一')

    # 第二批更正：同封来信给新归口，事件链重放取最新
    queue_c = next(q for q in QUEUES if q not in {HOLDOUT[target], queue_b})
    batch2, job2 = service.submit_correction(
        db, ROOT_ID, 'REV-002', [{'ticket_id': target, 'label': queue_c}], None, 'admin')
    assert job2.params['_label_overrides'] == {target: queue_c}

    # 撤回第一批：第一批作废，剩余有效事件只剩第二批
    withdrawal, job3 = service.withdraw_correction(db, batch1.id, None, 'admin')
    assert db.batches[batch1.id].status == 'withdrawn'
    assert withdrawal.kind == 'withdrawal'
    assert job3.params['_label_overrides'] == {target: queue_c}

    # 再撤回第二批：覆盖表清空，恢复原始归口
    service.withdraw_correction(db, batch2.id, None, 'admin')
    assert effective_overrides(db, ROOT_ID) == {}

    # 已撤回批次不能重复撤回
    try:
        service.withdraw_correction(db, batch1.id, None, 'admin')
    except ValueError as error:
        assert '已撤回' in str(error)
    else:
        raise AssertionError('重复撤回必须拒绝')


def test_unreviewed_run_not_correctable():
    root = AttrDict(id=2, dataset_id=9, parent_run_id=None, revision_no=0,
                    current_run_id=2, status=Status.COMPLETE, review=Review.PENDING,
                    params=dict(PARAMS), author='analyst', name='未复核运行')
    db = FakeDB([root], [])
    try:
        service.submit_correction(db, 2, 'REV-X',
                                  [{'ticket_id': next(iter(HOLDOUT)), 'label': next(iter(QUEUES))}],
                                  None, 'analyst')
    except ValueError as error:
        assert '通过复核并完成交付' in str(error)
    else:
        raise AssertionError('未通过复核的运行不能更正')


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
        print(f'PASS {test.__name__}')
    print(f'\n{len(tests)} 项归口更正规则全部通过。')


if __name__ == '__main__':
    main()

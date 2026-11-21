"""归口更正的事件链、撤回与重算调度。

更正以「复核号 + 修订序号」登记在运行链上。有效归口通过重放所有未撤回的
更正事件得到：同封来信按修订序号取最新一条；撤回事件整体作废对应批次，
使归口恢复到上一条有效更正（没有则回到原始归口）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import Review, Status
from src.domain.spec import QUEUES
from src.models.jobs import CorrectionBatch, Run


def root_run(db: Session, run_id: int, lock: bool = False):
    """沿 parent_run_id 找到运行链的原始运行。"""
    run = db.get(Run, run_id, with_for_update=lock)
    if run is None:
        return None
    seen = set()
    while run.parent_run_id is not None and run.id not in seen:
        seen.add(run.id)
        run = db.get(Run, run.parent_run_id, with_for_update=lock)
    return run


def active_events(db: Session, root_id: int):
    """按修订序号返回运行链上仍有效（未撤回）的更正事件。"""
    batches = db.scalars(
        select(CorrectionBatch)
        .where(CorrectionBatch.run_id == root_id, CorrectionBatch.status == 'active')
        .order_by(CorrectionBatch.revision_no, CorrectionBatch.id)
    ).all()
    return batches


def effective_overrides(db: Session, root_id: int):
    """重放事件链，得到当前各留出件的有效归口覆盖。"""
    overrides = {}
    for batch in active_events(db, root_id):
        if batch.kind == 'correction':
            for entry in batch.entries:
                overrides[entry['ticket_id']] = entry['label']
        # 撤回事件不携带归口，跳过即可：被撤回批次已不在有效事件内。
    return overrides


def holdout_labels(dataset) -> dict:
    return {row['ticket_id']: row['label'] for row in dataset.rows if row['split'] == 'test'}


def validate_entries(entries, labels):
    """整批校验：编号必须属于留出批，归口必须合法；返回规范化条目。"""
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ValueError('更正内容为空，请至少填写一封留出件的归口。')
    normalized = []
    seen = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f'第 {index} 条更正内容格式有误。')
        ticket_id = entry.get('ticket_id')
        label = entry.get('label')
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError(f'第 {index} 条更正缺少来信编号。')
        ticket_id = ticket_id.strip()
        if ticket_id not in labels:
            raise ValueError(f'来信编号「{ticket_id}」不属于该运行的留出批，不能更正归口。')
        if label not in QUEUES:
            raise ValueError(f'来信「{ticket_id}」的归口须为：{"、".join(QUEUES)}。')
        # 同封同号冲突：同一批次内同一来信出现不一致归口，整批退回。
        if ticket_id in seen:
            previous = next(item['label'] for item in normalized if item['ticket_id'] == ticket_id)
            if previous != label:
                raise ValueError(
                    f'同一封来信「{ticket_id}」在本批中出现冲突归口（{previous} / {label}），整批退回，请核对后重新提交。')
            continue
        seen.add(ticket_id)
        normalized.append({'ticket_id': ticket_id, 'label': label})
    return normalized


def ensure_correctable(db: Session, root: Run):
    """只有已通过复核并完成交付的运行才能发起更正。"""
    if root.status != Status.COMPLETE or root.review != Review.ACCEPTED:
        raise ValueError('只有已通过复核并完成交付的运行才能发起归口更正。')
    if root.parent_run_id is not None:
        raise ValueError('请基于原始已交付运行发起更正，重算版本不能再次挂接更正。')


def next_revision(db: Session, root_id: int) -> int:
    latest = db.scalar(
        select(CorrectionBatch.revision_no)
        .where(CorrectionBatch.run_id == root_id)
        .order_by(CorrectionBatch.revision_no.desc()).limit(1))
    return (latest or 0) + 1


def schedule_recalc(db: Session, root: Run, revision_no: int) -> Run:
    """沿用原批次与参数创建重算运行，生效归口取事件链重放结果。"""
    overrides = effective_overrides(db, root.id)
    job = Run(
        dataset_id=root.dataset_id, name=f'{root.name} · 归口更正第 {revision_no} 版',
        params=dict(root.params), author=root.author,
        parent_run_id=root.id, revision_no=revision_no,
        current_run_id=root.current_run_id or root.id,
    )
    job.params['_label_overrides'] = overrides
    db.add(job)
    db.flush()
    return job


def submit_correction(db: Session, root_id: int, review_no: str, entries, note, actor) -> CorrectionBatch:
    root = root_run(db, root_id, lock=True)
    if root is None:
        raise ValueError('找不到该运行记录。')
    ensure_correctable(db, root)
    review_no = review_no.strip()
    if not review_no or len(review_no) > 80:
        raise ValueError('复核号必填且不超过 80 个字符。')
    if db.scalar(select(CorrectionBatch.id).where(
            CorrectionBatch.run_id == root.id, CorrectionBatch.review_no == review_no)):
        raise ValueError(f'复核号「{review_no}」已登记，请使用新的复核号提交。')
    from src.models.source import Dataset
    dataset = db.get(Dataset, root.dataset_id)
    labels = holdout_labels(dataset)
    normalized = validate_entries(entries, labels)
    revision_no = next_revision(db, root.id)
    batch = CorrectionBatch(
        run_id=root.id, review_no=review_no, revision_no=revision_no, kind='correction',
        entries=normalized, note=note, author=actor)
    db.add(batch)
    db.flush()
    job = schedule_recalc(db, root, revision_no)
    batch.recalc_run_id = job.id
    return batch, job


def withdraw_correction(db: Session, batch_id: int, note, actor) -> CorrectionBatch:
    batch = db.get(CorrectionBatch, batch_id, with_for_update=True)
    if batch is None:
        raise ValueError('找不到该更正批次。')
    root = root_run(db, batch.run_id, lock=True)
    if batch.status != 'active':
        raise ValueError('该更正批次已撤回，无需重复操作。')
    # 撤回先到也算：先把目标批次作废，再重放剩余事件生成重算。
    batch.status = 'withdrawn'
    batch.withdrawn_by = actor
    from src.utils.time import now
    batch.withdrawn_at = now()
    revision_no = next_revision(db, root.id)
    withdrawal = CorrectionBatch(
        run_id=root.id, review_no=f'{batch.review_no}#撤回', revision_no=revision_no,
        kind='withdrawal', entries=[], note=note, author=actor, status='active')
    db.add(withdrawal)
    db.flush()
    job = schedule_recalc(db, root, revision_no)
    withdrawal.recalc_run_id = job.id
    return withdrawal, job

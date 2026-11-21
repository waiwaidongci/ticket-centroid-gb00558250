from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import CorrectionBatch, Run
from src.schemas.requests import CorrectionRequest, WithdrawalRequest
from src.services import corrections as service
from src.services.audit import record
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/corrections')


def _root_or_404(db: Session, run_id: int):
    root = service.root_run(db, run_id)
    if root is None:
        raise HTTPException(404, '找不到该运行记录')
    return root


@router.get('/runs/{run_id}')
def for_run(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    root = _root_or_404(db, run_id)
    batches = db.scalars(
        select(CorrectionBatch).where(CorrectionBatch.run_id == root.id)
        .order_by(CorrectionBatch.revision_no.desc(), CorrectionBatch.id.desc())).all()
    current = db.get(Run, root.current_run_id or root.id)
    return {
        'root_run_id': root.id,
        'current_run_id': root.current_run_id or root.id,
        'current_revision_no': current.revision_no if current else 0,
        'correctable': root.review == 'accepted' and root.status == 'complete',
        'overrides': service.effective_overrides(db, root.id),
        'batches': [views.correction(item) for item in batches],
        'versions': [views.run(item) for item in db.scalars(
            select(Run).where((Run.id == root.id) | (Run.parent_run_id == root.id))
            .order_by(Run.revision_no, Run.id))],
    }


@router.post('/runs/{run_id}', status_code=201)
def submit(run_id: int, body: CorrectionRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        batch, job = service.submit_correction(
            db, run_id, body.review_no, [entry.model_dump() for entry in body.entries],
            body.note or None, actor.username)
        record(db, actor.username, 'correction.submitted', batch.run_id,
               batch_id=batch.id, review_no=batch.review_no, revision_no=batch.revision_no,
               entries=batch.entries, recalc_run_id=job.id)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    wake()
    return views.correction(batch)


@router.post('/{batch_id}/withdraw', status_code=201)
def withdraw(batch_id: int, body: WithdrawalRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        withdrawal, job = service.withdraw_correction(db, batch_id, body.note or None, actor.username)
        record(db, actor.username, 'correction.withdrawn', withdrawal.run_id,
               batch_id=batch_id, revision_no=withdrawal.revision_no, recalc_run_id=job.id)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    wake()
    return views.correction(withdrawal)

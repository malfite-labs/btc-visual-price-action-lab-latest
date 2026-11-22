"""E-only candidate review adapter; uses existing comparison and checkpoint rules."""
import hashlib
import sqlite3
from datetime import datetime
from backend.evolution import ReviewCoordinator


class EShadowReview:
    def __init__(self, shadow_store):
        self.store=shadow_store
        self.path=shadow_store.path
        self.review=ReviewCoordinator(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS e_candidate_versions(
                batch INTEGER PRIMARY KEY,candidate_id TEXT UNIQUE NOT NULL,md TEXT NOT NULL,
                md_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)''')

    def propose(self, *, batch: int, candidate_md: str, now: datetime):
        if batch < 40 or batch % 40 or self.store.review_progress()['closed_trades'] < batch:
            return {'status':'INSUFFICIENT_CLOSED_TRADES'}
        if not isinstance(candidate_md,str) or not candidate_md.strip() or now.tzinfo is None:
            return {'status':'INVALID_CANDIDATE'}
        digest=hashlib.sha256(candidate_md.encode('utf8')).hexdigest()
        candidate_id=f'E-CANDIDATE-{batch}-{digest[:12]}'
        with sqlite3.connect(self.path) as db:
            old=db.execute('SELECT candidate_id,md_sha256 FROM e_candidate_versions WHERE batch=?',(batch,)).fetchone()
            if old:
                return {'status':'BATCH_ALREADY_RECORDED','candidate_id':old[0]}
        started=self.review.start_candidate('A',candidate_id,now=now)
        if not started.get('accepted'):
            return {'status':started.get('reason','CANDIDATE_BLOCKED')}
        try:
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT INTO e_candidate_versions VALUES(?,?,?,?,?)',(batch,candidate_id,candidate_md,digest,now.isoformat()))
        except sqlite3.Error:
            self.review.mark_candidate_finished('A','REJECTED')
            raise
        return {'status':'CANDIDATE_CREATED','candidate_id':candidate_id,'sha256':digest}

    def evaluate(self, *, now, checkpoint_day, **kwargs):
        return self.review.evaluate_candidate_checkpoint('A',checkpoint_day,now=now,
            old_position_open=self.store.account('E')['state']!='FLAT',**kwargs)

    def activate(self, *, approved, old_position_open, at_new_cycle):
        if approved is not True:
            return {'status':'APPROVAL_REQUIRED'}
        # Never trust the caller to claim an existing shadow position is flat.
        old_position_open=old_position_open or self.store.account('E')['state']!='FLAT'
        return self.review.activate_after_flat('A',old_position_open=old_position_open,at_new_cycle=at_new_cycle)

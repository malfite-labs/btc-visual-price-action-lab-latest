"""E-only adapter around the frozen paper ledger, stored in an exclusive database.

The legacy ledger accepts only A/B/C. Physical A is a private implementation slot
in this E-only database; every public row is translated back to E.
"""
import sqlite3
from pathlib import Path
from backend.ledger import Ledger


class EPaperStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.exists():
            try:
                with sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True) as db:
                    marker=db.execute("SELECT value FROM e_store_identity WHERE key='owner'").fetchone()
                if marker != ('E',):
                    raise ValueError('E_STORE_IDENTITY_INVALID')
            except sqlite3.Error as exc:
                raise ValueError('E_STORE_IDENTITY_INVALID') from exc
        else:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.execute('CREATE TABLE e_store_identity(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute("INSERT INTO e_store_identity VALUES('owner','E')")
        self._ledger=Ledger(self.path)

    @staticmethod
    def _check(agent):
        if agent != 'E':
            raise ValueError('E_ONLY')

    def account(self, agent):
        self._check(agent)
        return {**self._ledger.account('A'), 'agent':'E'}

    def account_metrics(self, agent, quote, cost_model):
        self._check(agent)
        return self._ledger.account_metrics('A', quote, cost_model)

    def open_position(self, *, agent, **kwargs):
        self._check(agent)
        return self._ledger.open_position(agent='A', **kwargs)

    def close_position(self, *, agent, **kwargs):
        self._check(agent)
        return self._ledger.close_position(agent='A', **kwargs)

    def request_stop_exit(self, *, agent, **kwargs):
        self._check(agent)
        return self._ledger.request_stop_exit(agent='A', **kwargs)

    def fills(self):
        return [{**row, 'agent':'E'} for row in self._ledger.fills('A')]

    def review_progress(self):
        # Derived from durable fills: repeated calls and restarts cannot double count.
        with sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True) as db:
            count=db.execute("SELECT count(DISTINCT trade_id) FROM fills WHERE agent='A' AND position_effect='CLOSE'").fetchone()[0]
        return {'closed_trades':count, 'due_batches':list(range(40,count+1,40)),
                'next_review_at':(count//40+1)*40, 'candidate_status':'NOT_IMPLEMENTED'}


class _ReadOnlyLedgerView:
    """Use the frozen valuation method with a strictly read-only connection."""
    account = Ledger.account
    account_metrics = Ledger.account_metrics

    def __init__(self, path):
        self.path = Path(path)

    def _session(self):
        from contextlib import contextmanager
        @contextmanager
        def session():
            db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
            db.row_factory=sqlite3.Row
            try:
                yield db
            finally:
                db.close()
        return session()


def read_e_store(path, *, quote=None):
    """Read an existing E-only ledger without creating or modifying any file."""
    import json
    from decimal import Decimal
    path=Path(path).resolve()
    if not path.exists():
        return {'status':'NOT_INITIALIZED'}
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            marker=db.execute("SELECT value FROM e_store_identity WHERE key='owner'").fetchone()
            if not marker or marker[0]!='E':
                return {'status':'INVALID_STORE'}
            row=db.execute("SELECT * FROM accounts WHERE agent='A'").fetchone()
            if row is None:
                return {'status':'INVALID_STORE'}
            account={**dict(row),'agent':'E'}
            fills=[{**dict(r),'agent':'E'} for r in db.execute("SELECT * FROM fills WHERE agent='A' ORDER BY occurred_at,fill_id")]
            events=[json.loads(r[0]) for r in db.execute("SELECT payload FROM audit_events WHERE agent='A' AND kind='POSITION_CLOSED'")]
            has_versions=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='e_candidate_versions'").fetchone()
            candidate=None
            if has_versions:
                candidate_row=db.execute("SELECT v.batch,v.candidate_id,v.md_sha256,c.status,c.started_at FROM e_candidate_versions v JOIN evo_candidates c ON c.candidate_id=v.candidate_id WHERE c.agent='A' ORDER BY v.batch DESC LIMIT 1").fetchone()
                candidate=dict(candidate_row) if candidate_row else None
        from backend.domain import CostModel
        metrics=_ReadOnlyLedgerView(path).account_metrics('A', quote, CostModel())
        pnl=[Decimal(str(e['net_pnl'])) for e in events]
        wins=sum(v>0 for v in pnl)
        return {'status':'READY','account':account,'fills':fills,'closed_trades':len(pnl),
                'realized_pnl':str(sum(pnl,Decimal(0))), 'win_rate':wins/len(pnl) if pnl else None,
                'equity':metrics.get('equity'),'unrealized_pnl':metrics.get('unrealized_pnl'),
                'valuation_status':metrics.get('valuation_status'),'candidate':candidate,
                'next_review_at':(len(pnl)//40+1)*40}
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        return {'status':'UNAVAILABLE'}

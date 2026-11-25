"""Resolve desktop strategy identities from the running V3 registry."""
import json


class StrategyVersionResolver:
    def __init__(self, fetch, pool_ids):
        self.fetch = fetch
        self.pool_ids = pool_ids

    def resolve(self):
        errors = {}

        def read(path):
            try:
                return self.fetch('/api/v1/' + path)
            except (OSError, ValueError) as exc:
                errors[path] = f'{path} 无法读取（{type(exc).__name__}）'
                return {}

        result = {}
        runtime = read('system/status').get('active_rule_versions', {})
        registry = read('strategies').get('items', [])
        pool = read('strategy-pool').get('items', [])
        for sid in 'ABC':
            item = runtime.get(sid, {})
            result[sid] = item if item.get('version') and item.get('sha256') else {
                'error': errors.get('system/status', '正式活动版本或规则哈希缺失')}
        d = read('d/status')
        g = read('g-mirror/status')
        e = [row for row in registry if row.get('agent') == 'E']
        for sid, item, field, source in (
            ('D', d, 'rule_version', 'd/status'),
            ('E', e[0] if len(e) == 1 else {}, 'driving_version', 'strategies'),
            ('G', g, 'version', 'g-mirror/status'),
        ):
            version = item.get(field)
            result[sid] = (dict(version=version, status=item.get('status'), source=source)
                           if version else {'error': errors.get(source, '活动执行标识缺失或不唯一')})
        for sid, strategy_id in self.pool_ids.items():
            matches = [row for row in pool if row.get('id') == strategy_id and not row.get('archived_at')]
            if len(matches) != 1 or not matches[0].get('active_version'):
                result[sid] = {'error': errors.get('strategy-pool', '活动注册记录缺失或不唯一')}
                continue
            row = matches[0]
            result[sid] = dict(version=row['active_version'], status=row.get('status'), source=strategy_id)
        return result

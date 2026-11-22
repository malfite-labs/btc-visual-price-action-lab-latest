"""Deterministic Chinese rendering for validated candle predicates."""
from scripts.rule_evolution_v2 import validate
NAMES={'open':'开盘价','high':'最高价','low':'最低价','close':'收盘价','body_ratio':'实体占全K线振幅比例','upper_wick_ratio':'上影线占全K线振幅比例','lower_wick_ratio':'下影线占全K线振幅比例','trend_pct':'区间收盘趋势变化百分比','range_pct':'区间最高最低振幅占收盘价百分比'}
OPS={'>':'大于','<':'小于','>=':'大于或等于','<=':'小于或等于'}
def feature(x):
 if not isinstance(x,dict):return str(x)
 k=x['kind'];off=x.get('offset',0);period=x.get('period',2)
 if k in ('rolling_high','rolling_low'):
  word='最高价' if k=='rolling_high' else '最低价'
  return (f'此前{period}根已收盘K线的{word}' if off==1 else f'从信号K线向前偏移{off}根为终点的{period}根已收盘K线的{word}')
 context='信号K线' if off==0 else f'信号K线之前第{off}根已收盘K线'
 if k in ('trend_pct','range_pct'):return f'以{context}为终点的{period}根K线{NAMES[k]}'
 return context+'的'+NAMES[k]
def describe_rule(rule):
 validate(rule);lines=[]
 for side,title in [('long','做多'),('short','做空')]:
  lines.append(title+'必须同时满足：')
  for c in rule[side]:lines.append('- '+feature(c['left'])+OPS[c['op']]+feature(c['right'])+'。')
 lines.append(f"信号收盘后下一根K线开盘模拟入场；止损取包含信号K线的最近{rule['stop_bars']}根结构极值外0.01美元；目标为入场风险距离{rule['target_r']}倍；最长持仓{rule['max_hold']}根K线，适用原有风控和成本。")
 lines.append('不赋予未经谓词证明的形态名称；满足两侧或无侧时不发出单向信号。')
 return '\n'.join(lines)
def v2_a_rule():
 return {'long':[{'left':{'kind':'close','offset':0},'op':'>','right':{'kind':'rolling_high','period':20,'offset':1}}], 'short':[{'left':{'kind':'close','offset':0},'op':'<','right':{'kind':'rolling_low','period':20,'offset':1}}], 'stop_bars':5,'target_r':1.5,'max_hold':12}

def claim_warnings(text,rule):
 """Fail closed on named patterns: human prose cannot establish predicate semantics."""
 validate(rule);s=text.casefold();warnings=[]
 if any(x in s for x in ('retest','re-test','回踩原突破','回测突破')):warnings.append('RETEST_REFERENCE_UNPROVEN')
 if any(x in s for x in ('second fail','second test','double test','two failed','两次失败','第二次失败')):warnings.append('DOUBLE_TEST_UNPROVEN')
 return warnings

"""Point capacity planning; never creates or changes platform resources."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from itertools import product
import workspace as s

STORAGE_BASE_MWH = Decimal(50)
STORAGE_BASE_COUNTS = {'PCS':94, 'BMS':78, 'GROUPBMS':78, 'CELL':78, 'CL':1, 'BOX':7}
STORAGE_POINTS_PER_MWH = 2600

def storage_capacity(rows, capacity_mwh, limits=None):
    """Find the closest point total near the 50 MWh reference device mix."""
    try:
        mwh = Decimal(str(capacity_mwh).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise s.Problem('容量须为大于 0 的 MWh 数值')
    if not mwh.is_finite() or mwh <= 0:
        raise s.Problem('容量须为大于 0 的 MWh 数值')
    if mwh*STORAGE_POINTS_PER_MWH>20000000:
        raise s.Problem('换算结果超过2000万目标点数上限，请降低容量')
    if not isinstance(rows,list) or len(rows)!=len(s.KINDS) or any(not isinstance(r,dict) for r in rows) or {r.get('type') for r in rows}!=set(s.KINDS):
        raise s.Problem('设备类型不完整')
    index = {r['type']:r for r in rows}
    points = {kind:index[kind].get('points') for kind in s.KINDS}
    if any(type(value) is not int or not 1<=value<=20000000 for value in points.values()):
        raise s.Problem('请先填写六类设备的单台点数')
    target = max(1,int((mwh*STORAGE_POINTS_PER_MWH).quantize(Decimal(1),rounding=ROUND_HALF_UP)))
    raw = {kind:Decimal(STORAGE_BASE_COUNTS[kind])*mwh/STORAGE_BASE_MWH for kind in ('PCS','BMS','BOX')}
    group_points=points['BMS']+points['GROUPBMS']+points['CELL']
    weighted = raw['PCS']*points['PCS']+raw['BMS']*group_points+raw['BOX']*points['BOX']
    factor = max(Decimal(0),Decimal(target-points['CL'])/weighted)
    reference = {kind:max(1,int((raw[kind]*factor).quantize(Decimal(1),rounding=ROUND_HALF_UP)))
                 for kind in ('PCS','BMS','BOX')}
    candidates = [sorted({max(1,reference[kind]+delta) for delta in range(-2,3)})
                  for kind in ('PCS','BMS','BOX')]
    best = None
    for pcs,bms,box in product(*candidates):
        totals = {'PCS':pcs,'BMS':bms,'GROUPBMS':bms,'CELL':bms,'CL':1,'BOX':box}
        if limits is not None and any(totals[kind]>limits.get(kind,0) for kind in s.KINDS):
            continue
        devices = sum(totals.values())
        actual = sum(totals[kind]*points[kind] for kind in s.KINDS)
        if devices>100000 or actual>20000000:
            continue
        distance = abs(pcs-reference['PCS'])+abs(bms-reference['BMS'])+abs(box-reference['BOX'])
        key=(abs(actual-target),distance,actual<target)
        if best is None or key<best[0]: best=(key,totals,devices,actual)
    if best is None:
        if limits is not None:
            raise s.Problem('已绑定设备池数量不足，无法按该容量换算；请降低容量或扩充设备池')
        raise s.Problem('换算结果超过10万设备或2000万点上限，请降低容量')
    _,totals,devices,actual=best
    return {'capacityMwh':str(mwh), 'target':target, 'actual':actual,
            'difference':actual-target, 'devices':devices, 'totals':totals,
            'unitCount':max(1,int((Decimal(25)*mwh/STORAGE_BASE_MWH).quantize(Decimal(1),rounding=ROUND_HALF_UP))),
            'pointRate':STORAGE_POINTS_PER_MWH,
            'note':'按 50MWh 基准比例和输入的单台点数换算，在基准附近的整数数量中选择最接近目标点数的组合；BMS 三类数量一致，CL 固定 1 台。'}

def target_points(value):
    match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([wW万]?)\s*(?:点)?\s*',str(value))
    if not match: raise s.Problem('目标点数支持 100000、10万、10w，例如 10万点')
    n=Decimal(match[1])*(10000 if match[2] else 1)
    if n!=int(n) or not 1<=n<=20000000: raise s.Problem('目标必须为1至2000万之间的整数点数')
    return int(n)

def calculate(profile, rows, target, mode):
    target=target_points(target)
    kinds=s.kinds_for(profile)
    if not isinstance(rows,list) or len(rows)!=len(kinds) or {r.get('type') for r in rows}!=set(kinds):
        raise s.Problem('设备类型不完整')
    index={r['type']:r for r in rows}
    groups=[('BMS',['BMS','GROUPBMS','CELL'])] if profile=='storage' else []
    groups += [(k,[k]) for k in kinds if profile!='storage' or k not in ('BMS','GROUPBMS','CELL')]
    if mode!='ratio' and mode not in [k for k,_ in groups]: raise s.Problem('请选择有效的换算方式')
    no_ratio=(mode=='ratio' and profile=='storage' and all(index[k].get('total')==0 for k in kinds))
    units=[]
    for name,members in groups:
        counts=[index[k].get('total') for k in members]
        if any(type(n) is not int or not 0<=n<=100000 for n in counts): raise s.Problem('设备数量必须是0至10万的整数')
        if mode=='ratio' and len(set(counts))!=1: raise s.Problem('按比例换算时 BMS、GROUPBMS、CELL 数量须一致')
        weight=(1 if no_ratio else counts[0]) if mode=='ratio' else int(mode==name)
        if not weight: continue
        points=[index[k].get('points') for k in members]
        if any(type(n) is not int or not 1<=n<=20000000 for n in points): raise s.Problem('请先填写参与换算类型的单设备点数')
        units.append({'members':members,'cost':sum(points),'weight':weight})
    if not units: raise s.Problem('按比例换算需至少一种设备数量大于0，或请选择单一设备类型')
    basis=sum(u['weight']*u['cost'] for u in units)
    ideals=[Fraction(target*u['weight'],basis) for u in units]
    amounts=[int(x) for x in ideals]
    actual=sum(n*u['cost'] for n,u in zip(amounts,units))
    while actual<target:
        i=max(range(len(units)),key=lambda i:ideals[i]-amounts[i])
        amounts[i]+=1;actual+=units[i]['cost']
    totals={k:0 for k in kinds}
    for u,n in zip(units,amounts):
        for k in u['members']:totals[k]=n
    if sum(totals.values())>100000 or actual>20000000: raise s.Problem('取整结果超过10万设备或2000万点上限，请降低目标')
    return {'target':target,'actual':actual,'difference':actual-target,'devices':sum(totals.values()),'totals':totals,
            'note':('当前数量全为0，先按 PCS、BMS组合、CL、BOX 各一组的初始比例分配；整数取整后不低于目标。' if no_ratio else '按当前数量比例近似分配，整数取整后保证不低于目标；不保证误差最小。') if mode=='ratio' else '按整台设备向上取整；其他类型数量归零。BMS 按三类设备一组计算。'}

"""Local CSV drafts matching the supplied 资产设备 examples; no platform writes."""
import csv
import io
import json
import zipfile


DEVICE = 'name,type,id,user,password,businessType,stationId'
HEADERS = {
    'Company': 'name,type,allInstallCapcity,allInstallPower,latitude,longitude',
    'powerStation': 'name,type,address,bmsCount,haveBms,haveAir,chargingCharges,energyCost,installCapacity,latitude,longitude,operationDate,paperId,pictureAddr,priceTimeseries,region,subsidyProceeds,systemType,tactics,tacticsDesc,attenuationArr,deviceCompany,voltageLevel,meterNum,customerNum,yiEarn,owerName,owerCompany,edgeNumber,owerPhone,isSync,loadAmmeter,autoMaxDemand,demandSet1,isAutoDemand,useStationCapCalc,needEdgeSync ,url,username,password,token',
    'energyStationUnit': 'name,type,businessType,stationId',
    'PCS': DEVICE+',onoff', 'BMS': DEVICE,
    'GROUPBMS': DEVICE+',batteryCap',
    'CELL': DEVICE+',cellChinese,temperatureNum,temperatureYc,voltageNum,voltageYc',
    'BOX': DEVICE, 'CL': 'name,type,id,name,pawd',
}
RELATION = 'fromType,fromName,toType,toName,type,typeGroup,additionalInfo'


def build_package(project, credentials=None):
    """Return a draft ZIP using name-based fallback credentials and blank site attributes."""
    credentials = credentials or (lambda binding: {})
    files, groups, pending = {}, {}, []

    def write(name, header, rows):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(header.split(','))
        writer.writerows(rows)
        files[name] = output.getvalue().encode('utf-8-sig')

    def dict_rows(header, rows):
        return [[r.get(k, '') for k in header.split(',')] for r in rows]

    cloud = project['cloud']
    assets = csv.reader(io.StringIO(cloud.get('asset_template', '')))
    for fields in assets:
        if not fields or not fields[0].strip() or fields[0].lstrip().startswith('#'):
            continue
        if len(fields) != 3 or fields[2].strip() not in ('Company', 'powerStation', 'energyStationUnit'):
            raise ValueError('资产模板需使用名称、数量、类型，类型为 Company / powerStation / energyStationUnit')
        label, count, kind = [v.strip() for v in fields]
        count = int(count)
        if not 1 <= count <= 10000:
            raise ValueError('资产数量须在 1 至 10000 之间')
        groups.setdefault(kind, []).extend(cloud.get('asset_name_prefix', '')+label+str(i) for i in range(1, count+1))
    for kind, names in groups.items():
        rows = [{'name':n, 'type':kind, 'businessType':'storageunit' if kind=='energyStationUnit' else ''} for n in names]
        suffix = {'Company':'company','powerStation':'station','energyStationUnit':'unit'}[kind]
        write(f'{len(rows)}_asset-{suffix}.csv', HEADERS[kind], dict_rows(HEADERS[kind], rows))

    bindings = {b['collectAlias']:b for b in project.get('bindings', [])}
    for spec in project['types']:
        kind, count = spec['type'], spec['total']
        if not count:
            continue
        rows, names = [], []
        for i in range(1, count+1):
            alias = kind.lower()+str(i)
            binding = bindings.get(alias)
            name = binding['platformName'] if binding else cloud['device_name_prefix']+alias
            cred = credentials(binding) if binding else {}
            if not cred or binding.get('credentialStatus')=='template_unverified':
                pending.append(name)
            if not cred: cred = {'client_id':name,'user_name':name,'pass_word':name}
            names.append(name)
            row = {'name':name, 'type':{'GROUPBMS':'bms','BOX':'otherDev','CL':'strategy'}.get(kind, kind.lower()),
                   'businessType':kind.lower(), 'id':cred.get('client_id',''),
                   'user':cred.get('user_name',''), 'password':cred.get('pass_word','')}
            if kind == 'CL':
                rows.append([name, 'strategy', row['id'], row['user'], row['password']])
            else:
                rows.extend(dict_rows(HEADERS.get(kind, DEVICE), [row]))
        groups[kind] = names
        write(f'{count}_{"策略" if kind=="CL" else kind.lower()}.csv', HEADERS.get(kind, DEVICE), rows)

    shared = []
    for binding in project.get('sharedBindings', []):
        name = binding['platformName']
        cred = credentials(binding)
        if binding.get('credentialStatus')=='template_unverified': pending.append(name)
        shared.append([name,'default',cred.get('client_id',name),cred.get('user_name',name),cred.get('pass_word',name),'',''])
    if shared: write(f'{len(shared)}_shared-channel.csv',DEVICE,shared)
    warnings = list(project.get('templateMappingWarnings', []))

    def relate(parent, child, filename, tag):
        parents, children = groups.get(parent, []), groups.get(child, [])
        if not children:
            return
        if not parents:
            warnings.append(f'{filename} 未生成：缺少 {parent}，请补充资产模板。')
            return
        rows, offset = [], 0
        base, extra = divmod(len(children), len(parents))
        for i, name in enumerate(parents):
            for target in children[offset:offset+base+(i<extra)]:
                rows.append(['ASSET' if parent in ('Company','powerStation','energyStationUnit') else 'DEVICE', name,
                             'ASSET' if child in ('Company','powerStation','energyStationUnit') else 'DEVICE', target,
                             'Contains','COMMON',json.dumps({'type':tag},ensure_ascii=False)])
            offset += base+(i<extra)
        write(filename+'_relation.csv', RELATION, rows)

    if project.get('profile') == 'microgrid':
        for spec in project['types']:
            relate('powerStation',spec['type'],spec['type'].lower()+'_station',spec['type'])
    else:
        for args in [('Company','powerStation','company_station','Station'),
                     ('powerStation','energyStationUnit','chuneng_station','Container'),
                     ('energyStationUnit','PCS','pcs-chuneng','PCS'),('energyStationUnit','BMS','bms-chuneng','BMS'),
                     ('BMS','GROUPBMS','bms-groupbms','GroupBMS'),('GROUPBMS','CELL','cell-groupbms','Cell'),
                     ('powerStation','BOX','box_station','BOX'),('powerStation','CL','strategy_station','strategy')]:
            relate(*args)
    manifest = {'projectId':project['id'],'revision':project['revision'],'kind':'asset-device-template',
                'pendingCredentials':pending,'warnings':warnings,'files':list(files),
                'notes':['先导入资产，再导入设备，最后导入关系。',
                         '这是待核对的导入模板，不表示平台校验通过；导入已有设备前请确认平台的重复资源处理方式。',
                         '现场属性和 stationId 留空，请按目标平台补充；关系按当前配置均衡分配，BMS 按采集序号配对，导入前核对。',
                         '查询不到的 MQTT Client ID、用户名、密码按示例设为设备名。已绑定设备保留本地真实凭据。模板凭据须导入平台并核对后才能上报。',
                         '策略 CSV 沿用示例的重复 name 列及 pawd 列；微电网 ESS/INV 使用通用设备列。']}
    files['import-manifest.json'] = json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()

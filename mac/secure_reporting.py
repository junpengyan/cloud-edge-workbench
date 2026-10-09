"""Only the independent copy uses protected cloud execution reports."""
import json
from pathlib import Path
from workspace import crypt, atomic
from app.services.reporting import result_to_output_dict

def write_reports(result,output_dir:Path):
    output_dir.mkdir(parents=True,exist_ok=True)
    data=result_to_output_dict(result)
    (output_dir/"entities.dpapi").write_bytes(crypt(json.dumps(data,ensure_ascii=False).encode()))
    atomic(output_dir/"summary.json",{"runId":result.run_id,"status":result.status,"assets":len(result.assets),"devices":len(result.devices),"relations":len(result.relations),"edges":len(result.edges),"credentialStorage":"Windows DPAPI","platformVerified":False,"hasError":bool(result.error)})

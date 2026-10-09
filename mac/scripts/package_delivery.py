"""Build and verify a source-only delivery ZIP using an explicit file allowlist."""
import argparse
import ast
import hashlib
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    output = dist / f'cloud-edge-workbench-{stamp}.zip'
    roots = {'app', 'gateway_engine', 'static', 'templates', 'tests', 'scripts'}
    extensions = {'.py', '.cjs', '.js', '.html', '.css', '.svg', '.xml', '.csv', '.json', '.md', '.ps1', '.bat'}
    files = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(part in {'.git', 'data', 'dist', '__pycache__', '.venv', '.validation-deps', 'backups', 'node_modules'} for part in rel.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if len(rel.parts)>1 and rel.parts[0] not in roots:
            continue
        if path.suffix not in extensions and rel.as_posix() not in {'requirements.txt', '.gitignore'}:
            continue
        files[rel.as_posix()] = path.read_bytes()
    required = {'asset_templates.py','server.py','workspace.py','requirements.txt','安装依赖.bat','启动工作台.bat','停止工作台.bat','交付说明.md','tests/test_template_workflow.py'}
    if not required <= files.keys():
        raise RuntimeError('Missing required delivery files')
    for name, data in files.items():
        if name.endswith('.py'):
            ast.parse(data, filename=name)
    with tempfile.TemporaryDirectory(prefix='workbench-delivery-') as temp:
        temp = Path(temp)
        preliminary = temp / 'source.zip'
        with zipfile.ZipFile(preliminary,'w',zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr('cloud-edge-workbench/'+name,data)
        with zipfile.ZipFile(preliminary) as archive:
            if archive.testzip(): raise RuntimeError('ZIP integrity failure')
            archive.extractall(temp / 'extracted')
        extracted = temp / 'extracted' / 'cloud-edge-workbench'
        results = []
        commands = [
            [sys.executable,'-m','unittest','discover','-s','tests','-p','test_template_workflow.py','-v'],
            [args.node,'tests/test_workflow_ui.cjs'],
            [args.node,'--check','static/app.js'],
            [args.node,'--check','static/deployment.js'],
        ]
        for command in commands:
            result = subprocess.run(command,cwd=extracted,capture_output=True,text=True,encoding='utf-8',errors='replace')
            results.append(' '.join(command[1:])+'\n'+result.stdout+result.stderr)
            if result.returncode:
                raise RuntimeError(results[-1])
        report = '交付检查结果\n生成时间：'+stamp+'\n\n'
        report += '检查通过：全部 Python 源文件语法；ZIP 完整性；解压副本执行以下检查。\n'
        report += '仅执行针对性离线测试，未执行完整依赖环境测试、真实平台/网关连接或 Windows DPAPI 验收。\n'
        report += '不包含 data、真实账号凭据、虚拟环境、历史工程包及旧交付包。\n\n'
        report += '\n'.join(results)
        files['交付检查结果.txt'] = report.encode('utf-8-sig')
    manifest = ''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items()))
    files['SHA256SUMS.txt'] = manifest.encode('utf-8')
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():
            archive.writestr('cloud-edge-workbench/'+name,data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip(): raise RuntimeError('Final ZIP integrity failure')
        for name,data in files.items():
            if archive.read('cloud-edge-workbench/'+name)!=data:
                raise RuntimeError('Final ZIP content mismatch: '+name)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(checksum+'  '+output.name+'\n',encoding='utf-8')
    print(f'ZIP: {output}\nFiles: {len(files)}\nBytes: {output.stat().st_size}\nSHA256: {checksum}')


if __name__ == '__main__':
    main()

"""Redacted pre-publication check of the Git index and reachable history.

Without Git, --tree scans only candidate public files; it does not certify
history or repository settings. Pattern scanning is not a security guarantee.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'AWS credential': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'GitHub credential': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'webhook credential': r'https?://[^\s\x22\x27<>]+/(?:trading/)?webhook/[A-Za-z0-9_-]{12,}',
    'broker account ID': r'\b(?:DU|U|F)[0-9]{5,12}\b',
    'personal path': r'(?i)(?:C:[/\\]Users[/\\][^\s\x22\x27<>]+|/ho' + r'me/[^\s\x22\x27<>]+)',
}


def private_path(path):
    p = Path(path);parts=set(p.parts);name=p.name.lower()
    return (bool(parts & {'.venv','venv','__pycache__','logs','reports','.git'})
            or (name.startswith('.env') and name != '.env.example')
            or name.endswith(('.log','.jsonl','.pem','.key','.p12','.pfx','.pkl','.zip','.bak','.backup','.db','.sqlite','.sqlite3'))
            or name.startswith('bar_cache')
            or ('data' in parts and p.suffix.lower() == '.csv'))


def findings(path, content):
    if private_path(path):
        return [(path,0,'private/runtime/data file')]
    try:text=content.decode('utf-8-sig')
    except UnicodeDecodeError:return [(path,0,'unreviewed binary file')]
    results=[]
    for line_num,line in enumerate(text.splitlines(),1):
        for label,pattern in PATTERNS.items():
            if re.search(pattern,line):results.append((path,line_num,label))
        # Detect environment/template literal values; never report the value.
        match=re.match(r'\s*(?:RISK_(?:SERVER|ADMIN)_API_KEY|TV_WEBHOOK_SECRET|TP_WEBHOOK_\w+)\s*=\s*(.*)',line)
        if match and match[1].strip().strip('\x22\x27'):
            results.append((path,line_num,'nonempty credential assignment'))
        quoted=re.search(r'(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*[\x22\x27]([^\x22\x27]{24,})[\x22\x27]',line)
        if quoted and len(set(quoted[1]))>8 and not any(v in quoted[1].lower() for v in ('example','replace','your_','your-','paste','getenv','environ')):
            results.append((path,line_num,'possible embedded credential'))
    return results


def git(*args):
    return subprocess.run(['git','-C',str(ROOT),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout


def scan(tree=False):
    issues=[];checked=0
    if tree:
        for p in ROOT.rglob('*'):
            rel=p.relative_to(ROOT).as_posix()
            if not p.is_file() or private_path(rel):continue
            if p.is_symlink():issues.append((rel,0,'symlink'));continue
            issues.extend(findings(rel,p.read_bytes()));checked+=1
    else:
        # Index entries include already-tracked ignored files; .gitignore alone
        # never removes an existing secret from the index or commit history.
        for entry in git('ls-files','--stage','-z').split(b'\0'):
            if not entry:continue
            header,path=entry.split(b'\t',1);mode,oid,stage=header.split()
            label=path.decode('utf-8')
            if mode not in (b'100644',b'100755'):
                issues.append((label,0,'symlink/submodule/unmerged file'));continue
            issues.extend(findings(label,git('cat-file','blob',oid.decode())));checked+=1
        for record in git('rev-list','--objects','--all').splitlines():
            oid,_,path=record.partition(b' ')
            if not path:continue
            if git('cat-file','-t',oid.decode()).strip()!=b'blob':continue
            issues.extend(findings('history:'+path.decode('utf-8'),git('cat-file','blob',oid.decode())));checked+=1
    return checked,sorted(set(issues))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--tree',action='store_true');args=ap.parse_args()
    try:checked,issues=scan(args.tree)
    except (subprocess.CalledProcessError,FileNotFoundError) as exc:
        print('Cannot verify Git index/history; initialize the repository or use --tree for a limited candidate-file scan.')
        return 2
    for path,line,label in issues:print(f'{path}:{line}: {label} [value redacted]')
    print(f'Checked {checked} file/blob entries; {len(issues)} findings. '+('Candidate tree only; history not checked.' if args.tree else 'Index and reachable history checked.'))
    return int(bool(issues))


if __name__=='__main__':sys.exit(main())

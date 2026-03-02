import ast, sys
from pathlib import Path

agent_dir = Path(r'C:\Users\z5866\Documents\amingclaw\aming_claw\agent')
errors = []
for f in sorted(agent_dir.glob('*.py')):
    try:
        ast.parse(f.read_text(encoding='utf-8'))
        print(f'  OK  {f.name}')
    except SyntaxError as e:
        print(f'  ERR {f.name}:{e.lineno} {e.msg}')
        errors.append(f.name)

if errors:
    print(f'\nFailed: {errors}')
    sys.exit(1)
else:
    print('\nAll agent/*.py syntax OK')

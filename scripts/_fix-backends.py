import re

path = r'C:\Users\z5866\Documents\amingclaw\aming_claw\agent\backends.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find all lines with unescaped " inside string literals in the guard blocks
# Pattern: lines like "禁止回复"收到/明白/后续执行"等确认语。\n"
# where the inner " are unescaped ASCII quotes

old = '"禁止回复"收到/明白/后续执行"等确认语。\\n"'
new = '"禁止回复\\"收到/明白/后续执行\\"等确认语。\\n"'

count = content.count(old)
print(f'Found {count} occurrences of the broken pattern')
content = content.replace(old, new)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done. Verifying...')
import ast
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()
try:
    ast.parse(src)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Still broken: {e}')

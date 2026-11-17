#!/usr/bin/env python3
"""Stand-in for readpst in the tests. The ".pst" it is given is JSON:

    {"root": "Outlook Data File",
     "folders": {"Inbox": ["<raw message>", ...], "Projects/2026": [...]},
     "exit": 0, "symlink": false}

It writes the same layout `readpst -M -o OUT` does: OUT/<root>/<folder>/<n>,
numbered from 1. "{BOUNDARY}" in a message becomes a fresh random MIME
boundary on every run, like readpst's own.
"""
import json
import os
import secrets
import sys

args = sys.argv[1:]
out = args[args.index('-o') + 1]
pst = args[-1]
with open(pst) as f:
    spec = json.load(f)
if spec.get('exit'):
    sys.stderr.write('Error opening File\n')
    sys.exit(spec['exit'])
root = os.path.join(out, spec.get('root', 'Outlook Data File'))
for folder, msgs in spec.get('folders', {}).items():
    d = os.path.join(root, *folder.split('/'))
    os.makedirs(d, exist_ok=True)
    for i, m in enumerate(msgs, 1):
        b = 'boundary-LibPST-iamunique-' + secrets.token_hex(8)
        with open(os.path.join(d, str(i)), 'w', newline='') as f:
            f.write(m.replace('{BOUNDARY}', b))
if spec.get('symlink'):
    os.symlink('/etc/passwd', os.path.join(root, 'Inbox', '999'))

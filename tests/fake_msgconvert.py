#!/usr/bin/env python3
"""Stand-in for msgconvert (libemail-outlook-message-perl) in the tests.

Called like the real one: `msgconvert --outfile OUT.eml IN.msg`. The fake
".msg" is what tests/test_mail_import.py `make_msg()` writes: the OLE
compound-file magic, an MSG stream name in UTF-16, then a line
"--EML--" followed by the RFC 822 message the real tool would produce.
A file containing "--FAIL--" makes it exit 1 the way a damaged .msg does.
"""
import sys

args = sys.argv[1:]
out = args[args.index('--outfile') + 1]
src = args[-1]
with open(src, 'rb') as f:
    data = f.read()
if b'--FAIL--' in data:
    sys.stderr.write('Not a valid OLE file\n')
    sys.exit(1)
marker = b'\n--EML--\n'
with open(out, 'wb') as f:
    f.write(data[data.index(marker) + len(marker):])

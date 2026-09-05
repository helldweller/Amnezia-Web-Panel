"""Syntax check for the JavaScript that lives inside the Jinja templates.

The page tests assert that particular handlers and elements are present, which
says nothing about the file still parsing: a merge that lands two blocks at the
same anchor can swallow a closing brace and every one of those assertions still
passes. `node --check` catches exactly that, so it runs here whenever node is
available (it is on the CI image; the test skips without it).
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, 'templates')
INLINE_SCRIPT_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S)


def inline_js(html):
    """Inline script bodies with the Jinja placeholders neutralised: `{{ x }}`
    becomes a bare identifier (valid both as a value and inside a string) and
    `{% ... %}` tags drop out."""
    js = '\n'.join(INLINE_SCRIPT_RE.findall(html))
    js = re.sub(r'\{\{.*?\}\}', 'JINJA', js, flags=re.S)
    return re.sub(r'\{%.*?%\}', '', js, flags=re.S)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TemplateJsSyntaxTests(unittest.TestCase):
    def test_every_template_script_parses(self):
        checked = []
        for path in sorted(glob.glob(os.path.join(TEMPLATES, '*.html'))):
            with open(path, encoding='utf-8') as f:
                js = inline_js(f.read())
            if not js.strip():
                continue
            name = os.path.basename(path)
            checked.append(name)
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as tmp:
                tmp.write(js)
                tmp_path = tmp.name
            try:
                result = subprocess.run(['node', '--check', tmp_path], capture_output=True, text=True)
            finally:
                os.unlink(tmp_path)
            self.assertEqual(result.returncode, 0, f'{name} does not parse:\n{result.stderr}')
        # the extraction itself must keep working
        self.assertIn('server.html', checked)
        self.assertGreaterEqual(len(checked), 5, f'only {checked} carried inline JS')


if __name__ == '__main__':
    unittest.main()

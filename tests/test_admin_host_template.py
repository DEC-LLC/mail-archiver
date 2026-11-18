"""1.3.4: the Host page's load sparkline. Jinja's `max` filter takes no
`default=` argument, so the page answered HTTP 500 as soon as three load
samples existed."""
import os
import re

import jinja2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sparkline_block():
    with open(os.path.join(ROOT, 'templates', 'admin_host.html'), encoding='utf-8') as f:
        text = f.read()
    start = text.index('{% if load_history and load_history|length >= 3 %}')
    end = text.index('{% endif %}', text.index('{% endfor %}', start)) + len('{% endif %}')
    block = text[start:end]
    # the block's own if needs its endif; keep whatever follows out of it
    return block if block.count('{% if') == block.count('{% endif %}') else block + '{% endif %}'


def _render(history):
    return jinja2.Environment().from_string(_sparkline_block()).render(load_history=history)


def test_no_default_argument_on_the_max_filter():
    with open(os.path.join(ROOT, 'templates', 'admin_host.html'), encoding='utf-8') as f:
        assert 'max(default' not in f.read()


def test_sparkline_renders_with_three_samples():
    out = _render([(1, 0.5), (2, 1.0), (3, 0.25)])
    assert out.count('class="bar"') == 3
    assert re.search(r'height: 40(\.0)?px', out)          # the largest value fills the height


def test_sparkline_renders_when_every_sample_is_missing_or_zero():
    assert _render([(1, None), (2, None), (3, None)]).count('class="bar"') == 0
    assert _render([(1, 0), (2, 0.0), (3, 0)]).count('class="bar"') == 3
